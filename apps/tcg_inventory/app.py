"""TCG Inventory -- local Pokémon card collection tracker.

Run with:

    python app.py

then open http://localhost:8000 in a browser. Single SQLite file, no
external services, no build step (server-rendered HTML + HTMX).
"""
from __future__ import annotations

import datetime as dt
import os
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlencode

from dotenv import load_dotenv
from fastapi import FastAPI, Form, HTTPException, Request, UploadFile
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func
from sqlalchemy.orm import Session, selectinload

import auth
import dropbox_client
import queries
from db import SessionLocal, init_db
from importer import import_dex_csv_files
from models import Binder, Card, Collection, ImportLog, SetReleaseOrder, Transaction

APP_DIR = Path(__file__).resolve().parent
load_dotenv(APP_DIR / ".env")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(title="TCG Inventory", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=APP_DIR / "static"), name="static")
templates = Jinja2Templates(directory=APP_DIR / "templates")
templates.env.filters["kr"] = lambda v: f"{v:,.0f} kr".replace(",", " ") if v is not None else "-"
templates.env.globals["auth_enabled"] = auth.is_configured


def _sort_url(
    request: Request,
    sort_param: str,
    dir_param: str,
    field: str,
    current_sort: str,
    current_dir: str,
    path: str = "/",
) -> str:
    """Build a link that sorts one table by `field`, toggling direction on
    repeat clicks, while preserving every other query param as-is (including
    other tables' own sort state on the same page).
    """
    next_dir = "desc" if current_sort == field and current_dir == "asc" else "asc"
    params = dict(request.query_params)
    params[sort_param] = field
    params[dir_param] = next_dir
    return path + "?" + urlencode(params)


templates.env.globals["sort_url"] = _sort_url

# Paths reachable without a session -- everything else needs a login once
# Supabase Auth is configured. Unconfigured (no SUPABASE_* env vars, e.g.
# local dev) leaves the app open, same as before this was added.
# /cron/dropbox-sync has its own separate auth (CRON_SECRET) -- a scheduled
# job has no browser session to log in with.
_PUBLIC_PATHS = {"/login", "/cron/dropbox-sync"}


@app.middleware("http")
async def auth_guard(request: Request, call_next):
    if not auth.is_configured() or request.url.path in _PUBLIC_PATHS or request.url.path.startswith("/static"):
        return await call_next(request)

    token = request.cookies.get(auth.SESSION_COOKIE)
    if token:
        try:
            auth.verify_access_token(token)
            return await call_next(request)
        except auth.AuthError:
            pass
    return RedirectResponse("/login", status_code=303)

SORT_COLUMNS = {
    "name": Card.name,
    "number": func.coalesce(Card.number_int, 999999),
    "series": Card.series,
    "set": Card.set,
    "reference_price": Card.reference_price,
    "qty": Card.qty,
    "rarity": Card.rarity,
    "illustrator": Card.illustrator,
}
# Cards not present in set_release_order (no research done for that set yet)
# sort after every known set, not before -- see SetReleaseOrder's docstring.
UNKNOWN_RELEASE_RANK = 999999

TOP_CARD_SORT_KEYS = {
    "name": lambda c: c.name.lower(),
    "number": lambda c: c.number_int if c.number_int is not None else 999999,
    "set": lambda c: (c.set or "").lower(),
    "reference_price": lambda c: c.reference_price or 0,
}


def _sorted_rows(rows, sort: str, direction: str, keys: dict):
    key_fn = keys.get(sort)
    if key_fn is None:  # no/unknown sort param -- keep the caller's default order
        return rows
    return sorted(rows, key=key_fn, reverse=(direction == "desc"))


# Inventory/Serie/Rarity's column headers only ever re-sort the deepest
# level -- the actual cards -- never the bucket rows themselves (collection,
# series, set, rarity always keep their default order from queries.py; see
# by_series_breakdown etc). This is deliberately a different key set from
# TOP_CARD_SORT_KEYS above: "unique" has no per-card equivalent to a bucket's
# unique_count, since a single card is always exactly 1 or 0.
CARD_LEAF_SORT_KEYS = {
    "name": lambda c: c.name.lower(),
    "unique": lambda c: 1 if c.qty > 0 else 0,
    "duplicates": lambda c: c.duplicates,
    "qty": lambda c: c.qty,
    "value": lambda c: c.unique_value,
    "total_value": lambda c: c.total_value,
}


