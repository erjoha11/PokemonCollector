import importlib
import sys
from pathlib import Path

import jwt
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx

from db import Base  # noqa: E402
import models  # noqa: E402,F401  (registers tables on Base.metadata)
import db as db_module  # noqa: E402
import auth as auth_module  # noqa: E402
import card_images  # noqa: E402


@pytest.fixture(autouse=True)
def no_jwks_network_calls(monkeypatch):
    """Every test that verifies a token goes through auth.verify_access_token,
    which tries Supabase's JWKS endpoint first. Stub that lookup to fail fast
    (no real network call) so the suite stays offline; tests that want the
    JWKS/ES256 path override auth._get_jwks_client themselves.
    """
    monkeypatch.setattr(auth_module, "_jwks_client", None)

    class NoMatch:
        def get_signing_key_from_jwt(self, token):
            raise jwt.PyJWKClientError("no matching key")

    monkeypatch.setattr(auth_module, "_get_jwks_client", lambda: NoMatch())


@pytest.fixture(autouse=True)
def no_card_image_network_calls(monkeypatch):
    """Every CSV import calls card_images.fetch_card_data for cards missing
    an image or with a stale price (see importer.py), which otherwise hits
    the real Pokemon TCG API. Stubbed at the httpx.get boundary (not
    fetch_card_data itself) so fetch_card_data's own query-building/parsing
    logic still runs -- a simulated connection failure exercises the same
    "lookup didn't work" path a real offline/rate-limited run would.
    test_card_images.py's own tests override httpx.get again with their own
    fakes to test success cases; everything else just gets a fast, offline
    None for both image and price.
    """

    def no_network(*args, **kwargs):
        raise httpx.ConnectError("network disabled in tests")

    monkeypatch.setattr(card_images.httpx, "get", no_network)


@pytest.fixture()
def db_session():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """A TestClient wired to a throwaway SQLite file per test, so tests
    never touch the app's real tcg_inventory.db.
    """
    db_path = tmp_path / "test.db"
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    monkeypatch.setattr(db_module, "engine", engine)
    monkeypatch.setattr(db_module, "SessionLocal", sessionmaker(bind=engine))

    import app as app_module

    importlib.reload(app_module)  # re-bind app's `from db import SessionLocal, init_db`

    with TestClient(app_module.app) as c:
        yield c


def seed_import(client, files, full_load=False):
    """Load Dex CSV fixtures straight into the `client` fixture's test DB.

    There is no manual CSV-upload route in the app (removed -- users never
    did this; see HANDOFF.md) -- the only real sync entry points are the
    daily/manual Dropbox sync and the cron job. Tests still need a fast way
    to seed data without going through either of those, so this calls the
    same import_dex_csv_files() those routes use directly, bypassing HTTP.
    `files` matches the old multipart shape so existing call sites need
    minimal changes: [("files", (filename, csv_bytes, content_type)), ...].
    """
    import db as db_module
    from importer import import_dex_csv_files

    payload = [(filename, data) for _, (filename, data, *_rest) in files]
    db = db_module.SessionLocal()
    try:
        return import_dex_csv_files(db, payload, full_load=full_load)
    finally:
        db.close()


HEADER = (
    "Type;Category;Locale;Series;Set;Id;Number;Name;Variant;Rarity;"
    "Illustrator;Quantity;Price;Note 1;Note 2;Note 3;Note 4;Note 5"
)


def make_csv(category: str, rows: list[dict]) -> bytes:
    """Build a minimal Dex-export CSV for one category from row dicts.

    Each row dict may set any of: id, number, series, set, name, variant,
    rarity, illustrator, qty, price, locale. Missing fields default to
    sensible values so tests only need to specify what they care about.
    """
    lines = [HEADER]
    for i, row in enumerate(rows):
        card_id = row.get("id", f"jpn_test-{i}")
        number = row.get("number", f"{i:03d}/100")
        series = row.get("series", "Test Series")
        set_ = row.get("set", "Test Set")
        name = row.get("name", f"Card {i}")
        variant = row.get("variant", "")
        rarity = row.get("rarity", "Common")
        illustrator = row.get("illustrator", "")
        qty = row.get("qty", 1)
        price = row.get("price", "10.0")
        locale = row.get("locale", "JPN")
        lines.append(
            f"Card;{category};{locale};{series};{set_};{card_id};{number};{name};"
            f"{variant};{rarity};{illustrator};{qty};{price};;;;;"
        )
    return "\n".join(lines).encode("utf-8")
