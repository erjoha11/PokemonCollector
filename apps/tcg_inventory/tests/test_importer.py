import datetime as dt

import pytest
from conftest import make_csv

import card_images
import importer
from importer import _parse_number_int, _parse_price, import_dex_csv_files
from models import Card, Collection, ImportLog


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("kr 0,48", 0.48),
        ("kr 783,70", 783.70),
        ("150.5", 150.5),
        ("150,5", 150.5),
        ("kr 1 234,56", 1234.56),  # space as thousands separator
        ("1,234.56", 1234.56),  # US-style thousands
        ("1.234,56", 1234.56),  # European-style thousands
        ("", None),
        (None, None),
        ("kr -", None),
        ("kr —", None),  # em dash, Dex's "no price" placeholder
        ("—", None),
    ],
)
def test_parse_price_handles_dex_currency_formatting(raw, expected):
    assert _parse_price(raw) == expected


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("109/189", 109),
        ("1/108", 1),
        ("SWSH175/307", 175),
        ("63", 63),
        ("", None),
        (None, None),
    ],
)
def test_parse_number_int_extracts_sortable_number(raw, expected):
    assert _parse_number_int(raw) == expected


def test_my_collection_sets_number_int_for_sorting(db_session):
    csv = make_csv("My Collection", [{"id": "a", "number": "9/189"}, {"id": "b", "number": "109/189"}])
    import_dex_csv_files(db_session, [("main.csv", csv)])
    a = db_session.query(Card).filter(Card.card_id == "a").one()
    b = db_session.query(Card).filter(Card.card_id == "b").one()
    assert a.number_int == 9
    assert b.number_int == 109


def test_my_collection_parses_norwegian_kr_price_format(db_session):
    # Real Dex exports look like "kr 0,48", not a plain float -- this was a
    # real bug: reference_price silently ended up None for every card.
    csv = make_csv("My Collection", [{"id": "a", "name": "Shellder", "price": "kr 0,48"}])
    import_dex_csv_files(db_session, [("main.csv", csv)])
    card = db_session.query(Card).filter(Card.card_id == "a").one()
    assert card.reference_price == 0.48


def test_my_collection_imports_utf16le_bom_export(db_session):
    # Dex's in-app CSV export writes UTF-16LE with a BOM, not UTF-8 -- a real
    # export downloaded via the Dropbox API failed to decode until this was
    # handled explicitly.
    csv_utf8 = make_csv("My Collection", [{"id": "a", "name": "Shellder", "price": "kr 0,48"}])
    csv_utf16 = csv_utf8.decode("utf-8").encode("utf-16")  # adds a BOM
    import_dex_csv_files(db_session, [("main.csv", csv_utf16)])
    card = db_session.query(Card).filter(Card.card_id == "a").one()
    assert card.name == "Shellder"
    assert card.reference_price == 0.48


def test_my_collection_fetches_tcgplayer_price_for_a_card_missing_one(db_session, monkeypatch):
    monkeypatch.setattr(
        card_images,
        "fetch_card_data",
        lambda name, set_name, number: card_images.CardApiData(image_url=None, tcgplayer_price=9.99),
    )
    csv = make_csv("My Collection", [{"id": "a", "name": "Shellder", "price": "kr 0,48"}])
    import_dex_csv_files(db_session, [("main.csv", csv)])
    card = db_session.query(Card).filter(Card.card_id == "a").one()
    assert card.tcgplayer_price == 9.99
    assert card.tcgplayer_price_updated_at == dt.date.today()
    # Dex's own Price column is still recorded independently.
    assert card.reference_price == 0.48


def test_my_collection_does_not_refetch_a_fresh_tcgplayer_price(db_session, monkeypatch):
    # image_url is set on the fake response too, so the image side of the
    # lookup also stops asking for more once satisfied -- otherwise a
    # still-missing image would keep triggering the shared API call and mask
    # what this test is actually checking (price staleness).
    calls = []
    monkeypatch.setattr(
        card_images,
        "fetch_card_data",
        lambda name, set_name, number: calls.append(1)
        or card_images.CardApiData("https://example.com/a.png", 5.0),
    )
    csv = make_csv("My Collection", [{"id": "a", "name": "Shellder"}])
    import_dex_csv_files(db_session, [("main.csv", csv)])
    assert len(calls) == 1

    # Re-import the same day: price was just fetched, so it's not stale yet.
    import_dex_csv_files(db_session, [("main.csv", csv)])
    assert len(calls) == 1


