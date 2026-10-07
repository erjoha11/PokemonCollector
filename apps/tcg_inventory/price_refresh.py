"""The daily pokemontcg.io price pass: TCGplayer prices by stored ID, in
batches (issue #349).

Writes each card's `pokemontcg` row in `card_prices` (models.CardPrice;
`cards.tcgplayer_price` is kept as a mirror) and re-resolves the touched
cards' market price (pricing.resolve_cards). `app.py`'s `/cron/price-refresh`
route runs `refresh_stale_prices` once a day, time-boxed.

How it works:

- **Which cards.** Every international card (masterdata language `int`)
  with a `pokemontcg` ID in `master_card_ids` and a print TCGplayer has a
  key for (card_images.has_tcgplayer_print, issue #350), that has no
  `pokemontcg` price fetched today. Dex's international `card_id` *is* the
  pokemontcg.io ID (masterdata.link_card stores it, `derived`). Japanese and
  zh-hans cards aren't on pokemontcg.io and are never asked for.
- **By ID, in batches.** The due cards' distinct IDs (variants of one
  printed card share one) go to pokemontcg.io CHUNK_SIZE at a time
  (pokemontcg_client.Client.cards_by_ids): prod's 452 IDs are 10 requests a day.
  Each card then picks its own print from the returned `tcgplayer.prices`
  (card_images._choose_tcgplayer_price).
- **Verified.** A returned card must have the requested ID, Dex's printed
  number and a name that overlaps Dex's, as the image lookup checks
  (card_images._pokemontcg_image). A price TCGplayer itself last updated
  more than MAX_UPSTREAM_AGE_DAYS ago isn't used.
- **Fallback search.** An ID missing from a successful response isn't on
  pokemontcg.io under that ID (Dex writes "sv35-27" for pokemontcg's
  "sv3pt5-27"). For that card the old name + set + number search runs once
  (card_images._is_confident_match); a confident hit is priced and its ID
  stored as `heuristic`, so later runs go by ID. masterdata.set_external_id
  keeps the Dex sync from reverting it to the derived ID.
- **Transient vs no-match.** A request that fails after its one retry
  (5xx, 429, timeout) writes nothing for its cards: no price, no failure
  stamp, so they're simply due again. A card is stamped `lookup_failed_at`
  (and listed in the result) only when a successful response says there's
  nothing usable: its ID is missing and the search found nothing confident,
  it failed verification, or it has no usable TCGplayer price. A stamped
  card isn't searched again for PRICE_RETRY_AFTER_DAYS. It is still asked
  for by ID (that costs nothing extra), so a price that appears is picked
  up the next day. (This differs from the per-card search days, when a
  stamped card wasn't asked for at all: with batches the backoff only has
  to save the search.)
- **Budgets.** A time budget (jobs.POKEMONTCG_SECONDS), at most
  MAX_FALLBACK_SEARCHES_PER_RUN searches, and MAX_CONSECUTIVE_ERRORS
  transient failures in a row (or one persisting 429) stop the pass for
  the day. Whatever wasn't reached stays due.

The Dex sync no longer looks up pokemontcg prices at all (only images).

Also a one-off CLI for forcing a re-price of every card that already has a
pokemontcg price (issue #209), through the same batch path:

    python price_refresh.py [--reprice-all] [--limit N] [--dry-run]

Uses the same DATABASE_URL as the app (see db.py), like
seed_set_release_order.py / backfill_images.py.
"""
from __future__ import annotations

import argparse
import datetime as dt
import math
import re
import time
from dataclasses import dataclass, field

from sqlalchemy import and_, exists
from sqlalchemy.orm import Session, selectinload

import card_images
import fx_rates
import masterdata
import pokemontcg_client
import pricing
import tcgdex_prices
from models import Card, CardPrice, MasterCard, MasterCardId

SOURCE = pricing.SOURCE_POKEMONTCG
ID_SOURCE = masterdata.SOURCE_POKEMONTCG  # master_card_ids.source

# Every due card is asked for every day (a price is due again the day after
# it was fetched). Well inside pricing.FRESH_DAYS, so a card's pokemontcg
# price only goes stale when pokemontcg.io stops answering for it.
REFRESH_EVERY_DAYS = 1

