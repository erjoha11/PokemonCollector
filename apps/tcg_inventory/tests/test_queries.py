import pytest
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

    # Pikachu (qty 2 -> 1 dup) + Charizard (qty 1 -> 0 dup) + Bulbasaur (qty 5 -> 4 dup)
    assert headline["duplicates"] == 5
    assert headline["avg_unique_value"] == (150.5 + 900 + 10) / 3
    assert headline["avg_physical_value"] == (2 * 150.5 + 900 + 5 * 10) / 8
    # duplicate_value = total_value - unique_value = value tied up in extra copies
    assert headline["duplicate_value"] == (2 * 150.5 + 900 + 5 * 10) - (150.5 + 900 + 10)


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


def test_series_breakdown_carries_per_series_sets_in_release_order(db_session):
    main = make_csv(
        "My Collection",
        [
            {"id": "a", "series": "Original", "set": "Base Set", "qty": 1},
            {"id": "b", "series": "Original", "set": "Jungle", "qty": 2},
        ],
    )
    import_dex_csv_files(db_session, [("main.csv", main)])

    db_session.add(SetReleaseOrder(series="Original", set="Base Set", release_rank=1))
    db_session.add(SetReleaseOrder(series="Original", set="Jungle", release_rank=2))
    db_session.commit()

    original = next(b for b in queries.by_series_breakdown(db_session) if b.name == "Original")
    set_names = [s.name for s in original.child_sets]
    assert set_names == ["Base Set", "Jungle"]
    assert original.child_sets[0].qty == 1
    assert original.child_sets[1].qty == 2


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


def test_rarity_breakdown_sorts_by_modern_tier_order(db_session):
    main = make_csv(
        "My Collection",
        [
            {"id": "a", "rarity": "Secret Rare"},
            {"id": "b", "rarity": "Special Illustration Rare"},
            {"id": "c", "rarity": "Amazing Rare"},  # not in the known tier list
            {"id": "d", "rarity": "Uncommon"},
            {"id": "e", "rarity": "Common"},
            {"id": "f", "rarity": "Ultra Rare"},
            {"id": "g", "rarity": "Rare"},
        ],
    )
    import_dex_csv_files(db_session, [("main.csv", main)])

    names = [b.name for b in queries.by_rarity_breakdown(db_session)]
    # Known tiers in their canonical low-to-high order; anything unknown
    # (no single universal ranking across eras) sorts alphabetically after.
    assert names == [
        "Common",
        "Uncommon",
        "Rare",
        "Ultra Rare",
        "Special Illustration Rare",
        "Secret Rare",
        "Amazing Rare",
    ]


def test_merge_pokemon_is_reusable_at_the_queries_layer(db_session):
    # merge_pokemon used to live inline in the /pokemon/merge route; it's now
    # a plain queries.py operation any caller can use directly (a bulk-import
    # merge, a future API, etc.) without going through the HTTP layer.
    main = make_csv(
        "My Collection",
        [{"id": "a", "name": "Celebi"}, {"id": "b", "name": "Dark Celebi"}],
    )
    import_dex_csv_files(db_session, [("main.csv", main)])

    queries.merge_pokemon(db_session, "Dark Celebi", "Celebi")
    db_session.commit()

    assert queries.pokemon_alias_map(db_session) == {"Dark Celebi": "Celebi"}
    bucket = next(b for b in queries.by_pokemon_breakdown(db_session) if b.name == "Celebi")
    assert bucket.unique_count == 2


def test_collection_value_growth_buckets_by_created_at_month(db_session):
    import datetime as dt

    from models import Card

    main = make_csv(
        "My Collection",
        [
            {"id": "a", "name": "Pikachu", "price": "100"},
            {"id": "b", "name": "Charizard", "price": "50"},
            {"id": "c", "name": "Magikarp", "price": "10"},
        ],
    )
    import_dex_csv_files(db_session, [("main.csv", main)])

    cards = {c.card_id: c for c in db_session.query(Card).all()}
    cards["a"].created_at = dt.datetime(2026, 1, 10)
    cards["b"].created_at = dt.datetime(2026, 1, 20)  # same month as "a"
    cards["c"].created_at = None  # predates created_at tracking
    db_session.commit()

    growth = queries.collection_value_growth(db_session)
    labels = [row["label"] for row in growth]
    assert labels == ["Før sporing", "2026-01"]
    assert growth[0]["added_value"] == 10  # Magikarp, untracked
    assert growth[1]["added_value"] == 150  # Pikachu + Charizard, same month
    assert growth[1]["cumulative_value"] == 160  # running total across both buckets


