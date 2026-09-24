"""Phase 1 "trust the numbers" rules, pinned with small known data sets:
collection membership vs. the deduplicated total, the single gain
definition, cards with no purchase price, completion, price-move %.
"""
import datetime as dt

import pytest
from conftest import make_csv, seed_import

import queries
from models import Card, Collection, Set


def _collection(db, name, rank=99):
    c = Collection(name=name, priority_rank=rank)
    db.add(c)
    db.flush()
    return c


def _card(db, card_id, price, qty=1, number=None, collections=(), variant="Normal", set_row=None):
    c = Card(
        card_id=card_id,
        name=card_id.upper(),
        variant=variant,
        qty=qty,
        number=number,
        reference_price=price,
        series=set_row.series if set_row else None,
        set=set_row.name if set_row else None,
        set_id=set_row.id if set_row else None,
    )
    c.collections = list(collections)
    db.add(c)
    db.flush()
    return c


@pytest.fixture
def collection_data(db_session):
    """Vintage: a, b, c. SV151: c, d. Bulk: e, f (f no longer owned).

    Physical: a2 b1 c3 d1 e4 f0 = 11; unique owned = 5 (f has qty 0).
    """
    vintage = _collection(db_session, "Vintage Collection", 2)
    sv151 = _collection(db_session, "Scarlet & Violet: 151 JP/KR", 4)
    cards = {
        "a": _card(db_session, "a", 100, qty=2, collections=[vintage]),
        "b": _card(db_session, "b", 50, qty=1, collections=[vintage]),
        "c": _card(db_session, "c", 200, qty=3, collections=[vintage, sv151]),
        "d": _card(db_session, "d", 10, qty=1, collections=[sv151]),
        "e": _card(db_session, "e", 5, qty=4),
        "f": _card(db_session, "f", 999, qty=0),
    }
    db_session.commit()
    return cards


def test_each_collection_row_counts_every_card_with_its_tag(db_session, collection_data):
    bd = queries.collection_membership_breakdown(db_session)
    rows = {b.name: b for b in bd["children"]}

    assert (rows["Vintage Collection"].unique_count, rows["Vintage Collection"].qty) == (3, 6)
    assert (rows["Scarlet & Violet: 151 JP/KR"].unique_count, rows["Scarlet & Violet: 151 JP/KR"].qty) == (2, 4)
    assert rows["Vintage Collection"].unique_value == 350
    assert rows["Vintage Collection"].total_value == 2 * 100 + 50 + 3 * 200


def test_sum_of_collection_rows_can_exceed_the_deduplicated_total(db_session, collection_data):
    bd = queries.collection_membership_breakdown(db_session)

    row_unique = sum(b.unique_count for b in bd["children"])
    assert row_unique == 5  # c counted in both collections
    assert bd["collections"].unique_count == 4  # a, b, c, d once each
    assert row_unique > bd["collections"].unique_count

    # Total = Collections + Bulk, every card once; qty 0 isn't unique.
    total = bd["total"]
    assert (total.unique_count, total.qty) == (5, 11)
    assert total.total_value == 2 * 100 + 50 + 3 * 200 + 10 + 4 * 5
    assert (bd["bulk"].unique_count, bd["bulk"].qty) == (1, 4)
    assert bd["collections"].qty + bd["bulk"].qty == total.qty

    # The Total row matches the headline KPI exactly.
    headline = queries.headline_summary(db_session)
    assert (headline["qty_unique"], headline["qty_physical"]) == (total.unique_count, total.qty)
    assert headline["total_value"] == total.total_value


def test_most_valuable_collection_uses_real_membership(db_session, collection_data):
    import app as app_module

    bd = queries.collection_membership_breakdown(db_session)
    top, _ = app_module._top_collection_and_series(bd, [])
    # Primary-credit would have given c (200) to Vintage only; SV151 gets it too
    # but Vintage (350) still wins. Membership, not credit, decides the numbers.
    assert top.name == "Vintage Collection"
    assert top.unique_value == 350


def test_bucket_gain_is_total_value_minus_net_invested(db_session, collection_data):
    bd = queries.collection_membership_breakdown(db_session)
    c = collection_data
    invested = {c["a"].id: 120, c["c"].id: 300}
    queries.assign_bucket_investment(bd["children"] + [bd["total"]], invested)
    vintage = next(b for b in bd["children"] if b.name == "Vintage Collection")

    assert vintage.net_invested == 420
    assert vintage.gain_loss == (2 * 100 + 50 + 3 * 200) - 420  # 430, not unique 350 - 420


def test_gain_summary_reports_cards_without_purchase_price(db_session, collection_data):
    c = collection_data
    invested = {c["a"].id: 120, c["c"].id: 300, c["f"].id: 10}
    g = queries.gain_summary(db_session.query(Card).all(), invested, net_invested=430)

    assert g["total_value"] == 2 * 100 + 50 + 3 * 200 + 10 + 4 * 5  # 880
    assert g["gain"] == 880 - 430
    # b (50), d (10), e (4 x 5) are owned with no transaction; f isn't owned.
    assert g["no_cost_count"] == 3
    assert g["no_cost_value"] == 50 + 10 + 20
    assert g["n_with_cost"] == 2  # a, c