def _sort_cards_in_buckets(buckets, sort: str, direction: str) -> None:
    """Sort each bucket's `.cards` list in place; buckets themselves are
    never reordered by this -- only what's nested inside them.
    """
    key_fn = CARD_LEAF_SORT_KEYS.get(sort)
    if key_fn is None:
        return
    reverse = direction == "desc"
    for bucket in buckets:
        bucket.cards.sort(key=key_fn, reverse=reverse)


def _sort_cards_in_series(series_list, sort: str, direction: str) -> None:
    """Same as `_sort_cards_in_buckets`, but for series -> set -> cards: the
    series and set rows both stay in their default order, only the cards
    inside each set move.
    """
    key_fn = CARD_LEAF_SORT_KEYS.get(sort)
    if key_fn is None:
        return
    reverse = direction == "desc"
    for series in series_list:
        for set_bucket in series.sets:
            set_bucket.cards.sort(key=key_fn, reverse=reverse)


def get_db_session() -> Session:
    return SessionLocal()


# --------------------------------------------------------------------------
# Dashboard
# --------------------------------------------------------------------------
@app.get("/")
def dashboard(
    request: Request,
    csort: str = "value",
    cdir: str = "desc",
    ssort: str = "value",
    sdir: str = "desc",
    rsort: str = "value",
    rdir: str = "desc",
    tsort: str = "reference_price",
    tdir: str = "desc",
):
    db = get_db_session()
    try:
        headline = queries.headline_summary(db)
        collection_breakdown = queries.collection_bulk_breakdown(db)
        series_breakdown = queries.by_series_breakdown(db)
        top_cards = queries.top_valuable_cards(db, limit=10)
        rarity_breakdown = queries.by_rarity_breakdown(db)

        # Bucket rows (collection, series, set, rarity) always keep their
        # default order from queries.py -- clicking a column header only
        # re-sorts the cards nested inside each bucket, never the buckets
        # themselves.
        collection_rows = collection_breakdown["children"] + [collection_breakdown["bulk"]]
        _sort_cards_in_buckets(collection_rows, csort, cdir)
        _sort_cards_in_series(series_breakdown, ssort, sdir)
        _sort_cards_in_buckets(rarity_breakdown, rsort, rdir)
        top_cards = _sorted_rows(top_cards, tsort, tdir, TOP_CARD_SORT_KEYS)

        # Highlights for the KPI row -- the single most valuable named
        # collection/series (Bulk isn't a collection, so excluded). The
        # collection highlight ranks by unique_value (not total_value) so
        # duplicates can't inflate which collection looks "most valuable".
        top_collection = max(collection_breakdown["children"], key=lambda b: b.unique_value, default=None)
        top_series = max(series_breakdown, key=lambda b: b.total_value, default=None)

        return templates.TemplateResponse(
            request,
            "dashboard.html",
            {
                "headline": headline,
                "collection_breakdown": collection_breakdown,
                "collection_rows": collection_rows,
                "series_breakdown": series_breakdown,
                "top_cards": top_cards,
                "rarity_breakdown": rarity_breakdown,
                "top_collection": top_collection,
                "top_series": top_series,
                "csort": csort,
                "cdir": cdir,
                "ssort": ssort,
                "sdir": sdir,
                "rsort": rsort,
                "rdir": rdir,
                "tsort": tsort,
                "tdir": tdir,
            },
        )
    finally:
        db.close()


# --------------------------------------------------------------------------
# Inventory
# --------------------------------------------------------------------------
def _apply_inventory_filters(db: Session, q, series, set_, collection, binder, dup, rarity):
    query = db.query(Card).options(selectinload(Card.collections), selectinload(Card.binder))
    if q:
        like = f"%{q.lower()}%"
        query = query.filter(
            func.lower(Card.name).like(like)
            | func.lower(func.coalesce(Card.card_id, "")).like(like)
            | func.lower(func.coalesce(Card.number, "")).like(like)
            | func.lower(func.coalesce(Card.illustrator, "")).like(like)
        )
    if series:
        query = query.filter(Card.series == series)
    if set_:
        query = query.filter(Card.set == set_)
    if binder:
        query = query.join(Card.binder).filter(Binder.name == binder)
    if collection:
        query = query.filter(Card.collections.any(Collection.name == collection))
    if dup:
        query = query.filter(Card.qty > 1)  # duplicates = max(qty - 1, 0)
    if rarity:
        query = query.filter(Card.rarity == rarity)
    return query