def test_my_collection_refetches_a_stale_tcgplayer_price(db_session, monkeypatch):
    card = Card(card_id="a", variant=None, name="Shellder", tcgplayer_price=1.0)
    card.tcgplayer_price_updated_at = dt.date.today() - dt.timedelta(days=importer._PRICE_STALE_AFTER_DAYS + 1)
    db_session.add(card)
    db_session.commit()

    monkeypatch.setattr(
        card_images,
        "fetch_card_data",
        lambda name, set_name, number: card_images.CardApiData(None, 42.0),
    )
    csv = make_csv("My Collection", [{"id": "a", "name": "Shellder"}])
    import_dex_csv_files(db_session, [("main.csv", csv)])

    refreshed = db_session.query(Card).filter(Card.card_id == "a").one()
    assert refreshed.tcgplayer_price == 42.0
    assert refreshed.tcgplayer_price_updated_at == dt.date.today()


def test_my_collection_same_id_different_variant_creates_two_cards(db_session):
    # Real Dex data: the same Id appears once per Variant the user owns
    # (e.g. a card's "Normal" and "Poké Ball Holo" prints are two separate
    # physical cards sharing one Id) -- Id alone used to be the unique key
    # and crashed every import with a duplicate-key error. (Id, Variant) is
    # the real natural key.
    csv = make_csv(
        "My Collection",
        [
            {"id": "jpn_sv2a-42", "name": "Golbat", "variant": "Normal", "qty": 2, "price": "kr 19,02"},
            {"id": "jpn_sv2a-42", "name": "Golbat", "variant": "Poké Ball Holo", "qty": 1, "price": "kr —"},
        ],
    )
    result = import_dex_csv_files(db_session, [("main.csv", csv)])

    assert result.cards_created == 2
    cards = db_session.query(Card).filter(Card.card_id == "jpn_sv2a-42").all()
    assert {c.variant for c in cards} == {"Normal", "Poké Ball Holo"}


def test_missing_detection_is_per_variant(db_session):
    first_sync = make_csv(
        "My Collection",
        [
            {"id": "a", "name": "Golbat", "variant": "Normal", "qty": 1},
            {"id": "a", "name": "Golbat", "variant": "Holo", "qty": 1},
        ],
    )
    import_dex_csv_files(db_session, [("main.csv", first_sync)])

    second_sync = make_csv(
        "My Collection",
        [{"id": "a", "name": "Golbat", "variant": "Normal", "qty": 1}],
    )
    result = import_dex_csv_files(db_session, [("main.csv", second_sync)])

    assert result.cards_flagged_missing == 1
    normal = db_session.query(Card).filter(Card.card_id == "a", Card.variant == "Normal").one()
    holo = db_session.query(Card).filter(Card.card_id == "a", Card.variant == "Holo").one()
    assert normal.flagged_missing_since is None
    assert holo.flagged_missing_since is not None


def test_binder_tag_matches_specific_variant_only(db_session):
    my_collection = make_csv(
        "My Collection",
        [
            {"id": "a", "name": "Golbat", "variant": "Normal", "qty": 1},
            {"id": "a", "name": "Golbat", "variant": "Holo", "qty": 1},
        ],
    )
    binder_csv = make_csv("Illustrator Binder", [{"id": "a", "variant": "Holo", "qty": 1}])

    import_dex_csv_files(db_session, [("main.csv", my_collection), ("binder.csv", binder_csv)])

    normal = db_session.query(Card).filter(Card.card_id == "a", Card.variant == "Normal").one()
    holo = db_session.query(Card).filter(Card.card_id == "a", Card.variant == "Holo").one()
    assert normal.binder_id is None
    assert holo.binder_id is not None


def test_my_collection_creates_cards_with_core_fields(db_session):
    csv = make_csv(
        "My Collection",
        [{"id": "jpn_sv2a-27", "name": "Pikachu", "qty": 2, "price": "150.5"}],
    )
    result = import_dex_csv_files(db_session, [("main.csv", csv)])

    assert result.cards_created == 1
    assert result.cards_updated == 0

    card = db_session.query(Card).filter(Card.card_id == "jpn_sv2a-27").one()
    assert card.name == "Pikachu"
    assert card.qty == 2
    assert card.reference_price == 150.5


def test_my_collection_parses_locale_into_language(db_session):
    csv = make_csv(
        "My Collection",
        [
            {"id": "a", "name": "Pikachu", "locale": "ENG"},
            {"id": "b", "name": "Charizard", "locale": "JPN"},
        ],
    )
    import_dex_csv_files(db_session, [("main.csv", csv)])

    cards = {c.card_id: c for c in db_session.query(Card).all()}
    assert cards["a"].language == "ENG"
    assert cards["b"].language == "JPN"


