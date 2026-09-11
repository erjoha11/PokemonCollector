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

from db import Base  # noqa: E402
import models  # noqa: E402,F401  (registers tables on Base.metadata)
import db as db_module  # noqa: E402
import auth as auth_module  # noqa: E402


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


HEADER = (
    "Type;Category;Locale;Series;Set;Id;Number;Name;Variant;Rarity;"
    "Illustrator;Quantity;Price;Note 1;Note 2;Note 3;Note 4;Note 5"
)


def make_csv(category: str, rows: list[dict]) -> bytes:
    """Build a minimal Dex-export CSV for one category from row dicts.

    Each row dict may set any of: id, number, series, set, name, variant,
    rarity, illustrator, qty, price. Missing fields default to sensible
    values so tests only need to specify what they care about.
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
        lines.append(
            f"Card;{category};JPN;{series};{set_};{card_id};{number};{name};"
            f"{variant};{rarity};{illustrator};{qty};{price};;;;;"
        )
    return "\n".join(lines).encode("utf-8")
