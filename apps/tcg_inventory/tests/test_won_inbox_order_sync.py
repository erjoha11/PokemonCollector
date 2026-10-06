"""Facebook wins items keep pointing at their order (#317): Edit order's
move/merge/split carries `won_items.purchase_id` along, "order missing"
covers registered items too, and a sale's leftovers get a prefilled Total
that leaves out what its earlier order(s) already count."""
from __future__ import annotations

import datetime as dt
import json

import db as db_module
import won_inbox
from models import Card, Transaction, WonItem

POST = "https://www.facebook.com/groups/somegroup/posts/1001/"
OTHER_POST = "https://www.facebook.com/groups/somegroup/posts/1002/"
SALE_END = dt.date(2026, 10, 3)


def _item(n: int, post: str = POST, **over) -> dict:
    post_id = post.rstrip("/").rsplit("/", 1)[1]
    item = {
        "external_ref": f"fbaw:{post_id}:{2000 + n}",
        "seller": "Seller One",
        "sale_type": "auction",
        "ended_on": SALE_END.isoformat(),
        "post_url": post,
        "lot_url": f"{post}?comment_id={2000 + n}",
        "label": f"{n}. Card {n}",
        "price": 100 + n,
        "shipping_text": None,
        "payment_text": None,
        "paid_at": None,
        "received_at": None,
    }
    item.update(over)
    return item


def _send(client, items: list[dict]) -> dict[str, int]:
    body = {"format": "fbaw-won", "version": 1, "sent_at": "2026-10-04T10:00:00Z", "items": items}
    r = client.post("/inbox/fb-wins", content=json.dumps(body), headers={"Content-Type": "application/json"})
    assert r.status_code == 200, r.text
    with db_module.SessionLocal() as s:
        return {w.external_ref: w.id for w in s.query(WonItem)}


def _card(name: str) -> int:
    with db_module.SessionLocal() as s:
        card = Card(card_id=name.lower(), name=name, set="Some Set", qty=1)
        s.add(card)
        s.commit()
        return card.id


def _won(item_id: int) -> WonItem:
    with db_module.SessionLocal() as s:
        row = s.get(WonItem, item_id)
        s.expunge(row)
        return row


def _register(client, rows, **extra) -> int:
    """rows: (card_id, price, note, won_item_id). Returns the new order's ID."""
    data = {
        "type": "purchase",
        "date": SALE_END.isoformat(),
        "platform": "Facebook",
        "card_id": [str(r[0]) for r in rows],
        "price": [r[1] for r in rows],
        "note": [r[2] for r in rows],
        "won_item_id": [r[3] for r in rows],
        **extra,
    }
    r = client.post("/transactions/purchase", data=data, follow_redirects=False)
    assert r.status_code == 303, r.text
    with db_module.SessionLocal() as s:
        return s.query(Transaction.purchase_id).order_by(Transaction.id.desc()).first()[0]


def _rows(purchase_id: int) -> list[Transaction]:
    with db_module.SessionLocal() as s:
        txs = s.query(Transaction).filter(Transaction.purchase_id == purchase_id).order_by(Transaction.id).all()
        s.expunge_all()
        return txs


def _edit(client, purchase_id: int, targets: dict[int, str], delete: list[int] = (), notes: dict[int, str] | None = None):
    """Posts Edit order for every row of the order: `targets` maps tx id ->
    its Order ID field ("" = Start new order); rows not in it stay."""
    txs = _rows(purchase_id)
    notes = notes or {}
    data = {
        "tx_id": [str(t.id) for t in txs],
        "type": [t.type for t in txs],
        "date": [t.date.isoformat() for t in txs],
        "price": [str(t.price) for t in txs],
        "platform": [t.platform or "" for t in txs],
        "note": [notes.get(t.id, t.note or "") for t in txs],
        "card_id": [str(t.card_id) for t in txs],
        "new_purchase_id": [targets.get(t.id, str(purchase_id)) for t in txs],
        "delete_tx_id": [str(d) for d in delete],
    }
    r = client.post(f"/transactions/purchase/{purchase_id}/edit", data=data, follow_redirects=False)
    assert r.status_code == 303, r.text


