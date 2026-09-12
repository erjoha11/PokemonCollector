from conftest import make_csv

import queries
from importer import import_dex_csv_files
from models import SetReleaseOrder


def _seed(db_session):
    main = make_csv(
        "My Collection",
        [
            {"id": "a", "name": "Pikachu", "series": "Scarlet & Violet", "qty": 2, "price": "150.5"},
            {"id": "b", "name": "Charizard", "series": "Scarlet & Violet", "qty": 1, "price": "900"},
            {"id": "c", "name": "Bulbasaur", "series": "Sword & Shield", "qty": 5, "price": "10"},
        ],
    )
    vintage = make_csv("Vintage Collection", [{"id": "a"}])
    illustrator = make_csv("Tomokazu Komiya Collection", [{"id": "b"}])
    binder = make_csv("Illustrator Binder", [{"id": "b", "price": "900"}])
    import_dex_csv_files(
        db_session,
        [("main.csv", main), ("vintage.csv", vintage), ("illustrator.csv", illustrator), ("binder.csv", binder)],
    )


def test_collection_bulk_parent_equals_sum_of_children_plus_bulk_is_separate(db_session):
    _seed(db_session)
    breakdown = queries.collection_bulk_breakdown(db_session)

    child_qty = sum(c.qty for c in breakdown["children"])
    child_value = sum(c.total_value for c in breakdown["children"])
    assert breakdown["parent"].qty == child_qty
    assert breakdown["parent"].total_value == child_value

    # Bulbasaur belongs to no collection -> counted only in Bulk, not in parent.
    assert breakdown["bulk"].qty == 5
    assert breakdown["parent"].qty == 3  # Pikachu (2) + Charizard (1)


def test_collection_bulk_display_order_pins_vintage_second_and_illustrators_last(db_session):
    main = make_csv("My Collection", [{"id": "a"}, {"id": "b"}, {"id": "c"}, {"id": "d"}])
    generic = make_csv("Collection", [{"id": "a"}])
    vintage = make_csv("Vintage Collection", [{"id": "b"}])
    illustrator = make_csv("Tomokazu Komiya Collection", [{"id": "c"}])
    other = make_csv("Scarlet & Violet: 151 JP/KR", [{"id": "d"}])
    import_dex_csv_files(
        db_session,
        [
            ("main.csv", main),
            ("generic.csv", generic),
            ("vintage.csv", vintage),
            ("illustrator.csv", illustrator),
            ("other.csv", other),
        ],
    )

    breakdown = queries.collection_bulk_breakdown(db_session)
    names = [c.name for c in breakdown["children"]]
    assert names == [
        "Collection",
        "Vintage Collection",
        "Scarlet & Violet: 151 JP/KR",
        "Tomokazu Komiya Collection",
    ]


def test_headline_summary_totals(db_session):
    _seed(db_session)
    headline = queries.headline_summary(db_session)
    assert headline["qty_physical"] == 8
    assert headline["qty_unique"] == 3
    assert headline["unique_value"] == 150.5 + 900 + 10
    assert headline["total_value"] == 2 * 150.5 + 900 + 5 * 10


def test_binder_breakdown_uses_unique_value_not_total_value(db_session):
    _seed(db_session)
    binders = queries.by_binder_breakdown(db_session)
    illustrator_binder = next(b for b in binders if b.name == "Illustrator Binder")
    # Charizard: qty=1, price=900 -> unique_value must be 900, NOT qty * price.
    assert illustrator_binder.qty == 1
    assert illustrator_binder.unique_value == 900


def test_top_valuable_cards_ranks_by_reference_price_not_total_value(db_session):
    main = make_csv(
        "My Collection",
        [
            {"id": "cheap-but-many", "qty": 100, "price": "5"},  # total_value 500
            {"id": "expensive-single", "qty": 1, "price": "400"},  # total_value 400
        ],
    )
    import_dex_csv_files(db_session, [("main.csv", main)])
    top = queries.top_valuable_cards(db_session, limit=10)
    assert top[0].card_id == "expensive-single"


def test_series_breakdown_sorts_by_release_order_not_alphabetically(db_session):
    main = make_csv(
        "My Collection",
        [
            {"id": "a", "series": "XY", "set": "XY"},
            {"id": "b", "series": "Original", "set": "Base Set"},
            {"id": "c", "series": "Unresearched Series", "set": "Some Set"},
        ],
    )
    import_dex_csv_files(db_session, [("main.csv", main)])

    db_session.add(SetReleaseOrder(series="Original", set="Base Set", release_rank=1))
    db_session.add(SetReleaseOrder(series="XY", set="XY", release_rank=50))
    db_session.commit()

    names = [b.name for b in queries.by_series_breakdown(db_session)]
    # "Original" (rank 1) before "XY" (rank 50) before the series with no
    # set_release_order row at all -- release order, not alphabetical
    # (which would put "Original" after "Unresearched Series").
    assert names == ["Original", "XY", "Unresearched Series"]


def test_rarity_breakdown_groups_by_rarity(db_session):
    main = make_csv(
        "My Collection",
        [
            {"id": "a", "rarity": "Common", "qty": 2, "price": "5"},
            {"id": "b", "rarity": "Common", "qty": 1, "price": "5"},
            {"id": "c", "rarity": "Rare", "qty": 1, "price": "50"},
        ],
    )
    import_dex_csv_files(db_session, [("main.csv", main)])

    rarities = {b.name: b for b in queries.by_rarity_breakdown(db_session)}
    assert rarities["Common"].qty == 3
    assert rarities["Common"].duplicates == 1  # card "a" has qty 2 -> 1 duplicate
    assert rarities["Rare"].qty == 1
