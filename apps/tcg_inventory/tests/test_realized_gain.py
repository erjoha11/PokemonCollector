"""Sold tab realized gain (issue #256): each sale's net proceeds minus the
cost of the copy it used up, taken FIFO -- the oldest acquisition (purchase,
ripped, trade "in") on or before the sale date that no earlier disposal has
used. Computed at page load, never stored. See queries.realized_gains."""
import datetime as dt

import queries
from conftest import make_csv, seed_import
from models import Listing, Transaction
from test_app import order_summary, summary_cell

D = dt.date


def _t(id, type, date, price=0.0, card_id=1, **kw):
    """A detached Transaction for the pure-function tests -- realized_gains
    only reads attributes, so no session is needed."""
    return Transaction(id=id, card_id=card_id, type=type, date=date, price=price, **kw)


def _gains(txs):
    return queries.realized_gains(txs, queries.shipping_shares(txs))


# --- queries.realized_gains ------------------------------------------------


def test_duplicates_bought_on_different_dates_are_used_oldest_first():
    txs = [
        _t(1, "purchase", D(2026, 1, 1), 10),
        _t(2, "purchase", D(2026, 2, 1), 30),
        _t(3, "sale", D(2026, 3, 1), 50),
        _t(4, "sale", D(2026, 4, 1), 50),
    ]
    gains = _gains(txs)
    assert gains[3].source.id == 1 and gains[3].cost == 10 and gains[3].gain == 40
    assert gains[4].source.id == 2 and gains[4].cost == 30 and gains[4].gain == 20


def test_order_of_rows_in_the_list_does_not_matter():
    txs = [
        _t(4, "sale", D(2026, 4, 1), 50),
        _t(2, "purchase", D(2026, 2, 1), 30),
        _t(3, "sale", D(2026, 3, 1), 50),
        _t(1, "purchase", D(2026, 1, 1), 10),
    ]
    gains = _gains(txs)
    assert (gains[3].cost, gains[4].cost) == (10, 30)


def test_sale_with_no_matching_acquisition_has_unknown_cost():
    gains = _gains([_t(1, "sale", D(2026, 3, 1), 50)])
    assert gains[1].source is None
    assert gains[1].cost is None and gains[1].gain is None
    assert gains[1].reason == "no_acquisition"
    assert gains[1].proceeds == 50


def test_copy_acquired_after_the_sale_date_is_never_used():
    txs = [_t(1, "sale", D(2026, 3, 1), 50), _t(2, "purchase", D(2026, 5, 1), 10)]
    assert _gains(txs)[1].reason == "no_acquisition"


def test_ripped_card_costs_zero():
    txs = [_t(1, "ripped", D(2026, 1, 1)), _t(2, "sale", D(2026, 2, 1), 80)]
    gains = _gains(txs)
    assert gains[2].cost == 0 and gains[2].gain == 80


def test_unpriced_purchase_and_traded_in_copy_have_unknown_cost():
    unpriced = _gains([_t(1, "purchase", D(2026, 1, 1), 0), _t(2, "sale", D(2026, 2, 1), 50)])
    assert unpriced[2].cost is None and unpriced[2].reason == "unpriced"

    traded = _gains([_t(1, "trade", D(2026, 1, 1), direction="in"), _t(2, "sale", D(2026, 2, 1), 50)])
    assert traded[2].cost is None and traded[2].reason == "trade"


def test_cost_and_proceeds_include_fees_and_shipping_shares():
    # Bought for 100 + 5 fees + 10 shipping (alone in its order) = 115.
    # Sold for 200 - 20 fees - 15 seller-paid shipping = 165 net.
    txs = [
        _t(1, "purchase", D(2026, 1, 1), 100, fees=5, purchase_id=1, purchase_shipping=10),
        _t(2, "sale", D(2026, 2, 1), 200, fees=20, purchase_id=2, purchase_shipping=15),
    ]
    gains = _gains(txs)
    assert gains[2].cost == 115
    assert gains[2].proceeds == 165
    assert gains[2].gain == 50


def test_trade_out_uses_up_a_copy_before_a_later_sale():
    txs = [
        _t(1, "purchase", D(2026, 1, 1), 10),
        _t(2, "purchase", D(2026, 2, 1), 30),
        _t(3, "trade", D(2026, 3, 1), direction="out"),
        _t(4, "sale", D(2026, 4, 1), 50),
    ]
    gains = _gains(txs)
    assert 3 not in gains  # only sales realize cash
    assert gains[4].source.id == 2 and gains[4].cost == 30


def test_copies_are_matched_per_card():
    txs = [
        _t(1, "purchase", D(2026, 1, 1), 10, card_id=1),
        _t(2, "purchase", D(2026, 1, 1), 99, card_id=2),
        _t(3, "sale", D(2026, 2, 1), 50, card_id=2),
    ]
    assert _gains(txs)[3].cost == 99


def test_fifo_matches_held_acquisition_ids():
    # Bought twice, sold once, qty now 1: the purchase side treats the newer
    # copy as held, so the sale must be costed against the older one.
    from models import Card

    card = Card(id=1, qty=1)
    old = _t(1, "purchase", D(2026, 1, 1), 10)
    new = _t(2, "purchase", D(2026, 2, 1), 30)
    sale = _t(3, "sale", D(2026, 3, 1), 50)
    for t in (old, new, sale):
        t.card = card
    assert queries.held_acquisition_ids([old, new, sale]) == {2}
    assert _gains([old, new, sale])[3].source is old


