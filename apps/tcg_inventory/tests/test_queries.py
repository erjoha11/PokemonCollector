import pytest
from conftest import make_csv

import queries
from importer import import_dex_csv_files
from models import Card, Set


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


def test_qty_zero_card_unique_value_is_zero_but_total_value_stays_zero_too(db_session):
    # Issue #132: unique_value wasn't gated on qty > 0 the way
    # duplicates/total_value already were, so a traded/sold-away (qty=0)
    # card's full market price still counted toward "Value" KPIs.
    main = make_csv(
        "My Collection",
        [{"id": "traded-away", "qty": 0, "price": "400"}],
    )
    import_dex_csv_files(db_session, [("main.csv", main)])
    card = db_session.query(Card).filter(Card.card_id == "traded-away").one()
    assert card.qty == 0
    assert card.unique_value == 0.0
    assert card.total_value == 0.0
    assert card.duplicates == 0


def test_top_valuable_cards_excludes_qty_zero_cards(db_session):
    main = make_csv(
        "My Collection",
        [
            {"id": "traded-away", "qty": 0, "price": "9999"},
            {"id": "still-owned", "qty": 1, "price": "10"},
        ],
    )
    import_dex_csv_files(db_session, [("main.csv", main)])
    top = queries.top_valuable_cards(db_session, limit=10)
    assert [c.card_id for c in top] == ["still-owned"]


def test_headline_and_bucket_unique_value_exclude_qty_zero_cards(db_session):
    main = make_csv(
        "My Collection",
        [
            {"id": "traded-away", "name": "Pikachu", "qty": 0, "price": "9999", "series": "Test Series"},
            {"id": "still-owned", "name": "Charizard", "qty": 1, "price": "10", "series": "Test Series"},
        ],
    )
    import_dex_csv_files(db_session, [("main.csv", main)])

    headline = queries.headline_summary(db_session)
    assert headline["unique_value"] == 10
    assert headline["total_value"] == 10

    series_breakdown = queries.by_series_breakdown(db_session)
    bucket = next(b for b in series_breakdown if b.name == "Test Series")
    assert bucket.unique_value == 10
    assert bucket.total_value == 10


def _link_set(db_session, series, set_name, release_rank=None, total_cards=None):
    """Test helper mirroring db.py's `_backfill_sets()`: get-or-create a
    `Set` row for (series, set_name) and link every matching card's
    `set_id` to it. `by_series_breakdown` reads release order off this FK
    (`Card.set_id -> Set.release_rank`), not the superseded
    `SetReleaseOrder` table -- see issue #138. The real app links this
    automatically via `db.py`'s `init_db()`/`_backfill_sets()`; the
    `db_session` fixture here doesn't run `init_db()`, so tests link it by
    hand. `total_cards` mirrors `set_sync.py`'s api.pokemontcg.io backfill,
    for #142's completion-percent field.
    """
    set_row = db_session.query(Set).filter_by(series=series, name=set_name).one_or_none()
    if set_row is None:
        set_row = Set(series=series, name=set_name, release_rank=release_rank, total_cards=total_cards)
        db_session.add(set_row)
        db_session.flush()
    else:
        set_row.release_rank = release_rank
        set_row.total_cards = total_cards
    db_session.query(Card).filter_by(series=series, set=set_name).update({Card.set_id: set_row.id})
    db_session.commit()
    return set_row


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

    _link_set(db_session, "Original", "Base Set", release_rank=1)
    _link_set(db_session, "XY", "XY", release_rank=50)

    names = [b.name for b in queries.by_series_breakdown(db_session)]
    # "Original" (rank 1) before "XY" (rank 50) before the series with no
    # linked Set/release_rank at all -- release order, not alphabetical
    # (which would put "Original" after "Unresearched Series").
    assert names == ["Original", "XY", "Unresearched Series"]


