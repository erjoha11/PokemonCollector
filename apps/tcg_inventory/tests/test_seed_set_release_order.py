import importlib

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import db as db_module
import seed_set_release_order
from models import SetReleaseOrder


def _use_temp_db(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'seed_test.db'}")
    monkeypatch.setattr(db_module, "engine", engine)
    monkeypatch.setattr(db_module, "SessionLocal", sessionmaker(bind=engine))
    # seed_set_release_order did `from db import SessionLocal, init_db` at
    # import time -- that snapshot needs re-binding to the patched objects.
    importlib.reload(seed_set_release_order)


def test_seed_loads_all_rows_from_the_bundled_json(tmp_path, monkeypatch):
    _use_temp_db(tmp_path, monkeypatch)
    seed_set_release_order.main()

    db = db_module.SessionLocal()
    try:
        assert db.query(SetReleaseOrder).count() == 100
        base_set = db.query(SetReleaseOrder).filter_by(series="Original", set="Base Set").one()
        assert base_set.release_rank == 1
    finally:
        db.close()


def test_seed_is_idempotent_and_updates_changed_ranks(tmp_path, monkeypatch):
    _use_temp_db(tmp_path, monkeypatch)
    seed_set_release_order.main()

    db = db_module.SessionLocal()
    try:
        base_set = db.query(SetReleaseOrder).filter_by(series="Original", set="Base Set").one()
        base_set.release_rank = 999  # simulate a stale rank
        db.commit()
    finally:
        db.close()

    seed_set_release_order.main()  # re-running should fix it back, not duplicate the row

    db = db_module.SessionLocal()
    try:
        assert db.query(SetReleaseOrder).count() == 100  # no duplicates
        base_set = db.query(SetReleaseOrder).filter_by(series="Original", set="Base Set").one()
        assert base_set.release_rank == 1
    finally:
        db.close()