def test_new_card_gets_created_at_but_existing_card_keeps_its_own(db_session):
    csv = make_csv("My Collection", [{"id": "a", "name": "Pikachu"}])
    import_dex_csv_files(db_session, [("main.csv", csv)])
    card = db_session.query(Card).filter(Card.card_id == "a").one()
    assert card.created_at is not None
    original_created_at = card.created_at

    # Re-importing (an update, not a creation) must never touch created_at.
    csv2 = make_csv("My Collection", [{"id": "a", "name": "Pikachu", "price": "5"}])
    import_dex_csv_files(db_session, [("main.csv", csv2)])
    db_session.refresh(card)
    assert card.created_at == original_created_at


def test_duplicates_total_value_unique_value_are_derived(db_session):
    csv = make_csv("My Collection", [{"id": "a", "qty": 3, "price": "100"}])
    import_dex_csv_files(db_session, [("main.csv", csv)])
    card = db_session.query(Card).filter(Card.card_id == "a").one()

    assert card.duplicates == 2  # max(qty - 1, 0)
    assert card.unique_value == 100.0
    assert card.total_value == 300.0


def test_duplicates_never_negative_for_zero_qty(db_session):
    csv = make_csv("My Collection", [{"id": "a", "qty": 0, "price": "5"}])
    import_dex_csv_files(db_session, [("main.csv", csv)])
    card = db_session.query(Card).filter(Card.card_id == "a").one()
    assert card.duplicates == 0


def test_wishlist_and_151_fullarts_are_fully_ignored(db_session):
    wishlist = make_csv("Wishlist", [{"id": "w1", "name": "Wishlist Card"}])
    fullarts = make_csv("151 Fullarts JPN", [{"id": "f1", "name": "Fullart Card"}])

    result = import_dex_csv_files(db_session, [("w.csv", wishlist), ("f.csv", fullarts)])

    assert db_session.query(Card).count() == 0
    assert db_session.query(Collection).count() == 0
    assert result.warnings == []  # excluded categories never even attempt to match cards


def test_binder_category_routes_to_binder_not_collection(db_session):
    main = make_csv("My Collection", [{"id": "a", "name": "Charizard"}])
    binder_csv = make_csv("Illustrator Binder", [{"id": "a"}])

    import_dex_csv_files(db_session, [("main.csv", main), ("binder.csv", binder_csv)])

    card = db_session.query(Card).filter(Card.card_id == "a").one()
    assert card.binder is not None
    assert card.binder.name == "Illustrator Binder"
    assert card.collections == []


def test_binder_membership_is_replaced_when_category_present_again(db_session):
    main = make_csv("My Collection", [{"id": "a"}, {"id": "b"}])
    import_dex_csv_files(db_session, [("main.csv", main)])

    binder_v1 = make_csv("Tradebinder", [{"id": "a"}, {"id": "b"}])
    import_dex_csv_files(db_session, [("main.csv", main), ("binder.csv", binder_v1)])

    binder_v2 = make_csv("Tradebinder", [{"id": "a"}])  # b no longer in the binder
    import_dex_csv_files(db_session, [("main.csv", main), ("binder.csv", binder_v2)])

    card_a = db_session.query(Card).filter(Card.card_id == "a").one()
    card_b = db_session.query(Card).filter(Card.card_id == "b").one()
    assert card_a.binder.name == "Tradebinder"
    assert card_b.binder is None


def test_auto_binder_rules_assign_illustrator_and_151_not_vintage(db_session):
    main = make_csv(
        "My Collection",
        [{"id": "a"}, {"id": "b"}, {"id": "c"}, {"id": "d"}],
    )
    illustrator = make_csv("Tomokazu Komiya Collection", [{"id": "a"}])
    sv151 = make_csv("Scarlet & Violet: 151 JP/KR", [{"id": "b"}])
    # Vintage Collection has no auto-binder rule -- it isn't a physical binder.
    vintage = make_csv("Vintage Collection", [{"id": "c"}])
    # d belongs to none of the above -- stays without a binder.
    import_dex_csv_files(
        db_session,
        [
            ("main.csv", main),
            ("illustrator.csv", illustrator),
            ("sv151.csv", sv151),
            ("vintage.csv", vintage),
        ],
    )

    def _binder_name(card_id):
        card = db_session.query(Card).filter(Card.card_id == card_id).one()
        return card.binder.name if card.binder else None

    assert _binder_name("a") == "Illustrator Binder"
    assert _binder_name("b") == "151 Binder"
    assert _binder_name("c") is None
    assert _binder_name("d") is None


