"""Tests for the in-app Release Notes page (issue #144): the `Release`
model, `/releases` (list + create) and `/releases/{id}/delete`.
"""
import datetime as dt


def test_release_model_round_trips(db_session):
    from models import Release

    release = Release(
        date=dt.date(2026, 9, 1),
        title="Release Notes page",
        body="Added an in-app release notes page.",
        created_at=dt.datetime(2026, 9, 1, 12, 0, 0),
    )
    db_session.add(release)
    db_session.commit()

    fetched = db_session.query(Release).one()
    assert fetched.title == "Release Notes page"
    assert fetched.body == "Added an in-app release notes page."
    assert fetched.date == dt.date(2026, 9, 1)


def test_releases_page_with_no_entries_shows_empty_state(client):
    response = client.get("/releases")

    assert response.status_code == 200
    assert "No release notes recorded yet" in response.text


def test_post_releases_creates_entry_and_redirects(client):
    response = client.post(
        "/releases",
        data={"date": "2026-09-19", "title": "Release Notes page", "body": "Line one.\nLine two."},
    )

    assert response.status_code == 200  # TestClient follows the 303 redirect by default
    assert response.url.path == "/releases"

    import db as db_module
    from models import Release

    db = db_module.SessionLocal()
    try:
        release = db.query(Release).one()
        assert release.title == "Release Notes page"
        assert release.body == "Line one.\nLine two."
        assert release.date == dt.date(2026, 9, 19)
    finally:
        db.close()


def test_releases_page_renders_multiple_entries_newest_first(client):
    client.post("/releases", data={"date": "2026-09-01", "title": "Older entry", "body": "First."})
    client.post("/releases", data={"date": "2026-09-19", "title": "Newer entry", "body": "Second."})

    response = client.get("/releases")

    assert response.status_code == 200
    assert response.text.index("Newer entry") < response.text.index("Older entry")


def test_releases_body_is_autoescaped_with_preserved_line_breaks(client):
    client.post(
        "/releases",
        data={"date": "2026-09-19", "title": "Escaping check", "body": "<script>alert(1)</script>\nsecond line"},
    )

    response = client.get("/releases")

    assert "<script>alert(1)</script>" not in response.text
    assert "&lt;script&gt;" in response.text
    assert "white-space: pre-wrap" in response.text


def test_post_releases_delete_removes_entry(client):
    client.post("/releases", data={"date": "2026-09-19", "title": "To delete", "body": "Body."})

    import db as db_module
    from models import Release

    db = db_module.SessionLocal()
    try:
        release_id = db.query(Release).one().id
    finally:
        db.close()

    response = client.post(f"/releases/{release_id}/delete")

    assert response.status_code == 200
    assert response.url.path == "/releases"

    db = db_module.SessionLocal()
    try:
        assert db.query(Release).count() == 0
    finally:
        db.close()


def test_delete_unknown_release_returns_404(client):
    response = client.post("/releases/999999/delete")

    assert response.status_code == 404


def test_releases_nav_link_present(client):
    response = client.get("/releases")

    assert response.status_code == 200
    assert 'href="/releases"' in response.text
