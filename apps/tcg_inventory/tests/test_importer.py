import datetime as dt

import pytest
from conftest import make_csv, owned_first

import card_images
import importer
import pricing
from importer import _parse_number_int, _parse_price, import_dex_csv_files
from sqlalchemy.exc import IntegrityError

from models import Card, CardSnapshot, Collection, ImportLog, Transaction


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


def test_sync_never_looks_up_tcgplayer_prices(db_session, monkeypatch):
    """Issue #349: pokemontcg.io prices come from the daily price cron, by
    ID. The sync writes only the Dex price, and asks for no exchange rate."""
    import fx_rates

    monkeypatch.setattr(card_images, "fetch_card_data", lambda *a, **kw: pytest.fail("price lookup in the sync"))
    monkeypatch.setattr(fx_rates, "get_rates", lambda *a, **kw: pytest.fail("FX lookup in the sync"))
    csv = make_csv("My Collection", [{"id": "sv2-109", "name": "Sudowoodo", "price": "kr 0,48"}])

    import_dex_csv_files(db_session, [("main.csv", csv)])

    card = db_session.query(Card).one()
    assert pricing.get_row(card, pricing.SOURCE_POKEMONTCG) is None
    assert (card.reference_price, card.market_price_source) == (0.48, "dex")


def test_sync_image_lookup_by_id_first_then_search_except_for_japanese(db_session, monkeypatch):
    by_id, searched = [], []
    monkeypatch.setattr(
        card_images,
        "fetch_image_by_card_id",
        lambda card_id, name, number: by_id.append(card_id) or ("https://img/by-id.png" if card_id == "sv2-1" else None),
    )
    monkeypatch.setattr(
        card_images, "search_image_url", lambda name, set_name, number: searched.append(name) or "https://img/search.png"
    )
    csv = make_csv(
        "My Collection",
        [
            {"id": "sv2-1", "name": "By Id"},
            {"id": "sv35-27", "name": "Searched"},
            {"id": "jpn_sv2a-1", "name": "Japanese"},
        ],
    )

    import_dex_csv_files(db_session, [("main.csv", csv)])

    images = {c.name: c.image_url for c in db_session.query(Card)}
    assert images == {"By Id": "https://img/by-id.png", "Searched": "https://img/search.png", "Japanese": None}
    assert by_id == ["sv2-1", "sv35-27", "jpn_sv2a-1"] and searched == ["Searched"]


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
    rows = [{"id": "a", "qty": 0, "price": "5"}]
    import_dex_csv_files(db_session, [("main.csv", make_csv("My Collection", owned_first(rows)))])
    import_dex_csv_files(db_session, [("main.csv", make_csv("My Collection", rows))])
    card = db_session.query(Card).filter(Card.card_id == "a").one()
    assert card.duplicates == 0


def test_wishlist_and_151_fullarts_are_fully_ignored(db_session):
    wishlist = make_csv("Wishlist", [{"id": "w1", "name": "Wishlist Card"}])
    fullarts = make_csv("151 Fullarts JPN", [{"id": "f1", "name": "Fullart Card"}])

    result = import_dex_csv_files(db_session, [("w.csv", wishlist), ("f.csv", fullarts)])

    assert db_session.query(Card).count() == 0
    assert db_session.query(Collection).count() == 0
    assert result.warnings == []  # excluded categories never even attempt to match cards


def test_incoming_is_ignored_like_wishlist(db_session):
    # Dex's "Incoming" folder holds won cards not yet arrived (#311). An
    # Incoming row is skipped the way a Wishlist row is: no collection, no
    # card, no warning -- even for a card that isn't in My Collection.
    incoming = make_csv("Incoming", [{"id": "i1", "name": "Incoming Card", "qty": 1}])
    wishlist = make_csv("Wishlist", [{"id": "w1", "name": "Wishlist Card"}])

    result = import_dex_csv_files(db_session, [("in.csv", incoming), ("w.csv", wishlist)])

    assert db_session.query(Card).count() == 0
    assert db_session.query(Collection).count() == 0
    assert result.warnings == []
    assert result.collections_touched == set()