def test_auto_binder_rules_never_override_an_explicit_binder_tag(db_session):
    # A card already placed in a physical binder via a real Dex Binder-
    # category export (e.g. Tradebinder) keeps that binder even though it
    # also happens to be in an illustrator collection.
    main = make_csv("My Collection", [{"id": "a"}])
    illustrator = make_csv("Tomokazu Komiya Collection", [{"id": "a"}])
    tradebinder = make_csv("Tradebinder", [{"id": "a"}])
    import_dex_csv_files(
        db_session,
        [("main.csv", main), ("illustrator.csv", illustrator), ("binder.csv", tradebinder)],
    )

    card = db_session.query(Card).filter(Card.card_id == "a").one()
    assert card.binder.name == "Tradebinder"


def test_import_writes_a_log_row(db_session):
    csv = make_csv("My Collection", [{"id": "a", "name": "Pikachu", "qty": 2, "price": "150"}])
    import_dex_csv_files(db_session, [("main.csv", csv)], source="dropbox")

    log = db_session.query(ImportLog).one()
    assert log.source == "dropbox"
    assert log.files == "main.csv"
    assert log.cards_created == 1
    assert log.cards_updated == 0
    assert log.cards_flagged_missing == 0
    assert log.warnings_count == 0
    assert log.ran_at is not None


def test_import_source_defaults_to_manual(db_session):
    csv = make_csv("My Collection", [{"id": "a"}])
    import_dex_csv_files(db_session, [("main.csv", csv)])
    log = db_session.query(ImportLog).one()
    assert log.source == "manual"


def test_each_import_call_adds_its_own_log_row(db_session):
    csv = make_csv("My Collection", [{"id": "a"}])
    import_dex_csv_files(db_session, [("main.csv", csv)], source="cron")
    import_dex_csv_files(db_session, [("main.csv", csv)], source="cron")
    assert db_session.query(ImportLog).count() == 2


def test_primary_collection_priority_illustrator_beats_vintage_and_generic(db_session):
    main = make_csv("My Collection", [{"id": "a", "name": "Charizard"}])
    illustrator = make_csv("Tomokazu Komiya Collection", [{"id": "a"}])
    vintage = make_csv("Vintage Collection", [{"id": "a"}])
    generic = make_csv("Collection", [{"id": "a"}])
    sv151 = make_csv("Scarlet & Violet: 151 JP/KR", [{"id": "a"}])

    import_dex_csv_files(
        db_session,
        [
            ("main.csv", main),
            ("illustrator.csv", illustrator),
            ("vintage.csv", vintage),
            ("generic.csv", generic),
            ("sv151.csv", sv151),
        ],
    )

    card = db_session.query(Card).filter(Card.card_id == "a").one()
    assert {c.name for c in card.collections} == {
        "Tomokazu Komiya Collection",
        "Vintage Collection",
        "Collection",
        "Scarlet & Violet: 151 JP/KR",
    }
    assert card.primary_collection.name == "Tomokazu Komiya Collection"


def test_priority_rank_ordering_vintage_then_generic_then_sv151(db_session):
    main = make_csv("My Collection", [{"id": "a"}])
    vintage = make_csv("Vintage Collection", [{"id": "a"}])
    generic = make_csv("Collection", [{"id": "a"}])
    sv151 = make_csv("Scarlet & Violet: 151 JP/KR", [{"id": "a"}])
    import_dex_csv_files(
        db_session,
        [("main.csv", main), ("vintage.csv", vintage), ("generic.csv", generic), ("sv151.csv", sv151)],
    )
    card = db_session.query(Card).filter(Card.card_id == "a").one()
    assert card.primary_collection.name == "Vintage Collection"


def test_unknown_collection_gets_default_lowest_priority(db_session):
    main = make_csv("My Collection", [{"id": "a"}])
    vintage = make_csv("Vintage Collection", [{"id": "a"}])
    mystery = make_csv("Some New Dex Folder", [{"id": "a"}])
    import_dex_csv_files(
        db_session, [("main.csv", main), ("vintage.csv", vintage), ("mystery.csv", mystery)]
    )
    card = db_session.query(Card).filter(Card.card_id == "a").one()
    # Vintage Collection (rank 2) still wins over an unrecognized category (rank 99).
    assert card.primary_collection.name == "Vintage Collection"