def _two_sales(client):
    """Two sales from one seller, each registered as its own order."""
    a, b, c = _card("Alpha"), _card("Bravo"), _card("Charlie")
    ids = _send(client, [_item(1), _item(2), _item(3, post=OTHER_POST)])
    i1, i2, i3 = ids["fbaw:1001:2001"], ids["fbaw:1001:2002"], ids["fbaw:1002:2003"]
    o1 = _register(client, [(a, "101", "1. Card 1 · Seller One", str(i1)), (b, "102", "2. Card 2 · Seller One", str(i2))])
    o2 = _register(client, [(c, "103", "3. Card 3 · Seller One", str(i3))])
    return (i1, i2, i3), (o1, o2)


# ── Edit order carries purchase_id along ──────────────────────────────────


def test_merging_a_sellers_sales_carries_items_and_nothing_is_missing(client):
    (i1, i2, i3), (o1, o2) = _two_sales(client)
    # The settled workflow: merge order o2 into o1 (every row's Order ID = o1).
    _edit(client, o2, {t.id: str(o1) for t in _rows(o2)})
    assert _rows(o2) == []
    assert {_won(i).purchase_id for i in (i1, i2, i3)} == {o1}
    assert all(_won(i).status == "registered" for i in (i1, i2, i3))
    html = client.get("/orders/purchased").text
    assert "missing" not in html
    assert "Facebook wins to register" not in html


def test_moving_every_row_to_another_existing_order(client):
    (i1, i2, _), (o1, o2) = _two_sales(client)
    _edit(client, o1, {t.id: str(o2) for t in _rows(o1)})
    assert _won(i1).purchase_id == o2 and _won(i2).purchase_id == o2


def test_split_follows_each_items_own_rows(client):
    (i1, i2, _), (o1, _) = _two_sales(client)
    card2_row = next(t for t in _rows(o1) if t.note.startswith("2."))
    _edit(client, o1, {card2_row.id: ""})  # "Start new order" on item 2's row
    (moved,) = [t for t in _all_rows() if t.id == card2_row.id]
    assert moved.purchase_id not in (o1, None)
    assert _won(i1).purchase_id == o1
    assert _won(i2).purchase_id == moved.purchase_id


def test_lot_split_across_orders_stays_where_some_rows_remain(client):
    a, b, c = _card("Alpha"), _card("Bravo"), _card("Charlie")
    ids = _send(client, [_item(1, label="1. Lot of 3", price=90)])
    i1 = ids["fbaw:1001:2001"]
    note = "1. Lot of 3 · Seller One"
    o1 = _register(client, [(a, "30", note, str(i1)), (b, "30", note, str(i1)), (c, "30", note, str(i1))])
    first = _rows(o1)[0]
    _edit(client, o1, {first.id: ""})
    # Two of its three rows are still on o1: the item stays there.
    assert _won(i1).purchase_id == o1
    # Then the other two go to the new order too: the item follows them.
    new = next(t.purchase_id for t in _all_rows() if t.id == first.id)
    _edit(client, o1, {t.id: str(new) for t in _rows(o1)})
    assert _won(i1).purchase_id == new


def test_item_with_an_edited_note_stays_while_its_order_has_rows(client):
    (i1, i2, _), (o1, o2) = _two_sales(client)
    rows = _rows(o1)
    # Item 2's note is edited in the same save that moves item 1's row:
    # no row carries item 2's note any more, so it goes by the order's
    # rows -- some are still on o1, so it stays.
    r1 = next(t for t in rows if t.note.startswith("1."))
    r2 = next(t for t in rows if t.note.startswith("2."))
    _edit(client, o1, {r1.id: str(o2)}, notes={r2.id: "changed"})
    assert _won(i1).purchase_id == o2
    assert _won(i2).purchase_id == o1


