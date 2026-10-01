"""Order history's Gain column (issue #246) -- per-order paper gain/loss:
today's value of the order's still-owned copies minus the Total the row
shows (typed, else auto Value + Shipping). See queries.order_gain."""
import datetime as dt

from conftest import make_csv, seed_import
from test_app import order_summary, summary_cell


def _seed(client, rows):
    """Import `rows` (make_csv dicts) and return {dex id: Card.id}."""
    import db as db_module
    from models import Card

    seed_import(client, [("files", ("main.csv", make_csv("My Collection", rows), "text/csv"))])
    db = db_module.SessionLocal()
    ids = {c.card_id: c.id for c in db.query(Card).all()}
    db.close()
    return ids


def _add(*txs):
    import db as db_module

    db = db_module.SessionLocal()
    db.add_all(list(txs))
    db.commit()
    db.close()


def _tx(card_id, purchase_id, price, type="purchase", date=dt.date(2026, 1, 1), **kw):
    from models import Transaction

    return Transaction(card_id=card_id, type=type, date=date, price=price, purchase_id=purchase_id, **kw)


def test_header_shows_gain_after_total_and_no_remaining(client):
    ids = _seed(client, [{"id": "a", "price": "100"}])
    _add(_tx(ids["a"], 1, 30))
    text = client.get("/transactions").text
    head = text.split('class="orders-row orders-head"', 1)[1].split("</div>", 1)[0]
    assert '<span class="oc-total num">Total</span>\n      <span class="oc-gain num">Gain</span>' in head
    assert "Remaining" not in head
    assert "oc-remaining" not in order_summary(text, 1)


def test_positive_gain_uses_auto_total_and_market_value(client):
    # Worth 100 today, paid 30 + 10 shipping (auto Total 40) -> +60.
    ids = _seed(client, [{"id": "a", "price": "100"}])
    _add(_tx(ids["a"], 1, 30, purchase_shipping=10))
    text = client.get("/transactions").text
    assert summary_cell(text, 1, "total") == "40 kr auto"
    assert summary_cell(text, 1, "gain") == "+60 kr"
    assert "viz-delta-gain" in order_summary(text, 1)


def test_negative_gain_uses_the_typed_total(client):
    # A typed Total (150) overrides Value + Shipping as the order's cost.
    ids = _seed(client, [{"id": "a", "price": "100"}])
    _add(_tx(ids["a"], 2, 100, purchase_total=150))
    text = client.get("/transactions").text
    assert summary_cell(text, 2, "gain") == "-50 kr"
    assert "viz-delta-loss" in order_summary(text, 2)


def test_gain_is_a_dash_when_no_card_has_a_market_price(client):
    import db as db_module
    from models import Card

    ids = _seed(client, [{"id": "a", "price": "100"}])
    db = db_module.SessionLocal()
    card = db.get(Card, ids["a"])
    card.market_price = None
    card.price_flags = "no_price"
    db.commit()
    db.close()
    _add(_tx(ids["a"], 3, 30))
    text = client.get("/transactions").text
    assert summary_cell(text, 3, "gain") == "—"
    assert "viz-delta" not in order_summary(text, 3)


def test_partially_priced_order_counts_unpriced_as_zero_and_flags_it(client):
    import db as db_module
    from models import Card

    ids = _seed(client, [{"id": "a", "price": "100"}, {"id": "b", "price": "50"}])
    db = db_module.SessionLocal()
    card = db.get(Card, ids["b"])
    card.market_price = None
    card.price_flags = "no_price"
    db.commit()
    db.close()
    _add(_tx(ids["a"], 4, 20), _tx(ids["b"], 4, 20))
    text = client.get("/transactions").text
    assert summary_cell(text, 4, "gain") == "+60 kr *"
    assert "1 card without a market price counted as 0." in order_summary(text, 4)


def test_sale_order_shows_a_dash(client):
    ids = _seed(client, [{"id": "a", "price": "100", "qty": 0}])
    _add(_tx(ids["a"], 5, 80, type="sale"))
    text = client.get("/transactions").text
    assert summary_cell(text, 5, "gain") == "—"
    assert "Sale order" in order_summary(text, 5)


def test_copy_no_longer_owned_contributes_no_value(client):
    # Bought for 40, since sold/traded away (qty 0): nothing left to value,
    # so the order is down by what it cost.
    ids = _seed(client, [{"id": "a", "price": "100", "qty": 0}])
    _add(_tx(ids["a"], 6, 40))
    text = client.get("/transactions").text
    assert summary_cell(text, 6, "gain") == "-40 kr"


def test_held_copies_are_credited_to_the_newest_orders(client):
    # Bought in two orders but only one copy left: disposals are treated as
    # oldest-first, so the newer order keeps the copy's value.
    ids = _seed(client, [{"id": "a", "price": "100", "qty": 1}])
    _add(
        _tx(ids["a"], 7, 30, date=dt.date(2026, 1, 1)),
        _tx(ids["a"], 8, 50, date=dt.date(2026, 2, 1)),
    )
    text = client.get("/transactions").text
    assert summary_cell(text, 7, "gain") == "-30 kr"
    assert summary_cell(text, 8, "gain") == "+50 kr"


def test_trade_order_gain_is_the_trade_gain(client):
    # Gave a 40 kr card, got a 100 kr card plus 10 kr cash -> +70, the same
    # figure as the expanded order's "Trade gain".
    ids = _seed(client, [{"id": "a", "price": "40", "qty": 0}, {"id": "b", "price": "100"}])
    _add(
        _tx(ids["a"], 9, 10, type="trade", direction="out"),
        _tx(ids["b"], 9, 0, type="trade", direction="in"),
    )
    text = client.get("/transactions").text
    assert summary_cell(text, 9, "gain") == "+70 kr"


def test_trade_without_directions_shows_a_dash(client):
    ids = _seed(client, [{"id": "a", "price": "40"}])
    _add(_tx(ids["a"], 10, 0, type="trade"))
    text = client.get("/transactions").text
    assert summary_cell(text, 10, "gain") == "—"
