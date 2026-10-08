"""Seed a set checklist from TCGdex (issue #368, epic #366).

    python set_checklist_seed.py --set ja:sv2a [--dry-run] [--display-name "..."]

Same shape as set_sync.py: uses the app's DATABASE_URL (local SQLite by
default, Supabase when set), runs init_db() first, prints a report. Safe to
re-run: a second run against unchanged TCGdex data changes nothing.

What it does, for one masterdata (language, set_code):

1. Fetch the set and every card in it from TCGdex (`/v2/{lang}/sets/{id}`
   plus one `/v2/{lang}/cards/{id}` per card; ~212 requests for sv2a),
   through tcgdex_prices.Client (sequential, paced, one retry on 429/5xx).
   Everything is fetched before anything is written; any failed request
   aborts the run with nothing changed.
2. Work out the prints (`plan_prints`). Per card: its one base print
   (TCGdex `normal` or `holo`, track `main` up to the set's official count,
   else `secret`), plus each standard-size, unstamped `reverse` print with
   a `pokeball` / `masterball` foil (tracks `poke_ball` / `master_ball`).
   Plain reverse prints and other foils aren't part of a master set by the
   user's definition; they're reported as skipped, not added.
3. Find or create a `master_cards` row for every print (no `Card` -- this
   is identity only, see masterdata.py), store its TCGdex ID in
   `master_card_ids` (`verified_number`; a `manual` mapping is never
   overwritten), and fill in name/rarity/image only where still empty.
4. Make the checklist's membership exactly those prints.
5. Set `sets.total_cards` (base prints, e.g. 210) on every `Set` row the
   set's owned cards link to.
6. Report slot counts per track, how many owned cards land on a slot, and
   every owned card that doesn't (card_id + Dex variant).

The base-slot rule (the part that matters). TCGdex lists exactly one base
print per number: `normal` for commons/uncommons, `holo` for rares, ex and
secret rares. Dex's Variant doesn't always say the same: the user's main-set
rares and ex are logged as "Holo", and there is no separate non-holo print
of those. So the base slot is *whatever master card already holds that
number's Normal or Holo print*, not a new row keyed on TCGdex's word for it.
Only when neither exists is a new row created, with TCGdex's variant. If
both a Normal and a Holo master exist for one number (should not happen),
the one with owned cards wins, then the one matching TCGdex; the other is
reported as a base conflict and its cards show up as unmatched. The seed
never re-links a `Card`. Ball prints match by their own variant code
(`poke_ball_holo`, `master_ball_holo`, as Dex's "Poké Ball Holo" /
"Master Ball Holo" normalize).

Never touches `cards`, qty, collections or binders.
"""
from __future__ import annotations

import argparse
import datetime as dt
from collections import Counter
from dataclasses import dataclass, field

from sqlalchemy import func
from sqlalchemy.orm import Session, selectinload

import masterdata
import set_checklists
import tcgdex_prices
from models import Card, MasterCard, Set, SetChecklist, SetChecklistCard

SOURCE = "tcgdex"


@dataclass(frozen=True)
class SetSpec:
    language: str  # masterdata language, e.g. "ja"
    set_code: str  # masterdata set code, e.g. "sv2a"
    display_name: str | None = None


# Sets with a known display name. Any other "<language>:<set_code>" works
# too (the display name then defaults to TCGdex's set name).
KNOWN_SETS = {
    # Dex has no Korean 151, so the user's Korean cards are logged as
    # Japanese sv2a; TCGdex's Korean SV2a is an empty stub, so the Japanese
    # list stands in (epic #366).
    "ja:sv2a": SetSpec("ja", "sv2a", "Pokémon Card 151 (Korean)"),
}

BASE_VARIANTS = ("normal", "holo")
# TCGdex reverse foil -> (track, masterdata variant code).
BALL_FOILS = {
    "pokeball": (set_checklists.TRACK_POKE_BALL, "poke_ball_holo"),
    "masterball": (set_checklists.TRACK_MASTER_BALL, "master_ball_holo"),
}


class SeedError(Exception):
    """The seed can't run safely (TCGdex unreachable, an unexpected payload).
    Raised before anything is written."""


