"""Tests for db.py's schema-version fast path around init_db()'s migration
chain (see README.md "Database migrations" and issue #97).
"""
import sys
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db as db_module  # noqa: E402


def _fresh_engine():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    return engine


def test_init_db_runs_full_chain_and_stamps_version_on_brand_new_database(monkeypatch):
    engine = _fresh_engine()
    monkeypatch.setattr(db_module, "engine", engine)
    monkeypatch.setattr(db_module, "SessionLocal", sessionmaker(bind=engine))

    db_module.init_db()

    assert db_module._get_schema_version() == db_module.CURRENT_SCHEMA_VERSION
    # The rest of the chain actually ran -- e.g. the cards table now exists.
    with engine.connect() as conn:
        conn.execute(text("SELECT COUNT(*) FROM cards"))


def test_init_db_skips_chain_when_version_already_current(monkeypatch):
    engine = _fresh_engine()
    monkeypatch.setattr(db_module, "engine", engine)
    monkeypatch.setattr(db_module, "SessionLocal", sessionmaker(bind=engine))

    db_module.init_db()  # first run: full chain, stamps the version

    calls = []
    monkeypatch.setattr(
        db_module.Base.metadata,
        "create_all",
        lambda *a, **k: calls.append("create_all"),
    )
    monkeypatch.setattr(
        db_module,
        "_add_missing_columns",
        lambda: calls.append("_add_missing_columns"),
    )
    monkeypatch.setattr(
        db_module,
        "_normalize_legacy_transaction_types",
        lambda: calls.append("_normalize_legacy_transaction_types"),
    )
    monkeypatch.setattr(
        db_module,
        "_widen_card_snapshot_source_constraint",
        lambda: calls.append("_widen_card_snapshot_source_constraint"),
    )
    # _backfill_sets() is deliberately NOT gated behind the version check
    # (see its docstring) -- left unpatched here so the version-gated chain
    # above is the only thing this test asserts short-circuits.

    db_module.init_db()  # second run: should short-circuit on the version check

    assert calls == []


def test_init_db_reruns_chain_when_stored_version_is_behind(monkeypatch):
    engine = _fresh_engine()
    monkeypatch.setattr(db_module, "engine", engine)
    monkeypatch.setattr(db_module, "SessionLocal", sessionmaker(bind=engine))

    db_module.init_db()  # stamps CURRENT_SCHEMA_VERSION
    db_module._set_schema_version(db_module.CURRENT_SCHEMA_VERSION - 1)

    calls = []
    original_create_all = db_module.Base.metadata.create_all

    def spy_create_all(*args, **kwargs):
        calls.append("create_all")
        return original_create_all(*args, **kwargs)

    monkeypatch.setattr(db_module.Base.metadata, "create_all", spy_create_all)

    db_module.init_db()

    assert calls == ["create_all"]
    assert db_module._get_schema_version() == db_module.CURRENT_SCHEMA_VERSION


def test_get_schema_version_returns_none_when_table_missing(monkeypatch):
    engine = _fresh_engine()
    monkeypatch.setattr(db_module, "engine", engine)

    assert db_module._get_schema_version() is None


def _init(monkeypatch, engine):
    monkeypatch.setattr(db_module, "engine", engine)
    monkeypatch.setattr(db_module, "SessionLocal", sessionmaker(bind=engine))
    db_module.init_db()


def test_backfill_sets_creates_set_rows_and_links_cards(monkeypatch):
    import models

    engine = _fresh_engine()
    _init(monkeypatch, engine)

    session = db_module.SessionLocal()
    session.add_all(
        [
            models.Card(card_id="1", variant=None, name="Pikachu", series="Base", set="Base Set"),
            models.Card(card_id="2", variant=None, name="Charizard", series="Base", set="Base Set"),
            models.Card(card_id="3", variant=None, name="Mew", series="Scarlet & Violet", set="151"),
        ]
    )
    session.commit()
    session.close()

    db_module.init_db()  # re-run picks up the newly-added cards, no version bump needed

    session = db_module.SessionLocal()
    sets = {(s.series, s.name): s for s in session.query(models.Set).all()}
    assert set(sets) == {("Base", "Base Set"), ("Scarlet & Violet", "151")}
    for card in session.query(models.Card).all():
        assert card.set_id == sets[(card.series, card.set)].id
    session.close()


