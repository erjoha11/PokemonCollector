"""Single-flight guard for background jobs, stored in the database (issue
#340, built to #274 point 2's design).

Usage (see `jobs._single_flight`, which every job in jobs.py runs under):

    try:
        lock = job_locks.acquire(lock_db, sync_status.DEX_SYNC, trigger="cron")
    except job_locks.AlreadyRunning as running:
        ...  # 409 {"status": "already_running", ...}; nothing written
    try:
        ...  # the job
    finally:
        job_locks.release(lock_db, lock)

Why a table (`models.JobLock`) and not something lighter:
- An in-process lock doesn't reach the other serverless instances.
- Session-level `pg_advisory_lock` isn't reliably held for a whole run with
  NullPool + Supabase's transaction-mode pooler (see db.py), and
  `pg_try_advisory_xact_lock` would need the whole sync in one transaction
  and still leave no trace of a killed run.

A run killed before `release()` (Vercel's 300 s timeout) leaves its row
behind. Once it's older than `STALE_AFTER` it's dead: the next `acquire()`
(or `reap_stale()`, run when /sync-status loads) replaces/removes it and
records that run on /sync-status as `failed`, "interrupted". Every write is
conditional on the row's `token`, so two instances racing for the same
stale lock record it once and only one of them gets the lock.

Every function here commits on its own: pass a session that isn't also
carrying the job's own work, so a lock commit never commits half a job and
a job rollback never drops the lock.

Every job in jobs.py runs behind it since #274 (one lock row per job name,
the `sync_status` job constants); nothing here is job-specific.
"""
from __future__ import annotations

import datetime as dt
import secrets
from dataclasses import dataclass

from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

import sync_status
from models import JobLock

# Comfortably above Vercel's 300 s function limit: a lock this old can't
# belong to a run that's still alive.
STALE_AFTER = dt.timedelta(minutes=15)


@dataclass(frozen=True)
class Lock:
    job: str
    token: str
    started_at: dt.datetime
    trigger: str | None


class AlreadyRunning(Exception):
    """A live (non-stale) run of the same job holds the lock."""

    def __init__(self, job: str, started_at: dt.datetime, trigger: str | None):
        super().__init__(f"{job} is already running (started {started_at:%Y-%m-%d %H:%M} UTC)")
        self.job = job
        self.started_at = started_at
        self.trigger = trigger


def _utcnow() -> dt.datetime:
    return dt.datetime.utcnow()


def _is_stale(started_at: dt.datetime, now: dt.datetime) -> bool:
    return now - started_at > STALE_AFTER


def _record_interrupted(db: Session, row_job: str, started_at: dt.datetime, trigger: str | None) -> None:
    sync_status.record_run(
        db,
        job=row_job,
        status=sync_status.FAILED,
        source=trigger or "unknown",
        ran_at=started_at,
        message=(
            f"Interrupted: started {started_at:%Y-%m-%d %H:%M} UTC and never finished, no result "
            "recorded (most likely stopped by the server's time limit). Nothing from that run "
            "was saved."
        ),
    )


def _read(db: Session, job: str) -> tuple[str, dt.datetime, str | None] | None:
    row = db.execute(select(JobLock.token, JobLock.started_at, JobLock.trigger).where(JobLock.job == job)).first()
    db.rollback()
    return None if row is None else (row.token, row.started_at, row.trigger)


def acquire(
    db: Session, job: str, *, trigger: str | None, now: dt.datetime | None = None, _attempts: int = 3
) -> Lock:
    """Take the lock for `job`, or raise AlreadyRunning. A stale lock is
    taken over and its run recorded as interrupted. Commits."""
    now = now or _utcnow()
    token = secrets.token_hex(16)
    try:
        db.add(JobLock(job=job, started_at=now, trigger=trigger, token=token))
        db.commit()
        return Lock(job, token, now, trigger)
    except IntegrityError:
        db.rollback()

    current = _read(db, job)
    if current is None:
        # Released between our INSERT and this read: try again.
        if _attempts <= 1:
            raise AlreadyRunning(job, now, None)
        return acquire(db, job, trigger=trigger, now=now, _attempts=_attempts - 1)
    old_token, old_started, old_trigger = current
    if not _is_stale(old_started, now):
        raise AlreadyRunning(job, old_started, old_trigger)

    taken = db.execute(
        update(JobLock)
        .where(JobLock.job == job, JobLock.token == old_token)
        .values(started_at=now, trigger=trigger, token=token)
    ).rowcount
    db.commit()
    if taken != 1:
        # Another instance took the stale lock over first; it's live now.
        winner = _read(db, job)
        if winner is None:
            if _attempts <= 1:
                raise AlreadyRunning(job, now, None)
            return acquire(db, job, trigger=trigger, now=now, _attempts=_attempts - 1)
        raise AlreadyRunning(job, winner[1], winner[2])
    _record_interrupted(db, job, old_started, old_trigger)
    return Lock(job, token, now, trigger)


def release(db: Session, lock: Lock) -> None:
    """Drop `lock` -- only if it's still ours (a run so slow that its lock
    went stale and was taken over must not release the new run's). Never
    raises: a failed release only means the lock goes stale and is cleaned
    up later, which mustn't turn a finished job into an error."""
    try:
        db.rollback()  # nothing of the caller's may ride along on this commit
        db.execute(delete(JobLock).where(JobLock.job == lock.job, JobLock.token == lock.token))
        db.commit()
    except Exception as exc:  # noqa: BLE001 -- see docstring
        db.rollback()
        print(f"[job-locks] could not release {lock.job}: {exc.__class__.__name__}: {exc}")


def reap_stale(db: Session, now: dt.datetime | None = None) -> int:
    """Remove every stale lock and record its run as interrupted, so a
    killed run shows on /sync-status without waiting for the job's next run
    (the Dex sync cron runs once a day). One SELECT when nothing is held.
    Never raises (it runs on a page load). Returns how many were reaped."""
    now = now or _utcnow()
    reaped = 0
    try:
        rows = [
            (row.job, row.token, row.started_at, row.trigger)
            for row in db.execute(select(JobLock)).scalars()
        ]
        db.rollback()
        for job, token, started_at, trigger in rows:
            if not _is_stale(started_at, now):
                continue
            gone = db.execute(delete(JobLock).where(JobLock.job == job, JobLock.token == token)).rowcount
            db.commit()
            if gone == 1:
                _record_interrupted(db, job, started_at, trigger)
                reaped += 1
    except Exception as exc:  # noqa: BLE001 -- see docstring
        db.rollback()
        print(f"[job-locks] could not reap stale locks: {exc.__class__.__name__}: {exc}")
    return reaped

