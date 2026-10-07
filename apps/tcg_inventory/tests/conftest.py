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
import fx_rates  # noqa: E402
import tcgdex_prices  # noqa: E402
import pokemontcg_client  # noqa: E402

# The USD/NOK rate every test converts at (see fixed_fx_rates below).
TEST_USD_TO_NOK = 10.0
TEST_EUR_TO_NOK = 11.0


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
def no_cron_secret_from_dotenv(monkeypatch):
    """app.py calls load_dotenv(APP_DIR / ".env") at import, and the `client`
    fixture reloads app, so a developer's real CRON_SECRET would otherwise
    leak into every test and turn open /cron/* calls into 401s (issue #217).
    Set to "" rather than deleted: load_dotenv never overrides a key that's
    already present, even when it's empty, and the /cron routes treat ""
    as "no secret configured". Tests that exercise the auth gate setenv
    their own secret.
    """
    monkeypatch.setenv("CRON_SECRET", "")


@pytest.fixture(autouse=True)
def no_inbox_token_from_dotenv(monkeypatch):
    """Same as above for INBOX_TOKEN (POST /inbox/fb-wins, #309): a real one
    in a developer's .env must not leak into the suite. Tests of the token
    gate setenv their own."""
    monkeypatch.setenv("INBOX_TOKEN", "")


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


@pytest.fixture(autouse=True)
def no_tcgdex_pauses(monkeypatch):
    """tcgdex_prices pauses between requests and before a retry (politeness
    towards a free API). The network itself is already off (the httpx.get
    stub above also covers tcgdex_prices); this just makes the pauses
    instant so e.g. the /cron/price-refresh tests don't sit in back-offs."""
    monkeypatch.setattr(tcgdex_prices, "_SLEEP", lambda seconds: None)
    # Same for the pokemontcg.io price pass's client (issue #349).
    monkeypatch.setattr(pokemontcg_client, "_SLEEP", lambda seconds: None)


@pytest.fixture(autouse=True)
def fixed_fx_rates():
    """card_images converts TCGplayer's USD prices at Norges Bank's live rate
    (fx_rates.get_rates). Seed that cache with a fixed rate so the suite
    stays offline and prices are deterministic. fx_rates.httpx.get is the
    same httpx.get that no_card_image_network_calls / test_card_images'
    fakes patch, so without this the FX lookup would hit those fakes.
    test_fx_rates.py calls fx_rates.reset_cache() to test the real lookup
    against its own fakes.
    """
    fx_rates.reset_cache()
    fx_rates.set_rates(
        fx_rates.FxRates(
            rates={"USD": TEST_USD_TO_NOK, "EUR": TEST_EUR_TO_NOK},
            as_of=None,
            source="live",
        ),
        ttl=10**9,
    )
    yield
    fx_rates.reset_cache()


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


def seed_import(client, files):
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
        return import_dex_csv_files(db, payload)
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


def owned_first(rows: list[dict]) -> list[dict]:
    """The same rows with every quantity raised to at least 1.

    Since #340 the importer never creates a card from a quantity-0 My
    Collection row (Dex's "all variants" export lists every unowned variant
    at 0); only a card already in the database can go to 0. A test that
    needs a sold/traded-away (qty 0) card imports `owned_first(rows)` first
    and then `rows`, the same way such a card reaches 0 in real life.
    """
    return [{**row, "qty": row.get("qty", 1) or 1} for row in rows]