def test_card_in_incoming_and_my_collection_imports_normally(db_session):
    main = make_csv("My Collection", [{"id": "a", "name": "Charizard", "qty": 2, "price": "100"}])
    incoming = make_csv("Incoming", [{"id": "a", "name": "Charizard", "qty": 2}])
    vintage = make_csv("Vintage Collection", [{"id": "a"}])

    result = import_dex_csv_files(
        db_session, [("main.csv", main), ("in.csv", incoming), ("v.csv", vintage)]
    )

    card = db_session.query(Card).filter(Card.card_id == "a").one()
    assert card.qty == 2
    assert card.flagged_missing_since is None
    assert [c.name for c in card.collections] == ["Vintage Collection"]
    assert card.primary_collection.name == "Vintage Collection"
    assert card.binder is None
    assert db_session.query(Collection).filter(Collection.name == "Incoming").count() == 0
    assert result.warnings == []


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
    result = import_dex_csv_files(db_session, [("main.csv", v2)])

    assert result.cards_flagged_missing == 1
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


def test_full_load_parameter_is_gone(db_session):
    # Removed deliberately in #225 -- no caller can ask the importer to
    # hard-delete cards any more.
    with pytest.raises(TypeError):
        import_dex_csv_files(db_session, [], full_load=True)


def _seed_card_with_history(db_session, ids=("a", "b")):
    import_dex_csv_files(db_session, [("main.csv", make_csv("My Collection", [{"id": i} for i in ids]))])
    card_b = db_session.query(Card).filter(Card.card_id == "b").one()
    db_session.add_all(
        [
            Transaction(card_id=card_b.id, type="purchase", date=dt.date(2026, 1, 1), price=100.0),
            Transaction(card_id=card_b.id, type="sale", date=dt.date(2026, 2, 1), price=150.0),
            CardSnapshot(card_id=card_b.id, date=dt.date(2026, 1, 2), source="cron", qty=1, reference_price=10.0),
        ]
    )
    db_session.commit()
    return card_b.id


def test_card_missing_from_export_keeps_its_transactions_and_snapshots(db_session):
    # The #225 regression: a missing card used to be deletable (full load),
    # cascading to its transactions and snapshots. Asserted explicitly --
    # SQLite doesn't enforce the FK cascade the way prod Postgres does.
    card_b_id = _seed_card_with_history(db_session)

    result = import_dex_csv_files(db_session, [("main.csv", make_csv("My Collection", [{"id": "a"}]))])

    assert result.cards_flagged_missing == 1
    card_b = db_session.get(Card, card_b_id)
    assert card_b is not None and card_b.flagged_missing_since is not None
    assert sorted(t.type for t in db_session.query(Transaction).filter(Transaction.card_id == card_b_id)) == [
        "purchase",
        "sale",
    ]
    assert db_session.query(CardSnapshot).filter(CardSnapshot.card_id == card_b_id).count() == 1


def test_orm_refuses_to_delete_a_card_with_transactions(db_session):
    # No delete cascade on Card.transactions any more: the ORM tries to null
    # the NOT NULL card_id and the flush fails, instead of silently deleting
    # the money history along with the card.
    card_b_id = _seed_card_with_history(db_session)

    db_session.delete(db_session.get(Card, card_b_id))
    with pytest.raises(IntegrityError):
        db_session.flush()
    db_session.rollback()

    assert db_session.get(Card, card_b_id) is not None
    assert db_session.query(Transaction).filter(Transaction.card_id == card_b_id).count() == 2


def _snapshot_state(db_session):
    return (
        sorted((c.card_id, c.flagged_missing_since, c.qty) for c in db_session.query(Card)),
        db_session.query(ImportLog).count(),
    )


