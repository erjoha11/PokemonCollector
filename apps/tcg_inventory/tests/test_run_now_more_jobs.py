"""Run-now buttons for set sync and image backfill on /sync-status (issue
#402): the same `_run_now` helper as #199's two buttons, calling
jobs.run_set_sync / jobs.run_image_backfill with trigger "manual", behind
the normal login, and every handled outcome a 200 partial."""
import datetime as dt
import time

import jwt
import pytest

import auth
import jobs
import job_locks
import set_sync
from models import Card, ImportLog, JobLock, Set

DEX = "/sync-status/run/dex-sync"
SETS = "/sync-status/run/set-sync"
IMAGES = "/sync-status/run/image-backfill"
JWT_SECRET = "s3cret-enough-for-hs256-tests"


def _session():
    import db as db_module

    return db_module.SessionLocal()


def _logs():
    with _session() as db:
        return [(row.job, row.status, row.source) for row in db.query(ImportLog).order_by(ImportLog.id)]


def _api_set(id_, name, series, release_date, total):
    return {"id": id_, "name": name, "series": series, "releaseDate": release_date, "total": total, "printedTotal": total}


@pytest.fixture()
def offline_sets(monkeypatch):
    """Two sets in the database, the API knowing only Base Set."""
    with _session() as db:
        db.add(Set(series="Original", name="Base Set"))
        db.add(Set(series="Neo", name="Neo Mystery"))
        db.commit()
    monkeypatch.setattr(set_sync, "fetch_api_sets", lambda: [_api_set("base1", "Base Set", "Base", "1999/01/09", 102)])


@pytest.fixture()
def offline_images(monkeypatch):
    """One card without an image; every lookup finds it one."""
    with _session() as db:
        db.add(Card(card_id="1", name="Pikachu", set="Base Set", number="58/102", created_at=dt.datetime.utcnow()))
        db.commit()
    monkeypatch.setattr("backfill_images.card_images.fetch_image_by_card_id", lambda *a: "https://example.com/p.png")


@pytest.fixture()
def configured_auth(monkeypatch):
    monkeypatch.setattr(auth, "SUPABASE_URL", "https://x.supabase.co")
    monkeypatch.setattr(auth, "SUPABASE_ANON_KEY", "anon")
    monkeypatch.setattr(auth, "SUPABASE_JWT_SECRET", JWT_SECRET)


def _log_in(client):
    payload = {"aud": "authenticated", "sub": "user-1", "exp": int(time.time()) + 3600}
    client.cookies.set(auth.SESSION_COOKIE, jwt.encode(payload, JWT_SECRET, algorithm="HS256"))


# --- the page --------------------------------------------------------------


def test_the_card_has_five_buttons_dex_sync_first_and_the_only_primary(client):
    html = client.get("/sync-status").text
    card = html[html.index('id="run-now"'):html.index('id="run-now-result"')]
    order = [
        card.index(f'hx-post="{path}"')
        for path in (DEX, "/sync-status/run/price-refresh", SETS, IMAGES, "/sync-status/run/poketrace-refresh")
    ]
    assert order == sorted(order)
    assert '<button type="submit" id="run-now-dex-sync" class="run-now-button">' in card
    for button_id in ("run-now-price-refresh", "run-now-set-sync", "run-now-image-backfill", "run-now-poketrace-refresh"):
        assert f'id="{button_id}" class="run-now-button secondary"' in card
    assert "Run set sync now" in card and "Run image backfill now" in card


def test_new_forms_share_the_target_lock_error_text_and_refocus(client):
    html = client.get("/sync-status").text
    for path, job in ((SETS, "set-sync"), (IMAGES, "image-backfill")):
        form = html[html.index(f'hx-post="{path}"'):]
        form = form[: form.index("</form>")]
        assert 'hx-target="#run-now-result"' in form and 'hx-swap="innerHTML"' in form
        assert 'hx-disabled-elt=".run-now-button"' in form
        assert f'hx-indicator="#run-now-busy-{job}"' in form
        assert f'data-refocus="run-now-{job}"' in form and f'id="run-now-{job}"' in form
        assert 'data-error-message="Lost contact with the server before the job finished' in form
        assert f'id="run-now-busy-{job}" class="run-now-busy"' in html


def test_set_sync_says_how_long_it_takes(client):
    html = client.get("/sync-status").text
    assert "Set sync running… usually a few seconds, up to about half a minute." in html
    assert "api.pokemontcg.io" in html


def test_routes_are_not_public_and_not_under_cron(client):
    import app as app_module

    for path in (SETS, IMAGES):
        assert path not in app_module._PUBLIC_PATHS
        assert not path.startswith("/cron/")


# --- the same job as the cron, recorded as manual -----------------------------


