"""eBay sold prices from PokeTrace, as a reference-only source (issue #414).

PokeTrace (`https://api.poketrace.com/v1`, free tier, key in
`POKETRACE_API_KEY`, sent as `X-API-Key`) aggregates eBay sold listings per
card and condition, with outliers already cleaned. We store one number per
card: eBay's **Near Mint `median30d`** in USD, converted to NOK at Norges
Bank's rate (fx_rates), as `card_prices` source `poketrace_ebay`, with
`sample_size` (the tier's `saleCount`) and `window_days` (30; PokeTrace
doesn't document which window `saleCount` covers, see README "Pricing").

**Reference-only.** `poketrace_ebay` is in `pricing.REFERENCE_SOURCES`, not
in `CHAIN`, so `pricing.resolve` never picks it: no market price, snapshot
or total changes because of this module. It never calls
`pricing.resolve_cards` either (there is nothing for it to change).

**Which cards** (`select_cards`): in hand (qty > 0, not flagged missing),
`market_price` >= POKETRACE_MIN_PRICE_NOK (default 20), most valuable
first, and due (no `poketrace_ebay` row fetched or failed in the last 7
days). Korean cards are skipped up front (PokeTrace has no Korean game),
and so are prints PokeTrace has no variant for (Poke Ball / Master Ball
patterns, cosmos holo, stamps, ...): no request, no failure recorded.

**Matching, never guessed** (a wrong price is worse than none):

1. A known PokeTrace ID (`master_card_ids` source `poketrace`, written on
   the first match) is refreshed without searching: in batches of up to 20
   via `tcgplayer_ids` when the match also stored `refs.tcgplayerId`
   (source `tcgplayer`), else `GET /cards/{id}`.
2. The Cardmarket shortcut (`cardmarket_ids`) is not used: on the free tier
   it's HTTP 403 UPGRADE_REQUIRED (EU market data needs Pro, checked live
   2026-10-14), and US records carry `refs.cardmarketId` null anyway.
3. Otherwise a search by name + card number + game (+ variant filter), and
   a candidate counts only if game, number, variant and name all agree and
   it is the only one that does (`match_candidates`). None or several ->
   `record_failure`, retried after 7 days.

**Limits:** >= 2.1 s between requests (burst limit ~1 per 2 s); a 429 is
honoured once (`retryAfter`) and a second one ends the run; the time box,
the daily request cap and `x-ratelimit-remaining` are checked before each
request. 401/403 ends the run. The key is only ever in the request header:
never in a log line, an exception message or `import_log`.
"""
from __future__ import annotations

import datetime as dt
import os
import re
import time
from dataclasses import dataclass, field
from typing import Callable

import httpx
from sqlalchemy import and_, case, exists, func, literal, or_
from sqlalchemy.orm import Session, selectinload

import constants
import fx_rates
import masterdata
import pricing
from models import Card, CardPrice, MasterCard
from tcgdex_prices import names_match, normalize_local_id

API_BASE = "https://api.poketrace.com/v1"
KEY_ENV = "POKETRACE_API_KEY"
SOURCE = pricing.SOURCE_POKETRACE_EBAY
SOURCE_ID = "poketrace"  # master_card_ids.source for PokeTrace's card UUID
SOURCE_TCGPLAYER = "tcgplayer"  # master_card_ids.source for refs.tcgplayerId
TIER = "NEAR_MINT"
WINDOW_DAYS = 30
MARKET = "US"

# masterdata language -> PokeTrace `game`. No Korean game exists.
GAMES = {"int": "pokemon", "ja": "pokemon-japanese", "zh-hans": "pokemon-chinese"}