@app.get("/inventory")
def inventory(
    request: Request,
    q: str = "",
    series: str = "",
    set: str = "",
    collection: str = "",
    binder: str = "",
    dup: bool = False,
    rarity: str = "",
    sort: str = "release",
    direction: str = "asc",
):
    db = get_db_session()
    try:
        query = _apply_inventory_filters(db, q, series, set, collection, binder, dup, rarity)
        number_sort = func.coalesce(Card.number_int, 999999)

        if sort == "release":
            # Default: actual print order -- Base Set #1 first, etc. Sets
            # with no research done yet (no set_release_order row) sort
            # after every known set rather than before (see UNKNOWN_RELEASE_RANK).
            query = query.outerjoin(
                SetReleaseOrder,
                (SetReleaseOrder.series == Card.series) & (SetReleaseOrder.set == Card.set),
            )
            release_rank = func.coalesce(SetReleaseOrder.release_rank, UNKNOWN_RELEASE_RANK)
            rank_col = release_rank.desc() if direction == "desc" else release_rank.asc()
            order_cols = [rank_col, Card.set.asc(), number_sort.asc()]
        else:
            sort_col = SORT_COLUMNS.get(sort, Card.name)
            sort_col = sort_col.desc() if direction == "desc" else sort_col.asc()
            order_cols = [sort_col] if sort == "number" else [sort_col, number_sort.asc()]

        cards = query.order_by(*order_cols).all()

        all_series = [r[0] for r in db.query(Card.series).filter(Card.series.isnot(None)).distinct().order_by(Card.series)]
        all_sets = [r[0] for r in db.query(Card.set).filter(Card.set.isnot(None)).distinct().order_by(Card.set)]
        all_collections = [r[0] for r in db.query(Collection.name).distinct().order_by(Collection.name)]
        all_binders = [r[0] for r in db.query(Binder.name).distinct().order_by(Binder.name)]

        context = {
            "cards": cards,
            "total": len(cards),
            "q": q,
            "series": series,
            "set": set,
            "collection": collection,
            "binder": binder,
            "dup": dup,
            "rarity": rarity,
            "sort": sort,
            "direction": direction,
            "all_series": all_series,
            "all_sets": all_sets,
            "all_collections": all_collections,
            "all_binders": all_binders,
        }
        is_htmx = bool(request.headers.get("HX-Request"))
        if not is_htmx:
            # The KPI module lives outside the htmx-swapped #inventory-results
            # target, so only compute it on a full page load, not on every
            # filter keystroke/select change.
            collection_breakdown = queries.collection_bulk_breakdown(db)
            series_breakdown = queries.by_series_breakdown(db)
            context.update(
                {
                    "headline": queries.headline_summary(db),
                    "top_cards": queries.top_valuable_cards(db, limit=10),
                    "top_collection": max(
                        collection_breakdown["children"], key=lambda b: b.unique_value, default=None
                    ),
                    "top_series": max(series_breakdown, key=lambda b: b.total_value, default=None),
                }
            )
        template = "partials/inventory_table.html" if is_htmx else "inventory.html"
        return templates.TemplateResponse(request, template, context)
    finally:
        db.close()


# --------------------------------------------------------------------------
# Transactions -- also shows when each card was first imported (merged from
# the former standalone "Lagt til" page, since the two were always used
# together: see a newly-synced card, then register what it cost).
# --------------------------------------------------------------------------
TRANSACTION_SORT_KEYS = {
    "id": lambda t: t.id,
    "purchase_id": lambda t: t.purchase_id if t.purchase_id is not None else -1,
    "date": lambda t: t.date,
    "type": lambda t: t.type,
    "name": lambda t: t.card.name.lower(),
    "variant": lambda t: (t.card.variant or "").lower(),
    "rarity": lambda t: (t.card.rarity or "").lower(),
    "series": lambda t: (t.card.series or "").lower(),
    "set": lambda t: (t.card.set or "").lower(),
    "number": lambda t: t.card.number_int if t.card.number_int is not None else 999999,
    "price": lambda t: t.price,
    "platform": lambda t: (t.platform or "").lower(),
    "fees": lambda t: t.fees if t.fees is not None else -1,
}


def _recently_added_cards(db):
    return (
        db.query(Card)
        .filter(Card.created_at.isnot(None))
        .order_by(Card.created_at.desc(), Card.id.desc())
        .limit(100)
        .all()
    )


def _cards_grouped_by_added_date(db):
    cards = (
        db.query(Card)
        .order_by(Card.created_at.desc().nullslast(), Card.id.desc())
        .all()
    )
    known_count = sum(1 for c in cards if c.created_at is not None)

    # Group consecutive cards under the same calendar date -- cheap since
    # `cards` is already sorted by created_at desc; unknown-date cards
    # (created_at is None, pre-dates this column) form their own trailing
    # group.
    groups: list[dict] = []
    for card in cards:
        label = card.created_at.date().isoformat() if card.created_at else "Ukjent dato"
        if not groups or groups[-1]["label"] != label:
            groups.append({"label": label, "cards": []})
        groups[-1]["cards"].append(card)
    return groups, known_count, len(cards)