def test_set_sync_now_runs_the_sync_as_manual(client, offline_sets):
    response = client.post(SETS)

    assert response.status_code == 200
    assert "Set sync done: 1 set matched, 1 unmatched." in response.text
    assert "Neo / Neo Mystery" in response.text
    assert _logs() == [("set-sync", "ok", "manual")]
    assert 'id="sync-glance"' in response.text
    assert response.headers["HX-Trigger"] == "sync-ran"
    with _session() as db:
        assert db.query(Set).filter_by(name="Base Set").one().total_cards == 102


def test_set_sync_now_api_unreachable(client, monkeypatch):
    monkeypatch.setattr(set_sync, "fetch_api_sets", lambda: [])

    response = client.post(SETS)

    assert response.status_code == 200
    assert "couldn't reach api.pokemontcg.io, so nothing was updated" in response.text
    assert _logs() == [("set-sync", "failed", "manual")]


def test_image_backfill_now_runs_the_backfill_as_manual(client, offline_images):
    response = client.post(IMAGES)

    assert response.status_code == 200
    assert "Image backfill done: 1 of 1 card got an image" in response.text
    assert "0 still to look up." in response.text and "run it again" not in response.text
    assert _logs() == [("image-backfill", "ok", "manual")]
    assert response.headers["HX-Trigger"] == "sync-ran"
    with _session() as db:
        assert db.query(Card).one().image_url == "https://example.com/p.png"


def test_image_backfill_now_uses_the_cron_routes_default_limit(client, monkeypatch):
    import inspect

    import app as app_module

    seen = {}

    def fake(db, *, trigger, limit=999):
        seen.update(trigger=trigger, limit=limit)
        return jobs.ImageBackfillResult(status="ok", trigger=trigger, attempted=100, filled=40, cards_with_image=40, remaining=7)

    monkeypatch.setattr(jobs, "run_image_backfill", fake)

    response = client.post(IMAGES)

    cron_default = inspect.signature(app_module.cron_image_backfill).parameters["limit"].default
    assert cron_default == 100
    assert seen == {"trigger": "manual", "limit": cron_default}
    assert "7 still to look up: run it again to continue." in response.text


# --- auth: the login, not CRON_SECRET ---------------------------------------


@pytest.mark.parametrize("path", [SETS, IMAGES])
def test_needs_the_login_when_auth_is_configured(client, monkeypatch, configured_auth, offline_sets, offline_images, path):
    monkeypatch.setenv("CRON_SECRET", "s3cr3t")

    response = client.post(path, headers={"Authorization": "Bearer s3cr3t"}, follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/login"
    assert _logs() == []


@pytest.mark.parametrize("path", [SETS, IMAGES])
def test_needs_no_cron_secret_when_logged_in(client, monkeypatch, configured_auth, offline_sets, offline_images, path):
    monkeypatch.setenv("CRON_SECRET", "s3cr3t")
    _log_in(client)

    response = client.post(path)

    assert response.status_code == 200
    assert [source for _job, _status, source in _logs()] == ["manual"]


# --- every outcome is a partial, never a 500 ---------------------------------


@pytest.mark.parametrize("path, job", [(SETS, "set-sync"), (IMAGES, "image-backfill")])
def test_already_running_renders_a_partial_and_runs_nothing(client, offline_sets, offline_images, path, job):
    with _session() as db:
        job_locks.acquire(db, job, trigger="cron", now=dt.datetime.utcnow() - dt.timedelta(minutes=2))

    response = client.post(path)

    assert response.status_code == 200
    assert "already running" in response.text and "cron" in response.text
    assert '<p class="muted run-now-note">' in response.text
    assert 'class="warnings"' not in response.text
    assert _logs() == []


@pytest.mark.parametrize(
    "path, job, target",
    [
        (SETS, "set-sync", "set_sync.sync_set_metadata"),
        (IMAGES, "image-backfill", "backfill_images.run_backfill"),
    ],
)
def test_unexpected_error_renders_a_partial(client, monkeypatch, path, job, target):
    def boom(*a, **kw):
        raise RuntimeError("api exploded")

    monkeypatch.setattr(f"jobs.{target}", boom)

    response = client.post(path)

    assert response.status_code == 200
    assert "failed with an unexpected error" in response.text
    assert "RuntimeError: api exploded" in response.text
    assert _logs() == [(job, "failed", "manual")]
    with _session() as db:
        assert db.query(JobLock).count() == 0


@pytest.mark.parametrize("path", [SETS, IMAGES])
def test_results_carry_no_role_of_their_own(client, offline_sets, offline_images, path):
    response = client.post(path)

    result = response.text[: response.text.index('id="sync-glance"')]
    assert 'role="' not in result
