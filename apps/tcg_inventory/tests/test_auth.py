import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec

import auth


def _hs256_token(secret="s3cret-enough-for-hs256-tests", **claims):
    payload = {"aud": "authenticated", "sub": "user-1", "exp": int(time.time()) + 3600}
    payload.update(claims)
    return jwt.encode(payload, secret, algorithm="HS256")


def test_is_configured_requires_url_and_anon_key_but_not_jwt_secret(monkeypatch):
    monkeypatch.setattr(auth, "SUPABASE_URL", "")
    monkeypatch.setattr(auth, "SUPABASE_ANON_KEY", "")
    monkeypatch.setattr(auth, "SUPABASE_JWT_SECRET", "")
    assert auth.is_configured() is False

    monkeypatch.setattr(auth, "SUPABASE_URL", "https://x.supabase.co")
    monkeypatch.setattr(auth, "SUPABASE_ANON_KEY", "anon")
    assert auth.is_configured() is True  # no JWT secret needed -- JWKS covers it


def test_verify_access_token_accepts_a_validly_signed_hs256_token(monkeypatch):
    # Falls through to the legacy secret because the JWKS fixture above
    # always reports "no matching key" -- exactly what a real project still
    # on the legacy HS256 secret would look like.
    monkeypatch.setattr(auth, "SUPABASE_JWT_SECRET", "s3cret-enough-for-hs256-tests")
    claims = auth.verify_access_token(_hs256_token())
    assert claims["sub"] == "user-1"


def test_verify_access_token_rejects_wrong_hs256_secret(monkeypatch):
    monkeypatch.setattr(auth, "SUPABASE_JWT_SECRET", "the-real-secret-value")
    with pytest.raises(auth.AuthError):
        auth.verify_access_token(_hs256_token(secret="a-different-secret-value"))


def test_verify_access_token_rejects_expired_hs256_token(monkeypatch):
    monkeypatch.setattr(auth, "SUPABASE_JWT_SECRET", "s3cret-enough-for-hs256-tests")
    expired = _hs256_token(exp=int(time.time()) - 10)
    with pytest.raises(auth.AuthError):
        auth.verify_access_token(expired)


def test_verify_access_token_without_jwt_secret_and_no_jwks_match_fails_clearly(monkeypatch):
    monkeypatch.setattr(auth, "SUPABASE_JWT_SECRET", "")
    with pytest.raises(auth.AuthError, match="ingen signeringsnøkkel"):
        auth.verify_access_token(_hs256_token())


def test_verify_access_token_accepts_a_validly_signed_es256_token(monkeypatch):
    # This is what a project that's rotated to Supabase's current default
    # (asymmetric JWT signing keys, e.g. ES256) actually issues -- verified
    # purely via JWKS, with no SUPABASE_JWT_SECRET involved at all.
    monkeypatch.setattr(auth, "SUPABASE_JWT_SECRET", "")
    private_key = ec.generate_private_key(ec.SECP256R1())
    public_key = private_key.public_key()

    class FakeSigningKey:
        key = public_key

    class FakeJwksClient:
        def get_signing_key_from_jwt(self, token):
            return FakeSigningKey()

    monkeypatch.setattr(auth, "_get_jwks_client", lambda: FakeJwksClient())

    payload = {"aud": "authenticated", "sub": "user-es256", "exp": int(time.time()) + 3600}
    token = jwt.encode(payload, private_key, algorithm="ES256")

    claims = auth.verify_access_token(token)
    assert claims["sub"] == "user-es256"


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