@dataclass(frozen=True)
class Print:
    number: str  # masterdata form, e.g. "1" (Dex's card_ids aren't zero-padded)
    local_id: str  # TCGdex's, e.g. "001"
    tcgdex_id: str
    track: str
    variant: str  # masterdata variant code; for a base print, TCGdex's word for it
    name: str | None
    rarity: str | None
    image_url: str | None

    @property
    def is_base(self) -> bool:
        return self.track in (set_checklists.TRACK_MAIN, set_checklists.TRACK_SECRET)


@dataclass
class SeedResult:
    language: str
    set_code: str
    display_name: str = ""
    source_set_id: str | None = None
    dry_run: bool = False
    checklist_created: bool = False
    checklist_updated: bool = False  # display name / source changed
    slots: Counter = field(default_factory=Counter)  # track -> member count
    masters_created: int = 0
    masters_reused: int = 0
    masters_filled: int = 0  # existing masters given a missing name/rarity/image
    ids_written: int = 0  # tcgdex master_card_ids added or changed
    ids_manual_kept: int = 0
    members_added: int = 0
    members_updated: int = 0
    members_removed: int = 0
    sets_total_updated: list[str] = field(default_factory=list)
    base_total: int = 0
    skipped_prints: list[str] = field(default_factory=list)
    base_conflicts: list[str] = field(default_factory=list)
    owned_rows: int = 0
    owned_copies: int = 0
    owned_on_slot: Counter = field(default_factory=Counter)  # track -> card rows
    unmatched: list[str] = field(default_factory=list)  # "card_id (Dex variant)"
    http_calls: int = 0

    @property
    def changed(self) -> bool:
        return bool(
            self.checklist_created
            or self.checklist_updated
            or self.masters_created
            or self.masters_filled
            or self.ids_written
            or self.members_added
            or self.members_updated
            or self.members_removed
            or self.sets_total_updated
        )


# --------------------------------------------------------------------------
# Fetching (network)
# --------------------------------------------------------------------------
def fetch_set(client: tcgdex_prices.Client, spec: SetSpec) -> tuple[dict, list[dict]]:
    """(set payload, card payloads) from TCGdex, or SeedError. The set is
    found in TCGdex's own set list (same rule as the price refresh); every
    card the set lists must come back."""
    lang = tcgdex_prices.LANGUAGES.get(spec.language)
    if lang is None:
        raise SeedError(f"TCGdex has no catalog for language {spec.language!r}")
    try:
        index = client.set_index(lang)
        set_id = next(
            (index[c]["id"] for c in tcgdex_prices.set_id_candidates(spec.language, spec.set_code) if c in index),
            None,
        )
        if set_id is None:
            raise SeedError(f"set {spec.set_code!r} not found in TCGdex's {lang} set list")
        detail = client.set_detail(lang, set_id)
        if not detail or not detail.get("cards"):
            raise SeedError(f"TCGdex returned no cards for {lang}/{set_id}")
        cards = []
        for entry in detail["cards"]:
            payload = client.get(f"{lang}/cards/{entry['id']}")
            if payload is None:
                raise SeedError(f"TCGdex card {entry['id']} listed in {set_id} but not found")
            cards.append(payload)
    except tcgdex_prices.TransientError as exc:
        raise SeedError(f"TCGdex request failed, nothing written: {exc}") from exc
    return detail, cards


# --------------------------------------------------------------------------
# Planning (pure)
# --------------------------------------------------------------------------
def _image(payload: dict) -> str | None:
    # Same form card_images uses for TCGdex's Japanese images.
    image = payload.get("image")
    return f"{image}/low.webp" if image else None


