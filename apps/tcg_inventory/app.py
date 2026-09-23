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
from fastapi import FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.routing import APIRoute
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func
from sqlalchemy.orm import Session, selectinload

APP_DIR = Path(__file__).resolve().parent
load_dotenv(APP_DIR / ".env")

import ads
import auth
import dropbox_client
import price_refresh
import queries
import snapshots
from constants import CARD_CONDITIONS
from db import SessionLocal, init_db
from importer import import_dex_csv_files
from models import (
    Binder,
    Card,
    Collection,
    FavoritePokemon,
    ImportLog,
    Listing,
    PokemonAlias,
    Release,
    Set,
    Transaction,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


# Starlette caps a parsed form at 1000 fields and answers 400 past that --
# which htmx then silently ignores. The bulk forms here send ~10 fields per
# row (Edit Order, the New Order cart, add-existing-cards), so a lot of
# ~90+ cards could never be saved. Raise the cap for every route; request
# body size is still bounded by the host (e.g. Vercel's 4.5 MB).
MAX_FORM_FIELDS = 50_000


class _LargeFormRequest(Request):
    def form(self, *, max_files=1000, max_fields=MAX_FORM_FIELDS, max_part_size=1024 * 1024):
        return super().form(max_files=max_files, max_fields=max_fields, max_part_size=max_part_size)


class _LargeFormRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def large_form_handler(request: Request):
            return await handler(_LargeFormRequest(request.scope, request.receive))

        return large_form_handler


app = FastAPI(title="TCG Inventory", lifespan=lifespan)
app.router.route_class = _LargeFormRoute
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


def _pick_url(request: Request, value: str, path: str = "/transactions") -> str:
    """Link that switches the card picker's "without an order / all cards"
    filter, preserving every other query param (the picker's own sort state,
    and which order is expanded) -- same idiom as `_sort_url` above, so a
    filter click never silently resets a sort and vice versa.
    """
    params = dict(request.query_params)
    params["pick"] = value
    return path + "?" + urlencode(params)


templates.env.globals["pick_url"] = _pick_url

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
    "rarity": queries.rarity_sort_expr(Card.rarity),  # tier order, not alphabetical
    "illustrator": Card.illustrator,
    "language": Card.language,
}
INVENTORY_VALUE_SORTS = {"net_invested", "gain_loss"}
# Cards with no linked Set row, or a linked one with no known release_rank
# yet (no research done for that set), sort after every known set, not
# before -- see Set's docstring.
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
    present = []
    missing = []
    for row in rows:
        (missing if key_fn(row) is None else present).append(row)
    present.sort(key=key_fn, reverse=(direction == "desc"))
    return present + missing


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
    def _apply(bucket_list):
        for bucket in bucket_list:
            bucket.cards[:] = _sorted_rows(bucket.cards, sort, direction, CARD_LEAF_SORT_KEYS)
            if bucket.child_sets:
                _apply(bucket.child_sets)

    _apply(buckets)


def get_db_session() -> Session:
    return SessionLocal()


# --------------------------------------------------------------------------
# Dashboard
# --------------------------------------------------------------------------
def _query_url(request: Request, path: str, **overrides) -> str:
    """A link to `path` with the current query params plus `overrides` --
    same "keep everything else as-is" idiom as `_sort_url` above (e.g. each
    Dashboard table's own sort state survives a chart toggle)."""
    params = dict(request.query_params)
    params.update(overrides)
    return f"{path}?" + urlencode(params)


def _metric_url(request: Request, metric_key: str, path: str = "/") -> str:
    """A link that switches a shared value-growth chart's metric (see
    `chart_card` in macros.html), preserving every other query param. `path`
    is the page/endpoint the chart lives on (Dashboard vs Transactions'
    `/transactions/charts`).
    """
    return _query_url(request, path, metric=metric_key)


# Per metric: (headline_summary key for today's value, headline key for the
# matching card count). Only "unique" and "total" have a Net invested to
# compare against -- purchase cost is recorded per card, not per copy, so
# there is no honest way to split what was paid between a card's first copy
# and its duplicates; Duplicates shows "–" for those two instead.
_METRIC_HEADLINE_KEYS = {
    "unique": ("unique_value", "qty_unique"),
    "duplicates": ("duplicate_value", "duplicates"),
    "total": ("total_value", "qty_physical"),
}


def _market_value_stats(headline: dict, economic: dict, metric: str) -> list[dict]:
    """The Net invested / Current value / Gain-loss row shown inside the
    Market Value chart itself (see `chart_card`'s `stats` param in
    macros.html). Follows the chart's own metric toggle: Current value is
    today's value for that metric, Gain / loss is it minus Net invested
    (Total's matches the Market Value KPI card's gain). Duplicates has no
    Net invested of its own (see _METRIC_HEADLINE_KEYS), so both are None,
    rendered as "–".
    """
    current = headline[_METRIC_HEADLINE_KEYS[metric][0]]
    if metric == "duplicates":
        invested = gain_loss = None
    else:
        invested = economic["net_invested"]
        gain_loss = current - invested
    return [
        {"label": "Net invested", "value": invested},
        {"label": "Current value", "value": current},
        {
            "label": "Gain / loss",
            "value": gain_loss,
            "delta_class": None if gain_loss is None else ("gain" if gain_loss >= 0 else "loss"),
        },
    ]


