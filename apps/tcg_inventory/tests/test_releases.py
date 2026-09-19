def _create_release(client, date="2026-01-15", title="v1.2 shipped", body="Added the Releases page."):
    return client.post(
        "/releases",
        data={"date": date, "title": title, "body": body},
    )


def _release_id(client):
    import db as db_module
    from models import Release

    db = db_module.SessionLocal()
    try:
        return db.query(Release).one().id
    finally:
        db.close()


def test_releases_page_with_no_entries_shows_empty_state(client):
    response = client.get("/releases")

    assert response.status_code == 200
    assert "No release notes yet" in response.text


def test_create_release_persists_and_shows_on_page(client):
    response = _create_release(client, title="v1.2 shipped", body="Added the Releases page.")

    assert response.status_code == 200  # redirect followed by TestClient

    page = client.get("/releases")
    assert "v1.2 shipped" in page.text
    assert "Added the Releases page." in page.text
    assert "2026-01-15" in page.text


def test_release_body_multiline_is_preserved_and_escaped(client):
    _create_release(client, body="Line one\nLine two <script>alert(1)</script>")

    page = client.get("/releases")

    assert "Line one\nLine two" in page.text
    assert "<script>alert(1)</script>" not in page.text  # must be escaped, not rendered
    assert "&lt;script&gt;" in page.text


def test_releases_listed_newest_first(client):
    _create_release(client, date="2026-01-01", title="older")
    _create_release(client, date="2026-02-01", title="newer")

    page = client.get("/releases")

    assert page.text.index("newer") < page.text.index("older")


def test_delete_release_removes_it(client):
    _create_release(client, title="to be deleted")
    release_id = _release_id(client)

    response = client.post(f"/releases/{release_id}/delete")

    assert response.status_code == 200
    page = client.get("/releases")
    assert "to be deleted" not in page.text
    assert "No release notes yet" in page.text


def test_delete_unknown_release_redirects_without_error(client):
    response = client.post("/releases/999/delete")

    assert response.status_code == 200  # redirect to /releases followed by TestClient
    assert response.url.path == "/releases"


def test_releases_nav_link_present(client):
    response = client.get("/releases")

    assert response.status_code == 200
    assert 'href="/releases"' in response.text
