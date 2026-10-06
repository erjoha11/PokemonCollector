"""Single-flight guard for /cron/dropbox-sync (issue #340, #274 point 2):
the `job_locks` table, stale takeover, and the interrupted-run record."""
import datetime as dt

import pytest
from conftest import make_csv

import dropbox_client
import job_locks
import sync_status
from models import ImportLog, JobLock
from test_dropbox_client import FakeDropbox, FakeListFolderResult, _file_entry

JOB = sync_status.DEX_SYNC
T0 = dt.datetime(2026, 10, 6, 14, 0)


def _logs(db):
    return db.query(ImportLog).order_by(ImportLog.id).all()


# --- schema ----------------------------------------------------------------


def test_migration_from_v13_creates_job_locks_with_rls(monkeypatch):
    """#340: an already-migrated (v13) database gets the job_locks table on
    the next init_db(), and the RLS step covers it on Postgres."""
    from sqlalchemy import inspect, text
    from sqlalchemy.orm import sessionmaker

    import db as db_module
    from test_db import _FakePostgresEngine, _fresh_engine

    engine = _fresh_engine()
    monkeypatch.setattr(db_module, "engine", engine)
    monkeypatch.setattr(db_module, "SessionLocal", sessionmaker(bind=engine))
    db_module.init_db()
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE job_locks"))
    db_module._set_schema_version(13)

    db_module.init_db()

    assert inspect(engine).has_table("job_locks")
    assert db_module._get_schema_version() == db_module.CURRENT_SCHEMA_VERSION
    fake = _FakePostgresEngine()
    monkeypatch.setattr(db_module, "engine", fake)
    db_module._enable_row_level_security()
    assert 'ALTER TABLE public."job_locks" ENABLE ROW LEVEL SECURITY' in fake.statements


# --- the lock itself ---------------------------------------------------------


def test_acquire_then_release(db_session):
    lock = job_locks.acquire(db_session, JOB, trigger="cron", now=T0)
    row = db_session.query(JobLock).one()
    assert (row.job, row.started_at, row.trigger, row.token) == (JOB, T0, "cron", lock.token)

    job_locks.release(db_session, lock)
    assert db_session.query(JobLock).count() == 0


def test_second_run_while_live_is_refused_and_writes_nothing(db_session):
    job_locks.acquire(db_session, JOB, trigger="cron", now=T0)

    with pytest.raises(job_locks.AlreadyRunning) as info:
        job_locks.acquire(db_session, JOB, trigger="manual", now=T0 + dt.timedelta(minutes=14))

    assert info.value.started_at == T0
    assert info.value.trigger == "cron"
    row = db_session.query(JobLock).one()
    assert (row.started_at, row.trigger) == (T0, "cron")  # untouched
    assert _logs(db_session) == []


def test_other_jobs_are_not_blocked(db_session):
    job_locks.acquire(db_session, JOB, trigger="cron", now=T0)
    job_locks.acquire(db_session, sync_status.PRICE_REFRESH, trigger="cron", now=T0)
    assert db_session.query(JobLock).count() == 2


def test_stale_lock_is_taken_over_and_the_killed_run_recorded(db_session):
    job_locks.acquire(db_session, JOB, trigger="cron", now=T0)
    later = T0 + job_locks.STALE_AFTER + dt.timedelta(seconds=1)

    lock = job_locks.acquire(db_session, JOB, trigger="manual", now=later)

    row = db_session.query(JobLock).one()
    assert (row.started_at, row.trigger, row.token) == (later, "manual", lock.token)
    (log,) = _logs(db_session)
    assert (log.job, log.status, log.source, log.ran_at) == (JOB, sync_status.FAILED, "cron", T0)
    assert log.message.startswith("Interrupted")


def test_a_late_release_never_drops_a_newer_runs_lock(db_session):
    old = job_locks.acquire(db_session, JOB, trigger="cron", now=T0)
    new = job_locks.acquire(db_session, JOB, trigger="manual", now=T0 + dt.timedelta(minutes=20))

    job_locks.release(db_session, old)  # the slow first run finally ends

    assert db_session.query(JobLock).one().token == new.token


