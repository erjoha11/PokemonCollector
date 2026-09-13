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
import charts
import dropbox_client
import queries
from db import SessionLocal, init_db
from importer import import_dex_csv_files
from models import (
    Binder,
    Card,
    Collection,
    FavoritePokemon,
    ImportLog,
    PokemonAlias,
    SetReleaseOrder,
    Transaction,
)

APP_DIR = Path(__file__).resolve().parent
load_dotenv(APP_DIR / ".env")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(title="TCG Inventory", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=APP_DIR / "static"), name="static")
templates = Jinja2Templates(directory=APP_DIR / "templates")


def _format_kr(v: float | None) -> str:
    return f"{v:,.0f} kr".replace(",", " ") if v is not None else "-"


templates.env.filters["kr"] = _format_kr
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
    "language": Card.language,
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


# Shared by CARD_LEAF_SORT_KEYS and POKEMON_BUCKET_SORT_KEYS below: both
# Card and Bucket expose these same attribute names, so these three sort the
# same way regardless of which kind of row they're given.
_COMMON_ROW_SORT_KEYS = {
    "duplicates": lambda row: row.duplicates,
    "qty": lambda row: row.qty,
    "value": lambda row: row.unique_value,
}

# Inventory/Serie/Rarity's column headers only ever re-sort the deepest
# level -- the actual cards -- never the bucket rows themselves (collection,
# series, set, rarity always keep their default order from queries.py; see
# by_series_breakdown etc). This is deliberately a different key set from
# TOP_CARD_SORT_KEYS above: "unique" has no per-card equivalent to a bucket's
# unique_count, since a single card is always exactly 1 or 0.
CARD_LEAF_SORT_KEYS = {
    "name": lambda c: c.name.lower(),
    "unique": lambda c: 1 if c.qty > 0 else 0,
    **_COMMON_ROW_SORT_KEYS,
    "total_value": lambda c: c.total_value,
}

# Unlike CARD_LEAF_SORT_KEYS (individual cards nested in a bucket), this
# sorts the Pokemon *buckets* themselves -- the top-10-by-unique cutoff
# needs to rank buckets before any column click ever happens.
POKEMON_BUCKET_SORT_KEYS = {
    "name": lambda b: b.name.lower(),
    "unique": lambda b: b.unique_count,
    **_COMMON_ROW_SORT_KEYS,
}


