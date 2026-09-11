"""Dropbox integration: list and download Dex CSV exports from a configured
Dropbox folder, so the Import page can pull them directly instead of the
user downloading and re-uploading each file by hand.

Read-only on purpose (files.metadata.read + files.content.read scopes) --
this app never writes to Dropbox.

Functions that talk to the Dropbox API take a `dbx` client as a parameter
(rather than reaching for a global) so they're easy to test against a fake
client. `build_client_from_env()` is the one place that actually
constructs a real client from environment variables -- see .env.example
and dropbox_setup.py for how to obtain the refresh token.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

import dropbox
from dropbox.exceptions import ApiError, AuthError


class DropboxNotConfigured(Exception):
    """Raised when the required DROPBOX_* environment variables are missing."""


class DropboxImportError(Exception):
    """Raised for auth/API failures talking to Dropbox, with a user-facing message."""


@dataclass
class DropboxFile:
    name: str
    path_lower: str
    client_modified: str  # ISO 8601 string, already serializable for templates
    size: int


def build_client_from_env() -> dropbox.Dropbox:
    app_key = os.environ.get("DROPBOX_APP_KEY")
    app_secret = os.environ.get("DROPBOX_APP_SECRET")
    refresh_token = os.environ.get("DROPBOX_REFRESH_TOKEN")
    if not (app_key and app_secret and refresh_token):
        raise DropboxNotConfigured(
            "DROPBOX_APP_KEY, DROPBOX_APP_SECRET, and DROPBOX_REFRESH_TOKEN must all be "
            "set (see .env.example and README.md -- run `python dropbox_setup.py` once)."
        )
    return dropbox.Dropbox(
        app_key=app_key, app_secret=app_secret, oauth2_refresh_token=refresh_token
    )


def default_folder() -> str:
    return os.environ.get("DROPBOX_FOLDER", "")


def list_csv_files(dbx: dropbox.Dropbox, folder: str) -> list[DropboxFile]:
    """List .csv files directly inside `folder` (not recursive), newest first."""
    try:
        result = dbx.files_list_folder(folder)
        entries = list(result.entries)
        while result.has_more:
            result = dbx.files_list_folder_continue(result.cursor)
            entries.extend(result.entries)
    except AuthError as exc:
        raise DropboxImportError(f"Dropbox-autentisering feilet: {exc}") from exc
    except ApiError as exc:
        raise DropboxImportError(f"Fant ikke mappen '{folder}' i Dropbox: {exc}") from exc
    except Exception as exc:  # network errors etc. -- surface as a friendly message
        raise DropboxImportError(f"Klarte ikke å nå Dropbox: {exc}") from exc

    files = [
        DropboxFile(
            name=e.name,
            path_lower=e.path_lower,
            client_modified=e.client_modified.isoformat(),
            size=e.size,
        )
        for e in entries
        if isinstance(e, dropbox.files.FileMetadata) and e.name.lower().endswith(".csv")
    ]
    files.sort(key=lambda f: f.client_modified, reverse=True)
    return files


def download_file(dbx: dropbox.Dropbox, path_lower: str) -> bytes:
    try:
        _metadata, response = dbx.files_download(path_lower)
        return response.content
    except AuthError as exc:
        raise DropboxImportError(f"Dropbox-autentisering feilet: {exc}") from exc
    except ApiError as exc:
        raise DropboxImportError(f"Kunne ikke laste ned '{path_lower}' fra Dropbox: {exc}") from exc
    except Exception as exc:  # network errors etc. -- surface as a friendly message
        raise DropboxImportError(f"Klarte ikke å laste ned '{path_lower}' fra Dropbox: {exc}") from exc
