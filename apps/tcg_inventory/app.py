"""TCG Inventory -- local Pokémon card collection tracker.

Run with:

    python app.py

then open http://localhost:8000 in a browser. Single SQLite file, no
external services, no build step (server-rendered HTML + HTMX).
"""
from __future__ import annotations

import datetime as dt
import hmac
import json
import os
import threading
from contextlib import asynccontextmanager, contextmanager, nullcontext
from pathlib import Path
from urllib.parse import quote, urlencode, urlsplit

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Form, HTTPException, Query, Request
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.routing import APIRoute
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import case, func, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload
from starlette.concurrency import run_in_threadpool

APP_DIR = Path(__file__).resolve().parent
load_dotenv(APP_DIR / ".env")

import ads
import auth
import card_lists
import cron_auth
import job_locks
import jobs
import missing_cards
import pricing
import sync_status
import queries
import won_inbox
import constants
import form_validation
from constants import CARD_CONDITIONS
from db import SessionLocal, init_db
from form_validation import (
    FormError,
    parse_amount,
    parse_date,
    parse_optional_amount,
    parse_tx_type,
    require_same_length,
)
from models import (
    Binder,
    Card,
    CardList,
    CardListItem,
    Collection,
    FavoritePokemon,
    ImportLog,
    Listing,
    PokemonAlias,
    Set,
    Transaction,
    WonItem,
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


# Issue #228: one error pattern for every htmx form. A rejected value is a
# 422 with a short plain-text message naming the field, which
# static/form-errors.js drops into the form's [data-form-error] slot (no
# swap, so the user's input stays). Plain-form routes catch FormError
# themselves and re-render their page with the message instead.
@app.exception_handler(FormError)
async def _form_error_handler(request: Request, exc: FormError):
    return PlainTextResponse(str(exc), status_code=422)


@app.exception_handler(RequestValidationError)
async def _request_validation_handler(request: Request, exc: RequestValidationError):
    # FastAPI's own 422 is JSON -- fine for API clients, but an htmx form
    # would show it raw in the error slot. Plain text for htmx requests.
    if request.headers.get("HX-Request"):
        message = form_validation.describe_request_validation_error(exc.errors())
        return PlainTextResponse(message, status_code=422)
    return await request_validation_exception_handler(request, exc)


def _format_kr(v: float | None) -> str:
    return f"{v:,.0f} kr".replace(",", " ") if v is not None else "-"


templates.env.filters["kr"] = _format_kr
templates.env.filters["lang"] = constants.language_code


def _days_ago(d: dt.date | None) -> str:
    """"today" / "1 d ago" / "12 d ago" -- a price's age on the card page."""
    if d is None:
        return ""
    days = (dt.date.today() - d).days
    if days <= 0:
        return "today"
    return f"{days} d ago"


templates.env.filters["days_ago"] = _days_ago
# Price source / flag labels (pricing.py, issue #210).
templates.env.filters["source_label"] = pricing.source_label
# `source | card_source_label(card)`: adds "(JP price)" for a Korean card
# priced as its Japanese print (issue #367). Use it wherever a label is
# shown for a specific card.
templates.env.filters["card_source_label"] = pricing.card_source_label
templates.env.filters["price_flags"] = pricing.flag_list
templates.env.filters["flag_label"] = pricing.flag_label
templates.env.filters["flag_title"] = pricing.flag_title


def _card_image_large(url: str | None) -> str | None:
    """The bigger version of a stored card thumbnail, for the card page
    and the card modal (partials/card_body.html). Each image host serves its sizes under a
    fixed naming scheme; an unknown host just gets the same URL back. The
    page falls back to the thumbnail itself if the large one fails to load.
    """
    if not url:
        return url
    if "images.pokemontcg.io" in url and url.endswith(".png") and not url.endswith("_hires.png"):
        return url[: -len(".png")] + "_hires.png"
    if "images.scrydex.com" in url and url.endswith("/small"):
        return url[: -len("/small")] + "/large"
    if "assets.tcgdex.net" in url and url.endswith("/low.webp"):
        return url[: -len("/low.webp")] + "/high.webp"
    return url


templates.env.filters["card_image_large"] = _card_image_large
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
    # A new order starts from the first page (Inventory's pager) -- page N
    # of a different ordering is a meaningless slice.
    params.pop("page", None)
    return path + "?" + urlencode(params)


templates.env.globals["sort_url"] = _sort_url


def _pick_url(request: Request, value: str, path: str | None = None) -> str:
    """Link that switches the card picker's "without an order / all cards"
    filter, preserving every other query param (the picker's own sort state,
    and which order is expanded) -- same idiom as `_sort_url` above, so a
    filter click never silently resets a sort and vice versa.

    `path` defaults to the current request's own path, so the link stays on
    whichever Orders tab rendered it (issue #255) -- a hardcoded path here
    used to bounce a click on one tab through a redirect to another.
    """
    path = path or request.url.path
    params = dict(request.query_params)
    params["pick"] = value
    return path + "?" + urlencode(params)


templates.env.globals["pick_url"] = _pick_url


def _type_filter_url(request: Request, path: str, value: str) -> str:
    """Link for one of the Purchased tab's ?type= pills (issue #255), keeping
    every other query param (sorts, picker filter) -- same idiom as
    `_pick_url`. "all" drops the param rather than spelling it out."""
    params = dict(request.query_params)
    params.pop("type", None)
    if value != "all":
        params["type"] = value
    return path + ("?" + urlencode(params) if params else "")


templates.env.globals["type_filter_url"] = _type_filter_url

# Paths reachable without a session -- everything else needs a login once
# Supabase Auth is configured. Unconfigured (no SUPABASE_* env vars, e.g.
# local dev) leaves the app open, same as before this was added.
# The four /cron/* routes have their own separate auth (CRON_SECRET, via
# cron_auth.require_cron_secret, failing closed, #226) -- a scheduled job has
# no browser session to log in with.
# /inbox/fb-wins (fb_auction_watcher sending your Facebook wins, #309) has its
# own secret, INBOX_TOKEN, and fails closed -- see _inbox_auth_error.
_PUBLIC_PATHS = {
    "/login", "/cron/dropbox-sync", "/cron/price-refresh", "/cron/image-backfill", "/cron/set-sync", "/inbox/fb-wins",
}


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

# The resolved market price (pricing.py, issue #210) -- a plain column, so
# Inventory sorts/pages by it in SQL.
_MARKET_PRICE_COL = Card.market_price

SORT_COLUMNS = {
    "name": Card.name,
    "number": func.coalesce(Card.number_int, 999999),
    "series": Card.series,
    "set": Card.set,
    "market_price": _MARKET_PRICE_COL,
    "qty": Card.qty,
    "total_value": Card.qty * func.coalesce(_MARKET_PRICE_COL, 0),
    "rarity": queries.rarity_sort_expr(Card.rarity),  # tier order, not alphabetical
    "illustrator": Card.illustrator,
    "language": Card.language,
}
INVENTORY_VALUE_SORTS = {"net_invested", "gain_loss"}
# Cards with no linked Set row, or a linked one with no known release_rank
# yet (no research done for that set), sort after every known set, not
# before -- see Set's docstring.
UNKNOWN_RELEASE_RANK = 999999

# Within one set, a number with a letter prefix (TG05, GG35, RC5, SV12, H1)
# is a subset printed after the main run: Dex's "Number" keeps the prefix but
# number_int strips it (importer._parse_number_int), so without this bucket
# TG05 sorted between main-set #5 and #6 (issue #376). 0 = plain number,
# 1 = prefixed or missing. Plain SUBSTR + BETWEEN so SQLite and Postgres agree.
_SUBSET_NUMBER_BUCKET = case(
    (func.substr(Card.number, 1, 1).between("0", "9"), 0),
    else_=1,
)

# Appended to every Inventory SQL sort: a unique final key (Card.id) so tied
# rows (variants of one number, number-less cards, same-named sets) come back
# in the same order on every request -- Postgres guarantees no order among
# ties, so page 8 and page 9 could otherwise overlap or skip rows (#376).
_INVENTORY_TIE_BREAKERS = (Card.number.asc(), Card.name.asc(), Card.variant.asc(), Card.language.asc(), Card.id.asc())

# The price sort key was `reference_price` before issue #210; old links and
# bookmarks with ?sort=reference_price (and gsort=) still work.
_SORT_KEY_ALIASES = {"reference_price": "market_price"}


def _sort_key(key: str) -> str:
    return _SORT_KEY_ALIASES.get(key, key)


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
# by_series_breakdown etc). "unique" has no per-card equivalent to a
# bucket's unique_count, since a single card is always exactly 1 or 0.
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
    is the page/endpoint the chart lives on (Dashboard vs the Orders page's
    `/orders/charts`).
    """
    return _query_url(request, path, metric=metric_key)


# --------------------------------------------------------------------------
# Orders page (issue #255) -- one nav item, three route-based tabs:
# /orders/purchased, /orders/sold, /orders/listings. These helpers are the
# single place that decides which tab an order lives on, so the tab lists
# and every post-write redirect can never disagree (if they did, an htmx
# swap with hx-select="#order-N" would find nothing and the order would
# silently vanish from the page).
# --------------------------------------------------------------------------
ORDER_TABS = {"purchased": "Purchased", "sold": "Sold", "listings": "Listings"}


def order_tab(types) -> str:
    """The Orders tab for an order (or a single ungrouped row) with these
    transaction types: Sold only if *every* row is a sale, Purchased for
    anything else -- mixed orders included, so an order that picked up a
    sale row via Edit order (or a purchase row) never drops off both tabs.
    """
    types = list(types)
    return "sold" if types and all(t == "sale" for t in types) else "purchased"


def _order_tab_for(db: Session, purchase_id: int | None) -> str | None:
    """`order_tab` for an order already in the DB, or None if it has no rows."""
    if purchase_id is None:
        return None
    types = [t for (t,) in db.query(Transaction.type).filter(Transaction.purchase_id == purchase_id).all()]
    return order_tab(types) if types else None


def _order_url(purchase_id: int | None = None, tab: str = "purchased") -> str:
    """Link to an Orders tab, optionally with one order expanded."""
    url = f"/orders/{tab}"
    return f"{url}?open_order={purchase_id}" if purchase_id is not None else url


def _redirect_keeping_query(request: Request, path: str, status_code: int = 308) -> RedirectResponse:
    """Redirect an old URL to its new home with the full query string intact
    (e.g. /transactions?open_order=5 -> /orders/purchased?open_order=5)."""
    query = request.url.query
    return RedirectResponse(path + (f"?{query}" if query else ""), status_code=status_code)


def _current_orders_tab(request: Request) -> str | None:
    """Which Orders tab the browser is showing, from htmx's HX-Current-URL
    header -- None for a non-htmx request or a page that isn't a tab."""
    current = request.headers.get("HX-Current-URL")
    if not current:
        return None
    path = urlsplit(current).path
    return next((tab for tab in ORDER_TABS if path == f"/orders/{tab}"), None)


def _order_redirect(request: Request, db: Session, purchase_id: int, fallback_tab: str = "purchased"):
    """Post-write redirect to the tab `purchase_id` now lives on, expanded.

    A normal 303 for a plain form post, and for an htmx request whose page
    is already on that tab (htmx follows it and hx-selects #order-N or
    #main-content out of the result as before). When an htmx request comes
    from a *different* tab (e.g. a cart registered or an order edited into
    the other tab), swapping would show Sold content under a /orders/purchased
    URL -- so answer with HX-Redirect instead and let the browser navigate.
    """
    tab = _order_tab_for(db, purchase_id) or fallback_tab
    url = _order_url(purchase_id, tab)
    if request.headers.get("HX-Request") and _current_orders_tab(request) != tab:
        return Response(status_code=200, headers={"HX-Redirect": url})
    return RedirectResponse(url, status_code=303)


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
    today's value for that metric; Gain / loss only on Total (the one app-wide
    gain definition, matching the Market Value KPI card). Duplicates has no
    Net invested of its own (see _METRIC_HEADLINE_KEYS), so both are None,
    rendered as "–".
    """
    current = headline[_METRIC_HEADLINE_KEYS[metric][0]]
    invested = None if metric == "duplicates" else economic["net_invested"]
    # Gain / loss has one definition app-wide -- total value (duplicates
    # included) minus Net invested, see queries.gain_summary -- so it's only
    # shown on the Total metric; Unique value minus Net invested would be a
    # second, smaller "gain" that pays for every copy but counts only one.
    gain_loss = current - invested if metric == "total" else None
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
        # Tooltip lines at price-source switch points (issue #210).
        "market_value_source_notes": queries.history_source_notes(db, history, live_cards),
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
        collection_breakdown = queries.collection_membership_breakdown(db, cards)
        series_breakdown = queries.by_series_breakdown(db, cards)
        invested_by_card = queries.net_invested_by_card(db, txs)
        queries.assign_bucket_investment(
            collection_breakdown["children"]
            + [collection_breakdown["bulk"], collection_breakdown["collections"], collection_breakdown["total"]],
            invested_by_card,
        )
        queries.assign_bucket_investment(series_breakdown, invested_by_card)
        for series_bucket in series_breakdown:
            queries.assign_bucket_investment(series_bucket.child_sets, invested_by_card)
        top_cards = queries.top_valuable_cards(db, limit=50)
        rarity_breakdown = queries.by_rarity_breakdown(db, cards)
        queries.assign_bucket_investment(rarity_breakdown, invested_by_card)

        # Bucket rows (collection, series, set, rarity) always keep their
        # default order from queries.py -- clicking a column header only
        # re-sorts the cards nested inside each bucket, never the buckets
        # themselves.
        collection_rows = collection_breakdown["children"] + [collection_breakdown["bulk"]]
        _sort_cards_in_buckets(collection_rows, csort, cdir)
        _sort_cards_in_buckets(series_breakdown, ssort, sdir)
        _sort_cards_in_buckets(rarity_breakdown, rsort, rdir)
        # "Most valuable cards" is always market price, highest first -- no
        # sort pills (issue #245). Re-sorted on display_price (what the tile
        # shows) since the query orders on the stored market_price. Old
        # ?tsort=/?tdir= bookmarks are simply ignored as unknown params.
        top_cards = sorted(top_cards, key=lambda c: c.display_price or 0, reverse=True)

        # Pokemon is different from the other breakdowns: it's a flat top-10
        # (by unique prints owned, the fixed cutoff), and a column click
        # re-orders those same 10 buckets -- same pattern as "Topp 10 mest
        # verdifulle kort" above, not the bucket-hierarchy tables.
        all_pokemon = queries.by_pokemon_breakdown(db, cards, alias_map)
        queries.assign_bucket_investment(all_pokemon, invested_by_card)
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
                # same module Transactions uses -- see /orders/purchased for the
                # full economic breakdown this is a compact preview of.
                **_market_value_context(request, db, headline, economic, txs, metric, period),
                "headline": headline,
                "net_invested": economic["net_invested"],
                "gain": queries.gain_summary(cards, invested_by_card, economic["net_invested"]),
                "collection_breakdown": collection_breakdown,
                "collection_rows": collection_rows,
                "series_breakdown": series_breakdown,
                "top_cards": top_cards,
                "price_movers": queries.price_movers(db, cards),
                "recently_added": queries.recently_added(cards),
                "recently_added_limit": queries.RECENTLY_ADDED_LIMIT,
                "invested_by_card": invested_by_card,
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
    inflate which collection looks "most valuable". Collections are ranked
    on real membership (every card with the tag, see
    `queries.collection_membership_breakdown`), not primary-collection credit.
    """
    top_collection = max(collection_breakdown["children"], key=lambda b: b.unique_value, default=None)
    top_series = max(series_breakdown, key=lambda b: b.total_value, default=None)
    return top_collection, top_series