def _market_value_context(
    request: Request, db: Session, headline: dict, economic: dict, txs, metric: str, period: str, path: str = "/"
) -> dict:
    """Everything the shared Market Value chart card needs (Dashboard and
    Transactions' lazy-loaded charts render the same card): the per-day
    history for `metric` over `period`, ending on today's live value; the
    Net invested line under it (not for Duplicates -- see
    _METRIC_HEADLINE_KEYS); the period's change; the key figures; and the
    metric/period toggle links (each keeps every other query param).
    """
    value_key, count_key = _METRIC_HEADLINE_KEYS[metric]
    history = queries.real_value_history(
        db, metric=metric, period=period, live=(headline[value_key], headline[count_key])
    )
    # history ends on the live value (real_value_history's `live`), so the
    # breakdown's end state is today's cards too.
    live_cards = db.query(Card).all()
    invested_line = None
    if metric != "duplicates" and history:
        invested_line = queries.net_invested_at_dates(txs, [row["date"] for row in history])
    return {
        "market_value_history": history,
        "market_value_invested": invested_line,
        "market_value_change": queries.period_change(history),
        "market_value_breakdown": queries.value_change_breakdown(db, metric, history, live_cards),
        "market_value_stats": _market_value_stats(headline, economic, metric),
        "metric": metric,
        "metric_label": queries.VALUE_GROWTH_METRICS[metric][0],
        "metric_options": [
            (key, label, _metric_url(request, key, path=path))
            for key, (label, _fn) in queries.VALUE_GROWTH_METRICS.items()
        ],
        "period": period,
        "period_label": queries.VALUE_HISTORY_PERIODS[period][0],
        "period_options": [
            (key, label, _query_url(request, path, period=key))
            for key, (label, _days) in queries.VALUE_HISTORY_PERIODS.items()
        ],
    }


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
    metric: str = "total",
    period: str = "all",
    open_pokemon_folder: bool = False,
):
    if metric not in queries.VALUE_GROWTH_METRICS:
        metric = "total"
    if period not in queries.VALUE_HISTORY_PERIODS:
        period = "all"
    db = get_db_session()
    try:
        # Loaded once and threaded through every breakdown below, instead of
        # each of the five re-querying the whole `cards` table itself.
        cards = queries.all_cards_with_collections(db)
        alias_map = queries.pokemon_alias_map(db)

        # Same idea for Transaction: economic_summary and net_invested_by_card
        # each used to independently run their own `Transaction.query.all()`
        # -- a full re-scan of the table twice per request (see #163) -- so
        # it's loaded once here and threaded through both.
        txs = db.query(Transaction).all()

        headline = queries.headline_summary(db, cards)
        collection_breakdown = queries.collection_bulk_breakdown(db, cards)
        series_breakdown = queries.by_series_breakdown(db, cards)
        invested_by_card = queries.net_invested_by_card(db, txs)
        queries.assign_bucket_investment(collection_breakdown["children"] + [collection_breakdown["bulk"]], invested_by_card)
        queries.assign_bucket_investment(series_breakdown, invested_by_card)
        for series_bucket in series_breakdown:
            queries.assign_bucket_investment(series_bucket.child_sets, invested_by_card)
        top_cards = queries.top_valuable_cards(db, limit=50)
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
        economic = queries.economic_summary(db, txs)

        return templates.TemplateResponse(
            request,
            "dashboard.html",
            {
                # Renders via the shared chart_card macro (macros.html), the
                # same module Transactions uses -- see /transactions for the
                # full economic breakdown this is a compact preview of.
                **_market_value_context(request, db, headline, economic, txs, metric, period),
                "headline": headline,
                "net_invested": economic["net_invested"],
                "gain": queries.gain_summary(cards, invested_by_card, economic["net_invested"]),
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


def _apply_inventory_filters(db: Session, q, series, set_, collection, binder, dup, rarity, language, unowned):
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
    # qty == 0 ("traded/sold away, but still present in the export" -- see
    # models.Card.unique_value's docstring / issue #132) is hidden from the
    # default browse view; `unowned=1` (the "Show cards I no longer own"
    # toggle) reveals them. Deliberately not applied to
    # pokemon_search_results.html's own query (see app.py's `/pokemon/search`
    # route) -- re-buying a previously-traded-away card there is intended.
    if not unowned:
        query = query.filter(Card.qty > 0)
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
    # Same query-string presence/truthiness idiom as `dup` above -- "Show
    # cards I no longer own" (qty == 0), default OFF/hidden. See issue #132.
    unowned: str = "",
    sort: str = "release",
    direction: str = "asc",
):
    db = get_db_session()
    try:
        query = _apply_inventory_filters(db, q, series, set, collection, binder, dup, rarity, language, unowned)
        number_sort = func.coalesce(Card.number_int, 999999)

        if sort == "release":
            # Default: actual print order -- Base Set #1 first, etc. Sets
            # with no research done yet (no linked Set row, or one with no
            # release_rank) sort after every known set rather than before
            # (see UNKNOWN_RELEASE_RANK). Joined via the real Card.set_id FK,
            # not a string match on (series, set) -- see Set's docstring.
            query = query.outerjoin(Set, Set.id == Card.set_id)
            release_rank = func.coalesce(Set.release_rank, UNKNOWN_RELEASE_RANK)
            rank_col = release_rank.desc() if direction == "desc" else release_rank.asc()
            order_cols = [rank_col, Card.set.asc(), number_sort.asc()]
        else:
            if sort in INVENTORY_VALUE_SORTS:
                order_cols = [number_sort.asc()]
            else:
                sort_col = SORT_COLUMNS.get(sort, Card.name)
                # Keep cards without a price at the bottom in either direction.
                # Direction first, THEN nulls_last(): the reverse order renders
                # "<col> NULLS LAST ASC", a syntax error on both SQLite and
                # Postgres (which expects "<col> ASC NULLS LAST"). See #176.
                sort_col = sort_col.desc() if direction == "desc" else sort_col.asc()
                sort_col = sort_col.nulls_last()
                order_cols = [sort_col] if sort == "number" else [sort_col, number_sort.asc()]
                if sort == "rarity":  # unrecognized names tie on rank -- break by name
                    order_cols.insert(1, Card.rarity.desc() if direction == "desc" else Card.rarity.asc())

        cards = query.order_by(*order_cols).all()
        # Net paid/Gain (see partials/inventory_table.html) are always shown
        # per row regardless of the active sort, so the Transaction table
        # load itself can't be skipped -- but it was previously re-run a
        # second (and, via economic_summary, third) time later in this same
        # request purely to feed the KPI module below. Loaded once here and
        # reused for both instead (see #163).
        txs = db.query(Transaction).all()
        invested_by_card = queries.net_invested_by_card(db, txs)
        ripped_card_ids = {t.card_id for t in txs if t.type == "ripped"}
        if sort in INVENTORY_VALUE_SORTS:
            def value_sort_key(card):
                invested = invested_by_card.get(card.id)
                if invested is None:
                    return float("-inf")
                if sort == "gain_loss":
                    return card.unique_value - invested
                return invested

            cards.sort(key=value_sort_key, reverse=direction == "desc")

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
            "unowned": unowned,
            "rarity": rarity,
            "language": language,
            "sort": sort,
            "direction": direction,
            "invested_by_card": invested_by_card,
            "ripped_card_ids": ripped_card_ids,
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
            all_cards = queries.all_cards_with_collections(db)
            collection_breakdown = queries.collection_bulk_breakdown(db, all_cards)
            series_breakdown = queries.by_series_breakdown(db, all_cards)
            # Reuses the invested_by_card/txs already loaded above instead of
            # re-scanning Transaction twice more (net_invested_by_card +
            # economic_summary each used to run their own independent query).
            queries.assign_bucket_investment(collection_breakdown["children"] + [collection_breakdown["bulk"]], invested_by_card)
            queries.assign_bucket_investment(series_breakdown, invested_by_card)
            for series_bucket in series_breakdown:
                queries.assign_bucket_investment(series_bucket.child_sets, invested_by_card)
            top_collection, top_series = _top_collection_and_series(collection_breakdown, series_breakdown)
            net_invested = queries.economic_summary(db, txs)["net_invested"]
            context.update(
                {
                    "headline": queries.headline_summary(db, all_cards),
                    "net_invested": net_invested,
                    "gain": queries.gain_summary(all_cards, invested_by_card, net_invested),
                    "top_cards": queries.top_valuable_cards(db, limit=50),
                    "top_collection": top_collection,
                    "top_series": top_series,
                }
            )
        template = "partials/inventory_table.html" if is_htmx else "inventory.html"
        return templates.TemplateResponse(request, template, context)
    finally:
        db.close()


# --------------------------------------------------------------------------
# Sales -- build a finn.no listing (title + description) from a selection of
# cards made on Inventory (see static/sale-list.js for how the selection is
# tracked client-side). Stateless generation (ads.py is a pure function);
# "Mark as listed" is the only write, and it never touches qty/collections
# -- listed != sold, see models.Listing's docstring.
# --------------------------------------------------------------------------
@app.get("/sales")
def sales_review(request: Request, card_ids: list[int] = Query(default=[])):
    db = get_db_session()
    try:
        cards = db.query(Card).filter(Card.id.in_(card_ids)).all() if card_ids else []
        # Preserve the order the user selected them in, not the DB's own order.
        cards_by_id = {c.id: c for c in cards}
        cards = [cards_by_id[cid] for cid in card_ids if cid in cards_by_id]
        return templates.TemplateResponse(
            request,
            "sales.html",
            {"cards": cards, "conditions": CARD_CONDITIONS},
        )
    finally:
        db.close()


def _sale_items_from_form(
    db: Session, card_ids: list[int], qtys: list[int], conditions: list[str], prices: list[str]
) -> list[ads.SaleItem]:
    cards_by_id = {c.id: c for c in db.query(Card).filter(Card.id.in_(card_ids)).all()}
    items = []
    for card_id, qty, condition, price_raw in zip(card_ids, qtys, conditions, prices):
        card = cards_by_id.get(card_id)
        if card is None:
            continue
        try:
            price = float(price_raw) if price_raw not in (None, "") else None
        except ValueError:
            price = None
        # Never let a stray form value exceed how many of this card exist --
        # the qty being sold, unlike the card's own qty, is a per-listing
        # decision that must not silently imply "sell everything owned".
        qty = max(1, min(qty, card.qty)) if card.qty else max(1, qty)
        items.append(
            ads.SaleItem(
                card_id=card.id,
                name=card.name,
                set=card.set,
                number=card.number,
                variant=card.variant,
                language=card.language,
                condition=condition or card.condition,
                qty=qty,
                price=price,
            )
        )
    return items


@app.post("/sales/generate")
def sales_generate(
    request: Request,
    card_id: list[int] = Form(...),
    qty: list[int] = Form(...),
    condition: list[str] = Form(...),
    price: list[str] = Form(...),
):
    db = get_db_session()
    try:
        # Persist any condition set here back onto the card -- it's real
        # per-card data (see models.Card.condition), not scoped just to this
        # one ad, so it should still be there next time this card is listed.
        for cid, cond in zip(card_id, condition):
            if cond:
                db.query(Card).filter(Card.id == cid).update({"condition": cond})
        db.commit()

        items = _sale_items_from_form(db, card_id, qty, condition, price)
        if not items:
            raise HTTPException(status_code=400, detail="No cards selected")
        draft = ads.build_listing(items)
        return templates.TemplateResponse(
            request,
            "partials/ad_draft.html",
            {"draft": draft, "card_ids": card_id},
        )
    finally:
        db.close()


@app.post("/sales/mark-listed")
def sales_mark_listed(
    request: Request,
    card_id: list[int] = Form(...),
    title: str = Form(...),
    description: str = Form(...),
    suggested_price: str = Form(""),
):
    db = get_db_session()
    try:
        cards = db.query(Card).filter(Card.id.in_(card_id)).all()
        try:
            price = float(suggested_price) if suggested_price else None
        except ValueError:
            price = None
        listing = Listing(
            created_at=dt.datetime.utcnow(),
            title=title,
            description=description,
            suggested_price=price,
            platform="finn.no",
            status="active",
        )
        listing.cards = cards
        db.add(listing)
        db.commit()
        return templates.TemplateResponse(request, "partials/listing_confirmation.html", {"listing": listing})
    finally:
        db.close()


# --------------------------------------------------------------------------
# Listings overview -- every `Listing` recorded via "Mark as listed" above,
# with cost/market/listed/sold prices side by side per card, plus per-row
# actions (mark sold, edit, delete, and "Remove listing"/delist). Excludes
# delisted listings by default ("Show delisted" toggle reveals them); a
# separate "Sold only" toggle (issue #127) narrows to just sold listings --
# see queries.listing_overview's docstring and README's "Sales listings
# (finn.no)" business rule.
# --------------------------------------------------------------------------
@app.get("/listings")
def listings_page(request: Request, show_delisted: bool = False, sold_only: bool = False):
    db = get_db_session()
    try:
        overview = queries.listing_overview(db, include_delisted=show_delisted, sold_only=sold_only)
        context = {"overview": overview, "show_delisted": show_delisted, "sold_only": sold_only}
        is_htmx = bool(request.headers.get("HX-Request"))
        template = "partials/listings_results.html" if is_htmx else "listings.html"
        return templates.TemplateResponse(request, template, context)
    finally:
        db.close()


@app.post("/listings/{listing_id}/delist")
def listings_delist(
    request: Request, listing_id: int, show_delisted: str = Form(""), sold_only: str = Form("")
):
    db = get_db_session()
    try:
        listing = db.query(Listing).filter(Listing.id == listing_id).first()
        if listing is None:
            raise HTTPException(status_code=404, detail="Listing not found")
        listing.status = "delisted"
        db.commit()

        show_delisted_flag = show_delisted in ("true", "1", "on")
        sold_only_flag = sold_only in ("true", "1", "on")
        if not show_delisted_flag or sold_only_flag:
            # Default view excludes delisted listings, and "Sold only" now
            # excludes this (just-delisted) row either way -- an empty
            # response swapped into the row's own outerHTML removes it.
            return HTMLResponse("")

        entry = queries.listing_entry(db, listing_id)
        return templates.TemplateResponse(
            request,
            "partials/listing_entry.html",
            {"entry": entry, "show_delisted": show_delisted_flag, "sold_only": sold_only_flag},
        )
    finally:
        db.close()


@app.post("/listings/{listing_id}/delete")
def listings_delete(request: Request, listing_id: int):
    """Hard-deletes the `Listing` row itself (issue #126) -- distinct from
    delist above, which only flips status and keeps history. The client is
    required to confirm first (`hx-confirm` on the "Delete" button in
    partials/listing_entry.html), since unlike delist this is irreversible.

    `listing.cards` is a many-to-many via `listing_cards` -- SQLAlchemy's ORM
    removes the matching association rows itself on delete, independent of
    listing_cards' `ondelete="CASCADE"` (which only fires if the DB
    connection has FK enforcement on, not guaranteed for SQLite here).
    Never touches `qty`, `card_collections`, `binder_id`, or `Transaction`
    rows -- same invariant as every other listing action.
    """
    db = get_db_session()
    try:
        listing = db.query(Listing).filter(Listing.id == listing_id).first()
        if listing is None:
            raise HTTPException(status_code=404, detail="Listing not found")
        db.delete(listing)
        db.commit()
        # Row removed outright regardless of the "Show delisted" toggle --
        # unlike delist, there's no state where a deleted listing reappears.
        return HTMLResponse("")
    finally:
        db.close()


def _listing_edit_context(
    listing_id: int,
    title: str,
    description: str,
    suggested_price: str,
    cards: list[Card],
    error: str | None = None,
) -> dict:
    return {
        "listing_id": listing_id,
        "title": title,
        "description": description,
        "suggested_price": suggested_price,
        "cards": cards,
        "error": error,
    }


@app.get("/listings/{listing_id}/edit")
def listing_edit_form(request: Request, listing_id: int):
    """Edit view (issue #126) -- title/description/suggested_price plus the
    attached card set (add/remove against `listing_cards`), mirroring the
    per-group edit pattern already established for Transactions
    (`/transactions/purchase/{id}/edit`) rather than inventing a new one.
    """
    db = get_db_session()
    try:
        listing = db.query(Listing).options(selectinload(Listing.cards)).filter(Listing.id == listing_id).first()
        if listing is None:
            return RedirectResponse("/listings", status_code=303)
        context = _listing_edit_context(
            listing_id,
            listing.title,
            listing.description,
            "" if listing.suggested_price is None else str(int(listing.suggested_price))
            if float(listing.suggested_price).is_integer()
            else str(listing.suggested_price),
            list(listing.cards),
        )
        return templates.TemplateResponse(request, "listing_edit.html", context)
    finally:
        db.close()


@app.get("/listings/{listing_id}/edit/card-search")
def listing_edit_card_search(request: Request, listing_id: int, q: str = ""):
    """Same search-then-append pattern as the Transactions order-edit
    relink search, but appends a new row instead of replacing one.
    """
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
            request,
            "partials/listing_edit_card_search_results.html",
            {"results": results, "listing_id": listing_id},
        )
    finally:
        db.close()