# masterdata variant code -> PokeTrace `variant` values (lowercased, letters
# and digits only) that are exactly that print. Same narrow policy as
# card_images._TCGPLAYER_KEYS (PokeTrace's variants are TCGplayer's
# printings). A code not listed (other than "unspecified") is a print
# PokeTrace has no record for, and is skipped before any request.
VARIANTS: dict[str, tuple[str, ...]] = {
    "normal": ("normal", "unlimited"),
    "holo": ("holofoil",),
    "reverse_holo": ("reverseholofoil",),
    "first_edition": ("1stedition",),
    "first_edition_holo": ("1steditionholofoil",),
}
# The `variant` search filter per code. "normal" has two PokeTrace values
# (Normal / Unlimited), so it isn't filtered; the matcher checks it.
_VARIANT_FILTER = {
    "holo": "Holofoil",
    "reverse_holo": "Reverse_Holofoil",
    "first_edition": "1st_Edition",
    "first_edition_holo": "1st_Edition_Holofoil",
}
_UNSPECIFIED = "unspecified"

REFRESH_EVERY_DAYS = 7
BATCH_SIZE = 20  # tcgplayer_ids per request, and PokeTrace's max `limit`
REQUEST_INTERVAL_S = 2.1
MAX_RETRY_AFTER_S = 10.0
DEFAULT_RETRY_AFTER_S = 2.0
MAX_CONSECUTIVE_ERRORS = 3
# A median whose newest NM sale PokeTrace dates more than this long ago
# isn't stored (same limit as the TCGdex / pokemontcg sources).
MAX_UPSTREAM_AGE_DAYS = 30
_TIMEOUT = 15.0

DEFAULT_MIN_SALES = 5
DEFAULT_MIN_PRICE_NOK = 20.0
DEFAULT_DAILY_REQUESTS = 200
DEFAULT_SECONDS = 60.0

# Seams for tests (conftest makes _SLEEP instant; tests swap in fakes).
_SLEEP = time.sleep
_CLOCK = time.monotonic


def _http_get(*args, **kwargs):
    return httpx.get(*args, **kwargs)


# --------------------------------------------------------------------------
# Settings (env, read per run)
# --------------------------------------------------------------------------
def api_key() -> str | None:
    value = (os.environ.get(KEY_ENV) or "").strip()
    return value or None


def _env_number(name: str, default, cast):
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = cast(raw)
    except ValueError:
        return default
    return value if value >= 0 else default


def min_sales() -> int:
    """POKETRACE_MIN_SALES: fewer NM sales than this -> `few_sales`, and the
    card page shows "Few sales" instead of the value."""
    return _env_number("POKETRACE_MIN_SALES", DEFAULT_MIN_SALES, int)


def min_price_nok() -> float:
    return _env_number("POKETRACE_MIN_PRICE_NOK", DEFAULT_MIN_PRICE_NOK, float)


def daily_requests() -> int:
    return _env_number("POKETRACE_DAILY_REQUESTS", DEFAULT_DAILY_REQUESTS, int)


def time_budget_s() -> float:
    return _env_number("POKETRACE_SECONDS", DEFAULT_SECONDS, float)


def shows_value(row) -> bool:
    """The card page's display gate for a `poketrace_ebay` row: a price, at
    least min_sales() sales behind it, and not `low_confidence`. Otherwise
    the row reads "Few sales" (stored, but too thin to compare)."""
    if row.price_nok is None:
        return False
    if (row.sample_size or 0) < min_sales():
        return False
    return pricing.FLAG_LOW_CONFIDENCE not in pricing.flag_list(row.flags)


def shown_price_nok_expr():
    """`shows_value` as a SQL expression over `CardPrice`: its `price_nok`
    when the gate passes, else NULL. Used to sort Inventory's eBay sold
    column (#416) so few-sales / low-confidence rows sort with the cards
    that have no row at all. Keep in step with `shows_value` above.
    Flags are stored comma-joined (pricing._join_flags), hence the
    `,flags,` LIKE `%,low_confidence,%` test."""
    padded_flags = literal(",") + func.coalesce(CardPrice.flags, "") + literal(",")
    return case(
        (
            and_(
                CardPrice.price_nok.isnot(None),
                func.coalesce(CardPrice.sample_size, 0) >= min_sales(),
                ~padded_flags.like(f"%,{pricing.FLAG_LOW_CONFIDENCE},%"),
            ),
            CardPrice.price_nok,
        ),
        else_=None,
    )


