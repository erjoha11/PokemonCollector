"""TCGdex as a price source (issue #211, pricing Phase 3 of epic #213).

One TCGdex card request (`https://api.tcgdex.net/v2/{en|ja}/cards/{id}`,
free, no key) carries two independent prices, stored as two `card_prices`
sources (see pricing.CHAIN for where they sit in the display chain):

- `tcgdex_tcgplayer` -- `pricing.tcgplayer`, USD `marketPrice` per print
  ("normal", "reverse-holofoil", "holofoil", ...). International cards only
  in practice (TCGdex has no TCGplayer data for Japanese prints). A backup
  for, and cross-check of, Dex's own TCGplayer price.
- `tcgdex_cardmarket` -- Cardmarket, EUR. The only free source we have for
  Japanese cards besides Dex itself. Which field is "the price" and how the
  `-holo` fields and per-print products map onto Dex's Variant is decided in
  `choose_cardmarket` below (and README "Pricing").

IDs. TCGdex's card IDs aren't Dex's: set codes differ in case/padding
("sv2a" -> "SV2a", pokemontcg.io's "sv3" -> "sv03", "sv3pt5" -> "sv03.5")
and local IDs are zero-padded ("62" -> "062"). Unlike card_images'
image lookup (where a guessed-then-checked ID is tolerable, see
backfill_images.py's 2026-09-16 tcgdex-guess incident), a price only ever
comes from an ID that was *verified*: the set is looked up in TCGdex's own
set list, the card in that set's own card list by printed number, and the
fetched card must then match Dex's set, number, set size and (for English
cards) name. Only then is the ID stored in `master_card_ids` (source
`tcgdex`), and every later refresh fetches by that ID -- no search.

`matched_by` says how it was verified (masterdata.MATCHED_VERIFIED for set
+ number + name, MATCHED_VERIFIED_NUMBER for Japanese cards, whose TCGdex
names are in Japanese and can't be compared with Dex's English ones -- set
+ number + printed set size only).

Refresh. `refresh_tcgdex_prices` runs inside `/cron/price-refresh`,
time-boxed like the image backfill. ~870 cards on a 7-day cadence is ~125
lookups a day. Requests are sequential with a short pause between them; a
429 or 5xx is retried once after a back-off, and a 429 that persists stops
the run for the day. A failed or blocked call never clears or overwrites a
stored price (pricing.py's stale rule keeps showing it); a transient error
doesn't even stamp a failure, so the card is simply tried again next run.

CLI (same DATABASE_URL convention as price_refresh.py):

    python tcgdex_prices.py [--limit N] [--dry-run]
"""
from __future__ import annotations

import argparse
import datetime as dt
import re
import time
import unicodedata
from dataclasses import dataclass, field
from typing import Callable

import httpx
from sqlalchemy import and_, exists
from sqlalchemy.orm import Session, aliased, selectinload

import card_images
import fx_rates
import masterdata
import pricing
from models import Card, CardPrice, MasterCard

API_BASE = "https://api.tcgdex.net/v2"
SOURCE_ID = "tcgdex"  # master_card_ids.source
SOURCE_CM = pricing.SOURCE_TCGDEX_CARDMARKET
SOURCE_TP = pricing.SOURCE_TCGDEX_TCGPLAYER
SOURCES = (SOURCE_CM, SOURCE_TP)

# masterdata language -> TCGdex catalog. zh-hans (3 cards) isn't covered.
LANGUAGES = {"int": "en", "ja": "ja"}

# ~870 cards refreshed weekly (price_refresh.PRICE_STALE_AFTER_DAYS) is ~125
# card lookups a day. Set/set-list requests needed to resolve new IDs come on
# top, but are cached per run (one per set, not per card).
MAX_LOOKUPS_PER_RUN = 125
STALE_AFTER_DAYS = 7
RETRY_AFTER_DAYS = 14
# A price TCGdex itself last updated longer ago than this isn't used: a
# stale upstream (like pokemontcg.io's Cardmarket block, frozen since
# 2025-11-21) must not look fresh just because we fetched it today.
MAX_UPSTREAM_AGE_DAYS = 30