def test_vintage_tag_survives_a_sync_without_a_fresh_vintage_export(db_session):
    main = make_csv("My Collection", [{"id": "a"}])
    vintage = make_csv("Vintage Collection", [{"id": "a"}])
    import_dex_csv_files(db_session, [("main.csv", main), ("vintage.csv", vintage)])

    # Second sync: only the main export, no Vintage file this time.
    import_dex_csv_files(db_session, [("main.csv", main)])

    card = db_session.query(Card).filter(Card.card_id == "a").one()
    assert [c.name for c in card.collections] == ["Vintage Collection"]


def test_collection_membership_is_replaced_when_its_category_is_present(db_session):
    main = make_csv("My Collection", [{"id": "a"}, {"id": "b"}])
    vintage_v1 = make_csv("Vintage Collection", [{"id": "a"}, {"id": "b"}])
    import_dex_csv_files(db_session, [("main.csv", main), ("vintage.csv", vintage_v1)])

    vintage_v2 = make_csv("Vintage Collection", [{"id": "a"}])  # b dropped out
    import_dex_csv_files(db_session, [("main.csv", main), ("vintage.csv", vintage_v2)])

    card_a = db_session.query(Card).filter(Card.card_id == "a").one()
    card_b = db_session.query(Card).filter(Card.card_id == "b").one()
    assert [c.name for c in card_a.collections] == ["Vintage Collection"]
    assert card_b.collections == []


def test_normal_sync_flags_missing_card_instead_of_deleting(db_session):
    v1 = make_csv("My Collection", [{"id": "a"}, {"id": "b"}])
    import_dex_csv_files(db_session, [("main.csv", v1)])

    v2 = make_csv("My Collection", [{"id": "a"}])  # b missing this time
    result = import_dex_csv_files(db_session, [("main.csv", v2)], full_load=False)

    assert result.cards_flagged_missing == 1
    assert result.cards_deleted == 0
    assert db_session.query(Card).count() == 2
    card_b = db_session.query(Card).filter(Card.card_id == "b").one()
    assert card_b.flagged_missing_since is not None


def test_flagged_missing_since_does_not_move_on_repeated_misses(db_session):
    v1 = make_csv("My Collection", [{"id": "a"}, {"id": "b"}])
    import_dex_csv_files(db_session, [("main.csv", v1)])

    v2 = make_csv("My Collection", [{"id": "a"}])
    import_dex_csv_files(db_session, [("main.csv", v2)], today=dt.date(2026, 1, 1))
    import_dex_csv_files(db_session, [("main.csv", v2)], today=dt.date(2026, 6, 1))

    card_b = db_session.query(Card).filter(Card.card_id == "b").one()
    assert card_b.flagged_missing_since == dt.date(2026, 1, 1)


def test_card_reappearing_clears_the_missing_flag(db_session):
    v1 = make_csv("My Collection", [{"id": "a"}, {"id": "b"}])
    import_dex_csv_files(db_session, [("main.csv", v1)])
    v2 = make_csv("My Collection", [{"id": "a"}])
    import_dex_csv_files(db_session, [("main.csv", v2)])

    import_dex_csv_files(db_session, [("main.csv", v1)])  # b is back
    card_b = db_session.query(Card).filter(Card.card_id == "b").one()
    assert card_b.flagged_missing_since is None


def test_full_load_deletes_missing_cards(db_session):
    v1 = make_csv("My Collection", [{"id": "a"}, {"id": "b"}])
    import_dex_csv_files(db_session, [("main.csv", v1)])

    v2 = make_csv("My Collection", [{"id": "a"}])
    result = import_dex_csv_files(db_session, [("main.csv", v2)], full_load=True)

    assert result.cards_deleted == 1
    assert db_session.query(Card).count() == 1
    assert db_session.query(Card).filter(Card.card_id == "b").one_or_none() is None


def test_sync_without_my_collection_file_never_flags_or_deletes(db_session):
    main = make_csv("My Collection", [{"id": "a"}, {"id": "b"}])
    import_dex_csv_files(db_session, [("main.csv", main)])

    # Only a collection export this time -- no My Collection file at all.
    vintage = make_csv("Vintage Collection", [{"id": "a"}])
    result = import_dex_csv_files(db_session, [("vintage.csv", vintage)])

    assert result.cards_flagged_missing == 0
    assert result.cards_deleted == 0
    assert db_session.query(Card).count() == 2


def test_collection_row_for_unknown_card_id_produces_a_warning(db_session):
    main = make_csv("My Collection", [{"id": "a"}])
    vintage = make_csv("Vintage Collection", [{"id": "does-not-exist"}])
    result = import_dex_csv_files(db_session, [("main.csv", main), ("vintage.csv", vintage)])

    assert any("does-not-exist" in w for w in result.warnings)
    assert db_session.query(Collection).filter(Collection.name == "Vintage Collection").one().cards == []
