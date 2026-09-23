"""Tests for masterdata.py: canonical card identity + external ID mapping."""
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import db as db_module
import masterdata
from conftest import make_csv
from importer import import_dex_csv_files
from models import Card, MasterCard, MasterCardId


@pytest.mark.parametrize(
    "card_id, expected",
    [
        ("base1-26", ("int", "base1", "26")),
        ("sv3pt5-199", ("int", "sv3pt5", "199")),
        ("swshp-SWSH050", ("int", "swshp", "SWSH050")),
        ("jpn_sv2a-168", ("ja", "sv2a", "168")),
        ("scn_csv9-79", ("zh-hans", "csv9", "79")),
        ("xyz_abc-1", ("xyz", "abc", "1")),  # unknown prefix kept, not guessed
        ("not an id", None),
        ("", None),
        (None, None),
    ],
)
def test_parse_dex_card_id(card_id, expected):
    assert masterdata.parse_dex_card_id(card_id) == expected


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("Normal", "normal"),
        ("Holo", "holo"),
        ("Reverse Holo", "reverse_holo"),
        ("Poké Ball Holo", "poke_ball_holo"),
        ("Pokeball Holo", "poke_ball_holo"),
        ("Friend Ball Holo", "friend_ball_holo"),
        ("Cracked Ice Holo", "cracked_ice_holo"),
        ("1st Edition", "first_edition"),
        ("Some New Foil", "some_new_foil"),  # unknown -> stable slug
        ("", "unspecified"),
        (None, "unspecified"),
    ],
)
def test_normalize_variant(raw, expected):
    assert masterdata.normalize_variant(raw) == expected


def _ids(master):
    return {row.source: (row.external_id, row.matched_by) for row in master.external_ids}


def test_import_links_cards_to_master_identity(db_session):
    csv = make_csv(
        "My Collection",
        [
            {"id": "sv2-109", "variant": "Normal", "name": "Pikachu", "number": "109/193"},
            {"id": "sv2-109", "variant": "Reverse Holo", "name": "Pikachu", "number": "109/193"},
            {"id": "jpn_sv2a-1", "variant": "Poké Ball Holo", "name": "Bulbasaur"},
        ],
    )
    import_dex_csv_files(db_session, [("my.csv", csv)])

    masters = {(m.language, m.set_code, m.number, m.variant): m for m in db_session.query(MasterCard).all()}
    assert set(masters) == {
        ("int", "sv2", "109", "normal"),
        ("int", "sv2", "109", "reverse_holo"),
        ("ja", "sv2a", "1", "poke_ball_holo"),
    }
    for card in db_session.query(Card).all():
        assert card.master_card is not None
        assert card.master_card.name == card.name

    normal = masters[("int", "sv2", "109", "normal")]
    assert normal.variant_label == "Normal"
    assert normal.printed_number == "109/193"
    assert _ids(normal) == {
        "dex": ("sv2-109", "exact_id"),
        "pokemontcg": ("sv2-109", "derived"),
    }
    # pokemontcg.io's ID is per print, so both variants share it.
    assert _ids(masters[("int", "sv2", "109", "reverse_holo")])["pokemontcg"] == ("sv2-109", "derived")
    # Japanese prints aren't in pokemontcg.io -- only the Dex ID is known.
    assert _ids(masters[("ja", "sv2a", "1", "poke_ball_holo")]) == {"dex": ("jpn_sv2a-1", "exact_id")}


def test_reimport_does_not_duplicate_masterdata(db_session):
    csv = make_csv("My Collection", [{"id": "sv2-109", "variant": "Normal"}])
    import_dex_csv_files(db_session, [("my.csv", csv)])
    import_dex_csv_files(db_session, [("my.csv", csv)])

    assert db_session.query(MasterCard).count() == 1
    assert db_session.query(MasterCardId).count() == 2


def test_unparseable_card_id_is_left_unlinked(db_session):
    csv = make_csv("My Collection", [{"id": "weird id", "variant": "Normal"}])
    import_dex_csv_files(db_session, [("my.csv", csv)])

    card = db_session.query(Card).one()
    assert card.master_card_id is None
    assert db_session.query(MasterCard).count() == 0


def test_manual_mapping_is_not_overwritten(db_session):
    card = Card(card_id="sv2-109", variant="Normal", name="Pikachu", qty=1)
    db_session.add(card)
    master = masterdata.link_card(db_session, card)
    masterdata.set_external_id(db_session, master, "pokemontcg", "sv2-109a", masterdata.MATCHED_MANUAL)
    db_session.commit()

    masterdata.link_card(db_session, card)  # automatic re-link must keep the manual fix
    masterdata.set_external_id(db_session, master, "tcgplayer", "12345", masterdata.MATCHED_HEURISTIC)
    db_session.commit()

    assert _ids(master)["pokemontcg"] == ("sv2-109a", "manual")
    assert _ids(master)["tcgplayer"] == ("12345", "heuristic")


def test_init_db_backfills_existing_cards(monkeypatch):
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    monkeypatch.setattr(db_module, "engine", engine)
    monkeypatch.setattr(db_module, "SessionLocal", sessionmaker(bind=engine))
    db_module.init_db()

    session = db_module.SessionLocal()
    session.add_all(
        [
            Card(card_id="base1-26", variant="Normal", name="Dratini", qty=1),
            Card(card_id="jpn_m1l-13", variant="Holo", name="Mega Card", qty=1),
            Card(card_id="bad", variant=None, name="Unknown", qty=1),
        ]
    )
    session.commit()
    session.close()

    db_module.init_db()
    db_module.init_db()  # idempotent

    session = db_module.SessionLocal()
    linked = {c.card_id: c.master_card_id for c in session.query(Card).all()}
    assert linked["base1-26"] is not None
    assert linked["jpn_m1l-13"] is not None
    assert linked["bad"] is None
    assert session.query(MasterCard).count() == 2
    session.close()