def test_series_breakdown_ignores_stale_set_release_order_rows(db_session):
    """Editing `SetReleaseOrder` directly (the superseded table) must not
    move these breakdowns any more -- only `Set.release_rank` does. Guards
    against regressing back to reading `set_release_order` (issue #138).
    """
    from models import SetReleaseOrder

    main = make_csv(
        "My Collection",
        [
            {"id": "a", "series": "XY", "set": "XY"},
            {"id": "b", "series": "Original", "set": "Base Set"},
        ],
    )
    import_dex_csv_files(db_session, [("main.csv", main)])

    # A stale/never-migrated SetReleaseOrder row claiming XY is oldest --
    # if by_series_breakdown were still reading this table, XY would sort
    # first. It must be ignored entirely now.
    db_session.add(SetReleaseOrder(series="XY", set="XY", release_rank=1))
    db_session.commit()

    names = [b.name for b in queries.by_series_breakdown(db_session)]
    # Neither series has a linked Set/release_rank, so both fall back to
    # the UNKNOWN_RELEASE_RANK tie-break (alphabetical by name).
    assert names == ["Original", "XY"]


def test_series_breakdown_carries_per_series_sets_in_release_order(db_session):
    main = make_csv(
        "My Collection",
        [
            {"id": "a", "series": "Original", "set": "Base Set", "qty": 1},
            {"id": "b", "series": "Original", "set": "Jungle", "qty": 2},
        ],
    )
    import_dex_csv_files(db_session, [("main.csv", main)])

    _link_set(db_session, "Original", "Base Set", release_rank=1)
    _link_set(db_session, "Original", "Jungle", release_rank=2)

    original = next(b for b in queries.by_series_breakdown(db_session) if b.name == "Original")
    set_names = [s.name for s in original.child_sets]
    assert set_names == ["Base Set", "Jungle"]
    assert original.child_sets[0].qty == 1
    assert original.child_sets[1].qty == 2


def test_series_breakdown_falls_back_for_cards_with_no_linked_set_or_no_rank(db_session):
    """Mirrors app.py's already-migrated Inventory sort fallback: a card
    with no linked `Set` row at all, and a card whose linked `Set` has no
    `release_rank` yet, both sort after every series/set with a known rank
    -- same UNKNOWN_RELEASE_RANK semantics, just exercised here.
    """
    main = make_csv(
        "My Collection",
        [
            {"id": "ranked", "series": "Original", "set": "Base Set"},
            # Linked to a real Set row, but nobody's researched its rank yet.
            {"id": "norank", "series": "Original", "set": "Jungle"},
            # No series/set at all -- _backfill_sets() would have nothing to
            # link this to in the real app either.
            {"id": "nolink", "series": "", "set": ""},
        ],
    )
    import_dex_csv_files(db_session, [("main.csv", main)])

    _link_set(db_session, "Original", "Base Set", release_rank=1)
    _link_set(db_session, "Original", "Jungle", release_rank=None)

    original = next(b for b in queries.by_series_breakdown(db_session) if b.name == "Original")
    set_names = [s.name for s in original.child_sets]
    # Base Set (rank 1) before Jungle (linked, no rank -> falls back last).
    assert set_names == ["Base Set", "Jungle"]

    names = [b.name for b in queries.by_series_breakdown(db_session)]
    # "Original" (rank 1, via its earliest ranked set) sorts before the
    # "(uten serie)" bucket the unlinked/no-series card lands in, which has
    # no ranked set at all and falls back to the unknown-rank tie-break.
    assert names.index("Original") < names.index("(uten serie)")


def test_set_bucket_completion_pct_computed_from_total_cards_and_partial_ownership(db_session):
    """#142: a set-level bucket with a known `total_cards` and partial
    ownership computes `unique_count / total_cards * 100` -- never stored,
    computed live from the already qty>0-gated `unique_count`.
    """
    main = make_csv(
        "My Collection",
        [
            {"id": "a", "series": "Original", "set": "Base Set", "qty": 1},
            {"id": "b", "series": "Original", "set": "Base Set", "qty": 0},
            {"id": "c", "series": "Original", "set": "Base Set", "qty": 3},
        ],
    )
    import_dex_csv_files(db_session, [("main.csv", main)])

    _link_set(db_session, "Original", "Base Set", release_rank=1, total_cards=102)

    original = next(b for b in queries.by_series_breakdown(db_session) if b.name == "Original")
    base_set = next(s for s in original.child_sets if s.name == "Base Set")

    assert base_set.total_cards == 102
    # 2 owned (qty 1 and qty 3) out of 3 cards, one at qty 0 -> unique_count 2.
    assert base_set.unique_count == 2
    assert base_set.completion_pct == pytest.approx(2 / 102 * 100)

    # The parent series-level bucket is not a set-level metric -- #142 says
    # leave it None/blank rather than aggregating or guessing.
    assert original.total_cards is None
    assert original.completion_pct is None


