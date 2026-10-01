"""Sync status (issue #264): recording background-job outcomes in
`import_log`, and reading them back for the `/sync-status` page.

Before #264 only a *successful* Dex sync left a row (written by
`importer._log_import`, inside the import's own transaction). Everything
else -- an empty Dropbox folder, a circuit-breaker abort (#225), a Dropbox
error, and every price-refresh / set-sync / image-backfill run -- was at
most a `print()` in Vercel's runtime logs. `record_run` writes those rows.

No new table: `import_log` got four nullable columns instead (`job`,
`status`, `message`, `warnings_text`; see `models.ImportLog`). A NULL
`job`/`status` is a pre-#264 row and means a successful Dex sync
(`db._backfill_import_log_defaults` fills them in on deploy, but the
queries here don't rely on that).
"""
from __future__ import annotations

import datetime as dt

from sqlalchemy import or_
from sqlalchemy.orm import Session

from models import ImportLog

DEX_SYNC = "dex-sync"
PRICE_REFRESH = "price-refresh"
SET_SYNC = "set-sync"
IMAGE_BACKFILL = "image-backfill"

# Display order on the at-a-glance block.
JOBS: list[tuple[str, str]] = [
    (DEX_SYNC, "Dex sync"),
    (PRICE_REFRESH, "Price refresh"),
    (SET_SYNC, "Set sync"),
    (IMAGE_BACKFILL, "Image backfill"),
]
JOB_LABELS = dict(JOBS)

OK = "ok"
EMPTY = "empty"  # Dex sync found no CSV files in the Dropbox folder
ABORTED = "aborted"  # Dex sync stopped by the import circuit breaker (#225)
FAILED = "failed"  # an error (Dropbox, API, or unexpected)
PROBLEM_STATUSES = (EMPTY, ABORTED, FAILED)


def record_run(
    db: Session,
    *,
    job: str,
    status: str,
    source: str,
    message: str | None = None,
    files: list[str] | None = None,
) -> ImportLog | None:
    """Add and commit one `import_log` row for a job run.

    Precondition: `db` has nothing pending -- the caller has already
    committed its work (success) or rolled it back (failure/abort). The row
    is committed on its own, so an aborted import's rollback never takes the
    log row with it.

    Never raises: failing to write the log must not turn a finished job (or
    its real error response) into a different error. A failure here is
    printed for Vercel's runtime logs and rolled back.
    """
    row = ImportLog(
        ran_at=dt.datetime.utcnow(),
        source=source,
        files=", ".join(files) if files else None,
        job=job,
        status=status,
        message=message,
    )
    try:
        db.add(row)
        db.commit()
        return row
    except Exception as exc:  # noqa: BLE001 -- see docstring
        db.rollback()
        print(f"[sync-status] could not record {job}/{status}: {exc.__class__.__name__}: {exc}")
        return None


def _job_filter(job: str):
    if job == DEX_SYNC:
        return or_(ImportLog.job.is_(None), ImportLog.job == DEX_SYNC)
    return ImportLog.job == job


def _ok_filter():
    return or_(ImportLog.status.is_(None), ImportLog.status == OK)


def _latest(db: Session, *filters) -> ImportLog | None:
    return db.query(ImportLog).filter(*filters).order_by(ImportLog.ran_at.desc(), ImportLog.id.desc()).first()


def overview(db: Session) -> list[dict]:
    """One entry per job for the page's at-a-glance block: the last
    successful run, plus the last empty/aborted/failed run *only* when it's
    newer than that success (an old, since-fixed problem isn't news)."""
    entries = []
    for job, label in JOBS:
        last_ok = _latest(db, _job_filter(job), _ok_filter())
        last_problem = _latest(db, _job_filter(job), ImportLog.status.in_(PROBLEM_STATUSES))
        if last_problem is not None and last_ok is not None:
            if (last_problem.ran_at, last_problem.id) < (last_ok.ran_at, last_ok.id):
                last_problem = None
        entries.append({"job": job, "label": label, "last_ok": last_ok, "last_problem": last_problem})
    return entries
