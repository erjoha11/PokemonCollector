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
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func
from sqlalchemy.orm import Session, selectinload

APP_DIR = Path(__file__).resolve().parent
load_dotenv(APP_DIR / ".env")

import auth
import dropbox_client
import price_refresh
import queries
import snapshots
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
# /cron/dropbox-sync and /cron/price-refresh have their own separate auth
# (CRON_SECRET) -- a scheduled job has no browser session to log in with.
_PUBLIC_PATHS = {"/login", "/cron/dropbox-sync", "/cron/price-refresh"}


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

_DISPLAY_PRICE_COL = func.coalesce(Card.tcgplayer_price, Card.reference_price)

SORT_COLUMNS = {
    "name": Card.name,
    "number": func.coalesce(Card.number_int, 999999),
    "series": Card.series,
    "set": Card.set,
    "reference_price": _DISPLAY_PRICE_COL,
    "qty": Card.qty,
    "total_value": Card.qty * func.coalesce(_DISPLAY_PRICE_COL, 0),
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
    "reference_price": lambda c: c.display_price or 0,
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
def _metric_url(request: Request, metric_key: str, path: str = "/") -> str:
    """A link that switches a shared value-growth chart's metric (see
    `chart_card` in macros.html), preserving every other query param
    (each table's own sort state on Dashboard) -- same "keep everything else
    as-is" idiom as `_sort_url` above. `path` is the page/endpoint the chart
    lives on (Dashboard vs Transactions' `/transactions/charts`).
    """
    params = dict(request.query_params)
    params["metric"] = metric_key
    return f"{path}?" + urlencode(params)


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
    metric: str = "unique",
    open_pokemon_folder: bool = False,
):
    if metric not in queries.VALUE_GROWTH_METRICS:
        metric = "unique"
    db = get_db_session()
    try:
        # Loaded once and threaded through every breakdown below, instead of
        # each of the five re-querying the whole `cards` table itself.
        cards = queries.all_cards_with_collections(db)
        alias_map = queries.pokemon_alias_map(db)

        headline = queries.headline_summary(db, cards)
        collection_breakdown = queries.collection_bulk_breakdown(db, cards)
        series_breakdown = queries.by_series_breakdown(db, cards)
        invested_by_card = queries.net_invested_by_card(db)
        queries.assign_bucket_investment(collection_breakdown["children"] + [collection_breakdown["bulk"]], invested_by_card)
        queries.assign_bucket_investment(series_breakdown, invested_by_card)
        for series_bucket in series_breakdown:
            queries.assign_bucket_investment(series_bucket.child_sets, invested_by_card)
        top_cards = queries.top_valuable_cards(db, limit=10)
        rarity_breakdown = queries.by_rarity_breakdown(db, cards)

        # Renders via the shared chart_card macro (macros.html), the same
        # module Transactions uses -- see /transactions for the full
        # economic breakdown this is a compact preview of.
        value_growth = queries.collection_value_growth(db, cards, metric=metric)
        comparison_metric = "total" if metric == "unique" else "unique"
        comparison_growth = queries.collection_value_growth(db, cards, metric=comparison_metric)
        comparison_by_label = {row["label"]: row["cumulative_value"] for row in comparison_growth}
        comparison_values = [comparison_by_label.get(row["label"], 0) for row in value_growth]
        metric_label = queries.VALUE_GROWTH_METRICS[metric][0]
        metric_options = [
            (key, label, _metric_url(request, key)) for key, (label, _fn) in queries.VALUE_GROWTH_METRICS.items()
        ]

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
        economic = queries.economic_summary(db)

        return templates.TemplateResponse(
            request,
            "dashboard.html",
            {
                "headline": headline,
                "net_invested": economic["net_invested"],
                "collection_breakdown": collection_breakdown,
                "collection_rows": collection_rows,
                "series_breakdown": series_breakdown,
                "top_cards": top_cards,
                "rarity_breakdown": rarity_breakdown,
                "value_growth": value_growth,
                "comparison_values": comparison_values,
                "metric": metric,
                "metric_label": metric_label,
                "comparison_label": queries.VALUE_GROWTH_METRICS[comparison_metric][0],
                "metric_options": metric_options,
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
                "open_pokemon_folder": open_pokemon_folder,
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
        return RedirectResponse("/?open_pokemon_folder=1", status_code=303)
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
        return RedirectResponse("/?open_pokemon_folder=1", status_code=303)
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
            invested_by_card = queries.net_invested_by_card(db)
            queries.assign_bucket_investment(collection_breakdown["children"] + [collection_breakdown["bulk"]], invested_by_card)
            queries.assign_bucket_investment(series_breakdown, invested_by_card)
            for series_bucket in series_breakdown:
                queries.assign_bucket_investment(series_bucket.child_sets, invested_by_card)
            top_collection, top_series = _top_collection_and_series(collection_breakdown, series_breakdown)
            context.update(
                {
                    "headline": queries.headline_summary(db),
                    "net_invested": queries.economic_summary(db)["net_invested"],
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


def _cards_with_known_added_date(db):
    """Cards with a known `created_at` -- these are the actual "recently
    added" cards someone would come here to price. Cards from before this
    column existed (`created_at` is None) have no real added-date and are
    returned separately as a flat, unsorted-by-date bucket for an
    optional/collapsed view, rather than dominating this list -- Inventory
    is already the place to browse the full collection.
    """
    known_cards = (
        db.query(Card)
        .filter(Card.created_at.isnot(None))
        .order_by(Card.created_at.desc(), Card.id.desc())
        .all()
    )
    unknown_cards = db.query(Card).filter(Card.created_at.is_(None)).order_by(Card.id.desc()).all()
    return known_cards, unknown_cards


def _registered_purchase_prices_by_card(txs) -> dict[int, list[float]]:
    by_card: dict[int, list[float]] = {}
    for tx in txs:
        if tx.type == "purchase":
            by_card.setdefault(tx.card_id, []).append(tx.price)
    return by_card


def _card_field_sort_keys(purchase_prices_by_card: dict[int, list[float]] | None = None) -> dict:
    """Sort keys for a flat list of Card rows -- used by both the "Recently
    Added" table and the "Ukjent dato" table. `registered_price` and `date`
    are only meaningful where those columns are actually shown (Recently
    Added).
    """
    keys = {
        "name": lambda c: c.name.lower(),
        "variant": lambda c: (c.variant or "").lower(),
        "series": lambda c: (c.series or "").lower(),
        "set": lambda c: (c.set or "").lower(),
        "reference_price": lambda c: c.display_price if c.display_price is not None else -1,
        "card_id": lambda c: c.card_id.lower(),
    }
    if purchase_prices_by_card is not None:
        keys["registered_price"] = lambda c: max(purchase_prices_by_card.get(c.id, [-1]))
        keys["date"] = lambda c: c.created_at
    return keys


def _group_transactions_by_purchase(txs: list[Transaction]) -> tuple[list[dict], list[Transaction]]:
    """Split a transaction list into purchase-id groups (cards bought/sold
    together under a shared purchase_id, e.g. a lot) plus the remaining
    ungrouped ones, keyed on purchase_id instead of created_at.

    Fixed display order, independent of the page's own tsort/tdir (which
    still governs the ungrouped table): each group's own cards rank by
    price, priciest first, and the groups themselves are ordered by
    purchase_id descending -- so the highest-numbered (most recent)
    purchase shows first, letting the user renumber purchase_id to
    control display order directly.
    """
    groups: dict[int, list[Transaction]] = {}
    ungrouped: list[Transaction] = []
    for tx in txs:
        if tx.purchase_id is None:
            ungrouped.append(tx)
        else:
            groups.setdefault(tx.purchase_id, []).append(tx)
    purchase_groups = []
    for pid, group_txs in groups.items():
        total_price = sum(t.price for t in group_txs)
        # Every row in a group carries its own copy of the same value (same
        # redundant-per-row pattern as date/platform) -- take whichever one
        # isn't null, since not all of a group's rows are guaranteed to have
        # it set (e.g. rows added before this field existed).
        purchase_total = next((t.purchase_total for t in group_txs if t.purchase_total is not None), None)
        purchase_shipping = next((t.purchase_shipping for t in group_txs if t.purchase_shipping is not None), None)
        # Unlike purchase_total/purchase_shipping (always set uniformly across
        # a group already, per the comment above), platform can legitimately
        # disagree across rows if a group was built up piecemeal via
        # individual per-row edits. Only prefill the bulk-edit field when
        # every row that has a platform set agrees on the same value;
        # otherwise leave it blank rather than assume one row's value speaks
        # for the whole order.
        row_platforms = {t.platform for t in group_txs if t.platform}
        group_platform = next(iter(row_platforms)) if len(row_platforms) == 1 else None
        purchase_groups.append(
            {
                "purchase_id": pid,
                "transactions": sorted(group_txs, key=lambda t: t.price, reverse=True),
                "total_price": total_price,
                "total_fees": sum(t.fees or 0 for t in group_txs),
                "purchase_total": purchase_total,
                "purchase_shipping": purchase_shipping,
                "platform": group_platform,
                # What's left unaccounted for once both the card prices and
                # any declared shipping are subtracted -- e.g. normal-print
                # cards not priced individually yet. None when no declared
                # total is set (shipping alone doesn't imply a diff).
                "diff": (
                    (purchase_total - total_price - (purchase_shipping or 0)) if purchase_total is not None else None
                ),
                "min_date": min(t.date for t in group_txs),
                "min_id": min(t.id for t in group_txs),
            }
        )
    purchase_groups.sort(key=lambda g: g["purchase_id"], reverse=True)
    return purchase_groups, ungrouped


def _transactions_context(
    db,
    request: Request,
    tsort: str,
    tdir: str,
    gsort: str = "date",
    gdir: str = "desc",
    usort: str = "name",
    udir: str = "asc",
    error: str | None = None,
    open_order: int | None = None,
) -> dict:
    txs = (
        db.query(Transaction)
        .options(selectinload(Transaction.card))
        .order_by(Transaction.date.desc(), Transaction.id.desc())
        .all()
    )
    txs = _sorted_rows(txs, tsort, tdir, TRANSACTION_SORT_KEYS)
    purchase_groups, ungrouped_transactions = _group_transactions_by_purchase(txs)
    known_cards, unknown_cards = _cards_with_known_added_date(db)
    known_count = len(known_cards)
    purchase_prices_by_card = _registered_purchase_prices_by_card(txs)

    card_keys = _card_field_sort_keys(purchase_prices_by_card)
    known_cards = _sorted_rows(known_cards, gsort, gdir, card_keys)
    unknown_cards = _sorted_rows(unknown_cards, usort, udir, _card_field_sort_keys())

    # Compact economic snapshot, folded in from the former standalone
    # Analyse page -- cheap enough (in-memory sums over already-fetched
    # rows) to compute on every load, unlike the value-growth/cash-flow
    # charts below it, which are lazy-loaded via /transactions/charts
    # instead so they aren't rebuilt on every column-sort click.
    economic = queries.economic_summary(db)
    cards = queries.all_cards_with_collections(db)
    headline = queries.headline_summary(db, cards)
    collection_breakdown = queries.collection_bulk_breakdown(db, cards)
    series_breakdown = queries.by_series_breakdown(db, cards)
    invested_by_card = queries.net_invested_by_card(db)
    queries.assign_bucket_investment(collection_breakdown["children"] + [collection_breakdown["bulk"]], invested_by_card)
    queries.assign_bucket_investment(series_breakdown, invested_by_card)
    for series_bucket in series_breakdown:
        queries.assign_bucket_investment(series_bucket.child_sets, invested_by_card)
    top_collection, top_series = _top_collection_and_series(collection_breakdown, series_breakdown)
    kpi = {
        "net_invested": economic["net_invested"],
        "unique_value": headline["unique_value"],
        "delta": headline["unique_value"] - economic["net_invested"],
    }

    return {
        "transactions": txs,
        "headline": headline,
        "top_cards": queries.top_valuable_cards(db, limit=10),
        "top_collection": top_collection,
        "top_series": top_series,
        "kpi": kpi,
        "purchase_groups": purchase_groups,
        "ungrouped_transactions": ungrouped_transactions,
        "error": error,
        "today": dt.date.today().isoformat(),
        "tsort": tsort,
        "tdir": tdir,
        "gsort": gsort,
        "gdir": gdir,
        "usort": usort,
        "udir": udir,
        "open_order": open_order,
        "known_cards": known_cards,
        "known_count": known_count,
        "unknown_cards": unknown_cards,
        "total_count": known_count + len(unknown_cards),
        # Display string (every price, comma-joined, if bought more than
        # once) -- vs. the single raw value below, only present when there's
        # exactly one to safely prefill/overwrite in the quick-register form.
        "registered_prices": {
            card_id: ", ".join(_format_kr(p) for p in prices) for card_id, prices in purchase_prices_by_card.items()
        },
    }


@app.get("/transactions")
def list_transactions(
    request: Request,
    tsort: str = "date",
    tdir: str = "desc",
    gsort: str = "date",
    gdir: str = "desc",
    usort: str = "name",
    udir: str = "asc",
    open_order: int | None = None,
):
    db = get_db_session()
    try:
        return templates.TemplateResponse(
            request,
            "transactions.html",
            _transactions_context(db, request, tsort, tdir, gsort, gdir, usort, udir, open_order=open_order),
        )
    finally:
        db.close()


def _next_purchase_id(db: Session) -> int:
    """The purchase_id a new cart will be registered under -- one past the
    highest one in use, so cards bought/sold together always land in a
    fresh, never-before-used group.
    """
    return (db.query(func.max(Transaction.purchase_id)).scalar() or 0) + 1


@app.get("/transactions/purchase/start")
def purchase_cart_start(request: Request, type: str = "purchase"):
    db = get_db_session()
    try:
        return templates.TemplateResponse(
            request,
            "partials/purchase_cart.html",
            {
                "type": type if type in ("purchase", "sale") else "purchase",
                "purchase_id": _next_purchase_id(db),
                "today": dt.date.today().isoformat(),
            },
        )
    finally:
        db.close()


@app.get("/transactions/purchase/search")
def purchase_cart_search(request: Request, q: str = ""):
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
            request, "partials/purchase_cart_search_results.html", {"results": results}
        )
    finally:
        db.close()


@app.get("/transactions/purchase/add-row")
def purchase_cart_add_row(request: Request, card_id: int):
    db = get_db_session()
    try:
        card = db.query(Card).filter(Card.id == card_id).one_or_none()
        if card is None:
            return HTMLResponse("")
        return templates.TemplateResponse(request, "partials/purchase_cart_row.html", {"card": card})
    finally:
        db.close()


@app.post("/transactions/purchase")
def create_purchase(
    request: Request,
    type: str = Form(...),
    date: str = Form(...),
    platform: str = Form(""),
    purchase_id: int = Form(...),
    purchase_total: float | None = Form(None),
    purchase_shipping: float | None = Form(None),
    card_id: list[int] = Form(default=[]),
    price: list[float] = Form(default=[]),
):
    db = get_db_session()
    try:
        if len(card_id) != len(price) or not card_id:
            return templates.TemplateResponse(
                request,
                "transactions.html",
                _transactions_context(
                    db, request, "date", "desc", error="No cards added to the order yet — search for at least one card first."
                ),
            )
        tx_date = dt.date.fromisoformat(date)
        for cid, p in zip(card_id, price):
            db.add(
                Transaction(
                    card_id=cid,
                    type=type,
                    date=tx_date,
                    price=p,
                    platform=platform or None,
                    purchase_id=purchase_id,
                    purchase_total=purchase_total,
                    purchase_shipping=purchase_shipping,
                )
            )
        db.commit()
        return RedirectResponse("/transactions", status_code=303)
    finally:
        db.close()


@app.post("/transactions/purchase/{purchase_id}/total")
def set_purchase_total(
    purchase_id: int,
    purchase_total: float | None = Form(None),
    purchase_shipping: float | None = Form(None),
    platform: str | None = Form(None),
):
    """Sets (or clears) the declared total, shipping cost, and platform for
    every row already sharing this purchase_id — the "agreed"/shipping half
    of the registered/shipping/agreed/diff line in History, editable after
    the fact for purchases built up piecemeal (e.g. via direct
    reconciliation) rather than through the cart form. Platform is a
    bulk-overwrite of the whole order, same semantics as total/shipping — a
    blank submission clears it (sets NULL) on every row, matching per-row
    edit behavior; a mixed order that should keep one card on a different
    platform still needs the per-row edit form.
    """
    db = get_db_session()
    try:
        db.query(Transaction).filter(Transaction.purchase_id == purchase_id).update(
            {"purchase_total": purchase_total, "purchase_shipping": purchase_shipping, "platform": platform or None}
        )
        db.commit()
        return RedirectResponse(f"/transactions?open_order={purchase_id}", status_code=303)
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
                    db, request, "date", "desc", error="Card not found — pick one from the search results."
                ),
            )

        # `upsert`: correct the one existing "purchase" for this card instead
        # of adding a second one for the same card. Only auto-update when
        # there's exactly one existing purchase to correct; with zero or
        # several (a genuine re-buy already on record), fall back to
        # inserting a new row rather than guessing which to change.
        existing = None
        if upsert and type == "purchase":
            candidates = (
                db.query(Transaction)
                .filter(Transaction.card_id == card.id, Transaction.type == "purchase")
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


@app.get("/transactions/{tx_id}/edit")
def edit_transaction_form(request: Request, tx_id: int):
    db = get_db_session()
    try:
        tx = db.query(Transaction).filter(Transaction.id == tx_id).one_or_none()
        if tx is None:
            return HTMLResponse("")
        return templates.TemplateResponse(request, "partials/tx_row_edit.html", {"tx": tx})
    finally:
        db.close()


@app.get("/transactions/{tx_id}/row")
def view_transaction_row(request: Request, tx_id: int):
    db = get_db_session()
    try:
        tx = db.query(Transaction).filter(Transaction.id == tx_id).one_or_none()
        if tx is None:
            return HTMLResponse("")
        return templates.TemplateResponse(request, "partials/tx_row_view.html", {"tx": tx})
    finally:
        db.close()


@app.post("/transactions/{tx_id}")
def update_transaction(
    request: Request,
    tx_id: int,
    date: str = Form(...),
    type: str = Form(...),
    price: float = Form(...),
    platform: str = Form(""),
    fees: float | None = Form(None),
):
    db = get_db_session()
    try:
        tx = db.query(Transaction).filter(Transaction.id == tx_id).one_or_none()
        if tx is None:
            return HTMLResponse("")
        tx.date = dt.date.fromisoformat(date)
        tx.type = type
        tx.price = price
        tx.platform = platform or None
        tx.fees = fees
        db.commit()
        db.refresh(tx)
        return templates.TemplateResponse(request, "partials/tx_row_view.html", {"tx": tx})
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
                "error": "Select at least one file to sync.",
                "dsort": "client_modified",
                "ddir": "desc",
            },
        )
    db = get_db_session()
    try:
        dbx = dropbox_client.build_client_from_env()
        payload = [(path.rsplit("/", 1)[-1], dropbox_client.download_file(dbx, path)) for path in paths]
        result = import_dex_csv_files(db, payload, full_load=full_load, source="dropbox")
        snapshots.record_daily_snapshot(db, source="manual")
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
    from a tool that can't set custom headers, or a person running this by
    hand mid-day) may instead pass the same value as `?secret=`.

    The two are told apart for snapshotting purposes: only a request
    carrying that exact header is trusted as the real scheduled Vercel
    invocation (CardSnapshot.source="cron"); a `?secret=` request is treated
    as a manual off-schedule run (source="manual") even though it hits this
    same route -- see snapshots.record_daily_snapshot and HANDOFF.md. With
    no CRON_SECRET configured at all there's no way to tell the two apart,
    so every request is treated as "manual".
    """
    cron_secret = os.environ.get("CRON_SECRET", "")
    is_scheduled_invocation = bool(cron_secret) and request.headers.get("authorization") == f"Bearer {cron_secret}"
    authorized = not cron_secret or is_scheduled_invocation or secret == cron_secret
    if not authorized:
        raise HTTPException(status_code=401, detail="Unauthorized")
    snapshot_source = "cron" if is_scheduled_invocation else "manual"

    folder = dropbox_client.default_folder()
    db = get_db_session()
    try:
        dbx = dropbox_client.build_client_from_env()
        files = dropbox_client.list_csv_files(dbx, folder)
        if not files:
            snapshotted = snapshots.record_daily_snapshot(db, source=snapshot_source)
            return {
                "status": "ok",
                "folder": folder,
                "message": "No CSV files found",
                "cards_snapshotted": snapshotted,
            }
        payload = [(f.name, dropbox_client.download_file(dbx, f.path_lower)) for f in files]
        result = import_dex_csv_files(db, payload, full_load=False, source="cron")
        # Snapshot after the sync, not before -- a cron run should always
        # record today's post-sync qty/price, never yesterday's leftover
        # state (see snapshots.record_daily_snapshot / README "Value history").
        snapshotted = snapshots.record_daily_snapshot(db, source=snapshot_source)
        print(
            f"[cron/dropbox-sync] ok: files={[f.name for f in files]} "
            f"created={result.cards_created} updated={result.cards_updated} "
            f"flagged={result.cards_flagged_missing} "
            f"collections={sorted(result.collections_touched)} "
            f"binders={sorted(result.binders_touched)} "
            f"warnings={len(result.warnings)} "
            f"snapshotted={snapshotted}"
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
            "cards_snapshotted": snapshotted,
        }
    except (dropbox_client.DropboxNotConfigured, dropbox_client.DropboxImportError) as exc:
        # Cron runs unattended -- nobody's watching a response body, so this
        # has to land in Vercel's runtime logs to be debuggable at all.
        print(f"[cron/dropbox-sync] failed: {exc}")
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    finally:
        db.close()


@app.get("/cron/price-refresh")
def cron_price_refresh(request: Request, secret: str = ""):
    """Scheduled TCGPlayer price refresh, decoupled from Dex sync (see
    price_refresh.py and issue #93) -- its own Vercel Cron entry in
    vercel.json, separate from /cron/dropbox-sync's schedule so pricing
    keeps moving even on a day the Dex sync doesn't run (or once Dex sync
    becomes optional). Same CRON_SECRET-gated pattern as
    /cron/dropbox-sync -- see that route's docstring for the
    scheduled-vs-manual distinction, which also decides the CardSnapshot
    source recorded here ("price-cron" vs "manual", both distinct from the
    Dex sync's own "cron"/"manual" sources -- see queries.real_value_history,
    which already labels charts by source when more than one exists for a
    given day).
    """
    cron_secret = os.environ.get("CRON_SECRET", "")
    is_scheduled_invocation = bool(cron_secret) and request.headers.get("authorization") == f"Bearer {cron_secret}"
    authorized = not cron_secret or is_scheduled_invocation or secret == cron_secret
    if not authorized:
        raise HTTPException(status_code=401, detail="Unauthorized")
    snapshot_source = "price-cron" if is_scheduled_invocation else "manual"

    db = get_db_session()
    try:
        result = price_refresh.refresh_stale_prices(db)
        # Snapshot right after refreshing, same reasoning as
        # /cron/dropbox-sync: today's post-refresh prices, not yesterday's.
        snapshotted = snapshots.record_daily_snapshot(db, source=snapshot_source)
        print(
            f"[cron/price-refresh] ok: checked={result.cards_checked} "
            f"updated={result.cards_updated} "
            f"low_confidence={len(result.cards_low_confidence)} "
            f"variant_uncertain={len(result.cards_variant_uncertain)} "
            f"snapshotted={snapshotted}"
        )
        return {
            "status": "ok",
            "cards_checked": result.cards_checked,
            "cards_updated": result.cards_updated,
            "cards_low_confidence": result.cards_low_confidence,
            "cards_variant_uncertain": result.cards_variant_uncertain,
            "cards_snapshotted": snapshotted,
        }
    finally:
        db.close()


# --------------------------------------------------------------------------
# Transactions charts -- the former standalone Analyse page's value-growth
# and cash-flow charts, now a collapsible section on Transactions
# (folded in since the two were always read together). Its own endpoint,
# fetched lazily via hx-get on first expand, so the two SVG charts aren't
# rebuilt on every /transactions page load or column-sort click while
# collapsed -- see transactions.html's "Vis grafer" <details>.
# --------------------------------------------------------------------------
@app.get("/analyse")
def analyse_redirect():
    return RedirectResponse("/transactions", status_code=308)


@app.get("/transactions/charts")
def transactions_charts(request: Request, metric: str = "unique"):
    if metric not in queries.VALUE_GROWTH_METRICS:
        metric = "unique"
    db = get_db_session()
    try:
        value_growth = queries.collection_value_growth(db, metric=metric)
        comparison_metric = "total" if metric == "unique" else "unique"
        comparison_growth = queries.collection_value_growth(db, metric=comparison_metric)
        comparison_by_label = {row["label"]: row["cumulative_value"] for row in comparison_growth}
        comparison_values = [comparison_by_label.get(row["label"], 0) for row in value_growth]
        real_history = queries.real_value_history(db, metric=metric)
        cash_flow = queries.cash_flow_by_month(db)

        return templates.TemplateResponse(
            request,
            "partials/transactions_charts.html",
            {
                "value_growth": value_growth,
                "comparison_values": comparison_values,
                "real_history": real_history,
                "cash_flow": cash_flow,
                "metric": metric,
                "metric_label": queries.VALUE_GROWTH_METRICS[metric][0],
                "comparison_label": queries.VALUE_GROWTH_METRICS[comparison_metric][0],
                "metric_options": [
                    (key, label, _metric_url(request, key, path="/transactions/charts"))
                    for key, (label, _fn) in queries.VALUE_GROWTH_METRICS.items()
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