@app.get("/listings/{listing_id}/edit/card-row")
def listing_edit_card_row(request: Request, listing_id: int, card_id: int):
    db = get_db_session()
    try:
        card = db.query(Card).filter(Card.id == card_id).one_or_none()
        if card is None:
            return HTMLResponse("")
        return templates.TemplateResponse(request, "partials/listing_edit_card_row.html", {"card": card})
    finally:
        db.close()


@app.post("/listings/{listing_id}/edit/regenerate")
def listing_edit_regenerate(request: Request, listing_id: int, card_id: list[int] = Form(default=[])):
    """"Regenerate ad text" (issue #126) -- reruns the existing pure
    `ads.build_listing` off the *currently selected* cards in the edit form
    (via hx-include, not what's saved in the DB yet), so title/description
    don't go stale relative to which cards are actually in the lot after an
    edit. Qty is always 1 and price is the card's current `display_price` --
    Listing doesn't store a per-card qty/price the way a fresh /sales draft
    does, so this is a best-effort re-derivation, not a replay of the
    original draft's inputs.
    """
    db = get_db_session()
    try:
        unique_ids = list(dict.fromkeys(card_id))
        cards_by_id = {c.id: c for c in db.query(Card).filter(Card.id.in_(unique_ids)).all()} if unique_ids else {}
        ordered = [cards_by_id[cid] for cid in unique_ids if cid in cards_by_id]
        if not ordered:
            return templates.TemplateResponse(request, "partials/listing_edit_regenerate_empty.html", {})
        items = [
            ads.SaleItem(
                card_id=card.id,
                name=card.name,
                set=card.set,
                number=card.number,
                variant=card.variant,
                language=card.language,
                condition=card.condition,
                qty=1,
                price=card.display_price,
            )
            for card in ordered
        ]
        draft = ads.build_listing(items)
        return templates.TemplateResponse(request, "partials/listing_edit_regenerate.html", {"draft": draft})
    finally:
        db.close()


