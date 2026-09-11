from conftest import make_csv

import queries
from importer import import_dex_csv_files


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


def test_data_quality_counts(db_session):
    main = make_csv("My Collection", [{"id": "a", "price": "10"}, {"id": "b", "price": ""}])
    import_dex_csv_files(db_session, [("main.csv", main)])
    v2 = make_csv("My Collection", [{"id": "a", "price": "10"}])  # b now missing -> flagged
    import_dex_csv_files(db_session, [("main.csv", v2)])

    quality = queries.data_quality(db_session)
    assert quality["missing_price_count"] == 1
    assert quality["flagged_missing_count"] == 1