def plan_prints(set_payload: dict, card_payloads: list[dict]) -> tuple[list[Print], list[str]]:
    """Every print that belongs on the checklist, and a description of every
    print TCGdex lists that doesn't (plain reverse, other foils, stamped or
    oversized). SeedError when a card's payload can't be read safely."""
    set_id = set_payload.get("id")
    official = (set_payload.get("cardCount") or {}).get("official")
    listed = {c.get("id"): c.get("localId") for c in set_payload.get("cards") or []}
    prints: list[Print] = []
    skipped: list[str] = []
    seen_numbers: set[str] = set()
    for payload in card_payloads:
        tcgdex_id = payload.get("id")
        local_id = payload.get("localId")
        if tcgdex_id not in listed or (payload.get("set") or {}).get("id") != set_id:
            raise SeedError(f"card {tcgdex_id} isn't in set {set_id}")
        if tcgdex_prices.normalize_local_id(local_id) != tcgdex_prices.normalize_local_id(listed[tcgdex_id]):
            raise SeedError(f"card {tcgdex_id}: number {local_id} != listed {listed[tcgdex_id]}")
        number = tcgdex_prices.normalize_local_id(local_id)
        if number in seen_numbers:
            raise SeedError(f"number {local_id} appears twice in {set_id}")
        seen_numbers.add(number)

        variants = payload.get("variants_detailed")
        if not isinstance(variants, list) or not variants:
            raise SeedError(f"card {tcgdex_id} has no variants_detailed")
        base = []
        for entry in variants:
            type_, foil = entry.get("type"), entry.get("foil")
            regular = not entry.get("stamp") and entry.get("size", "standard") == "standard"
            if regular and type_ in BASE_VARIANTS and not foil:
                base.append(type_)
            elif regular and type_ == "reverse" and foil in BALL_FOILS:
                track, code = BALL_FOILS[foil]
                prints.append(
                    Print(number, local_id, tcgdex_id, track, code, payload.get("name"),
                          payload.get("rarity"), _image(payload))
                )
            else:
                skipped.append(f"{tcgdex_id} {type_}" + (f"/{foil}" if foil else "")
                               + (" (stamped)" if entry.get("stamp") else "")
                               + ("" if entry.get("size", "standard") == "standard" else f" ({entry.get('size')})"))
        if len(base) != 1:
            raise SeedError(f"card {tcgdex_id}: expected exactly one normal/holo print, got {base or 'none'}")
        main = number.isdigit() and official is not None and int(number) <= int(official)
        track = set_checklists.TRACK_MAIN if main else set_checklists.TRACK_SECRET
        prints.append(
            Print(number, local_id, tcgdex_id, track, base[0], payload.get("name"),
                  payload.get("rarity"), _image(payload))
        )
    return prints, skipped


# --------------------------------------------------------------------------
# Writing
# --------------------------------------------------------------------------
def _choose_base(candidates: list[MasterCard], tcgdex_variant: str, owned_ids: set[int]) -> MasterCard:
    """Of a number's existing Normal/Holo masters, the base slot: the one
    with owned cards, then the one matching TCGdex, then the oldest."""
    return sorted(
        candidates,
        key=lambda m: (m.id not in owned_ids, m.variant != tcgdex_variant, m.id),
    )[0]


def _fill(master: MasterCard, p: Print) -> bool:
    """Name/rarity/image from TCGdex where the master has none yet."""
    changed = False
    for attr, value in (("name", p.name), ("rarity", p.rarity), ("image_url", p.image_url)):
        if value and getattr(master, attr) is None:
            setattr(master, attr, value)
            changed = True
    return changed