def test_set_bucket_with_unknown_total_cards_reports_none_not_0_or_100_pct(db_session):
    """#142: a set with no `Set.total_cards` yet (permanent for JP/KR sets
    api.pokemontcg.io doesn't cover, temporary otherwise until `set_sync.py`
    runs again) must report `completion_pct is None` -- the template renders
    an explicit "unknown" state for this, never a bare 0%/100%.
    """
    main = make_csv(
        "My Collection",
        [{"id": "a", "series": "Original", "set": "Base Set", "qty": 1}],
    )
    import_dex_csv_files(db_session, [("main.csv", main)])

    _link_set(db_session, "Original", "Base Set", release_rank=1, total_cards=None)

    original = next(b for b in queries.by_series_breakdown(db_session) if b.name == "Original")
    base_set = next(s for s in original.child_sets if s.name == "Base Set")

    assert base_set.total_cards is None
    assert base_set.completion_pct is None


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


def test_rarity_breakdown_sorts_by_tier_order(db_session):
    expected = [
        "Common",
        "Uncommon",
        "Rare",
        "Double Rare",
        "Ultra Rare",
        "Amazing Rare",
        "Holo Rare",
        "Holo Rare V",
        "Triple Rare",
        "Illustration Rare",
        "Special Illustration Rare",
        "Secret Rare",
        "Mystery Rare",  # not a known tier: after every known tier...
        "No Rarity",  # ...but still before the no-real-rarity tail
        "Promo",
    ]
    main = make_csv(
        "My Collection",
        [{"id": str(i), "rarity": name} for i, name in enumerate(reversed(expected))],
    )
    import_dex_csv_files(db_session, [("main.csv", main)])

    names = [b.name for b in queries.by_rarity_breakdown(db_session)]
    assert names == expected


def test_rarity_rank_puts_missing_rarity_with_the_no_rarity_tail():
    ranked = sorted(["Promo", None, "Triple Rare", "No Rarity", "Common"], key=queries.rarity_rank)
    assert ranked == ["Common", "Triple Rare", "No Rarity", None, "Promo"]


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
    assert labels == ["Before tracking", "2026-01"]
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
    assert [row["card_count"] for row in unique] == [1, 1]
    assert total[0]["cumulative_value"] == 200  # qty=2 * 100
    assert total[1]["cumulative_value"] == 300  # qty=3 * 100
    assert [row["card_count"] for row in total] == [2, 3]

    with pytest.raises(ValueError):
        queries.real_value_history(db_session, metric="not-a-real-metric")


def test_real_value_history_empty_with_no_snapshots(db_session):
    assert queries.real_value_history(db_session) == []


def test_real_value_history_keeps_one_point_per_day_the_latest_source(db_session):
    import datetime as dt

    import snapshots
    from models import Card

    main = make_csv(
        "My Collection",
        [{"id": "a", "name": "Pikachu", "qty": 2, "price": "100"}],
    )
    import_dex_csv_files(db_session, [("main.csv", main)])
    card = db_session.query(Card).filter(Card.card_id == "a").one()

    snapshots.record_daily_snapshot(db_session, as_of=dt.date(2026, 1, 1), source="cron")
    card.qty = 4
    db_session.commit()
    snapshots.record_daily_snapshot(db_session, as_of=dt.date(2026, 1, 1), source="manual")
    card.qty = 3
    db_session.commit()
    snapshots.record_daily_snapshot(db_session, as_of=dt.date(2026, 1, 1), source="price-cron")
    snapshots.record_daily_snapshot(db_session, as_of=dt.date(2026, 1, 2), source="cron")

    total = queries.real_value_history(db_session, metric="total", today=dt.date(2026, 1, 2))

    assert [row["label"] for row in total] == ["2026-01-01", "2026-01-02"]
    assert total[0]["cumulative_value"] == 400  # manual is the day's last point, after price-cron
    assert total[0]["date"] == dt.date(2026, 1, 1)


