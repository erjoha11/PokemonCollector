"""Issue #254: a sale counts at net proceeds (price - fees - its share of
seller-paid shipping) in every Net invested figure, and Mark sold / the New
Order cart capture order-level fees and shipping."""

import datetime as dt

import pytest
from conftest import make_csv, seed_import

import queries
from importer import import_dex_csv_files
from models import Card, Transaction


def _cards(db_session, n=3):
    rows = [{"id": c, "name": f"Card {c}"} for c in "abcdefgh"[:n]]
    import_dex_csv_files(db_session, [("main.csv", make_csv("My Collection", rows))])
    return {c.card_id: c.id for c in db_session.query(Card).all()}


def _assert_invariant(db_session, expected_net):
    summary = queries.economic_summary(db_session)
    by_card = queries.net_invested_by_card(db_session)
    assert summary["net_invested"] == pytest.approx(expected_net)
    assert sum(by_card.values()) == pytest.approx(summary["net_invested"])
    txs = db_session.query(Transaction).all()
    last = max(t.date for t in txs)
    assert queries.net_invested_at_dates(txs, [last]) == [pytest.approx(expected_net)]
    flow = queries.cash_flow_by_month(db_session)
    assert flow[-1]["cumulative_invested"] == pytest.approx(expected_net)
    return summary, by_card


def test_sale_fees_reduce_proceeds(db_session):
    ids = _cards(db_session, 1)
    db_session.add(Transaction(card_id=ids["a"], type="purchase", date=dt.date(2026, 1, 5), price=100))
    db_session.add(Transaction(card_id=ids["a"], type="sale", date=dt.date(2026, 2, 1), price=150, fees=12))
    db_session.commit()

    summary, by_card = _assert_invariant(db_session, 100 - (150 - 12))
    assert summary["total_bought"] == 100
    assert summary["total_sold"] == 138
    assert by_card[ids["a"]] == pytest.approx(-38)
    flow = queries.cash_flow_by_month(db_session)
    assert flow[1]["sold"] == 138


def test_sale_shipping_reduces_proceeds(db_session):
    ids = _cards(db_session, 1)
    db_session.add(Transaction(card_id=ids["a"], type="purchase", date=dt.date(2026, 1, 5), price=100))
    db_session.add(
        Transaction(card_id=ids["a"], type="sale", date=dt.date(2026, 2, 1), price=150, purchase_id=7, purchase_shipping=40)
    )
    db_session.commit()

    summary, _ = _assert_invariant(db_session, 100 - 110)
    assert summary["total_sold"] == 110


def test_ungrouped_sale_row_carries_its_own_shipping(db_session):
    ids = _cards(db_session, 1)
    db_session.add(
        Transaction(card_id=ids["a"], type="sale", date=dt.date(2026, 2, 1), price=50, fees=5, purchase_shipping=20)
    )
    db_session.commit()
    summary, _ = _assert_invariant(db_session, -25)
    assert summary["total_sold"] == 25


def test_mixed_history_multi_row_sale_order_splits_shipping_by_price(db_session):
    ids = _cards(db_session, 3)
    # Purchase order #1: 100 + 200 with 30 shipping and 6 fees on one row.
    db_session.add(Transaction(card_id=ids["a"], type="purchase", date=dt.date(2026, 1, 5), price=100, fees=6,
                               purchase_id=1, purchase_shipping=30))
    db_session.add(Transaction(card_id=ids["b"], type="purchase", date=dt.date(2026, 1, 5), price=200,
                               purchase_id=1, purchase_shipping=30))
    # Sale order #2: two cards, 60 + 20, 16 shipping (split 12 : 4) and fees.
    sale_a = Transaction(card_id=ids["a"], type="sale", date=dt.date(2026, 3, 1), price=60, fees=3,
                         purchase_id=2, purchase_shipping=16)
    sale_c = Transaction(card_id=ids["c"], type="sale", date=dt.date(2026, 3, 1), price=20, fees=1,
                         purchase_id=2, purchase_shipping=16)
    db_session.add_all([sale_a, sale_c])
    # A trade row in the history never carries shipping or fees.
    db_session.add(Transaction(card_id=ids["c"], type="trade", direction="in", date=dt.date(2026, 2, 1), price=0,
                               fees=99, purchase_id=3, purchase_shipping=50))
    db_session.commit()

    shares = queries.shipping_shares(db_session.query(Transaction).all())
    assert shares[sale_a.id] == pytest.approx(12)
    assert shares[sale_c.id] == pytest.approx(4)

    bought = 100 + 6 + 200 + 30
    sold = (60 - 3 - 12) + (20 - 1 - 4)
    summary, by_card = _assert_invariant(db_session, bought - sold)
    assert summary["total_bought"] == pytest.approx(bought)
    assert summary["total_sold"] == pytest.approx(sold)
    assert by_card[ids["a"]] == pytest.approx(100 + 6 + 10 - 45)
    assert by_card[ids["b"]] == pytest.approx(200 + 20)
    assert by_card[ids["c"]] == pytest.approx(-15)


