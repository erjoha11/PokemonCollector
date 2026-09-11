import time

import jwt
import pytest

import auth


@pytest.fixture()
def configured_auth(monkeypatch):
    monkeypatch.setattr(auth, "SUPABASE_URL", "https://x.supabase.co")
    monkeypatch.setattr(auth, "SUPABASE_ANON_KEY", "anon")
    monkeypatch.setattr(auth, "SUPABASE_JWT_SECRET", "s3cret-enough-for-hs256-tests")


def _valid_token():
    payload = {"aud": "authenticated", "sub": "user-1", "exp": int(time.time()) + 3600}
    return jwt.encode(payload, "s3cret-enough-for-hs256-tests", algorithm="HS256")


def test_unauthenticated_request_redirects_to_login(client, configured_auth):
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_login_page_itself_is_public(client, configured_auth):
    response = client.get("/login")
    assert response.status_code == 200


def test_static_files_are_public(client, configured_auth):
    response = client.get("/static/style.css")
    assert response.status_code == 200


def test_valid_session_cookie_grants_access(client, configured_auth):
    client.cookies.set(auth.SESSION_COOKIE, _valid_token())
    response = client.get("/")
    assert response.status_code == 200


def test_login_post_sets_cookie_and_redirects_on_success(client, configured_auth, monkeypatch):
    class FakeResponse:
        status_code = 200

        def json(self):
            return {"access_token": _valid_token(), "refresh_token": "r", "expires_in": 3600}

    monkeypatch.setattr(auth.httpx, "post", lambda *a, **k: FakeResponse())

    response = client.post(
        "/login", data={"email": "erik@erjoha.com", "password": "correct"}, follow_redirects=False
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/"
    assert auth.SESSION_COOKIE in response.cookies


def test_login_post_shows_error_on_bad_credentials(client, configured_auth, monkeypatch):
    class FakeResponse:
        status_code = 400

        def json(self):
            return {"error_description": "Invalid login credentials"}

    monkeypatch.setattr(auth.httpx, "post", lambda *a, **k: FakeResponse())

    response = client.post("/login", data={"email": "erik@erjoha.com", "password": "wrong"})
    assert response.status_code == 200
    assert "Invalid login credentials" in response.text
    assert auth.SESSION_COOKIE not in response.cookies


def test_logout_clears_cookie_and_redirects(client, configured_auth):
    client.cookies.set(auth.SESSION_COOKIE, _valid_token())
    response = client.get("/logout", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_auth_disabled_when_not_configured(client):
    # No `configured_auth` fixture here -- Supabase env vars are unset by
    # default in tests, matching local `python app.py` with no .env.
    response = client.get("/")
    assert response.status_code == 200