_TIMEOUT = 10.0
REQUEST_INTERVAL_S = 0.2  # pause between requests (be polite, limits unknown)
BACKOFF_S = 3.0  # wait before the one retry of a 429/5xx/timeout
MAX_RETRY_AFTER_S = 15.0
MAX_CONSECUTIVE_ERRORS = 3  # transient errors in a row -> give up for today


# Indirection so the test suite can make every pause instant (conftest.py).
_SLEEP = time.sleep


class TransientError(Exception):
    """A request that failed for reasons that say nothing about the card
    (timeout, 5xx, rate limit) -- never recorded as a failed lookup."""


class RateLimited(TransientError):
    pass


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------
class Client:
    """Sequential, polite TCGdex client with per-run caches for the set
    lists used to resolve IDs. `get` returns None on 404 and raises
    TransientError when a retry didn't help."""

    def __init__(self, sleep: Callable[[float], None] | None = None):
        self._sleep = sleep or (lambda seconds: _SLEEP(seconds))
        self._last_request: float | None = None
        self.calls = 0
        self._set_index: dict[str, dict[str, dict]] = {}
        self._set_detail: dict[tuple[str, str], dict | None] = {}

    def _pause(self) -> None:
        if self._last_request is not None:
            wait = REQUEST_INTERVAL_S - (time.monotonic() - self._last_request)
            if wait > 0:
                self._sleep(wait)

    def get(self, path: str):
        url = f"{API_BASE}/{path.lstrip('/')}"
        for attempt in range(2):
            self._pause()
            self.calls += 1
            try:
                response = httpx.get(url, timeout=_TIMEOUT)
            except httpx.HTTPError as exc:
                self._last_request = time.monotonic()
                if attempt == 0:
                    self._sleep(BACKOFF_S)
                    continue
                raise TransientError(f"{url}: {exc.__class__.__name__}") from exc
            self._last_request = time.monotonic()
            if response.status_code == 404:
                return None
            if response.status_code == 429 or response.status_code >= 500:
                if attempt == 0:
                    self._sleep(_retry_after(response))
                    continue
                cls = RateLimited if response.status_code == 429 else TransientError
                raise cls(f"{url}: HTTP {response.status_code}")
            if response.status_code >= 400:
                return None
            try:
                return response.json()
            except ValueError as exc:
                raise TransientError(f"{url}: invalid JSON") from exc
        raise TransientError(url)  # pragma: no cover (loop always returns/raises)

    def set_index(self, lang: str) -> dict[str, dict]:
        """TCGdex's sets for `lang`, keyed by lowercased set ID."""
        if lang not in self._set_index:
            payload = self.get(f"{lang}/sets") or []
            self._set_index[lang] = {s["id"].lower(): s for s in payload if isinstance(s, dict) and s.get("id")}
        return self._set_index[lang]

    def set_detail(self, lang: str, set_id: str) -> dict | None:
        key = (lang, set_id)
        if key not in self._set_detail:
            self._set_detail[key] = self.get(f"{lang}/sets/{set_id}")
        return self._set_detail[key]


def _retry_after(response) -> float:
    try:
        value = float(response.headers.get("retry-after", ""))
    except ValueError:
        return BACKOFF_S
    return max(0.0, min(value, MAX_RETRY_AFTER_S))


# --------------------------------------------------------------------------
# ID resolution + verification (pure helpers)
# --------------------------------------------------------------------------
_LOCAL_ID = re.compile(r"^([A-Za-z]*)0*(\d+)([A-Za-z]*)$")