def test_reap_stale_records_and_removes_only_stale_locks(db_session):
    job_locks.acquire(db_session, JOB, trigger="cron", now=T0)
    job_locks.acquire(db_session, sync_status.PRICE_REFRESH, trigger="cron", now=T0 + dt.timedelta(minutes=10))

    reaped = job_locks.reap_stale(db_session, now=T0 + dt.timedelta(minutes=16))

    assert reaped == 1
    assert [row.job for row in db_session.query(JobLock)] == [sync_status.PRICE_REFRESH]
    (log,) = _logs(db_session)
    assert (log.job, log.status, log.ran_at) == (JOB, sync_status.FAILED, T0)
    # Idempotent: nothing left to reap, nothing recorded twice.
    assert job_locks.reap_stale(db_session, now=T0 + dt.timedelta(minutes=16)) == 0
    assert len(_logs(db_session)) == 1


def test_reap_stale_with_nothing_held_writes_nothing(db_session):
    assert job_locks.reap_stale(db_session) == 0
    assert _logs(db_session) == []


# --- /cron/dropbox-sync -------------------------------------------------------


def _fake_dropbox(monkeypatch, calls=None):
    csv_bytes = make_csv("My Collection", [{"id": "a", "name": "Pikachu", "qty": 1}])
    fake = FakeDropbox(
        pages=[FakeListFolderResult([_file_entry("main.csv")])],
        download_bytes={"/exports/main.csv": csv_bytes},
    )

    def build():
        if calls is not None:
            calls.append("dropbox")
        return fake

    monkeypatch.setattr(dropbox_client, "build_client_from_env", build)


def _hold_lock(started_at, trigger="cron"):
    import db as db_module

    with db_module.SessionLocal() as db:
        return job_locks.acquire(db, JOB, trigger=trigger, now=started_at)


def test_route_refuses_while_a_run_is_in_progress(client, monkeypatch):
    calls = []
    _fake_dropbox(monkeypatch, calls)
    _hold_lock(dt.datetime.utcnow() - dt.timedelta(minutes=2))

    response = client.get("/cron/dropbox-sync")

    assert response.status_code == 409
    body = response.json()
    assert body["status"] == "already_running"
    assert body["job"] == JOB
    assert body["trigger"] == "cron"
    assert calls == []  # never got as far as Dropbox
    import db as db_module

    with db_module.SessionLocal() as db:
        assert _logs(db) == []  # "writes nothing"
        assert db.query(JobLock).count() == 1  # the running sync's lock stays


def test_route_releases_the_lock_after_success_and_failure(client, monkeypatch):
    import db as db_module

    _fake_dropbox(monkeypatch)
    assert client.get("/cron/dropbox-sync").status_code == 200
    with db_module.SessionLocal() as db:
        assert db.query(JobLock).count() == 0

    def broken():
        raise dropbox_client.DropboxImportError("boom")

    monkeypatch.setattr(dropbox_client, "build_client_from_env", broken)
    assert client.get("/cron/dropbox-sync").status_code == 502
    with db_module.SessionLocal() as db:
        assert db.query(JobLock).count() == 0


def test_route_takes_over_a_stale_lock_and_shows_the_killed_run(client, monkeypatch):
    _fake_dropbox(monkeypatch)
    killed_at = dt.datetime.utcnow() - dt.timedelta(minutes=30)
    _hold_lock(killed_at)

    response = client.get("/cron/dropbox-sync")

    assert response.status_code == 200
    import db as db_module

    with db_module.SessionLocal() as db:
        statuses = [(log.status, log.ran_at) for log in _logs(db)]
        assert (sync_status.FAILED, killed_at) in statuses
        assert any(status == sync_status.OK for status, _ in statuses)
        assert db.query(JobLock).count() == 0
    page = client.get("/sync-status").text
    assert "Interrupted" in page


def test_sync_status_page_reaps_a_stale_lock(client):
    _hold_lock(dt.datetime.utcnow() - dt.timedelta(minutes=30))

    page = client.get("/sync-status").text

    assert "Interrupted" in page
    import db as db_module

    with db_module.SessionLocal() as db:
        assert db.query(JobLock).count() == 0
        (log,) = _logs(db)
        assert log.status == sync_status.FAILED


def test_sync_status_page_leaves_a_live_lock_alone(client):
    _hold_lock(dt.datetime.utcnow() - dt.timedelta(minutes=2))

    client.get("/sync-status")

    import db as db_module

    with db_module.SessionLocal() as db:
        assert db.query(JobLock).count() == 1
        assert _logs(db) == []
