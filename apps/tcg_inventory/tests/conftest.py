import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db import Base  # noqa: E402
import models  # noqa: E402,F401  (registers tables on Base.metadata)


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
