import dropbox_client
from conftest import make_csv, seed_import
from models import CardSnapshot
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


def test_dropbox_file_list_can_be_sorted_by_column(client, monkeypatch):
    fake = FakeDropbox(
        pages=[FakeListFolderResult([_file_entry("zzz.csv"), _file_entry("aaa.csv")])]
    )
    monkeypatch.setattr(dropbox_client, "build_client_from_env", lambda: fake)

    def _table_body(html: str) -> str:
        return html.split("<tbody>", 1)[1].split("</tbody>", 1)[0]

    asc = _table_body(client.get("/import/dropbox/list?dsort=name&ddir=asc").text)
    assert asc.index("aaa.csv") < asc.index("zzz.csv")

    desc = _table_body(client.get("/import/dropbox/list?dsort=name&ddir=desc").text)
    assert desc.index("zzz.csv") < desc.index("aaa.csv")


def test_dropbox_file_list_table_scrolls_instead_of_widening_the_page(client, monkeypatch):
    fake = FakeDropbox(pages=[FakeListFolderResult([_file_entry("my_collection.csv")])])
    monkeypatch.setattr(dropbox_client, "build_client_from_env", lambda: fake)

    response = client.get("/import/dropbox/list?folder=/Dex Exports")
    assert '<div class="table-scroll">' in response.text


def test_dropbox_sync_downloads_selected_files_and_imports_them(client, monkeypatch):
    csv_bytes = make_csv("My Collection", [{"id": "a", "name": "Pikachu", "qty": 2, "price": "150"}])
    fake = FakeDropbox(download_bytes={"/exports/main.csv": csv_bytes})
    monkeypatch.setattr(dropbox_client, "build_client_from_env", lambda: fake)

    response = client.post(
        "/import/dropbox/sync",
        data={"folder": "/exports", "paths": ["/exports/main.csv"]},
    )
    assert response.status_code == 200
    assert "Cards created</td><td>1" in response.text
    assert fake.downloaded_paths == ["/exports/main.csv"]

    inventory = client.get("/inventory")
    assert "Pikachu" in inventory.text


def test_dropbox_sync_without_selection_shows_error(client):
    response = client.post("/import/dropbox/sync", data={"folder": "/exports"})
    assert response.status_code == 200
    assert "Select at least one file" in response.text


def test_cron_sync_requires_secret_when_configured(client, monkeypatch):
    monkeypatch.setenv("CRON_SECRET", "s3cr3t")
    response = client.get("/cron/dropbox-sync")
    assert response.status_code == 401


