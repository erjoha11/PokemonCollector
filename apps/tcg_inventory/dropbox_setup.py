"""One-time setup: obtain a Dropbox refresh token for tcg_inventory.

Run this once after creating a Dropbox app (see README.md "Dropbox import
setup"). It performs the no-redirect OAuth2 flow entirely in the terminal
(no local web server needed): it prints a URL, you approve access in your
browser, then paste the code Dropbox shows back into the terminal.

Usage:
    python dropbox_setup.py
"""
from __future__ import annotations

import requests
from dropbox import DropboxOAuth2FlowNoRedirect


def main() -> None:
    app_key = input("Dropbox App key: ").strip()
    app_secret = input("Dropbox App secret: ").strip()

    flow = DropboxOAuth2FlowNoRedirect(
        app_key,
        consumer_secret=app_secret,
        token_access_type="offline",  # offline = we get a refresh token back
        scope=["files.metadata.read", "files.content.read"],
    )

    authorize_url = flow.start()
    print("\n1. Open this URL in your browser and approve access:")
    print(f"   {authorize_url}")
    print("2. Copy the code Dropbox shows you and paste it below.\n")

    auth_code = input("Authorization code: ").strip()

    try:
        result = flow.finish(auth_code)
    except requests.exceptions.HTTPError as exc:
        # flow.finish() raises via resp.raise_for_status(), which swallows the
        # response body -- that body is exactly what says *why* Dropbox
        # rejected the exchange (wrong App secret, expired/already-used code,
        # App key/secret mismatch, ...), so surface it instead of a bare 400.
        body = exc.response.text if exc.response is not None else "(no response)"
        print(f"\nDropbox rejected the token exchange: {exc}\nResponse from Dropbox: {body}")
        print(
            "\nMost common causes: wrong App key/secret (check against the Dropbox "
            "App Console), or the code expired/was already used -- rerun the "
            "script from scratch (new URL, new code, without delay)."
        )
        raise SystemExit(1) from exc

    print("\nDone. Add this to your .env file (see .env.example):\n")
    print(f"DROPBOX_APP_KEY={app_key}")
    print(f"DROPBOX_APP_SECRET={app_secret}")
    print(f"DROPBOX_REFRESH_TOKEN={result.refresh_token}")
    print("\nThe refresh token never expires -- the app renews the access token automatically.")


if __name__ == "__main__":
    main()