@app.post("/listings/{listing_id}/edit")
def listing_edit_submit(
    request: Request,
    listing_id: int,
    title: str = Form(...),
    description: str = Form(...),
    suggested_price: str = Form(""),
    card_id: list[int] = Form(default=[]),
):
    db = get_db_session()
    try:
        listing = db.query(Listing).filter(Listing.id == listing_id).first()
        if listing is None:
            return RedirectResponse("/listings", status_code=303)

        unique_ids = list(dict.fromkeys(card_id))
        cards = db.query(Card).filter(Card.id.in_(unique_ids)).all() if unique_ids else []
        if not cards:
            # A listing with no cards in it isn't meaningful -- re-render the
            # form with what the user submitted rather than saving an empty
            # lot or silently falling back to the old card set.
            context = _listing_edit_context(
                listing_id,
                title,
                description,
                suggested_price,
                [],
                error="Select at least one card before saving.",
            )
            return templates.TemplateResponse(request, "listing_edit.html", context)

        try:
            price = float(suggested_price) if suggested_price else None
        except ValueError:
            price = None

        listing.title = title
        listing.description = description
        listing.suggested_price = price
        listing.cards = cards
        db.commit()
        return RedirectResponse("/listings", status_code=303)
    finally:
        db.close()


def _mark_sold_rows(listing: Listing) -> list[dict]:
    """One row per card currently in `listing`, each pre-filled with a
    starting-guess price (`suggested_price / card_count`, editable, never
    auto-submitted -- see models.Listing's mark-sold docstring) for the
    mark-sold form.
    """
    cards = list(listing.cards)
    default_price = None
    if listing.suggested_price and cards:
        default_price = round(listing.suggested_price / len(cards), 2)
    return [{"card": card, "default_price": default_price} for card in cards]


@app.get("/listings/{listing_id}/mark-sold")
def listing_mark_sold_form(request: Request, listing_id: int):
    """Mark-sold form (issue #127) -- reuses the purchase-cart UI/route
    pattern (`/transactions/purchase/start` + `.../add-row`) rather than a
    single-click status flip: every card's real sale price must be
    explicitly confirmed here before any `Transaction` is written. See
    models.Listing's docstring for the full flow.
    """
    db = get_db_session()
    try:
        listing = db.query(Listing).options(selectinload(Listing.cards)).filter(Listing.id == listing_id).first()
        if listing is None or listing.status == "sold" or not listing.cards:
            # No form to fill in for a missing listing, an already-sold one
            # (re-running mark-sold must be a no-op, not a second round of
            # Transactions -- see acceptance criteria), or an empty lot.
            return RedirectResponse("/listings", status_code=303)
        return templates.TemplateResponse(
            request,
            "listing_mark_sold.html",
            {
                "listing": listing,
                "rows": _mark_sold_rows(listing),
                "today": dt.date.today().isoformat(),
                "error": None,
            },
        )
    finally:
        db.close()