def test_real_value_history_metrics_only_count_owned_copies(db_session):
    import datetime as dt

    import snapshots
    from models import Card

    main = make_csv(
        "My Collection",
        [{"id": "a", "qty": 3, "price": "10"}, {"id": "b", "qty": 1, "price": "5"}],
    )
    import_dex_csv_files(db_session, [("main.csv", main)])
    db_session.query(Card).filter(Card.card_id == "b").one().qty = 0  # sold, still in the table
    db_session.commit()
    snapshots.record_daily_snapshot(db_session, as_of=dt.date(2026, 1, 1))

    def value(metric):
        return queries.real_value_history(db_session, metric=metric)[0]["cumulative_value"]

    assert value("unique") == 10
    assert value("duplicates") == 20
    assert value("total") == 30


def test_real_value_history_ends_on_live_value_and_filters_by_period(db_session):
    import datetime as dt

    import snapshots

    main = make_csv("My Collection", [{"id": "a", "qty": 1, "price": "100"}])
    import_dex_csv_files(db_session, [("main.csv", main)])
    today = dt.date(2026, 6, 30)
    for day in (dt.date(2025, 1, 1), dt.date(2026, 6, 1), dt.date(2026, 6, 25), today):
        snapshots.record_daily_snapshot(db_session, as_of=day)

    live = (123.0, 1)
    all_time = queries.real_value_history(db_session, metric="total", today=today, live=live)
    assert [row["label"] for row in all_time] == ["2025-01-01", "2026-06-01", "2026-06-25", "2026-06-30"]
    assert all_time[-1]["cumulative_value"] == 123  # today's snapshot replaced by the live value

    week = queries.real_value_history(db_session, metric="total", period="1w", today=today, live=live)
    assert [row["label"] for row in week] == ["2026-06-25", "2026-06-30"]
    assert queries.period_change(week) == {"change": 23, "pct": pytest.approx(23.0)}

    year = queries.real_value_history(db_session, metric="total", period="1y", today=today)
    assert [row["label"] for row in year] == ["2026-06-01", "2026-06-25", "2026-06-30"]

    # No snapshot today yet: the live value is added as today's point.
    tomorrow = today + dt.timedelta(days=1)
    ahead = queries.real_value_history(db_session, metric="total", period="1w", today=tomorrow, live=live)
    assert ahead[-1]["label"] == "2026-07-01"

    with pytest.raises(ValueError):
        queries.real_value_history(db_session, period="2w")


def test_real_value_history_live_value_never_invents_history(db_session):
    assert queries.real_value_history(db_session, live=(100.0, 1)) == []


def test_value_change_breakdown_splits_price_moves_from_new_cards(db_session):
    import datetime as dt

    import snapshots
    from models import Card

    main = make_csv("My Collection", [{"id": "a", "qty": 1, "price": "100"}, {"id": "b", "qty": 0, "price": "50"}])
    import_dex_csv_files(db_session, [("main.csv", main)])
    a = db_session.query(Card).filter(Card.card_id == "a").one()
    b = db_session.query(Card).filter(Card.card_id == "b").one()
    start, end = dt.date(2026, 1, 1), dt.date(2026, 1, 10)
    snapshots.record_daily_snapshot(db_session, as_of=start)
    # Then: a's price rises 100 -> 120, a second copy of a is added, and b
    # (worth 50 -> 60) is bought.
    a.qty, a.reference_price = 2, 120
    b.qty, b.reference_price = 1, 60
    db_session.commit()
    snapshots.record_daily_snapshot(db_session, as_of=end)

    total = queries.real_value_history(db_session, metric="total", today=end)
    bd = queries.value_change_breakdown(db_session, "total", total)
    assert bd == {"price": 20, "cards": 180, "card_delta": 2}  # 1*20; 1*120 + 1*60
    assert bd["price"] + bd["cards"] == queries.period_change(total)["change"]

    unique = queries.real_value_history(db_session, metric="unique", today=end)
    assert queries.value_change_breakdown(db_session, "unique", unique) == {"price": 20, "cards": 60, "card_delta": 1}

    dup = queries.real_value_history(db_session, metric="duplicates", today=end)
    assert queries.value_change_breakdown(db_session, "duplicates", dup) == {"price": 0, "cards": 120, "card_delta": 1}

    # Live cards stand in for the last point.
    a.reference_price = 130
    db_session.commit()
    live = queries.value_change_breakdown(db_session, "total", total, live_cards=db_session.query(Card).all())
    assert live == {"price": 30, "cards": 190, "card_delta": 2}

    assert queries.value_change_breakdown(db_session, "total", total[:1]) is None


