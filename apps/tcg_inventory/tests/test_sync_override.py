"""The in-app override for a mass-missing Dex sync abort (issue #275): shown
only after a manual Run-now abort that ImportAborted marks overridable,
confirmed with a required checkbox, capped by the count the user saw
(`expected_missing`, enforced inside the importer before any write), and
unreachable from /cron/* or a connector."""
import ast
import inspect
import pathlib
import time

import jwt
import pytest
from conftest import make_csv, seed_import

import auth
import dropbox_client
import importer
import jobs
from models import Card, ImportLog
from test_dropbox_client import FakeDropbox, FakeListFolderResult, _file_entry

DEX = "/sync-status/run/dex-sync"
OVERRIDE = "/sync-status/run/dex-sync/override"
JWT_SECRET = "s3cr3t-enough-for-hs256-tests"
APP_DIR = pathlib.Path(__file__).resolve().parent.parent


def _session():
    import db as db_module

    return db_module.SessionLocal()


def _logs():
    with _session() as db:
        return [(row.job, row.status, row.source) for row in db.query(ImportLog).order_by(ImportLog.id)]


def _flagged():
    with _session() as db:
        return db.query(Card).filter(Card.flagged_missing_since.isnot(None)).count()


def _collection(n):
    return make_csv("My Collection", [{"id": f"c{i}", "name": f"Card {i:03d}"} for i in range(n)])


def _dropbox_with(monkeypatch, csv_bytes):
    fake = FakeDropbox(
        pages=[FakeListFolderResult([_file_entry("main.csv")])],
        download_bytes={"/exports/main.csv": csv_bytes},
    )
    monkeypatch.setattr(dropbox_client, "build_client_from_env", lambda: fake)


@pytest.fixture()
def seeded(client):
    """400 cards in the database; the tests then serve a shorter export."""
    seed_import(client, [("files", ("main.csv", _collection(400), "text/csv"))])
    return client


def _confirm(client, expected, **extra):
    return client.post(OVERRIDE, data={"expected_missing": str(expected), "confirm": "yes", **extra})


# --- when the override renders ------------------------------------------------


def test_manual_mass_missing_abort_renders_the_override_form(seeded, monkeypatch):
    _dropbox_with(monkeypatch, _collection(10))

    response = seeded.post(DEX)

    assert response.status_code == 200
    html = response.text
    assert "This export would flag <strong>390 of 400 cards</strong> as missing (limit 5%)" in html
    assert "re-export My Collection from Dex and run the sync again" in html
    assert "Nothing is deleted: the cards are only flagged" in html
    assert "Files: main.csv" in html
    assert "First 10 of them" in html and "<li>Card 010</li>" in html
    assert f'hx-post="{OVERRIDE}"' in html
    assert 'hx-target="#run-now-result"' in html and 'hx-swap="innerHTML"' in html
    assert 'name="expected_missing" value="390"' in html
    assert '<input type="checkbox" name="confirm" value="yes" required>' in html
    assert "Yes, I removed about 390 cards in Dex. Flag them as missing." in html
    assert '<button type="submit" class="run-now-button secondary">Run sync anyway</button>' in html
    assert "hx-confirm" not in html
    assert response.headers["HX-Trigger"] == "sync-ran"
    assert _logs()[-1] == ("dex-sync", "aborted", "manual")
    assert _flagged() == 0


def test_empty_my_collection_abort_offers_no_override(client, monkeypatch):
    _dropbox_with(monkeypatch, make_csv("My Collection", []))

    response = client.post(DEX)

    assert "stopped before writing anything" in response.text
    assert OVERRIDE not in response.text and "Run sync anyway" not in response.text


@pytest.mark.parametrize("trigger", [jobs.CRON, jobs.CONNECTOR])
def test_a_non_manual_abort_never_renders_the_override(seeded, monkeypatch, trigger):
    # Only a Run-now answer renders this partial, but the template itself
    # also refuses to show the form for an abort another trigger recorded.
    import app as app_module
    import sync_status

    _dropbox_with(monkeypatch, _collection(10))
    with _session() as db:
        run = jobs.run_dex_sync(db, trigger=trigger)
        assert run.status == "aborted" and run.abort.overridable
        html = app_module.templates.get_template("partials/run_now_result.html").render(
            job="dex-sync",
            run=run,
            error=None,
            oob=True,
            overview=sync_status.overview(db),
            missing=[],
            missing_changed=False,
            job_labels=sync_status.JOB_LABELS,
        )
    assert OVERRIDE not in html and "Run sync anyway" not in html
    assert "stopped before writing anything" in html