def _sort_cards_in_buckets(buckets, sort: str, direction: str) -> None:
    """Sort every bucket's `.cards` list in place, recursing into any nested
    `.child_sets` (only series buckets have these -- a Bucket is a
    self-similar tree, one level deep at most in practice). Buckets/series/
    sets themselves are never reordered by this, only what's nested inside.
    """
    key_fn = CARD_LEAF_SORT_KEYS.get(sort)
    if key_fn is None:
        return
    reverse = direction == "desc"

    def _apply(bucket_list):
        for bucket in bucket_list:
            bucket.cards.sort(key=key_fn, reverse=reverse)
            if bucket.child_sets:
                _apply(bucket.child_sets)

    _apply(buckets)


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
    psort: str = "unique",
    pdir: str = "desc",
    fsort: str = "name",
    fdir: str = "asc",
    tsort: str = "reference_price",
    tdir: str = "desc",
):
    db = get_db_session()
    try:
        # Loaded once and threaded through every breakdown below, instead of
        # each of the five re-querying the whole `cards` table itself.
        cards = queries.all_cards_with_collections(db)
        alias_map = queries.pokemon_alias_map(db)

        headline = queries.headline_summary(db, cards)
        collection_breakdown = queries.collection_bulk_breakdown(db, cards)
        series_breakdown = queries.by_series_breakdown(db, cards)
        top_cards = queries.top_valuable_cards(db, limit=10)
        rarity_breakdown = queries.by_rarity_breakdown(db, cards)

        # Bucket rows (collection, series, set, rarity) always keep their
        # default order from queries.py -- clicking a column header only
        # re-sorts the cards nested inside each bucket, never the buckets
        # themselves.
        collection_rows = collection_breakdown["children"] + [collection_breakdown["bulk"]]
        _sort_cards_in_buckets(collection_rows, csort, cdir)
        _sort_cards_in_buckets(series_breakdown, ssort, sdir)
        _sort_cards_in_buckets(rarity_breakdown, rsort, rdir)
        top_cards = _sorted_rows(top_cards, tsort, tdir, TOP_CARD_SORT_KEYS)

        # Pokemon is different from the other breakdowns: it's a flat top-10
        # (by unique prints owned, the fixed cutoff), and a column click
        # re-orders those same 10 buckets -- same pattern as "Topp 10 mest
        # verdifulle kort" above, not the bucket-hierarchy tables.
        all_pokemon = queries.by_pokemon_breakdown(db, cards, alias_map)
        pokemon_top = sorted(all_pokemon, key=lambda b: b.unique_count, reverse=True)[:10]
        pokemon_top = _sorted_rows(pokemon_top, psort, pdir, POKEMON_BUCKET_SORT_KEYS)

        # Favorited Pokemon always show here regardless of the top-10 cutoff
        # above -- that's the whole point of favoriting one that isn't
        # already in your most-unique-prints list. Sortable the same way as
        # the Topp 10 table above it (same column set, same POKEMON_BUCKET_
        # SORT_KEYS), just with its own independent sort state.
        favorite_names = queries.favorite_pokemon_names(db)
        favorite_breakdown = sorted(
            (b for b in all_pokemon if b.name in favorite_names), key=lambda b: b.name.lower()
        )
        favorite_breakdown = _sorted_rows(favorite_breakdown, fsort, fdir, POKEMON_BUCKET_SORT_KEYS)

        # For the "combine Pokemon" form: every raw printed name (pre-alias)
        # to autocomplete from -- derived from the cards already loaded above
        # rather than a fresh query. Aliases are similarly derived from the
        # alias_map already loaded above, ordered by folder (canonical_name)
        # first so the template's `groupby` filter (which just walks
        # consecutive rows) produces one group per folder.
        all_pokemon_names = sorted({c.name for c in cards})
        pokemon_aliases = sorted(
            ({"name": name, "canonical_name": canonical} for name, canonical in alias_map.items()),
            key=lambda a: (a["canonical_name"], a["name"]),
        )

        # Highlights for the KPI row -- the single most valuable named
        # collection/series (Bulk isn't a collection, so excluded). The
        # collection highlight ranks by unique_value (not total_value) so
        # duplicates can't inflate which collection looks "most valuable".
        top_collection, top_series = _top_collection_and_series(collection_breakdown, series_breakdown)

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
                "pokemon_top": pokemon_top,
                "favorite_pokemon": favorite_names,
                "favorite_breakdown": favorite_breakdown,
                "all_pokemon_names": all_pokemon_names,
                "pokemon_aliases": pokemon_aliases,
                "top_collection": top_collection,
                "top_series": top_series,
                "csort": csort,
                "cdir": cdir,
                "ssort": ssort,
                "sdir": sdir,
                "rsort": rsort,
                "rdir": rdir,
                "psort": psort,
                "pdir": pdir,
                "fsort": fsort,
                "fdir": fdir,
                "tsort": tsort,
                "tdir": tdir,
            },
        )
    finally:
        db.close()


@app.get("/pokemon/search")
def pokemon_search(request: Request, q: str = ""):
    db = get_db_session()
    try:
        results = []
        if q and len(q) >= 2:
            results = [
                row[0]
                for row in db.query(Card.name)
                .filter(func.lower(Card.name).like(_like_pattern(q)))
                .distinct()
                .order_by(Card.name)
                .limit(20)
                .all()
            ]
        alias_map = queries.pokemon_alias_map(db)
        favorite_names = queries.favorite_pokemon_names(db)
        favorited_results = {
            name for name in results if queries.resolve_pokemon_name(name, alias_map) in favorite_names
        }
        return templates.TemplateResponse(
            request,
            "partials/pokemon_search_results.html",
            {"results": results, "favorite_pokemon": favorited_results},
        )
    finally:
        db.close()


