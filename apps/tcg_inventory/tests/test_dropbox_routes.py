import dropbox_client
from conftest import make_csv
from test_dropbox_client import FakeDropbox, FakeListFolderResult, _file_entry


def test_dropbox_list_shows_not_configured_message(client):
    response = client.get("/import/dropbox/list")
    assert response.status_code == 200
    assert "DROPBOX_APP_KEY" in response.text


def test_dropbox_list_shows_files_when_configured(client, monkeypatch):
    fake = FakeDropbox(pages=[FakeListFolderResult([_file_entry("my_collection.csv")])])
    monkeypatch.setattr(dropbox_client, "build_client_from_env", lambda: fake)

    response = client.get("/import/dropbox/list?folder=/Dex Exports")
    assert response.status_code == 200
    assert "my_collection.csv" in response.text


def test_dropbox_sync_downloads_selected_files_and_imports_them(client, monkeypatch):
    csv_bytes = make_csv("My Collection", [{"id": "a", "name": "Pikachu", "qty": 2, "price": "150"}])
    fake = FakeDropbox(download_bytes={"/exports/main.csv": csv_bytes})
    monkeypatch.setattr(dropbox_client, "build_client_from_env", lambda: fake)

    response = client.post(
        "/import/dropbox/sync",
        data={"folder": "/exports", "paths": ["/exports/main.csv"], "full_load": "false"},
    )
    assert response.status_code == 200
    assert "Kort opprettet</td><td>1" in response.text
    assert fake.downloaded_paths == ["/exports/main.csv"]

    inventory = client.get("/inventory")
    assert "Pikachu" in inventory.text


def test_dropbox_sync_without_selection_shows_error(client):
    response = client.post("/import/dropbox/sync", data={"folder": "/exports"})
    assert response.status_code == 200
    assert "Velg minst én fil" in response.text