def test_backfill_sets_is_idempotent(monkeypatch):
    import models

    engine = _fresh_engine()
    _init(monkeypatch, engine)

    session = db_module.SessionLocal()
    session.add(models.Card(card_id="1", variant=None, name="Pikachu", series="Base", set="Base Set"))
    session.commit()
    session.close()

    db_module.init_db()
    db_module.init_db()
    db_module.init_db()

    session = db_module.SessionLocal()
    assert session.query(models.Set).count() == 1
    session.close()


def test_backfill_sets_carries_over_existing_release_rank(monkeypatch):
    import models

    engine = _fresh_engine()
    _init(monkeypatch, engine)

    session = db_module.SessionLocal()
    session.add(models.SetReleaseOrder(series="Base", set="Base Set", release_rank=1))
    session.add(models.Card(card_id="1", variant=None, name="Pikachu", series="Base", set="Base Set"))
    session.commit()
    session.close()

    db_module.init_db()

    session = db_module.SessionLocal()
    set_row = session.query(models.Set).filter_by(series="Base", name="Base Set").one()
    assert set_row.release_rank == 1
    card = session.query(models.Card).filter_by(card_id="1").one()
    assert card.set_id == set_row.id
    session.close()


def test_backfill_sets_leaves_release_rank_null_when_no_set_release_order_row(monkeypatch):
    import models

    engine = _fresh_engine()
    _init(monkeypatch, engine)

    session = db_module.SessionLocal()
    session.add(models.Card(card_id="1", variant=None, name="Pikachu", series="Base", set="Base Set"))
    session.commit()
    session.close()

    db_module.init_db()

    session = db_module.SessionLocal()
    set_row = session.query(models.Set).filter_by(series="Base", name="Base Set").one()
    assert set_row.release_rank is None
    session.close()


def test_backfill_sets_skips_cards_with_no_series_or_set(monkeypatch):
    import models

    engine = _fresh_engine()
    _init(monkeypatch, engine)

    session = db_module.SessionLocal()
    session.add(models.Card(card_id="1", variant=None, name="Mystery", series=None, set=None))
    session.commit()
    session.close()

    db_module.init_db()

    session = db_module.SessionLocal()
    assert session.query(models.Set).count() == 0
    card = session.query(models.Card).filter_by(card_id="1").one()
    assert card.set_id is None
    session.close()


def test_pin_postgres_driver_names_psycopg2_for_bare_postgres_urls():
    pin = db_module._pin_postgres_driver
    assert pin("postgresql://u:p@host:6543/postgres") == "postgresql+psycopg2://u:p@host:6543/postgres"
    assert pin("postgres://u:p@host/db") == "postgresql+psycopg2://u:p@host/db"
    assert pin("postgresql+psycopg://u@host/db") == "postgresql+psycopg://u@host/db"
    assert pin("sqlite:///x.db") == "sqlite:///x.db"


def test_pinned_postgres_url_resolves_to_psycopg2_dialect():
    from sqlalchemy.engine import make_url

    url = make_url(db_module._pin_postgres_driver("postgresql://u:p@host/db"))
    assert url.get_dialect().driver == "psycopg2"


