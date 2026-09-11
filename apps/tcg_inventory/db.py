"""Database engine/session setup for the TCG inventory app.

Single SQLite file, no server setup. The file lives next to this module so
`python app.py` works regardless of the caller's current directory.
"""
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

APP_DIR = Path(__file__).resolve().parent
DB_PATH = APP_DIR / "tcg_inventory.db"
DATABASE_URL = f"sqlite:///{DB_PATH}"

engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
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
