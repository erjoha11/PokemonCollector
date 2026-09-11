"""Supabase Auth integration.

Single-user/personal app -- there's no public signup route on purpose.
Create the user account directly in the Supabase dashboard (Authentication
-> Users -> Add user), then log in here with that email/password.

Auth is enabled whenever SUPABASE_URL (+ the other SUPABASE_* vars) is
configured, regardless of where the app runs. Running `python app.py`
locally without those env vars set stays unauthenticated, same as before --
see the `auth_guard` middleware in app.py.

Access tokens are verified locally against the Supabase project's JWT
secret (HS256) -- no network call needed on every request. This is the
"legacy" HS256 JWT secret (Supabase dashboard -> Settings -> API -> JWT
Secret); if a project has switched to asymmetric JWT signing keys, that
secret is generally still valid for verification, but JWKS-based
verification isn't implemented here.
"""
from __future__ import annotations

import os

import httpx
import jwt

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_ANON_KEY = os.environ.get("SUPABASE_ANON_KEY", "")
SUPABASE_JWT_SECRET = os.environ.get("SUPABASE_JWT_SECRET", "")

SESSION_COOKIE = "tcg_session"


class AuthError(Exception):
    """A user-facing auth failure (bad credentials, expired session, ...)."""


def is_configured() -> bool:
    return bool(SUPABASE_URL and SUPABASE_ANON_KEY and SUPABASE_JWT_SECRET)


def login(email: str, password: str) -> dict:
    """Exchange email/password for Supabase tokens via the GoTrue password grant."""
    try:
        response = httpx.post(
            f"{SUPABASE_URL}/auth/v1/token?grant_type=password",
            headers={"apikey": SUPABASE_ANON_KEY, "Content-Type": "application/json"},
            json={"email": email, "password": password},
            timeout=10,
        )
    except httpx.HTTPError as exc:
        raise AuthError(f"Klarte ikke å nå Supabase: {exc}") from exc

    if response.status_code != 200:
        detail = response.json().get("error_description") or response.json().get("msg") or "Feil e-post eller passord."
        raise AuthError(detail)
    return response.json()


def verify_access_token(token: str) -> dict:
    """Verify a Supabase-issued access token locally and return its claims."""
    try:
        return jwt.decode(
            token, SUPABASE_JWT_SECRET, algorithms=["HS256"], audience="authenticated"
        )
    except jwt.PyJWTError as exc:
        raise AuthError(str(exc)) from exc