# --- the override run ------------------------------------------------------------


def test_confirmed_override_runs_the_sync_as_manual(seeded, monkeypatch):
    _dropbox_with(monkeypatch, _collection(10))
    seeded.post(DEX)

    response = _confirm(seeded, 390)

    assert response.status_code == 200
    assert "Dex sync done" in response.text and "390 flagged missing" in response.text
    assert _logs()[-1] == ("dex-sync", "ok", "manual")
    assert _flagged() == 390
    # #401's answer shape: glance (and Missing from Dex, changed) out-of-band,
    # the run log re-fetching itself on the HX-Trigger.
    assert response.headers["HX-Trigger"] == "sync-ran"
    assert 'id="sync-glance"' in response.text and 'id="missing-cards"' in response.text


def test_override_with_fewer_missing_than_confirmed_runs(seeded, monkeypatch):
    _dropbox_with(monkeypatch, _collection(20))  # 380 missing, confirmed 390

    response = _confirm(seeded, 390)

    assert "Dex sync done" in response.text
    assert _logs()[-1] == ("dex-sync", "ok", "manual")
    assert _flagged() == 380


def test_override_refuses_a_changed_export_and_shows_the_new_count(seeded, monkeypatch):
    _dropbox_with(monkeypatch, _collection(10))
    seeded.post(DEX)  # 390 shown
    _dropbox_with(monkeypatch, _collection(9))  # the export changed: 391

    response = _confirm(seeded, 390)

    assert response.status_code == 200
    html = response.text
    assert "the export changed since you confirmed 390 cards" in html
    assert "<strong>391 of 400 cards</strong>" in html
    assert 'name="expected_missing" value="391"' in html
    assert "Yes, I removed about 391 cards in Dex." in html
    assert _logs()[-1] == ("dex-sync", "aborted", "manual")
    assert _flagged() == 0  # nothing written
    with _session() as db:
        assert db.query(Card).count() == 400

    # Confirming the new count goes through.
    assert "Dex sync done" in _confirm(seeded, 391).text
    assert _flagged() == 391


def test_importer_enforces_expected_missing_before_any_write(db_session):
    importer.import_dex_csv_files(db_session, [("main.csv", _collection(400))])
    db_session.commit()

    with pytest.raises(importer.ImportAborted) as exc:
        importer.import_dex_csv_files(
            db_session, [("main.csv", _collection(10))], allow_mass_missing=True, expected_missing=389
        )
    assert exc.value.overridable
    assert (exc.value.newly_missing, exc.value.expected_missing) == (390, 389)
    db_session.rollback()
    assert db_session.query(Card).filter(Card.flagged_missing_since.isnot(None)).count() == 0

    result = importer.import_dex_csv_files(
        db_session, [("main.csv", _collection(10))], allow_mass_missing=True, expected_missing=390
    )
    assert result.cards_flagged_missing == 390


@pytest.mark.parametrize("data", [{"expected_missing": "390"}, {"expected_missing": "390", "confirm": ""}])
def test_override_refuses_without_the_checkbox(seeded, monkeypatch, data):
    _dropbox_with(monkeypatch, _collection(10))
    before = _logs()

    response = seeded.post(OVERRIDE, data=data)

    assert response.status_code == 422
    assert "Tick the confirmation box" in response.text
    assert _logs() == before and _flagged() == 0


@pytest.mark.parametrize("expected", ["", "abc", "-1", "3.5"])
def test_override_refuses_a_missing_or_invalid_count(seeded, monkeypatch, expected):
    _dropbox_with(monkeypatch, _collection(10))
    before = _logs()

    response = seeded.post(OVERRIDE, data={"expected_missing": expected, "confirm": "yes"})

    assert response.status_code == 422
    assert _logs() == before and _flagged() == 0


def test_override_never_skips_the_empty_my_collection_abort(seeded, monkeypatch):
    _dropbox_with(monkeypatch, make_csv("My Collection", []))

    response = _confirm(seeded, 100000)

    assert "stopped before writing anything" in response.text
    assert "Run sync anyway" not in response.text
    assert _logs()[-1] == ("dex-sync", "aborted", "manual")
    assert _flagged() == 0


# --- the route: login, POST-only, not cron, not public ---------------------------


def test_override_route_is_post_only_not_public_not_under_cron(client):
    import app as app_module

    assert OVERRIDE not in app_module._PUBLIC_PATHS
    assert not OVERRIDE.startswith("/cron/")
    routes = [r for r in app_module.app.routes if getattr(r, "path", None) == OVERRIDE]
    assert [sorted(r.methods) for r in routes] == [["POST"]]
    assert client.get(OVERRIDE).status_code == 405