@app.post("/listings/{listing_id}/mark-sold")
def listing_mark_sold_submit(
    request: Request,
    listing_id: int,
    date: str = Form(...),
    platform: str = Form(""),
    card_id: list[int] = Form(default=[]),
    price: list[str] = Form(default=[]),
):
    """Creates one `Transaction(type="sale", listing_id=<this listing>.id)`
    per card in the lot, all sharing one fresh `purchase_id` (same grouping
    convention the purchase-cart form uses), then flips `Listing.status` to
    `"sold"` -- all in a single `db.commit()` so a validation failure never
    leaves orphaned Transactions or a status stuck between "active" and
    "sold" (acceptance criteria). Never touches `qty`, `card_collections`,
    or `binder_id` -- same invariant as every other listing action.
    """
    db = get_db_session()
    try:
        listing = db.query(Listing).options(selectinload(Listing.cards)).filter(Listing.id == listing_id).first()
        if listing is None:
            raise HTTPException(status_code=404, detail="Listing not found")
        if listing.status == "sold":
            # Re-running mark-sold on an already-sold listing is a no-op --
            # no duplicate Transactions (acceptance criteria).
            return RedirectResponse("/listings", status_code=303)

        def _rerender(error: str) -> HTMLResponse:
            return templates.TemplateResponse(
                request,
                "listing_mark_sold.html",
                {
                    "listing": listing,
                    "rows": _mark_sold_rows(listing),
                    "today": date or dt.date.today().isoformat(),
                    "error": error,
                },
            )

        lot_card_ids = {c.id for c in listing.cards}
        submitted_ids = list(dict.fromkeys(card_id))
        if not submitted_ids or set(submitted_ids) != lot_card_ids:
            return _rerender("Every card currently in this lot needs a price — none can be added or skipped here.")

        if len(price) != len(card_id):
            return _rerender("Missing a price for one or more cards.")

        parsed_prices: dict[int, float] = {}
        for cid, price_raw in zip(card_id, price):
            try:
                p = float(price_raw)
            except (TypeError, ValueError):
                return _rerender("Every card needs a valid, positive sold price.")
            if p <= 0:
                return _rerender("Every card needs a valid, positive sold price.")
            parsed_prices[cid] = p

        try:
            tx_date = dt.date.fromisoformat(date)
        except ValueError:
            return _rerender("Invalid date.")

        new_purchase_id = _next_purchase_id(db)
        for cid in submitted_ids:
            db.add(
                Transaction(
                    card_id=cid,
                    type="sale",
                    date=tx_date,
                    price=parsed_prices[cid],
                    platform=platform or listing.platform or None,
                    purchase_id=new_purchase_id,
                    listing_id=listing.id,
                )
            )
        listing.status = "sold"
        db.commit()
        return RedirectResponse("/listings", status_code=303)
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
    "rarity": lambda t: queries.rarity_rank(t.card.rarity),
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


def _purchase_ids_by_card(txs) -> dict[int, list[int]]:
    """Which order(s) each card is already on, for the merged card table's
    "Order" column. When Recently Added and Legacy import were two separate
    tables, "does this card still need an order?" was encoded positionally
    -- which table the row appeared in. Merging them into one table loses
    that unless it becomes a real column, so derive it here (over the
    already-loaded `txs`, no extra query) rather than dropping the
    information. A card can appear on more than one order across repeat
    purchases, hence a list.
    """
    by_card: dict[int, list[int]] = {}
    for tx in txs:
        if tx.purchase_id is not None:
            ids = by_card.setdefault(tx.card_id, [])
            if tx.purchase_id not in ids:
                ids.append(tx.purchase_id)
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


# Transaction types that record how a card was acquired without any money
# changing hands through the row's price being a cost: trade (cards swapped;
# price is side cash, see Transaction.direction) and ripped (pulled from a
# pack yourself, always price 0). Both stay out of an order's registered
# Value/Remaining and out of Net invested.
NON_CASH_TYPES = ("trade", "ripped")

# Transaction types that mean "this card has been registered as acquired":
# the card picker's "Without an order" filter and the cart's "Show cards
# without an order" leave out any card with one of these. A trade counts
# whichever way it went -- a card traded in was acquired by the trade, and
# one traded out needs no order of its own either.
ACQUIRED_TYPES = ("purchase", "ripped", "trade")


def _price_for(tx_type: str, price: float) -> float:
    """A ripped card is free by definition -- whatever price a form sent."""
    return 0.0 if tx_type == "ripped" else price


def _trade_direction(tx_type: str, values: list[str], i: int) -> str | None:
    """The `direction` to store for form row `i`: "in"/"out" on a trade row,
    NULL on anything else (see Transaction.direction). Tolerates a form that
    didn't send a direction for this row at all -- older clients, or a
    caller that only ever registers purchases -- by storing NULL.
    """
    if tx_type != "trade" or i >= len(values):
        return None
    return values[i] if values[i] in ("in", "out") else None


