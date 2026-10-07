"""The shared job-run service (jobs.py, issue #274): trigger vs snapshot
source, single-flight for every job, structured ImportAborted, and no way
for a cron route to override the circuit breaker."""
import datetime as dt
import inspect
from types import SimpleNamespace

import pytest
from conftest import make_csv, seed_import

import dropbox_client
import importer
import job_locks
import jobs
import pokemontcg_client
import set_sync
import sync_status
from importer import import_dex_csv_files
from models import CardSnapshot, ImportLog, JobLock
from test_dropbox_client import FakeDropbox, FakeListFolderResult, _file_entry
from test_price_refresh import FakeClient


def _session():
    import db as db_module

    return db_module.SessionLocal()


def _logs():
    with _session() as db:
        return [(row.job, row.status, row.source) for row in db.query(ImportLog).order_by(ImportLog.id)]


def _snapshot_sources():
    with _session() as db:
        return sorted({row.source for row in db.query(CardSnapshot)})


def _collection(n):
    return make_csv("My Collection", [{"id": f"c{i}"} for i in range(n)])


def _dropbox_with(monkeypatch, csv_bytes):
    fake = FakeDropbox(
        pages=[FakeListFolderResult([_file_entry("main.csv")])],
        download_bytes={"/exports/main.csv": csv_bytes},
    )
    monkeypatch.setattr(dropbox_client, "build_client_from_env", lambda: fake)


@pytest.fixture()
def offline_jobs(monkeypatch):
    monkeypatch.setattr(dropbox_client, "build_client_from_env", lambda: FakeDropbox(pages=[FakeListFolderResult([])]))
    monkeypatch.setattr(pokemontcg_client, "Client", lambda: FakeClient())
    monkeypatch.setattr(
        set_sync, "sync_set_metadata", lambda db: SimpleNamespace(api_call_succeeded=True, matched=[], unmatched=[])
    )


RUNNERS = {
    sync_status.DEX_SYNC: jobs.run_dex_sync,
    sync_status.PRICE_REFRESH: jobs.run_price_refresh,
    sync_status.IMAGE_BACKFILL: jobs.run_image_backfill,
    sync_status.SET_SYNC: jobs.run_set_sync,
}
ROUTES = {
    sync_status.DEX_SYNC: "/cron/dropbox-sync",
    sync_status.PRICE_REFRESH: "/cron/price-refresh",
    sync_status.IMAGE_BACKFILL: "/cron/image-backfill",
    sync_status.SET_SYNC: "/cron/set-sync",
}


# --- trigger vs snapshot source ----------------------------------------------


@pytest.mark.parametrize(
    "trigger, snapshot",
    [("cron", "cron"), ("manual", "manual"), ("connector", "manual")],
)
def test_dex_sync_records_the_trigger_and_maps_the_snapshot_source(client, monkeypatch, trigger, snapshot):
    _dropbox_with(monkeypatch, make_csv("My Collection", [{"id": "a", "name": "Pikachu"}]))
    with _session() as db:
        result = jobs.run_dex_sync(db, trigger=trigger)

    assert result.status == "ok"
    assert result.import_result.cards_created == 1
    assert result.snapshot_source == snapshot
    assert _logs() == [("dex-sync", "ok", trigger)]  # import_log.source = who started it
    assert _snapshot_sources() == [snapshot]


@pytest.mark.parametrize(
    "trigger, snapshot",
    [("cron", "price-cron"), ("manual", "manual"), ("connector", "manual")],
)
def test_price_refresh_records_the_trigger_and_maps_the_snapshot_source(
    client, offline_jobs, trigger, snapshot
):
    seed_import(client, [("files", ("main.csv", make_csv("My Collection", [{"id": "a"}]), "text/csv"))])
    with _session() as db:
        result = jobs.run_price_refresh(db, trigger=trigger)

    assert result.status == "ok"
    assert result.snapshot_source == snapshot
    assert _logs()[-1] == ("price-refresh", "ok", trigger)
    assert _snapshot_sources() == [snapshot]


def test_snapshot_sources_never_leave_the_three_known_ones():
    import queries

    known = set(queries._SNAPSHOT_SOURCE_ORDER)
    assert set(jobs._DEX_SNAPSHOT_SOURCE.values()) | set(jobs._PRICE_SNAPSHOT_SOURCE.values()) <= known
    assert set(jobs._DEX_SNAPSHOT_SOURCE) == set(jobs._PRICE_SNAPSHOT_SOURCE) == set(jobs.TRIGGERS)


@pytest.mark.parametrize("job", list(RUNNERS))
def test_unknown_trigger_is_refused_before_anything_runs(client, offline_jobs, job):
    with _session() as db, pytest.raises(ValueError):
        RUNNERS[job](db, trigger="price-cron")
    assert _logs() == []


# --- single-flight, every job -------------------------------------------------


def _hold_lock(job, started_at, trigger="cron"):
    with _session() as db:
        return job_locks.acquire(db, job, trigger=trigger, now=started_at)