def rows_by_card(db: Session, card_ids) -> dict[int, CardPrice]:
    """Each card's `poketrace_ebay` row, for a page of cards, in one IN
    query (Inventory's eBay sold column, #416). Cards without a row are
    simply absent."""
    ids = list(card_ids)
    if not ids:
        return {}
    rows = db.query(CardPrice).filter(CardPrice.source == SOURCE, CardPrice.card_id.in_(ids)).all()
    return {row.card_id: row for row in rows}


# --------------------------------------------------------------------------
# HTTP client
# --------------------------------------------------------------------------
class Stop(Exception):
    """The run must end now. `reason`: time | daily_cap | quota |
    rate_limited | forbidden | errors."""

    def __init__(self, reason: str, detail: str = ""):
        super().__init__(f"{reason}{': ' + detail if detail else ''}")
        self.reason = reason


class TransientError(Exception):
    """A timeout, connection error, 5xx or unreadable body: the card is
    skipped without stamping anything and tried again next run."""


class Client:
    """PokeTrace GET with pacing, the 429 rule and the run's limits. Counts
    every request it sends (`requests`), retries included."""

    def __init__(
        self,
        key: str,
        *,
        max_requests: int | None = None,
        time_budget_s: float | None = None,
        http_get: Callable | None = None,
        clock: Callable[[], float] | None = None,
        sleep: Callable[[float], None] | None = None,
    ):
        self._key = key
        self._get = http_get or _http_get
        self.clock = clock or _CLOCK
        self._sleep = sleep or (lambda seconds: _SLEEP(seconds))
        self.max_requests = max_requests
        self.deadline = self.clock() + time_budget_s if time_budget_s is not None else None
        self.requests = 0
        self.rate_remaining: int | None = None
        self._retried_429 = False
        self._last_request: float | None = None

    def __repr__(self) -> str:  # never the key
        return f"<poketrace_prices.Client requests={self.requests}>"

    def _check_limits(self) -> None:
        if self.max_requests is not None and self.requests >= self.max_requests:
            raise Stop("daily_cap")
        if self.rate_remaining is not None and self.rate_remaining <= 0:
            raise Stop("quota")
        if self.deadline is not None and self.clock() >= self.deadline:
            raise Stop("time")

    def _pace(self) -> None:
        if self._last_request is not None:
            wait = self._last_request + REQUEST_INTERVAL_S - self.clock()
            if wait > 0:
                self._sleep(wait)

    def get(self, path: str, params: dict | None = None):
        """The decoded JSON body; None for a 4xx other than 401/403/429
        (e.g. 404: no such card). Raises Stop or TransientError."""
        self._check_limits()
        self._pace()
        self._check_limits()
        self.requests += 1
        try:
            response = self._get(
                f"{API_BASE}{path}",
                params=params,
                headers={"X-API-Key": self._key, "Accept": "application/json"},
                timeout=_TIMEOUT,
            )
        except httpx.HTTPError as exc:
            self._last_request = self.clock()
            raise TransientError(f"{path}: {exc.__class__.__name__}") from None
        self._last_request = self.clock()
        remaining = _int_header(response, "x-ratelimit-remaining")
        if remaining is not None:
            self.rate_remaining = remaining
        status = response.status_code
        if status == 429:
            body = _json_or_none(response) or {}
            code = body.get("code") if isinstance(body, dict) else None
            if self._retried_429 or (code and code != "BURST_RATE_LIMIT_EXCEEDED"):
                raise Stop("rate_limited", f"{path}: HTTP 429 {code or ''}".strip())
            self._retried_429 = True
            self._sleep(_retry_after(response, body if isinstance(body, dict) else {}))
            self._last_request = None  # the wait above was the pause
            return self.get(path, params)
        if status in (401, 403):
            body = _json_or_none(response)
            code = body.get("code") if isinstance(body, dict) else None
            raise Stop("forbidden", f"{path}: HTTP {status} {code or ''}".strip())
        if status >= 500:
            raise TransientError(f"{path}: HTTP {status}")
        if status >= 400:
            return None
        body = _json_or_none(response)
        if body is None:
            raise TransientError(f"{path}: invalid JSON")
        return body


def _json_or_none(response):
    try:
        return response.json()
    except ValueError:
        return None


def _int_header(response, name: str) -> int | None:
    try:
        return int(response.headers.get(name))
    except (TypeError, ValueError):
        return None