def test_header_only_my_collection_aborts_with_no_changes(db_session):
    _seed_card_with_history(db_session)
    before = _snapshot_state(db_session)

    header_only = make_csv("My Collection", [])
    with pytest.raises(importer.ImportAborted) as exc:
        import_dex_csv_files(db_session, [("My Collection.csv", header_only)])

    assert "no data rows" in str(exc.value)
    assert not exc.value.overridable
    db_session.rollback()
    assert _snapshot_state(db_session) == before


def test_empty_file_alongside_other_categories_aborts_when_my_collection_has_no_rows(db_session):
    _seed_card_with_history(db_session)
    before = _snapshot_state(db_session)

    with pytest.raises(importer.ImportAborted):
        import_dex_csv_files(
            db_session,
            [("main.csv", b""), ("vintage.csv", make_csv("Vintage Collection", [{"id": "a"}]))],
        )
    db_session.rollback()
    assert _snapshot_state(db_session) == before


def test_empty_side_file_is_fine_when_my_collection_has_rows(db_session):
    main = make_csv("My Collection", [{"id": "a"}])
    result = import_dex_csv_files(db_session, [("main.csv", main), ("wishlist.csv", make_csv("Wishlist", []))])
    assert result.cards_created == 1


def _collection(n):
    return make_csv("My Collection", [{"id": f"c{i}"} for i in range(n)])


def test_mass_drop_above_threshold_aborts_with_no_changes(db_session):
    import_dex_csv_files(db_session, [("main.csv", _collection(400))])
    before = _snapshot_state(db_session)

    # A truncated export: 400 -> 300 would flag 100 cards (25%).
    with pytest.raises(importer.ImportAborted) as exc:
        import_dex_csv_files(db_session, [("main.csv", _collection(300))])

    assert "100 of 400" in str(exc.value)
    assert exc.value.overridable
    db_session.rollback()
    assert _snapshot_state(db_session) == before


def test_mass_drop_can_be_overridden_and_only_flags(db_session):
    import_dex_csv_files(db_session, [("main.csv", _collection(400))])

    result = import_dex_csv_files(db_session, [("main.csv", _collection(300))], allow_mass_missing=True)

    assert result.cards_flagged_missing == 100
    assert db_session.query(Card).count() == 400


def test_small_drop_below_threshold_still_flags_normally(db_session):
    import_dex_csv_files(db_session, [("main.csv", _collection(400))])

    # 20 of 400 = 5%, exactly at the limit (limit is "more than").
    result = import_dex_csv_files(db_session, [("main.csv", _collection(380))])

    assert result.cards_flagged_missing == 20
    assert db_session.query(Card).count() == 400


def test_already_flagged_cards_dont_count_toward_the_threshold(db_session):
    import_dex_csv_files(db_session, [("main.csv", _collection(400))])
    import_dex_csv_files(db_session, [("main.csv", _collection(380))])  # 20 flagged
    import_dex_csv_files(db_session, [("main.csv", _collection(360))])  # 20 more, still fine

    assert db_session.query(Card).filter(Card.flagged_missing_since.isnot(None)).count() == 40


def test_sync_without_my_collection_file_never_flags_or_deletes(db_session):
    main = make_csv("My Collection", [{"id": "a"}, {"id": "b"}])
    import_dex_csv_files(db_session, [("main.csv", main)])

    # Only a collection export this time -- no My Collection file at all.
    vintage = make_csv("Vintage Collection", [{"id": "a"}])
    result = import_dex_csv_files(db_session, [("vintage.csv", vintage)])

    assert result.cards_flagged_missing == 0
    assert db_session.query(Card).count() == 2


def test_collection_row_for_unknown_card_id_produces_a_warning(db_session):
    main = make_csv("My Collection", [{"id": "a"}])
    vintage = make_csv("Vintage Collection", [{"id": "does-not-exist"}])
    result = import_dex_csv_files(db_session, [("main.csv", main), ("vintage.csv", vintage)])

    assert any("does-not-exist" in w for w in result.warnings)
    assert db_session.query(Collection).filter(Collection.name == "Vintage Collection").one().cards == []


