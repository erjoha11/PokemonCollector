"""Issue #228 (b): Order IDs are assigned by the server when an order is
saved (never reserved when a form opens), and Edit order only ever edits or
deletes rows that belong to the order being edited."""

import datetime as dt
import threading
import time

from conftest import make_csv, seed_import

HX = {"HX-Request": "true"}


def _seed(client):
    import db as db_module
    from models import Card

    main = make_csv(
        "My Collection",
        [
            {"id": "a", "name": "Pikachu", "price": "10"},
            {"id": "b", "name": "Charizard", "price": "50"},
            {"id": "c", "name": "Blastoise", "price": "20"},
        ],
    )
    seed_import(client, [("files", ("main.csv", main, "text/csv"))])
    db = db_module.SessionLocal()
    try:
        return {c.card_id: c.id for c in db.query(Card).all()}
    finally:
        db.close()


def _listing(client, card_ids):
    import db as db_module
    from models import Listing

    client.post(
        "/sales/mark-listed",
        data={"card_id": [str(c) for c in card_ids], "title": "Lot", "description": "Selger.", "suggested_price": "30"},
    )
    db = db_module.SessionLocal()
    try:
        return db.query(Listing).order_by(Listing.id.desc()).first().id
    finally:
        db.close()


def _orders():
    """{purchase_id: sorted [(card_id, type)]} for every transaction."""
    import db as db_module
    from models import Transaction

    db = db_module.SessionLocal()
    try:
        out: dict = {}
        for t in db.query(Transaction).order_by(Transaction.id).all():
            out.setdefault(t.purchase_id, []).append((t.card_id, t.type))
        return {k: sorted(v) for k, v in out.items()}
    finally:
        db.close()


def _add_tx(card_id, purchase_id, type="purchase", price=10.0):
    import db as db_module
    from models import Transaction

    db = db_module.SessionLocal()
    try:
        tx = Transaction(card_id=card_id, type=type, date=dt.date(2026, 1, 1), price=price, purchase_id=purchase_id)
        db.add(tx)
        db.commit()
        return tx.id
    finally:
        db.close()


# --- Server-assigned Order IDs --------------------------------------------


def test_two_tab_race_cart_then_mark_sold_yields_two_distinct_orders(client):
    """The race from the issue: open the New Order cart (which used to
    reserve max+1), mark a listing sold in another tab (also max+1), then
    register the cart. The purchase must not merge into the sale's order."""
    ids = _seed(client)
    listing_id = _listing(client, [ids["a"]])

    # Tab 1: the cart opens -- no ID reserved.
    cart = client.get("/transactions/purchase/start")
    assert cart.status_code == 200
    assert 'name="purchase_id"' not in cart.text

    # Tab 2: the listing is marked sold first.
    sold = client.post(
        f"/listings/{listing_id}/mark-sold",
        data={"date": "2026-03-01", "card_id": [str(ids["a"])], "price": ["40"]},
    )
    assert sold.status_code in (200, 303)

    # Tab 1: the cart is registered -- including a stale purchase_id the
    # old cart form would have posted (1, which the sale now owns). Ignored.
    r = client.post(
        "/transactions/purchase",
        data={"type": "purchase", "date": "2026-03-02", "purchase_id": "1",
              "card_id": [str(ids["b"]), str(ids["c"])], "price": ["50", "20"]},
        headers=HX,
    )
    assert r.status_code == 200

    orders = _orders()
    assert len(orders) == 2
    assert orders[1] == [(ids["a"], "sale")]
    assert orders[2] == sorted([(ids["b"], "purchase"), (ids["c"], "purchase")])


def test_cart_ignores_a_posted_purchase_id_naming_an_existing_order(client):
    ids = _seed(client)
    _add_tx(ids["a"], 7)

    client.post(
        "/transactions/purchase",
        data={"type": "purchase", "date": "2026-03-02", "purchase_id": "7",
              "card_id": [str(ids["b"])], "price": ["50"]},
    )
    orders = _orders()
    assert orders[7] == [(ids["a"], "purchase")]
    assert orders[8] == [(ids["b"], "purchase")]


def test_order_id_allocation_is_serialized_until_commit(client):
    """Two saves racing in parallel threads: the second can't read
    max(purchase_id) until the first has committed, so they get distinct
    IDs. Without the lock, B would read the same max while A is still
    uncommitted."""
    import sys

    import db as db_module
    from models import Transaction

    app_module = sys.modules["app"]
    ids = _seed(client)
    a_inside, b_started = threading.Event(), threading.Event()
    got: dict = {}
    errors: list = []

    def save(name, card_id, before_commit=None):
        db = db_module.SessionLocal()
        try:
            with app_module._allocating_order_id(db) as pid:
                if before_commit:
                    before_commit()
                db.add(Transaction(card_id=card_id, type="purchase", date=dt.date(2026, 1, 1), price=1, purchase_id=pid))
                db.commit()
                got[name] = pid
        except Exception as exc:  # pragma: no cover - surfaced by the assert below
            errors.append(exc)
        finally:
            db.close()

    def a_hold():
        a_inside.set()
        b_started.wait(5)
        time.sleep(0.2)  # give B time to (try to) allocate while A is uncommitted

    def run_b():
        a_inside.wait(5)
        b_started.set()
        save("b", ids["b"])

    ta = threading.Thread(target=save, args=("a", ids["a"], a_hold))
    tb = threading.Thread(target=run_b)
    ta.start()
    tb.start()
    ta.join(10)
    tb.join(10)

    assert not errors
    assert got == {"a": 1, "b": 2}