def _retry_after(response, body: dict) -> float:
    value = body.get("retryAfter")
    if value is None:
        value = response.headers.get("retry-after")
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        seconds = DEFAULT_RETRY_AFTER_S
    return max(0.0, min(seconds, MAX_RETRY_AFTER_S))


# --------------------------------------------------------------------------
# Matching (pure)
# --------------------------------------------------------------------------
def variant_code(dex_variant: str | None) -> str:
    return masterdata.normalize_variant(dex_variant)


def has_poketrace_print(dex_variant: str | None) -> bool:
    code = variant_code(dex_variant)
    return code == _UNSPECIFIED or code in VARIANTS


def _variant_form(value: str | None) -> str:
    return re.sub(r"[^a-z0-9]", "", (value or "").lower())


def variant_matches(dex_variant: str | None, api_variant: str | None) -> bool:
    code = variant_code(dex_variant)
    if code == _UNSPECIFIED:
        return True
    return _variant_form(api_variant) in VARIANTS.get(code, ())


def _split_number(value: str | None) -> tuple[str | None, str | None]:
    if not value:
        return None, None
    local, _, size = str(value).partition("/")
    size = size.strip()
    return normalize_local_id(local), (str(int(size)) if size.isdigit() else None)


def numbers_match(api_number: str | None, dex_number: str | None) -> bool:
    """Same printed number ("062" == "62"), and the same set size when both
    give one ("199/165" vs "199/165")."""
    api_local, api_size = _split_number(api_number)
    dex_local, dex_size = _split_number(dex_number)
    if not api_local or api_local != dex_local:
        return False
    return api_size is None or dex_size is None or api_size == dex_size


@dataclass(frozen=True)
class Target:
    game: str
    name: str
    number: str
    variant: str | None


def target_for(card: Card) -> Target | None:
    """What to look for, or None when PokeTrace can't have this print:
    Korean, an uncovered language, a print with no PokeTrace variant, or no
    name/number to pin it by."""
    master = card.master_card
    if master is None or master.language not in GAMES:
        return None
    if constants.language_code(card.language) == "KR":
        return None
    if not has_poketrace_print(card.variant):
        return None
    number = card.number or master.printed_number or master.number
    if not number or not card.name:
        return None
    return Target(game=GAMES[master.language], name=card.name, number=number, variant=card.variant)


@dataclass(frozen=True)
class Match:
    record: dict | None
    reason: str | None = None


def _agrees(record, target: Target) -> bool:
    if not isinstance(record, dict) or not record.get("id"):
        return False
    if record.get("market") not in (None, MARKET):
        return False
    if record.get("game") != target.game:
        return False
    if not numbers_match(record.get("cardNumber"), target.number):
        return False
    if not variant_matches(target.variant, record.get("variant")):
        return False
    return names_match(target.name, record.get("name"))


def match_candidates(records, target: Target) -> Match:
    """The one search result that is `target` (game, number, variant and
    name all agree), or no match with the reason: none, or several."""
    survivors = [r for r in records or [] if _agrees(r, target)]
    if len(survivors) == 1:
        return Match(survivors[0])
    if not survivors:
        return Match(None, f"no PokeTrace card matches {target.name} {target.number} ({target.game})")
    return Match(None, f"ambiguous: {len(survivors)} PokeTrace cards match {target.name} {target.number}")


def verify_known(record, poketrace_id: str, target: Target) -> Match:
    """A record fetched by its stored ID: still that ID and that number."""
    if not isinstance(record, dict) or record.get("id") != poketrace_id:
        return Match(None, "stored PokeTrace ID not returned")
    if not numbers_match(record.get("cardNumber"), target.number):
        return Match(None, f"stored PokeTrace ID now returns number {record.get('cardNumber')}")
    return Match(record)


# --------------------------------------------------------------------------
# The price (pure)
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class EbayPrice:
    price: float
    sample_size: int | None
    updated: dt.date | None
    flags: tuple[str, ...]


