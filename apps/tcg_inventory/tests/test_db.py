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