def test_period_change_needs_two_points_and_handles_a_zero_start():
    assert queries.period_change([{"cumulative_value": 5}]) is None
    assert queries.period_change([{"cumulative_value": 0}, {"cumulative_value": 5}]) == {"change": 5, "pct": None}


def test_net_invested_at_dates_accumulates_purchases_minus_sales(db_session):
    import datetime as dt

    from models import Card, Transaction

    main = make_csv("My Collection", [{"id": "a"}])
    import_dex_csv_files(db_session, [("main.csv", main)])
    card = db_session.query(Card).one()
    db_session.add(Transaction(card_id=card.id, type="purchase", date=dt.date(2026, 1, 5), price=100, fees=10))
    db_session.add(Transaction(card_id=card.id, type="sale", date=dt.date(2026, 2, 1), price=60))
    db_session.commit()
    txs = db_session.query(Transaction).all()

    dates = [dt.date(2026, 1, 1), dt.date(2026, 1, 5), dt.date(2026, 3, 1)]
    assert queries.net_invested_at_dates(txs, dates) == [0, 110, 50]
    assert queries.net_invested_at_dates(txs, [dt.date(2026, 3, 1)])[-1] == queries.economic_summary(db_session, txs)["net_invested"]


def test_cash_flow_by_month_tracks_real_transactions_not_estimates(db_session):
    import datetime as dt

    from models import Card, Transaction

    main = make_csv("My Collection", [{"id": "a", "name": "Pikachu"}])
    import_dex_csv_files(db_session, [("main.csv", main)])
    card = db_session.query(Card).filter(Card.card_id == "a").one()

    db_session.add(Transaction(card_id=card.id, type="purchase", date=dt.date(2026, 1, 5), price=100, fees=10))
    db_session.add(Transaction(card_id=card.id, type="sale", date=dt.date(2026, 2, 1), price=60))
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

    db_session.add(Transaction(card_id=card.id, type="purchase", date=dt.date(2026, 1, 5), price=100, fees=10))
    db_session.add(Transaction(card_id=card.id, type="sale", date=dt.date(2026, 2, 1), price=60))
    db_session.commit()

    summary = queries.economic_summary(db_session)
    assert summary["total_bought"] == 110
    assert summary["total_sold"] == 60
    assert summary["net_invested"] == 50


def test_economic_summary_includes_purchase_shipping(db_session):
    import datetime as dt

    from models import Card, Transaction

    main = make_csv(
        "My Collection", [{"id": "a", "name": "Pikachu"}, {"id": "b", "name": "Charizard"}]
    )
    import_dex_csv_files(db_session, [("main.csv", main)])
    cards = {c.card_id: c.id for c in db_session.query(Card).all()}

    # Two cards sharing one order/purchase_id -- shipping is a per-order
    # value duplicated onto every row of the order (see app.py's
    # create_purchase), so it must be counted once for the order, not once
    # per row, even though total_bought must still include it at all
    # (previously omitted entirely).
    db_session.add(
        Transaction(
            card_id=cards["a"], type="purchase", date=dt.date(2026, 1, 5), price=100, fees=10,
            purchase_id=1, purchase_shipping=50,
        )
    )
    db_session.add(
        Transaction(
            card_id=cards["b"], type="purchase", date=dt.date(2026, 1, 5), price=200,
            purchase_id=1, purchase_shipping=50,
        )
    )
    db_session.commit()

    summary = queries.economic_summary(db_session)
    # 100 + 10 fees + 200 + 50 shipping (once, not 100) = 360
    assert summary["total_bought"] == 360
    assert summary["net_invested"] == 360

    invested = queries.net_invested_by_card(db_session)
    # Shipping is split across the order's cards by price (100 : 200), so
    # the per-card figures still sum to the same total.
    assert round(sum(invested.values()), 6) == 360
    assert round(invested[cards["a"]], 6) == round(100 + 10 + 50 * 100 / 300, 6)
    assert round(invested[cards["b"]], 6) == round(200 + 50 * 200 / 300, 6)