def test_edit_order_start_new_order_assigns_one_fresh_id_on_save(client):
    """Edit order's "Start new order" no longer pre-fills a reserved number:
    it blanks the Order ID, and every blank row in the save moves into one
    new order whose ID is assigned at save time."""
    ids = _seed(client)
    keep = _add_tx(ids["a"], 1)
    split1 = _add_tx(ids["b"], 1)
    split2 = _add_tx(ids["c"], 1)

    page = client.get("/transactions/purchase/1/edit")
    assert page.status_code == 200
    assert "this.previousElementSibling.value = ''" in page.text

    # Another flow takes the next number in the meantime.
    _add_tx(ids["a"], 2, type="sale")

    r = client.post(
        "/transactions/purchase/1/edit",
        data={
            "tx_id": [str(keep), str(split1), str(split2)],
            "type": ["purchase"] * 3,
            "direction": [""] * 3,
            "date": ["2026-01-01"] * 3,
            "price": ["10", "10", "10"],
            "platform": [""] * 3,
            "note": [""] * 3,
            "card_id": [str(ids["a"]), str(ids["b"]), str(ids["c"])],
            "new_purchase_id": ["1", "", ""],
        },
        headers=HX,
    )
    assert r.status_code == 200
    orders = _orders()
    assert orders[1] == [(ids["a"], "purchase")]
    assert orders[2] == [(ids["a"], "sale")]
    assert orders[3] == sorted([(ids["b"], "purchase"), (ids["c"], "purchase")])


def test_edit_order_rejects_a_non_numeric_order_id(client):
    ids = _seed(client)
    tx = _add_tx(ids["a"], 1)
    r = client.post(
        "/transactions/purchase/1/edit",
        data={"tx_id": [str(tx)], "type": ["purchase"], "direction": [""], "date": ["2026-01-01"],
              "price": ["10"], "platform": [""], "note": [""], "card_id": [str(ids["a"])],
              "new_purchase_id": ["abc"]},
        headers=HX,
    )
    assert r.status_code == 422
    assert "Order ID on row 1" in r.text
    assert _orders() == {1: [(ids["a"], "purchase")]}


# --- update_purchase scoped to its own order ------------------------------


def _edit_data(rows, ids_by_tx):
    return {
        "tx_id": [str(t) for t in rows],
        "type": ["purchase"] * len(rows),
        "direction": [""] * len(rows),
        "date": ["2026-05-05"] * len(rows),
        "price": ["99"] * len(rows),
        "platform": ["Changed"] * len(rows),
        "note": ["changed"] * len(rows),
        "card_id": [str(ids_by_tx[t]) for t in rows],
        "new_purchase_id": ["1"] * len(rows),
    }


def _snapshot():
    import db as db_module
    from models import Transaction

    db = db_module.SessionLocal()
    try:
        return [
            (t.id, t.card_id, t.type, t.date, t.price, t.platform, t.note, t.purchase_id)
            for t in db.query(Transaction).order_by(Transaction.id).all()
        ]
    finally:
        db.close()


def test_update_purchase_rejects_a_cross_order_delete(client):
    ids = _seed(client)
    own = _add_tx(ids["a"], 1)
    other = _add_tx(ids["b"], 2)
    before = _snapshot()

    data = _edit_data([own], {own: ids["a"]})
    data["delete_tx_id"] = [str(other)]
    r = client.post("/transactions/purchase/1/edit", data=data, headers=HX)

    assert r.status_code == 422
    assert f"Transaction {other} belongs to order #2" in r.text
    assert _snapshot() == before  # other order intact, and nothing else written


def test_update_purchase_rejects_a_cross_order_edit(client):
    ids = _seed(client)
    own = _add_tx(ids["a"], 1)
    other = _add_tx(ids["b"], 2)
    unordered = _add_tx(ids["c"], None)
    before = _snapshot()

    for foreign in (other, unordered):
        data = _edit_data([own, foreign], {own: ids["a"], foreign: ids["b"]})
        r = client.post("/transactions/purchase/1/edit", data=data, headers=HX)
        assert r.status_code == 422
        assert f"Transaction {foreign} belongs to" in r.text
        assert "not order #1" in r.text
    assert _snapshot() == before


def test_update_purchase_still_deletes_and_edits_its_own_rows(client):
    ids = _seed(client)
    own = _add_tx(ids["a"], 1)
    gone = _add_tx(ids["b"], 1)
    other = _add_tx(ids["c"], 2)
    other_before = [row for row in _snapshot() if row[0] == other]

    data = _edit_data([own, gone], {own: ids["a"], gone: ids["b"]})
    data["delete_tx_id"] = [str(gone)]
    r = client.post("/transactions/purchase/1/edit", data=data, headers=HX)

    assert r.status_code == 200
    after = _snapshot()
    assert [row for row in after if row[0] == own] == [
        (own, ids["a"], "purchase", dt.date(2026, 5, 5), 99.0, "Changed", "changed", 1)
    ]
    assert not [row for row in after if row[0] == gone]
    assert [row for row in after if row[0] == other] == other_before


def test_update_purchase_skips_an_already_deleted_row(client):
    """A row deleted in another tab (id gone entirely) is skipped as
    before, not rejected -- it isn't another order's row."""
    ids = _seed(client)
    own = _add_tx(ids["a"], 1)
    data = _edit_data([own], {own: ids["a"]})
    data["delete_tx_id"] = ["999999"]
    r = client.post("/transactions/purchase/1/edit", data=data, headers=HX)
    assert r.status_code == 200
    assert _orders() == {1: [(ids["a"], "purchase")]}