# A card whose lookup came back with nothing usable (stamped on its
# pokemontcg card_prices row's lookup_failed_at) isn't searched again for
# this many days (issue #216). With prices fetched by ID it's still asked
# for in the daily batch; only the per-card fallback search backs off.
PRICE_RETRY_AFTER_DAYS = 14

# Fallback searches (one request per printed card) per run. The first runs
# after #349 search every card whose Dex ID isn't pokemontcg.io's; once each
# is either found (and its ID stored) or stamped, this is a handful a day.
MAX_FALLBACK_SEARCHES_PER_RUN = 40

MAX_CONSECUTIVE_ERRORS = 3  # transient request failures in a row -> stop for today

# Same rule as TCGdex's: a price the upstream itself hasn't updated in this
# long must not pass as fresh because we fetched it today.
MAX_UPSTREAM_AGE_DAYS = tcgdex_prices.MAX_UPSTREAM_AGE_DAYS

_SAFE_ID = re.compile(r"^[A-Za-z0-9.\-]+$")


# --------------------------------------------------------------------------
# Which cards are due
# --------------------------------------------------------------------------
def _pokemontcg_row_exists(*conditions):
    return exists().where(CardPrice.card_id == Card.id, CardPrice.source == SOURCE, *conditions)


def _has_pokemontcg_id():
    return exists().where(
        MasterCard.id == Card.master_card_id,
        MasterCard.language == "int",
        MasterCardId.master_card_id == MasterCard.id,
        MasterCardId.source == ID_SOURCE,
    )


def due_filter(today: dt.date):
    """SQL filter: an international card with a pokemontcg ID and no
    pokemontcg price fetched today. (Prints TCGplayer has no key for are
    filtered out in Python, see _due_cards.)"""
    cutoff = today - dt.timedelta(days=REFRESH_EVERY_DAYS - 1)
    priced_today = _pokemontcg_row_exists(CardPrice.price_nok.isnot(None), CardPrice.fetched_at >= cutoff)
    return and_(_has_pokemontcg_id(), ~priced_today)


def _load_cards(db: Session, *criteria) -> list[Card]:
    cards = (
        db.query(Card)
        .options(
            selectinload(Card.prices),
            selectinload(Card.master_card).selectinload(MasterCard.external_ids),
        )
        .filter(*criteria)
        .order_by(Card.id)
        .all()
    )
    # A print TCGplayer has no key for is never looked up (issue #350).
    return [card for card in cards if card_images.has_tcgplayer_print(card.variant)]


def _due_cards(db: Session, today: dt.date) -> list[Card]:
    return _load_cards(db, due_filter(today))


def _id_row(card: Card) -> MasterCardId | None:
    master = card.master_card
    if master is None:
        return None
    return next((row for row in master.external_ids if row.source == ID_SOURCE), None)


def _backed_off(card: Card, today: dt.date) -> bool:
    row = pricing.get_row(card, SOURCE)
    if row is None or row.lookup_failed_at is None:
        return False
    return row.lookup_failed_at >= today - dt.timedelta(days=PRICE_RETRY_AFTER_DAYS)


def _group_priority(cards: list[Card]) -> tuple:
    """Oldest price first (never-priced first of all), so a run that stops
    early has spent its requests where they matter most."""
    dates = []
    for card in cards:
        row = pricing.get_row(card, SOURCE)
        dates.append(row.fetched_at if row is not None and row.price_nok is not None and row.fetched_at else dt.date.min)
    return (min(dates), min(card.id for card in cards))


def _groups(cards: list[Card]) -> dict[str, list[Card]]:
    """Cards by their pokemontcg ID, oldest-priced group first."""
    groups: dict[str, list[Card]] = {}
    for card in cards:
        row = _id_row(card)
        if row is not None and row.external_id:
            groups.setdefault(row.external_id, []).append(card)
    return dict(sorted(groups.items(), key=lambda item: _group_priority(item[1])))


