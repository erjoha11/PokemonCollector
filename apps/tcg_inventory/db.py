"""Database engine/session setup for the TCG inventory app.

Local default is a single SQLite file next to this module (no server
setup, `python app.py` just works). Set DATABASE_URL to point at a
Postgres database instead (e.g. a Supabase connection string) for the
Vercel deployment -- see README.md "Deploying to Vercel + Supabase".
"""
import os
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from sqlalchemy.pool import NullPool

APP_DIR = Path(__file__).resolve().parent
DB_PATH = APP_DIR / "tcg_inventory.db"
DEFAULT_SQLITE_URL = f"sqlite:///{DB_PATH}"

DATABASE_URL = os.environ.get("DATABASE_URL", DEFAULT_SQLITE_URL)

if os.environ.get("VERCEL") and DATABASE_URL == DEFAULT_SQLITE_URL:
    raise RuntimeError(
        "DATABASE_URL is not set. Vercel's filesystem is read-only/ephemeral, so "
        "SQLite won't work there -- set DATABASE_URL to a Supabase Postgres "
        "connection string (see README.md 'Deploying to Vercel + Supabase')."
    )

_engine_kwargs = {"pool_pre_ping": True}
if DATABASE_URL.startswith("sqlite"):
    _engine_kwargs["connect_args"] = {"check_same_thread": False}
elif os.environ.get("VERCEL"):
    # Serverless: each invocation is a fresh process, so pooled connections
    # just pile up against Postgres/Supabase's connection limit instead of
    # being reused. A fresh connection per request, closed after, is safer
    # here -- use Supabase's connection-pooling (pgbouncer) endpoint in
    # DATABASE_URL too (see README).
    _engine_kwargs["poolclass"] = NullPool

engine = create_engine(DATABASE_URL, **_engine_kwargs)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


class Base(DeclarativeBase):
    pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db():
    import models  # noqa: F401  (registers models on Base.metadata)

    Base.metadata.create_all(bind=engine)
