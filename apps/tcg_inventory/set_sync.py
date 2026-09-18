"""One-off/occasional sync: populate `Set.release_rank` and `Set.total_cards`
from api.pokemontcg.io's `/v2/sets` (~166 sets, one call, no pagination
needed -- see `_PAGE_SIZE`). Fast-follow to issue #133 (`models.Set`), per
issue #136.

`release_rank` was previously "hand-entered once researched, never
guessed" (see `models.Set`'s docstring and README's "Chronological
sorting"). This is a deliberate, explicitly-approved change to that rule:
the API's own published `releaseDate` is real data, not a guess, so it's
now the primary source -- see `_release_ranks` below for how a date
becomes a rank. `total_cards` was nullable and unpopulated before this;
same API call fills it in for every matched set.

Matching is by (series, name) with judgement, not a blind string-equality
join -- api.pokemontcg.io's series/name strings don't always exactly match
Dex's own (e.g. Dex's "Original" vs the API's "Base"), so series alone is
too strict to be useful and name alone can occasionally collide. See
`_match`: match on name first (case-insensitive, unique match wins
outright), only falling back to series as a tie-break when a name is
ambiguous across more than one API set, and leaving a `Set` row entirely
untouched (never a wrong guess) if neither resolves it uniquely.

Known accepted gap: api.pokemontcg.io lags on Japanese/Korean sets (e.g.
this collection's own "Scarlet & Violet: 151 JP/KR" collection) -- those
simply never appear in the API's set list, so they fall out as unmatched
here with no special-casing needed, same as any other set that doesn't
clear the confidence bar. Unmatched sets keep whatever `release_rank`/
`total_cards` they already had (typically null) -- see
`queries.sets_missing_release_rank` for a durable way to see which sets
that is, beyond this script's own printed summary.

Usage:
    python set_sync.py

Uses the same DATABASE_URL as the app (see db.py) -- run it locally against
SQLite, or with DATABASE_URL set to the Supabase connection string to
update the live database. Safe to re-run any time -- idempotent, and only
ever touches `release_rank`/`total_cards` on rows it actually matches.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

import httpx

from db import SessionLocal, init_db
from models import Set

_API_URL = "https://api.pokemontcg.io/v2/sets"
_TIMEOUT = 10.0
# Comfortably above the ~166 sets that exist today (per issue #136), so one
# page covers all of them -- avoids pagination complexity for a number this
# small, and would just come back with everything again if the real total
# ever creeps past this (still cheaper than adding pagination pre-emptively
# for a one-off/occasional script).
_PAGE_SIZE = 500


@dataclass
class SetSyncResult:
    matched: list[str] = field(default_factory=list)
    unmatched: list[str] = field(default_factory=list)
    # True unless the whole API call failed (network error, non-2xx after
    # retry, bad JSON) -- distinguishes "ran, found nothing to match" from
    # "didn't actually run", since both would otherwise look like an empty
    # result to a caller only checking matched/unmatched.
    api_call_succeeded: bool = True


def _normalize(value: str | None) -> str:
    return (value or "").strip().lower()


def fetch_api_sets() -> list[dict]:
    """All sets from api.pokemontcg.io in one call. Never raises -- a failed
    fetch just returns an empty list, same "best-effort, no crash" spirit as
    card_images.py's fetch_card_data. One retry for the same reason
    card_images.py retries once: this API's free tier is flaky, and a
    one-off sync script re-run is cheap compared to a spuriously empty
    result.
    """
    for _attempt in range(2):
        try:
            response = httpx.get(_API_URL, params={"pageSize": _PAGE_SIZE}, timeout=_TIMEOUT)
            response.raise_for_status()
            return response.json().get("data") or []
        except (httpx.HTTPError, ValueError):
            continue
    return []


def _parse_release_date(value: str | None) -> dt.date:
    """API dates are "YYYY/MM/DD". A set with a missing/unparsable date
    sorts last (dt.date.max) rather than being dropped from ranking
    entirely -- still gets *some* rank, just never ahead of a set with a
    real known date.
    """
    if value:
        try:
            return dt.datetime.strptime(value, "%Y/%m/%d").date()
        except ValueError:
            pass
    return dt.date.max


def _release_ranks(api_sets: list[dict]) -> dict[str, int]:
    """Ordinal rank (1..N), earliest release first, over *every* API set --
    not just the ones that end up matched -- so the resulting scale is
    dense and consistent across the whole matched population instead of
    leaving gaps. Same comparison semantics `UNKNOWN_RELEASE_RANK` already
    relies on (a plain integer, smaller = earlier) -- see app.py/queries.py.
    Ties (identical/missing release date) break on the API's own set `id`
    for a deterministic order across runs.
    """
    ordered = sorted(api_sets, key=lambda s: (_parse_release_date(s.get("releaseDate")), s.get("id") or ""))
    return {api_set["id"]: rank for rank, api_set in enumerate(ordered, start=1)}


def _match(set_rows: list[Set], api_sets: list[dict]) -> dict[int, dict]:
    """See module docstring for the strategy. Returns {Set.id: api_set}."""
    by_name: dict[str, list[dict]] = {}
    for api_set in api_sets:
        by_name.setdefault(_normalize(api_set.get("name")), []).append(api_set)

    matches: dict[int, dict] = {}
    for row in set_rows:
        candidates = by_name.get(_normalize(row.name)) or []
        if len(candidates) == 1:
            matches[row.id] = candidates[0]
        elif len(candidates) > 1:
            series_filtered = [c for c in candidates if _normalize(c.get("series")) == _normalize(row.series)]
            if len(series_filtered) == 1:
                matches[row.id] = series_filtered[0]
            # else: still ambiguous even after the series tie-break --
            # leave unmatched rather than guess between candidates.
        # else: no name match at all (e.g. a JP/KR set the API doesn't
        # carry) -- leave unmatched.
    return matches


def sync_set_metadata(db, api_sets: list[dict] | None = None) -> SetSyncResult:
    """Match every existing `Set` row against api.pokemontcg.io and write
    `release_rank`/`total_cards` for whatever matches -- overwrites any
    prior value on a matched row (this API's release date is now the
    source of truth, see module docstring), leaves an unmatched row's
    existing value(s) completely untouched. `api_sets` is only for tests
    (fixture data) -- production callers should leave it None and let this
    fetch live.
    """
    result = SetSyncResult()
    if api_sets is None:
        api_sets = fetch_api_sets()
        if not api_sets:
            result.api_call_succeeded = False
            return result
    elif not api_sets:
        return result

    ranks = _release_ranks(api_sets)
    set_rows = db.query(Set).all()
    matches = _match(set_rows, api_sets)

    for row in set_rows:
        api_set = matches.get(row.id)
        if api_set is None:
            result.unmatched.append(f"{row.series} / {row.name}")
            continue
        row.release_rank = ranks.get(api_set["id"])
        row.total_cards = api_set.get("total") if api_set.get("total") is not None else api_set.get("printedTotal")
        result.matched.append(f"{row.series} / {row.name}")

    db.commit()
    return result


def main() -> None:
    init_db()
    db = SessionLocal()
    try:
        result = sync_set_metadata(db)
        if not result.api_call_succeeded:
            print("set_sync: api.pokemontcg.io/v2/sets call failed (network/HTTP error) -- nothing changed, rerun later.")
            return
        print(f"set_sync: {len(result.matched)} matched, {len(result.unmatched)} unmatched")
        if result.unmatched:
            print(
                "Unmatched sets (release_rank/total_cards left as-is -- likely JP/KR, "
                "or a name/series mismatch worth a manual look):"
            )
            for name in sorted(result.unmatched):
                print(f"  - {name}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
