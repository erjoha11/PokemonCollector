"""Tests for the Sync status page (issue #264): which job outcomes get an
`import_log` row, the at-a-glance overview, and the additive schema change.
"""
import datetime as dt

import pytest

import dropbox_client
import sync_status
from conftest import make_csv, seed_import
from models import ImportLog
from test_dropbox_client import FakeDropbox, FakeListFolderResult, _file_entry


def _logs():
    import db as db_module

    with db_module.SessionLocal() as db:
        return [
            (row.job, row.status, row.source, row.message, row.files)
            for row in db.query(ImportLog).order_by(ImportLog.id).all()
        ]


def _big_collection(n):
    return make_csv("My Collection", [{"id": f"c{i}"} for i in range(n)])


# --- what gets recorded --------------------------------------------------------


def test_successful_import_records_dex_sync_ok_with_warning_text(client):
    main = make_csv("My Collection", [{"id": "a", "name": "Pikachu"}, {"id": "", "name": "No id"}])
    seed_import(client, [("files", ("main.csv", main, "text/csv"))])

    import db as db_module

    with db_module.SessionLocal() as db:
        row = db.query(ImportLog).one()
        assert (row.job, row.status) == ("dex-sync", "ok")
        assert row.warnings_count == 1
        assert row.warnings_list == ["My Collection: rad uten Id hoppet over."]

    page = client.get("/sync-status").text
    assert "My Collection: rad uten Id hoppet over." in page
    assert 'class="sync-warnings"' in page


def test_cron_empty_folder_is_recorded(client, monkeypatch):
    monkeypatch.delenv("CRON_SECRET", raising=False)
    monkeypatch.setattr(dropbox_client, "build_client_from_env", lambda: FakeDropbox(pages=[FakeListFolderResult([])]))

    assert client.get("/cron/dropbox-sync").status_code == 200

    [(job, status, source, message, _files)] = _logs()
    assert (job, status, source) == ("dex-sync", "empty", "manual")
    assert "No CSV files found" in message


def test_cron_circuit_breaker_abort_is_recorded_and_survives_the_rollback(client, monkeypatch):
    monkeypatch.delenv("CRON_SECRET", raising=False)
    seed_import(client, [("files", ("main.csv", _big_collection(400), "text/csv"))])
    fake = FakeDropbox(
        pages=[FakeListFolderResult([_file_entry("main.csv")])],
        download_bytes={"/exports/main.csv": _big_collection(10)},
    )
    monkeypatch.setattr(dropbox_client, "build_client_from_env", lambda: fake)

    assert client.get("/cron/dropbox-sync").status_code == 409

    logs = _logs()
    assert len(logs) == 2  # the seed import, then the abort
    job, status, _source, message, files = logs[-1]
    assert (job, status, files) == ("dex-sync", "aborted", "main.csv")
    assert "390 of 400" in message

    import db as db_module
    from models import Card

    with db_module.SessionLocal() as db:
        # The import itself was still rolled back: nothing flagged missing.
        assert db.query(Card).filter(Card.flagged_missing_since.isnot(None)).count() == 0


def test_cron_dropbox_error_is_recorded(client, monkeypatch):
    monkeypatch.delenv("CRON_SECRET", raising=False)

    assert client.get("/cron/dropbox-sync").status_code == 502  # Dropbox not configured

    [(job, status, _source, message, _files)] = _logs()
    assert (job, status) == ("dex-sync", "failed")
    assert message.startswith("Dropbox error:") and "DROPBOX_APP_KEY" in message


def test_cron_source_is_cron_only_for_the_real_scheduled_header(client, monkeypatch):
    monkeypatch.setenv("CRON_SECRET", "s3cr3t")
    monkeypatch.setattr(dropbox_client, "build_client_from_env", lambda: FakeDropbox(pages=[FakeListFolderResult([])]))

    client.get("/cron/dropbox-sync", headers={"Authorization": "Bearer s3cr3t"})
    client.get("/cron/dropbox-sync?secret=s3cr3t")

    assert [log[2] for log in _logs()] == ["cron", "manual"]


def test_price_refresh_run_is_recorded(client, monkeypatch):
    import pokemontcg_client
    from test_price_refresh import FakeClient

    monkeypatch.delenv("CRON_SECRET", raising=False)
    monkeypatch.setattr(pokemontcg_client, "Client", lambda: FakeClient())

    assert client.get("/cron/price-refresh").status_code == 200

    [(job, status, _source, message, _files)] = _logs()
    assert (job, status) == ("price-refresh", "ok")
    assert message.startswith("TCGplayer (pokemontcg.io): 0 requests, priced 0 of 0, 0 unmatched, 0 transient errors")