def test_cron_sync_accepts_correct_secret(client, monkeypatch):
    monkeypatch.setenv("CRON_SECRET", "s3cr3t")
    csv_bytes = make_csv("My Collection", [{"id": "a", "name": "Pikachu", "qty": 2, "price": "150"}])
    fake = FakeDropbox(
        pages=[FakeListFolderResult([_file_entry("main.csv")])],
        download_bytes={"/exports/main.csv": csv_bytes},
    )
    monkeypatch.setattr(dropbox_client, "build_client_from_env", lambda: fake)

    response = client.get(
        "/cron/dropbox-sync", headers={"Authorization": "Bearer s3cr3t"}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["cards_created"] == 1
    assert body["files_synced"] == ["main.csv"]
    assert body["cards_snapshotted"] == 1  # snapshot written right after the sync

    inventory = client.get("/inventory")
    assert "Pikachu" in inventory.text

    import db as db_module

    with db_module.SessionLocal() as db:
        snap = db.query(CardSnapshot).one()
        assert snap.source == "cron"  # real Vercel header -> the scheduled slot


def test_cron_sync_accepts_secret_as_query_param(client, monkeypatch):
    # Fallback for callers that can't set a custom Authorization header.
    monkeypatch.setenv("CRON_SECRET", "s3cr3t")
    csv_bytes = make_csv("My Collection", [{"id": "a", "name": "Pikachu", "qty": 2, "price": "150"}])
    fake = FakeDropbox(
        pages=[FakeListFolderResult([_file_entry("main.csv")])],
        download_bytes={"/exports/main.csv": csv_bytes},
    )
    monkeypatch.setattr(dropbox_client, "build_client_from_env", lambda: fake)

    response = client.get("/cron/dropbox-sync?secret=s3cr3t")
    assert response.status_code == 200
    assert response.json()["cards_created"] == 1

    import db as db_module

    with db_module.SessionLocal() as db:
        snap = db.query(CardSnapshot).one()
        assert snap.source == "manual"  # off-schedule trigger, not the real cron header


def test_cron_sync_scheduled_then_manual_same_day_yields_two_snapshot_rows(client, monkeypatch):
    monkeypatch.setenv("CRON_SECRET", "s3cr3t")
    csv_bytes = make_csv("My Collection", [{"id": "a", "name": "Pikachu", "qty": 2, "price": "150"}])
    fake = FakeDropbox(
        pages=[FakeListFolderResult([_file_entry("main.csv")])],
        download_bytes={"/exports/main.csv": csv_bytes},
    )
    monkeypatch.setattr(dropbox_client, "build_client_from_env", lambda: fake)

    client.get("/cron/dropbox-sync", headers={"Authorization": "Bearer s3cr3t"})
    client.get("/cron/dropbox-sync?secret=s3cr3t")

    import db as db_module

    with db_module.SessionLocal() as db:
        rows = db.query(CardSnapshot).order_by(CardSnapshot.source).all()
        assert [r.source for r in rows] == ["cron", "manual"]


def test_cron_sync_rejects_wrong_query_param_secret(client, monkeypatch):
    monkeypatch.setenv("CRON_SECRET", "s3cr3t")
    response = client.get("/cron/dropbox-sync?secret=wrong")
    assert response.status_code == 401


def test_cron_sync_works_without_secret_configured(client, monkeypatch):
    # No CRON_SECRET env var set at all -- open endpoint (still requires
    # Dropbox to be configured to do anything, but no auth check blocks it).
    monkeypatch.delenv("CRON_SECRET", raising=False)
    fake = FakeDropbox(pages=[FakeListFolderResult([])])
    monkeypatch.setattr(dropbox_client, "build_client_from_env", lambda: fake)

    response = client.get("/cron/dropbox-sync")
    assert response.status_code == 200
    body = response.json()
    assert body["message"] == "No CSV files found"
    assert body["cards_snapshotted"] == 0  # snapshot still runs, just nothing to snapshot yet


def test_cron_sync_reports_dropbox_not_configured(client, monkeypatch):
    # Not the auth check -- isolate from whatever CRON_SECRET the developer's
    # own .env happens to have set (app.py loads it into os.environ on import).
    monkeypatch.delenv("CRON_SECRET", raising=False)
    response = client.get("/cron/dropbox-sync")
    assert response.status_code == 502
    assert "DROPBOX_APP_KEY" in response.text


# --- import circuit breaker (issue #225) ----------------------------------------


def _big_collection(n):
    return make_csv("My Collection", [{"id": f"c{i}"} for i in range(n)])


def test_dropbox_sync_page_has_no_full_load_option(client, monkeypatch):
    fake = FakeDropbox(pages=[FakeListFolderResult([_file_entry("main.csv")])])
    monkeypatch.setattr(dropbox_client, "build_client_from_env", lambda: fake)

    response = client.get("/import/dropbox/list?folder=/exports")
    assert "main.csv" in response.text
    assert "full_load" not in response.text


def test_dropbox_sync_header_only_file_shows_readable_abort(client, monkeypatch):
    fake = FakeDropbox(download_bytes={"/exports/main.csv": make_csv("My Collection", [])})
    monkeypatch.setattr(dropbox_client, "build_client_from_env", lambda: fake)

    response = client.post("/import/dropbox/sync", data={"folder": "/exports", "paths": ["/exports/main.csv"]})

    assert response.status_code == 200
    assert "Sync aborted" in response.text
    assert "no data rows" in response.text
    assert "Sync anyway" not in response.text  # an empty export can't be overridden

    import db as db_module
    from models import ImportLog

    with db_module.SessionLocal() as db:
        assert db.query(ImportLog).count() == 0


def test_dropbox_sync_mass_drop_offers_override(client, monkeypatch):
    seed_import(client, [("files", ("main.csv", _big_collection(400), "text/csv"))])
    fake = FakeDropbox(download_bytes={"/exports/main.csv": _big_collection(300)})
    monkeypatch.setattr(dropbox_client, "build_client_from_env", lambda: fake)

    response = client.post("/import/dropbox/sync", data={"folder": "/exports", "paths": ["/exports/main.csv"]})
    assert "Sync aborted" in response.text
    assert "Sync anyway" in response.text
    assert 'name="allow_mass_missing" value="true"' in response.text
    assert 'name="paths" value="/exports/main.csv"' in response.text

    response = client.post(
        "/import/dropbox/sync",
        data={"folder": "/exports", "paths": ["/exports/main.csv"], "allow_mass_missing": "true"},
    )
    assert "Sync aborted" not in response.text
    assert "<td>100</td>" in response.text  # 100 cards flagged, none deleted


def test_cron_sync_aborts_with_non_ok_status_and_writes_nothing(client, monkeypatch):
    monkeypatch.delenv("CRON_SECRET", raising=False)
    seed_import(client, [("files", ("main.csv", _big_collection(400), "text/csv"))])
    fake = FakeDropbox(
        pages=[FakeListFolderResult([_file_entry("main.csv")])],
        download_bytes={"/exports/main.csv": _big_collection(10)},
    )
    monkeypatch.setattr(dropbox_client, "build_client_from_env", lambda: fake)

    response = client.get("/cron/dropbox-sync")

    assert response.status_code == 409
    body = response.json()
    assert body["status"] == "aborted"
    assert "390 of 400" in body["error"]

    import db as db_module
    from models import Card

    with db_module.SessionLocal() as db:
        assert db.query(Card).filter(Card.flagged_missing_since.isnot(None)).count() == 0
        assert db.query(CardSnapshot).count() == 0
