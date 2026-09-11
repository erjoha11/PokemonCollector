"""One-time setup: obtain a Dropbox refresh token for tcg_inventory.

Run this once after creating a Dropbox app (see README.md "Dropbox import
setup"). It performs the no-redirect OAuth2 flow entirely in the terminal
(no local web server needed): it prints a URL, you approve access in your
browser, then paste the code Dropbox shows back into the terminal.

Usage:
    python dropbox_setup.py
"""
from __future__ import annotations

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
    print("\n1. Åpne denne URL-en i nettleseren og godkjenn tilgang:")
    print(f"   {authorize_url}")
    print("2. Kopier koden Dropbox viser deg og lim den inn under.\n")

    auth_code = input("Authorization code: ").strip()

    result = flow.finish(auth_code)

    print("\nFerdig. Legg dette inn i din .env-fil (se .env.example):\n")
    print(f"DROPBOX_APP_KEY={app_key}")
    print(f"DROPBOX_APP_SECRET={app_secret}")
    print(f"DROPBOX_REFRESH_TOKEN={result.refresh_token}")
    print("\nRefresh-tokenet utløper ikke -- appen fornyer tilgangs-token automatisk.")


if __name__ == "__main__":
    main()