def test_set_sync_run_is_recorded_including_api_failure(client, monkeypatch):
    import set_sync

    monkeypatch.delenv("CRON_SECRET", raising=False)
    monkeypatch.setattr(set_sync, "fetch_api_sets", lambda: [])
    client.get("/cron/set-sync")

    def _boom():
        raise RuntimeError("down")

    monkeypatch.setattr(set_sync, "fetch_api_sets", _boom)
    with pytest.raises(RuntimeError):  # re-raised after recording (TestClient surfaces it)
        client.get("/cron/set-sync")

    statuses = [(log[0], log[1]) for log in _logs()]
    assert statuses[0][0] == "set-sync"
    assert statuses[-1] == ("set-sync", "failed")


def test_image_backfill_run_is_recorded(client, monkeypatch):
    monkeypatch.delenv("CRON_SECRET", raising=False)

    assert client.get("/cron/image-backfill").status_code == 200

    [(job, status, _source, message, _files)] = _logs()
    assert (job, status) == ("image-backfill", "ok")
    assert "Filled 0 of 0" in message


def test_record_run_never_raises(db_session, monkeypatch):
    def _broken_commit():
        raise RuntimeError("db gone")

    monkeypatch.setattr(db_session, "commit", _broken_commit)
    assert sync_status.record_run(db_session, job="set-sync", status="ok", source="manual") is None


# --- at-a-glance overview ----------------------------------------------------------


def _row(db, minutes, **kw):
    db.add(ImportLog(ran_at=dt.datetime(2026, 10, 1, 6, 0) + dt.timedelta(minutes=minutes), source="cron", **kw))


def test_overview_shows_problem_only_when_newer_than_last_success(db_session):
    # Pre-#264 row: NULL job/status reads as a successful Dex sync.
    _row(db_session, 0, job=None, status=None)
    _row(db_session, 1, job="price-refresh", status="failed", message="boom")
    _row(db_session, 2, job="price-refresh", status="ok", message="fine")
    _row(db_session, 3, job="dex-sync", status="aborted", message="390 of 400")
    db_session.commit()

    by_job = {e["job"]: e for e in sync_status.overview(db_session)}

    assert by_job["dex-sync"]["last_ok"].ran_at.minute == 0
    assert by_job["dex-sync"]["last_problem"].message == "390 of 400"
    assert by_job["price-refresh"]["last_ok"].message == "fine"
    assert by_job["price-refresh"]["last_problem"] is None  # since fixed
    assert by_job["set-sync"]["last_ok"] is None


def test_sync_status_page_shows_glance_block_and_problem(client, monkeypatch):
    monkeypatch.delenv("CRON_SECRET", raising=False)
    client.get("/cron/dropbox-sync")  # Dropbox not configured -> failed

    page = client.get("/sync-status").text
    assert 'id="sync-glance"' in page
    for label in ("Dex sync", "Price refresh", "Set sync", "Image backfill"):
        assert label in page
    assert "status-problem" in page
    assert "Dropbox error:" in page
    assert ">Sync status</a>" in page  # nav renamed
    assert "Activity Log" not in page
    assert "Release Notes" not in page


# --- schema (additive only) ----------------------------------------------------------


def test_init_db_adds_columns_to_an_old_import_log_and_backfills_defaults(monkeypatch):
    from sqlalchemy import create_engine, inspect, text
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    import db as db_module

    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    monkeypatch.setattr(db_module, "engine", engine)
    monkeypatch.setattr(db_module, "SessionLocal", sessionmaker(bind=engine))
    with engine.begin() as conn:
        conn.execute(
            text(
                "CREATE TABLE import_log (id INTEGER PRIMARY KEY, ran_at DATETIME NOT NULL, source VARCHAR NOT NULL,"
                " files VARCHAR, cards_created INTEGER NOT NULL DEFAULT 0, cards_updated INTEGER NOT NULL DEFAULT 0,"
                " cards_flagged_missing INTEGER NOT NULL DEFAULT 0, cards_deleted INTEGER NOT NULL DEFAULT 0,"
                " collections_touched VARCHAR, binders_touched VARCHAR, warnings_count INTEGER NOT NULL DEFAULT 0)"
            )
        )
        conn.execute(text("INSERT INTO import_log (ran_at, source) VALUES ('2026-09-01 06:00:00', 'cron')"))

    db_module.init_db()

    columns = {c["name"] for c in inspect(engine).get_columns("import_log")}
    assert {"job", "status", "message", "warnings_text", "cards_deleted"} <= columns
    with engine.connect() as conn:
        assert conn.execute(text("SELECT job, status, source FROM import_log")).one() == ("dex-sync", "ok", "cron")