def _v9_database(monkeypatch, tmp_path, today=None):
    """A database shaped like prod at schema version 9 (before issue #210):
    no card_prices/fx_rates tables, no market_price columns on cards, no
    price_source on card_snapshots -- plus legacy price data to backfill."""
    import datetime as dt

    today = today or dt.date.today()

    def ago(days):
        return (today - dt.timedelta(days=days)).isoformat()

    engine = create_engine(f"sqlite:///{tmp_path / 'v9.db'}")
    monkeypatch.setattr(db_module, "engine", engine)
    monkeypatch.setattr(db_module, "SessionLocal", sessionmaker(bind=engine))
    db_module.Base.metadata.create_all(bind=engine)
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE card_prices"))
        conn.execute(text("DROP TABLE fx_rates"))
        for column in ("market_price", "market_price_source", "market_price_as_of", "price_flags"):
            conn.execute(text(f"ALTER TABLE cards DROP COLUMN {column}"))
        conn.execute(text("ALTER TABLE card_snapshots DROP COLUMN price_source"))
        rows = [
            # id, card_id, reference_price, tcgplayer_price, tcgplayer_price_updated_at, price_lookup_failed_at, flagged_missing_since
            (1, "both", 100.0, 110.0, ago(2), None, None),
            (2, "dex-only", 50.0, None, None, None, None),
            (3, "failed", None, None, None, ago(5), None),
            (4, "nothing", None, None, None, None, None),
            (5, "old-tcg", None, 30.0, ago(60), None, None),
            (6, "missing", 20.0, None, None, None, ago(20)),
        ]
        for r in rows:
            conn.execute(
                text(
                    "INSERT INTO cards (id, card_id, name, qty, reference_price, tcgplayer_price, "
                    "tcgplayer_price_updated_at, price_lookup_failed_at, flagged_missing_since) "
                    "VALUES (:id, :cid, :cid, 1, :ref, :tcg, :tcg_at, :failed, :missing)"
                ),
                dict(zip(("id", "cid", "ref", "tcg", "tcg_at", "failed", "missing"), r)),
            )
        conn.execute(
            text("INSERT INTO import_log (ran_at, source, cards_created, cards_updated, cards_flagged_missing, cards_deleted, warnings_count) VALUES (:ran_at, 'cron', 0, 0, 0, 0, 0)"),
            {"ran_at": f"{ago(1)} 14:43:20.000000"},
        )
        conn.execute(
            text("INSERT INTO card_snapshots (card_id, date, source, qty, reference_price) VALUES (1, :d, 'cron', 1, 110.0)"),
            {"d": ago(1)},
        )
    db_module._set_schema_version(9)
    return engine


def test_migration_from_v9_adds_pricing_schema_and_backfills_every_card(monkeypatch, tmp_path):
    import datetime as dt

    import models

    today = dt.date.today()
    _v9_database(monkeypatch, tmp_path, today)

    def ago(days):
        return today - dt.timedelta(days=days)

    db_module.init_db()

    assert db_module._get_schema_version() == db_module.CURRENT_SCHEMA_VERSION == 10
    session = db_module.SessionLocal()
    prices = {(p.card.card_id, p.source): p for p in session.query(models.CardPrice)}
    assert set(prices) == {
        ("both", "dex"),
        ("both", "pokemontcg"),
        ("dex-only", "dex"),
        ("failed", "pokemontcg"),
        ("old-tcg", "pokemontcg"),
        ("missing", "dex"),
    }
    dex = prices[("both", "dex")]
    assert (dex.price_nok, dex.currency, dex.fx_rate, dex.fetched_at) == (100.0, "NOK", 1.0, ago(1))
    tcg = prices[("both", "pokemontcg")]
    assert (tcg.price, tcg.price_nok, tcg.currency, tcg.fx_rate, tcg.fetched_at) == (None, 110.0, "USD", None, ago(2))
    failed = prices[("failed", "pokemontcg")]
    assert (failed.price_nok, failed.lookup_failed_at) == (None, ago(5))
    assert prices[("missing", "dex")].fetched_at == ago(20)  # last seen in Dex

    cards = {c.card_id: c for c in session.query(models.Card)}
    resolved = {cid: (c.market_price, c.market_price_source, c.price_flags) for cid, c in cards.items()}
    assert resolved == {
        "both": (100.0, "dex", None),  # Dex first in the chain
        "dex-only": (50.0, "dex", None),
        "failed": (None, None, "no_price"),
        "nothing": (None, None, "no_price"),
        "old-tcg": (30.0, "pokemontcg", "stale"),  # kept, never dropped
        "missing": (20.0, "dex", "stale"),
    }
    # Existing snapshot rows keep a NULL source (unknown, pre-#210).
    assert session.query(models.CardSnapshot).one().price_source is None
    session.close()

    # Idempotent: a second start neither duplicates rows nor re-resolves.
    db_module.init_db()
    session = db_module.SessionLocal()
    assert session.query(models.CardPrice).count() == 6
    session.close()


def test_card_price_backfill_failure_is_never_fatal(monkeypatch, tmp_path):
    import pricing

    _v9_database(monkeypatch, tmp_path)

    def boom(session, today=None):
        raise RuntimeError("simulated")

    monkeypatch.setattr(pricing, "backfill_from_legacy", boom)
    db_module.init_db()  # doesn't raise

    assert db_module._get_schema_version() == db_module.CURRENT_SCHEMA_VERSION
