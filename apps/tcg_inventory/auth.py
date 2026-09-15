"""Supabase Auth integration.

Single-user/personal app -- there's no public signup route on purpose.
Create the user account directly in the Supabase dashboard (Authentication
-> Users -> Add user), then log in here with that email/password.

Auth is enabled whenever SUPABASE_URL (+ the other SUPABASE_* vars) is
configured, regardless of where the app runs. Running `python app.py`
locally without those env vars set stays unauthenticated, same as before --
see the `auth_guard` middleware in app.py.

Access tokens are verified against Supabase's public JWKS endpoint --
Supabase's current default is signing tokens with an asymmetric key
(e.g. ES256), which is exactly what JWKS is for: the public key is fetched
once (Supabase's SDK caches it) and reused, so this still doesn't need a
network round-trip on every request in practice. SUPABASE_JWT_SECRET
(the legacy shared HS256 secret) is optional and only used as a fallback,
for a project that hasn't migrated off it.
"""
from __future__ import annotations

import os

import httpx
import jwt
from jwt import PyJWKClient

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").strip().rstrip("/")
SUPABASE_ANON_KEY = os.environ.get("SUPABASE_ANON_KEY", "").strip()
SUPABASE_JWT_SECRET = os.environ.get("SUPABASE_JWT_SECRET", "").strip()

SESSION_COOKIE = "tcg_session"

_jwks_client: PyJWKClient | None = None


class AuthError(Exception):
    """A user-facing auth failure (bad credentials, expired session, ...)."""


def is_configured() -> bool:
    # SUPABASE_JWT_SECRET is not required -- JWKS verification doesn't need it.
    return bool(SUPABASE_URL and SUPABASE_ANON_KEY)


def login(email: str, password: str) -> dict:
    """Exchange email/password for Supabase tokens via the GoTrue password grant."""
    if not SUPABASE_ANON_KEY.isascii():
        # A real Supabase key/JWT is always plain ASCII. Non-ASCII here means
        # the env var got corrupted somewhere along the way (e.g. invisible
        # characters picked up during copy/paste) -- fail clearly instead of
        # crashing deep inside httpx's header encoding with a raw
        # UnicodeEncodeError and a bare 500.
        raise AuthError(
            "SUPABASE_ANON_KEY contains invalid characters. Re-copy the key "
            "directly from Supabase (Settings -> API -> anon key) and paste it "
            "again in Vercel."
        )
    try:
        response = httpx.post(
            f"{SUPABASE_URL}/auth/v1/token?grant_type=password",
            headers={"apikey": SUPABASE_ANON_KEY, "Content-Type": "application/json"},
            json={"email": email, "password": password},
            timeout=10,
        )
    except httpx.HTTPError as exc:
        raise AuthError(f"Could not reach Supabase: {exc}") from exc

    if response.status_code != 200:
        detail = response.json().get("error_description") or response.json().get("msg") or "Incorrect email or password."
        raise AuthError(detail)
    return response.json()


def _get_jwks_client() -> PyJWKClient:
    global _jwks_client
    if _jwks_client is None:
        _jwks_client = PyJWKClient(f"{SUPABASE_URL}/auth/v1/.well-known/jwks.json")
    return _jwks_client


def verify_access_token(token: str) -> dict:
    """Verify a Supabase-issued access token and return its claims.

    Tries JWKS first (Supabase's current default -- an asymmetric signing
    key such as ES256). Falls back to the legacy shared HS256 secret if
    JWKS verification doesn't apply (e.g. a project still on the legacy
    secret, or the JWKS endpoint being unreachable).
    """
    try:
        signing_key = _get_jwks_client().get_signing_key_from_jwt(token)
    except Exception:
        # No matching key in the JWKS (e.g. a legacy HS256 token, whose
        # secret is never published there -- by design) or the endpoint is
        # unreachable. Either way, fall through to the legacy secret below
        # rather than failing here. Note jwt.PyJWKClientError is itself a
        # PyJWTError subclass, so this can't narrow to PyJWTError alone.
        signing_key = None

    if signing_key is not None:
        try:
            return jwt.decode(
                token,
                signing_key.key,
                algorithms=["ES256", "RS256", "PS256"],
                audience="authenticated",
            )
        except jwt.PyJWTError as exc:
            raise AuthError(str(exc)) from exc

    if not SUPABASE_JWT_SECRET:
        raise AuthError("Could not verify the session (no signing key available).")
    try:
        return jwt.decode(
            token, SUPABASE_JWT_SECRET, algorithms=["HS256"], audience="authenticated"
        )
    except jwt.PyJWTError as exc:
        raise AuthError(str(exc)) from exc
