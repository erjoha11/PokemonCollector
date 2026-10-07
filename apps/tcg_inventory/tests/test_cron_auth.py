"""The shared /cron/* auth check (cron_auth.require_cron_secret, issue #226),
over all four routes: fails closed on a deploy without CRON_SECRET, Bearer
first, the deprecated `?secret=` still accepted, local no-login dev open.
"""
from types import SimpleNamespace

import pytest

import auth
import cron_auth
import dropbox_client
import jobs
import pokemontcg_client
import set_sync
from models import ImportLog
from test_dropbox_client import FakeDropbox, FakeListFolderResult
from test_price_refresh import FakeClient

SECRET = "s3cr3t"
ROUTES = ["/cron/dropbox-sync", "/cron/price-refresh", "/cron/image-backfill", "/cron/set-sync"]
JOBS = {
    "/cron/dropbox-sync": "dex-sync",
    "/cron/price-refresh": "price-refresh",
    "/cron/image-backfill": "image-backfill",
    "/cron/set-sync": "set-sync",
}


@pytest.fixture(autouse=True)
def offline_jobs(monkeypatch):
    """Each job's body, cheap and offline: an empty Dropbox folder, an empty
    pokemontcg.io, and a set sync that matched nothing. Only the auth gate
    is under test here."""
    monkeypatch.setattr(dropbox_client, "build_client_from_env", lambda: FakeDropbox(pages=[FakeListFolderResult([])]))
    monkeypatch.setattr(pokemontcg_client, "Client", lambda: FakeClient())
    monkeypatch.setattr(
        set_sync,
        "sync_set_metadata",
        lambda db: SimpleNamespace(api_call_succeeded=True, matched=[], unmatched=[]),
    )


@pytest.fixture()
def configured_auth(monkeypatch):
    monkeypatch.setattr(auth, "SUPABASE_URL", "https://x.supabase.co")
    monkeypatch.setattr(auth, "SUPABASE_ANON_KEY", "anon")


def _sources(job):
    import db as db_module

    with db_module.SessionLocal() as db:
        return [row.source for row in db.query(ImportLog).filter(ImportLog.job == job).order_by(ImportLog.id)]


@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize("unset", ["", "   "])
def test_deploy_without_cron_secret_refuses_everything(client, monkeypatch, configured_auth, route, unset):
    monkeypatch.setenv("CRON_SECRET", unset)
    for kwargs in ({}, {"headers": {"Authorization": "Bearer "}}, {"params": {"secret": ""}}):
        response = client.get(route, **kwargs)
        assert response.status_code == 503, (kwargs, response.text)
        assert "CRON_SECRET" in response.text
    assert _sources(JOBS[route]) == []  # refused before the job ran


@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize("auth_on", [False, True])
def test_missing_or_wrong_secret_is_refused(client, monkeypatch, request, route, auth_on):
    if auth_on:
        request.getfixturevalue("configured_auth")
    monkeypatch.setenv("CRON_SECRET", SECRET)
    for kwargs in (
        {},
        {"headers": {"Authorization": "Bearer wrong"}},
        {"headers": {"Authorization": f"Basic {SECRET}"}},
        {"headers": {"Authorization": SECRET}},
        {"params": {"secret": "wrong"}},
        {"params": {"secret": SECRET[:-1]}},
    ):
        response = client.get(route, **kwargs)
        assert response.status_code == 401, (kwargs, response.text)
        assert response.headers.get("www-authenticate") == "Bearer"
    assert _sources(JOBS[route]) == []


@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize("auth_on", [False, True])
def test_correct_bearer_runs_as_cron(client, monkeypatch, request, route, auth_on):
    if auth_on:
        request.getfixturevalue("configured_auth")
    monkeypatch.setenv("CRON_SECRET", SECRET)
    response = client.get(route, headers={"Authorization": f"Bearer {SECRET}"})
    assert response.status_code == 200, response.text
    assert _sources(JOBS[route]) == ["cron"]  # the same trigger split on every route


@pytest.mark.parametrize("route", ROUTES)
def test_deprecated_query_secret_still_runs_as_manual(client, monkeypatch, configured_auth, route):
    monkeypatch.setenv("CRON_SECRET", SECRET)
    response = client.get(route, params={"secret": SECRET})
    assert response.status_code == 200, response.text
    assert _sources(JOBS[route]) == ["manual"]


@pytest.mark.parametrize("route", ROUTES)
def test_query_secret_refused_once_switched_off(client, monkeypatch, configured_auth, route):
    # The one-line removal planned with #199: Bearer keeps working.
    monkeypatch.setattr(cron_auth, "ACCEPT_QUERY_SECRET", False)
    monkeypatch.setenv("CRON_SECRET", SECRET)
    assert client.get(route, params={"secret": SECRET}).status_code == 401
    assert client.get(route, headers={"Authorization": f"Bearer {SECRET}"}).status_code == 200


@pytest.mark.parametrize("route", ROUTES)
def test_local_dev_without_login_or_secret_stays_open(client, monkeypatch, route):
    assert not auth.is_configured()
    monkeypatch.setenv("CRON_SECRET", "")
    response = client.get(route)
    assert response.status_code == 200, response.text
    assert _sources(JOBS[route]) == ["manual"]


def test_cron_routes_still_skip_the_login(client, monkeypatch, configured_auth):
    # The login middleware must not redirect them to /login: they carry no session.
    monkeypatch.setenv("CRON_SECRET", SECRET)
    for route in ROUTES:
        response = client.get(route, follow_redirects=False)
        assert response.status_code == 401, route


def test_trigger_values_match_jobs():
    # The trigger cron_auth returns is passed straight to jobs.run_*(trigger=...).
    assert (cron_auth.CRON, cron_auth.MANUAL) == (jobs.CRON, jobs.MANUAL)
