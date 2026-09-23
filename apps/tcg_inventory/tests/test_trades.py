"""Trade direction (In/Out) and the per-order trade summary -- see
Transaction.direction and queries.trade_summary."""
import datetime as dt

from conftest import make_csv, seed_import

import queries
from models import Card, CardSnapshot, Transaction


def _card(db, card_id, name, price):
    card = Card(card_id=card_id, name=name, variant="Normal", qty=1, reference_price=price)
    db.add(card)
    db.flush()
    return card


def _trade(db, card, direction, price=0.0, date=dt.date(2026, 9, 12), purchase_id=13):
    tx = Transaction(card_id=card.id, type="trade", direction=direction, date=date, price=price, purchase_id=purchase_id)
    db.add(tx)
    db.flush()
    return tx


# --- queries.trade_summary / trade_prices_at ------------------------------


def test_trade_summary_splits_gave_and_got_and_computes_gain(db_session):
    venusaur = _card(db_session, "v", "Mega Venusaur ex", 18.92)
    hypno = _card(db_session, "h", "Hypno", 11.79)
    slowbro = _card(db_session, "s", "Slowbro", 1.43)
    out = _trade(db_session, venusaur, "out")
    in1 = _trade(db_session, hypno, "in")
    in2 = _trade(db_session, slowbro, "in")

    t = queries.trade_summary([out, in1, in2])

    assert t["gave"] == [out]
    assert t["got"] == [in1, in2]
    assert t["value_out_now"] == 18.92
    assert round(t["value_in_now"], 2) == 13.22
    assert round(t["gain_now"], 2) == -5.70
    # No snapshot prices passed -> no "at trade date" figure.
    assert t["gain_then"] is None


def test_trade_summary_counts_cash_paid_and_received(db_session):
    a = _card(db_session, "a", "A", 100.0)
    b = _card(db_session, "b", "B", 60.0)
    # Gave A (worth 100) and received 30 cash, got B (worth 60) and paid 5.
    t = queries.trade_summary([_trade(db_session, a, "out", price=30), _trade(db_session, b, "in", price=5)])

    assert t["cash_received"] == 30
    assert t["cash_paid"] == 5
    assert t["gain_now"] == 60 - 100 + 30 - 5


def test_trade_summary_leaves_rows_without_direction_out_of_totals(db_session):
    a = _card(db_session, "a", "A", 10.0)
    b = _card(db_session, "b", "B", 50.0)
    legacy = _trade(db_session, b, None)

    t = queries.trade_summary([_trade(db_session, a, "in"), legacy])

    assert t["unknown"] == [legacy]
    assert t["gain_now"] == 10.0


def test_trade_summary_is_none_for_an_order_without_trades(db_session):
    a = _card(db_session, "a", "A", 10.0)
    tx = Transaction(card_id=a.id, type="purchase", date=dt.date(2026, 1, 1), price=10.0)
    db_session.add(tx)
    db_session.flush()

    assert queries.trade_summary([tx]) is None


def test_trade_prices_at_uses_latest_snapshot_on_or_before_trade_date(db_session):
    a = _card(db_session, "a", "A", 99.0)
    b = _card(db_session, "b", "B", 99.0)
    db_session.add_all(
        [
            CardSnapshot(card_id=a.id, date=dt.date(2026, 9, 1), qty=1, reference_price=10.0),
            CardSnapshot(card_id=a.id, date=dt.date(2026, 9, 10), qty=1, reference_price=12.0),
            CardSnapshot(card_id=a.id, date=dt.date(2026, 9, 20), qty=1, reference_price=50.0),  # after the trade
            CardSnapshot(card_id=b.id, date=dt.date(2026, 9, 20), qty=1, reference_price=7.0),  # only after
        ]
    )
    out = _trade(db_session, a, "out")
    in_ = _trade(db_session, b, "in")

    prices = queries.trade_prices_at(db_session, [out, in_])

    assert prices == {out.id: 12.0, in_.id: None}
    # One side without a trade-date price -> no "then" gain at all.
    assert queries.trade_summary([out, in_], prices)["gain_then"] is None


def test_trade_summary_gain_then_when_every_card_has_a_trade_date_price(db_session):
    a = _card(db_session, "a", "A", 99.0)
    b = _card(db_session, "b", "B", 99.0)
    out = _trade(db_session, a, "out")
    in_ = _trade(db_session, b, "in")

    t = queries.trade_summary([out, in_], {out.id: 20.0, in_.id: 25.0})

    assert t["value_out_then"] == 20.0
    assert t["value_in_then"] == 25.0
    assert t["gain_then"] == 5.0


# --- routes ---------------------------------------------------------------