def test_sum_realized_leaves_unknown_costs_out():
    txs = [
        _t(1, "purchase", D(2026, 1, 1), 10),
        _t(2, "sale", D(2026, 2, 1), 50),
        _t(3, "sale", D(2026, 2, 1), 70, card_id=2),
    ]
    gains = _gains(txs)
    assert queries.sum_realized([gains[2], gains[3]]) == {"cost": 10, "gain": 40, "unknown": 1}
    assert queries.sum_realized([gains[3]]) == {"cost": 0, "gain": None, "unknown": 1}


# --- Sold tab --------------------------------------------------------------


def _seed(client):
    import db as db_module
    from models import Card

    rows = [{"id": "a", "name": "Pikachu", "price": "100"}, {"id": "b", "name": "Eevee", "price": "20"}]
    seed_import(client, [("files", ("main.csv", make_csv("My Collection", rows), "text/csv"))])
    db = db_module.SessionLocal()
    ids = {c.card_id: c.id for c in db.query(Card).all()}
    db.close()
    return ids


def _add(*objs):
    import db as db_module

    db = db_module.SessionLocal()
    db.add_all(list(objs))
    db.commit()
    db.close()


def _row(card_id, type, date, price, purchase_id=None, **kw):
    return Transaction(card_id=card_id, type=type, date=date, price=price, purchase_id=purchase_id, **kw)


def test_sold_row_shows_cost_basis_and_signed_realized_gain(client):
    ids = _seed(client)
    _add(
        _row(ids["a"], "purchase", D(2026, 1, 1), 10, 1),
        _row(ids["a"], "purchase", D(2026, 2, 1), 30, 2),
        _row(ids["a"], "sale", D(2026, 3, 1), 50, 3, fees=5),
    )
    text = client.get("/orders/sold").text
    assert summary_cell(text, 3, "value") == "50 kr"
    assert summary_cell(text, 3, "fees") == "5 kr"
    assert summary_cell(text, 3, "cost") == "10 kr"
    assert summary_cell(text, 3, "gain") == "+35 kr"
    assert "viz-delta-gain" in order_summary(text, 3)


def test_loss_is_shown_with_a_minus_sign(client):
    ids = _seed(client)
    _add(_row(ids["a"], "purchase", D(2026, 1, 1), 80, 1), _row(ids["a"], "sale", D(2026, 2, 1), 50, 2))
    text = client.get("/orders/sold").text
    assert summary_cell(text, 2, "gain") == "-30 kr"
    assert "viz-delta-loss" in order_summary(text, 2)


def test_sale_with_no_acquisition_shows_unknown_cost_not_full_proceeds(client):
    ids = _seed(client)
    _add(_row(ids["a"], "sale", D(2026, 2, 1), 50, 7))
    text = client.get("/orders/sold").text
    assert summary_cell(text, 7, "gain") == "unknown cost"
    assert summary_cell(text, 7, "cost") == "—"
    assert "+50 kr" not in text


def test_partly_unknown_order_flags_its_gain(client):
    ids = _seed(client)
    _add(
        _row(ids["a"], "ripped", D(2026, 1, 1), 0, 1),
        _row(ids["a"], "sale", D(2026, 2, 1), 40, 2),
        _row(ids["b"], "sale", D(2026, 2, 1), 10, 2),
    )
    text = client.get("/orders/sold").text
    assert summary_cell(text, 2, "gain") == "+40 kr *"
    assert "1 card with unknown cost left out" in order_summary(text, 2)


def test_header_shows_total_realized_gain(client):
    ids = _seed(client)
    _add(
        _row(ids["a"], "purchase", D(2026, 1, 1), 10, 1),
        _row(ids["b"], "purchase", D(2026, 1, 1), 5, 1),
        _row(ids["a"], "sale", D(2026, 2, 1), 50, 2),
        _row(ids["b"], "sale", D(2026, 3, 1), 3),  # individually registered: -2
    )
    text = client.get("/orders/sold").text
    head = text.split('class="section-summary"', 1)[1].split("</span>\n      <a", 1)[0]
    assert "Realized gain" in head
    assert "+38 kr" in head


def test_expanded_sale_lists_the_copy_used_per_card(client):
    ids = _seed(client)
    _add(_row(ids["a"], "purchase", D(2026, 1, 5), 10, 4), _row(ids["a"], "sale", D(2026, 2, 1), 50, 9))
    text = client.get("/orders/sold").text
    body = text.split('id="order-9"', 1)[1].split("</details>", 1)[0]
    assert "realized-table" in body
    assert "Purchase 2026-01-05" in body
    assert 'href="/orders/purchased?open_order=4#order-4"' in body


def test_individually_registered_sale_gets_a_breakdown_too(client):
    ids = _seed(client)
    _add(_row(ids["a"], "ripped", D(2026, 1, 1), 0), _row(ids["a"], "sale", D(2026, 2, 1), 25))
    text = client.get("/orders/sold").text
    ungrouped = text.split('class="ungrouped-section"', 1)[1]
    assert "realized-table" in ungrouped
    assert "Ripped 2026-01-01" in ungrouped


def test_sold_row_links_its_listing(client):
    import db as db_module

    ids = _seed(client)
    db = db_module.SessionLocal()
    listing = Listing(
        created_at=dt.datetime(2026, 1, 1), title="Pikachu lot", description="", status="sold"
    )
    db.add(listing)
    db.flush()
    db.add(_row(ids["a"], "sale", D(2026, 2, 1), 50, 5, listing_id=listing.id))
    db.commit()
    listing_id = listing.id
    db.close()
    text = client.get("/orders/sold").text
    assert summary_cell(text, 5, "listing") == "Pikachu lot"
    assert f'href="/orders/listings?sold_only=true#listing-{listing_id}"' in order_summary(text, 5)


def test_realized_gain_is_never_stored(client):
    from models import Transaction as T

    assert not any("realized" in c.name or "cost_basis" in c.name for c in T.__table__.columns)