def _parse_date(value) -> dt.date | None:
    if not value or not isinstance(value, str):
        return None
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def ebay_price(record: dict, today: dt.date, min_sales_n: int) -> tuple[EbayPrice | None, str | None]:
    """eBay's NM `median30d` from a PokeTrace record, or (None, reason).
    Flags: `low_confidence` when PokeTrace says so (a tier without a
    `confidence` field, as in the live US records, is not low), `few_sales`
    under `min_sales_n` sales. `updated` is the tier's own `lastUpdated`,
    falling back to the record's."""
    tier = ((record.get("prices") or {}).get("ebay") or {}).get(TIER)
    if not isinstance(tier, dict):
        return None, "no eBay Near Mint sales"
    median = tier.get("median30d")
    if not isinstance(median, (int, float)) or isinstance(median, bool) or median <= 0:
        return None, "no eBay Near Mint 30-day median"
    updated = _parse_date(tier.get("lastUpdated")) or _parse_date(record.get("lastUpdated"))
    if updated is not None and (today - updated).days > MAX_UPSTREAM_AGE_DAYS:
        return None, f"newest eBay Near Mint data is from {updated.isoformat()}"
    count = tier.get("saleCount")
    sample = count if isinstance(count, int) and not isinstance(count, bool) else None
    flags = []
    if str(tier.get("confidence") or "").lower() == "low":
        flags.append(pricing.FLAG_LOW_CONFIDENCE)
    if (sample or 0) < min_sales_n:
        flags.append(pricing.FLAG_FEW_SALES)
    return EbayPrice(float(median), sample, updated, tuple(flags)), None


# --------------------------------------------------------------------------
# Which cards
# --------------------------------------------------------------------------
def due_filter(today: dt.date):
    """No poketrace_ebay row fetched or failed in the last 7 days."""
    cutoff = today - dt.timedelta(days=REFRESH_EVERY_DAYS)
    recent = exists().where(
        CardPrice.card_id == Card.id,
        CardPrice.source == SOURCE,
        or_(CardPrice.fetched_at > cutoff, CardPrice.lookup_failed_at > cutoff),
    )
    return ~recent


@dataclass
class Selection:
    cards: list[Card]
    korean_skipped: int = 0
    uncovered_skipped: int = 0


def select_cards(db: Session, today: dt.date, min_price: float | None = None) -> Selection:
    min_price = min_price_nok() if min_price is None else min_price
    candidates = (
        db.query(Card)
        .join(MasterCard, MasterCard.id == Card.master_card_id)
        .options(selectinload(Card.prices), selectinload(Card.master_card).selectinload(MasterCard.external_ids))
        .filter(
            and_(
                Card.qty > 0,
                Card.flagged_missing_since.is_(None),
                Card.market_price.isnot(None),
                Card.market_price >= min_price,
                MasterCard.language.in_(list(GAMES)),
                due_filter(today),
            )
        )
        .all()
    )
    selection = Selection(cards=[])
    for card in candidates:
        if constants.language_code(card.language) == "KR":
            selection.korean_skipped += 1
        elif target_for(card) is None:
            selection.uncovered_skipped += 1
        else:
            selection.cards.append(card)
    selection.cards.sort(key=lambda c: (-(c.market_price or 0), c.id))
    return selection


# --------------------------------------------------------------------------
# The refresh
# --------------------------------------------------------------------------
@dataclass
class RefreshResult:
    cards_due: int = 0
    cards_checked: int = 0
    cards_priced: int = 0
    cards_thin: int = 0  # priced, but few sales / low confidence
    ids_matched: int = 0
    cards_unmatched: list[str] = field(default_factory=list)
    korean_skipped: int = 0
    uncovered_skipped: int = 0
    transient_errors: int = 0
    requests: int = 0
    rate_remaining: int | None = None
    stopped: str | None = None
    usd_to_nok: float | None = None
    fx_source: str | None = None
    status: str = "ok"
    degraded_reason: str | None = None

    @property
    def cards_left(self) -> int:
        return max(0, self.cards_due - self.cards_checked)


def _label(card: Card) -> str:
    variant = f" {card.variant}" if card.variant else ""
    return f"{card.name} ({card.card_id or '?'}{variant})"


def _external_id(master: MasterCard, source: str) -> str | None:
    row = next((r for r in master.external_ids if r.source == source), None)
    return row.external_id if row else None