def _group_transactions_by_purchase(
    txs: list[Transaction], trade_prices_then: dict[int, float | None] | None = None
) -> tuple[list[dict], list[Transaction]]:
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
        # A group can legitimately mix purchase/sale rows (both real cash
        # flow, both belong in the registered total) with trade rows (no
        # cash changes hands -- see models.py Transaction.type) if it was
        # built up piecemeal via direct DB edits (see HANDOFF.md's 2026-09-14
        # entry, order #11). A trade row's price must never contribute to
        # the registered total or its diff against the agreed total.
        # Ripped rows are the same: always 0, never part of what was paid.
        priced_txs = [t for t in group_txs if t.type not in NON_CASH_TYPES]
        total_price = sum(t.price for t in priced_txs)
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
        # For the collapsed summary line (unlike the bulk-edit form's
        # group_platform above, which stays blank on disagreement so it
        # never silently overwrites a mixed group), just surface whichever
        # row has one set -- same first-non-null-across-the-group pattern
        # already used for purchase_total/purchase_shipping/diff.
        summary_platform = next((t.platform for t in group_txs if t.platform), None)
        purchase_groups.append(
            {
                "purchase_id": pid,
                "transactions": sorted(group_txs, key=lambda t: t.price, reverse=True),
                "total_price": total_price,
                "total_fees": sum(t.fees or 0 for t in priced_txs),
                "purchase_total": purchase_total,
                "purchase_shipping": purchase_shipping,
                "platform": group_platform,
                "summary_platform": summary_platform,
                # What's left unaccounted for once both the card prices and
                # any declared shipping are subtracted -- e.g. normal-print
                # cards not priced individually yet. None when no declared
                # total is set (shipping alone doesn't imply a diff).
                "diff": (
                    (purchase_total - total_price - (purchase_shipping or 0)) if purchase_total is not None else None
                ),
                "min_date": min(t.date for t in group_txs),
                "min_id": min(t.id for t in group_txs),
                # None for an order with no trade rows -- see queries.trade_summary.
                "trade": queries.trade_summary(group_txs, trade_prices_then),
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
    pick: str = "unordered",
) -> dict:
    txs = (
        db.query(Transaction)
        .options(selectinload(Transaction.card))
        .order_by(Transaction.date.desc(), Transaction.id.desc())
        .all()
    )
    txs = _sorted_rows(txs, tsort, tdir, TRANSACTION_SORT_KEYS)
    trade_prices_then = queries.trade_prices_at(db, [t for t in txs if t.type == "trade"])
    purchase_groups, ungrouped_transactions = _group_transactions_by_purchase(txs, trade_prices_then)
    known_cards, unknown_cards = _cards_with_known_added_date(db)
    purchase_prices_by_card = _registered_purchase_prices_by_card(txs)
    card_keys = _card_field_sort_keys(purchase_prices_by_card)
    acquired_card_ids = {t.card_id for t in txs if t.type in ACQUIRED_TYPES}

    # The page's single card-picking table: "Recently Added" (cards with a
    # known added date) and the former separate "Legacy import" table
    # (created_at IS NULL) merged into one list, since both existed only to
    # feed the same order.
    #
    # They applied two *different* inclusion rules, which is the thing to be
    # careful about when merging: Recently Added listed every dated card
    # whether or not it already had an order, while Legacy was filtered down
    # to cards still missing one. Merging them naively would apply both rules
    # to one table. So membership here is "every card", and the distinction
    # becomes an explicit filter (`pick`) instead:
    #
    #   unordered -- no purchase transaction yet (the old Legacy rule, and
    #                what you actually want while filling an order)
    #   recent    -- has a known added date (the old Recently Added rule)
    #   all       -- everything
    #
    # Sorting is the single gsort/gdir pair for the merged table (usort/udir
    # is retired but still accepted, so old links don't 422). `_sorted_rows`
    # already buckets null-key rows to the end, so undated legacy rows sink
    # below the dated ones on a date sort instead of needing a separate
    # table -- no `or datetime.min` defence needed, and adding one would
    # scatter them through the list instead.
    # Built from the *unfiltered* lists: "All" has to mean all, including a
    # card that's both undated and already on an order. (The old Legacy
    # table pre-filtered those out, which was right when it was a
    # "cards still needing an order" table and wrong once it became one
    # filter of a general one.)
    all_picker_cards = known_cards + unknown_cards
    if pick == "unordered":
        picker_cards = [c for c in all_picker_cards if c.id not in acquired_card_ids]
    elif pick == "recent":
        picker_cards = [c for c in all_picker_cards if c.created_at is not None]
    else:
        picker_cards = list(all_picker_cards)
    picker_cards = _sorted_rows(picker_cards, gsort, gdir, card_keys)
    undated_count = sum(1 for c in picker_cards if c.created_at is None)
    pick_counts = {
        "unordered": sum(1 for c in all_picker_cards if c.id not in acquired_card_ids),
        "recent": sum(1 for c in all_picker_cards if c.created_at is not None),
        "all": len(all_picker_cards),
    }

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
        "delta": headline["total_value"] - economic["net_invested"],  # same as the Market Value KPI's gain
    }

    return {
        "transactions": txs,
        "headline": headline,
        # No "top_cards" here on purpose: kpi_module.html only renders its
        # "Most valuable cards" tile on the Dashboard (`request.url.path ==
        # '/'`), so computing a 50-card ranking for this page was pure work
        # on every load and every sort click. The other breakdowns above
        # stay -- the "Most valuable collection/series" tiles do render here.
        "top_collection": top_collection,
        "top_series": top_series,
        "kpi": kpi,
        "gain": queries.gain_summary(cards, invested_by_card, economic["net_invested"]),
        "purchase_groups": purchase_groups,
        # Each purchase row's share of its order's shipping -- shown under
        # the row's price, since it's part of what the card really cost.
        "shipping_by_tx": queries.shipping_shares(txs),
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
        # known_cards/unknown_cards/known_count are gone with the two
        # tables that rendered them -- both are now `picker_cards` under a
        # `pick` filter.
        "picker_cards": picker_cards,
        "picker_count": len(picker_cards),
        "undated_count": undated_count,
        "pick": pick,
        "pick_counts": pick_counts,
        "purchase_ids_by_card": _purchase_ids_by_card(txs),
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
    pick: str = "unordered",
):
    db = get_db_session()
    try:
        return templates.TemplateResponse(
            request,
            "transactions.html",
            _transactions_context(
                db, request, tsort, tdir, gsort, gdir, usort, udir, open_order=open_order, pick=pick
            ),
        )
    finally:
        db.close()


def _next_purchase_id(db: Session) -> int:
    """The purchase_id a new cart will be registered under -- one past the
    highest one in use, so cards bought/sold together always land in a
    fresh, never-before-used group.
    """
    return (db.query(func.max(Transaction.purchase_id)).scalar() or 0) + 1


def _create_default_purchase_transaction(db: Session, purchase_id: int, card_id: int) -> Transaction:
    """One freshly-created Transaction against an already-committed order,
    defaulted to today/purchase/0 so it's immediately editable rather than
    blocking on a fully-filled-in form -- shared by the Edit Order page's
    own add-card (issue #155) and the Legacy import table's "add to
    existing order" control.
    """
    tx = Transaction(card_id=card_id, type="purchase", date=dt.date.today(), price=0, purchase_id=purchase_id)
    db.add(tx)
    db.commit()
    db.refresh(tx)
    return tx