# --------------------------------------------------------------------------
# Verifying a hit and choosing its price (pure)
# --------------------------------------------------------------------------
def _numbers_match(api_number: str | None, dex_number: str | None) -> bool:
    """"4" vs Dex's "4/101", and promo numbers like "SWSH050" too."""
    if card_images._same_number(api_number, dex_number):
        return True
    dex_printed = (dex_number or "").split("/", 1)[0]
    api_key = tcgdex_prices.normalize_local_id(api_number)
    return api_key is not None and api_key == tcgdex_prices.normalize_local_id(dex_printed)


def verify_hit(card: Card, requested_id: str, api_card: dict) -> str | None:
    """None when pokemontcg.io's `api_card` is the Dex card asked for by
    `requested_id`, else the reason it isn't."""
    if api_card.get("id") != requested_id:
        return f"returned {api_card.get('id')!r} for {requested_id!r}"
    if not _numbers_match(api_card.get("number"), card.number):
        return f"number {api_card.get('number')!r} != {card.number!r}"
    api_name = api_card.get("name")
    if not (card_images._names_overlap(card.name, api_name) or tcgdex_prices.names_match(card.name, api_name)):
        return f"name {api_name!r} != {card.name!r}"
    return None


def parse_updated_at(value) -> dt.date | None:
    """pokemontcg.io's `tcgplayer.updatedAt` is "2026/10/07"."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return dt.date.fromisoformat(value.strip()[:10].replace("/", "-"))
    except ValueError:
        return None


# --------------------------------------------------------------------------
# The refresh
# --------------------------------------------------------------------------
@dataclass
class PriceRefreshResult:
    cards_checked: int = 0  # cards a successful response answered for
    cards_updated: int = 0  # ...of which priced
    cards_unmatched: list[str] = field(default_factory=list)  # stamped, with the reason
    cards_low_confidence: list[str] = field(default_factory=list)  # fallback search hit, not confident
    cards_variant_uncertain: list[str] = field(default_factory=list)
    cards_backed_off: int = 0  # nothing usable again, inside the retry window: not re-stamped
    cards_deferred: int = 0  # due, not reached (stopped, or past the search budget): still due
    ids_found: int = 0  # pokemontcg IDs found by the fallback search, stored as heuristic
    requests: int = 0  # HTTP requests, retries included
    batch_requests: int = 0
    fallback_searches: int = 0
    transient_errors: int = 0
    stopped: str | None = None  # "time" | "rate_limited" | "errors"
    # The USD/NOK rate this run converted at, and where it came from
    # ("live" / "last-known" / "stored" / "fallback", see fx_rates).
    usd_to_nok: float | None = None
    fx_source: str | None = None
    fx_as_of: dt.date | None = None
    # "ok", or "degraded" when the only rate available was fx_rates' fallback
    # constant (issue #229): then nothing is requested or written at all --
    # every due card stays due (cards_skipped) and the next run retries.
    status: str = "ok"
    degraded_reason: str | None = None
    cards_skipped: int = 0
    # Cards whose stored TCGplayer price was for another print and was
    # dropped this run (pricing.drop_other_print_tcgplayer_rows, issue #350).
    other_print_dropped: int = 0


def _label(card: Card) -> str:
    variant = f", {card.variant}" if card.variant else ""
    return f"{card.name} ({card.card_id or '?'}{variant})"


class _Pass:
    """One batch pass over `cards`. `record_failures=False` (--reprice-all)
    never stamps anything; `fallback=False` skips the search."""

    def __init__(self, db, today, result, client, *, record_failures, fallback, time_budget_s):
        self.db = db
        self.today = today
        self.result = result
        self.client = client
        self.record_failures = record_failures
        self.fallback = fallback
        self.time_budget_s = time_budget_s
        self.started = time.monotonic()
        self.consecutive_errors = 0
        self.pending: list[int] = []

    # -- bookkeeping --------------------------------------------------------
    def _out_of_time(self) -> bool:
        return self.time_budget_s is not None and time.monotonic() - self.started > self.time_budget_s

    def _flush(self, final: bool = False) -> None:
        """Resolve and commit what's been written so far. Commits between
        batches keep the loaded cards (no expiry): reloading ~500 cards one
        by one would cost more round trips than the whole pass. The final
        commit expires them as usual, since resolve_cards writes
        market_price behind the ORM's back."""
        pricing.resolve_cards(self.db, self.pending, today=self.today)
        self.pending.clear()
        if final:
            self.db.commit()
            return
        previous = self.db.expire_on_commit
        self.db.expire_on_commit = False
        try:
            self.db.commit()
        finally:
            self.db.expire_on_commit = previous

    def _transient(self, exc: pokemontcg_client.TransientError) -> bool:
        """Count a failed request; True when the pass must stop."""
        self.result.transient_errors += 1
        print(f"[price_refresh] transient: {exc}")
        if isinstance(exc, pokemontcg_client.RateLimited):
            self.result.stopped = "rate_limited"
            return True
        self.consecutive_errors += 1
        if self.consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
            self.result.stopped = "errors"
            return True
        return False

    def _no_match(self, card: Card, reason: str) -> None:
        if not self.record_failures:
            return
        if _backed_off(card, self.today):
            self.result.cards_backed_off += 1
            return
        pricing.record_failure(card, SOURCE, self.today)
        self.result.cards_unmatched.append(f"{_label(card)}: {reason}")

    # -- pricing one card from a verified hit ------------------------------
    def _apply(self, card: Card, api_card: dict) -> None:
        self.result.cards_checked += 1
        self.pending.append(card.id)
        tcgplayer = api_card.get("tcgplayer") or {}
        choice = card_images._choose_tcgplayer_price(tcgplayer, card.variant)
        if choice is None:
            self._no_match(card, "no TCGplayer price for this print")
            return
        updated = parse_updated_at(tcgplayer.get("updatedAt"))
        if updated is not None and (self.today - updated).days > MAX_UPSTREAM_AGE_DAYS:
            self._no_match(card, f"TCGplayer price last updated {updated.isoformat()}")
            return
        pricing.record_price(
            card,
            SOURCE,
            price_nok=choice.nok,
            price=choice.usd,
            currency="USD",
            fx_rate=choice.usd_to_nok,
            variant_key=choice.key,
            fetched_at=self.today,
            source_updated_at=updated,
            flags=[pricing.FLAG_VARIANT_UNCERTAIN] if choice.uncertain else [],
        )
        card.tcgplayer_price = choice.nok
        card.tcgplayer_price_updated_at = self.today
        self.result.cards_updated += 1
        if choice.uncertain:
            self.result.cards_variant_uncertain.append(_label(card))

    def _answer(self, requested_id: str, cards: list[Card], api_card: dict) -> None:
        for card in cards:
            reason = verify_hit(card, requested_id, api_card)
            if reason is None:
                self._apply(card, api_card)
            else:
                self.result.cards_checked += 1
                self.pending.append(card.id)
                self._no_match(card, reason)

    # -- the two steps ------------------------------------------------------
    def run(self, groups: dict[str, list[Card]]) -> None:
        missing: dict[str, list[Card]] = {}
        ids = [card_id for card_id in groups if _SAFE_ID.match(card_id)]
        # An ID that can't be put in a query is as good as missing.
        missing.update({card_id: cards for card_id, cards in groups.items() if not _SAFE_ID.match(card_id)})
        chunks = [ids[i : i + pokemontcg_client.CHUNK_SIZE] for i in range(0, len(ids), pokemontcg_client.CHUNK_SIZE)]

        for index, chunk in enumerate(chunks):
            if self._out_of_time():
                self.result.stopped = "time"
            if self.result.stopped:
                self.result.cards_deferred += sum(len(groups[i]) for c in chunks[index:] for i in c)
                break
            try:
                found = self.client.cards_by_ids(chunk)
            except pokemontcg_client.TransientError as exc:
                self.result.cards_deferred += sum(len(groups[i]) for i in chunk)
                self._transient(exc)
                continue
            self.consecutive_errors = 0
            for card_id in chunk:
                if card_id in found:
                    self._answer(card_id, groups[card_id], found[card_id])
                else:
                    missing[card_id] = groups[card_id]
            self._flush()

        if self.result.stopped:
            self.result.cards_deferred += sum(len(cards) for cards in missing.values())
        else:
            self._fallback(missing)
        self._flush(final=True)

    def _fallback(self, missing: dict[str, list[Card]]) -> None:
        searches = 0
        # Cards that already have a pokemontcg price first: the old name
        # search found them before, so their search is the likeliest to
        # find the real ID. A card from a set pokemontcg.io doesn't have yet
        # waits (stable sort, so oldest-priced order otherwise).
        def has_price(cards: list[Card]) -> bool:
            rows = [pricing.get_row(card, SOURCE) for card in cards]
            return any(r is not None and r.price_nok is not None for r in rows)

        ordered = sorted(missing.items(), key=lambda item: not has_price(item[1]))
        for card_id, cards in ordered:
            searchable = [
                card for card in cards
                if self.fallback and not _backed_off(card, self.today)
                and (_id_row(card) is None or _id_row(card).matched_by != masterdata.MATCHED_MANUAL)
            ]
            for card in cards:
                if card not in searchable:
                    self.result.cards_checked += 1
                    self.pending.append(card.id)
                    self._no_match(card, f"{card_id} not on pokemontcg.io")
            if not searchable:
                continue
            if self.result.stopped or searches >= MAX_FALLBACK_SEARCHES_PER_RUN or self._out_of_time():
                if self._out_of_time() and not self.result.stopped:
                    self.result.stopped = "time"
                self.result.cards_deferred += len(searchable)
                continue
            first = searchable[0]
            searches += 1
            try:
                hit = self.client.search(first.name, first.set, first.number)
            except pokemontcg_client.TransientError as exc:
                self.result.cards_deferred += len(searchable)
                self._transient(exc)
                continue
            self.consecutive_errors = 0
            if hit is None:
                for card in searchable:
                    self.result.cards_checked += 1
                    self.pending.append(card.id)
                    self._no_match(card, f"{card_id} not on pokemontcg.io, and the search found nothing")
                continue
            if not card_images._is_confident_match(first.name, first.number, hit) or not hit.get("id"):
                for card in searchable:
                    self.result.cards_checked += 1
                    self.pending.append(card.id)
                    self.result.cards_low_confidence.append(_label(card))
                    self._no_match(
                        card, f"{card_id} not on pokemontcg.io, and the search's {hit.get('id')!r} isn't a confident match"
                    )
                continue
            found_id = hit["id"]
            for card in searchable:
                masterdata.set_external_id(self.db, card.master_card, ID_SOURCE, found_id, masterdata.MATCHED_HEURISTIC)
            self.result.ids_found += 1
            print(f"[price_refresh] {card_id} -> {found_id} (search, stored as heuristic)")
            self._answer(found_id, searchable, hit)


