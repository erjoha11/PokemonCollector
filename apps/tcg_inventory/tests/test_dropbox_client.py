import datetime as dt

import dropbox.files as dbx_files
import pytest

import dropbox_client as dc


class FakeListFolderResult:
    def __init__(self, entries, has_more=False, cursor=None):
        self.entries = entries
        self.has_more = has_more
        self.cursor = cursor


class FakeDropbox:
    """Minimal stand-in for dropbox.Dropbox exposing only what our code calls."""

    def __init__(self, pages=None, download_bytes=None):
        self._pages = pages or [FakeListFolderResult([])]
        self._download_bytes = download_bytes or {}
        self.downloaded_paths = []

    def files_list_folder(self, folder):
        return self._pages[0]

    def files_list_folder_continue(self, cursor):
        # `cursor` doubles as the index of the next page in these tests.
        return self._pages[cursor]

    def files_download(self, path):
        content = self._download_bytes[path]
        self.downloaded_paths.append(path)
        response = type("Resp", (), {"content": content})()
        metadata = dbx_files.FileMetadata(name=path.rsplit("/", 1)[-1])
        return metadata, response


def _file_entry(name, when=dt.datetime(2026, 1, 1), size=100):
    return dbx_files.FileMetadata(
        name=name, client_modified=when, size=size, path_lower=f"/exports/{name.lower()}"
    )


def test_list_csv_files_filters_to_csv_and_sorts_newest_first():
    entries = [
        _file_entry("old.csv", dt.datetime(2026, 1, 1)),
        _file_entry("new.csv", dt.datetime(2026, 6, 1)),
        _file_entry("readme.txt", dt.datetime(2026, 6, 1)),
        dbx_files.FolderMetadata(name="subfolder"),
    ]
    fake = FakeDropbox(pages=[FakeListFolderResult(entries)])

    files = dc.list_csv_files(fake, "/exports")

    assert [f.name for f in files] == ["new.csv", "old.csv"]


def test_list_csv_files_follows_pagination():
    page1 = FakeListFolderResult([_file_entry("a.csv")], has_more=True, cursor=1)
    page2 = FakeListFolderResult([_file_entry("b.csv")], has_more=False)
    fake = FakeDropbox(pages=[page1, page2])

    files = dc.list_csv_files(fake, "/exports")

    assert {f.name for f in files} == {"a.csv", "b.csv"}


def test_download_file_returns_bytes():
    fake = FakeDropbox(download_bytes={"/exports/a.csv": b"hello"})
    content = dc.download_file(fake, "/exports/a.csv")
    assert content == b"hello"
    assert fake.downloaded_paths == ["/exports/a.csv"]


def test_build_client_from_env_requires_all_three_vars(monkeypatch):
    monkeypatch.delenv("DROPBOX_APP_KEY", raising=False)
    monkeypatch.delenv("DROPBOX_APP_SECRET", raising=False)
    monkeypatch.delenv("DROPBOX_REFRESH_TOKEN", raising=False)
    with pytest.raises(dc.DropboxNotConfigured):
        dc.build_client_from_env()


def test_default_folder_reads_env(monkeypatch):
    monkeypatch.setenv("DROPBOX_FOLDER", "/Dex Exports")
    assert dc.default_folder() == "/Dex Exports"
