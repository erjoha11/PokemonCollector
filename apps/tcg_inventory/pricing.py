"""Per-source card prices and the resolver that picks the displayed one
(issue #210, pricing Phase 2 of epic #213).

Every price source writes its latest value to `card_prices` (models.CardPrice,
one row per card + source). The resolver then picks one per card and
materializes it onto `cards.market_price` / `market_price_source` /
`market_price_as_of` / `price_flags` -- see Card.display_price for why that's
stored rather than computed. Every consumer reads the resolved value; nothing
reads a source row to decide what to display.

Resolution rule (`resolve`):

1. The first *fresh* and *confident* price in `CHAIN` order wins. Fresh =
   fetched within `FRESH_DAYS` (14). That's deliberately longer than the
   7-day refresh cadence (price_refresh.PRICE_STALE_AFTER_DAYS), or every
   price would expire right before its refresh and the source would flap.
2. If no source is fresh, the most recently fetched price is kept, flagged
   `stale` -- never 0 and never None while any price ever existed.
3. Only a card with no price from any source at all is flagged `no_price`.

The winning row's own flags (e.g. `variant_price_uncertain`) are carried onto
the card.

Resolution runs (a) after every write to card_prices, in the same
transaction (importer.py, price_refresh.py -- batched per run, see
`resolve_cards`), and (b) as a full DB-only pass at the end of each cron
(app.py), which is how freshness expiry gets applied to cards no source
touched that day. Everything here is bulk: a handful of statements for the
whole collection, never one round trip per card (see #193's Vercel timeout).
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Iterable

from sqlalchemy import Date, Float, String, and_, case, exists, func, insert, literal, select, update
from sqlalchemy.orm import Session

from models import Card, CardPrice, ImportLog

# Display priority, TCGplayer-first (owner's choice, epic #213). A module
# constant, not a user setting (yet). The tcgdex_* sources are written by
# tcgdex_prices.py (#211): TCGplayer via TCGdex backs up Dex for
# international cards; Cardmarket via TCGdex comes last, but is the only
# independent source for Japanese cards.
SOURCE_DEX = "dex"
SOURCE_TCGDEX_TCGPLAYER = "tcgdex_tcgplayer"
SOURCE_POKEMONTCG = "pokemontcg"
SOURCE_TCGDEX_CARDMARKET = "tcgdex_cardmarket"
CHAIN: tuple[str, ...] = (SOURCE_DEX, SOURCE_TCGDEX_TCGPLAYER, SOURCE_POKEMONTCG, SOURCE_TCGDEX_CARDMARKET)

# Human labels, for the UI (market: which price it is; via: where we got it).
SOURCE_LABELS: dict[str, str] = {
    SOURCE_DEX: "TCGplayer via Dex",
    SOURCE_TCGDEX_TCGPLAYER: "TCGplayer via TCGdex",
    SOURCE_POKEMONTCG: "TCGplayer via pokemontcg.io",
    SOURCE_TCGDEX_CARDMARKET: "Cardmarket via TCGdex",
}

FRESH_DAYS = 14

FLAG_STALE = "stale"
FLAG_NO_PRICE = "no_price"
FLAG_VARIANT_UNCERTAIN = "variant_price_uncertain"
FLAG_LOW_CONFIDENCE = "low_confidence"
# A source row carrying one of these is never chosen while it's fresh (it
# can still be the stale last resort). Nothing writes one yet; #212's
# cross-checks will.
DISQUALIFYING_FLAGS = frozenset({FLAG_LOW_CONFIDENCE})

# Flag chips on the card page: (label, tooltip). Unknown flags fall back to
# the raw value, so a flag added later still shows up (just unlabelled).
FLAG_LABELS: dict[str, tuple[str, str]] = {
    FLAG_STALE: ("Stale", f"No source has a price fetched in the last {FRESH_DAYS} days -- showing the most recent one."),
    FLAG_NO_PRICE: ("No price", "No source has ever returned a price for this card."),
    FLAG_VARIANT_UNCERTAIN: (
        "Variant uncertain",
        "The source had several prints of this card and the variant couldn't be matched to one of them.",
    ),
    FLAG_LOW_CONFIDENCE: ("Low confidence", "This price failed a cross-check against the other sources."),
}


def source_label(source: str | None) -> str:
    """Human label for a `card_prices.source` / `market_price_source` value
    ("TCGplayer via Dex"). The raw value for anything not in SOURCE_LABELS,
    "–" for none."""
    if not source:
        return "–"
    return SOURCE_LABELS.get(source, source)


def flag_list(value: str | None) -> list[str]:
    """A stored comma-joined flags value as a list (template helper)."""
    return _split_flags(value)


def flag_label(flag: str) -> str:
    return FLAG_LABELS.get(flag, (flag, ""))[0]


def flag_title(flag: str) -> str:
    return FLAG_LABELS.get(flag, (flag, ""))[1]


def source_switch_note(old: str | None, new: str | None, n_cards: int | None = None) -> str | None:
    """Chart tooltip line for a price-source change between two consecutive
    points ("Source: TCGplayer via pokemontcg.io → TCGplayer via Dex"), or
    None when it isn't a switch. Gaining or losing a price altogether (either
    side None) isn't a switch, same rule as queries.price_movers."""
    if not old or not new or old == new:
        return None
    note = f"Source: {source_label(old)} → {source_label(new)}"
    if n_cards is not None:
        note += f" ({n_cards} card{'s' if n_cards != 1 else ''})"
    return note

