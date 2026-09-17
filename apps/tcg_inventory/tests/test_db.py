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