def _start(db: Session, today: dt.date, result: PriceRefreshResult) -> bool:
    """Resolve the exchange rate once for the run. False (degraded) when
    only fx_rates' fallback constant is available (issue #229)."""
    rates = fx_rates.get_rates(db.get_bind())
    result.usd_to_nok = rates.to_nok("USD")
    result.fx_source = rates.source
    result.fx_as_of = rates.as_of
    if rates.usable("USD"):
        return True
    result.status = "degraded"
    result.degraded_reason = fx_rates.FALLBACK_REASON
    return False


def refresh_stale_prices(
    db: Session,
    today: dt.date | None = None,
    time_budget_s: float | None = None,
    client: pokemontcg_client.Client | None = None,
    limit: int | None = None,
) -> PriceRefreshResult:
    """The daily pass (module docstring): every due card, by ID in batches,
    plus fallback searches, within `time_budget_s`. `limit` caps the number
    of distinct IDs asked for (CLI only)."""
    today = today or dt.date.today()
    client = client or pokemontcg_client.Client()
    result = PriceRefreshResult()
    result.other_print_dropped = pricing.drop_other_print_tcgplayer_rows(db, today)
    if result.other_print_dropped:
        db.commit()

    cards = _due_cards(db, today)
    if not _start(db, today, result):
        # Nothing is requested or stamped, so freshness doesn't advance and
        # every card is still due next run.
        result.cards_skipped = len(cards)
        return result

    groups = _groups(cards)
    if limit is not None:
        groups = dict(list(groups.items())[:limit])
    _Pass(db, today, result, client, record_failures=True, fallback=True, time_budget_s=time_budget_s).run(groups)
    _finish(result, client)
    return result