def normalize_local_id(value: str | None) -> str | None:
    """Printed number in a comparable form: "062" / "62" -> "62", "TG05" ->
    "TG5", "SV001" -> "SV1". None for nothing."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    match = _LOCAL_ID.match(text)
    if not match:
        return text.upper()
    prefix, digits, suffix = match.groups()
    return f"{prefix.upper()}{int(digits)}{suffix.upper()}"


def set_id_candidates(language: str, set_code: str) -> list[str]:
    """TCGdex set IDs Dex's set code could correspond to, lowercased, most
    likely first. They're only ever looked up in TCGdex's own set list,
    never fetched blind.

    - Japanese: same code, TCGdex just capitalizes it ("sv2a" -> "SV2a").
    - International (pokemontcg.io's IDs): TCGdex zero-pads the Scarlet &
      Violet / Mega Evolution set numbers and writes half sets with a dot
      ("sv3" -> "sv03", "sv3pt5" -> "sv03.5", "me1" -> "me01",
      "swsh12pt5gg" -> "swsh12.5gg").
    """
    code = set_code.lower()
    candidates = [code]
    if language == "int":
        match = re.match(r"^([a-z]+)(\d+)(?:pt(\d+))?([a-z]*)$", code)
        if match:
            prefix, number, half, suffix = match.groups()
            for n in dict.fromkeys([number, number.zfill(2)]):
                candidates.append(f"{prefix}{n}" + (f".{half}" if half else "") + suffix)
        # Dex writes some half sets without the "pt" ("sv35", "swsh45",
        # "sv105b" for TCGdex's "sv03.5", "swsh4.5", "sv10.5b"). Tried last,
        # so a real set with the literal code always wins.
        match = re.match(r"^([a-z]+)(\d+)5([a-z]*)$", code)
        if match:
            prefix, number, suffix = match.groups()
            for n in dict.fromkeys([number, number.zfill(2)]):
                candidates.append(f"{prefix}{n}.5{suffix}")
    return list(dict.fromkeys(candidates))


def _dex_set_size(number: str | None) -> str | None:
    """The "/165" part of Dex's printed number, when it's plain digits."""
    if not number or "/" not in number:
        return None
    size = number.split("/", 1)[1].strip()
    return str(int(size)) if size.isdigit() else None


def _name_key(name: str | None) -> str:
    text = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "", text)


def names_match(dex_name: str | None, api_name: str | None) -> bool:
    """Dex vs TCGdex English name, ignoring case, accents, spaces and
    punctuation ("Charizard ex" == "Charizard-EX", "Flabébé" == "Flabebe").
    One containing the other also counts ("Dark Celebi" / "Celebi") -- the
    set and number already pin the card; this catches a scheme mismatch."""
    a, b = _name_key(dex_name), _name_key(api_name)
    if not a or not b:
        return False
    return a == b or (min(len(a), len(b)) >= 4 and (a in b or b in a))


@dataclass(frozen=True)
class Verification:
    ok: bool
    matched_by: str | None = None
    reason: str | None = None


def verify_card(payload: dict, *, language: str, set_id: str | None, number: str,
                dex_number: str | None, dex_name: str | None) -> Verification:
    """Does TCGdex's card `payload` really describe the Dex card? Set (when
    known), printed number, printed set size (when both sides have one) and,
    for English cards, name must all agree."""
    if not isinstance(payload, dict) or not payload.get("id"):
        return Verification(False, reason="no card")
    api_set = payload.get("set") or {}
    if set_id is not None and (api_set.get("id") or "").lower() != set_id.lower():
        return Verification(False, reason=f"set {api_set.get('id')} != {set_id}")
    if normalize_local_id(payload.get("localId")) != normalize_local_id(number):
        return Verification(False, reason=f"number {payload.get('localId')} != {number}")
    dex_size = _dex_set_size(dex_number)
    official = (api_set.get("cardCount") or {}).get("official")
    if dex_size is not None and official is not None and str(official) != dex_size:
        return Verification(False, reason=f"set size {official} != {dex_size}")
    if language == "int":
        if not names_match(dex_name, payload.get("name")):
            return Verification(False, reason=f"name {payload.get('name')!r} != {dex_name!r}")
        return Verification(True, masterdata.MATCHED_VERIFIED)
    return Verification(True, masterdata.MATCHED_VERIFIED_NUMBER)


def resolve_tcgdex_id(client: Client, language: str, set_code: str, number: str) -> tuple[str, str] | None:
    """(TCGdex set ID, card ID) for a Dex (language, set, number), found
    through TCGdex's own set list and that set's card list -- exactly one
    card with this printed number, or None. Not yet verified against the
    card itself (see verify_card)."""
    lang = LANGUAGES[language]
    index = client.set_index(lang)
    set_id = next((index[c]["id"] for c in set_id_candidates(language, set_code) if c in index), None)
    if set_id is None:
        return None
    detail = client.set_detail(lang, set_id) or {}
    wanted = normalize_local_id(number)
    hits = [c.get("id") for c in detail.get("cards") or [] if normalize_local_id(c.get("localId")) == wanted]
    return (set_id, hits[0]) if len(hits) == 1 and hits[0] else None


# --------------------------------------------------------------------------
# Choosing the price (pure)
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class SourcePrice:
    price: float  # native currency
    currency: str
    variant_key: str
    updated: dt.date | None
    uncertain: bool
    field: str | None = None  # which Cardmarket field it came from


def _parse_date(value) -> dt.date | None:
    if not value or not isinstance(value, str):
        return None
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def _positive(value) -> float | None:
    """A usable price: a positive number. Cardmarket reports 0 (not null)
    for "no data" on some fields."""
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
        return float(value)
    return None


# Which Cardmarket field is "the price", in order of preference (issue #211).
# `trend` is Cardmarket's own smoothed price and tracked Dex best on the
# 2026-09-30 sample; `avg30` fills in when there's no trend (thin markets
# report trend 0). `avg7`/`avg1` are left out: on thinly traded cards they
# swing with single sales (see README "Pricing").
CARDMARKET_FIELDS = ("trend", "avg30")


def cardmarket_value(block: dict | None, holo: bool) -> tuple[float, str] | None:
    """(price, field) from a Cardmarket block, from the plain fields or the
    `-holo` ones (Cardmarket's foil/reverse-holo copy of the same product)."""
    if not block:
        return None
    for name in CARDMARKET_FIELDS:
        key = f"{name}-holo" if holo else name
        value = _positive(block.get(key))
        if value is not None:
            return value, key
    return None


def _cardmarket_options(payload: dict) -> list[tuple[str, str | None, dict | None]]:
    """(type, foil, cardmarket block) per regular print of the card: TCGdex's
    `variants_detailed`, minus stamped/oversized promos. Cards without that
    list fall back to the `variants` flags against the card-level block."""
    detailed = payload.get("variants_detailed")
    top = (payload.get("pricing") or {}).get("cardmarket")
    if isinstance(detailed, list) and detailed:
        options = []
        for entry in detailed:
            if not isinstance(entry, dict) or entry.get("stamp") or entry.get("size", "standard") != "standard":
                continue
            block = (entry.get("pricing") or {}).get("cardmarket")
            if block is None and not entry.get("foil"):
                # Many (older) cards list their prints without per-print
                # pricing; the card-level block is the base product, whose
                # -holo fields are its reverse-holo copy. A special foil
                # (Poké Ball, Master Ball, ...) is its own product and never
                # borrows the base one.
                block = top
            options.append((entry.get("type") or "", entry.get("foil"), block))
        return options
    flags = payload.get("variants") or {}
    return [(t, None, top) for t in ("normal", "holo", "reverse") if flags.get(t)]


def _option_price(option) -> tuple[float, str] | None:
    type_, _foil, block = option
    # A reverse print (plain or ball/energy foil) is priced from the product's
    # -holo fields; its plain fields are the non-foil copy (or 0).
    if type_ == "reverse":
        return cardmarket_value(block, holo=True)
    return cardmarket_value(block, holo=False)


def _option_key(option) -> str:
    type_, foil, _block = option
    return f"{type_}-{foil}" if foil else type_


_BALL_PAIR = ("pokeball", "masterball")


def _ball_pair_inverted(options) -> bool:
    """True when a card has both a Poké Ball and a Master Ball reverse and the
    Poké Ball one isn't the cheaper of the two. Master Ball reverses are the
    scarce ones; on the 2026-09-30 sample TCGdex had the two Cardmarket
    products swapped on 3 of 9 SV2a cards (a Poké Ball Slowbro at 59 EUR),
    so an inverted pair means "can't tell which product is which"."""
    prices = {}
    for option in options:
        if option[0] == "reverse" and option[1] in _BALL_PAIR:
            value = _option_price(option)
            if value is not None:
                prices[option[1]] = value[0]
    return len(prices) == 2 and prices["pokeball"] >= prices["masterball"]


def choose_cardmarket(payload: dict, dex_variant: str | None) -> SourcePrice | None:
    """The Cardmarket price (EUR) for the Dex card's print, or None.

    Same unambiguous-only policy as card_images._match_variant_key:

    - Dex "Normal" -> the plain `normal` print; "Holo" -> the plain `holo`
      print (a holo rare's own product, not a reverse); "Reverse Holo" ->
      the plain `reverse` print's `-holo` fields (or, when TCGdex lists no
      reverse print at all, the card's own `-holo` fields -- Cardmarket's
      reverse-holo copy of that product); "Poké Ball Holo" / "Master Ball
      Holo" / any "<x> Ball Holo" -> the reverse print with that foil --
      unless the Poké Ball / Master Ball pair looks swapped
      (_ball_pair_inverted).
    - Dex variant blank: fine if the card has exactly one priced print.
    - Anything else (no such print, several candidates, a variant with no
      rule) -> the card's base price (first regular print), flagged
      uncertain. Never a silent guess.
    """
    options = _cardmarket_options(payload)
    top = (payload.get("pricing") or {}).get("cardmarket") or {}
    updated = _parse_date(top.get("updated"))
    code = masterdata.normalize_variant(dex_variant)

    def pick(option, uncertain: bool) -> SourcePrice | None:
        value = _option_price(option)
        if value is None:
            return None
        block_updated = _parse_date((option[2] or {}).get("updated")) or updated
        return SourcePrice(value[0], "EUR", _option_key(option), block_updated, uncertain, value[1])

    wanted: tuple[str, str | None] | None = None
    if code == "normal":
        wanted = ("normal", None)
    elif code == "holo":
        wanted = ("holo", None)
    elif code == "reverse_holo":
        wanted = ("reverse", None)
    elif code.endswith("_ball_holo"):
        wanted = ("reverse", code[: -len("_holo")].replace("_", ""))

    if wanted is not None:
        matches = [o for o in options if (o[0], o[1]) == wanted]
        if wanted[1] in _BALL_PAIR and _ball_pair_inverted(options):
            matches = []  # TCGdex's product mapping looks swapped -- don't trust it
        if len(matches) == 1:
            choice = pick(matches[0], False)
            if choice is not None:
                return choice
        elif not matches and wanted == ("reverse", None) and not any(o[0] == "reverse" for o in options):
            value = cardmarket_value(top, holo=True)
            if value is not None:
                return SourcePrice(value[0], "EUR", "reverse", updated, False, value[1])

    priced = [o for o in options if _option_price(o) is not None]
    if code == "unspecified" and len(priced) == 1:
        return pick(priced[0], False)
    if priced:
        return pick(priced[0], True)
    value = cardmarket_value(top, holo=False)
    if value is not None:
        return SourcePrice(value[0], "EUR", "base", updated, True, value[1])
    return None


def choose_tcgplayer(payload: dict, dex_variant: str | None) -> SourcePrice | None:
    """The TCGplayer market price (USD) for the Dex card's print, or None --
    card_images' pokemontcg rule verbatim (a print TCGplayer has no key for,
    e.g. a Poké Ball / Master Ball pattern -> None, issue #350; single
    print -> it; else the key that is Dex's print via _match_variant_key;
    else the first print, flagged). TCGdex spells the keys
    "reverse-holofoil" where pokemontcg.io has "reverseHolofoil";
    _match_variant_key compares them with the hyphens removed."""
    if not card_images.has_tcgplayer_print(dex_variant):
        return None
    block = (payload.get("pricing") or {}).get("tcgplayer")
    if not isinstance(block, dict):
        return None
    prices: dict[str, float] = {}
    for key, entry in block.items():
        if isinstance(entry, dict):
            value = _positive(entry.get("marketPrice"))
            if value is not None:
                prices[key] = value
    if not prices:
        return None
    updated = _parse_date(block.get("updated"))
    if len(prices) == 1:
        key = next(iter(prices))
        return SourcePrice(prices[key], "USD", key, updated, False)
    matched = card_images._match_variant_key(dex_variant, list(prices))
    if matched:
        return SourcePrice(prices[matched], "USD", matched, updated, False)
    key = next(iter(prices))
    return SourcePrice(prices[key], "USD", key, updated, True)


# --------------------------------------------------------------------------
# Which cards are due
# --------------------------------------------------------------------------
def due_filter(today: dt.date):
    """Cards with a TCGdex-covered language, no TCGdex price fetched in the
    last STALE_AFTER_DAYS, and not backing off after a lookup that found
    nothing usable (stamped on the tcgdex_cardmarket row, and not followed
    by a successful tcgdex_tcgplayer price the same run)."""
    stale_cutoff = today - dt.timedelta(days=STALE_AFTER_DAYS)
    retry_cutoff = today - dt.timedelta(days=RETRY_AFTER_DAYS)
    fresh = exists().where(
        CardPrice.card_id == Card.id,
        CardPrice.source.in_(SOURCES),
        CardPrice.price_nok.isnot(None),
        CardPrice.fetched_at >= stale_cutoff,
    )
    cm = aliased(CardPrice)
    tp = aliased(CardPrice)
    backed_off = exists().where(
        cm.card_id == Card.id,
        cm.source == SOURCE_CM,
        cm.lookup_failed_at >= retry_cutoff,
        ~exists().where(tp.card_id == Card.id, tp.source == SOURCE_TP, tp.fetched_at >= cm.lookup_failed_at),
    )
    covered = exists().where(MasterCard.id == Card.master_card_id, MasterCard.language.in_(list(LANGUAGES)))
    return and_(covered, ~fresh, ~backed_off)


def _priority(card: Card) -> tuple:
    """Budget order: cards with no market price at all first (the Japanese
    gaps this source exists for), then stale TCGdex prices (oldest first),
    then never-tried cards, then retries after a failed lookup."""
    rows = [pricing.get_row(card, s) for s in SOURCES]
    rows = [r for r in rows if r is not None]
    fetched = [r.fetched_at for r in rows if r.price_nok is not None and r.fetched_at is not None]
    failed = [r.lookup_failed_at for r in rows if r.lookup_failed_at is not None]
    if card.market_price is None and not failed:
        return (0, dt.date.min, card.id)
    if fetched:
        return (1, max(fetched), card.id)
    if not failed:
        return (2, dt.date.min, card.id)
    return (3, max(failed), card.id)


# --------------------------------------------------------------------------
# The refresh
# --------------------------------------------------------------------------
@dataclass
class TcgdexRefreshResult:
    cards_checked: int = 0
    cards_priced: int = 0
    ids_matched: int = 0
    cards_unmatched: list[str] = field(default_factory=list)
    cards_variant_uncertain: list[str] = field(default_factory=list)
    transient_errors: int = 0
    http_calls: int = 0
    stopped: str | None = None  # "time" | "rate_limited" | "errors" | "fx_unavailable"
    eur_to_nok: float | None = None
    usd_to_nok: float | None = None
    fx_source: str | None = None
    # "ok", or "degraded" when the only rate available was fx_rates' fallback
    # constant (issue #229): then the run is skipped entirely -- no request,
    # no price, no failure stamp -- and every due card is retried next run.
    status: str = "ok"
    degraded_reason: str | None = None
    # Cards whose stored TCGplayer price was for another print and was
    # dropped this run (pricing.drop_other_print_tcgplayer_rows, issue #350).
    other_print_dropped: int = 0


def _label(card: Card) -> str:
    return f"{card.name} ({card.card_id or '?'} {card.variant or ''})".replace(" )", ")")


def _existing_id(master: MasterCard) -> str | None:
    row = next((r for r in master.external_ids if r.source == SOURCE_ID), None)
    return row.external_id if row else None


def _upstream_too_old(price: SourcePrice, today: dt.date) -> bool:
    return price.updated is not None and (today - price.updated).days > MAX_UPSTREAM_AGE_DAYS


def lookup_card(client: Client, db: Session, card: Card, today: dt.date, rates, result: TcgdexRefreshResult) -> bool:
    """Look one card up and record what came back. Returns True when at
    least one TCGdex price was stored. Raises TransientError (nothing
    recorded) when TCGdex couldn't be asked. `rates` must not be the
    fallback constant (refresh_tcgdex_prices checks, issue #229); a fallback
    rate here raises ValueError rather than storing a wrong price."""
    if not (rates.usable("EUR") and rates.usable("USD")):
        raise ValueError("tcgdex_prices.lookup_card: refusing to price at the FX fallback constant (issue #229)")
    master = card.master_card
    language = master.language
    parsed = masterdata.parse_dex_card_id(card.card_id)
    number = parsed[2] if parsed else master.number
    lang = LANGUAGES[language]

    tcgdex_id = _existing_id(master)
    known = tcgdex_id is not None
    set_id = None
    if not known:
        resolved = resolve_tcgdex_id(client, language, master.set_code, number)
        if resolved:
            set_id, tcgdex_id = resolved
    payload = client.get(f"{lang}/cards/{tcgdex_id}") if tcgdex_id else None

    if payload is None:
        verification = Verification(False, reason="not found on TCGdex")
    elif known:
        # A stored ID (maybe a manual mapping) is only re-checked on number.
        verification = _verify_known(payload, number)
    else:
        verification = verify_card(
            payload, language=language, set_id=set_id, number=number, dex_number=card.number, dex_name=card.name
        )

    if not verification.ok:
        pricing.record_failure(card, SOURCE_CM, today)
        if pricing.get_row(card, SOURCE_TP) is not None:
            pricing.record_failure(card, SOURCE_TP, today)
        result.cards_unmatched.append(f"{_label(card)}: {verification.reason}")
        return False

    if not known:
        masterdata.set_external_id(db, master, SOURCE_ID, payload["id"], verification.matched_by)
        result.ids_matched += 1

    stored = False
    uncertain = False
    for source, choose, currency in (
        (SOURCE_CM, choose_cardmarket, "EUR"),
        (SOURCE_TP, choose_tcgplayer, "USD"),
    ):
        choice = choose(payload, card.variant)
        if choice is not None and _upstream_too_old(choice, today):
            choice = None
        if choice is None:
            # Keep the old price; just note this source had nothing today.
            if pricing.get_row(card, source) is not None:
                pricing.record_failure(card, source, today)
            continue
        rate = rates.to_nok(currency)
        pricing.record_price(
            card,
            source,
            price_nok=round(choice.price * rate, 2),
            price=choice.price,
            currency=currency,
            fx_rate=rate,
            variant_key=choice.variant_key,
            fetched_at=today,
            source_updated_at=choice.updated,
            flags=[pricing.FLAG_VARIANT_UNCERTAIN] if choice.uncertain else [],
        )
        stored = True
        uncertain = uncertain or choice.uncertain
    if not stored:
        # Found the card, but neither source priced it: back off like a miss.
        pricing.record_failure(card, SOURCE_CM, today)
    if uncertain:
        result.cards_variant_uncertain.append(_label(card))
    return stored


def _verify_known(payload: dict, number: str) -> Verification:
    if normalize_local_id(payload.get("localId")) != normalize_local_id(number):
        return Verification(False, reason=f"stored ID now returns number {payload.get('localId')}")
    return Verification(True)


def refresh_tcgdex_prices(
    db: Session,
    today: dt.date | None = None,
    budget: int = MAX_LOOKUPS_PER_RUN,
    time_budget_s: float | None = None,
    client: Client | None = None,
) -> TcgdexRefreshResult:
    """Refresh both TCGdex prices for up to `budget` due cards (due_filter,
    _priority order), stopping early after `time_budget_s`, on a persisting
    429, or after MAX_CONSECUTIVE_ERRORS transient errors in a row. Commits
    (and re-resolves the touched cards) every 25 cards and at the end."""
    today = today or dt.date.today()
    client = client or Client()
    result = TcgdexRefreshResult()
    # Stored TCGplayer prices for a print TCGplayer has no key for (issue
    # #350) go first, whatever the exchange rate.
    result.other_print_dropped = pricing.drop_other_print_tcgplayer_rows(db, today)
    if result.other_print_dropped:
        db.commit()
    rates = fx_rates.get_rates(db.get_bind())
    result.eur_to_nok = rates.to_nok("EUR")
    result.usd_to_nok = rates.to_nok("USD")
    result.fx_source = rates.source
    if not (rates.usable("EUR") and rates.usable("USD")):
        # Both TCGdex prices need a USD or EUR rate; never store one at the
        # fallback constant (issue #229). Skipping the whole run (rather than
        # per card) also skips ID resolution -- harmless, it happens on the
        # next run that has a real rate.
        result.status = "degraded"
        result.degraded_reason = fx_rates.FALLBACK_REASON
        result.stopped = "fx_unavailable"
        return result

    cards = (
        db.query(Card)
        .options(
            selectinload(Card.prices),
            selectinload(Card.master_card).selectinload(MasterCard.external_ids),
        )
        .filter(due_filter(today))
        .all()
    )
    cards.sort(key=_priority)

    started = time.monotonic()
    pending: list[int] = []
    consecutive_errors = 0
    for card in cards[:budget]:
        if time_budget_s is not None and time.monotonic() - started > time_budget_s:
            result.stopped = "time"
            break
        try:
            priced = lookup_card(client, db, card, today, rates, result)
        except RateLimited:
            result.transient_errors += 1
            result.stopped = "rate_limited"
            break
        except TransientError:
            result.transient_errors += 1
            consecutive_errors += 1
            if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                result.stopped = "errors"
                break
            continue
        consecutive_errors = 0
        result.cards_checked += 1
        result.cards_priced += int(priced)
        pending.append(card.id)
        if len(pending) >= 25:
            pricing.resolve_cards(db, pending, today=today)
            pending.clear()
            db.commit()

    pricing.resolve_cards(db, pending, today=today)
    db.commit()
    result.http_calls = client.calls
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="TCGdex price refresh (see module docstring).")
    parser.add_argument("--limit", type=int, default=MAX_LOOKUPS_PER_RUN, help="max cards to look up this run")
    parser.add_argument("--dry-run", action="store_true", help="only print how many cards are due")
    args = parser.parse_args()

    from db import SessionLocal, init_db

    init_db()
    db = SessionLocal()
    try:
        if args.dry_run:
            due = db.query(Card).filter(due_filter(dt.date.today())).count()
            print(f"tcgdex_prices (dry run): {due} card(s) due, would look up {min(due, args.limit)}")
            return
        result = refresh_tcgdex_prices(db, budget=args.limit)
        print(
            f"tcgdex_prices: status={result.status} checked={result.cards_checked} priced={result.cards_priced} "
            f"ids_matched={result.ids_matched} unmatched={len(result.cards_unmatched)} "
            f"variant_uncertain={len(result.cards_variant_uncertain)} "
            f"transient_errors={result.transient_errors} stopped={result.stopped} "
            f"http_calls={result.http_calls} eur_to_nok={result.eur_to_nok} ({result.fx_source})"
        )
        for line in result.cards_unmatched:
            print(f"  unmatched: {line}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