_CHUNK = 500


def _split_flags(value: str | None) -> list[str]:
    return [f for f in (value or "").split(",") if f]


def _join_flags(flags: Iterable[str]) -> str | None:
    unique = sorted(set(flags))
    return ",".join(unique) if unique else None


def chain_rank(source: str) -> int:
    """Position in CHAIN (display priority); unknown sources sort last."""
    return CHAIN.index(source) if source in CHAIN else len(CHAIN)


_chain_rank = chain_rank


@dataclass(frozen=True)
class Resolution:
    price: float | None
    source: str | None
    as_of: dt.date | None
    flags: str | None


def is_fresh(fetched_at: dt.date | None, today: dt.date) -> bool:
    return fetched_at is not None and (today - fetched_at).days <= FRESH_DAYS


def resolve(rows: Iterable, today: dt.date) -> Resolution:
    """Pick the displayed price from one card's card_prices rows (ORM
    CardPrice objects or anything with source/price_nok/fetched_at/flags).
    Pure -- no DB access. See the module docstring for the rule."""
    priced = [r for r in rows if r.price_nok is not None]
    if not priced:
        return Resolution(None, None, None, FLAG_NO_PRICE)

    by_chain = sorted(priced, key=lambda r: _chain_rank(r.source))
    for row in by_chain:
        row_flags = _split_flags(row.flags)
        if is_fresh(row.fetched_at, today) and not DISQUALIFYING_FLAGS.intersection(row_flags):
            return Resolution(row.price_nok, row.source, row.fetched_at, _join_flags(row_flags))

    # Nothing fresh: keep the most recently fetched price (chain order breaks
    # ties; an unknown fetch date counts as oldest), flagged stale.
    row = max(by_chain, key=lambda r: (r.fetched_at or dt.date.min, -_chain_rank(r.source)))
    return Resolution(row.price_nok, row.source, row.fetched_at, _join_flags([*_split_flags(row.flags), FLAG_STALE]))


# --------------------------------------------------------------------------
# Writing source rows (ORM, one card at a time -- for the few-per-run
# pokemontcg lookups). Callers must call resolve_cards() for the touched
# cards before committing.
# --------------------------------------------------------------------------
def get_row(card: Card, source: str) -> CardPrice | None:
    return next((p for p in card.prices if p.source == source), None)


def _row_for_write(card: Card, source: str) -> CardPrice:
    row = get_row(card, source)
    if row is None:
        row = CardPrice(source=source)
        card.prices.append(row)
    return row


def record_price(
    card: Card,
    source: str,
    *,
    price_nok: float,
    fetched_at: dt.date,
    price: float | None = None,
    currency: str | None = None,
    fx_rate: float | None = None,
    variant_key: str | None = None,
    source_updated_at: dt.date | None = None,
    flags: Iterable[str] = (),
) -> CardPrice:
    """Store a successful lookup as `card`'s latest `source` price (replacing
    whatever was there, flags included) and clear any failure stamp."""
    row = _row_for_write(card, source)
    row.price = price
    row.currency = currency
    row.fx_rate = fx_rate
    row.price_nok = price_nok
    row.variant_key = variant_key
    row.fetched_at = fetched_at
    row.source_updated_at = source_updated_at
    row.flags = _join_flags(flags)
    row.lookup_failed_at = None
    return row


