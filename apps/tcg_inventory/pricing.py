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
the card. Then the cross-checks (`cross_check`, issue #212) add
`sources_disagree` and `suspicious_jump`; see their constants below for the
thresholds and the prod data they were calibrated on. Every flag is
recomputed on every resolve, so one clears as soon as its condition goes
away.

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

from models import Card, CardPrice, CardSnapshot, ImportLog, MasterCard

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
FLAG_SOURCES_DISAGREE = "sources_disagree"
FLAG_SUSPICIOUS_JUMP = "suspicious_jump"
# A source row carrying one of these is never chosen while it's fresh (it
# can still be the stale last resort). Nothing writes one yet: #212's
# cross-checks flag the card but never withhold a price.
DISQUALIFYING_FLAGS = frozenset({FLAG_LOW_CONFIDENCE})

# Most to least urgent (#212 / UX_NOTES 2026-09-30): the "!" markers name the
# flags in this order. Unknown flags sort last.
FLAG_SEVERITY: tuple[str, ...] = (
    FLAG_NO_PRICE,
    FLAG_STALE,
    FLAG_SUSPICIOUS_JUMP,
    FLAG_SOURCES_DISAGREE,
    FLAG_VARIANT_UNCERTAIN,
    FLAG_LOW_CONFIDENCE,
)

# --- Cross-checks (issue #212) ---------------------------------------------
# All thresholds were calibrated on prod data on 2026-09-30 (read-only; the
# numbers are in README "Pricing" -> "Price flags" and HANDOFF.md). Prices
# are compared in NOK.
#
# (a) Same market: the winning TCGplayer-family price against every other
# fresh TCGplayer-family price. Dex vs pokemontcg.io after #209 (n=208):
# median 1.00, p5 0.90, p95 1.19 overall, but for cards >= 5 NOK (n=36)
# p10-p95 0.92-1.03; all of the spread beyond 25% sits below 5 NOK, where
# a one-cent TCGplayer step is ~2-20 %.
TCGPLAYER_FAMILY = frozenset({SOURCE_DEX, SOURCE_TCGDEX_TCGPLAYER, SOURCE_POKEMONTCG})
SAME_MARKET_MAX_RATIO = 1.25  # larger / smaller
SAME_MARKET_FLOOR_NOK = 5.0  # no flag unless the larger price is at least this
# (b) Cross market, Japanese cards only (they have no second TCGplayer
# source): Dex against Cardmarket via TCGdex. The markets differ by design,
# so this is a wide band. Japanese sample (n=113 confident TCGdex matches):
# Dex/Cardmarket median 1.21, p10 0.69, p90 4.80 overall, but for cards
# >= 10 NOK (n=35) median 1.00, p10 0.53, p90 1.86. Cardmarket bottoms out
# at 0.02 EUR, Dex at ~0.5-1 NOK, so cheap cards are left out. PROVISIONAL:
# re-measure once prod has real tcgdex_cardmarket rows.
CROSS_MARKET_LANGUAGES = frozenset({"ja"})
CROSS_MARKET_BAND = (1 / 3, 3.0)  # allowed Dex / Cardmarket ratio
CROSS_MARKET_FLOOR_NOK = 10.0
# suspicious_jump: the price is more than JUMP_FACTOR x (or under 1 /
# JUMP_FACTOR of) the card's resolved price a week earlier, and no other
# fresh source agrees with the new price within JUMP_AGREE_RATIO. The price
# is still displayed, just flagged. The earlier price is the card's latest
# card_snapshots row at least JUMP_LOOKBACK_DAYS old (and no older than
# JUMP_LOOKBACK_DAYS + JUMP_BASELINE_WINDOW_DAYS), so the flag lasts about a
# week after a jump and then clears on its own; comparing with the previous
# resolve instead would clear it at the next cron, hours later. Prod
# snapshots 2026-09-15..29: 14 week-over-week 3x moves on 869 cards, 9 of
# them on cards under 10 NOK; the same floor as (a) keeps the sub-5-NOK ones out.
JUMP_FACTOR = 3.0
JUMP_AGREE_RATIO = 1.25
JUMP_FLOOR_NOK = 5.0
JUMP_LOOKBACK_DAYS = 7
JUMP_BASELINE_WINDOW_DAYS = 14

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
    FLAG_SUSPICIOUS_JUMP: (
        "Price jump",
        f"The price moved more than {JUMP_FACTOR:g}x up or down from a week ago and no other source confirms the new "
        "price. It's still used; check it before relying on it.",
    ),
    FLAG_SOURCES_DISAGREE: (
        "Sources disagree",
        "Another fresh source has a clearly different price for this card (TCGplayer sources more than "
        f"{round((SAME_MARKET_MAX_RATIO - 1) * 100)} % apart; for Japanese cards, Dex and Cardmarket more than "
        f"{CROSS_MARKET_BAND[1]:g}x apart). See Price sources on the card page.",
    ),
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


def sorted_flags(value: str | None) -> list[str]:
    """Stored flags, most urgent first (FLAG_SEVERITY)."""
    order = {f: i for i, f in enumerate(FLAG_SEVERITY)}
    return sorted(_split_flags(value), key=lambda f: (order.get(f, len(order)), f))


def flags_summary(value: str | None) -> str:
    """One line for a "!" marker's title/aria-label ("Price needs a look:
    Price jump, Sources disagree"), most urgent flag first; "" when clean."""
    flags = sorted_flags(value)
    if not flags:
        return ""
    return "Price needs a look: " + ", ".join(flag_label(f) for f in flags)


def flags_detail(value: str | None) -> list[str]:
    """Each flag's tooltip text, most urgent first (the sales prefill note)."""
    return [f"{flag_label(f)}: {flag_title(f)}" if flag_title(f) else flag_label(f) for f in sorted_flags(value)]


def flags_level(value: str | None) -> str | None:
    """"error" (red) for no price, "warn" (amber) for any other flag, None
    when clean -- one amber style for every flag, red only for no price."""
    flags = _split_flags(value)
    if not flags:
        return None
    return "error" if FLAG_NO_PRICE in flags else "warn"


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


def _pick(rows, today: dt.date):
    """(winning row, is_stale) per the module docstring's rule, or (None,
    False) when no row has a price."""
    priced = [r for r in rows if r.price_nok is not None]
    if not priced:
        return None, False
    by_chain = sorted(priced, key=lambda r: _chain_rank(r.source))
    for row in by_chain:
        if is_fresh(row.fetched_at, today) and not DISQUALIFYING_FLAGS.intersection(_split_flags(row.flags)):
            return row, False
    # Nothing fresh: keep the most recently fetched price (chain order breaks
    # ties; an unknown fetch date counts as oldest), flagged stale.
    return max(by_chain, key=lambda r: (r.fetched_at or dt.date.min, -_chain_rank(r.source))), True


def _ratio(a: float, b: float) -> float:
    """Larger over smaller (both > 0)."""
    return max(a, b) / min(a, b)


def cross_check(
    winner, rows: Iterable, today: dt.date, *, language: str | None = None, baseline: float | None = None
) -> list[str]:
    """The #212 flags for a card whose resolved price came from `winner`
    (one of `rows`). `language` is the card's masterdata language ("int",
    "ja", ...); `baseline` its resolved price a week earlier (see
    JUMP_LOOKBACK_DAYS), None when unknown. Pure. See the constants above.

    A source row flagged variant_price_uncertain (possibly another print)
    or disqualified is never used to judge the winner, either way."""
    price = winner.price_nok
    if price is None or price <= 0:
        return []
    ignored = DISQUALIFYING_FLAGS | {FLAG_VARIANT_UNCERTAIN}
    others = [
        r
        for r in rows
        if r.source != winner.source
        and r.price_nok is not None
        and r.price_nok > 0
        and is_fresh(r.fetched_at, today)
        and not ignored.intersection(_split_flags(r.flags))
    ]
    flags: list[str] = []

    disagree = False
    if winner.source in TCGPLAYER_FAMILY:  # (a) same market
        disagree = any(
            r.source in TCGPLAYER_FAMILY
            and max(price, r.price_nok) >= SAME_MARKET_FLOOR_NOK
            and _ratio(price, r.price_nok) > SAME_MARKET_MAX_RATIO
            for r in others
        )
    if not disagree and language in CROSS_MARKET_LANGUAGES and winner.source == SOURCE_DEX:  # (b)
        lo, hi = CROSS_MARKET_BAND
        disagree = any(
            r.source == SOURCE_TCGDEX_CARDMARKET
            and max(price, r.price_nok) >= CROSS_MARKET_FLOOR_NOK
            and not lo <= price / r.price_nok <= hi
            for r in others
        )
    if disagree:
        flags.append(FLAG_SOURCES_DISAGREE)

    if (
        baseline is not None
        and baseline > 0
        and max(price, baseline) >= JUMP_FLOOR_NOK
        and (price > baseline * JUMP_FACTOR or price * JUMP_FACTOR < baseline)
        and not any(_ratio(price, r.price_nok) <= JUMP_AGREE_RATIO for r in others)
    ):
        flags.append(FLAG_SUSPICIOUS_JUMP)
    return flags


def resolve(
    rows: Iterable, today: dt.date, *, language: str | None = None, baseline: float | None = None
) -> Resolution:
    """Pick the displayed price from one card's card_prices rows (ORM
    CardPrice objects or anything with source/price_nok/fetched_at/flags)
    and flag it (`cross_check`; `language`/`baseline` as documented there).
    Pure -- no DB access. See the module docstring for the rule."""
    rows = list(rows)
    row, stale = _pick(rows, today)
    if row is None:
        return Resolution(None, None, None, FLAG_NO_PRICE)
    flags = _split_flags(row.flags)
    if stale:
        flags.append(FLAG_STALE)
    flags += cross_check(row, rows, today, language=language, baseline=baseline)
    return Resolution(row.price_nok, row.source, row.fetched_at, _join_flags(flags))


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
            Card.id,
            Card.market_price,
            Card.market_price_source,
            Card.market_price_as_of,
            Card.price_flags,
            MasterCard.language,
        ).outerjoin(MasterCard, MasterCard.id == Card.master_card_id)
        price_q = select(CardPrice.card_id, CardPrice.source, CardPrice.price_nok, CardPrice.fetched_at, CardPrice.flags)
        if chunk is not None:
            card_q = card_q.where(Card.id.in_(chunk))
            price_q = price_q.where(CardPrice.card_id.in_(chunk))
        rows_by_card: dict[int, list] = {}
        for row in db.execute(price_q):
            rows_by_card.setdefault(row.card_id, []).append(row)
        baselines = _baselines(db, chunk, today)
        for card in db.execute(card_q):
            res = resolve(
                rows_by_card.get(card.id, []), today, language=card.language, baseline=baselines.get(card.id)
            )
            current = Resolution(card.market_price, card.market_price_source, card.market_price_as_of, card.price_flags)
            if res != current:
                changes[card.id] = res

    _write_resolutions(db, changes)
    return len(changes)