def seed_checklist(
    db: Session,
    spec: SetSpec,
    set_payload: dict,
    card_payloads: list[dict],
    *,
    dry_run: bool = False,
    now: dt.datetime | None = None,
) -> SeedResult:
    """Apply a fetched TCGdex set to the database (see module docstring).
    Commits, or rolls everything back when `dry_run`."""
    now = now or dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)  # naive UTC, like the rest
    prints, skipped = plan_prints(set_payload, card_payloads)
    result = SeedResult(spec.language, spec.set_code, dry_run=dry_run, skipped_prints=skipped)
    result.display_name = spec.display_name or set_payload.get("name") or spec.set_code
    result.source_set_id = set_payload.get("id")
    official = (set_payload.get("cardCount") or {}).get("official")

    masters = (
        db.query(MasterCard)
        .options(selectinload(MasterCard.external_ids))
        .filter(MasterCard.language == spec.language, MasterCard.set_code == spec.set_code)
        .all()
    )
    by_number: dict[str, list[MasterCard]] = {}
    for m in masters:
        by_number.setdefault(tcgdex_prices.normalize_local_id(m.number), []).append(m)
    owned_ids = {
        row[0]
        for row in db.query(Card.master_card_id)
        .join(MasterCard, Card.master_card_id == MasterCard.id)
        .filter(MasterCard.language == spec.language, MasterCard.set_code == spec.set_code)
        .distinct()
    }
    # New masters copy series/set_name from the set's existing ones (Dex's
    # names), so they group with the owned cards.
    names = Counter((m.series, m.set_name) for m in masters if m.set_name)
    series, set_name = names.most_common(1)[0][0] if names else (None, set_payload.get("name"))
    # A new master's number is written the way Dex writes this set's IDs
    # (unpadded, "jpn_sv2a-1"), so a card bought later links to it instead of
    # creating a parallel row. Zero-padded only if the set's Dex IDs are.
    padded = any(len(m.number) > 1 and m.number.startswith("0") for m in masters)

    placed: list[tuple[MasterCard, str]] = []  # (master, track), one per print
    for p in prints:
        candidates = by_number.get(p.number, [])
        if p.is_base:
            matches = [m for m in candidates if m.variant in BASE_VARIANTS]
        else:
            matches = [m for m in candidates if m.variant == p.variant]
        if matches:
            master = _choose_base(matches, p.variant, owned_ids) if p.is_base else matches[0]
            for other in matches:
                if other is not master:
                    result.base_conflicts.append(
                        f"#{p.number}: base slot is {master.variant} (master {master.id}), "
                        f"{other.variant} (master {other.id}) left off the checklist"
                    )
            result.masters_reused += 1
            if _fill(master, p):
                result.masters_filled += 1
        else:
            master = MasterCard(
                language=spec.language,
                set_code=spec.set_code,
                number=p.local_id if padded else p.number,
                variant=p.variant,
                variant_label=masterdata.VARIANT_LABELS.get(p.variant, p.variant),
                name=p.name,
                series=series,
                set_name=set_name,
                printed_number=f"{p.local_id}/{official}" if official else p.local_id,
                rarity=p.rarity,
                image_url=p.image_url,
                created_at=now,
            )
            db.add(master)
            by_number.setdefault(p.number, []).append(master)
            result.masters_created += 1

        before = next(((r.external_id, r.matched_by) for r in master.external_ids if r.source == SOURCE), None)
        row = masterdata.set_external_id(db, master, SOURCE, p.tcgdex_id, masterdata.MATCHED_VERIFIED_NUMBER)
        if row.matched_by == masterdata.MATCHED_MANUAL and row.external_id != p.tcgdex_id:
            result.ids_manual_kept += 1
        elif before != (row.external_id, row.matched_by):
            result.ids_written += 1
        if any(m is master for m, _track in placed):
            raise SeedError(f"two prints map to one master card ({master.variant} #{p.number})")
        placed.append((master, p.track))
    db.flush()  # one batch of INSERTs for the new masters and IDs
    desired = {master.id: track for master, track in placed}  # master id -> track

    checklist = (
        db.query(SetChecklist)
        .options(selectinload(SetChecklist.cards))
        .filter_by(language=spec.language, set_code=spec.set_code)
        .one_or_none()
    )
    if checklist is None:
        checklist = SetChecklist(language=spec.language, set_code=spec.set_code,
                                 display_name=result.display_name, source=SOURCE,
                                 source_set_id=result.source_set_id)
        db.add(checklist)
        result.checklist_created = True
    else:
        for attr, value in (("display_name", result.display_name), ("source", SOURCE),
                            ("source_set_id", result.source_set_id)):
            if getattr(checklist, attr) != value:
                setattr(checklist, attr, value)
                result.checklist_updated = True

    current = {member.master_card_id: member for member in checklist.cards}
    for master_id, track in desired.items():
        counts = set_checklists.TRACKS[track]
        member = current.get(master_id)
        if member is None:
            checklist.cards.append(SetChecklistCard(master_card_id=master_id, track=track,
                                                    counts_toward_completion=counts))
            result.members_added += 1
        elif member.track != track or member.counts_toward_completion != counts:
            member.track = track
            member.counts_toward_completion = counts
            result.members_updated += 1
    for master_id, member in current.items():
        if master_id not in desired:
            checklist.cards.remove(member)
            result.members_removed += 1
    result.slots = Counter(desired.values())

    # Set.total_cards: the set's base prints, on every Set row its cards link to.
    result.base_total = sum(1 for p in prints if p.is_base)
    set_ids = {
        row[0]
        for row in db.query(Card.set_id)
        .join(MasterCard, Card.master_card_id == MasterCard.id)
        .filter(MasterCard.language == spec.language, MasterCard.set_code == spec.set_code,
                Card.set_id.isnot(None))
        .distinct()
    }
    for set_row in db.query(Set).filter(Set.id.in_(set_ids)).order_by(Set.id) if set_ids else []:
        if set_row.total_cards != result.base_total:
            result.sets_total_updated.append(f"{set_row.series} / {set_row.name}: "
                                             f"{set_row.total_cards} -> {result.base_total}")
            set_row.total_cards = result.base_total

    if result.changed:
        checklist.fetched_at = now
    db.flush()

    # Owned cards vs the checklist.
    owned = (
        db.query(Card.card_id, Card.variant, Card.qty, Card.master_card_id)
        .join(MasterCard, Card.master_card_id == MasterCard.id)
        .filter(MasterCard.language == spec.language, MasterCard.set_code == spec.set_code)
        .order_by(func.coalesce(Card.number_int, 0), Card.card_id, Card.variant)
        .all()
    )
    for card_id, variant, qty, master_id in owned:
        result.owned_rows += 1
        result.owned_copies += qty or 0
        track = desired.get(master_id)
        if track is None:
            result.unmatched.append(f"{card_id} ({variant or 'no variant'})")
        else:
            result.owned_on_slot[track] += 1

    if dry_run:
        db.rollback()
    else:
        db.commit()
    return result