def reprice_all(
    db: Session,
    today: dt.date | None = None,
    limit: int | None = None,
    client: pokemontcg_client.Client | None = None,
) -> PriceRefreshResult:
    """Re-fetch the pokemontcg price for every card that already has one,
    whatever its date -- a forced full re-price (issue #209), through the
    same batch path. A card with nothing usable keeps its old price *and*
    its old fetch date, and isn't stamped; no fallback search. Stored values
    are only ever replaced by a fresh lookup, never rescaled. `limit` caps
    the number of distinct IDs, oldest-priced first."""
    today = today or dt.date.today()
    client = client or pokemontcg_client.Client()
    result = PriceRefreshResult()
    cards = _load_cards(db, _has_pokemontcg_id(), _has_price_filter())
    if not _start(db, today, result):
        result.cards_skipped = len(cards)
        return result
    groups = _groups(cards)
    if limit is not None:
        groups = dict(list(groups.items())[:limit])
    _Pass(db, today, result, client, record_failures=False, fallback=False, time_budget_s=None).run(groups)
    _finish(result, client)
    return result


def _finish(result: PriceRefreshResult, client: pokemontcg_client.Client) -> None:
    result.requests = client.calls
    result.batch_requests = client.batch_requests
    result.fallback_searches = client.searches