def test_import_links_a_new_card_to_a_newly_created_unranked_set(db_session):
    # Issue #134: importer.py links Card.set_id inline, at import time,
    # rather than waiting for db.py's init_db()-time _backfill_sets() to
    # catch up on the next restart.
    csv = make_csv("My Collection", [{"id": "a", "series": "Scarlet & Violet", "set": "Obsidian Flames"}])
    import_dex_csv_files(db_session, [("main.csv", csv)])

    card = db_session.query(Card).filter(Card.card_id == "a").one()
    assert card.set_id is not None
    assert card.linked_set.series == "Scarlet & Violet"
    assert card.linked_set.name == "Obsidian Flames"
    # Never guessed -- a set seen for the first time via import gets an
    # unranked row, not a fabricated release_rank.
    assert card.linked_set.release_rank is None


def test_import_reuses_an_existing_set_row_instead_of_duplicating_it(db_session):
    from models import Set

    existing = Set(series="Scarlet & Violet", name="Obsidian Flames", release_rank=5)
    db_session.add(existing)
    db_session.commit()

    csv = make_csv("My Collection", [{"id": "a", "series": "Scarlet & Violet", "set": "Obsidian Flames"}])
    import_dex_csv_files(db_session, [("main.csv", csv)])

    card = db_session.query(Card).filter(Card.card_id == "a").one()
    assert card.set_id == existing.id
    # Reused, not overwritten -- a pre-existing researched rank survives.
    assert card.linked_set.release_rank == 5
    assert db_session.query(Set).filter(
        Set.series == "Scarlet & Violet", Set.name == "Obsidian Flames"
    ).count() == 1


def test_full_sync_leaves_no_unlinked_cards_without_a_subsequent_init_db_call(db_session):
    csv = make_csv(
        "My Collection",
        [
            {"id": "a", "series": "Scarlet & Violet", "set": "Obsidian Flames"},
            {"id": "b", "series": "Scarlet & Violet", "set": "Obsidian Flames"},
            {"id": "c", "series": "Sword & Shield", "set": "Vivid Voltage"},
        ],
    )
    import_dex_csv_files(db_session, [("main.csv", csv)])

    cards = db_session.query(Card).all()
    assert len(cards) == 3
    assert all(card.set_id is not None for card in cards)


def test_import_updates_set_id_when_an_existing_card_moves_sets(db_session):
    # A Dex re-categorization (or correction) of an existing card's set
    # should re-link it, not leave it pointing at its old Set row.
    csv1 = make_csv("My Collection", [{"id": "a", "series": "Scarlet & Violet", "set": "Obsidian Flames"}])
    import_dex_csv_files(db_session, [("main.csv", csv1)])
    first_set_id = db_session.query(Card).filter(Card.card_id == "a").one().set_id

    csv2 = make_csv("My Collection", [{"id": "a", "series": "Sword & Shield", "set": "Vivid Voltage"}])
    import_dex_csv_files(db_session, [("main.csv", csv2)])

    card = db_session.query(Card).filter(Card.card_id == "a").one()
    assert card.set_id != first_set_id
    assert card.linked_set.name == "Vivid Voltage"


# --- Quantity-0 rows from Dex's "all variants" export (issue #340) ---


def test_qty_zero_my_collection_row_for_unknown_card_creates_nothing(db_session):
    csv = make_csv(
        "My Collection",
        [
            {"id": "owned", "variant": "Normal", "qty": 1},
            {"id": "owned", "variant": "Reverse Holo", "qty": 0},
            {"id": "unowned", "qty": 0},
        ],
    )
    result = import_dex_csv_files(db_session, [("main.csv", csv)])

    cards = {(c.card_id, c.variant) for c in db_session.query(Card)}
    assert cards == {("owned", "Normal")}
    assert result.cards_created == 1
    assert result.unowned_rows_skipped == 2
    assert result.warnings == []
    log = db_session.query(ImportLog).one()
    assert log.warnings_count == 0
    assert "Skipped 2 rows with quantity 0" in log.message