def test_sale_shipping_split_evenly_when_no_row_is_priced():
    rows = [
        Transaction(id=i, card_id=i, type="sale", date=dt.date(2026, 1, 1), price=0, purchase_id=9, purchase_shipping=9)
        for i in (1, 2, 3)
    ]
    assert queries.shipping_shares(rows) == {1: 3, 2: 3, 3: 3}


def test_purchase_shipping_total_counts_sale_orders_once_when_asked():
    rows = [
        Transaction(id=i, card_id=i, type="sale", date=dt.date(2026, 1, 1), price=10, purchase_id=4, purchase_shipping=25)
        for i in (1, 2)
    ]
    assert queries._purchase_shipping_total(rows) == 0
    assert queries._purchase_shipping_total(rows, ("sale",)) == 25


def test_split_order_fees_by_price_whole_ore_and_exact_total():
    import app as app_module

    assert app_module._split_order_fees(None, [10, 20]) == [None, None]
    assert app_module._split_order_fees(0, [10, 20]) == [None, None]
    assert app_module._split_order_fees(9, [10, 20]) == [3.0, 6.0]
    split = app_module._split_order_fees(10, [1, 1, 1])
    assert split == [3.34, 3.33, 3.33]
    assert round(sum(split), 2) == 10
    # Nothing priced -> even split; a 0-price row among priced rows gets none,
    # and no share is ever negative.
    assert app_module._split_order_fees(0.03, [1, 1, 0]) == [0.02, 0.01, 0.0]
    assert app_module._split_order_fees(4, [0, 0]) == [2.0, 2.0]


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------
def _seed_client_cards(client):
    import db as db_module

    main = make_csv("My Collection", [{"id": "a", "name": "Pikachu", "price": "10"},
                                      {"id": "b", "name": "Charizard", "price": "50"}])
    seed_import(client, [("files", ("main.csv", main, "text/csv"))])
    db = db_module.SessionLocal()
    try:
        return {c.card_id: c.id for c in db.query(Card).all()}
    finally:
        db.close()


def _listing(client, card_ids):
    import db as db_module
    from models import Listing

    client.post("/sales/mark-listed", data={"card_id": [str(c) for c in card_ids], "title": "Lot",
                                             "description": "x", "suggested_price": "40"})
    db = db_module.SessionLocal()
    try:
        return db.query(Listing).one().id
    finally:
        db.close()


def test_mark_sold_form_has_fees_and_shipping_fields(client):
    ids = _seed_client_cards(client)
    listing_id = _listing(client, [ids["a"]])
    text = client.get(f"/listings/{listing_id}/mark-sold").text
    assert 'name="fees"' in text
    assert 'name="shipping"' in text