class _Run:
    def __init__(self, db: Session, client: Client, today: dt.date, rate: float, min_sales_n: int, result):
        self.db, self.client, self.today, self.rate = db, client, today, rate
        self.min_sales_n, self.result = min_sales_n, result
        self.consecutive_errors = 0
        self.pending = 0

    def fail(self, card: Card, reason: str) -> None:
        pricing.record_failure(card, SOURCE, self.today)
        self.result.cards_unmatched.append(f"{_label(card)}: {reason}")
        self._checked()

    def store(self, card: Card, record: dict, *, new_match: bool) -> None:
        master = card.master_card
        if new_match:
            masterdata.set_external_id(self.db, master, SOURCE_ID, str(record["id"]), masterdata.MATCHED_VERIFIED)
            tcgplayer_id = (record.get("refs") or {}).get("tcgplayerId")
            if tcgplayer_id:
                masterdata.set_external_id(
                    self.db, master, SOURCE_TCGPLAYER, str(tcgplayer_id), masterdata.MATCHED_VERIFIED
                )
            self.result.ids_matched += 1
        price, reason = ebay_price(record, self.today, self.min_sales_n)
        if price is None:
            self.fail(card, reason)
            return
        pricing.record_price(
            card,
            SOURCE,
            price_nok=round(price.price * self.rate, 2),
            price=price.price,
            currency="USD",
            fx_rate=self.rate,
            variant_key=record.get("variant"),
            fetched_at=self.today,
            source_updated_at=price.updated,
            flags=price.flags,
            sample_size=price.sample_size,
            window_days=WINDOW_DAYS,
        )
        self.result.cards_priced += 1
        if price.flags:
            self.result.cards_thin += 1
        self._checked()

    def _checked(self) -> None:
        self.result.cards_checked += 1
        self.consecutive_errors = 0
        self.pending += 1
        if self.pending >= 10:
            self.db.commit()
            self.pending = 0

    def transient(self) -> None:
        self.result.transient_errors += 1
        self.consecutive_errors += 1
        if self.consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
            raise Stop("errors")


def _batch_lookup(run: _Run, cards: list[Card]) -> None:
    """Up to BATCH_SIZE distinct TCGplayer IDs in one `tcgplayer_ids`
    request (more pages only if PokeTrace has more prints than `limit`)."""
    product_ids = list(dict.fromkeys(_external_id(c.master_card, SOURCE_TCGPLAYER) for c in cards))
    params = {"tcgplayer_ids": ",".join(product_ids), "market": MARKET, "limit": BATCH_SIZE}
    by_id: dict[str, dict] = {}
    try:
        while True:
            body = run.client.get("/cards", params)
            if not isinstance(body, dict):
                break
            for record in body.get("data") or []:
                if isinstance(record, dict) and record.get("id"):
                    by_id[str(record["id"])] = record
            pagination = body.get("pagination") or {}
            if not (pagination.get("hasMore") and pagination.get("nextCursor")):
                break
            params = {**params, "cursor": pagination["nextCursor"]}
    except TransientError:
        run.transient()
        return
    for card in cards:
        poketrace_id = _external_id(card.master_card, SOURCE_ID)
        match = verify_known(by_id.get(poketrace_id), poketrace_id, target_for(card))
        if match.record is None:
            run.fail(card, match.reason)
        else:
            run.store(card, match.record, new_match=False)


def _single_lookup(run: _Run, card: Card) -> None:
    poketrace_id = _external_id(card.master_card, SOURCE_ID)
    try:
        body = run.client.get(f"/cards/{poketrace_id}")
    except TransientError:
        run.transient()
        return
    record = body.get("data") if isinstance(body, dict) else None
    match = verify_known(record, poketrace_id, target_for(card))
    if match.record is None:
        run.fail(card, match.reason)
    else:
        run.store(card, match.record, new_match=False)


def search_params(target: Target) -> dict:
    params = {
        "search": target.name,
        "card_number": target.number,
        "game": target.game,
        "market": MARKET,
        "limit": BATCH_SIZE,
    }
    variant = _VARIANT_FILTER.get(variant_code(target.variant))
    if variant:
        params["variant"] = variant
    return params