# Several snapshots can share a date (cron, price-cron, manual); the later
# one in the day wins. All of them are written right after a full resolve.
_SNAPSHOT_SOURCE_ORDER = {"manual": 0, "cron": 1, "price-cron": 2}


def _baselines(db: Session, card_ids: list[int] | None, today: dt.date) -> dict[int, float]:
    """card id -> resolved price from the card's latest snapshot dated
    between JUMP_LOOKBACK_DAYS and JUMP_LOOKBACK_DAYS +
    JUMP_BASELINE_WINDOW_DAYS days ago (suspicious_jump's "previous
    price"). Two statements per chunk: MAX(date) per card, joined back."""
    newest = today - dt.timedelta(days=JUMP_LOOKBACK_DAYS)
    oldest = newest - dt.timedelta(days=JUMP_BASELINE_WINDOW_DAYS)
    latest = select(CardSnapshot.card_id, func.max(CardSnapshot.date).label("d")).where(
        CardSnapshot.date <= newest, CardSnapshot.date >= oldest, CardSnapshot.reference_price.isnot(None)
    )
    if card_ids is not None:
        latest = latest.where(CardSnapshot.card_id.in_(card_ids))
    latest = latest.group_by(CardSnapshot.card_id).subquery()
    q = select(CardSnapshot.card_id, CardSnapshot.source, CardSnapshot.reference_price).join(
        latest, and_(CardSnapshot.card_id == latest.c.card_id, CardSnapshot.date == latest.c.d)
    ).where(CardSnapshot.reference_price.isnot(None))
    best: dict[int, tuple[int, float]] = {}
    for row in db.execute(q):
        rank = _SNAPSHOT_SOURCE_ORDER.get(row.source, -1)
        if row.card_id not in best or rank > best[row.card_id][0]:
            best[row.card_id] = (rank, row.reference_price)
    return {cid: price for cid, (_, price) in best.items()}


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