def test_override_needs_the_login(client, monkeypatch):
    monkeypatch.setattr(auth, "SUPABASE_URL", "https://x.supabase.co")
    monkeypatch.setattr(auth, "SUPABASE_ANON_KEY", "anon")
    monkeypatch.setattr(auth, "SUPABASE_JWT_SECRET", JWT_SECRET)
    monkeypatch.setenv("CRON_SECRET", "s3cr3t")
    _dropbox_with(monkeypatch, _collection(10))

    response = client.post(
        OVERRIDE,
        data={"expected_missing": "390", "confirm": "yes"},
        headers={"Authorization": "Bearer s3cr3t"},
        follow_redirects=False,
    )
    assert response.status_code == 303 and response.headers["location"] == "/login"
    assert _logs() == []

    payload = {"aud": "authenticated", "sub": "user-1", "exp": int(time.time()) + 3600}
    client.cookies.set(auth.SESSION_COOKIE, jwt.encode(payload, JWT_SECRET, algorithm="HS256"))
    assert client.post(OVERRIDE, data={"expected_missing": "390", "confirm": "yes"}).status_code == 200
    assert _logs() == [("dex-sync", "ok", "manual")]


# --- no override from cron or a connector ------------------------------------------


@pytest.mark.parametrize("bearer", [False, True])
def test_cron_route_ignores_override_parameters_and_still_aborts(seeded, monkeypatch, bearer):
    headers = {}
    if bearer:
        monkeypatch.setenv("CRON_SECRET", "s3cr3t")
        headers = {"Authorization": "Bearer s3cr3t"}
    else:
        monkeypatch.delenv("CRON_SECRET", raising=False)
    _dropbox_with(monkeypatch, _collection(10))
    params = {"allow_mass_missing": "true", "expected_missing": "390", "confirm": "yes"}

    response = seeded.get("/cron/dropbox-sync", params=params, headers=headers)

    assert response.status_code == 409 and response.json()["status"] == "aborted"
    assert _flagged() == 0
    # Neither does a POST body sneak it in: the cron route is GET-only.
    assert seeded.post("/cron/dropbox-sync", data=params, headers=headers).status_code == 405
    assert _flagged() == 0


def test_no_cron_route_takes_an_override_parameter():
    import app as app_module

    for route in app_module.app.routes:
        if getattr(route, "path", "").startswith("/cron/"):
            params = inspect.signature(route.endpoint).parameters
            assert "allow_mass_missing" not in params and "expected_missing" not in params, route.path


@pytest.mark.parametrize("trigger", [jobs.CRON, jobs.CONNECTOR])
def test_jobs_refuses_the_override_for_any_trigger_but_manual(seeded, monkeypatch, trigger):
    _dropbox_with(monkeypatch, _collection(10))
    before = _logs()

    with _session() as db, pytest.raises(ValueError, match="in-app only"):
        jobs.run_dex_sync(db, trigger=trigger, allow_mass_missing=True, expected_missing=390)
    assert _logs() == before and _flagged() == 0


def test_jobs_refuses_the_override_without_a_confirmed_count(seeded, monkeypatch):
    _dropbox_with(monkeypatch, _collection(10))

    with _session() as db, pytest.raises(ValueError, match="expected_missing"):
        jobs.run_dex_sync(db, trigger=jobs.MANUAL, allow_mass_missing=True)
    assert _flagged() == 0


def _calls_passing_allow_mass_missing():
    """(module, enclosing function) of every call in the app's own code
    (tests excluded) that passes `allow_mass_missing` as a keyword."""
    found = []
    for path in sorted(APP_DIR.rglob("*.py")):
        if "tests" in path.relative_to(APP_DIR).parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for func in ast.walk(tree):
            if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for node in ast.walk(func):
                if isinstance(node, ast.Call) and any(k.arg == "allow_mass_missing" for k in node.keywords):
                    found.append((path.stem, func.name))
    return sorted(set(found))


def test_only_the_override_route_passes_allow_mass_missing():
    # The override reaches the importer through exactly one path: the in-app
    # route -> jobs.run_dex_sync -> jobs._dex_sync -> import_dex_csv_files.
    # A new cron route, connector tool or /mcp handler passing it would add
    # an entry here and fail this test.
    assert _calls_passing_allow_mass_missing() == [
        ("app", "run_now_dex_sync_override"),
        ("jobs", "_dex_sync"),
    ]
