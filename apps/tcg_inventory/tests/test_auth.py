import time

import jwt
import pytest

import auth


def _token(secret="s3cret-enough-for-hs256-tests", **claims):
    payload = {"aud": "authenticated", "sub": "user-1", "exp": int(time.time()) + 3600}
    payload.update(claims)
    return jwt.encode(payload, secret, algorithm="HS256")


def test_is_configured_requires_all_three_vars(monkeypatch):
    monkeypatch.setattr(auth, "SUPABASE_URL", "")
    monkeypatch.setattr(auth, "SUPABASE_ANON_KEY", "")
    monkeypatch.setattr(auth, "SUPABASE_JWT_SECRET", "")
    assert auth.is_configured() is False

    monkeypatch.setattr(auth, "SUPABASE_URL", "https://x.supabase.co")
    monkeypatch.setattr(auth, "SUPABASE_ANON_KEY", "anon")
    monkeypatch.setattr(auth, "SUPABASE_JWT_SECRET", "secret")
    assert auth.is_configured() is True


def test_verify_access_token_accepts_a_validly_signed_token(monkeypatch):
    monkeypatch.setattr(auth, "SUPABASE_JWT_SECRET", "s3cret-enough-for-hs256-tests")
    claims = auth.verify_access_token(_token())
    assert claims["sub"] == "user-1"


def test_verify_access_token_rejects_wrong_secret(monkeypatch):
    monkeypatch.setattr(auth, "SUPABASE_JWT_SECRET", "the-real-secret-value")
    with pytest.raises(auth.AuthError):
        auth.verify_access_token(_token(secret="a-different-secret-value"))


def test_verify_access_token_rejects_expired_token(monkeypatch):
    monkeypatch.setattr(auth, "SUPABASE_JWT_SECRET", "s3cret-enough-for-hs256-tests")
    expired = _token(exp=int(time.time()) - 10)
    with pytest.raises(auth.AuthError):
        auth.verify_access_token(expired)


def test_login_raises_auth_error_on_bad_credentials(monkeypatch):
    monkeypatch.setattr(auth, "SUPABASE_URL", "https://x.supabase.co")
    monkeypatch.setattr(auth, "SUPABASE_ANON_KEY", "anon")

    class FakeResponse:
        status_code = 400

        def json(self):
            return {"error_description": "Invalid login credentials"}

    monkeypatch.setattr(auth.httpx, "post", lambda *a, **k: FakeResponse())

    with pytest.raises(auth.AuthError, match="Invalid login credentials"):
        auth.login("user@example.com", "wrong-password")


def test_login_rejects_non_ascii_anon_key_with_a_clear_error(monkeypatch):
    # A real Supabase anon key/JWT is always plain ASCII. Non-ASCII means the
    # env var got corrupted (e.g. invisible characters from a copy/paste) --
    # this must fail with a clear AuthError, not a raw UnicodeEncodeError
    # from deep inside httpx's header encoding (a real regression once).
    monkeypatch.setattr(auth, "SUPABASE_URL", "https://x.supabase.co")
    monkeypatch.setattr(auth, "SUPABASE_ANON_KEY", "eyJhbGci​OiJIUzI1NiJ9")

    with pytest.raises(auth.AuthError, match="ugyldige tegn"):
        auth.login("user@example.com", "correct-password")


def test_login_returns_tokens_on_success(monkeypatch):
    monkeypatch.setattr(auth, "SUPABASE_URL", "https://x.supabase.co")
    monkeypatch.setattr(auth, "SUPABASE_ANON_KEY", "anon")

    class FakeResponse:
        status_code = 200

        def json(self):
            return {"access_token": "abc", "refresh_token": "def", "expires_in": 3600}

    monkeypatch.setattr(auth.httpx, "post", lambda *a, **k: FakeResponse())

    tokens = auth.login("user@example.com", "correct-password")
    assert tokens["access_token"] == "abc"
