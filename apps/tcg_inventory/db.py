"""Database engine/session setup for the TCG inventory app.

Local default is a single SQLite file next to this module (no server
setup, `python app.py` just works). Set DATABASE_URL to point at a
Postgres database instead (e.g. a Supabase connection string) for the
Vercel deployment -- see README.md "Deploying to Vercel + Supabase".
"""
import os
from pathlib import Path

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from sqlalchemy.pool import NullPool

APP_DIR = Path(__file__).resolve().parent
DB_PATH = APP_DIR / "tcg_inventory.db"
DEFAULT_SQLITE_URL = f"sqlite:///{DB_PATH}"

DATABASE_URL = os.environ.get("DATABASE_URL", "")

if not DATABASE_URL:
    # No DATABASE_URL -- try local SQLite. Don't rely on a "we're on Vercel"
    # env var to decide whether that's OK (Vercel's Python runtime doesn't
    # reliably expose one) -- instead, actually test whether the path is
    # writable. A read-only host fails right here with a clear, actionable
    # error instead of a cryptic "unable to open database file" three
    # layers deep in SQLAlchemy's connection pool once a request comes in.
    try:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        DB_PATH.touch(exist_ok=True)
        DATABASE_URL = DEFAULT_SQLITE_URL
    except OSError as exc:
        raise RuntimeError(
            "DATABASE_URL is not set, and the local SQLite path isn't writable "
            f"({exc}) -- this looks like a read-only host (e.g. Vercel). Set "
            "DATABASE_URL to a Supabase Postgres connection string in the "
            "project's environment variables (see README.md 'Deploying to "
            "Vercel + Supabase'), then redeploy."
        ) from exc

_engine_kwargs = {"pool_pre_ping": True}
if DATABASE_URL.startswith("sqlite"):
    _engine_kwargs["connect_args"] = {"check_same_thread": False}
else:
    # Postgres: assume a serverless-style caller (fresh process per
    # invocation) unless proven otherwise -- pooled connections would just
    # pile up against Supabase's connection limit instead of being reused.
    # A fresh connection per request, closed after, is the safe default;
    # use Supabase's connection-pooling (pgbouncer) endpoint in
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


def _add_missing_columns():
    """`create_all()` only ever creates whole new tables -- it silently does
    nothing for a column added to a model whose table already exists (e.g.
    on an already-deployed Supabase database). This adds any column that's
    on a model but missing from its live table, so a new nullable column
    never needs a manual `ALTER TABLE` against production. Only ever adds
    columns, never drops/renames/retypes one -- anything beyond that still
    needs a deliberate, reviewed migration.
    """
    inspector = inspect(engine)
    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            if not inspector.has_table(table.name):
                continue  # brand new table -- create_all() above already made it
            existing_columns = {col["name"] for col in inspector.get_columns(table.name)}
            for column in table.columns:
                if column.name in existing_columns:
                    continue
                ddl_type = column.type.compile(dialect=conn.dialect)
                conn.execute(text(f'ALTER TABLE {table.name} ADD COLUMN "{column.name}" {ddl_type}'))


_LEGACY_TRANSACTION_TYPES = {"kjøp": "purchase", "salg": "sale", "bytte": "trade"}


def _normalize_legacy_transaction_types():
    """One-time data cleanup: `transactions.type` used to store Norwegian
    values ("kjøp"/"salg"/"bytte") from before the app's UI was translated
    to English. Idempotent (the WHERE clause only ever matches the old
    values, so this is a no-op on every run after the first) -- safe to
    call unconditionally on every startup, same spirit as
    `_add_missing_columns()` above, just normalizing data instead of schema.
    """
    inspector = inspect(engine)
    if not inspector.has_table("transactions"):
        return  # brand new database -- nothing to normalize
    with engine.begin() as conn:
        for old, new in _LEGACY_TRANSACTION_TYPES.items():
            conn.execute(
                text("UPDATE transactions SET type = :new WHERE type = :old"),
                {"new": new, "old": old},
            )


def _widen_card_snapshot_source_constraint():
    """`card_snapshots` used to be unique on (card_id, date) alone, before
    the `source` column (cron vs manual sync) existed -- see models.py and
    HANDOFF.md. That old constraint would reject a same-day manual snapshot
    once the cron's already run that day, so it needs widening to
    (card_id, date, source). `_add_missing_columns()` above already adds the
    `source` column itself (nullable, no default -- a generic ALTER ADD
    COLUMN); this backfills existing NULL rows (all written before `source`
    existed, so all cron in practice) and swaps the constraint, both
    idempotently, on every startup -- so this ships with no manual migration
    step against production. SQLite (local/tests) always creates the table
    fresh via `create_all()` with the final constraint already in place, so
    this is a no-op there.
    """
    if engine.dialect.name != "postgresql":
        return
    inspector = inspect(engine)
    if not inspector.has_table("card_snapshots"):
        return
    with engine.begin() as conn:
        conn.execute(text("UPDATE card_snapshots SET source = 'cron' WHERE source IS NULL"))
        conn.execute(text("ALTER TABLE card_snapshots ALTER COLUMN source SET DEFAULT 'cron'"))
        conn.execute(text("ALTER TABLE card_snapshots ALTER COLUMN source SET NOT NULL"))
        constraints = {c["name"] for c in inspector.get_unique_constraints("card_snapshots")}
        if "uq_card_snapshots_card_id_date" in constraints:
            conn.execute(text("ALTER TABLE card_snapshots DROP CONSTRAINT uq_card_snapshots_card_id_date"))
        if "uq_card_snapshots_card_id_date_source" not in constraints:
            conn.execute(
                text(
                    "ALTER TABLE card_snapshots ADD CONSTRAINT uq_card_snapshots_card_id_date_source "
                    "UNIQUE (card_id, date, source)"
                )
            )


def init_db():
    import models  # noqa: F401  (registers models on Base.metadata)

    Base.metadata.create_all(bind=engine)
    _add_missing_columns()
    _normalize_legacy_transaction_types()
    _widen_card_snapshot_source_constraint()