def test_existing_card_going_to_qty_zero_is_still_updated(db_session):
    first = make_csv("My Collection", [{"id": "a", "qty": 2, "price": "5.0"}])
    import_dex_csv_files(db_session, [("main.csv", first)])

    second = make_csv("My Collection", [{"id": "a", "qty": 0, "price": "7.0"}])
    result = import_dex_csv_files(db_session, [("main.csv", second)])

    card = db_session.query(Card).filter(Card.card_id == "a").one()
    assert card.qty == 0
    assert card.reference_price == 7.0
    assert card.flagged_missing_since is None  # seen, so not flagged missing
    assert result.cards_updated == 1
    assert result.unowned_rows_skipped == 0


def test_flagged_card_reappearing_at_qty_zero_is_unflagged_as_before(db_session):
    import_dex_csv_files(db_session, [("main.csv", make_csv("My Collection", [{"id": "a"}, {"id": "b"}]))])
    import_dex_csv_files(db_session, [("main.csv", make_csv("My Collection", [{"id": "b"}]))])
    card = db_session.query(Card).filter(Card.card_id == "a").one()
    assert card.flagged_missing_since is not None

    csv = make_csv("My Collection", [{"id": "a", "qty": 0}, {"id": "b"}])
    import_dex_csv_files(db_session, [("main.csv", csv)])

    db_session.refresh(card)
    assert card.flagged_missing_since is None
    assert card.qty == 0


def test_qty_zero_rows_still_count_as_seen_for_the_circuit_breaker(db_session):
    rows = [{"id": f"c{i}"} for i in range(30)]
    import_dex_csv_files(db_session, [("main.csv", make_csv("My Collection", rows))])

    # Every existing card comes back at qty 0: nothing is newly missing, so
    # the breaker doesn't trip and nothing is flagged.
    zeros = [{"id": f"c{i}", "qty": 0} for i in range(30)]
    result = import_dex_csv_files(db_session, [("main.csv", make_csv("My Collection", zeros))])

    assert result.cards_flagged_missing == 0
    assert result.cards_updated == 30


def test_unowned_qty_zero_collection_rows_are_counted_not_warned(db_session):
    main = make_csv("My Collection", [{"id": "v1", "variant": "Normal"}])
    vintage = make_csv(
        "Vintage Collection",
        [
            {"id": "v1", "variant": "Normal", "qty": 1},
            {"id": "v1", "variant": "Reverse Holo", "qty": 0},
            {"id": "v2", "qty": 0},
            {"id": "v3", "qty": 0},
        ],
    )
    result = import_dex_csv_files(db_session, [("main.csv", main), ("vintage.csv", vintage)])

    assert result.warnings == []
    assert result.unowned_rows_skipped == 3
    vintage_coll = db_session.query(Collection).filter(Collection.name == "Vintage Collection").one()
    assert {(c.card_id, c.variant) for c in vintage_coll.cards} == {("v1", "Normal")}


def test_unknown_collection_row_with_quantity_still_warns_individually(db_session):
    main = make_csv("My Collection", [{"id": "v1"}])
    vintage = make_csv("Vintage Collection", [{"id": "v1"}, {"id": "ghost", "qty": 1}, {"id": "zero", "qty": 0}])
    result = import_dex_csv_files(db_session, [("main.csv", main), ("vintage.csv", vintage)])

    assert len(result.warnings) == 1
    assert "'ghost'" in result.warnings[0]
    assert "finnes ikke i databasen" in result.warnings[0]
    assert result.unowned_rows_skipped == 1