def record_failure(card: Card, source: str, today: dt.date) -> CardPrice:
    """Stamp a lookup that came back with no usable price. The row's last
    price (if any) is left untouched."""
    row = _row_for_write(card, source)
    row.lookup_failed_at = today
    return row


def bulk_record_prices(
    db: Session,
    source: str,
    prices: dict[int, float],
    today: dt.date,
    currency: str = "NOK",
    fx_rate: float = 1.0,
) -> None:
    """Upsert many NOK-denominated prices for one source in a few statements
    (importer.py's Dex Price column, ~every card every sync). Rows that
    already exist are updated with one UPDATE ... CASE per chunk -- the
    ORM would issue one UPDATE per row, which is what timed out on Vercel
    in #193."""
    if not prices:
        return
    db.flush()
    existing: set[int] = set()
    ids = list(prices)
    for start in range(0, len(ids), _CHUNK):
        chunk = ids[start : start + _CHUNK]
        existing.update(
            db.execute(
                select(CardPrice.card_id).where(CardPrice.source == source, CardPrice.card_id.in_(chunk))
            ).scalars()
        )

    to_update = [cid for cid in ids if cid in existing]
    for start in range(0, len(to_update), _CHUNK):
        chunk = {cid: prices[cid] for cid in to_update[start : start + _CHUNK]}
        value = case(chunk, value=CardPrice.card_id)
        db.execute(
            update(CardPrice)
            .where(CardPrice.source == source, CardPrice.card_id.in_(chunk.keys()))
            .values(
                price=value,
                price_nok=value,
                currency=currency,
                fx_rate=fx_rate,
                fetched_at=today,
                lookup_failed_at=None,
            )
            .execution_options(synchronize_session=False)
        )

    new_rows = [
        {
            "card_id": cid,
            "source": source,
            "price": prices[cid],
            "currency": currency,
            "fx_rate": fx_rate,
            "price_nok": prices[cid],
            "fetched_at": today,
        }
        for cid in ids
        if cid not in existing
    ]
    if new_rows:
        db.execute(insert(CardPrice), new_rows)


# --------------------------------------------------------------------------
# Resolving (bulk, Core statements)
# --------------------------------------------------------------------------
def resolve_cards(db: Session, card_ids: Iterable[int] | None = None, today: dt.date | None = None) -> int:
    """Re-resolve `card_ids` (every card when None) from card_prices and write
    the result onto `cards`. Only cards whose resolution changed are
    written, in one UPDATE ... CASE per chunk. Flushes pending ORM changes
    first so rows written through the ORM are seen. Doesn't commit. Returns
    how many cards changed.

    Uses Core statements, so ORM Card objects already loaded in this session
    keep their old market_price until the session expires them (commit).
    """
    today = today or dt.date.today()
    db.flush()

    if card_ids is None:
        id_chunks = [None]
    else:
        ids = list(dict.fromkeys(card_ids))
        if not ids:
            return 0
        id_chunks = [ids[start : start + _CHUNK] for start in range(0, len(ids), _CHUNK)]

    changes: dict[int, Resolution] = {}
    for chunk in id_chunks:
        card_q = select(
            Card.id, Card.market_price, Card.market_price_source, Card.market_price_as_of, Card.price_flags
        )
        price_q = select(CardPrice.card_id, CardPrice.source, CardPrice.price_nok, CardPrice.fetched_at, CardPrice.flags)
        if chunk is not None:
            card_q = card_q.where(Card.id.in_(chunk))
            price_q = price_q.where(CardPrice.card_id.in_(chunk))
        rows_by_card: dict[int, list] = {}
        for row in db.execute(price_q):
            rows_by_card.setdefault(row.card_id, []).append(row)
        for card in db.execute(card_q):
            res = resolve(rows_by_card.get(card.id, []), today)
            current = Resolution(card.market_price, card.market_price_source, card.market_price_as_of, card.price_flags)
            if res != current:
                changes[card.id] = res

    _write_resolutions(db, changes)
    return len(changes)