@app.post("/pokemon/favorite")
def toggle_pokemon_favorite(name: str = Form(...)):
    db = get_db_session()
    try:
        alias_map = queries.pokemon_alias_map(db)
        name = queries.resolve_pokemon_name(name, alias_map)
        existing = db.query(FavoritePokemon).filter(FavoritePokemon.name == name).one_or_none()
        if existing is not None:
            db.delete(existing)
        else:
            db.add(FavoritePokemon(name=name))
        db.commit()
        return RedirectResponse("/", status_code=303)
    finally:
        db.close()


@app.post("/pokemon/merge")
def merge_pokemon(name: str = Form(...), canonical: str = Form(...)):
    db = get_db_session()
    try:
        queries.merge_pokemon(db, name, canonical)
        db.commit()
        return RedirectResponse("/", status_code=303)
    finally:
        db.close()


@app.post("/pokemon/unmerge")
def unmerge_pokemon(name: str = Form(...)):
    db = get_db_session()
    try:
        alias = db.query(PokemonAlias).filter(PokemonAlias.name == name).one_or_none()
        if alias is not None:
            db.delete(alias)
            db.commit()
        return RedirectResponse("/", status_code=303)
    finally:
        db.close()


# --------------------------------------------------------------------------
# Inventory
# --------------------------------------------------------------------------
def _like_pattern(q: str) -> str:
    return f"%{q.lower()}%"


def _distinct_values(db: Session, column) -> list[str]:
    """Every distinct, non-null value of one column, sorted -- the "options
    for this filter dropdown" idiom used for series/set/collection/binder.
    """
    return [row[0] for row in db.query(column).filter(column.isnot(None)).distinct().order_by(column)]


def _top_collection_and_series(collection_breakdown: dict, series_breakdown: list):
    """The KPI row's highlights -- the single most valuable named
    collection/series (Bulk isn't a collection, so excluded). The collection
    highlight ranks by unique_value (not total_value) so duplicates can't
    inflate which collection looks "most valuable".
    """
    top_collection = max(collection_breakdown["children"], key=lambda b: b.unique_value, default=None)
    top_series = max(series_breakdown, key=lambda b: b.total_value, default=None)
    return top_collection, top_series


def _apply_inventory_filters(db: Session, q, series, set_, collection, binder, dup, rarity, language):
    query = db.query(Card).options(selectinload(Card.collections), selectinload(Card.binder))
    if q:
        like = _like_pattern(q)
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
    if language:
        query = query.filter(Card.language == language)
    return query


