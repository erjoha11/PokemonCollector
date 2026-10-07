"""The shared job-run service (issue #274): one function per background job,
called by every trigger -- the `/cron/*` routes today, #199's in-app Run-now
buttons and #276's Claude connector later. Each trigger only maps the
returned result to its own response format (the cron routes: JSON, 409 for
an abort or a run already in progress), so the job bodies, their
/sync-status rows and the circuit breaker can't drift between triggers.

Every `run_*` function:

- takes the caller's session `db` (the job's own work) and a `trigger`
  (`cron` / `manual` / `connector`), which is what `import_log.source`
  records, so /sync-status shows who started a run;
- maps that trigger to the job's **snapshot** source, which stays within
  `cron` / `price-cron` / `manual` (a `connector` run snapshots as
  `manual`): `queries._SNAPSHOT_SOURCE_ORDER` and the one-point-per-(day,
  source) rule in `snapshots.record_daily_snapshot` assume exactly those;
- is single-flight per job (`job_locks`, issue #340): a second run while one
  is in progress returns `AlreadyRunningResult` and writes nothing; a lock
  left by a killed run goes stale and that run is recorded as interrupted;
- records its outcome on /sync-status (`sync_status.record_run`, or the
  importer's own log row for a successful Dex sync);
- returns a result dataclass, never an HTTP response. Expected outcomes
  (empty folder, circuit-breaker abort, Dropbox error, API failure,
  degraded prices) are results. An unexpected exception is recorded as
  `failed` and then re-raised, as the cron routes always did.

The circuit breaker behaves the same for every trigger: only
`run_dex_sync(..., allow_mass_missing=True)` overrides it, and no cron route
(or, later, connector tool) passes that.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session

import backfill_images
import dropbox_client
import job_locks
import price_refresh
import pricing
import set_sync
import snapshots
import sync_status
import tcgdex_prices
from importer import ImportAborted, ImportResult, import_dex_csv_files
from models import Card

CRON = "cron"
MANUAL = "manual"
CONNECTOR = "connector"
TRIGGERS = (CRON, MANUAL, CONNECTOR)

# Snapshot source per trigger, per job. Never a fourth snapshot source.
_DEX_SNAPSHOT_SOURCE = {CRON: "cron", MANUAL: "manual", CONNECTOR: "manual"}
_PRICE_SNAPSHOT_SOURCE = {CRON: "price-cron", MANUAL: "manual", CONNECTOR: "manual"}

# Per daily price-refresh run: the pokemontcg.io pass (issue #349) is
# ~10 batch requests of ~6 s each (measured 2026-10-07 on prod's 452 IDs)
# plus the first days' fallback searches (~1-2 s each, at most
# price_refresh.MAX_FALLBACK_SEARCHES_PER_RUN). Checked before each request,
# so the worst overrun is one request with its retry
# (pokemontcg_client: 2 x 12 s timeout + 3 s back-off)...
POKEMONTCG_SECONDS = 100.0
# ...a modest image pass after prices...
IMAGE_BACKFILL_PER_CRON = 60
IMAGE_BACKFILL_SECONDS = 25.0
# ...and the TCGdex price pass (issue #211): ~0.75 s per card sequentially,
# so ~120 of the 125-card budget fits; the rest wait for tomorrow.
TCGDEX_SECONDS = 90.0

# The on-demand image backfill: per-call card cap and time box.
IMAGE_BACKFILL_MAX_LIMIT = 300
IMAGE_BACKFILL_CALL_SECONDS = 50.0


# --------------------------------------------------------------------------
# Results
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class AlreadyRunningResult:
    """Another run of the same job holds its lock. Nothing was written."""

    job: str
    started_at: dt.datetime
    trigger: str | None
    message: str
    status: str = "already_running"


@dataclass
class DexSyncResult:
    """`status`: `ok`, `empty` (no CSV files), `aborted` (circuit breaker,
    see `abort` for the structured counts) or `failed` (a Dropbox error,
    see `error`)."""

    status: str
    trigger: str
    snapshot_source: str
    folder: str
    files: list[str] = field(default_factory=list)
    import_result: ImportResult | None = None
    cards_snapshotted: int = 0
    error: str | None = None
    abort: ImportAborted | None = None


@dataclass
class PriceRefreshResult:
    """`status`: `ok` or `degraded` (a price pass had only fx_rates' fallback
    constant and wrote nothing, issue #229). `tcgdex` is None when the
    TCGdex pass failed (contained, see `_run_tcgdex_refresh`)."""

    status: str
    trigger: str
    snapshot_source: str
    pokemontcg: Any  # price_refresh's result
    tcgdex: Any | None  # tcgdex_prices' result
    images: Any  # backfill_images' result
    cards_snapshotted: int
    degraded_reason: str | None = None
    message: str = ""


@dataclass
class ImageBackfillResult:
    status: str
    trigger: str
    attempted: int
    filled: int
    cards_with_image: int
    remaining: int


@dataclass
class SetSyncResult:
    """`status`: `ok` or `api_call_failed` (nothing updated)."""

    status: str
    trigger: str
    matched: int
    unmatched: list[str]


# --------------------------------------------------------------------------
# Plumbing
# --------------------------------------------------------------------------
def _check_trigger(trigger: str) -> None:
    if trigger not in TRIGGERS:
        raise ValueError(f"unknown trigger {trigger!r}, expected one of {TRIGGERS}")


def _single_flight(db: Session, job: str, trigger: str, body):
    """Run `body()` holding `job`'s lock, or return AlreadyRunningResult.

    The lock lives in its own session on the same engine: its commits must
    never carry (or be rolled back with) the job's work, and vice versa."""
    lock_db = Session(bind=db.get_bind(), autoflush=False)
    try:
        try:
            lock = job_locks.acquire(lock_db, job, trigger=trigger)
        except job_locks.AlreadyRunning as running:
            print(f"[jobs/{job}] already running since {running.started_at} ({running.trigger})")
            return AlreadyRunningResult(
                job=running.job, started_at=running.started_at, trigger=running.trigger, message=str(running)
            )
        try:
            return body()
        finally:
            job_locks.release(lock_db, lock)
    finally:
        lock_db.close()


def _record_failure(db: Session, job: str, trigger: str, exc: Exception, files: list[str] | None = None) -> None:
    db.rollback()
    sync_status.record_run(
        db,
        job=job,
        status=sync_status.FAILED,
        source=trigger,
        files=files,
        message=f"{exc.__class__.__name__}: {exc}",
    )


def resolve_all_prices(db: Session) -> None:
    """Full DB-only re-resolve of every card's market price (pricing.py,
    issue #210) -- run at the end of each sync/refresh, right before its
    snapshot. This is what applies freshness expiry (a source going stale)
    to cards nothing re-priced today. No HTTP, a few statements."""
    pricing.resolve_cards(db)
    db.commit()


# --------------------------------------------------------------------------
# Dex sync (Dropbox)
# --------------------------------------------------------------------------
def run_dex_sync(db: Session, *, trigger: str, allow_mass_missing: bool = False) -> DexSyncResult | AlreadyRunningResult:
    """Pull every CSV in the configured Dropbox folder and run a normal sync
    (flags missing cards, never deletes). A circuit-breaker trip writes
    nothing and returns `aborted`; only `allow_mass_missing=True` (the
    in-app override, #275) skips the mass-missing check.

    Every outcome leaves an `import_log` row: a successful import writes its
    own (importer._log_import); empty, aborted and failed are recorded here,
    committed separately after the rollback so the row survives it."""
    _check_trigger(trigger)
    return _single_flight(
        db, sync_status.DEX_SYNC, trigger, lambda: _dex_sync(db, trigger, allow_mass_missing)
    )


def _dex_sync(db: Session, trigger: str, allow_mass_missing: bool) -> DexSyncResult:
    snapshot_source = _DEX_SNAPSHOT_SOURCE[trigger]
    folder = dropbox_client.default_folder()
    result = DexSyncResult(status="ok", trigger=trigger, snapshot_source=snapshot_source, folder=folder)
    try:
        dbx = dropbox_client.build_client_from_env()
        files = dropbox_client.list_csv_files(dbx, folder)
        result.files = [f.name for f in files]
        if not files:
            resolve_all_prices(db)
            result.cards_snapshotted = snapshots.record_daily_snapshot(db, source=snapshot_source)
            sync_status.record_run(
                db,
                job=sync_status.DEX_SYNC,
                status=sync_status.EMPTY,
                source=trigger,
                message=f"No CSV files found in {folder}",
            )
            print(f"[jobs/dex-sync] empty: no CSV files in {folder} trigger={trigger}")
            result.status = "empty"
            return result
        payload = [(f.name, dropbox_client.download_file(dbx, f.path_lower)) for f in files]
        try:
            # Export dates (issue #351): the dex prices are dated at their
            # export, not at this re-read, and the newest file wins a category.
            result.import_result = import_dex_csv_files(
                db,
                payload,
                source=trigger,
                allow_mass_missing=allow_mass_missing,
                file_dates={f.name: f.export_date for f in files},
            )
        except ImportAborted as exc:
            # A row on /sync-status, no snapshot -- nothing ran. The rollback
            # discards the import; the log row is its own commit after it.
            db.rollback()
            print(f"[jobs/dex-sync] aborted: files={result.files} trigger={trigger} reason={exc}")
            sync_status.record_run(
                db,
                job=sync_status.DEX_SYNC,
                status=sync_status.ABORTED,
                source=trigger,
                files=result.files,
                message=str(exc),
            )
            result.status = "aborted"
            result.abort = exc
            result.error = str(exc)
            return result
        # Snapshot after the sync, not before -- always today's post-sync
        # qty/price, never yesterday's leftover state (see
        # snapshots.record_daily_snapshot / README "Value history").
        resolve_all_prices(db)
        result.cards_snapshotted = snapshots.record_daily_snapshot(db, source=snapshot_source)
        imported = result.import_result
        print(
            f"[jobs/dex-sync] ok: trigger={trigger} files={result.files} "
            f"created={imported.cards_created} updated={imported.cards_updated} "
            f"flagged={imported.cards_flagged_missing} "
            f"collections={sorted(imported.collections_touched)} "
            f"binders={sorted(imported.binders_touched)} "
            f"warnings={len(imported.warnings)} "
            f"unowned_skipped={imported.unowned_rows_skipped} "
            f"snapshotted={result.cards_snapshotted}"
        )
        return result
    except (dropbox_client.DropboxNotConfigured, dropbox_client.DropboxImportError) as exc:
        # Usually unattended -- this lands in Vercel's runtime logs and on
        # /sync-status.
        print(f"[jobs/dex-sync] failed: trigger={trigger} {exc}")
        db.rollback()
        sync_status.record_run(
            db,
            job=sync_status.DEX_SYNC,
            status=sync_status.FAILED,
            source=trigger,
            files=result.files,
            message=f"Dropbox error: {exc}",
        )
        result.status = "failed"
        result.error = str(exc)
        return result
    except Exception as exc:
        _record_failure(db, sync_status.DEX_SYNC, trigger, exc, files=result.files)
        raise


# --------------------------------------------------------------------------
# Price refresh
# --------------------------------------------------------------------------
def run_price_refresh(db: Session, *, trigger: str) -> PriceRefreshResult | AlreadyRunningResult:
    """The pokemontcg.io pass, the TCGdex pass, a full re-resolve, the day's
    snapshot (`price-cron` for a cron run, else `manual`), then a small
    image pass. `degraded` when a price pass only had the FX fallback."""
    _check_trigger(trigger)
    return _single_flight(db, sync_status.PRICE_REFRESH, trigger, lambda: _price_refresh(db, trigger))


def _price_refresh(db: Session, trigger: str) -> PriceRefreshResult:
    snapshot_source = _PRICE_SNAPSHOT_SOURCE[trigger]
    try:
        # pokemontcg.io prices by stored ID, ~10 batch requests plus a few
        # fallback searches (issue #349), time-boxed. Transient failures are
        # counted and stop the pass, never raised.
        result = price_refresh.refresh_stale_prices(db, time_budget_s=POKEMONTCG_SECONDS)
        # Then TCGdex (issue #211): both its TCGplayer and Cardmarket prices,
        # time-boxed. A TCGdex problem must never cost the day's snapshot.
        tcgdex = _run_tcgdex_refresh(db)
        # Snapshot right after refreshing: today's post-refresh prices.
        resolve_all_prices(db)
        snapshotted = snapshots.record_daily_snapshot(db, source=snapshot_source)
        # Then a small pass of missing card images, time-boxed.
        images = backfill_images.run_backfill(db, limit=IMAGE_BACKFILL_PER_CRON, time_budget_s=IMAGE_BACKFILL_SECONDS)
        print(f"[jobs/price-refresh] images: attempted={images.attempted} filled={images.filled}")
        print(
            f"[jobs/price-refresh] pokemontcg: trigger={trigger} {price_refresh.summary_line(result)} "
            f"snapshotted={snapshotted}"
        )
        if tcgdex is not None:
            print(
                f"[jobs/price-refresh] tcgdex: checked={tcgdex.cards_checked} priced={tcgdex.cards_priced} "
                f"ids_matched={tcgdex.ids_matched} unmatched={len(tcgdex.cards_unmatched)} "
                f"variant_uncertain={len(tcgdex.cards_variant_uncertain)} "
                f"transient_errors={tcgdex.transient_errors} stopped={tcgdex.stopped} "
                f"http_calls={tcgdex.http_calls} eur_to_nok={tcgdex.eur_to_nok}"
            )
        # Degraded (issue #229) when either price pass had only fx_rates'
        # fallback constant and so wrote nothing -- the snapshot and image
        # pass still ran, but it's never reported as "ok".
        degraded_reasons = list(
            dict.fromkeys(
                r.degraded_reason for r in (result, tcgdex) if r is not None and r.status != "ok" and r.degraded_reason
            )
        )
        if degraded_reasons:
            print(f"[jobs/price-refresh] DEGRADED: {' '.join(degraded_reasons)}")
        message = price_refresh_message(result, tcgdex, images, snapshotted)
        sync_status.record_run(
            db,
            job=sync_status.PRICE_REFRESH,
            status=sync_status.DEGRADED if degraded_reasons else sync_status.OK,
            source=trigger,
            message=f"{' '.join(degraded_reasons)} {message}" if degraded_reasons else message,
        )
        return PriceRefreshResult(
            status="degraded" if degraded_reasons else "ok",
            trigger=trigger,
            snapshot_source=snapshot_source,
            pokemontcg=result,
            tcgdex=tcgdex,
            images=images,
            cards_snapshotted=snapshotted,
            degraded_reason=" ".join(degraded_reasons) or None,
            message=message,
        )
    except Exception as exc:
        _record_failure(db, sync_status.PRICE_REFRESH, trigger, exc)
        raise


def price_refresh_message(result, tcgdex, images, snapshotted: int) -> str:
    """One-line /sync-status summary of a price-refresh run."""
    tcgplayer = (
        f"TCGplayer (pokemontcg.io): {result.requests} requests, "
        f"priced {result.cards_updated} of {result.cards_checked}, "
        f"{len(result.cards_unmatched)} unmatched, {result.transient_errors} transient errors"
    )
    if result.stopped:
        tcgplayer += f", stopped ({result.stopped}), {result.cards_deferred} cards left for tomorrow"
    parts = [tcgplayer]
    if result.ids_found:
        parts.append(f"{result.ids_found} pokemontcg IDs found by search")
    if result.cards_variant_uncertain:
        parts.append(f"{len(result.cards_variant_uncertain)} variant uncertain")
    if tcgdex is None:
        parts.append("TCGdex: failed")
    else:
        parts.append(f"TCGdex: checked {tcgdex.cards_checked}, priced {tcgdex.cards_priced}")
    parts.append(f"images: filled {images.filled} of {images.attempted}")
    parts.append(f"{snapshotted} cards snapshotted")
    if result.usd_to_nok is not None:
        parts.append(f"USD/NOK {result.usd_to_nok:g} ({result.fx_source})")
    return "; ".join(parts)


def _run_tcgdex_refresh(db: Session):
    """tcgdex_prices.refresh_tcgdex_prices, contained: an unexpected error is
    logged and rolled back (whatever it committed so far stays) so the run
    still resolves and snapshots. Returns None when it failed."""
    try:
        return tcgdex_prices.refresh_tcgdex_prices(db, time_budget_s=TCGDEX_SECONDS)
    except Exception as exc:  # noqa: BLE001 -- see docstring
        db.rollback()
        print(f"[jobs/price-refresh] tcgdex failed: {exc.__class__.__name__}: {exc}")
        return None


# --------------------------------------------------------------------------
# Image backfill
# --------------------------------------------------------------------------
def run_image_backfill(db: Session, *, trigger: str, limit: int = 100) -> ImageBackfillResult | AlreadyRunningResult:
    """A catch-up pass for missing card images (backfill_images.py), up to
    `limit` cards (clamped to 1..IMAGE_BACKFILL_MAX_LIMIT), time-boxed; call
    again while `remaining` > 0."""
    _check_trigger(trigger)
    return _single_flight(db, sync_status.IMAGE_BACKFILL, trigger, lambda: _image_backfill(db, trigger, limit))


def _image_backfill(db: Session, trigger: str, limit: int) -> ImageBackfillResult:
    try:
        result = backfill_images.run_backfill(
            db, limit=max(1, min(limit, IMAGE_BACKFILL_MAX_LIMIT)), time_budget_s=IMAGE_BACKFILL_CALL_SECONDS
        )
        remaining = db.query(Card).filter(Card.image_url.is_(None), Card.image_lookup_failed_at.is_(None)).count()
        with_image = db.query(Card).filter(Card.image_url.isnot(None)).count()
        print(
            f"[jobs/image-backfill] trigger={trigger} attempted={result.attempted} filled={result.filled} "
            f"remaining={remaining}"
        )
        sync_status.record_run(
            db,
            job=sync_status.IMAGE_BACKFILL,
            status=sync_status.OK,
            source=trigger,
            message=f"Filled {result.filled} of {result.attempted} attempted; {remaining} still missing",
        )
        return ImageBackfillResult(
            status="ok",
            trigger=trigger,
            attempted=result.attempted,
            filled=result.filled,
            cards_with_image=with_image,
            remaining=remaining,
        )
    except Exception as exc:
        _record_failure(db, sync_status.IMAGE_BACKFILL, trigger, exc)
        raise


# --------------------------------------------------------------------------
# Set sync
# --------------------------------------------------------------------------
def run_set_sync(db: Session, *, trigger: str) -> SetSyncResult | AlreadyRunningResult:
    """Set metadata from api.pokemontcg.io (set_sync.py): `total_cards`, and
    a `release_rank` only for sets still missing one."""
    _check_trigger(trigger)
    return _single_flight(db, sync_status.SET_SYNC, trigger, lambda: _set_sync(db, trigger))


def _set_sync(db: Session, trigger: str) -> SetSyncResult:
    try:
        result = set_sync.sync_set_metadata(db)
        print(
            f"[jobs/set-sync] trigger={trigger} api_ok={result.api_call_succeeded} "
            f"matched={len(result.matched)} unmatched={len(result.unmatched)}"
        )
        sync_status.record_run(
            db,
            job=sync_status.SET_SYNC,
            status=sync_status.OK if result.api_call_succeeded else sync_status.FAILED,
            source=trigger,
            message=(
                f"Matched {len(result.matched)} sets, {len(result.unmatched)} unmatched"
                if result.api_call_succeeded
                else "api.pokemontcg.io call failed; nothing updated"
            ),
        )
        return SetSyncResult(
            status="ok" if result.api_call_succeeded else "api_call_failed",
            trigger=trigger,
            matched=len(result.matched),
            unmatched=sorted(result.unmatched),
        )
    except Exception as exc:
        _record_failure(db, sync_status.SET_SYNC, trigger, exc)
        raise