# Inventory's collection filter value for "cards with no collection" (the
# Dashboard's Bulk row) -- not a name a real Dex collection can have.
NO_COLLECTION_FILTER = "__none__"
# Rows per Inventory page; `page_size=0` shows every row on one page.
INVENTORY_PAGE_SIZE = 100
# Optional Inventory columns only shown when at least one card in the
# current result has a value (see inventory_table.html's column chooser for
# the rest, which the viewer can hide themselves).
INVENTORY_SPARSE_COLUMNS = ("classification", "location", "notes")


def _inventory_page_links(request: Request, page: int, page_count: int) -> list[tuple[int | None, str | None]]:
    """(page number, url) for the Inventory pager: first, last, and two
    either side of the current page; (None, None) marks a gap ("…")."""
    wanted = sorted({1, page_count, *range(page - 2, page + 3)} & set(range(1, page_count + 1)))
    links: list[tuple[int | None, str | None]] = []
    for i, n in enumerate(wanted):
        if i and n - wanted[i - 1] > 1:
            links.append((None, None))
        links.append((n, _query_url(request, "/inventory", page=n)))
    return links


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
    if collection == NO_COLLECTION_FILTER:
        query = query.filter(~Card.collections.any())
    elif collection:
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
    page: int = 1,
    page_size: int = INVENTORY_PAGE_SIZE,
):
    sort = _sort_key(sort)
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
            # Card.series after Card.set: two unranked sets that share a name
            # in different series (both at UNKNOWN_RELEASE_RANK) would
            # otherwise interleave number by number (#376).
            order_cols = [
                rank_col,
                Card.set.asc(),
                Card.series.asc(),
                _SUBSET_NUMBER_BUCKET.asc(),
                number_sort.asc(),
            ]
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

        cards = query.order_by(*order_cols, *_INVENTORY_TIE_BREAKERS).all()
        # Net paid/Gain (see partials/inventory_table.html) are always shown
        # per row regardless of the active sort, so the Transaction table
        # load itself can't be skipped -- but it was previously re-run a
        # second (and, via economic_summary, third) time later in this same
        # request purely to feed the KPI module below. Loaded once here and
        # reused for both instead (see #163).
        txs = db.query(Transaction).all()
        invested_by_card = queries.net_invested_by_card(db, txs)
        ripped_card_ids = {t.card_id for t in txs if t.type == "ripped"}
        # "Listed" badge (issue #257): one query for every active listing's
        # cards, not one per row. Unfiltered -- active listings are few.
        listed_by_card = queries.active_listings_by_card(db)
        if sort in INVENTORY_VALUE_SORTS:
            def value_sort_key(card):
                invested = invested_by_card.get(card.id)
                if invested is None:
                    return float("-inf")
                if sort == "gain_loss":
                    return card.total_value - invested
                return invested

            cards.sort(key=value_sort_key, reverse=direction == "desc")

        # Paginated after sorting (value sorts are done in Python above), so
        # a page is always a slice of the full, correctly ordered result.
        # Loading all rows is cheap at this size; rendering 850+ was not.
        total = len(cards)
        page_size = max(page_size, 0)
        page_count = max(1, -(-total // page_size)) if page_size else 1
        page = min(max(page, 1), page_count)
        page_cards = cards[(page - 1) * page_size : page * page_size] if page_size else cards
        empty_columns = [col for col in INVENTORY_SPARSE_COLUMNS if not any(getattr(card, col) for card in cards)]

        all_series = _distinct_values(db, Card.series)
        all_sets = _distinct_values(db, Card.set)
        all_collections = _distinct_values(db, Collection.name)
        all_binders = _distinct_values(db, Binder.name)
        all_languages = _distinct_values(db, Card.language)

        context = {
            "cards": page_cards,
            "total": total,
            "page": page,
            "page_size": page_size,
            "page_count": page_count,
            "default_page_size": INVENTORY_PAGE_SIZE,
            "empty_columns": empty_columns,
            "page_links": _inventory_page_links(request, page, page_count),
            "show_all_url": _query_url(request, "/inventory", page=1, page_size=0),
            "paged_url": _query_url(request, "/inventory", page=1, page_size=INVENTORY_PAGE_SIZE),
            "no_collection_filter": NO_COLLECTION_FILTER,
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
            "listed_by_card": listed_by_card,
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
            collection_breakdown = queries.collection_membership_breakdown(db, all_cards)
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
# Card detail and collection pages -- the app's own view of a card /
# collection. Card names across the app link to /cards/{id}; Dex is one
# link on that page, not where a name click goes.
# --------------------------------------------------------------------------
def _card_detail_context(db: Session, card_pk: int) -> dict:
    """Everything partials/card_body.html needs -- shared by the full card
    page (/cards/{id}) and the in-page modal's panel (/cards/{id}/panel,
    issue #280), so the two can never drift. 404s for an unknown card."""
    card = (
        db.query(Card)
        .options(
            selectinload(Card.collections),
            selectinload(Card.binder),
            selectinload(Card.linked_set),
            selectinload(Card.transactions),
            selectinload(Card.prices),
        )
        .filter(Card.id == card_pk)
        .one_or_none()
    )
    if card is None:
        raise HTTPException(status_code=404, detail="Card not found")
    txs = sorted(card.transactions, key=lambda t: (t.date, t.id))
    # Net invested by the app-wide rules (shipping shares included), so
    # this page's Gain matches Inventory's and the Dashboard's.
    order_ids = {t.purchase_id for t in txs if t.purchase_id is not None}
    order_txs = db.query(Transaction).filter(Transaction.purchase_id.in_(order_ids)).all() if order_ids else []
    all_txs = {t.id: t for t in order_txs + txs}.values()
    invested = queries.net_invested_by_card(db, list(all_txs)).get(card.id)
    shipping = queries.shipping_shares(list(all_txs))
    history = queries.card_price_history(db, card.id)
    # The set name links to the set's master-set page when it has a
    # checklist (#369).
    set_key = queries.card_set_key(card)
    master_set_url = _master_set_url(*set_key) if set_key and set_key in queries.checklist_keys(db) else None
    return {
        "master_set_url": master_set_url,
        "card": card,
        "transactions": txs,
        "shipping_by_tx": shipping,
        "invested": invested,
        "gain": (card.total_value - invested) if invested is not None else None,
        "price_history": history,
        # Per-source table, display-priority order (pricing.CHAIN).
        "price_rows": sorted(card.prices, key=lambda p: pricing.chain_rank(p.source)),
        "fresh_days": pricing.FRESH_DAYS,
        "missing_entry": _missing_entry(db, card),
    }


@app.get("/cards/{card_pk}")
def card_detail(request: Request, card_pk: int):
    db = get_db_session()
    try:
        return templates.TemplateResponse(request, "card_detail.html", _card_detail_context(db, card_pk))
    finally:
        db.close()


@app.get("/cards/{card_pk}/panel")
def card_panel(request: Request, card_pk: int):
    """The card page's body as a fragment (no <html> shell) for the in-page
    card modal (static/card-modal.js, issue #280). A separate URL on
    purpose rather than /cards/{id} varied on HX-Request: the browser cache
    can then never serve the fragment as the full page."""
    db = get_db_session()
    try:
        context = _card_detail_context(db, card_pk)
        return templates.TemplateResponse(request, "partials/card_panel.html", {**context, "in_panel": True})
    finally:
        db.close()


def _missing_entry(db: Session, card: Card | None) -> "missing_cards.MissingCard | None":
    """The card's "Missing from Dex" row (counts + deletable), or None when
    it isn't flagged missing."""
    if card is None or card.flagged_missing_since is None:
        return None
    return next((m for m in missing_cards.flagged_cards(db) if m.card.id == card.id), None)


@app.post("/cards/{card_pk}/delete-missing")
def delete_missing_card(
    request: Request,
    card_pk: int,
    confirm: str = Form(""),
    return_to: str = Form("sync-status"),
    in_panel: str = Form(""),
):
    """Delete a card a Dex sync flagged missing, registered in error.

    Behind the normal login (not in _PUBLIC_PATHS). Every guard lives in
    missing_cards.delete_missing_card: confirmation ticked, card flagged
    missing, zero transactions, zero listings -- otherwise nothing changes.
    Each handled outcome (deleted or refused) is a 200 with the re-rendered
    block and its message, since htmx 1.9 doesn't swap a 4xx.

    With return_to=card a successful delete also sends
    `HX-Trigger: {"cardDeleted": {"id": N}}`, so the card modal
    (static/card-modal.js, issue #280) can drop the card's row from
    /sync-status underneath. `in_panel` marks a form rendered inside that
    modal (its success message then offers Close, not "Back to Sync status").
    """
    db = get_db_session()
    try:
        result = missing_cards.delete_missing_card(db, card_pk, confirmed=bool(confirm))
        if return_to == "card":
            card = None if result.ok else db.get(Card, card_pk)
            response = templates.TemplateResponse(
                request,
                "partials/card_delete_missing.html",
                {
                    "card": card,
                    "missing_entry": _missing_entry(db, card),
                    "result": result,
                    "in_panel": bool(in_panel),
                },
            )
            if result.ok:
                response.headers["HX-Trigger"] = json.dumps({"cardDeleted": {"id": card_pk}})
            return response
        return templates.TemplateResponse(
            request,
            "partials/missing_cards.html",
            {"missing": missing_cards.flagged_cards(db), "result": result},
        )
    finally:
        db.close()


# --------------------------------------------------------------------------
# Collections (issue #378, replacing #369's "Sets & lists"): /collections
# is the overview, /collections/{id} one collection's page. A set with a
# checklist (set_checklists.py) shows its master-set block (#369's set page:
# progress, missing, duplicates, unmatched, add to list) in its *home*
# collection's section (queries.set_homes: the collection holding the most
# of the set's owned cards, ties to the lowest id; computed per request,
# nothing stored). Block counts are set-wide (queries.master_set_detail),
# never only the cards tagged with that collection. /sets/{language}/
# {set_code} redirects to the home, and still renders the block on its own
# for a set no collection holds.
# --------------------------------------------------------------------------
# Master-set filters: Track ("all" = every print that counts toward the
# master set, so Master Ball stays hidden unless picked) and Show.
MASTER_SET_TRACK_FILTERS = ("all", *queries.MASTER_SET_TRACK_LABELS)
MASTER_SET_SHOW_FILTERS = {"all": "All", "missing": "Missing", "owned": "Owned", "duplicates": "Duplicates"}


def _master_set_url(language: str, set_code: str, **params) -> str:
    url = f"/sets/{quote(language, safe='')}/{quote(set_code, safe='')}"
    params = {k: v for k, v in params.items() if v and v != "all"}
    return f"{url}?{urlencode(params)}" if params else url


def _set_dom_key(language: str, set_code: str) -> str:
    """The suffix on every id inside a set's master-set block (and its
    section anchor, #set-<key>), so two blocks on one collection page never
    share an id: hx-select / hx-target / the Copy button's getElementById
    would otherwise silently act on the first block."""
    return "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in f"{language}-{set_code}")


def _collection_url(
    collection_id: int,
    key: tuple[str, str] | None = None,
    track: str = "all",
    show: str = "all",
    owned: str = "",
    anchor: str = "",
) -> str:
    """/collections/{id}, with the page's one active master-set filter
    (`?set=lang:code&track=&show=`, dropped when both are defaults), the
    `owned=0` toggle when set, and `#<anchor>-<set key>` when asked."""
    params = []
    if key is not None and (track != "all" or show != "all"):
        params.append(("set", f"{key[0]}:{key[1]}"))
        if track != "all":
            params.append(("track", track))
        if show != "all":
            params.append(("show", show))
    if owned == "0":
        params.append(("owned", "0"))
    url = f"/collections/{collection_id}"
    if params:
        url += "?" + urlencode(params, safe=":")
    if key is not None and anchor:
        url += f"#{anchor}-{_set_dom_key(*key)}"
    return url


def _parse_set_param(value: str) -> tuple[str, str] | None:
    language, sep, set_code = (value or "").partition(":")
    return (language, set_code) if sep and language and set_code else None


def _master_set_block(
    db: Session,
    detail: "queries.MasterSetDetail",
    track: str,
    show: str,
    pill_url,
    collection: Collection | None = None,
    lists: list | None = None,
) -> dict:
    """Everything one master-set block renders (partials/master_set_block.html).
    `pill_url(track=, show=)` builds a filter pill's URL (the collection page,
    or /sets/... on the fallback page). With `collection`, owned tiles not
    tagged with it get an "in: <other collection>" badge."""
    language, set_code = detail.checklist.language, detail.checklist.set_code
    track = track if track in MASTER_SET_TRACK_FILTERS else "all"
    show = show if show in MASTER_SET_SHOW_FILTERS else "all"
    slots = queries.filter_master_set_slots(detail, track, show)
    track_pills = [("all", "All")] + [(t.key, t.label) for t in detail.tracks]
    elsewhere = {}
    if collection is not None:
        owned_slots = [s for s in detail.slots if s.owned]
        tags = queries.collections_by_card(db, [c.id for s in owned_slots for c in s.cards])
        for slot in owned_slots:
            in_cards = [t for c in slot.cards for t in tags.get(c.id, [])]
            if not any(coll_id == collection.id for coll_id, _ in in_cards):
                elsewhere[slot.master.id] = ", ".join(sorted({name for _, name in in_cards})) or "no collection"
    return {
        "detail": detail,
        "language": language,
        "set_code": set_code,
        "dom_key": _set_dom_key(language, set_code),
        "collection_id": collection.id if collection is not None else "",
        "track": track,
        "show": show,
        "slots": slots,
        "track_pills": [(key, label, pill_url(track=key, show=show)) for key, label in track_pills],
        "show_pills": [(key, label, pill_url(track=track, show=key)) for key, label in MASTER_SET_SHOW_FILTERS.items()],
        "master_ball_track": next((t for t in detail.tracks if t.key == "master_ball"), None),
        "missing_text": _missing_list_text(detail),
        "spares_sales_url": "/sales?" + urlencode({"card_ids": [s.card.id for s in detail.spare_slots]}, doseq=True),
        "elsewhere": elsewhere,
        **_list_add_context(db, slots, lists=lists),
    }


@app.get("/collections")
def collections_overview(request: Request):
    """Every collection (empty ones too) with cards owned, total value, the
    master set X/Y of each set it's home to and duplicates; then the want /
    sale lists (#370) and "New list"."""
    db = get_db_session()
    try:
        lists = card_lists.all_lists(db)
        return templates.TemplateResponse(
            request,
            "collections.html",
            {
                "rows": queries.collections_overview(db),
                "want_lists": [d for d in lists if d.kind == "want"],
                "sale_lists": [d for d in lists if d.kind == "sale"],
                "list_kinds": card_lists.LIST_KINDS,
            },
        )
    finally:
        db.close()


@app.get("/collections/{collection_id}")
def collection_page(
    request: Request,
    collection_id: int,
    owned: str = "1",
    set_param: str = Query("", alias="set"),
    track: str = "all",
    show: str = "all",
):
    """One collection, a section per Dex set. A set with a checklist whose
    home is this collection shows its master-set block; elsewhere that set
    shows its plain gallery plus a one-line link to the home. One active
    filter per page (`?set=lang:code&track=&show=`): the named block uses
    it, every other block renders at the defaults. `owned=0` also shows
    tagged cards no longer owned (qty 0), in the plain galleries."""
    db = get_db_session()
    try:
        detail = queries.collection_detail(db, collection_id)
        if detail is None:
            raise HTTPException(status_code=404, detail="Collection not found")
        collection = detail["collection"]
        bucket = detail["bucket"]
        invested_by_card = queries.net_invested_by_card(db)
        queries.assign_bucket_investment([bucket], invested_by_card)

        active = _parse_set_param(set_param)
        tracked = queries.checklist_keys(db)
        homes = queries.set_homes(db, tracked)
        details: dict = {}
        lists = None
        claimed = set()
        sections = []
        plain = queries.Bucket(name=bucket.name)
        for set_b in bucket.child_sets:
            key = queries.section_set_key(set_b.cards, tracked)
            section = {"bucket": set_b, "block": None, "link": None, "other_cards": []}
            if key is not None and key not in details:
                details[key] = queries.master_set_detail(db, *key)
            ms_detail = details.get(key)
            if ms_detail is not None and homes.get(key) == collection.id and key not in claimed:
                claimed.add(key)
                if lists is None:
                    lists = db.query(CardList).order_by(CardList.name, CardList.id).all()
                track_, show_ = (track, show) if key == active else ("all", "all")
                section["block"] = _master_set_block(
                    db,
                    ms_detail,
                    track_,
                    show_,
                    lambda _key=key, **p: _collection_url(collection.id, _key, owned=owned, **p),
                    collection=collection,
                    lists=lists,
                )
                # Cards filed under this Dex set name that belong to another set.
                section["other_cards"] = [c for c in set_b.cards if queries.card_set_key(c) != key]
            else:
                plain.child_sets.append(set_b)
                for card in set_b.cards:
                    plain.add(card)
                if ms_detail is not None:
                    home_id = homes.get(key)
                    home = db.get(Collection, home_id) if home_id is not None else None
                    section["link"] = {
                        "detail": ms_detail,
                        "url": _collection_url(home.id, key, anchor="set") if home else _master_set_url(*key),
                        "label": home.name if home else ms_detail.checklist.display_name,
                    }
            sections.append(section)
        return templates.TemplateResponse(
            request,
            "collection.html",
            {
                **detail,
                "invested_by_card": invested_by_card,
                "owned_only": owned != "0",
                "owned_toggle_url": _collection_url(
                    collection.id,
                    active if active in claimed else None,
                    track=track if track in MASTER_SET_TRACK_FILTERS else "all",
                    show=show if show in MASTER_SET_SHOW_FILTERS else "all",
                    owned="1" if owned == "0" else "0",
                ),
                "sections": sections,
                "has_blocks": bool(claimed),
                # Per-Dex-row figures (Unique cards, Duplicates, Completion)
                # only over the sections without a master-set block, which
                # has its own per-print Duplicates.
                "plain_bucket": plain if plain.child_sets else None,
                "plain_completion": plain.series_completion,
            },
        )
    finally:
        db.close()


def _missing_list_text(detail: "queries.MasterSetDetail") -> str:
    """The "Copy missing list" block: one line per missing master-set print."""
    missing = detail.missing
    lines = [f"{detail.checklist.display_name}: missing {len(missing)} of {detail.master_set.total}"]
    for slot in missing:
        line = f"{slot.number_label} {slot.name}".rstrip()
        if slot.track not in ("main", "secret"):
            line += f" · {slot.track_label}"
        lines.append(line)
    return "\n".join(lines)


templates.env.globals["master_set_url"] = _master_set_url
templates.env.globals["set_dom_key"] = _set_dom_key
templates.env.globals["list_kinds"] = card_lists.LIST_KINDS


@app.get("/collecting")
def collecting_redirect():
    """#369's "Sets & lists" landing page, replaced by /collections (#378)."""
    return RedirectResponse("/collections", status_code=302)


# Where /sets/{language}/{set_code}?at=... lands in the home collection's
# block (default: the section itself). The list page's "See Unmatched" link
# uses it, since the redirect's own #fragment would override one on the link.
_SET_ANCHORS = {"unmatched": "unmatched"}


@app.get("/sets/{language}/{set_code}")
def master_set_page(request: Request, language: str, set_code: str, track: str = "all", show: str = "all", at: str = ""):
    """A set with a checklist redirects to its home collection's section,
    keeping the query string as that page's active filter
    (`/sets/ja/sv2a?show=missing` → `/collections/6?set=ja:sv2a&show=missing#set-ja-sv2a`).
    The card page and list rows link here, so those links keep working.

    Rendered here instead (master_set.html) when no collection holds the
    set (zero owned cards, or none tagged), with the same block and pills
    on this URL, and for a set with no checklist (an empty state)."""
    db = get_db_session()
    try:
        key = (language, set_code)
        if key in queries.checklist_keys(db):
            home = queries.set_homes(db, {key}).get(key)
            if home is not None:
                params = [("set", f"{language}:{set_code}")] + [
                    (k, v) for k, v in request.query_params.multi_items() if k not in ("set", "at")
                ]
                anchor = _SET_ANCHORS.get(at, "set")
                url = f"/collections/{home}?{urlencode(params, safe=':')}#{anchor}-{_set_dom_key(language, set_code)}"
                return RedirectResponse(url, status_code=302)
        detail = queries.master_set_detail(db, language, set_code)
        context = {"language": language, "set_code": set_code, "detail": detail}
        if detail is not None:
            context["ms"] = _master_set_block(
                db, detail, track, show, lambda **p: _master_set_url(language, set_code, **p)
            )
        return templates.TemplateResponse(request, "master_set.html", context)
    finally:
        db.close()


# --------------------------------------------------------------------------
# Want and sale lists (issue #370, epic #366): card_lists.py. Items are
# master cards; every status is computed live. Lists never touch qty,
# collections, binders or transactions.
# --------------------------------------------------------------------------
def _list_add_context(
    db: Session, slots: list, message: dict | None = None, selected: dict | None = None, lists: list | None = None
) -> dict:
    """A master-set block's bulk "Add to list" panel. Its counts follow the
    current Track / Show filter: `slots` are the prints the grid shows.
    `lists` lets a page with several blocks query the lists once."""
    if lists is None:
        lists = db.query(CardList).order_by(CardList.name, CardList.id).all()
    spare_slots = [s for s in slots if s.spares]
    return {
        "add_missing": [s for s in slots if not s.owned],
        "add_spares": spare_slots,
        "add_spare_copies": sum(s.spares for s in spare_slots),
        "want_lists": [cl for cl in lists if cl.kind == "want"],
        "sale_lists": [cl for cl in lists if cl.kind == "sale"],
        "add_message": message,
        "add_selected": selected or {},
    }


@app.post("/sets/{language}/{set_code}/add-to-list")
def master_set_add_to_list(
    request: Request,
    language: str,
    set_code: str,
    what: str = Form(...),
    list_id: str = Form(""),
    new_list_name: str = Form(""),
    track: str = Form("all"),
    show: str = Form("all"),
    collection_id: str = Form(""),
):
    """"Add N missing to [want list]" / "Add N spares to [sale list]" in a
    master-set block. Server-side and filter-aware: the prints are the ones
    the grid shows under `track` / `show` (so Master Ball only when its
    track is shown); spares go in with qty = spare count. Idempotent: prints
    already on the list are left alone and counted. Answers with the
    re-rendered panel, whose always-present status line says what happened;
    without htmx, redirects back to the page the form was on: the
    collection (`collection_id`, #378), else the /sets/... page."""
    track = track if track in MASTER_SET_TRACK_FILTERS else "all"
    show = show if show in MASTER_SET_SHOW_FILTERS else "all"
    if what not in ("missing", "spares"):
        raise FormError("Pick missing cards or spares to add.")
    kind = "want" if what == "missing" else "sale"
    db = get_db_session()
    try:
        detail = queries.master_set_detail(db, language, set_code)
        if detail is None:
            raise HTTPException(status_code=404, detail="Set has no checklist")
        slots = queries.filter_master_set_slots(detail, track, show)
        if what == "missing":
            entries = [(s.master.id, 1) for s in slots if not s.owned]
        else:
            entries = [(s.master.id, s.spares) for s in slots if s.spares]
        if list_id == "new":
            card_list = card_lists.create_list(db, new_list_name, kind)
        else:
            card_list = db.get(CardList, int(list_id)) if list_id.strip().isdigit() else None
            if card_list is None:
                raise FormError("Pick a list, or “New list…” with a name.")
            if card_list.kind != kind:
                raise FormError(
                    f"“{card_list.name}” is a {card_lists.LIST_KINDS[card_list.kind].lower()}: "
                    f"{what} go on a {card_lists.LIST_KINDS[kind].lower()}."
                )
        added, already = card_lists.add_items(db, card_list, entries, source=what)
        db.commit()
        coll_id = int(collection_id) if collection_id.strip().isdigit() else None
        if not request.headers.get("HX-Request"):
            if coll_id is not None:
                back = _collection_url(coll_id, (language, set_code), track=track, show=show, anchor="set")
            else:
                back = _master_set_url(language, set_code, track=track, show=show)
            return RedirectResponse(back, status_code=303)
        message = {"list": card_list, "added": added, "already": already}
        context = {
            "language": language,
            "set_code": set_code,
            "dom_key": _set_dom_key(language, set_code),
            "collection_id": coll_id or "",
            "track": track,
            "show": show,
            **_list_add_context(db, slots, message=message, selected={kind: card_list.id}),
        }
        return templates.TemplateResponse(request, "partials/set_list_add.html", context)
    finally:
        db.close()


def _get_list(db: Session, list_id: int) -> CardList:
    card_list = db.get(CardList, list_id)
    if card_list is None:
        raise HTTPException(status_code=404, detail="List not found")
    return card_list


def _get_list_item(db: Session, list_id: int, item_id: int) -> CardListItem:
    item = db.get(CardListItem, item_id)
    if item is None or item.list_id != list_id:
        raise HTTPException(status_code=404, detail="List item not found")
    return item


def _list_url(list_id: int, hide_got: bool = False) -> str:
    return f"/lists/{list_id}" + ("?hide_got=1" if hide_got else "")


@app.post("/lists")
def list_create(request: Request, name: str = Form(""), kind: str = Form(""), note: str = Form("")):
    """The "New list" form on /collections. The kind is fixed from here on."""
    db = get_db_session()
    try:
        card_list = card_lists.create_list(db, name, kind, note)
        db.commit()
        url = _list_url(card_list.id)
        if request.headers.get("HX-Request"):
            return Response(status_code=200, headers={"HX-Redirect": url})
        return RedirectResponse(url, status_code=303)
    finally:
        db.close()


@app.get("/lists/{list_id}")
def list_page(request: Request, list_id: int, hide_got: str = ""):
    db = get_db_session()
    try:
        card_list = _get_list(db, list_id)
        detail = card_lists.list_detail(db, card_list)
        hide = card_list.kind == "want" and hide_got == "1"
        rows = [v for v in detail.items if not (hide and v.status == "got")]
        return templates.TemplateResponse(
            request, "card_list.html", {"detail": detail, "card_list": card_list, "rows": rows, "hide_got": hide}
        )
    finally:
        db.close()


def _list_title_response(request: Request, card_list: CardList, editing: bool):
    return templates.TemplateResponse(
        request, "partials/card_list_title.html", {"card_list": card_list, "editing": editing}
    )


@app.get("/lists/{list_id}/title")
def list_title(request: Request, list_id: int, edit: str = ""):
    """The list's name/note heading, or (edit=1) its in-place rename form."""
    db = get_db_session()
    try:
        return _list_title_response(request, _get_list(db, list_id), editing=edit == "1")
    finally:
        db.close()


@app.post("/lists/{list_id}/rename")
def list_rename(request: Request, list_id: int, name: str = Form(""), note: str = Form("")):
    db = get_db_session()
    try:
        card_list = _get_list(db, list_id)
        card_list.name = card_lists.clean_name(name)
        card_list.note = note.strip() or None
        db.commit()
        if not request.headers.get("HX-Request"):
            return RedirectResponse(_list_url(list_id), status_code=303)
        response = _list_title_response(request, card_list, editing=False)
        response.headers["HX-Trigger"] = LIST_CHANGED_EVENT  # the copy text is headed with the name
        return response
    finally:
        db.close()


def _navigate(request: Request, url: str):
    """After a list write that changes the whole page: HX-Redirect for
    htmx, a 303 for a plain form post."""
    if request.headers.get("HX-Request"):
        return Response(status_code=200, headers={"HX-Redirect": url})
    return RedirectResponse(url, status_code=303)


@app.post("/lists/{list_id}/delete")
def list_delete(request: Request, list_id: int):
    """Delete a list and its items (the button asks for confirmation
    first). Cards, masterdata and everything else are untouched."""
    db = get_db_session()
    try:
        db.delete(_get_list(db, list_id))
        db.commit()
        return _navigate(request, "/collections")
    finally:
        db.close()


@app.post("/lists/{list_id}/remove-got-it")
def list_remove_got_it(request: Request, list_id: int, hide_got: str = Form("")):
    """"Remove got-it items (N)": the one way items leave a want list in bulk."""
    db = get_db_session()
    try:
        card_lists.remove_got_it(db, _get_list(db, list_id))
        db.commit()
        return _navigate(request, _list_url(list_id, hide_got == "1"))
    finally:
        db.close()


@app.get("/lists/{list_id}/ad")
def list_make_ad(list_id: int):
    """"Make finn.no ad from this list": hands a sale list to the existing
    /sales → /listings flow, one owned card per print (the print's card with
    most copies). The list itself never builds an ad."""
    db = get_db_session()
    try:
        ids = card_lists.list_detail(db, _get_list(db, list_id)).sale_card_ids
        query = "?" + urlencode({"card_ids": ids}, doseq=True) if ids else ""
        return RedirectResponse("/sales" + query, status_code=303)
    finally:
        db.close()


# An htmx event a list write fires (HX-Trigger): the page's summary region
# (totals, status counts, copy text) re-fetches itself on it, so an
# in-place edit, a removal or a rename never leaves it stale. An event
# rather than an out-of-band swap, because a <tr> response can't carry a
# <div> along with it.
LIST_CHANGED_EVENT = "list-changed"


def _list_row_response(request: Request, db: Session, card_list: CardList, item_id: int, editing=False, changed=False):
    """One table row: display, or (editing) the in-place edit row."""
    detail = card_lists.list_detail(db, card_list)
    view = next(v for v in detail.items if v.item.id == item_id)
    response = templates.TemplateResponse(
        request, "partials/card_list_row.html", {"card_list": card_list, "view": view, "editing": editing}
    )
    if changed:
        response.headers["HX-Trigger"] = LIST_CHANGED_EVENT
    return response


@app.get("/lists/{list_id}/summary")
def list_summary(request: Request, list_id: int, hide_got: str = ""):
    """The list page's summary region (totals, status counts, actions,
    copy text), re-fetched on LIST_CHANGED_EVENT."""
    db = get_db_session()
    try:
        card_list = _get_list(db, list_id)
        detail = card_lists.list_detail(db, card_list)
        return templates.TemplateResponse(
            request,
            "partials/card_list_summary.html",
            {"detail": detail, "card_list": card_list, "hide_got": card_list.kind == "want" and hide_got == "1"},
        )
    finally:
        db.close()


@app.get("/lists/{list_id}/items/{item_id}")
def list_item_row(request: Request, list_id: int, item_id: int, edit: str = ""):
    """An item's row, or (edit=1) its in-place edit row."""
    db = get_db_session()
    try:
        card_list = _get_list(db, list_id)
        _get_list_item(db, list_id, item_id)
        return _list_row_response(request, db, card_list, item_id, editing=edit == "1")
    finally:
        db.close()


@app.post("/lists/{list_id}/items/{item_id}")
def list_item_save(
    request: Request,
    list_id: int,
    item_id: int,
    qty: str = Form("1"),
    target_price: str = Form(""),
    note: str = Form(""),
):
    """In-place edit of an item's qty, target price and note. Its print
    and source never change."""
    db = get_db_session()
    try:
        card_list = _get_list(db, list_id)
        item = _get_list_item(db, list_id, item_id)
        text_qty = qty.strip()
        if not text_qty.isdigit() or int(text_qty) < 1:
            raise FormError("Qty must be a whole number, 1 or more.")
        item.qty = int(text_qty)
        item.target_price = parse_optional_amount(target_price, "Target price")
        item.note = note.strip() or None
        db.commit()
        return _list_row_response(request, db, card_list, item_id, changed=True)
    finally:
        db.close()


@app.post("/lists/{list_id}/items/{item_id}/delete")
def list_item_delete(request: Request, list_id: int, item_id: int):
    """Remove one item. htmx gets an empty 200 (the row's outerHTML swap
    removes it) and the summary refresh event."""
    db = get_db_session()
    try:
        _get_list(db, list_id)
        db.delete(_get_list_item(db, list_id, item_id))
        db.commit()
        if not request.headers.get("HX-Request"):
            return RedirectResponse(_list_url(list_id), status_code=303)
        return HTMLResponse("", headers={"HX-Trigger": LIST_CHANGED_EVENT})
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
        # Cards already in another active listing get a "Listed" badge and a
        # warning (issue #257) -- not a block, listing a card twice can be
        # deliberate, but it should be a conscious choice.
        listed_by_card = queries.active_listings_by_card(db, [c.id for c in cards])
        return templates.TemplateResponse(
            request,
            "sales.html",
            {"cards": cards, "conditions": CARD_CONDITIONS, "listed_by_card": listed_by_card},
        )
    finally:
        db.close()


def _sale_items_from_form(
    db: Session, card_ids: list[int], qtys: list[int], conditions: list[str], prices: list[str]
) -> list[ads.SaleItem]:
    require_same_length(card_id=card_ids, qty=qtys, condition=conditions, price=prices)
    # Blank asking price = "no price in the ad"; anything else must be a
    # finite, non-negative number (issue #228) -- never "nan kr" in an ad.
    parsed_prices = [
        parse_optional_amount(raw, "Asking price", row=i + 1) for i, raw in enumerate(prices)
    ]
    cards_by_id = {c.id: c for c in db.query(Card).filter(Card.id.in_(card_ids)).all()}
    items = []
    for card_id, qty, condition, price in zip(card_ids, qtys, conditions, parsed_prices):
        card = cards_by_id.get(card_id)
        if card is None:
            continue
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
        # Validate before persisting anything (issue #228).
        require_same_length(card_id=card_id, qty=qty, condition=condition, price=price)
        for i, raw in enumerate(price):
            parse_optional_amount(raw, "Asking price", row=i + 1)
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
        # Repeated next to "Mark as listed" (issue #257) so the warning from
        # the review list is still in view at the moment of the write.
        listed_by_card = queries.active_listings_by_card(db, [item.card_id for item in items])
        return templates.TemplateResponse(
            request,
            "partials/ad_draft.html",
            {"draft": draft, "card_ids": card_id, "already_listed_count": len(listed_by_card)},
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
        price = parse_optional_amount(suggested_price, "Suggested price")
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
def _flag(value) -> bool:
    """A checkbox/hidden-input flag as the listings forms send it."""
    return value in (True, "true", "1", "on")


def _listings_url(show_delisted=False, sold_only=False) -> str:
    """The Listings tab, keeping its two filters (issue #255) -- every
    listing action that lands back on the tab (edit, mark-sold cancel, a
    non-htmx delist/delete) should come back to the same filtered view."""
    params = {}
    if _flag(show_delisted):
        params["show_delisted"] = "true"
    if _flag(sold_only):
        params["sold_only"] = "true"
    return "/orders/listings" + ("?" + urlencode(params) if params else "")


@app.get("/listings")
def listings_redirect(request: Request):
    """Old Listings page -- now the Orders page's Listings tab (issue #255).
    308 keeps the full query string (show_delisted, sold_only) for
    bookmarks and old links."""
    return _redirect_keeping_query(request, "/orders/listings")


@app.get("/orders/listings")
def orders_listings(request: Request, show_delisted: bool = False, sold_only: bool = False):
    db = get_db_session()
    try:
        overview = queries.listing_overview(db, include_delisted=show_delisted, sold_only=sold_only)
        context = {
            "tab": "listings",
            "overview": overview,
            "show_delisted": show_delisted,
            "sold_only": sold_only,
            # Tells "no listings at all" apart from "nothing matches these
            # filters" in the empty state.
            "has_any_listings": db.query(Listing.id).first() is not None,
            "active_count": db.query(func.count(Listing.id)).filter(Listing.status == "active").scalar() or 0,
        }
        # Only the filter form's own hx-gets (which target #listings-results)
        # get the bare fragment. Keying this on any HX-Request, as the old
        # /listings did, would hand a fragment with no #main-content to any
        # hx-select="#main-content" swap of this tab -- which then swaps in
        # nothing (issue #255).
        is_results_swap = request.headers.get("HX-Target") == "listings-results"
        template = "partials/listings_results.html" if is_results_swap else "listings.html"
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

        show_delisted_flag = _flag(show_delisted)
        sold_only_flag = _flag(sold_only)
        if not request.headers.get("HX-Request"):
            # Plain form post (no htmx): back to the same filtered tab.
            return RedirectResponse(_listings_url(show_delisted_flag, sold_only_flag), status_code=303)
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
def listings_delete(request: Request, listing_id: int, show_delisted: str = Form(""), sold_only: str = Form("")):
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
        if not request.headers.get("HX-Request"):
            return RedirectResponse(_listings_url(show_delisted, sold_only), status_code=303)
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
            return RedirectResponse("/orders/listings", status_code=303)
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
            return RedirectResponse("/orders/listings", status_code=303)

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
            return templates.TemplateResponse(request, "listing_edit.html", context, status_code=422)

        try:
            price = parse_optional_amount(suggested_price, "Suggested price")
        except FormError as exc:
            # Re-render with everything as submitted -- before, a bad price
            # was silently saved as "no price" (issue #228).
            cards_by_id = {c.id: c for c in cards}
            context = _listing_edit_context(
                listing_id,
                title,
                description,
                suggested_price,
                [cards_by_id[cid] for cid in unique_ids if cid in cards_by_id],
                error=str(exc),
            )
            return templates.TemplateResponse(request, "listing_edit.html", context, status_code=422)

        listing.title = title
        listing.description = description
        listing.suggested_price = price
        listing.cards = cards
        db.commit()
        return RedirectResponse("/orders/listings", status_code=303)
    finally:
        db.close()


def _split_order_fees(total: float | None, prices: list[float]) -> list[float | None]:
    """Split an order-level fee across the order's rows (issue #254), since
    `Transaction.fees` is per row. In proportion to each row's price, or
    evenly when no row has a price. Shares are in whole øre: each row gets
    its exact share rounded down, then the leftover øre go one each to the
    rows with the largest dropped fractions (ties: earlier row first) --
    so the stored fees add up to exactly `total` and none is ever negative.
    A 0-price row in an order where other rows are priced gets no fee.
    None/0 total -> no fees on any row (all None).
    """
    if not total or not prices:
        return [None] * len(prices)
    price_sum = sum(p or 0.0 for p in prices)
    total_cents = round(total * 100)
    exact = [
        total_cents * ((p or 0.0) / price_sum if price_sum > 0 else 1 / len(prices)) for p in prices
    ]
    cents = [int(e) for e in exact]
    leftover = total_cents - sum(cents)
    by_fraction = sorted(range(len(prices)), key=lambda i: (-(exact[i] - cents[i]), i))
    for i in by_fraction[:leftover]:
        cents[i] += 1
    return [c / 100 for c in cents]


def _mark_sold_rows(listing: Listing, submitted: dict[int, str] | None = None) -> list[dict]:
    """One row per card currently in `listing`, each pre-filled with a
    starting-guess price (`suggested_price / card_count`, editable, never
    auto-submitted -- see models.Listing's mark-sold docstring) for the
    mark-sold form. `submitted` (card id -> the raw price as posted) wins
    over the guess, so a rejected submit re-renders what the user typed.
    """
    cards = list(listing.cards)
    default_price = None
    if listing.suggested_price and cards:
        default_price = round(listing.suggested_price / len(cards), 2)
    submitted = submitted or {}
    return [{"card": card, "default_price": submitted.get(card.id, default_price)} for card in cards]


@app.get("/listings/{listing_id}/mark-sold")
def listing_mark_sold_form(request: Request, listing_id: int, show_delisted: bool = False, sold_only: bool = False):
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
            return RedirectResponse(_listings_url(show_delisted, sold_only), status_code=303)
        return templates.TemplateResponse(
            request,
            "listing_mark_sold.html",
            {
                "listing": listing,
                "rows": _mark_sold_rows(listing),
                "today": dt.date.today().isoformat(),
                "error": None,
                "platform": listing.platform or "",
                "fees": "",
                "shipping": "",
                # Cancel returns to the Listings tab as it was filtered (#255).
                "show_delisted": show_delisted,
                "sold_only": sold_only,
                "cancel_url": _listings_url(show_delisted, sold_only),
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
    fees: str = Form(""),
    shipping: str = Form(""),
    show_delisted: str = Form(""),
    sold_only: str = Form(""),
):
    """Creates one `Transaction(type="sale", listing_id=<this listing>.id)`
    per card in the lot, all sharing one fresh `purchase_id` (same grouping
    convention the purchase-cart form uses), then flips `Listing.status` to
    `"sold"` -- all in a single `db.commit()` so a validation failure never
    leaves orphaned Transactions or a status stuck between "active" and
    "sold" (acceptance criteria). Never touches `qty`, `card_collections`,
    or `binder_id` -- same invariant as every other listing action.

    `fees` and `shipping` are optional order-level amounts (issue #254):
    the platform's sale fees and the shipping the seller paid. Fees are
    split across the rows' `fees` by price (`_split_order_fees`); shipping
    is stored once per order in `purchase_shipping`, same as a purchase
    order's, and split at read time by `queries.shipping_shares`. Both come
    off the sale's net proceeds in every Net invested figure.
    """
    db = get_db_session()
    try:
        listing = db.query(Listing).options(selectinload(Listing.cards)).filter(Listing.id == listing_id).first()
        if listing is None:
            raise HTTPException(status_code=404, detail="Listing not found")
        if listing.status == "sold":
            # Re-running mark-sold on an already-sold listing is a no-op --
            # no duplicate Transactions (acceptance criteria).
            return RedirectResponse(_listings_url(show_delisted, sold_only), status_code=303)

        def _rerender(error: str) -> HTMLResponse:
            # 422 + every submitted value re-filled (issue #228).
            return templates.TemplateResponse(
                request,
                "listing_mark_sold.html",
                {
                    "listing": listing,
                    "rows": _mark_sold_rows(listing, dict(zip(card_id, price))),
                    "today": date or dt.date.today().isoformat(),
                    "error": error,
                    "platform": platform,
                    "fees": fees,
                    "shipping": shipping,
                    "show_delisted": _flag(show_delisted),
                    "sold_only": _flag(sold_only),
                    "cancel_url": _listings_url(show_delisted, sold_only),
                },
                status_code=422,
            )

        lot_card_ids = {c.id for c in listing.cards}
        submitted_ids = list(dict.fromkeys(card_id))
        if not submitted_ids or set(submitted_ids) != lot_card_ids:
            return _rerender("Every card currently in this lot needs a price — none can be added or skipped here.")

        if len(price) != len(card_id):
            return _rerender("Missing a price for one or more cards.")

        parsed_prices: dict[int, float] = {}
        for i, (cid, price_raw) in enumerate(zip(card_id, price)):
            try:
                p = parse_amount(price_raw, "Sold price", row=i + 1)
            except FormError:
                p = None
            # A sale needs a real price: finite and above 0 (NaN/inf
            # rejected too, issue #228).
            if p is None or p <= 0:
                return _rerender(f"Sold price on row {i + 1} must be a number above 0.")
            parsed_prices[cid] = p

        try:
            tx_date = parse_date(date)
            fees_total = parse_optional_amount(fees, "Fees")
            shipping_total = parse_optional_amount(shipping, "Shipping")
        except FormError as exc:
            return _rerender(str(exc))

        row_fees = _split_order_fees(fees_total, [parsed_prices[cid] for cid in submitted_ids])
        with _allocating_order_id(db) as new_purchase_id:
            for cid, fee in zip(submitted_ids, row_fees):
                db.add(
                    Transaction(
                        card_id=cid,
                        type="sale",
                        date=tx_date,
                        price=parsed_prices[cid],
                        fees=fee,
                        platform=platform or listing.platform or None,
                        purchase_id=new_purchase_id,
                        purchase_shipping=shipping_total,
                        listing_id=listing.id,
                    )
                )
            listing.status = "sold"
            db.commit()
        # Straight to the new sale order on the Sold tab, expanded (#255).
        return RedirectResponse(f"{_order_url(new_purchase_id, 'sold')}#order-{new_purchase_id}", status_code=303)
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
        "market_price": lambda c: c.display_price if c.display_price is not None else -1,
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
    # Which acquisition rows' copies are still owned -- judged across every
    # transaction, since one card's copies can sit in several orders (see
    # queries.held_acquisition_ids). Feeds each order's Gain (issue #246).
    held_ids = queries.held_acquisition_ids(txs)
    purchase_groups = []
    for pid, group_txs in groups.items():
        # A group can legitimately mix purchase/sale rows (both real cash
        # flow, both belong in the registered total) with trade rows (no
        # cash changes hands -- see models.py Transaction.type) if it was
        # built up piecemeal via direct DB edits (see HANDOFF.md's 2026-09-14
        # entry, order #11). A trade row's price must never contribute to
        # the registered total or its diff against the order's Total.
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
        auto_total = total_price + (purchase_shipping or 0)
        trade = queries.trade_summary(group_txs, trade_prices_then)
        purchase_groups.append(
            {
                "purchase_id": pid,
                "transactions": sorted(group_txs, key=lambda t: t.price, reverse=True),
                "total_price": total_price,
                "total_fees": sum(t.fees or 0 for t in priced_txs),
                "purchase_total": purchase_total,
                "purchase_shipping": purchase_shipping,
                # What Order history's Total column shows when no total has
                # been typed/saved (issue #202): Value + Shipping, rendered as
                # "auto". Display-only -- never stored, and it doesn't make
                # `diff` non-None, so Remaining stays "—" for such an order.
                "auto_total": auto_total,
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
                "trade": trade,
                # Order history's Gain column (issue #246): today's value of
                # this order's still-owned copies minus the Total the row
                # shows (typed, else auto). See queries.order_gain.
                "gain": queries.order_gain(
                    group_txs, purchase_total if purchase_total is not None else auto_total, held_ids, trade
                ),
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
    tab: str = "purchased",
    type_filter: str = "all",
) -> dict:
    """Context for the Orders page's Purchased or Sold tab (issue #255).

    Both tabs group every transaction the same way, then keep only the
    orders (and ungrouped rows) whose `order_tab` is this tab -- the same
    function every post-write redirect uses. The Purchased tab adds the KPI
    header, the `?type=` pills and the card picker; the Sold tab gets its
    own read-straight-off-the-rows summary instead (see `_sold_summary`).
    """
    txs = (
        db.query(Transaction)
        .options(selectinload(Transaction.card))
        .order_by(Transaction.date.desc(), Transaction.id.desc())
        .all()
    )
    txs = _sorted_rows(txs, tsort, tdir, TRANSACTION_SORT_KEYS)
    trade_prices_then = queries.trade_prices_at(db, [t for t in txs if t.type == "trade"])
    all_groups, all_ungrouped = _group_transactions_by_purchase(txs, trade_prices_then)
    purchase_groups = [g for g in all_groups if order_tab(t.type for t in g["transactions"]) == tab]
    ungrouped_transactions = [t for t in all_ungrouped if order_tab([t.type]) == tab]
    page_path = _order_url(None, tab)
    base = {
        "tab": tab,
        "page_path": page_path,
        "error": error,
        "today": dt.date.today().isoformat(),
        "tsort": tsort,
        "tdir": tdir,
        "open_order": open_order,
        # Every order's tab, for links that point at an order from elsewhere
        # on the page (the card picker's Order column).
        "order_tab_by_id": {
            g["purchase_id"]: order_tab(t.type for t in g["transactions"]) for g in all_groups
        },
        # Each purchase row's share of its order's shipping -- shown under
        # the row's price, since it's part of what the card really cost.
        "shipping_by_tx": queries.shipping_shares(txs),
    }

    if tab == "sold":
        # Realized gain per sale row (issue #256): FIFO over every
        # transaction, since the copy a sale used up can sit in any order.
        realized = queries.realized_gains(txs, base["shipping_by_tx"])
        for g in purchase_groups:
            g["realized"] = queries.sum_realized([realized[t.id] for t in g["transactions"]])
            g["listing"] = next((t.listing for t in g["transactions"] if t.listing_id), None)
        return {
            **base,
            "purchase_groups": purchase_groups,
            "ungrouped_transactions": ungrouped_transactions,
            "realized": realized,
            "sold": _sold_summary(purchase_groups, ungrouped_transactions, base["shipping_by_tx"], realized),
        }

    # Purchased tab: the ?type= pills. Counted over this tab's orders before
    # filtering, so each pill says how many orders it would show.
    type_filter = type_filter if type_filter in ORDER_TYPE_FILTERS else "all"
    type_counts = {key: 0 for key in ORDER_TYPE_FILTERS}
    type_counts["all"] = len(purchase_groups)
    for g in purchase_groups:
        type_counts[order_kind(t.type for t in g["transactions"])] += 1
    # The card picker's "Adding to" list: every Purchased-tab order, whatever
    # the pills show -- never a sale order (add-existing-cards writes
    # purchase rows, see add_cards_to_existing_order's guard).
    picker_orders = list(purchase_groups)
    if type_filter != "all":
        purchase_groups = [g for g in purchase_groups if order_kind(t.type for t in g["transactions"]) == type_filter]
        ungrouped_transactions = [t for t in ungrouped_transactions if t.type == type_filter]

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
    gsort = _sort_key(gsort)
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
    # charts below it, which are lazy-loaded via /orders/charts
    # instead so they aren't rebuilt on every column-sort click.
    economic = queries.economic_summary(db)
    cards = queries.all_cards_with_collections(db)
    headline = queries.headline_summary(db, cards)
    collection_breakdown = queries.collection_membership_breakdown(db, cards)
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
        **base,
        "type_filter": type_filter,
        "type_counts": type_counts,
        "picker_orders": picker_orders,
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
        "ungrouped_transactions": ungrouped_transactions,
        # "Facebook wins to register" (#309): pending inbox items, one entry per sale.
        "fb_wins": won_inbox.pending_sales(db),
        "gsort": gsort,
        "gdir": gdir,
        "usort": usort,
        "udir": udir,
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


# Purchased tab's ?type= pills: key -> label. An order's key is its
# `order_kind`; anything not in here (a typo, an old link) means "all".
ORDER_TYPE_FILTERS = {"all": "All", "purchase": "Purchases", "trade": "Trades", "ripped": "Ripped"}


def order_kind(types) -> str:
    """Which Purchased-tab pill an order falls under: "trade" if it has any
    trade row (trade orders are what the trade summary is for), "ripped" if
    it's nothing but ripped rows, otherwise "purchase" -- including an order
    mixing purchases with ripped or sale rows. Exactly one kind per order,
    so the pill counts add up to All.
    """
    types = set(types)
    if "trade" in types:
        return "trade"
    if types == {"ripped"}:
        return "ripped"
    return "purchase"


def _sold_summary(
    groups: list[dict], ungrouped: list[Transaction], shipping_by_tx: dict[int, float], realized: dict
) -> dict:
    """The Sold tab's header. Net received is each row's
    `queries.net_proceeds` (price - fees - its share of seller-paid
    shipping, issue #254) -- the same figure Net invested subtracts.
    Realized gain (issue #256) is net received minus the FIFO cost of each
    copy sold (`queries.realized_gains`), over the rows with a known cost.
    Not Paper gain: that would jump on Mark sold until the next Dex sync
    lowers qty, which reads as realized profit on a tab called Sold."""
    rows = [t for g in groups for t in g["transactions"]] + list(ungrouped)
    sold_for = sum(t.price for t in rows)
    net_received = sum(queries.net_proceeds(t, shipping_by_tx) for t in rows)
    return {
        "sold_for": sold_for,
        "fees_and_shipping": sold_for - net_received,
        "net_received": net_received,
        "realized": queries.sum_realized([realized[t.id] for t in rows]),
        # One sale = one order, or one individually registered row.
        "sales": len(groups) + len(ungrouped),
        "cards": len(rows),
    }


def _render_orders(request: Request, db: Session, tab: str = "purchased", **kwargs):
    """Render the Purchased or Sold tab -- also used by the POST routes that
    re-render a tab with an error message."""
    tsort = kwargs.pop("tsort", "date")
    tdir = kwargs.pop("tdir", "desc")
    return templates.TemplateResponse(
        request, "orders.html", _transactions_context(db, request, tsort, tdir, tab=tab, **kwargs)
    )


@app.get("/transactions")
def transactions_redirect(request: Request):
    """Old Transactions page -- now the Orders page's Purchased tab (#255).
    308 with the full query string, so ?open_order=N deep links survive."""
    return _redirect_keeping_query(request, "/orders/purchased")


@app.get("/orders")
def orders_index(request: Request):
    return _redirect_keeping_query(request, "/orders/purchased")


@app.get("/orders/purchased")
def orders_purchased(
    request: Request,
    tsort: str = "date",
    tdir: str = "desc",
    gsort: str = "date",
    gdir: str = "desc",
    usort: str = "name",
    udir: str = "asc",
    open_order: int | None = None,
    pick: str = "unordered",
    type: str = "all",
):
    db = get_db_session()
    try:
        return _render_orders(
            request, db, "purchased", tsort=tsort, tdir=tdir, gsort=gsort, gdir=gdir, usort=usort, udir=udir,
            open_order=open_order, pick=pick, type_filter=type,
        )
    finally:
        db.close()


@app.get("/orders/sold")
def orders_sold(request: Request, tsort: str = "date", tdir: str = "desc", open_order: int | None = None):
    db = get_db_session()
    try:
        return _render_orders(request, db, "sold", tsort=tsort, tdir=tdir, open_order=open_order)
    finally:
        db.close()


# Serializes Order ID allocation within this process (issue #228 b). On
# SQLite (local, one uvicorn process) this alone stops two request threads
# from both reading the same max(purchase_id); on Postgres (Vercel, several
# instances) a transaction-scoped advisory lock does the same across
# processes. Both are held until the allocating transaction commits.
_ORDER_ID_LOCK = threading.Lock()
_ORDER_ID_ADVISORY_KEY = 228_000_001  # arbitrary, app-wide constant


@contextmanager
def _allocating_order_id(db: Session):
    """Yields a fresh purchase_id (one past the highest in use) for a new
    order, computed when the order is saved -- never when a form opens, so
    two flows (the cart in one tab, Mark sold in another) can't both take
    the same number and merge into one order (issue #228 b).

    The caller adds its rows and calls `db.commit()` *inside* the `with`
    block: the lock is only released after that, so a concurrent save
    can't read the same max in between.
    """
    with _ORDER_ID_LOCK:
        if db.get_bind().dialect.name == "postgresql":
            db.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": _ORDER_ID_ADVISORY_KEY})
        yield (db.query(func.max(Transaction.purchase_id)).scalar() or 0) + 1


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
    # No Order ID here: it's assigned when the cart is registered
    # (create_purchase), not reserved when it opens (issue #228 b).
    return templates.TemplateResponse(
        request,
        "partials/purchase_cart.html",
        {
            "type": type if type in ("purchase", "sale", "trade", "ripped") else "purchase",
            "today": dt.date.today().isoformat(),
        },
    )


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
def purchase_cart_add_row(request: Request, card_id: int, won_item_id: int | None = None, price: str = ""):
    """One cart row. From the Facebook wins panel's "Link selected" (#309)
    it also carries the imported item it's linked to (`won_item_id`), that
    item's note ("<label> · <seller>") and a prefilled price (the item's
    price, or its share of a lot). Every row renders the ref and note
    inputs, blank when unlinked, so they stay index-aligned with
    card_id/price."""
    db = get_db_session()
    try:
        card = db.query(Card).filter(Card.id == card_id).one_or_none()
        if card is None:
            return HTMLResponse("")
        item = db.get(WonItem, won_item_id) if won_item_id is not None else None
        try:
            prefill = f"{float(price):.2f}" if price.strip() else ""
        except ValueError:
            prefill = ""
        return templates.TemplateResponse(
            request,
            "partials/purchase_cart_row.html",
            {"card": card, "won_item": item, "note": won_inbox.note_for(item) if item else "", "price": prefill},
        )
    finally:
        db.close()


@app.get("/orders/fb-wins/{item_id}/cart")
def fb_win_cart(request: Request, item_id: int):
    """"Open in cart" (#309 slice 2): the New Order cart prefilled from one
    won sale (the sale `item_id` belongs to), in one response -- date =
    the sale's end date, platform Facebook, Total = the known prices -- with
    an "Imported items" panel kept outside the form: each item's candidate
    cards (unticked, never auto-linked), "Link selected", and an
    item-scoped search."""
    db = get_db_session()
    try:
        opened = won_inbox.open_sale(db, item_id)
        if opened is None:
            return HTMLResponse(
                '<p class="muted">Nothing from this sale is waiting to be registered any more. Reload the page.</p>'
            )
        return templates.TemplateResponse(
            request,
            "partials/purchase_cart.html",
            {
                "type": "purchase",
                "today": (opened.sale.ended_on or dt.date.today()).isoformat(),
                "imported": opened,
                "candidates": won_inbox.candidates(db, opened.sale.items, ACQUIRED_TYPES),
            },
        )
    finally:
        db.close()


@app.get("/orders/fb-wins/{item_id}/search")
def fb_win_search(request: Request, item_id: int, q: str = ""):
    """An imported item's own "Not listed? Search…": results are checkboxes
    inside that item's block, so "Link selected" links them to that item
    (the cart's general search adds rows with no item attached)."""
    db = get_db_session()
    try:
        item = db.get(WonItem, item_id)
        results = []
        if item is not None and len(q.strip()) >= 2:
            like = _like_pattern(q.strip())
            cards = (
                db.query(Card)
                .filter(func.lower(Card.name).like(like) | func.lower(Card.card_id).like(like))
                .order_by(Card.name)
                .limit(20)
                .all()
            )
            results = won_inbox.annotate_cards(db, cards, item.ended_on, ACQUIRED_TYPES)
        return templates.TemplateResponse(
            request,
            "partials/fb_win_candidates.html",
            {"item": item, "cands": results, "searched": True, "q": q},
        )
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
        target_tab = _order_tab_for(db, purchase_id)
        error = None
        if not cards:
            error = "Select at least one card first."
        elif target_tab is None:
            error = f"Order #{purchase_id} doesn't exist yet -- pick an existing Order ID from History above."
        elif target_tab == "sold":
            # This route writes type="purchase" rows, so adding to a sale
            # order would silently turn it into a mixed purchase/sale order
            # (and move it off the Sold tab). The picker only offers
            # Purchased-tab orders; this guards a stale page or crafted post.
            error = (
                f"Order #{purchase_id} is a sale order -- cards can't be added to it from the card list, "
                "which records purchases. Use that order's Edit order on the Sold tab instead."
            )
        if error:
            return _render_orders(request, db, "purchased", error=error)
        for card in cards:
            _create_default_purchase_transaction(db, purchase_id, card.id)
        return _order_redirect(request, db, purchase_id)
    finally:
        db.close()


def _parse_item_ref(raw: str, row: int) -> int | None:
    """A cart row's `won_item_id`: blank (not linked) or an inbox item's id."""
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        raise FormError(f"Row {row}'s imported-item link is broken -- nothing was saved. Reload the page.") from None


@app.post("/transactions/purchase")
def create_purchase(
    request: Request,
    type: str = Form(...),
    date: str = Form(...),
    platform: str = Form(""),
    purchase_total: str = Form(""),
    purchase_shipping: str = Form(""),
    fees: str = Form(""),
    card_id: list[int] = Form(default=[]),
    price: list[str] = Form(default=[]),
    direction: list[str] = Form(default=[]),
    note: list[str] = Form(default=[]),
    won_item_id: list[str] = Form(default=[]),
    keep_pending: list[int] = Form(default=[]),
):
    """Registers the New Order cart. `purchase_shipping` is the order's
    shipping whatever its type -- on a Sale it's the seller-paid shipping,
    which comes off the sale's net proceeds (issue #254, see
    `queries.shipping_shares`). `fees` is an optional order-level fee
    (platform/payment fees), split across the rows' per-row `fees` by price
    (`_split_order_fees`); ignored for trade/ripped orders, which aren't
    cash orders and never count fees anywhere.

    The order's `purchase_id` is assigned here, inside the save transaction
    (`_allocating_order_id`); a `purchase_id` posted by the client (an old
    cached cart form) is ignored, so a Mark sold in another tab can never
    merge into this order (issue #228 b).

    Facebook wins (#309): each row also posts `note` and `won_item_id`
    (blank on rows not linked to an imported item), index-aligned with
    card_id/price. A linked row's transaction gets the note; every linked
    item becomes `registered` on this order in the same commit, except
    those in `keep_pending` (a lot not complete yet), which stay pending
    pointing at it. Items not linked aren't touched: they stay in the inbox.
    A form with neither list at all (a cart opened before this change) is
    read as all-blank.
    """
    # Validate everything before touching the DB (issue #228): a rejected
    # value is a plain-text 422 shown in the cart's error slot, and the
    # cart stays as typed.
    type = parse_tx_type(type)
    tx_date = parse_date(date)
    total_value = parse_optional_amount(purchase_total, "Total")
    shipping_value = parse_optional_amount(purchase_shipping, "Shipping")
    fees_value = parse_optional_amount(fees, "Fees")
    if not note and not won_item_id:
        note = won_item_id = [""] * len(card_id)
    require_same_length(card_id=card_id, price=price, note=note, won_item_id=won_item_id)
    parsed = [parse_amount(raw, "Price", row=i + 1) for i, raw in enumerate(price)]
    item_refs = [_parse_item_ref(raw, row=i + 1) for i, raw in enumerate(won_item_id)]
    db = get_db_session()
    try:
        if not card_id:
            return _render_orders(
                request, db, order_tab([type]),
                error="No cards added to the order yet — search for at least one card first.",
            )
        try:
            linked = won_inbox.linkable_items(db, {r for r in item_refs if r is not None})
        except won_inbox.LinkError as exc:
            raise FormError(str(exc)) from None
        prices = [_price_for(type, p) for p in parsed]
        cash_order = type in ("purchase", "sale")
        row_fees = _split_order_fees(fees_value if cash_order and fees_value else None, prices)
        with _allocating_order_id(db) as purchase_id:
            for i, (cid, p) in enumerate(zip(card_id, prices)):
                db.add(
                    Transaction(
                        card_id=cid,
                        type=type,
                        direction=_trade_direction(type, direction, i),
                        date=tx_date,
                        price=p,
                        fees=row_fees[i],
                        platform=platform or None,
                        purchase_id=purchase_id,
                        purchase_total=total_value,
                        purchase_shipping=shipping_value,
                        note=note[i].strip() or None,
                    )
                )
            won_inbox.mark_registered(linked, purchase_id, set(keep_pending))
            db.commit()
        # Same open_order + hx-select/hx-target/hx-swap="outerHTML" pattern
        # as set_purchase_total below -- htmx swaps in just the newly
        # registered order's <details> (open, per open_order) instead of
        # navigating the whole page, so any other expanded groups / sort
        # state on Transactions survive registering an order. Lands on the
        # order's own Orders tab (HX-Redirect if that's not the current one).
        return _order_redirect(request, db, purchase_id)
    finally:
        db.close()


@app.post("/transactions/purchase/{purchase_id}/total")
def set_purchase_total(
    request: Request,
    purchase_id: int,
    purchase_total: str = Form(""),
    purchase_shipping: str = Form(""),
    platform: str | None = Form(None),
):
    """Sets the order's Total, shipping cost, and platform on every row
    already sharing this purchase_id -- Order history's per-order
    total/shipping/platform form, editable after the fact for orders built
    up piecemeal (e.g. via direct reconciliation) rather than through the
    cart form.

    Blank-field semantics (issue #202, closing HANDOFF.md's 2026-09-20 open
    item):

    - **Total**: blank clears it (NULL), which puts the order back on the
      automatic Value + Shipping display with Remaining "—". A typed value
      is stored on every row and drives Remaining/Distribute as before.
    - **Shipping** and **Platform**: blank means "leave as is" -- the rows
      keep whatever they already have. Before, a blank field NULLed them on
      every row, so e.g. a mixed-platform order (whose platform field
      prefills blank on purpose, see _group_transactions_by_purchase) lost
      every row's platform just by saving a Total. To actually zero
      shipping, enter 0; to clear/split platforms, use Edit order's
      per-row fields. A non-blank platform still overwrites every row.
    """
    total_value = parse_optional_amount(purchase_total, "Total")
    shipping_value = parse_optional_amount(purchase_shipping, "Shipping")
    db = get_db_session()
    try:
        values: dict = {"purchase_total": total_value}
        if shipping_value is not None:
            values["purchase_shipping"] = shipping_value
        if platform and platform.strip():
            values["platform"] = platform.strip()
        db.query(Transaction).filter(Transaction.purchase_id == purchase_id).update(values)
        db.commit()
        # The form swaps hx-select="#order-N" out of the redirect target, so
        # the target must be the tab that order is actually listed on.
        return _order_redirect(request, db, purchase_id)
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
            return RedirectResponse("/orders/purchased", status_code=303)
        # Back/links go to the tab this order is on *now* -- after a save
        # that retyped every row, that may not be the tab it came from.
        tab = order_tab(t.type for t in txs)
        purchase_total = next((t.purchase_total for t in txs if t.purchase_total is not None), None)
        purchase_shipping = next((t.purchase_shipping for t in txs if t.purchase_shipping is not None), None)
        # No Total saved yet -- default the field to shipping + the
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
                "saved": saved,
                "order_tab": tab,
                "order_tab_label": ORDER_TABS[tab],
                "back_url": _order_url(purchase_id, tab),
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
            {"purchase_id": purchase_id, "tx": tx},
        )
    finally:
        db.close()


def _parse_target_order_id(raw: str, row: int) -> int | None:
    """An Edit order row's Order ID field: blank means "start a new order"
    (ID assigned at save), otherwise a whole number."""
    value = (raw or "").strip()
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        raise FormError(
            f"Order ID on row {row} must be a whole number, or blank to start a new order."
        ) from None


def _apply_purchase_edits(
    db, purchase_id, split_order_id, tx_id, delete_set, row_types, row_dates, row_prices,
    row_targets, platform, note, card_id, direction, total_value, shipping_value,
) -> None:
    """update_purchase's write pass, every query scoped to `purchase_id`."""
    for i, txid in enumerate(tx_id):
        if txid in delete_set:
            continue
        tx = (
            db.query(Transaction)
            .filter(Transaction.id == txid, Transaction.purchase_id == purchase_id)
            .one_or_none()
        )
        if tx is None:
            continue
        tx.card_id = card_id[i]
        tx.type = row_types[i]
        tx.direction = _trade_direction(row_types[i], direction, i)
        tx.date = row_dates[i]
        tx.price = _price_for(row_types[i], row_prices[i])
        tx.platform = platform[i] or None
        tx.note = note[i] or None
        target_purchase_id = row_targets[i] if row_targets[i] is not None else split_order_id
        if target_purchase_id != purchase_id:
            tx.purchase_id = target_purchase_id
            tx.purchase_total = None
            tx.purchase_shipping = None
        else:
            tx.purchase_id = purchase_id
            tx.purchase_total = total_value
            tx.purchase_shipping = shipping_value
    if delete_set:
        db.query(Transaction).filter(
            Transaction.id.in_(delete_set), Transaction.purchase_id == purchase_id
        ).delete(synchronize_session=False)


@app.post("/transactions/purchase/{purchase_id}/edit")
def update_purchase(
    request: Request,
    purchase_id: int,
    tx_id: list[int] = Form(default=[]),
    type: list[str] = Form(default=[]),
    date: list[str] = Form(default=[]),
    price: list[str] = Form(default=[]),
    platform: list[str] = Form(default=[]),
    note: list[str] = Form(default=[]),
    card_id: list[int] = Form(default=[]),
    new_purchase_id: list[str] = Form(default=[]),
    delete_tx_id: list[int] = Form(default=[]),
    purchase_total: str = Form(""),
    purchase_shipping: str = Form(""),
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

    A blank Order ID (what the row's "Start new order" button sets) splits
    the row into one new order whose ID is assigned here, at save time --
    every blank row in the same save lands in that same new order. The page
    never pre-reserves a number (issue #228 b).

    Every posted `tx_id`/`delete_tx_id` must belong to this order: one that
    belongs to another order is rejected with a 422 and nothing is written
    (issue #228 b), so a stale or crafted form can't edit or delete another
    order's rows. An id that no longer exists at all is skipped, as before
    (e.g. a row already deleted in another tab).

    Facebook wins items registered on this order follow its rows in the
    same commit (#317, `won_inbox.follow_order_edit`): a merge or a whole
    move takes them along, a split takes each item where its own rows
    (matched by the note Register gave them) went.
    """
    # Validate every row being kept before writing any (issue #228):
    # misaligned lists used to IndexError into a 500, and a bad
    # type/date/price on row 5 would otherwise only be caught after rows
    # 1-4 were already changed. A row being deleted needs no valid fields
    # (deleting a mistyped row must not be blocked by the typo).
    delete_set = set(delete_tx_id)
    kept = [i for i, txid in enumerate(tx_id) if txid not in delete_set]
    row_types: dict[int, str] = {}
    row_dates: dict[int, dt.date] = {}
    row_prices: dict[int, float] = {}
    row_targets: dict[int, int | None] = {}  # None = start a new order
    if kept:
        require_same_length(
            tx_id=tx_id, type=type, date=date, price=price, platform=platform,
            note=note, card_id=card_id, new_purchase_id=new_purchase_id,
        )
        for i in kept:
            row_types[i] = parse_tx_type(type[i], row=i + 1)
            row_dates[i] = parse_date(date[i], row=i + 1)
            row_prices[i] = parse_amount(price[i], "Price", row=i + 1)
            row_targets[i] = _parse_target_order_id(new_purchase_id[i], row=i + 1)
    total_value = parse_optional_amount(purchase_total, "Total")
    shipping_value = parse_optional_amount(purchase_shipping, "Shipping")
    db = get_db_session()
    try:
        # Scope every edit/delete to this order (issue #228 b): reject, before
        # any write, a posted id that belongs to a different order.
        posted_ids = set(tx_id) | delete_set
        foreign = (
            db.query(Transaction.id, Transaction.purchase_id)
            .filter(Transaction.id.in_(posted_ids))
            .filter((Transaction.purchase_id != purchase_id) | Transaction.purchase_id.is_(None))
            .order_by(Transaction.id)
            .all()
            if posted_ids
            else []
        )
        if foreign:
            fid, fpid = foreign[0]
            where = f"order #{fpid}" if fpid is not None else "no order"
            raise FormError(
                f"Transaction {fid} belongs to {where}, not order #{purchase_id} -- nothing was saved. "
                "Reload the page and try again."
            )
        # Only take the Order ID lock when a row is actually being split off.
        needs_new_order = any(t is None for t in row_targets.values())
        # Every row this order has before the save, with its note then: how
        # its Facebook wins items find where their rows went (#317).
        notes_before = dict(
            db.query(Transaction.id, Transaction.note).filter(Transaction.purchase_id == purchase_id).all()
        )
        with _allocating_order_id(db) if needs_new_order else nullcontext(None) as split_order_id:
            _apply_purchase_edits(
                db, purchase_id, split_order_id, tx_id, delete_set, row_types, row_dates, row_prices,
                row_targets, platform, note, card_id, direction, total_value, shipping_value,
            )
            db.flush()
            won_inbox.follow_order_edit(db, purchase_id, notes_before)
            db.commit()
        # Stay on the edit page (with a "Saved" confirmation and a Back
        # button) so the user can check the result or keep editing -- unless
        # every row was moved out/deleted, leaving nothing here to show.
        # Nothing left: back to the tab the page is on (or Purchased).
        remaining = db.query(Transaction).filter(Transaction.purchase_id == purchase_id).count()
        target = (
            f"/transactions/purchase/{purchase_id}/edit?saved=1"
            if remaining
            else _order_url(None, _current_orders_tab(request) or "purchased")
        )
        return RedirectResponse(target, status_code=303)
    finally:
        db.close()


@app.post("/transactions")
def create_transaction(
    request: Request,
    card_id: int = Form(...),
    type: str = Form(...),
    date: str = Form(...),
    price: str = Form(...),
    platform: str = Form(""),
    fees: str = Form(""),
    purchase_id: int | None = Form(None),
    upsert: bool = Form(False),
):
    type = parse_tx_type(type)
    tx_date = parse_date(date)
    price_value = parse_amount(price, "Price")
    fees_value = parse_optional_amount(fees, "Fees")
    db = get_db_session()
    try:
        card = db.query(Card).filter(Card.id == card_id).one_or_none()
        if card is None:
            return _render_orders(
                request, db, order_tab([type]), error="Card not found — pick one from the search results."
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
            existing.date = tx_date
            existing.price = _price_for(type, price_value)
            existing.purchase_id = purchase_id
        else:
            tx = Transaction(
                card_id=card.id,
                type=type,
                date=tx_date,
                price=_price_for(type, price_value),
                platform=platform or None,
                fees=fees_value,
                purchase_id=purchase_id,
            )
            db.add(tx)
        db.commit()
        if purchase_id is not None:
            return _order_redirect(request, db, purchase_id)
        return RedirectResponse(_order_url(None, order_tab([type])), status_code=303)
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
    price: str = Form(...),
    platform: str = Form(""),
    fees: str = Form(""),
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
    # Rejected -> plain-text 422 into the row form's error slot; the row
    # stays in edit mode with the input as typed (issue #228).
    type = parse_tx_type(type)
    tx_date = parse_date(date)
    price_value = parse_amount(price, "Price")
    fees_value = parse_optional_amount(fees, "Fees")
    db = get_db_session()
    try:
        tx = db.query(Transaction).filter(Transaction.id == tx_id).one_or_none()
        if tx is None:
            return HTMLResponse("")
        tx.date = tx_date
        tx.type = type
        tx.direction = _trade_direction(type, [direction], 0)
        tx.price = _price_for(type, price_value)
        tx.platform = platform or None
        tx.fees = fees_value
        tx.purchase_id = purchase_id
        db.commit()
        db.refresh(tx)
        # The row is swapped in place, so a type/Order ID edit that moves it
        # (or its whole order) to the other tab would leave it on the wrong
        # one until reload. Say so in the row rather than reshuffling the
        # page under the user (issue #255).
        new_tab = _order_tab_for(db, tx.purchase_id) or order_tab([tx.type])
        current_tab = _current_orders_tab(request)
        moved_to = new_tab if current_tab in ("purchased", "sold") and new_tab != current_tab else None
        return templates.TemplateResponse(
            request,
            "partials/tx_row_view.html",
            {
                "tx": tx,
                "moved_to": moved_to,
                "moved_to_url": _order_url(tx.purchase_id, new_tab) if moved_to else None,
                "moved_to_label": ORDER_TABS.get(new_tab),
            },
        )
    finally:
        db.close()


# --------------------------------------------------------------------------
# Run log (import_log) -- shown on /sync-status
# --------------------------------------------------------------------------
LOG_SORT_KEYS = {
    "ran_at": lambda log: log.ran_at,
    "job": lambda log: log.job_name,
    "status": lambda log: log.status_name,
    "source": lambda log: log.source,
    "files": lambda log: (log.files or "").lower(),
    "cards_created": lambda log: log.cards_created,
    "cards_updated": lambda log: log.cards_updated,
    "cards_flagged_missing": lambda log: log.cards_flagged_missing,
    "warnings_count": lambda log: log.warnings_count,
    "collections_touched": lambda log: (log.collections_touched or "").lower(),
    "binders_touched": lambda log: (log.binders_touched or "").lower(),
}

# Dex syncs plus the daily price refresh: 30 rows is roughly two weeks.
_SYNC_LOG_LIMIT = 30


def _recent_import_logs(
    db: Session, lsort: str = "ran_at", ldir: str = "desc", limit: int = _SYNC_LOG_LIMIT
) -> list[ImportLog]:
    logs = db.query(ImportLog).order_by(ImportLog.ran_at.desc(), ImportLog.id.desc()).limit(limit).all()
    return _sorted_rows(logs, lsort, ldir, LOG_SORT_KEYS)


# --------------------------------------------------------------------------
# Sync status (issue #264) -- the slimmed-down former Activity Log: an
# at-a-glance block per background job, "Missing from Dex" (the one write:
# deleting a flagged card, see missing_cards.py), then the run log.
# --------------------------------------------------------------------------
@app.get("/sync-status")
def sync_status_page(request: Request, lsort: str = "ran_at", ldir: str = "desc"):
    db = get_db_session()
    try:
        # A run killed before releasing its job lock (issue #340) shows up
        # here as "failed, interrupted" once the lock is stale, instead of
        # only on that job's next run. One SELECT when nothing is held.
        job_locks.reap_stale(db)
        context = {
            "overview": sync_status.overview(db),
            "missing": missing_cards.flagged_cards(db),
            "logs": _recent_import_logs(db, lsort, ldir),
            "lsort": lsort,
            "ldir": ldir,
            "job_labels": sync_status.JOB_LABELS,
        }
        return templates.TemplateResponse(request, "sync_status.html", context)
    finally:
        db.close()


@app.get("/sync-status/log")
def sync_log_partial(request: Request, lsort: str = "ran_at", ldir: str = "desc"):
    """The run log's own column-sort target (see partials/import_log.html):
    swaps just that section back in via htmx, so a sort doesn't reload the
    page and jump back above the at-a-glance block."""
    db = get_db_session()
    try:
        return templates.TemplateResponse(
            request,
            "partials/import_log.html",
            {
                "logs": _recent_import_logs(db, lsort, ldir),
                "lsort": lsort,
                "ldir": ldir,
                "job_labels": sync_status.JOB_LABELS,
            },
        )
    finally:
        db.close()


# Old addresses of this page (#159 merged the Sync Log into /releases; #264
# renamed it again). 308s keep any lsort/ldir query string.
@app.get("/import")
def import_redirect(request: Request):
    return _redirect_keeping_query(request, "/sync-status")


@app.get("/releases")
def releases_redirect(request: Request):
    return _redirect_keeping_query(request, "/sync-status")


@app.get("/releases/sync-log")
def releases_sync_log_redirect(request: Request):
    return _redirect_keeping_query(request, "/sync-status/log")


def _already_running_response(result: jobs.AlreadyRunningResult) -> JSONResponse:
    return JSONResponse(
        status_code=409,
        content={
            "status": "already_running",
            "job": result.job,
            "started_at": result.started_at.isoformat(),
            "trigger": result.trigger,
            "error": result.message,
        },
    )


@app.get("/cron/dropbox-sync")
def cron_dropbox_sync(trigger: str = Depends(cron_auth.require_cron_secret)):
    """Scheduled sync, triggered by the Vercel Cron job in vercel.json.

    A thin wrapper over `jobs.run_dex_sync` (issue #274), which holds the
    job body, its /sync-status rows and its single-flight lock; this only
    checks the secret and maps the result to JSON.

    Pulls every CSV currently in the configured Dropbox folder and runs a
    normal sync -- no sync ever deletes cards, only flags missing ones (see
    importer.py). If the import's circuit breaker trips (empty/header-only
    My Collection, or a mass drop), nothing is written and this returns 409
    with `"status": "aborted"` -- the cron never overrides the breaker (it
    has no way to pass `allow_mass_missing`). Protected by
    CRON_SECRET rather than the Supabase login: Vercel's cron invocations
    carry no browser session to log in with. Vercel automatically sends
    `Authorization: Bearer <CRON_SECRET>` on cron requests when that env
    var is set -- see README "Automatic daily sync". A manual trigger (e.g.
    from a tool that can't set custom headers, or a person running this by
    hand mid-day) may instead pass the same value as `?secret=` (deprecated,
    kept until #199's Run-now buttons exist). The check itself, shared by
    every /cron route and failing closed, is cron_auth.require_cron_secret.

    The two are told apart: only a request carrying that exact header is
    trusted as the real scheduled Vercel invocation (trigger "cron",
    CardSnapshot.source="cron"); a `?secret=` request is a manual
    off-schedule run (trigger and snapshot source "manual") even though it
    hits this same route -- see snapshots.record_daily_snapshot and
    HANDOFF.md. With no CRON_SECRET configured (only allowed locally,
    without login) there's no way to tell the two apart, so every request
    is treated as "manual".

    A second request while a run is in progress gets 409
    `{"status": "already_running"}` and writes nothing (issue #340).
    """
    # Auth and trigger ("cron" for the Bearer header, else "manual"):
    # cron_auth.require_cron_secret (#226), shared by every /cron route.
    db = get_db_session()
    try:
        result = jobs.run_dex_sync(db, trigger=trigger)
    finally:
        db.close()
    if isinstance(result, jobs.AlreadyRunningResult):
        return _already_running_response(result)
    if result.status == "failed":
        raise HTTPException(status_code=502, detail=result.error)
    if result.status == "aborted":
        # Unattended: a non-2xx so the cron run shows as failed.
        return JSONResponse(
            status_code=409,
            content={"status": "aborted", "folder": result.folder, "files": result.files, "error": result.error},
        )
    if result.status == "empty":
        return {
            "status": "ok",
            "folder": result.folder,
            "message": "No CSV files found",
            "cards_snapshotted": result.cards_snapshotted,
        }
    imported = result.import_result
    return {
        # Always "ok" since #349: the sync no longer looks up TCGplayer
        # prices, so it can't be degraded by the FX fallback (#229).
        "status": "ok",
        "folder": result.folder,
        "files_synced": result.files,
        "cards_created": imported.cards_created,
        "cards_updated": imported.cards_updated,
        "cards_flagged_missing": imported.cards_flagged_missing,
        "collections_touched": sorted(imported.collections_touched),
        "binders_touched": sorted(imported.binders_touched),
        "warnings": imported.warnings,
        "unowned_rows_skipped": imported.unowned_rows_skipped,
        "cards_snapshotted": result.cards_snapshotted,
    }


@app.get("/cron/price-refresh")
def cron_price_refresh(trigger: str = Depends(cron_auth.require_cron_secret)):
    """Scheduled TCGplayer price refresh, decoupled from Dex sync (see
    price_refresh.py and issue #93) -- its own Vercel Cron entry in
    vercel.json, separate from /cron/dropbox-sync's schedule so pricing
    keeps moving even on a day the Dex sync doesn't run. A thin wrapper over
    `jobs.run_price_refresh` (issue #274). Same CRON_SECRET-gated pattern
    as /cron/dropbox-sync -- see that route's docstring for the
    scheduled-vs-manual distinction, which also decides the CardSnapshot
    source recorded ("price-cron" vs "manual", both distinct from the Dex
    sync's own "cron"/"manual" sources -- see queries.real_value_history).
    """
    # Auth and trigger ("cron" for the Bearer header, else "manual"):
    # cron_auth.require_cron_secret (#226), shared by every /cron route.
    db = get_db_session()
    try:
        run = jobs.run_price_refresh(db, trigger=trigger)
    finally:
        db.close()
    if isinstance(run, jobs.AlreadyRunningResult):
        return _already_running_response(run)
    result, images = run.pokemontcg, run.images
    return {
        # "degraded" (issue #229) is still a 200: the snapshot and image
        # pass ran.
        "status": run.status,
        "degraded_reason": run.degraded_reason,
        "usd_to_nok": result.usd_to_nok,
        "fx_source": result.fx_source,
        "fx_as_of": result.fx_as_of.isoformat() if result.fx_as_of else None,
        # The pokemontcg.io pass (issue #349).
        "requests": result.requests,
        "batch_requests": result.batch_requests,
        "fallback_searches": result.fallback_searches,
        "cards_checked": result.cards_checked,
        "cards_updated": result.cards_updated,
        "cards_unmatched": result.cards_unmatched,
        "cards_low_confidence": result.cards_low_confidence,
        "cards_variant_uncertain": result.cards_variant_uncertain,
        "cards_backed_off": result.cards_backed_off,
        "cards_deferred": result.cards_deferred,
        "ids_found": result.ids_found,
        "transient_errors": result.transient_errors,
        "stopped": result.stopped,
        "cards_skipped": result.cards_skipped,
        "cards_snapshotted": run.cards_snapshotted,
        "images_attempted": images.attempted,
        "images_filled": images.filled,
        "tcgdex": _tcgdex_summary(run.tcgdex),
    }


def _tcgdex_summary(result) -> dict | None:
    if result is None:
        return None
    return {
        "cards_checked": result.cards_checked,
        "cards_priced": result.cards_priced,
        "ids_matched": result.ids_matched,
        "cards_unmatched": result.cards_unmatched,
        "cards_variant_uncertain": result.cards_variant_uncertain,
        "transient_errors": result.transient_errors,
        "stopped": result.stopped,
        "http_calls": result.http_calls,
        "eur_to_nok": result.eur_to_nok,
        "fx_source": result.fx_source,
        "status": result.status,
        "degraded_reason": result.degraded_reason,
    }


@app.get("/cron/image-backfill")
def cron_image_backfill(limit: int = 100, trigger: str = Depends(cron_auth.require_cron_secret)):
    """Manual catch-up pass for missing card images (backfill_images.py) --
    the daily /cron/price-refresh already does a small one; this lets a
    person fill in a whole collection in a few calls instead of waiting.
    A thin wrapper over `jobs.run_image_backfill` (issue #274). Same
    CRON_SECRET gate as the other /cron routes. Time-boxed, so call it
    again while `remaining` > 0.
    """
    db = get_db_session()
    try:
        result = jobs.run_image_backfill(db, trigger=trigger, limit=limit)
    finally:
        db.close()
    if isinstance(result, jobs.AlreadyRunningResult):
        return _already_running_response(result)
    return {
        "status": result.status,
        "attempted": result.attempted,
        "filled": result.filled,
        "cards_with_image": result.cards_with_image,
        "remaining": result.remaining,
    }


@app.get("/cron/set-sync")
def cron_set_sync(trigger: str = Depends(cron_auth.require_cron_secret)):
    """Monthly set metadata sync (set_sync.py): `total_cards` for Dashboard
    completion, plus a `release_rank` for any set still missing one --
    existing ranks are never overwritten from here (see
    `set_sync.sync_set_metadata`). A route rather than only the script
    because Vercel is where api.pokemontcg.io is reachable from, and
    without it `total_cards` stayed empty on prod (0 of 109 sets, found
    24.09.2026). A thin wrapper over `jobs.run_set_sync` (issue #274).
    Same CRON_SECRET gate as the other /cron routes.
    """
    db = get_db_session()
    try:
        result = jobs.run_set_sync(db, trigger=trigger)
    finally:
        db.close()
    if isinstance(result, jobs.AlreadyRunningResult):
        return _already_running_response(result)
    return {"status": result.status, "matched": result.matched, "unmatched": result.unmatched}


# --------------------------------------------------------------------------
# Facebook wins inbox (#309) -- fb_auction_watcher sends the lots you won;
# they wait in `won_items` (won_inbox.py) until registered as an order. The
# endpoint skips the login (the extension has no session) and is guarded by
# its own INBOX_TOKEN, never CRON_SECRET: a leaked token can only add or
# refresh pending inbox rows, never reach transactions or cards.
# --------------------------------------------------------------------------
def _inbox_auth_error(request: Request) -> JSONResponse | None:
    """None if the request may write to the inbox, else the refusal.

    Fails closed (like the cron routes, cron_auth.py, #226): with
    INBOX_TOKEN set, only `Authorization: Bearer <INBOX_TOKEN>` is accepted,
    compared in constant time, and never a query-string secret. With it
    unset, the endpoint is refused whenever login is configured (a deploy
    missing the variable), and open only when login isn't (local dev)."""
    token = os.environ.get("INBOX_TOKEN", "").strip()
    if not token:
        if auth.is_configured():
            return JSONResponse(
                {"status": "error", "error": "The inbox is off: INBOX_TOKEN isn't set on this server."},
                status_code=503,
            )
        return None
    header = request.headers.get("authorization", "")
    scheme, _, given = header.partition(" ")
    if scheme.lower() != "bearer" or not hmac.compare_digest(given.strip().encode(), token.encode()):
        return JSONResponse(
            {"status": "error", "error": "Missing or wrong token: send Authorization: Bearer <INBOX_TOKEN>."},
            status_code=401,
            headers={"WWW-Authenticate": "Bearer"},
        )
    return None


def _inbox_error(message: str, status_code: int) -> JSONResponse:
    return JSONResponse({"status": "error", "error": message}, status_code=status_code)


async def _read_capped_body(request: Request, limit: int) -> bytes | None:
    """The body, or None once it passes `limit` bytes (checked on the
    declared length first, then while reading, for a chunked body)."""
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            if int(declared) > limit:
                return None
        except ValueError:
            return None
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > limit:
            return None
    return bytes(body)


def _store_won_items(items: list[won_inbox.WonItemIn]) -> dict:
    db = get_db_session()
    try:
        result = won_inbox.upsert_items(db, items)
        db.commit()
        return result.as_dict()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


@app.post("/inbox/fb-wins")
async def inbox_fb_wins(request: Request):
    """Receives `{"format": "fbaw-won", "version": 1, ...}` from
    fb_auction_watcher (contract: that app's docs/spec.md "Sending wins to
    tcg_inventory") and upserts it into `won_items` by external_ref.
    Writes nothing at all on any refusal: wrong/missing token, too big,
    not JSON, unknown version, or any item that doesn't check out."""
    refused = _inbox_auth_error(request)
    if refused is not None:
        return refused
    body = await _read_capped_body(request, won_inbox.MAX_BODY_BYTES)
    if body is None:
        return _inbox_error(f"The body is larger than {won_inbox.MAX_BODY_BYTES // 1024} KB.", 413)
    try:
        data = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return _inbox_error("The body isn't valid JSON.", 400)
    try:
        items = won_inbox.parse_payload(data)
    except won_inbox.PayloadError as exc:
        return _inbox_error(str(exc), 422)
    try:
        counts = await run_in_threadpool(_store_won_items, items)
    except IntegrityError:
        # Two sends racing to add the same new ref: nothing was written; the next send is fine.
        return _inbox_error("Another send was writing the same items. Send again.", 409)
    return {"status": "ok", "received": len(items), **counts}


@app.post("/orders/fb-wins/{item_id}/ignore")
def ignore_fb_win(request: Request, item_id: int):
    """Ignore one pending inbox item (a cancelled or duplicate win): it
    leaves the list and later sends never bring it back. Behind the normal
    login (not under /inbox/, which skips it)."""
    db = get_db_session()
    try:
        won_inbox.ignore_item(db, item_id)
        db.commit()
        if request.headers.get("hx-request"):
            return templates.TemplateResponse(
                request, "partials/fb_wins_inbox.html", {"fb_wins": won_inbox.pending_sales(db)}
            )
        return RedirectResponse("/orders/purchased#fb-wins", status_code=303)
    finally:
        db.close()


@app.post("/orders/fb-wins/{item_id}/complete")
def complete_fb_win(request: Request, item_id: int):
    """"Lot complete" for a lot registered as not complete (#309): it's
    pending and points at its order; once the rest of its cards are on that
    order (Edit order), this marks it registered and it leaves the list."""
    db = get_db_session()
    try:
        won_inbox.complete_item(db, item_id)
        db.commit()
        if request.headers.get("hx-request"):
            return templates.TemplateResponse(
                request, "partials/fb_wins_inbox.html", {"fb_wins": won_inbox.pending_sales(db)}
            )
        return RedirectResponse("/orders/purchased#fb-wins", status_code=303)
    finally:
        db.close()


# --------------------------------------------------------------------------
# Orders charts -- the former standalone Analyse page's value-growth and
# cash-flow charts, now a collapsible "View charts" section on the Orders
# page's Purchased tab (folded in since the two were always read together).
# Its own endpoint, fetched lazily via hx-get on first expand, so the charts
# aren't rebuilt on every page load or column-sort click while collapsed --
# see orders.html's "View charts" <details>. Was /transactions/charts before
# issue #255, which now 308s here.
# --------------------------------------------------------------------------
@app.get("/analyse")
def analyse_redirect():
    return RedirectResponse("/orders/purchased", status_code=308)


@app.get("/transactions/charts")
def transactions_charts_redirect(request: Request):
    return _redirect_keeping_query(request, "/orders/charts")


@app.get("/orders/charts")
def orders_charts(request: Request, metric: str = "total", period: str = "all"):
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
                    request, db, headline, economic, txs, metric, period, path="/orders/charts"
                ),
                "cash_flow": cash_flow,
            },
        )
    finally:
        db.close()


# --------------------------------------------------------------------------
# The in-app Wiki was removed (issue #264) -- README.md is the one reference
# now. Old bookmarks land on the Dashboard.
# --------------------------------------------------------------------------
@app.get("/wiki")
def wiki_redirect():
    return RedirectResponse("/", status_code=308)


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