def test_mark_sold_persists_fees_and_shipping_and_counts_net_proceeds(client):
    import db as db_module

    ids = _seed_client_cards(client)
    listing_id = _listing(client, [ids["a"], ids["b"]])
    response = client.post(
        f"/listings/{listing_id}/mark-sold",
        data={"date": "2026-02-01", "platform": "finn.no", "card_id": [str(ids["a"]), str(ids["b"])],
              "price": ["10", "30"], "fees": "8", "shipping": "20"},
        follow_redirects=False,
    )
    assert response.status_code == 303

    db = db_module.SessionLocal()
    try:
        txs = {t.card_id: t for t in db.query(Transaction).filter(Transaction.listing_id == listing_id).all()}
        assert txs[ids["a"]].fees == 2.0 and txs[ids["b"]].fees == 6.0
        assert all(t.purchase_shipping == 20 for t in txs.values())
        summary = queries.economic_summary(db)
        assert summary["total_sold"] == pytest.approx(40 - 8 - 20)
        assert sum(queries.net_invested_by_card(db).values()) == pytest.approx(summary["net_invested"])
    finally:
        db.close()


def test_mark_sold_blank_fees_and_shipping_store_nothing(client):
    import db as db_module

    ids = _seed_client_cards(client)
    listing_id = _listing(client, [ids["a"]])
    client.post(f"/listings/{listing_id}/mark-sold",
                data={"date": "2026-02-01", "card_id": [str(ids["a"])], "price": ["10"], "fees": "", "shipping": ""})
    db = db_module.SessionLocal()
    try:
        tx = db.query(Transaction).one()
        assert tx.fees is None and tx.purchase_shipping is None
    finally:
        db.close()


def test_mark_sold_rejects_negative_fees_without_writing(client):
    import db as db_module
    from models import Listing

    ids = _seed_client_cards(client)
    listing_id = _listing(client, [ids["a"]])
    response = client.post(f"/listings/{listing_id}/mark-sold",
                           data={"date": "2026-02-01", "card_id": [str(ids["a"])], "price": ["10"], "fees": "-5"})
    assert response.status_code == 422
    assert "Fees must be blank or a number of 0 or more" in response.text
    db = db_module.SessionLocal()
    try:
        assert db.query(Transaction).count() == 0
        assert db.query(Listing).one().status != "sold"
    finally:
        db.close()


def test_cart_sale_persists_fees_and_shipping(client):
    import db as db_module

    ids = _seed_client_cards(client)
    response = client.post(
        "/transactions/purchase",
        data={"type": "sale", "date": "2026-03-01", "purchase_shipping": "15", "fees": "5",
              "card_id": [str(ids["a"]), str(ids["b"])], "price": ["25", "75"]},
        follow_redirects=True,
    )
    assert response.status_code == 200
    db = db_module.SessionLocal()
    try:
        txs = {t.card_id: t for t in db.query(Transaction).filter(Transaction.purchase_id == 1).all()}
        assert txs[ids["a"]].fees == 1.25 and txs[ids["b"]].fees == 3.75
        assert all(t.purchase_shipping == 15 for t in txs.values())
        assert queries.economic_summary(db)["total_sold"] == pytest.approx(100 - 5 - 15)
    finally:
        db.close()


def test_cart_ignores_fees_on_trade_orders(client):
    import db as db_module

    ids = _seed_client_cards(client)
    client.post(
        "/transactions/purchase",
        data={"type": "trade", "date": "2026-03-01", "purchase_id": "12", "fees": "5",
              "card_id": [str(ids["a"])], "price": ["0"], "direction": ["in"]},
    )
    db = db_module.SessionLocal()
    try:
        assert db.query(Transaction).one().fees is None
    finally:
        db.close()


def test_cart_has_fees_field(client):
    text = client.get("/transactions/purchase/start?type=sale").text
    assert 'name="fees"' in text


def test_order_gain_ignores_sale_orders_even_with_fees_and_shipping():
    # #246's per-order Gain never values sale orders, so net proceeds don't
    # feed into it -- sale fees/shipping only move the headline figures.
    rows = [
        Transaction(id=1, card_id=1, type="sale", date=dt.date(2026, 1, 1), price=50, fees=5,
                    purchase_id=3, purchase_shipping=10),
    ]
    assert queries.order_gain(rows, 60, set(), None)["reason"] == "sale"