@app.get("/transactions/purchase/start")
def purchase_cart_start(request: Request, type: str = "purchase"):
    db = get_db_session()
    try:
        return templates.TemplateResponse(
            request,
            "partials/purchase_cart.html",
            {
                "type": type if type in ("purchase", "sale", "trade", "ripped") else "purchase",
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


_BROWSE_UNORDERED_LIMIT = 50


@app.get("/transactions/purchase/browse-unordered")
def purchase_cart_browse_unordered(request: Request):
    """Cards with no linked "purchase" transaction at all -- the same "no
    order yet" concept the Transactions page's per-row Order column
    (issue #153) already surfaces, but browsable from inside the New Order
    cart (issue #156) instead of requiring a scroll-and-click on the main
    page. Spans both "Recently Added" and "Legacy import" cards, not just
    the latter -- "no order" isn't the same question as "no known date".
    Capped since the unordered backlog can be the whole collection.
    """
    db = get_db_session()
    try:
        # A ripped card is accounted for too -- it just didn't cost anything.
        # Ripped and traded cards are accounted for too -- see ACQUIRED_TYPES.
        ordered_card_ids = db.query(Transaction.card_id).filter(Transaction.type.in_(ACQUIRED_TYPES)).distinct()
        base = db.query(Card).filter(~Card.id.in_(ordered_card_ids))
        total_count = base.count()
        results = base.order_by(Card.name).limit(_BROWSE_UNORDERED_LIMIT).all()
        return templates.TemplateResponse(
            request, "partials/purchase_cart_unordered_results.html", {"results": results, "total_count": total_count}
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


@app.post("/transactions/purchase/add-existing-cards")
def add_cards_to_existing_order(request: Request, card_id: list[int] = Form(default=[]), purchase_id: int = Form(...)):
    """Adds one or more cards directly to an already-committed order -- the
    Legacy import table's own bulk "add to order" control (checkboxes +
    one order picker), for cards that were never picked up by a New Order
    cart in the first place. Plain form POST + redirect (not htmx) since
    this table can list hundreds of rows; reuses the same default-row
    creation as the Edit Order page's add-card (issue #155).
    """
    db = get_db_session()
    try:
        cards = db.query(Card).filter(Card.id.in_(card_id)).all() if card_id else []
        order_exists = db.query(Transaction).filter(Transaction.purchase_id == purchase_id).first() is not None
        if not cards or not order_exists:
            error = (
                "Select at least one card first."
                if not cards
                else f"Order #{purchase_id} doesn't exist yet -- pick an existing Order ID from History above."
            )
            return templates.TemplateResponse(
                request, "transactions.html", _transactions_context(db, request, "date", "desc", error=error)
            )
        for card in cards:
            _create_default_purchase_transaction(db, purchase_id, card.id)
        return RedirectResponse(f"/transactions?open_order={purchase_id}", status_code=303)
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
    direction: list[str] = Form(default=[]),
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
        for i, (cid, p) in enumerate(zip(card_id, price)):
            db.add(
                Transaction(
                    card_id=cid,
                    type=type,
                    direction=_trade_direction(type, direction, i),
                    date=tx_date,
                    price=_price_for(type, p),
                    platform=platform or None,
                    purchase_id=purchase_id,
                    purchase_total=purchase_total,
                    purchase_shipping=purchase_shipping,
                )
            )
        db.commit()
        # Same open_order + hx-select/hx-target/hx-swap="outerHTML" pattern
        # as set_purchase_total below -- htmx swaps in just the newly
        # registered order's <details> (open, per open_order) instead of
        # navigating the whole page, so any other expanded groups / sort
        # state on Transactions survive registering an order.
        return RedirectResponse(f"/transactions?open_order={purchase_id}", status_code=303)
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


@app.get("/transactions/purchase/{purchase_id}/edit")
def purchase_edit_form(request: Request, purchase_id: int, saved: bool = False):
    """Order-level edit view (issue #109) -- one row per transaction sharing
    this purchase_id, all fields editable including relinking the card and
    reassigning purchase_id itself (which is how a row is moved to another
    order, merged into one, or split off into a new one -- there's no
    separate move/merge/split verb, see update_purchase below).
    """
    db = get_db_session()
    try:
        txs = (
            db.query(Transaction)
            .options(selectinload(Transaction.card))
            .filter(Transaction.purchase_id == purchase_id)
            .order_by(Transaction.price.desc())
            .all()
        )
        if not txs:
            return RedirectResponse("/transactions", status_code=303)
        purchase_total = next((t.purchase_total for t in txs if t.purchase_total is not None), None)
        purchase_shipping = next((t.purchase_shipping for t in txs if t.purchase_shipping is not None), None)
        # No agreed total saved yet -- default the field to shipping + the
        # cards already priced (price == 0 means "not priced yet", the same
        # convention the Legacy import table's Order column uses), so the
        # user starts from a real number rather than blank/zero. Once a
        # total is actually saved, it's a real value the user typed and
        # always wins here -- never silently recalculated out from under
        # them.
        if purchase_total is None:
            purchase_total = round(sum(t.price for t in txs if t.price) + (purchase_shipping or 0), 2)
        return templates.TemplateResponse(
            request,
            "purchase_edit.html",
            {
                "purchase_id": purchase_id,
                "transactions": txs,
                "purchase_total": purchase_total,
                "purchase_shipping": purchase_shipping,
                "next_purchase_id": _next_purchase_id(db),
                "saved": saved,
            },
        )
    finally:
        db.close()


@app.get("/transactions/purchase/{purchase_id}/edit/row/{tx_id}/relink-search")
def purchase_edit_relink_search(request: Request, purchase_id: int, tx_id: int, q: str = ""):
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
            request,
            "partials/purchase_edit_relink_results.html",
            {"results": results, "purchase_id": purchase_id, "tx_id": tx_id},
        )
    finally:
        db.close()


@app.get("/transactions/purchase/{purchase_id}/edit/row/{tx_id}/relink")
def purchase_edit_relink_select(request: Request, purchase_id: int, tx_id: int, card_id: int):
    db = get_db_session()
    try:
        card = db.query(Card).filter(Card.id == card_id).one_or_none()
        if card is None:
            return HTMLResponse("")
        return templates.TemplateResponse(
            request, "partials/purchase_edit_relink_cell.html", {"purchase_id": purchase_id, "tx_id": tx_id, "card": card}
        )
    finally:
        db.close()


@app.get("/transactions/purchase/{purchase_id}/edit/add-card-search")
def purchase_edit_add_card_search(request: Request, purchase_id: int, q: str = ""):
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
            request, "partials/purchase_edit_add_card_results.html", {"results": results, "purchase_id": purchase_id}
        )
    finally:
        db.close()


@app.post("/transactions/purchase/{purchase_id}/edit/add-card")
def purchase_edit_add_card(request: Request, purchase_id: int, card_id: int):
    """Appends a brand-new card to an already-committed order (issue #155) --
    creates one Transaction row immediately, defaulted to today/purchase/0,
    then returns it rendered through the same row markup the edit form's own
    rows use so it's immediately editable and included in the next Save.
    Deliberately its own route rather than folded into update_purchase's
    tx_id-keyed loop, which only ever edits rows that already exist.
    """
    db = get_db_session()
    try:
        card = db.query(Card).filter(Card.id == card_id).one_or_none()
        if card is None:
            return HTMLResponse("")
        tx = _create_default_purchase_transaction(db, purchase_id, card.id)
        return templates.TemplateResponse(
            request,
            "partials/purchase_edit_new_row.html",
            {"purchase_id": purchase_id, "tx": tx, "next_purchase_id": _next_purchase_id(db)},
        )
    finally:
        db.close()


@app.post("/transactions/purchase/{purchase_id}/edit")
def update_purchase(
    request: Request,
    purchase_id: int,
    tx_id: list[int] = Form(default=[]),
    type: list[str] = Form(default=[]),
    date: list[str] = Form(default=[]),
    price: list[float] = Form(default=[]),
    platform: list[str] = Form(default=[]),
    note: list[str] = Form(default=[]),
    card_id: list[int] = Form(default=[]),
    new_purchase_id: list[int] = Form(default=[]),
    delete_tx_id: list[int] = Form(default=[]),
    purchase_total: float | None = Form(None),
    purchase_shipping: float | None = Form(None),
    direction: list[str] = Form(default=[]),
):
    """Applies every row edit for this order -- including reassigning a
    row's purchase_id, which is move/merge/split's shared underlying
    primitive (see issue #109's scoping) -- in one commit.

    A row whose purchase_id changes has its purchase_total/purchase_shipping
    cleared rather than carried over, split, or summed onto the destination
    order: those fields are redundantly stored per row (see
    Transaction.purchase_total's comment in models.py) and there's no way to
    tell how much of the old total belongs there. The user must set the
    destination order's total/shipping afterward via the existing
    set_purchase_total form. Rows staying in this order get this form's
    purchase_total/purchase_shipping applied uniformly, same semantics as
    set_purchase_total.
    """
    db = get_db_session()
    try:
        delete_set = set(delete_tx_id)
        for i, txid in enumerate(tx_id):
            if txid in delete_set:
                db.query(Transaction).filter(Transaction.id == txid).delete()
                continue
            tx = db.query(Transaction).filter(Transaction.id == txid).one_or_none()
            if tx is None:
                continue
            tx.card_id = card_id[i]
            tx.type = type[i]
            tx.direction = _trade_direction(type[i], direction, i)
            tx.date = dt.date.fromisoformat(date[i])
            tx.price = _price_for(type[i], price[i])
            tx.platform = platform[i] or None
            tx.note = note[i] or None
            target_purchase_id = new_purchase_id[i]
            if target_purchase_id != purchase_id:
                tx.purchase_id = target_purchase_id
                tx.purchase_total = None
                tx.purchase_shipping = None
            else:
                tx.purchase_id = purchase_id
                tx.purchase_total = purchase_total
                tx.purchase_shipping = purchase_shipping
        db.commit()
        # Stay on the edit page (with a "Saved" confirmation and a Back
        # button) so the user can check the result or keep editing -- unless
        # every row was moved out/deleted, leaving nothing here to show.
        remaining = db.query(Transaction).filter(Transaction.purchase_id == purchase_id).count()
        target = f"/transactions/purchase/{purchase_id}/edit?saved=1" if remaining else "/transactions"
        return RedirectResponse(target, status_code=303)
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
            existing.price = _price_for(type, price)
            existing.purchase_id = purchase_id
        else:
            tx = Transaction(
                card_id=card.id,
                type=type,
                date=dt.date.fromisoformat(date),
                price=_price_for(type, price),
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
    purchase_id: int | None = Form(None),
    direction: str = Form(""),
):
    """Quick per-row edit, including reassigning purchase_id for a single
    row (e.g. an ungrouped transaction, or pulling one card out of an order
    without touching the rest of it). For editing several rows of the same
    order together -- relinking a card, adding a note, deleting a row, or
    reassigning several rows' purchase_id at once with the total/shipping
    reconciliation that requires -- use the order-level editor
    (update_purchase above) instead; this single-row form intentionally
    doesn't try to replicate that reconciliation.
    """
    db = get_db_session()
    try:
        tx = db.query(Transaction).filter(Transaction.id == tx_id).one_or_none()
        if tx is None:
            return HTMLResponse("")
        tx.date = dt.date.fromisoformat(date)
        tx.type = type
        tx.direction = _trade_direction(type, [direction], 0)
        tx.price = _price_for(type, price)
        tx.platform = platform or None
        tx.fees = fees
        tx.purchase_id = purchase_id
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
def import_redirect(request: Request):
    """The Sync Log used to be its own page -- now merged into /releases
    (issue #159) as its own section. Redirects old bookmarks/links there,
    carrying over any lsort/ldir query string so a saved sorted view still
    sorts the same way.
    """
    query = f"?{request.query_params}" if request.query_params else ""
    return RedirectResponse(f"/releases{query}#sync-log", status_code=308)


@app.get("/releases/sync-log")
def sync_log_partial(request: Request, lsort: str = "ran_at", ldir: str = "desc"):
    """Sync Log's own column-sort target (see partials/import_log.html) --
    swaps just that section back in via htmx instead of a plain page
    navigation, which would otherwise reload /releases and land at the top,
    above the Release Notes section below it.
    """
    db = get_db_session()
    try:
        return templates.TemplateResponse(
            request, "partials/import_log.html", {"logs": _recent_import_logs(db, lsort, ldir), "lsort": lsort, "ldir": ldir}
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
def transactions_charts(request: Request, metric: str = "total", period: str = "all"):
    if metric not in queries.VALUE_GROWTH_METRICS:
        metric = "total"
    if period not in queries.VALUE_HISTORY_PERIODS:
        period = "all"
    db = get_db_session()
    try:
        txs = db.query(Transaction).all()
        cash_flow = queries.cash_flow_by_month(db)
        headline = queries.headline_summary(db)
        economic = queries.economic_summary(db, txs)

        return templates.TemplateResponse(
            request,
            "partials/transactions_charts.html",
            {
                **_market_value_context(
                    request, db, headline, economic, txs, metric, period, path="/transactions/charts"
                ),
                "cash_flow": cash_flow,
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
# Release Notes (issue #144) -- a small, hand-authored log of user-facing
# changes, stored in the `releases` table (see models.Release's docstring
# for why a DB table, not a CHANGELOG.md file or git/PR-history generation).
# No new RBAC: both routes pass through the same auth_guard middleware as
# every other non-public route. No edit-in-place for v1 -- delete and
# re-add a mistaken entry instead.
# --------------------------------------------------------------------------
_RECENT_RELEASES_LIMIT = 15


@app.get("/releases")
def releases_page(request: Request, lsort: str = "ran_at", ldir: str = "desc"):
    """Merged with the former Sync Log page (issue #159) -- an operational,
    read-only sync history and a hand-authored, editable release changelog
    don't share an action model, so they're two stacked sections here
    (#sync-log first, since it's the more routinely checked one) rather than
    interleaved into one timeline.
    """
    db = get_db_session()
    try:
        releases = db.query(Release).order_by(Release.date.desc(), Release.id.desc()).all()
        context = {
            "releases": releases,
            "recent_releases": releases[:_RECENT_RELEASES_LIMIT],
            "older_releases": releases[_RECENT_RELEASES_LIMIT:],
            "today": dt.date.today().isoformat(),
            "logs": _recent_import_logs(db, lsort, ldir),
            "lsort": lsort,
            "ldir": ldir,
        }
        return templates.TemplateResponse(request, "releases.html", context)
    finally:
        db.close()


@app.post("/releases")
def releases_create(
    request: Request,
    date: dt.date = Form(...),
    title: str = Form(...),
    body: str = Form(...),
):
    db = get_db_session()
    try:
        db.add(Release(date=date, title=title, body=body, created_at=dt.datetime.utcnow()))
        db.commit()
        return RedirectResponse("/releases", status_code=303)
    finally:
        db.close()


@app.post("/releases/{release_id}/delete")
def releases_delete(request: Request, release_id: int):
    db = get_db_session()
    try:
        release = db.query(Release).filter(Release.id == release_id).first()
        if release is not None:
            db.delete(release)
            db.commit()
        # Already gone (e.g. double-submit or two tabs) is treated as a no-op,
        # not an error -- the user's intent (this entry should not exist) is
        # already satisfied.
        return RedirectResponse("/releases", status_code=303)
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
