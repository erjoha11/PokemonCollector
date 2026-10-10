"""The in-app Run-now buttons on /sync-status (issue #199): the same
jobs.run_* as the cron routes with trigger "manual", behind the normal login
(no CRON_SECRET), and every outcome rendered as a partial, never a 500."""
import datetime as dt
import time

import jwt
import pytest
from conftest import make_csv, seed_import

import auth
import dropbox_client
import job_locks
import pokemontcg_client
import price_refresh
from models import CardSnapshot, ImportLog, JobLock
from test_dropbox_client import FakeDropbox, FakeListFolderResult, _file_entry
from test_price_refresh import FakeClient

DEX = "/sync-status/run/dex-sync"
PRICES = "/sync-status/run/price-refresh"
JWT_SECRET = "s3cret-enough-for-hs256-tests"


def _session():
    import db as db_module

    return db_module.SessionLocal()


def _logs():
    with _session() as db:
        return [(row.job, row.status, row.source) for row in db.query(ImportLog).order_by(ImportLog.id)]


def _snapshot_sources():
    with _session() as db:
        return sorted({row.source for row in db.query(CardSnapshot)})


def _dropbox_with(monkeypatch, csv_bytes):
    fake = FakeDropbox(
        pages=[FakeListFolderResult([_file_entry("main.csv")])],
        download_bytes={"/exports/main.csv": csv_bytes},
    )
    monkeypatch.setattr(dropbox_client, "build_client_from_env", lambda: fake)
    return fake


@pytest.fixture()
def offline_prices(monkeypatch):
    monkeypatch.setattr(pokemontcg_client, "Client", lambda: FakeClient())


@pytest.fixture()
def configured_auth(monkeypatch):
    monkeypatch.setattr(auth, "SUPABASE_URL", "https://x.supabase.co")
    monkeypatch.setattr(auth, "SUPABASE_ANON_KEY", "anon")
    monkeypatch.setattr(auth, "SUPABASE_JWT_SECRET", JWT_SECRET)


def _log_in(client):
    payload = {"aud": "authenticated", "sub": "user-1", "exp": int(time.time()) + 3600}
    client.cookies.set(auth.SESSION_COOKIE, jwt.encode(payload, JWT_SECRET, algorithm="HS256"))


# --- the page --------------------------------------------------------------


def test_sync_status_page_has_both_buttons_disabled_while_running(client):
    html = client.get("/sync-status").text
    assert f'hx-post="{DEX}"' in html
    assert f'hx-post="{PRICES}"' in html
    assert html.count('hx-disabled-elt=".run-now-button"') == 2
    assert "Run Dex sync now" in html and "Run price refresh now" in html


def test_run_now_routes_are_not_public_and_not_under_cron(client):
    import app as app_module

    for path in (DEX, PRICES):
        assert path not in app_module._PUBLIC_PATHS
        assert not path.startswith("/cron/")


# --- the same job as the cron, recorded as manual -----------------------------


def test_dex_sync_now_runs_the_sync_as_manual(client, monkeypatch):
    monkeypatch.delenv("CRON_SECRET", raising=False)
    _dropbox_with(monkeypatch, make_csv("My Collection", [{"id": "a", "name": "Pikachu", "qty": 2}]))

    response = client.post(DEX)

    assert response.status_code == 200
    assert "Dex sync done" in response.text and "1 created" in response.text
    assert _logs() == [("dex-sync", "ok", "manual")]
    assert _snapshot_sources() == ["manual"]
    # The rest of the page comes back out-of-band, with the new run in it.
    for element_id in ("sync-glance", "missing-cards", "sync-log"):
        assert f'id="{element_id}"' in response.text
    assert response.text.count('hx-swap-oob="true"') == 3
    assert "Pikachu" in client.get("/inventory").text


def test_price_refresh_now_runs_the_refresh_as_manual(client, monkeypatch, offline_prices):
    monkeypatch.delenv("CRON_SECRET", raising=False)

    response = client.post(PRICES)

    assert response.status_code == 200
    assert "Price refresh done" in response.text
    assert _logs() == [("price-refresh", "ok", "manual")]


def test_price_refresh_now_snapshots_as_manual(client, monkeypatch, offline_prices):
    monkeypatch.delenv("CRON_SECRET", raising=False)
    seed_import(client, [("files", ("main.csv", make_csv("My Collection", [{"id": "a"}]), "text/csv"))])

    client.post(PRICES)

    assert _snapshot_sources() == ["manual"]  # never "price-cron"