def _search_lookup(run: _Run, card: Card) -> None:
    target = target_for(card)
    try:
        body = run.client.get("/cards", search_params(target))
    except TransientError:
        run.transient()
        return
    records = body.get("data") if isinstance(body, dict) else None
    match = match_candidates(records or [], target)
    if match.record is None:
        run.fail(card, match.reason)
    else:
        run.store(card, match.record, new_match=True)


def refresh_poketrace_prices(
    db: Session,
    client: Client,
    *,
    today: dt.date | None = None,
    min_sales_n: int | None = None,
    min_price: float | None = None,
) -> RefreshResult:
    """One pass over the due cards, within `client`'s limits. Commits what
    it wrote; never resolves a market price (reference-only)."""
    today = today or dt.date.today()
    min_sales_n = min_sales() if min_sales_n is None else min_sales_n
    result = RefreshResult()
    rates = fx_rates.get_rates(db.get_bind())
    result.usd_to_nok = rates.to_nok("USD")
    result.fx_source = rates.source
    if not rates.usable("USD"):
        result.status = "degraded"
        result.degraded_reason = fx_rates.FALLBACK_REASON
        result.stopped = "fx_unavailable"
        return result

    selection = select_cards(db, today, min_price)
    result.korean_skipped = selection.korean_skipped
    result.uncovered_skipped = selection.uncovered_skipped
    result.cards_due = len(selection.cards)

    batched, rest = [], []
    for card in selection.cards:
        master = card.master_card
        if _external_id(master, SOURCE_ID) and _external_id(master, SOURCE_TCGPLAYER):
            batched.append(card)
        else:
            rest.append(card)

    run = _Run(db, client, today, rates.to_nok("USD"), min_sales_n, result)
    try:
        chunk: list[Card] = []
        product_ids: set[str] = set()
        for card in batched:
            product_id = _external_id(card.master_card, SOURCE_TCGPLAYER)
            if product_id not in product_ids and len(product_ids) >= BATCH_SIZE:
                _batch_lookup(run, chunk)
                chunk, product_ids = [], set()
            chunk.append(card)
            product_ids.add(product_id)
        if chunk:
            _batch_lookup(run, chunk)
        for card in rest:
            if _external_id(card.master_card, SOURCE_ID):
                _single_lookup(run, card)
            else:
                _search_lookup(run, card)
    except Stop as stop:
        result.stopped = stop.reason
    db.commit()
    result.requests = client.requests
    result.rate_remaining = client.rate_remaining
    return result


STOP_MESSAGES = {
    "time": "stopped at the time limit",
    "daily_cap": "stopped at today's request cap",
    "quota": "stopped: PokeTrace's daily quota is used up",
    "rate_limited": "stopped: rate-limited by PokeTrace (HTTP 429 twice)",
    "forbidden": "stopped: PokeTrace refused the key (HTTP 401/403)",
    "errors": f"stopped after {MAX_CONSECUTIVE_ERRORS} errors in a row",
    "fx_unavailable": "no USD/NOK rate, nothing fetched",
}


def summary_line(result: RefreshResult) -> str:
    parts = [f"{result.requests} requests", f"priced {result.cards_priced} of {result.cards_due} due"]
    if result.cards_thin:
        parts.append(f"{result.cards_thin} with few sales or low confidence")
    parts.append(f"{len(result.cards_unmatched)} unmatched")
    if result.ids_matched:
        parts.append(f"{result.ids_matched} new matches")
    if result.transient_errors:
        parts.append(f"{result.transient_errors} transient errors")
    if result.korean_skipped:
        parts.append(f"{result.korean_skipped} Korean skipped")
    if result.uncovered_skipped:
        parts.append(f"{result.uncovered_skipped} prints PokeTrace doesn't list skipped")
    if result.stopped:
        parts.append(f"{STOP_MESSAGES.get(result.stopped, result.stopped)}, {result.cards_left} cards left")
    if result.rate_remaining is not None:
        parts.append(f"PokeTrace says {result.rate_remaining} requests left today")
    return "; ".join(parts)