def test_rows_added_without_a_note_follow_a_merge(client):
    """A lot whose later cards came in through Edit order's add-card (no
    note) still follows a whole merge."""
    (i1, i2, _), (o1, o2) = _two_sales(client)
    with db_module.SessionLocal() as s:
        for t in s.query(Transaction).filter(Transaction.purchase_id == o1):
            t.note = None
        s.commit()
    _edit(client, o1, {t.id: str(o2) for t in _rows(o1)})
    assert _won(i1).purchase_id == o2 and _won(i2).purchase_id == o2


def test_ignored_items_are_left_alone(client):
    (i1, _, _), (o1, o2) = _two_sales(client)
    with db_module.SessionLocal() as s:
        s.get(WonItem, i1).status = "ignored"
        s.commit()
    _edit(client, o1, {t.id: str(o2) for t in _rows(o1)})
    assert _won(i1).purchase_id == o1


def test_follow_order_edit_pick_rule():
    from collections import Counter

    assert won_inbox._pick_order(Counter(), 5) is None
    assert won_inbox._pick_order(Counter({7: 2}), 5) == 7
    assert won_inbox._pick_order(Counter({5: 1, 7: 3}), 5) == 5
    assert won_inbox._pick_order(Counter({7: 1, 9: 2}), 5) == 9
    assert won_inbox._pick_order(Counter({9: 1, 7: 1}), 5) == 7


def _all_rows() -> list[Transaction]:
    with db_module.SessionLocal() as s:
        txs = s.query(Transaction).order_by(Transaction.id).all()
        s.expunge_all()
        return txs


# ── "Order missing" covers registered items ───────────────────────────────


def test_registered_item_whose_order_is_deleted_comes_back_as_missing(client):
    (i1, i2, i3), (o1, _) = _two_sales(client)
    _edit(client, o1, {}, delete=[t.id for t in _rows(o1)])
    # Deleting every row keeps the items pointing at o1, now gone.
    assert _won(i1).purchase_id == o1
    html = client.get("/orders/purchased").text
    assert "Facebook wins to register" in html
    assert f"order #{o1} missing" in html
    assert "1. Card 1" in html and "2. Card 2" in html
    # The other sale's registration still stands: not listed.
    assert "3. Card 3" not in html
    # It can be opened, re-registered, and then it's gone again.
    cart = client.get(f"/orders/fb-wins/{i1}/cart").text
    assert cart.count('class="fb-import-item"') == 2
    assert f"order #{o1} missing" in cart
    a = _card("Delta")
    o3 = _register(client, [(a, "101", "1. Card 1 · Seller One", str(i1))])
    assert (_won(i1).status, _won(i1).purchase_id) == ("registered", o3)
    assert _won(i2).purchase_id == o1  # still missing, still listed
    html = client.get("/orders/purchased").text
    assert "1. Card 1</a>" not in html and "2. Card 2" in html


def test_registered_item_with_a_missing_order_can_be_ignored(client):
    (i1, _, _), (o1, _) = _two_sales(client)
    _edit(client, o1, {}, delete=[t.id for t in _rows(o1)])
    client.post(f"/orders/fb-wins/{i1}/ignore")
    assert _won(i1).status == "ignored"


def test_registered_item_on_an_existing_order_cant_be_ignored_or_relinked(client):
    (i1, _, _), _ = _two_sales(client)
    client.post(f"/orders/fb-wins/{i1}/ignore")
    assert _won(i1).status == "registered"
    r = client.post(
        "/transactions/purchase",
        data={"type": "purchase", "date": "2026-10-03", "card_id": [str(_card("Echo"))], "price": ["1"],
              "note": ["n"], "won_item_id": [str(i1)]},
    )
    assert r.status_code == 422 and "already registered" in r.text