@pytest.mark.parametrize("job", list(RUNNERS))
def test_second_run_of_any_job_is_already_running_and_writes_nothing(client, offline_jobs, job):
    _hold_lock(job, dt.datetime.utcnow() - dt.timedelta(minutes=2))

    with _session() as db:
        result = RUNNERS[job](db, trigger="connector")

    assert isinstance(result, jobs.AlreadyRunningResult)
    assert (result.status, result.job, result.trigger) == ("already_running", job, "cron")
    assert _logs() == []
    with _session() as db:
        assert db.query(JobLock).count() == 1  # the running job keeps its lock


@pytest.mark.parametrize("job", list(RUNNERS))
def test_cron_route_of_any_job_answers_409_already_running(client, offline_jobs, job):
    _hold_lock(job, dt.datetime.utcnow() - dt.timedelta(minutes=2))

    response = client.get(ROUTES[job])

    assert response.status_code == 409
    body = response.json()
    assert (body["status"], body["job"], body["trigger"]) == ("already_running", job, "cron")
    assert _logs() == []


@pytest.mark.parametrize("job", list(RUNNERS))
def test_stale_lock_of_any_job_is_taken_over_and_the_killed_run_recorded(client, offline_jobs, job):
    killed_at = dt.datetime.utcnow() - dt.timedelta(minutes=30)
    _hold_lock(job, killed_at, trigger="manual")

    with _session() as db:
        result = RUNNERS[job](db, trigger="cron")

    assert not isinstance(result, jobs.AlreadyRunningResult)
    with _session() as db:
        rows = db.query(ImportLog).order_by(ImportLog.id).all()
        interrupted = [r for r in rows if r.status == sync_status.FAILED]
        assert [(r.job, r.ran_at, r.source) for r in interrupted] == [(job, killed_at, "manual")]
        assert "Interrupted" in interrupted[0].message
        assert db.query(JobLock).count() == 0  # released after the new run


@pytest.mark.parametrize("job", list(RUNNERS))
def test_lock_is_released_after_an_unexpected_error(client, monkeypatch, offline_jobs, job):
    def boom(*args, **kwargs):
        raise RuntimeError("bug")

    monkeypatch.setattr(dropbox_client, "list_csv_files", boom)
    monkeypatch.setattr(jobs.price_refresh, "refresh_stale_prices", boom)
    monkeypatch.setattr(jobs.backfill_images, "run_backfill", boom)
    monkeypatch.setattr(set_sync, "sync_set_metadata", boom)

    with _session() as db, pytest.raises(RuntimeError):
        RUNNERS[job](db, trigger="manual")

    assert _logs() == [(job, "failed", "manual")]
    with _session() as db:
        assert db.query(JobLock).count() == 0


# --- structured abort, and no override from cron ------------------------------


def test_mass_missing_abort_carries_structured_counts(db_session):
    import_dex_csv_files(db_session, [("main.csv", _collection(400))])

    with pytest.raises(importer.ImportAborted) as exc:
        import_dex_csv_files(db_session, [("main.csv", _collection(300))])

    aborted = exc.value
    assert aborted.overridable
    assert (aborted.newly_missing, aborted.total_cards, aborted.limit_fraction) == (
        100,
        400,
        importer.MISSING_ABORT_FRACTION,
    )
    assert aborted.sample_names == [f"Card {i}" for i in range(300, 310)]
    db_session.rollback()


def test_empty_my_collection_abort_has_no_counts(db_session):
    with pytest.raises(importer.ImportAborted) as exc:
        import_dex_csv_files(db_session, [("My Collection.csv", make_csv("My Collection", []))])
    assert not exc.value.overridable
    assert (exc.value.newly_missing, exc.value.total_cards, exc.value.sample_names) == (None, None, [])
    db_session.rollback()


def test_aborted_dex_sync_result_and_override(client, monkeypatch):
    seed_import(client, [("files", ("main.csv", _collection(400), "text/csv"))])
    _dropbox_with(monkeypatch, _collection(10))

    with _session() as db:
        result = jobs.run_dex_sync(db, trigger="connector")
    assert result.status == "aborted"
    assert (result.abort.newly_missing, result.abort.total_cards) == (390, 400)
    assert _logs()[-1] == ("dex-sync", "aborted", "connector")

    with _session() as db:
        result = jobs.run_dex_sync(db, trigger="manual", allow_mass_missing=True)
    assert result.status == "ok"
    assert result.import_result.cards_flagged_missing == 390


def test_cron_route_cannot_override_the_circuit_breaker(client, monkeypatch):
    seed_import(client, [("files", ("main.csv", _collection(400), "text/csv"))])
    _dropbox_with(monkeypatch, _collection(10))

    response = client.get("/cron/dropbox-sync", params={"allow_mass_missing": "true"})

    assert response.status_code == 409
    assert response.json()["status"] == "aborted"
    import app as app_module

    for route in app_module.app.routes:
        if getattr(route, "path", "").startswith("/cron/"):
            assert "allow_mass_missing" not in inspect.signature(route.endpoint).parameters, route.path