def _case_or_null(values: dict[int, object]):
    """CASE cards.id WHEN ... for one column. A column whose new values are
    all NULL is set to plain NULL: an all-NULL CASE has no type on Postgres
    and can't be assigned to a float/date column."""
    if all(v is None for v in values.values()):
        return None
    return case(values, value=Card.id)


def _write_resolutions(db: Session, changes: dict[int, Resolution]) -> None:
    items = list(changes.items())
    for start in range(0, len(items), _CHUNK):
        chunk = dict(items[start : start + _CHUNK])
        db.execute(
            update(Card)
            .where(Card.id.in_(chunk.keys()))
            .values(
                market_price=_case_or_null({cid: r.price for cid, r in chunk.items()}),
                market_price_source=_case_or_null({cid: r.source for cid, r in chunk.items()}),
                market_price_as_of=_case_or_null({cid: r.as_of for cid, r in chunk.items()}),
                price_flags=_case_or_null({cid: r.flags for cid, r in chunk.items()}),
            )
            .execution_options(synchronize_session=False)
        )


# --------------------------------------------------------------------------
# One-time backfill from the pre-#210 columns (db.init_db)
# --------------------------------------------------------------------------
def _unresolved_filter():
    # Every resolved card has a winning source or at least the no_price flag.
    return and_(Card.market_price_source.is_(None), Card.price_flags.is_(None))


def backfill_from_legacy(db: Session, today: dt.date | None = None) -> int:
    """Seed card_prices from the legacy columns for every card the resolver
    has never seen, then resolve those cards. Idempotent: once every card
    is resolved it's a single `SELECT ... LIMIT 1` returning nothing.
    Commits. Returns how many cards were resolved.

    - `dex` row from `reference_price` (NOK). Its fetch date isn't recorded
      anywhere, so it's taken as the last Dex sync (latest import_log run),
      or the day the card went missing from Dex (`flagged_missing_since`).
    - `pokemontcg` row from `tcgplayer_price` (already NOK; the native USD
      value and rate weren't stored, so price/fx_rate stay null),
      `tcgplayer_price_updated_at` and the #216 failure stamp
      `price_lookup_failed_at`.

    Set-based INSERT ... SELECT, so it's the same handful of statements for
    870 cards or 87,000 (Vercel cold start, #193).
    """
    today = today or dt.date.today()
    if db.execute(select(Card.id).where(_unresolved_filter()).limit(1)).first() is None:
        return 0

    last_sync = db.execute(select(func.max(ImportLog.ran_at))).scalar()
    if isinstance(last_sync, str):  # SQLite without type processing, just in case
        last_sync = dt.datetime.fromisoformat(last_sync)
    sync_date = last_sync.date() if last_sync is not None else today

    def _no_row(source: str):
        return ~exists().where(CardPrice.card_id == Card.id, CardPrice.source == source)

    db.execute(
        insert(CardPrice).from_select(
            ["card_id", "source", "price", "currency", "fx_rate", "price_nok", "fetched_at"],
            select(
                Card.id,
                literal(SOURCE_DEX, String),
                Card.reference_price,
                literal("NOK", String),
                literal(1.0, Float),
                Card.reference_price,
                func.coalesce(Card.flagged_missing_since, literal(sync_date, Date)),
            ).where(_unresolved_filter(), Card.reference_price.isnot(None), _no_row(SOURCE_DEX)),
        )
    )
    db.execute(
        insert(CardPrice).from_select(
            ["card_id", "source", "currency", "price_nok", "fetched_at", "lookup_failed_at"],
            select(
                Card.id,
                literal(SOURCE_POKEMONTCG, String),
                literal("USD", String),
                Card.tcgplayer_price,
                Card.tcgplayer_price_updated_at,
                Card.price_lookup_failed_at,
            ).where(
                _unresolved_filter(),
                (Card.tcgplayer_price.isnot(None)) | (Card.price_lookup_failed_at.isnot(None)),
                _no_row(SOURCE_POKEMONTCG),
            ),
        )
    )

    ids = list(db.execute(select(Card.id).where(_unresolved_filter())).scalars())
    resolve_cards(db, ids, today=today)
    db.commit()
    return len(ids)