def test_missing_registered_lot_relinked_as_not_complete_goes_back_to_pending(client):
    (i1, _, _), (o1, _) = _two_sales(client)
    _edit(client, o1, {}, delete=[t.id for t in _rows(o1)])
    o3 = _register(client, [(_card("Foxtrot"), "101", "1. Card 1 · Seller One", str(i1))], keep_pending=str(i1))
    assert (_won(i1).status, _won(i1).purchase_id) == ("pending", o3)


# ── Leftovers: the prefilled Total leaves out what earlier orders count ───


def _prefilled_total(html: str) -> str | None:
    tag = html.split('name="purchase_total"', 1)[1].split(">", 1)[0]
    if "value=" not in tag:
        return None
    return tag.split('value="', 1)[1].split('"', 1)[0]


def test_leftover_total_excludes_what_the_first_order_counts_as_remaining(client):
    a = _card("Alpha")
    ids = _send(client, [_item(1, price=100), _item(2, price=60), _item(3, price=40)])
    i1, i2 = ids["fbaw:1001:2001"], ids["fbaw:1001:2002"]
    # First order: Total prefilled with the whole sale (200), only item 1 linked.
    o1 = _register(client, [(a, "100", "1. Card 1 · Seller One", str(i1))], purchase_total="200")
    html = client.get(f"/orders/fb-wins/{i2}/cart").text
    # Order #o1's Remaining (100) already covers both leftovers.
    assert _prefilled_total(html) == "0.0"
    assert f"#{o1}</a>" in html
    assert "leaves out 100 kr" in html


def test_leftover_total_partly_accounted(client):
    a = _card("Alpha")
    ids = _send(client, [_item(1, price=100), _item(2, price=60), _item(3, price=40)])
    i1, i2 = ids["fbaw:1001:2001"], ids["fbaw:1001:2002"]
    # The first order's Total covered its card and one leftover only.
    _register(client, [(a, "100", "1. Card 1 · Seller One", str(i1))], purchase_total="160", purchase_shipping="0")
    opened = _open(i2)
    assert opened.accounted == 60
    assert opened.total == 40


def test_leftover_total_full_when_first_order_total_covers_only_its_cards(client):
    a = _card("Alpha")
    ids = _send(client, [_item(1, price=100), _item(2, price=60)])
    i1, i2 = ids["fbaw:1001:2001"], ids["fbaw:1001:2002"]
    _register(client, [(a, "100", "1. Card 1 · Seller One", str(i1))], purchase_total="100")
    html = client.get(f"/orders/fb-wins/{i2}/cart").text
    assert _prefilled_total(html) == "60.0"
    assert "leaves out" not in html


def test_leftover_total_full_when_first_order_has_no_total(client):
    a = _card("Alpha")
    ids = _send(client, [_item(1, price=100), _item(2, price=60)])
    i1, i2 = ids["fbaw:1001:2001"], ids["fbaw:1001:2002"]
    _register(client, [(a, "100", "1. Card 1 · Seller One", str(i1))])
    assert _open(i2).total == 60


def test_leftover_total_excludes_a_lot_already_on_an_order(client):
    a = _card("Alpha")
    ids = _send(client, [_item(1, label="1. Lot of 2", price=100), _item(2, price=60)])
    i1, i2 = ids["fbaw:1001:2001"], ids["fbaw:1001:2002"]
    o1 = _register(client, [(a, "100", "1. Lot of 2 · Seller One", str(i1))], keep_pending=str(i1), purchase_total="100")
    opened = _open(i2)
    # The lot's 100 is on order o1's rows already; only item 2 is left.
    assert opened.registered_orders == [o1]
    assert opened.on_order_known == 100
    assert opened.total == 60


def test_no_earlier_order_keeps_the_whole_known_total(client):
    ids = _send(client, [_item(1, price=100), _item(2, price=60)])
    opened = _open(ids["fbaw:1001:2001"])
    assert opened.registered_orders == [] and opened.excluded == 0 and opened.total == 160


def _open(item_id: int) -> won_inbox.OpenSale:
    with db_module.SessionLocal() as s:
        return won_inbox.open_sale(s, item_id)