def parse_set_arg(value: str, display_name: str | None = None) -> SetSpec:
    """"ja:sv2a" -> SetSpec (with its known display name, unless overridden)."""
    key = value.strip().lower()
    if ":" not in key:
        raise argparse.ArgumentTypeError("--set must look like <language>:<set_code>, e.g. ja:sv2a")
    language, set_code = key.split(":", 1)
    known = KNOWN_SETS.get(key)
    return SetSpec(language, set_code, display_name or (known.display_name if known else None))


def format_report(result: SeedResult) -> list[str]:
    tracks = [set_checklists.TRACK_MAIN, set_checklists.TRACK_SECRET,
              set_checklists.TRACK_POKE_BALL, set_checklists.TRACK_MASTER_BALL]
    prefix = "set_checklist_seed (dry run, nothing written)" if result.dry_run else "set_checklist_seed"
    lines = [
        f"{prefix}: {result.language}/{result.set_code} \"{result.display_name}\" "
        f"from TCGdex {result.source_set_id} ({result.http_calls} requests)",
        "  slots: " + ", ".join(f"{t} {result.slots.get(t, 0)}" for t in tracks)
        + f" (base prints {result.base_total})",
        f"  checklist {'created' if result.checklist_created else 'updated' if result.checklist_updated else 'existing'}; members "
        f"+{result.members_added} ~{result.members_updated} -{result.members_removed}",
        f"  master cards: {result.masters_created} created, {result.masters_reused} reused "
        f"({result.masters_filled} given missing name/rarity/image); tcgdex IDs written "
        f"{result.ids_written}, manual kept {result.ids_manual_kept}",
        "  sets.total_cards: " + ("; ".join(result.sets_total_updated) or "unchanged"),
        f"  owned: {result.owned_rows} card rows ({result.owned_copies} copies), "
        f"{sum(result.owned_on_slot.values())} on a slot ("
        + ", ".join(f"{t} {result.owned_on_slot.get(t, 0)}" for t in tracks)
        + f"), {len(result.unmatched)} unmatched",
    ]
    lines += [f"  unmatched: {line}" for line in result.unmatched]
    lines += [f"  base conflict: {line}" for line in result.base_conflicts]
    if result.skipped_prints:
        lines.append(f"  {len(result.skipped_prints)} TCGdex print(s) not part of a master set (skipped):")
        lines += [f"    {line}" for line in result.skipped_prints]
    lines.append("  changed: " + ("yes" if result.changed else "nothing"))
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Seed a set checklist from TCGdex (see module docstring).")
    parser.add_argument("--set", required=True, help="<language>:<set_code>, e.g. ja:sv2a")
    parser.add_argument("--display-name", help="override the set's display name")
    parser.add_argument("--dry-run", action="store_true", help="report what would change, write nothing")
    args = parser.parse_args(argv)
    spec = parse_set_arg(args.set, args.display_name)

    from db import SessionLocal, init_db

    client = tcgdex_prices.Client()
    try:
        set_payload, cards = fetch_set(client, spec)
    except SeedError as exc:
        print(f"set_checklist_seed: {exc}")
        return 1

    init_db()
    db = SessionLocal()
    try:
        result = seed_checklist(db, spec, set_payload, cards, dry_run=args.dry_run)
    except SeedError as exc:
        db.rollback()
        print(f"set_checklist_seed: {exc} -- nothing written")
        return 1
    finally:
        db.close()
    result.http_calls = client.calls
    for line in format_report(result):
        print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