# --- auth: the login, not CRON_SECRET ---------------------------------------


@pytest.mark.parametrize("path", [DEX, PRICES])
def test_run_now_needs_the_login_when_auth_is_configured(client, monkeypatch, configured_auth, offline_prices, path):
    monkeypatch.setenv("CRON_SECRET", "s3cr3t")
    _dropbox_with(monkeypatch, make_csv("My Collection", [{"id": "a"}]))

    # Not even the right Bearer header gets past the login gate.
    response = client.post(path, headers={"Authorization": "Bearer s3cr3t"}, follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/login"
    assert _logs() == []


@pytest.mark.parametrize("path", [DEX, PRICES])
def test_run_now_needs_no_cron_secret_when_logged_in(client, monkeypatch, configured_auth, offline_prices, path):
    monkeypatch.setenv("CRON_SECRET", "s3cr3t")
    _dropbox_with(monkeypatch, make_csv("My Collection", [{"id": "a"}]))
    _log_in(client)

    response = client.post(path)

    assert response.status_code == 200
    assert [source for _job, _status, source in _logs()] == ["manual"]


@pytest.mark.parametrize("path", [DEX, PRICES])
def test_run_now_ignores_a_query_secret(client, monkeypatch, offline_prices, path):
    # Nothing secret-related is read: the trigger is always "manual".
    monkeypatch.setenv("CRON_SECRET", "s3cr3t")
    _dropbox_with(monkeypatch, make_csv("My Collection", [{"id": "a"}]))

    assert client.post(path, params={"secret": "s3cr3t"}).status_code == 200
    assert [source for _job, _status, source in _logs()] == ["manual"]


# --- every outcome is a partial, never a 500 ---------------------------------


@pytest.mark.parametrize("path, job", [(DEX, "dex-sync"), (PRICES, "price-refresh")])
def test_already_running_renders_a_partial_and_runs_nothing(client, monkeypatch, offline_prices, path, job):
    _dropbox_with(monkeypatch, make_csv("My Collection", [{"id": "a"}]))
    with _session() as db:
        job_locks.acquire(db, job, trigger="cron", now=dt.datetime.utcnow() - dt.timedelta(minutes=2))

    response = client.post(path)

    assert response.status_code == 200
    assert "already running" in response.text
    assert "cron" in response.text  # who started it
    assert _logs() == []


def test_dex_sync_now_without_dropbox_configured(client, monkeypatch):
    for name in ("DROPBOX_APP_KEY", "DROPBOX_APP_SECRET", "DROPBOX_REFRESH_TOKEN"):
        monkeypatch.delenv(name, raising=False)

    response = client.post(DEX)

    assert response.status_code == 200
    assert "Dropbox isn" in response.text and "set up on this server" in response.text
    assert _logs() == [("dex-sync", "failed", "manual")]


def test_dex_sync_now_dropbox_error(client, monkeypatch):
    def broken():
        raise dropbox_client.DropboxImportError("Dropbox auth failed: token revoked")

    monkeypatch.setattr(dropbox_client, "build_client_from_env", broken)

    response = client.post(DEX)

    assert response.status_code == 200
    assert "Dex sync failed, nothing was imported" in response.text
    assert "token revoked" in response.text
    assert _logs() == [("dex-sync", "failed", "manual")]


def test_dex_sync_now_circuit_breaker_abort(client, monkeypatch):
    seed_import(client, [("files", ("main.csv", make_csv("My Collection", [{"id": f"c{i}"} for i in range(400)]), "text/csv"))])
    _dropbox_with(monkeypatch, make_csv("My Collection", [{"id": f"c{i}"} for i in range(10)]))

    response = client.post(DEX)

    assert response.status_code == 200
    assert "stopped before writing anything" in response.text
    assert "390 of 400" in response.text
    assert _logs()[-1] == ("dex-sync", "aborted", "manual")


def test_price_refresh_now_unexpected_error_renders_a_partial(client, monkeypatch):
    def boom(db, **kw):
        raise RuntimeError("pokemontcg exploded")

    monkeypatch.setattr(price_refresh, "refresh_stale_prices", boom)

    response = client.post(PRICES)

    assert response.status_code == 200
    assert "failed with an unexpected error" in response.text
    assert "RuntimeError: pokemontcg exploded" in response.text
    assert _logs() == [("price-refresh", "failed", "manual")]  # recorded by jobs.py
    with _session() as db:
        assert db.query(JobLock).count() == 0  # the lock was released