def _transactions_context(db, request: Request, tsort: str, tdir: str, error: str | None = None) -> dict:
    txs = (
        db.query(Transaction)
        .options(selectinload(Transaction.card))
        .order_by(Transaction.date.desc(), Transaction.id.desc())
        .all()
    )
    txs = _sorted_rows(txs, tsort, tdir, TRANSACTION_SORT_KEYS)
    groups, known_count, total_count = _cards_grouped_by_added_date(db)
    return {
        "transactions": txs,
        "error": error,
        "today": dt.date.today().isoformat(),
        "recent_cards": _recently_added_cards(db),
        "tsort": tsort,
        "tdir": tdir,
        "groups": groups,
        "known_count": known_count,
        "total_count": total_count,
    }


@app.get("/transactions")
def list_transactions(request: Request, tsort: str = "date", tdir: str = "desc"):
    db = get_db_session()
    try:
        return templates.TemplateResponse(
            request, "transactions.html", _transactions_context(db, request, tsort, tdir)
        )
    finally:
        db.close()


@app.get("/transactions/card-search")
def card_search(request: Request, q: str = ""):
    db = get_db_session()
    try:
        results = []
        if q and len(q) >= 2:
            like = f"%{q.lower()}%"
            results = (
                db.query(Card)
                .filter(func.lower(Card.name).like(like) | func.lower(Card.card_id).like(like))
                .order_by(Card.name)
                .limit(20)
                .all()
            )
        return templates.TemplateResponse(
            request, "partials/card_search_results.html", {"results": results}
        )
    finally:
        db.close()


@app.post("/transactions")
def create_transaction(
    request: Request,
    card_id: int = Form(...),
    type: str = Form(...),
    date: str = Form(...),
    price: float = Form(...),
    platform: str = Form(""),
    fees: float | None = Form(None),
    purchase_id: int | None = Form(None),
):
    db = get_db_session()
    try:
        card = db.query(Card).filter(Card.id == card_id).one_or_none()
        if card is None:
            return templates.TemplateResponse(
                request,
                "transactions.html",
                _transactions_context(
                    db, request, "date", "desc", error="Fant ikke kortet -- velg et kort fra søkeresultatene."
                ),
            )

        tx = Transaction(
            card_id=card.id,
            type=type,
            date=dt.date.fromisoformat(date),
            price=price,
            platform=platform or None,
            fees=fees,
            purchase_id=purchase_id,
        )
        db.add(tx)
        db.commit()
        return RedirectResponse("/transactions", status_code=303)
    finally:
        db.close()


# --------------------------------------------------------------------------
# CSV import / sync
# --------------------------------------------------------------------------
def _recent_import_logs(db: Session, limit: int = 20) -> list[ImportLog]:
    return db.query(ImportLog).order_by(ImportLog.ran_at.desc(), ImportLog.id.desc()).limit(limit).all()


@app.get("/import")
def import_form(request: Request):
    db = get_db_session()
    try:
        return templates.TemplateResponse(
            request, "import.html", {"result": None, "logs": _recent_import_logs(db)}
        )
    finally:
        db.close()


@app.post("/import")
async def run_import(request: Request, files: list[UploadFile], full_load: bool = Form(False)):
    payload = [(f.filename or "upload.csv", await f.read()) for f in files]
    db = get_db_session()
    try:
        result = import_dex_csv_files(db, payload, full_load=full_load, source="manual")
        return templates.TemplateResponse(
            request, "import.html", {"result": result, "logs": _recent_import_logs(db)}
        )
    finally:
        db.close()


# --------------------------------------------------------------------------
# CSV import / sync -- straight from Dropbox
# --------------------------------------------------------------------------
@app.get("/import/dropbox/list")
def import_dropbox_list(request: Request, folder: str = ""):
    folder = folder or dropbox_client.default_folder()
    context = {"folder": folder, "files": None, "error": None}
    try:
        dbx = dropbox_client.build_client_from_env()
        context["files"] = dropbox_client.list_csv_files(dbx, folder)
    except dropbox_client.DropboxNotConfigured as exc:
        context["error"] = str(exc)
    except dropbox_client.DropboxImportError as exc:
        context["error"] = str(exc)
    return templates.TemplateResponse(request, "partials/dropbox_files.html", context)


