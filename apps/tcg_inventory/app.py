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

from dotenv import load_dotenv
from fastapi import FastAPI, Form, Request, UploadFile
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
from models import Binder, Card, Collection, Transaction

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

# Paths reachable without a session -- everything else needs a login once
# Supabase Auth is configured. Unconfigured (no SUPABASE_* env vars, e.g.
# local dev) leaves the app open, same as before this was added.
_PUBLIC_PATHS = {"/login"}


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

PAGE_SIZE = 50

SORT_COLUMNS = {
    "name": Card.name,
    "number": Card.number,
    "series": Card.series,
    "set": Card.set,
    "reference_price": Card.reference_price,
    "qty": Card.qty,
    "rarity": Card.rarity,
    "illustrator": Card.illustrator,
}


def get_db_session() -> Session:
    return SessionLocal()


# --------------------------------------------------------------------------
# Dashboard
# --------------------------------------------------------------------------
@app.get("/")
def dashboard(request: Request):
    db = get_db_session()
    try:
        headline = queries.headline_summary(db)
        collection_breakdown = queries.collection_bulk_breakdown(db)
        series_breakdown = queries.by_series_breakdown(db)
        binder_breakdown = queries.by_binder_breakdown(db)
        top_cards = queries.top_valuable_cards(db, limit=10)
        quality = queries.data_quality(db)
        return templates.TemplateResponse(
            request,
            "dashboard.html",
            {
                "headline": headline,
                "collection_breakdown": collection_breakdown,
                "series_breakdown": series_breakdown,
                "binder_breakdown": binder_breakdown,
                "top_cards": top_cards,
                "quality": quality,
            },
        )
    finally:
        db.close()


# --------------------------------------------------------------------------
# Inventory
# --------------------------------------------------------------------------
def _apply_inventory_filters(db: Session, q, series, set_, collection, binder):
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
    return query


@app.get("/inventory")
def inventory(
    request: Request,
    q: str = "",
    series: str = "",
    set: str = "",
    collection: str = "",
    binder: str = "",
    sort: str = "name",
    direction: str = "asc",
    page: int = 1,
):
    db = get_db_session()
    try:
        query = _apply_inventory_filters(db, q, series, set, collection, binder)
        total = query.count()

        sort_col = SORT_COLUMNS.get(sort, Card.name)
        sort_col = sort_col.desc() if direction == "desc" else sort_col.asc()
        page = max(page, 1)
        cards = (
            query.order_by(sort_col, Card.number.asc())
            .offset((page - 1) * PAGE_SIZE)
            .limit(PAGE_SIZE)
            .all()
        )

        all_series = [r[0] for r in db.query(Card.series).filter(Card.series.isnot(None)).distinct().order_by(Card.series)]
        all_sets = [r[0] for r in db.query(Card.set).filter(Card.set.isnot(None)).distinct().order_by(Card.set)]
        all_collections = [r[0] for r in db.query(Collection.name).distinct().order_by(Collection.name)]
        all_binders = [r[0] for r in db.query(Binder.name).distinct().order_by(Binder.name)]

        context = {
            "cards": cards,
            "total": total,
            "page": page,
            "page_size": PAGE_SIZE,
            "total_pages": max((total + PAGE_SIZE - 1) // PAGE_SIZE, 1),
            "q": q,
            "series": series,
            "set": set,
            "collection": collection,
            "binder": binder,
            "sort": sort,
            "direction": direction,
            "all_series": all_series,
            "all_sets": all_sets,
            "all_collections": all_collections,
            "all_binders": all_binders,
        }
        template = "partials/inventory_table.html" if request.headers.get("HX-Request") else "inventory.html"
        return templates.TemplateResponse(request, template, context)
    finally:
        db.close()


# --------------------------------------------------------------------------
# Transactions
# --------------------------------------------------------------------------
@app.get("/transactions")
def list_transactions(request: Request):
    db = get_db_session()
    try:
        txs = (
            db.query(Transaction)
            .options(selectinload(Transaction.card))
            .order_by(Transaction.date.desc(), Transaction.id.desc())
            .all()
        )
        return templates.TemplateResponse(
            request,
            "transactions.html",
            {"transactions": txs, "error": None, "today": dt.date.today().isoformat()},
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
    card_id: str = Form(...),
    type: str = Form(...),
    date: str = Form(...),
    price: float = Form(...),
    platform: str = Form(""),
    fees: float | None = Form(None),
):
    db = get_db_session()
    try:
        card = db.query(Card).filter(Card.card_id == card_id.strip()).one_or_none()
        if card is None:
            txs = (
                db.query(Transaction)
                .options(selectinload(Transaction.card))
                .order_by(Transaction.date.desc(), Transaction.id.desc())
                .all()
            )
            return templates.TemplateResponse(
                request,
                "transactions.html",
                {
                    "transactions": txs,
                    "error": f"Fant ikke noe kort med Card ID '{card_id}'.",
                    "today": dt.date.today().isoformat(),
                },
            )

        tx = Transaction(
            card_id=card.id,
            type=type,
            date=dt.date.fromisoformat(date),
            price=price,
            platform=platform or None,
            fees=fees,
        )
        db.add(tx)
        db.commit()
        return RedirectResponse("/transactions", status_code=303)
    finally:
        db.close()


# --------------------------------------------------------------------------
# CSV import / sync
# --------------------------------------------------------------------------
@app.get("/import")
def import_form(request: Request):
    return templates.TemplateResponse(request, "import.html", {"result": None})


@app.post("/import")
async def run_import(request: Request, files: list[UploadFile], full_load: bool = Form(False)):
    payload = [(f.filename or "upload.csv", await f.read()) for f in files]
    db = get_db_session()
    try:
        result = import_dex_csv_files(db, payload, full_load=full_load)
        return templates.TemplateResponse(request, "import.html", {"result": result})
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
        result = import_dex_csv_files(db, payload, full_load=full_load)
        return templates.TemplateResponse(request, "partials/import_result.html", {"result": result})
    except (dropbox_client.DropboxNotConfigured, dropbox_client.DropboxImportError) as exc:
        return templates.TemplateResponse(
            request,
            "partials/dropbox_files.html",
            {"folder": folder, "files": None, "error": str(exc)},
        )
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