def test_collection_value_growth_metric_switches_unique_duplicates_total(db_session):
    import datetime as dt

    from models import Card

    # qty=3, price=10 -> unique_value=10, total_value=30, duplicate value=20.
    main = make_csv("My Collection", [{"id": "a", "name": "Pikachu", "qty": 3, "price": "10"}])
    import_dex_csv_files(db_session, [("main.csv", main)])
    card = db_session.query(Card).filter(Card.card_id == "a").one()
    card.created_at = dt.datetime(2026, 1, 10)
    db_session.commit()

    unique = queries.collection_value_growth(db_session, metric="unique")
    duplicates = queries.collection_value_growth(db_session, metric="duplicates")
    total = queries.collection_value_growth(db_session, metric="total")

    assert unique[0]["cumulative_value"] == 10
    assert duplicates[0]["cumulative_value"] == 20
    assert total[0]["cumulative_value"] == 30

    with pytest.raises(ValueError):
        queries.collection_value_growth(db_session, metric="not-a-real-metric")


def test_real_value_history_sums_snapshots_by_date_not_approximated(db_session):
    import datetime as dt

    import snapshots
    from models import Card

    main = make_csv(
        "My Collection",
        [{"id": "a", "name": "Pikachu", "qty": 2, "price": "100"}],
    )
    import_dex_csv_files(db_session, [("main.csv", main)])
    card = db_session.query(Card).filter(Card.card_id == "a").one()

    snapshots.record_daily_snapshot(db_session, as_of=dt.date(2026, 1, 1))
    card.qty = 3
    db_session.commit()
    snapshots.record_daily_snapshot(db_session, as_of=dt.date(2026, 1, 2))

    unique = queries.real_value_history(db_session, metric="unique")
    total = queries.real_value_history(db_session, metric="total")

    assert [row["label"] for row in unique] == ["2026-01-01", "2026-01-02"]
    assert unique[0]["cumulative_value"] == 100  # unique_value unaffected by qty
    assert total[0]["cumulative_value"] == 200  # qty=2 * 100
    assert total[1]["cumulative_value"] == 300  # qty=3 * 100

    with pytest.raises(ValueError):
        queries.real_value_history(db_session, metric="not-a-real-metric")


def test_real_value_history_empty_with_no_snapshots(db_session):
    assert queries.real_value_history(db_session) == []


def test_cash_flow_by_month_tracks_real_transactions_not_estimates(db_session):
    import datetime as dt

    from models import Card, Transaction

    main = make_csv("My Collection", [{"id": "a", "name": "Pikachu"}])
    import_dex_csv_files(db_session, [("main.csv", main)])
    card = db_session.query(Card).filter(Card.card_id == "a").one()

    db_session.add(Transaction(card_id=card.id, type="kjøp", date=dt.date(2026, 1, 5), price=100, fees=10))
    db_session.add(Transaction(card_id=card.id, type="salg", date=dt.date(2026, 2, 1), price=60))
    db_session.commit()

    flow = queries.cash_flow_by_month(db_session)
    assert flow[0]["label"] == "2026-01"
    assert flow[0]["bought"] == 110  # price + fees
    assert flow[0]["sold"] == 0
    assert flow[0]["cumulative_invested"] == 110

    assert flow[1]["label"] == "2026-02"
    assert flow[1]["bought"] == 0
    assert flow[1]["sold"] == 60
    assert flow[1]["cumulative_invested"] == 50  # 110 bought - 60 sold


def test_economic_summary_computes_net_invested(db_session):
    import datetime as dt

    from models import Card, Transaction

    main = make_csv("My Collection", [{"id": "a", "name": "Pikachu"}])
    import_dex_csv_files(db_session, [("main.csv", main)])
    card = db_session.query(Card).filter(Card.card_id == "a").one()

    db_session.add(Transaction(card_id=card.id, type="kjøp", date=dt.date(2026, 1, 5), price=100, fees=10))
    db_session.add(Transaction(card_id=card.id, type="salg", date=dt.date(2026, 2, 1), price=60))
    db_session.commit()

    summary = queries.economic_summary(db_session)
    assert summary["total_bought"] == 110
    assert summary["total_sold"] == 60
    assert summary["net_invested"] == 50