@app.post("/import/dropbox/sync")
def import_dropbox_sync(
    request: Request,
    folder: str = Form(""),
    paths: list[str] = Form(default=[]),
    full_load: bool = Form(False),
):
    if not paths:
        return templates.TemplateResponse(
            request,
            "partials/dropbox_files.html",
            {"folder": folder, "files": None, "error": "Velg minst én fil å synke."},
        )
    db = get_db_session()
    try:
        dbx = dropbox_client.build_client_from_env()
        payload = [(path.rsplit("/", 1)[-1], dropbox_client.download_file(dbx, path)) for path in paths]
        result = import_dex_csv_files(db, payload, full_load=full_load, source="dropbox")
        return templates.TemplateResponse(request, "partials/import_result.html", {"result": result})
    except (dropbox_client.DropboxNotConfigured, dropbox_client.DropboxImportError) as exc:
        return templates.TemplateResponse(
            request,
            "partials/dropbox_files.html",
            {"folder": folder, "files": None, "error": str(exc)},
        )
    finally:
        db.close()


@app.get("/cron/dropbox-sync")
def cron_dropbox_sync(request: Request, secret: str = ""):
    """Scheduled sync, triggered by the Vercel Cron job in vercel.json.

    Pulls every CSV currently in the configured Dropbox folder and runs a
    normal (non-full-load) sync -- a cron job runs unattended, so it never
    deletes cards, only flags missing ones (see importer.py). Protected by
    CRON_SECRET rather than the Supabase login: Vercel's cron invocations
    carry no browser session to log in with. Vercel automatically sends
    `Authorization: Bearer <CRON_SECRET>` on cron requests when that env
    var is set -- see README "Automatic daily sync". A manual trigger (e.g.
    from a tool that can't set custom headers) may instead pass the same
    value as `?secret=`.
    """
    cron_secret = os.environ.get("CRON_SECRET", "")
    authorized = not cron_secret or request.headers.get("authorization") == f"Bearer {cron_secret}" or secret == cron_secret
    if not authorized:
        raise HTTPException(status_code=401, detail="Unauthorized")

    folder = dropbox_client.default_folder()
    db = get_db_session()
    try:
        dbx = dropbox_client.build_client_from_env()
        files = dropbox_client.list_csv_files(dbx, folder)
        if not files:
            return {"status": "ok", "folder": folder, "message": "No CSV files found"}
        payload = [(f.name, dropbox_client.download_file(dbx, f.path_lower)) for f in files]
        result = import_dex_csv_files(db, payload, full_load=False, source="cron")
        print(
            f"[cron/dropbox-sync] ok: files={[f.name for f in files]} "
            f"created={result.cards_created} updated={result.cards_updated} "
            f"flagged={result.cards_flagged_missing} "
            f"collections={sorted(result.collections_touched)} "
            f"binders={sorted(result.binders_touched)} "
            f"warnings={len(result.warnings)}"
        )
        return {
            "status": "ok",
            "folder": folder,
            "files_synced": [f.name for f in files],
            "cards_created": result.cards_created,
            "cards_updated": result.cards_updated,
            "cards_flagged_missing": result.cards_flagged_missing,
            "collections_touched": sorted(result.collections_touched),
            "binders_touched": sorted(result.binders_touched),
            "warnings": result.warnings,
        }
    except (dropbox_client.DropboxNotConfigured, dropbox_client.DropboxImportError) as exc:
        # Cron runs unattended -- nobody's watching a response body, so this
        # has to land in Vercel's runtime logs to be debuggable at all.
        print(f"[cron/dropbox-sync] failed: {exc}")
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    finally:
        db.close()


# --------------------------------------------------------------------------
# Auth (Supabase) -- only enforced when SUPABASE_* env vars are set
# --------------------------------------------------------------------------
def _cookie_secure() -> bool:
    # Vercel sets VERCEL=1 at runtime; treat that as "served over https".
    # Locally (no VERCEL var) cookies stay non-secure so http://localhost works.
    return bool(os.environ.get("VERCEL"))


@app.get("/login")
def login_form(request: Request):
    return templates.TemplateResponse(request, "login.html", {"error": None})


@app.post("/login")
def login_submit(request: Request, email: str = Form(...), password: str = Form(...)):
    try:
        tokens = auth.login(email, password)
    except auth.AuthError as exc:
        return templates.TemplateResponse(request, "login.html", {"error": str(exc)})

    response = RedirectResponse("/", status_code=303)
    response.set_cookie(
        auth.SESSION_COOKIE,
        tokens["access_token"],
        httponly=True,
        secure=_cookie_secure(),
        samesite="lax",
        max_age=tokens.get("expires_in", 3600),
    )
    return response


@app.get("/logout")
def logout():
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(auth.SESSION_COOKIE)
    return response


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app:app", host="127.0.0.1", port=8000, reload=False)
