"""The one auth check for every `/cron/*` route (issue #226).

The cron routes skip the Supabase login (a scheduled Vercel call has no
browser session) and are guarded by `CRON_SECRET` instead. Vercel Cron sends
`Authorization: Bearer <CRON_SECRET>` on its own requests when that env var
is set, so the Bearer header is the primary (and eventually only) path.

Fails closed, like the Facebook wins inbox's INBOX_TOKEN:

- `CRON_SECRET` set: only a matching secret is accepted, compared in
  constant time (`hmac.compare_digest`).
- `CRON_SECRET` missing/empty and login configured (`SUPABASE_URL` +
  `SUPABASE_ANON_KEY`, i.e. a deploy): every request is refused (503). A
  deploy that lost the variable must not leave the jobs open to anyone.
- `CRON_SECRET` missing/empty and login not configured (local
  `python app.py`): open, as before, so local dev needs no setup.

`require_cron_secret` is a FastAPI dependency returning the run's trigger:
`"cron"` only for a request carrying the exact Bearer header (the real
scheduled call), else `"manual"`. Each route maps that to its own snapshot
source (`cron`/`price-cron`/`manual`) and records it as the run's
`import_log.source`.
"""

from __future__ import annotations

import hmac
import os

from fastapi import HTTPException, Request

import auth

CRON = "cron"
MANUAL = "manual"

# DEPRECATED: `?secret=<CRON_SECRET>` in the query string. It ends up in
# Vercel's request logs and browser history, so #226 wants it gone, but until
# #199's logged-in "Run now" buttons exist it's the only way to start a job by
# hand from a browser. Still constant-time compared and under the same
# fail-closed rule. To drop it (with #199), set this to False.
ACCEPT_QUERY_SECRET = True


def _configured_secret() -> str:
    return os.environ.get("CRON_SECRET", "").strip()


def _matches(given: str, secret: str) -> bool:
    return hmac.compare_digest(given.encode(), secret.encode())


def _bearer(request: Request) -> str | None:
    scheme, _, given = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "bearer":
        return None
    return given.strip()


def require_cron_secret(request: Request, secret: str = "") -> str:
    """FastAPI dependency: the trigger (`"cron"`/`"manual"`) if the request
    may run a cron job, else raises 401 (missing/wrong secret) or 503 (no
    secret configured on a deploy with login). `secret` is the deprecated
    query parameter, see ACCEPT_QUERY_SECRET."""
    configured = _configured_secret()
    if not configured:
        if auth.is_configured():
            raise HTTPException(
                status_code=503,
                detail="Cron jobs are off: CRON_SECRET isn't set on this server.",
            )
        # Local no-login dev: open. Nothing to tell a scheduled call apart by.
        return MANUAL

    bearer = _bearer(request)
    if bearer is not None and _matches(bearer, configured):
        return CRON
    if ACCEPT_QUERY_SECRET and secret and _matches(secret, configured):
        print(f"[cron-auth] {request.url.path}: deprecated ?secret= used; send Authorization: Bearer instead")
        return MANUAL
    raise HTTPException(
        status_code=401,
        detail="Unauthorized",
        headers={"WWW-Authenticate": "Bearer"},
    )