def _has_price_filter():
    return _pokemontcg_row_exists(CardPrice.price_nok.isnot(None))


def summary_line(result: PriceRefreshResult) -> str:
    return (
        f"status={result.status} requests={result.requests} (batches={result.batch_requests} "
        f"searches={result.fallback_searches}) checked={result.cards_checked} priced={result.cards_updated} "
        f"unmatched={len(result.cards_unmatched)} ids_found={result.ids_found} "
        f"backed_off={result.cards_backed_off} deferred={result.cards_deferred} "
        f"transient_errors={result.transient_errors} stopped={result.stopped} "
        f"variant_uncertain={len(result.cards_variant_uncertain)} "
        f"other_print_dropped={result.other_print_dropped} "
        f"usd_to_nok={result.usd_to_nok} ({result.fx_source}, as of {result.fx_as_of})"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="pokemontcg.io price refresh (see module docstring).")
    parser.add_argument(
        "--reprice-all",
        action="store_true",
        help="re-price every card that already has a pokemontcg price, whatever its date",
    )
    parser.add_argument("--limit", type=int, default=None, help="max distinct pokemontcg IDs to ask for")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="only print how many cards/IDs/requests it would take and the FX rate it would use",
    )
    args = parser.parse_args()

    from db import SessionLocal, init_db

    init_db()
    db = SessionLocal()
    try:
        if args.dry_run:
            today = dt.date.today()
            if args.reprice_all:
                cards = _load_cards(db, _has_pokemontcg_id(), _has_price_filter())
            else:
                cards = _due_cards(db, today)
            groups = _groups(cards)
            if args.limit is not None:
                groups = dict(list(groups.items())[: args.limit])
            n_cards = sum(len(c) for c in groups.values())
            rates = fx_rates.get_rates(db.get_bind())
            print(
                f"price_refresh (dry run): {n_cards} card(s), {len(groups)} pokemontcg ID(s), "
                f"{math.ceil(len(groups) / pokemontcg_client.CHUNK_SIZE)} batch request(s) plus fallback searches, "
                f"at USD/NOK {rates.to_nok('USD')} ({rates.source}, as of {rates.as_of})"
            )
            return
        if args.reprice_all:
            result = reprice_all(db, limit=args.limit)
        else:
            result = refresh_stale_prices(db, limit=args.limit)
        if result.status != "ok":
            print(f"price_refresh: DEGRADED -- {result.degraded_reason} skipped={result.cards_skipped}")
        print(f"price_refresh: {summary_line(result)}")
        for line in result.cards_unmatched:
            print(f"  unmatched: {line}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