@app.get("/inventory")
def inventory(
    request: Request,
    q: str = "",
    series: str = "",
    set: str = "",
    collection: str = "",
    binder: str = "",
    # Bare `bool` rejects an empty-string query value (?dup=) with a 422 --
    # and the filter form's own hidden `dup` input submits exactly that when
    # unchecked (its hx-include picks up every field in the form, not just
    # the one the user touched). Query-string presence/truthiness, not a
    # real bool type, is what every "if dup" check below actually wants.
    dup: str = "",
    rarity: str = "",
    language: str = "",
    sort: str = "release",
    direction: str = "asc",
):
    db = get_db_session()
    try:
        query = _apply_inventory_filters(db, q, series, set, collection, binder, dup, rarity, language)
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

        all_series = _distinct_values(db, Card.series)
        all_sets = _distinct_values(db, Card.set)
        all_collections = _distinct_values(db, Collection.name)
        all_binders = _distinct_values(db, Binder.name)
        all_languages = _distinct_values(db, Card.language)

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
            "language": language,
            "sort": sort,
            "direction": direction,
            "all_series": all_series,
            "all_sets": all_sets,
            "all_collections": all_collections,
            "all_binders": all_binders,
            "all_languages": all_languages,
        }
        is_htmx = bool(request.headers.get("HX-Request"))
        if not is_htmx:
            # The KPI module lives outside the htmx-swapped #inventory-results
            # target, so only compute it on a full page load, not on every
            # filter keystroke/select change.
            collection_breakdown = queries.collection_bulk_breakdown(db)
            series_breakdown = queries.by_series_breakdown(db)
            top_collection, top_series = _top_collection_and_series(collection_breakdown, series_breakdown)
            context.update(
                {
                    "headline": queries.headline_summary(db),
                    "top_cards": queries.top_valuable_cards(db, limit=10),
                    "top_collection": top_collection,
                    "top_series": top_series,
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


def _cards_grouped_by_added_date(db):
    """Cards with a known `created_at`, grouped by calendar date -- these are
    the actual "newly added" cards someone would come here to price. Cards
    from before this column existed (`created_at` is None) have no real
    added-date and are returned separately as a flat, unsorted-by-date bucket
    for an optional/collapsed view, rather than dominating this list --
    Inventory is already the place to browse the full collection.
    """
    known_cards = (
        db.query(Card)
        .filter(Card.created_at.isnot(None))
        .order_by(Card.created_at.desc(), Card.id.desc())
        .all()
    )
    unknown_cards = db.query(Card).filter(Card.created_at.is_(None)).order_by(Card.id.desc()).all()

    # Group consecutive cards under the same calendar date -- cheap since
    # `known_cards` is already sorted by created_at desc.
    groups: list[dict] = []
    for card in known_cards:
        label = card.created_at.date().isoformat()
        if not groups or groups[-1]["label"] != label:
            groups.append({"label": label, "cards": []})
        groups[-1]["cards"].append(card)
    return groups, unknown_cards


def _registered_purchase_prices_by_card(txs) -> dict[int, list[float]]:
    by_card: dict[int, list[float]] = {}
    for tx in txs:
        if tx.type == "kjøp":
            by_card.setdefault(tx.card_id, []).append(tx.price)
    return by_card


def _registered_purchase_ids_by_card(txs) -> dict[int, list[int | None]]:
    by_card: dict[int, list[int | None]] = {}
    for tx in txs:
        if tx.type == "kjøp":
            by_card.setdefault(tx.card_id, []).append(tx.purchase_id)
    return by_card


def _card_field_sort_keys(purchase_prices_by_card: dict[int, list[float]] | None = None) -> dict:
    """Sort keys for a flat list of Card rows -- used by both the "Kort lagt
    til" date groups and the "Ukjent dato" table. `registered_price` is only
    meaningful where that column is actually shown (Kort lagt til).
    """
    keys = {
        "name": lambda c: c.name.lower(),
        "variant": lambda c: (c.variant or "").lower(),
        "series": lambda c: (c.series or "").lower(),
        "set": lambda c: (c.set or "").lower(),
        "reference_price": lambda c: c.reference_price if c.reference_price is not None else -1,
        "card_id": lambda c: c.card_id.lower(),
    }
    if purchase_prices_by_card is not None:
        keys["registered_price"] = lambda c: max(purchase_prices_by_card.get(c.id, [-1]))
    return keys


def _group_transactions_by_purchase(txs: list[Transaction]) -> tuple[list[dict], list[Transaction]]:
    """Split an already tsort/tdir-ordered transaction list into purchase-id
    groups (cards bought/sold together under a shared purchase_id, e.g. a
    lot) plus the remaining ungrouped ones -- mirrors the "Kort lagt til"
    date-groups pattern, but keyed on purchase_id instead of created_at.
    Group order follows first appearance in `txs`, so the default
    date-desc sort naturally puts the most recent purchase first.
    """
    groups: dict[int, list[Transaction]] = {}
    ungrouped: list[Transaction] = []
    for tx in txs:
        if tx.purchase_id is None:
            ungrouped.append(tx)
        else:
            groups.setdefault(tx.purchase_id, []).append(tx)
    purchase_groups = [
        {
            "purchase_id": pid,
            "transactions": group_txs,
            "total_price": sum(t.price for t in group_txs),
            "total_fees": sum(t.fees or 0 for t in group_txs),
        }
        for pid, group_txs in groups.items()
    ]
    return purchase_groups, ungrouped


def _transactions_context(
    db,
    request: Request,
    tsort: str,
    tdir: str,
    gsort: str = "name",
    gdir: str = "asc",
    usort: str = "name",
    udir: str = "asc",
    error: str | None = None,
) -> dict:
    txs = (
        db.query(Transaction)
        .options(selectinload(Transaction.card))
        .order_by(Transaction.date.desc(), Transaction.id.desc())
        .all()
    )
    txs = _sorted_rows(txs, tsort, tdir, TRANSACTION_SORT_KEYS)
    purchase_groups, ungrouped_transactions = _group_transactions_by_purchase(txs)
    groups, unknown_cards = _cards_grouped_by_added_date(db)
    # Capture recency order before the per-group sort below (gsort/gdir may
    # reorder each group's own cards e.g. by name) -- same cards, same
    # created_at-desc order `_recently_added_cards` used to re-query for.
    recent_cards = [card for group in groups for card in group["cards"]][:100]
    known_count = sum(len(g["cards"]) for g in groups)
    purchase_prices_by_card = _registered_purchase_prices_by_card(txs)
    purchase_ids_by_card = _registered_purchase_ids_by_card(txs)

    card_keys = _card_field_sort_keys(purchase_prices_by_card)
    for group in groups:
        group["cards"] = _sorted_rows(group["cards"], gsort, gdir, card_keys)
    unknown_cards = _sorted_rows(unknown_cards, usort, udir, _card_field_sort_keys())

    return {
        "transactions": txs,
        "purchase_groups": purchase_groups,
        "ungrouped_transactions": ungrouped_transactions,
        "error": error,
        "today": dt.date.today().isoformat(),
        "recent_cards": recent_cards,
        "tsort": tsort,
        "tdir": tdir,
        "gsort": gsort,
        "gdir": gdir,
        "usort": usort,
        "udir": udir,
        "groups": groups,
        "known_count": known_count,
        "unknown_cards": unknown_cards,
        "total_count": known_count + len(unknown_cards),
        # Display string (every price, comma-joined, if bought more than
        # once) -- vs. the single raw value below, only present when there's
        # exactly one to safely prefill/overwrite in the quick-register form.
        "registered_prices": {
            card_id: ", ".join(_format_kr(p) for p in prices) for card_id, prices in purchase_prices_by_card.items()
        },
        "single_registered_price": {
            card_id: prices[0] for card_id, prices in purchase_prices_by_card.items() if len(prices) == 1
        },
        # Prefills the quick-register form's Kjøps-ID field when correcting
        # the one existing purchase -- same "exactly one kjøp" condition as
        # single_registered_price above, and empty when that purchase never
        # got tagged with a purchase_id.
        "single_registered_purchase_id": {
            card_id: pids[0]
            for card_id, pids in purchase_ids_by_card.items()
            if len(pids) == 1 and pids[0] is not None
        },
    }


@app.get("/transactions")
def list_transactions(
    request: Request,
    tsort: str = "date",
    tdir: str = "desc",
    gsort: str = "name",
    gdir: str = "asc",
    usort: str = "name",
    udir: str = "asc",
):
    db = get_db_session()
    try:
        return templates.TemplateResponse(
            request, "transactions.html", _transactions_context(db, request, tsort, tdir, gsort, gdir, usort, udir)
        )
    finally:
        db.close()


@app.get("/transactions/card-search")
def card_search(request: Request, q: str = ""):
    db = get_db_session()
    try:
        results = []
        if q and len(q) >= 2:
            like = _like_pattern(q)
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
    upsert: bool = Form(False),
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

        # `upsert` comes only from the "Kort lagt til" quick-register form --
        # re-submitting a price there is meant to correct the one already
        # registered, not add a second "kjøp" for the same card. Only
        # auto-update when there's exactly one existing kjøp to correct;
        # with zero or several (a genuine re-buy already on record), fall
        # back to inserting a new row rather than guessing which to change.
        existing = None
        if upsert and type == "kjøp":
            candidates = (
                db.query(Transaction)
                .filter(Transaction.card_id == card.id, Transaction.type == "kjøp")
                .all()
            )
            if len(candidates) == 1:
                existing = candidates[0]

        if existing is not None:
            existing.date = dt.date.fromisoformat(date)
            existing.price = price
            existing.purchase_id = purchase_id
        else:
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
LOG_SORT_KEYS = {
    "ran_at": lambda log: log.ran_at,
    "source": lambda log: log.source,
    "files": lambda log: (log.files or "").lower(),
    "cards_created": lambda log: log.cards_created,
    "cards_updated": lambda log: log.cards_updated,
    "cards_flagged_missing": lambda log: log.cards_flagged_missing,
    "cards_deleted": lambda log: log.cards_deleted,
    "warnings_count": lambda log: log.warnings_count,
    "collections_touched": lambda log: (log.collections_touched or "").lower(),
    "binders_touched": lambda log: (log.binders_touched or "").lower(),
}

DROPBOX_FILE_SORT_KEYS = {
    "name": lambda f: f.name.lower(),
    "client_modified": lambda f: f.client_modified,
    "size": lambda f: f.size,
}


def _recent_import_logs(db: Session, lsort: str = "ran_at", ldir: str = "desc", limit: int = 20) -> list[ImportLog]:
    logs = db.query(ImportLog).order_by(ImportLog.ran_at.desc(), ImportLog.id.desc()).limit(limit).all()
    return _sorted_rows(logs, lsort, ldir, LOG_SORT_KEYS)


@app.get("/import")
def import_form(request: Request, lsort: str = "ran_at", ldir: str = "desc"):
    db = get_db_session()
    try:
        return templates.TemplateResponse(
            request,
            "import.html",
            {"result": None, "logs": _recent_import_logs(db, lsort, ldir), "lsort": lsort, "ldir": ldir},
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
            request,
            "import.html",
            {"result": result, "logs": _recent_import_logs(db), "lsort": "ran_at", "ldir": "desc"},
        )
    finally:
        db.close()


# --------------------------------------------------------------------------
# CSV import / sync -- straight from Dropbox
# --------------------------------------------------------------------------
@app.get("/import/dropbox/list")
def import_dropbox_list(request: Request, folder: str = "", dsort: str = "client_modified", ddir: str = "desc"):
    folder = folder or dropbox_client.default_folder()
    context = {"folder": folder, "files": None, "error": None, "dsort": dsort, "ddir": ddir}
    try:
        dbx = dropbox_client.build_client_from_env()
        files = dropbox_client.list_csv_files(dbx, folder)
        context["files"] = _sorted_rows(files, dsort, ddir, DROPBOX_FILE_SORT_KEYS)
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
            {
                "folder": folder,
                "files": None,
                "error": "Velg minst én fil å synke.",
                "dsort": "client_modified",
                "ddir": "desc",
            },
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
# Analyse -- economic development over time
# --------------------------------------------------------------------------
@app.get("/analyse")
def analyse(request: Request, metric: str = "unique"):
    if metric not in queries.VALUE_GROWTH_METRICS:
        metric = "unique"
    db = get_db_session()
    try:
        value_growth = queries.collection_value_growth(db, metric=metric)
        cash_flow = queries.cash_flow_by_month(db)

        value_chart = charts.build_line_chart(
            labels=[row["label"] for row in value_growth],
            values=[row["cumulative_value"] for row in value_growth],
        )
        cash_chart = charts.build_grouped_bar_chart(
            labels=[row["label"] for row in cash_flow],
            series=[[row["bought"] for row in cash_flow], [row["sold"] for row in cash_flow]],
        )

        return templates.TemplateResponse(
            request,
            "analyse.html",
            {
                "summary": queries.economic_summary(db),
                "headline": queries.headline_summary(db),
                "value_growth": value_growth,
                "cash_flow": cash_flow,
                "value_chart": value_chart,
                "cash_chart": cash_chart,
                "metric": metric,
                "metric_label": queries.VALUE_GROWTH_METRICS[metric][0],
                "metric_options": [
                    (key, label) for key, (label, _fn) in queries.VALUE_GROWTH_METRICS.items()
                ],
            },
        )
    finally:
        db.close()


# --------------------------------------------------------------------------
# Wiki -- static in-app reference, no DB access
# --------------------------------------------------------------------------
@app.get("/wiki")
def wiki(request: Request):
    return templates.TemplateResponse(request, "wiki.html", {})


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