def _seed_cards(client):
    import db as db_module

    main = make_csv("My Collection", [{"id": "a", "name": "Pikachu"}, {"id": "b", "name": "Charizard"}])
    seed_import(client, [("files", ("main.csv", main, "text/csv"))])
    db = db_module.SessionLocal()
    try:
        return {c.card_id: c.id for c in db.query(Card).all()}
    finally:
        db.close()


def _txs(purchase_id):
    import db as db_module

    db = db_module.SessionLocal()
    try:
        return {
            t.card_id: (t.type, t.direction)
            for t in db.query(Transaction).filter(Transaction.purchase_id == purchase_id).all()
        }
    finally:
        db.close()


def test_new_trade_order_stores_each_rows_direction(client):
    ids = _seed_cards(client)

    client.post(
        "/transactions/purchase",
        data={
            "type": "trade",
            "date": "2026-09-12",
            "purchase_id": "13",
            "card_id": [str(ids["a"]), str(ids["b"])],
            "price": ["0", "0"],
            "direction": ["out", "in"],
        },
    )

    assert _txs(13) == {ids["a"]: ("trade", "out"), ids["b"]: ("trade", "in")}


def test_new_purchase_order_ignores_direction(client):
    ids = _seed_cards(client)

    client.post(
        "/transactions/purchase",
        data={
            "type": "purchase",
            "date": "2026-09-12",
            "purchase_id": "3",
            "card_id": [str(ids["a"])],
            "price": ["10"],
            "direction": ["in"],
        },
    )

    assert _txs(3) == {ids["a"]: ("purchase", None)}


def test_edit_order_sets_direction_on_trade_rows_only(client):
    import db as db_module

    ids = _seed_cards(client)
    for cid in (ids["a"], ids["b"]):
        client.post("/transactions", data={"card_id": cid, "type": "trade", "date": "2026-09-12", "price": "0", "purchase_id": "13"})
    db = db_module.SessionLocal()
    rows = db.query(Transaction).filter(Transaction.purchase_id == 13).order_by(Transaction.id).all()
    tx_ids = [t.id for t in rows]
    card_ids = [t.card_id for t in rows]
    db.close()

    client.post(
        "/transactions/purchase/13/edit",
        data={
            "tx_id": [str(t) for t in tx_ids],
            "card_id": [str(c) for c in card_ids],
            "type": ["trade", "purchase"],
            "direction": ["out", "in"],
            "date": ["2026-09-12", "2026-09-12"],
            "price": ["0", "0"],
            "platform": ["", ""],
            "note": ["", ""],
            "new_purchase_id": ["13", "13"],
        },
    )

    # The second row became a purchase, so its "in" is dropped.
    assert _txs(13) == {card_ids[0]: ("trade", "out"), card_ids[1]: ("purchase", None)}


def test_edit_order_without_direction_field_still_saves(client):
    """A form that predates the direction field must keep working."""
    import db as db_module

    ids = _seed_cards(client)
    client.post("/transactions", data={"card_id": ids["a"], "type": "trade", "date": "2026-09-12", "price": "0", "purchase_id": "13"})
    db = db_module.SessionLocal()
    tx = db.query(Transaction).filter(Transaction.purchase_id == 13).one()
    db.close()

    response = client.post(
        "/transactions/purchase/13/edit",
        data={
            "tx_id": [str(tx.id)],
            "card_id": [str(ids["a"])],
            "type": ["trade"],
            "date": ["2026-09-12"],
            "price": ["0"],
            "platform": [""],
            "note": ["hello"],
            "new_purchase_id": ["13"],
        },
    )

    assert response.status_code in (200, 303)
    assert _txs(13) == {ids["a"]: ("trade", None)}


def test_single_row_edit_sets_trade_direction(client):
    import db as db_module

    ids = _seed_cards(client)
    client.post("/transactions", data={"card_id": ids["a"], "type": "trade", "date": "2026-09-12", "price": "0", "purchase_id": "13"})
    db = db_module.SessionLocal()
    tx_id = db.query(Transaction).one().id
    db.close()

    client.post(
        f"/transactions/{tx_id}",
        data={"date": "2026-09-12", "type": "trade", "direction": "in", "price": "0", "purchase_id": "13"},
    )

    assert _txs(13) == {ids["a"]: ("trade", "in")}


def test_transactions_page_shows_gave_and_got_for_a_trade_order(client):
    ids = _seed_cards(client)
    client.post(
        "/transactions/purchase",
        data={
            "type": "trade",
            "date": "2026-09-12",
            "purchase_id": "13",
            "card_id": [str(ids["a"]), str(ids["b"])],
            "price": ["0", "0"],
            "direction": ["out", "in"],
        },
    )

    html = client.get("/transactions").text

    assert "trade-summary" in html
    assert "Gave" in html and "Got" in html
    assert "Trade gain" in html