def test_series_completion_counts_numbers_not_variants_and_says_what_it_covers(db_session):
    base = Set(series="Original", name="Base Set", total_cards=10)
    jungle = Set(series="Original", name="Jungle", total_cards=None)  # e.g. not synced
    db_session.add_all([base, jungle])
    db_session.flush()
    _card(db_session, "b1", 1, number="1/10", set_row=base)
    _card(db_session, "b1", 1, number="1/10", set_row=base, variant="Reverse Holo")  # same number
    _card(db_session, "b2", 1, number="2/10", set_row=base)
    _card(db_session, "b3", 1, number="3/10", set_row=base, qty=0)  # not owned
    _card(db_session, "j1", 1, number="1/64", set_row=jungle)
    db_session.commit()

    original = next(b for b in queries.by_series_breakdown(db_session) if b.name == "Original")
    base_b = next(s for s in original.child_sets if s.name == "Base Set")
    jungle_b = next(s for s in original.child_sets if s.name == "Jungle")

    assert base_b.unique_count == 3  # three owned rows...
    assert base_b.owned_numbers == 2  # ...but only numbers 1 and 2
    assert base_b.completion_pct == pytest.approx(20.0)
    assert jungle_b.completion_pct is None
    assert original.series_completion == {
        "pct": pytest.approx(20.0),
        "owned": 2,
        "total": 10,
        "sets_known": 1,
        "sets": 2,
    }


def test_series_completion_is_none_without_any_known_set_size(db_session):
    s = Set(series="Neo", name="Neo Genesis", total_cards=None)
    db_session.add(s)
    db_session.flush()
    _card(db_session, "n1", 1, number="1/111", set_row=s)
    db_session.commit()
    neo = queries.by_series_breakdown(db_session)[0]
    assert neo.series_completion is None


@pytest.mark.parametrize(
    "old,new,shown", [(3, 6, False), (100, 109.99, False), (100, 110, True), (500, 480, True)]
)
def test_price_move_pct_only_shown_from_10_kr(db_session, old, new, shown):
    move = queries.PriceMove(Card(card_id="x", name="X", variant="Normal", qty=1), old, new)
    assert move.show_pct is shown


# --------------------------------------------------------------------------
# Rendered on the Dashboard
# --------------------------------------------------------------------------
def _seed_dashboard(client):
    main = make_csv(
        "My Collection",
        [
            {"id": "a", "name": "Pikachu", "qty": 2, "price": "100"},
            {"id": "b", "name": "Charizard", "qty": 1, "price": "900"},
            {"id": "c", "name": "Bulbasaur", "qty": 1, "price": "10"},
        ],
    )
    vintage = make_csv("Vintage Collection", [{"id": "a"}])
    illustrator = make_csv("Tomokazu Komiya Collection", [{"id": "a"}, {"id": "b"}])
    seed_import(
        client,
        [
            ("files", ("main.csv", main, "text/csv")),
            ("files", ("vintage.csv", vintage, "text/csv")),
            ("files", ("illustrator.csv", illustrator, "text/csv")),
        ],
    )


def test_dashboard_shows_total_row_shared_badge_and_no_cost_line(client):
    _seed_dashboard(client)
    import db as db_module

    db = db_module.SessionLocal()
    charizard_id = db.query(Card).filter_by(name="Charizard").one().id
    db.close()
    client.post("/transactions", data={"card_id": charizard_id, "type": "purchase", "date": "2026-01-01", "price": "500"})

    html = client.get("/").text
    inventory = html[html.index('id="dashboard-inventory-card"'):]
    inventory = inventory[: inventory.index('id="dashboard-series-card"')]

    assert "Total" in inventory and "total-row" in inventory
    # Pikachu is in Vintage + Komiya: listed (and marked shared) in both.
    assert inventory.count("shared with 1") == 2
    # Pikachu (2 x 100) and Bulbasaur (10) have no purchase price.
    assert "No purchase price" in html
    assert "2 cards" in html and "210 kr" in html
    assert "Above / below cost" in html


def test_market_value_change_is_labelled_from_the_first_snapshot(client):
    import db as db_module
    import snapshots

    _seed_dashboard(client)
    first = dt.date.today() - dt.timedelta(days=5)
    db = db_module.SessionLocal()
    snapshots.record_daily_snapshot(db, as_of=first)
    db.close()

    html = client.get("/?period=all").text
    assert f"since first snapshot {first.strftime('%d.%m.%Y')}" in html
    assert "all time" not in html


# --------------------------------------------------------------------------
# /cron/set-sync
# --------------------------------------------------------------------------
def test_cron_set_sync_fills_total_cards_and_keeps_existing_rank(client, monkeypatch):
    import db as db_module
    import set_sync

    db = db_module.SessionLocal()
    db.add(Set(series="Original", name="Base Set", release_rank=40, total_cards=None))
    db.commit()
    db.close()
    monkeypatch.setattr(
        set_sync,
        "fetch_api_sets",
        lambda: [{"id": "base1", "name": "Base Set", "series": "Base", "releaseDate": "1999/01/09", "total": 102}],
    )

    resp = client.get("/cron/set-sync")
    assert resp.status_code == 200
    assert resp.json()["matched"] == 1

    db = db_module.SessionLocal()
    row = db.query(Set).filter_by(name="Base Set").one()
    assert (row.release_rank, row.total_cards) == (40, 102)
    db.close()


def test_cron_set_sync_requires_the_cron_secret_when_set(client, monkeypatch):
    monkeypatch.setenv("CRON_SECRET", "s3cret")
    assert client.get("/cron/set-sync").status_code == 401