def _purchase(card_id, price, purchase_id=1, shipping=None, tx_id=None):
    import datetime as dt

    from models import Transaction

    return Transaction(
        id=tx_id, card_id=card_id, type="purchase", date=dt.date(2026, 1, 5), price=price,
        purchase_id=purchase_id, purchase_shipping=shipping,
    )


def test_shipping_shares_split_an_order_by_price():
    # One 25 kr card + 38 kr shipping: the card really cost 63 kr.
    single = _purchase(1, 25, shipping=38, tx_id=1)
    assert queries.shipping_shares([single]) == {1: 38}

    rows = [_purchase(1, 10, purchase_id=2, shipping=30, tx_id=2), _purchase(2, 20, purchase_id=2, shipping=30, tx_id=3)]
    shares = queries.shipping_shares(rows)
    assert shares == {2: 10, 3: 20}


def test_shipping_shares_split_evenly_when_nothing_is_priced_yet():
    rows = [_purchase(c, 0, shipping=44, tx_id=c) for c in (1, 2, 3, 4)]
    assert queries.shipping_shares(rows) == {1: 11, 2: 11, 3: 11, 4: 11}


def test_shipping_shares_ungrouped_row_keeps_its_own_shipping_and_trades_get_none():
    import datetime as dt

    from models import Transaction

    loose = _purchase(1, 5, purchase_id=None, shipping=12, tx_id=1)
    trade = Transaction(id=2, card_id=2, type="trade", direction="in", date=dt.date(2026, 1, 5), price=0,
                        purchase_id=3, purchase_shipping=9)
    assert queries.shipping_shares([loose, trade]) == {1: 12}


def _seed_unlinked_cards(db_session):
    # Built directly via the ORM (bypassing import_dex_csv_files), which
    # since issue #134 links Card.set_id inline at import time -- this
    # tests queries.unlinked_set_cards()'s own grouping logic against cards
    # that are genuinely unlinked (e.g. predating that change, or reached
    # the database some other way), independent of the importer.
    db_session.add_all(
        [
            Card(card_id="a", variant=None, name="Pikachu", series="Scarlet & Violet", set="Test Set", set_id=None),
            Card(card_id="b", variant=None, name="Charizard", series="Scarlet & Violet", set="Test Set", set_id=None),
            Card(card_id="c", variant=None, name="Bulbasaur", series="Sword & Shield", set="Test Set", set_id=None),
        ]
    )
    db_session.commit()


def test_unlinked_set_cards_groups_unlinked_cards_by_series_and_set(db_session):
    _seed_unlinked_cards(db_session)

    unlinked = queries.unlinked_set_cards(db_session)
    by_pair = {(row.series, row.set): row.card_count for row in unlinked}

    assert by_pair == {
        ("Scarlet & Violet", "Test Set"): 2,  # Pikachu, Charizard
        ("Sword & Shield", "Test Set"): 1,  # Bulbasaur
    }


def test_unlinked_set_cards_excludes_cards_with_a_linked_set(db_session):
    _seed_unlinked_cards(db_session)
    linked_card = db_session.query(Card).filter_by(name="Pikachu").one()
    set_row = Set(series=linked_card.series, name=linked_card.set, release_rank=1)
    db_session.add(set_row)
    db_session.flush()
    linked_card.set_id = set_row.id
    db_session.commit()

    unlinked = queries.unlinked_set_cards(db_session)
    by_pair = {(row.series, row.set): row.card_count for row in unlinked}

    # Charizard shares Pikachu's (series, set) but isn't itself linked --
    # only the actually-linked card drops out of the count.
    assert by_pair[("Scarlet & Violet", "Test Set")] == 1
    assert by_pair[("Sword & Shield", "Test Set")] == 1


def test_unlinked_set_cards_includes_cards_with_no_series_or_set(db_session):
    db_session.add(Card(card_id="1", variant=None, name="Solo", series=None, set=None, set_id=None))
    db_session.commit()

    assert queries.unlinked_set_cards(db_session) == [
        queries.UnlinkedSetCards(series=None, set=None, card_count=1)
    ]


def test_sets_missing_release_rank_only_lists_null_rank_sets(db_session):
    # Since issue #134, import_dex_csv_files() already get-or-creates and
    # links the Set rows for these cards inline -- reuse those rows (just
    # setting a release_rank on one) rather than creating new ones, which
    # would collide with the unique (series, name) constraint.
    _seed(db_session)
    linked_card = db_session.query(Card).filter_by(name="Pikachu").one()
    ranked = db_session.query(Set).filter_by(series=linked_card.series, name=linked_card.set).one()
    ranked.release_rank = 1
    unranked = db_session.query(Set).filter_by(series="Sword & Shield", name="Test Set").one()
    assert unranked.release_rank is None  # unranked from import, as expected below
    db_session.commit()

    missing = queries.sets_missing_release_rank(db_session)

    assert missing == [
        queries.SetMissingReleaseRank(series="Sword & Shield", name="Test Set", card_count=1)
    ]


def test_sets_missing_release_rank_includes_sets_with_no_cards(db_session):
    db_session.add(Set(series="Empty Series", name="Empty Set", release_rank=None))
    db_session.commit()

    missing = queries.sets_missing_release_rank(db_session)

    assert missing == [
        queries.SetMissingReleaseRank(series="Empty Series", name="Empty Set", card_count=0)
    ]


def test_price_movers_ranks_by_kr_since_the_period_start(db_session):
    import datetime as dt

    import queries
    from models import Card, CardSnapshot

    today = dt.date(2026, 9, 24)
    cards = [
        Card(card_id="a", name="Charizard", qty=1, reference_price=500),
        Card(card_id="b", name="Mew", qty=1, reference_price=90),
        Card(card_id="c", name="Pikachu", qty=1, reference_price=4),  # +100 %, but only +2 kr
        Card(card_id="d", name="Umbreon", qty=1, reference_price=300),
        Card(card_id="e", name="Sold", qty=0, reference_price=999),  # not owned: ignored
    ]
    db_session.add_all(cards)
    db_session.flush()
    old = {"a": 400, "b": 100, "c": 2, "d": 300, "e": 1}
    for card in cards:
        # an older day (ignored: a later one still precedes the period start) and the start day
        db_session.add(CardSnapshot(card_id=card.id, date=dt.date(2026, 8, 1), source="cron", qty=1, reference_price=1))
        db_session.add(CardSnapshot(card_id=card.id, date=dt.date(2026, 8, 20), source="cron", qty=1, reference_price=old[card.card_id]))
    db_session.commit()

    result = queries.price_movers(db_session, cards, days=30, today=today)
    assert result["since"] == dt.date(2026, 8, 20) and result["days"] == 35
    assert [(m.card.name, m.change) for m in result["up"]] == [("Charizard", 100), ("Pikachu", 2)]
    assert [(m.card.name, m.change) for m in result["down"]] == [("Mew", -10)]
    assert round(result["up"][0].pct) == 25
    assert (result["n_up"], result["n_down"]) == (2, 1)


def test_price_movers_uses_earliest_day_when_history_is_short_and_none_without_history(db_session):
    import datetime as dt

    import queries
    from models import Card, CardSnapshot

    card = Card(card_id="a", name="Charizard", qty=1, reference_price=500)
    db_session.add(card)
    db_session.commit()
    today = dt.date(2026, 9, 24)
    assert queries.price_movers(db_session, [card], today=today) is None

    db_session.add(CardSnapshot(card_id=card.id, date=dt.date(2026, 9, 20), source="cron", qty=1, reference_price=450))
    db_session.commit()
    result = queries.price_movers(db_session, [card], today=today)
    assert result["days"] == 4 and result["up"][0].change == 50
