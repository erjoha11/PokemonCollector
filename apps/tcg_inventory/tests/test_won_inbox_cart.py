"""The Facebook wins link flow (#309 slice 2): "Open in cart" prefills the
New Order cart from one won sale, items link to cards as normal cart rows
(ref + note + price, index-aligned), and Register marks them registered."""
from __future__ import annotations

import datetime as dt
import json
import re

import pytest

import db as db_module
import won_inbox
from models import Card, Transaction, WonItem

POST = "https://www.facebook.com/groups/somegroup/posts/1001/"
SALE_END = dt.date(2026, 10, 3)


def _item(n: int, **over) -> dict:
    item = {
        "external_ref": f"fbaw:1001:{2000 + n}",
        "seller": "Seller One",
        "sale_type": "auction",
        "ended_on": SALE_END.isoformat(),
        "post_url": POST,
        "lot_url": f"{POST}?comment_id={2000 + n}",
        "label": f"{n}. Card {n}",
        "price": 100 + n,
        "shipping_text": "50 kr tracked",
        "payment_text": "Vipps",
        "paid_at": None,
        "received_at": None,
    }
    item.update(over)
    return item


def _send(client, items: list[dict]) -> dict[str, int]:
    """Sends the items (the inbox is open locally: no token, no login) and
    returns {external_ref: won_items.id}."""
    body = {"format": "fbaw-won", "version": 1, "sent_at": "2026-10-04T10:00:00Z", "items": items}
    r = client.post("/inbox/fb-wins", content=json.dumps(body), headers={"Content-Type": "application/json"})
    assert r.status_code == 200, r.text
    with db_module.SessionLocal() as s:
        return {w.external_ref: w.id for w in s.query(WonItem)}


def _card(name: str, card_id: str, *, number: str | None = None, created: dt.date | None = None) -> int:
    with db_module.SessionLocal() as s:
        card = Card(
            card_id=card_id, name=name, number=number, set="Some Set", qty=1,
            created_at=dt.datetime.combine(created, dt.time(12)) if created else None,
        )
        s.add(card)
        s.commit()
        return card.id


def _purchase(card_id: int, purchase_id: int) -> None:
    with db_module.SessionLocal() as s:
        s.add(Transaction(card_id=card_id, type="purchase", date=dt.date(2026, 1, 1), price=10, purchase_id=purchase_id))
        s.commit()


def _won(item_id: int) -> WonItem:
    with db_module.SessionLocal() as s:
        row = s.get(WonItem, item_id)
        s.expunge(row)
        return row


def _register(client, rows: list[tuple[int, str, str, str]], **extra):
    """rows: (card_id, price, note, won_item_id) -- posted as the cart does."""
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
    return client.post("/transactions/purchase", data=data, follow_redirects=False)


# ── Open in cart: the prefill ─────────────────────────────────────────────


def test_open_in_cart_button_per_sale(client):
    ids = _send(client, [_item(1), _item(2)])
    html = client.get("/orders/purchased").text
    first = min(ids.values())
    assert f'hx-get="/orders/fb-wins/{first}/cart"' in html
    assert html.count(">Open in cart</button>") == 1
    assert 'hx-trigger="click[confirmDiscardCart()]"' in html


def test_open_in_cart_prefills_the_new_order_cart(client):
    ids = _send(client, [_item(1, price=100), _item(2, price=50.5), _item(3, price=None)])
    html = client.get(f"/orders/fb-wins/{ids['fbaw:1001:2002']}/cart").text

    assert "New Order from Facebook: Seller One" in html
    assert 'name="date" value="2026-10-03"' in html
    assert 'name="platform" placeholder="Platform (optional)" value="Facebook"' in html
    total_input = html.split('name="purchase_total"', 1)[1].split(">", 1)[0]
    assert 'value="150.5"' in total_input and 'data-prefill-base="150.5"' in total_input
    shipping_input = html.split('name="purchase_shipping"', 1)[1].split(">", 1)[0]
    assert "value=" not in shipping_input
    assert "Seller: 50 kr tracked" in html
    # The panel sits outside the form, so none of it is submitted by accident.
    panel_at, form_at = html.index("data-imported-sale"), html.index('<form id="purchase-cart-form"')
    assert panel_at < form_at
    assert html.count('class="fb-import-item"') == 3
    assert "price unknown" in html
    # Unlinked items are counted above Register, and unknown prices flagged.
    assert "3 of 3 items not linked (150 kr + ?)" in html
    assert "1 item without a known price" in html
    assert html.index("data-import-summary") < html.index(">Register</button>")


def test_open_in_cart_with_no_known_price_leaves_total_blank(client):
    ids = _send(client, [_item(1, price=None)])
    html = client.get(f"/orders/fb-wins/{ids['fbaw:1001:2001']}/cart").text
    total_input = html.split('name="purchase_total"', 1)[1].split(">", 1)[0]
    assert "value=" not in total_input


def test_open_in_cart_only_shows_this_sales_pending_items(client):
    other = "https://www.facebook.com/groups/somegroup/posts/1002/"
    ids = _send(client, [_item(1), _item(2), _item(3, external_ref="fbaw:1002:1", post_url=other, lot_url=other)])
    with db_module.SessionLocal() as s:
        s.get(WonItem, ids["fbaw:1001:2002"]).status = "ignored"
        s.commit()
    html = client.get(f"/orders/fb-wins/{ids['fbaw:1001:2001']}/cart").text
    assert html.count('class="fb-import-item"') == 1
    assert "1. Card 1" in html and "2. Card 2" not in html and "3. Card 3" not in html


def test_open_in_cart_for_a_sale_with_nothing_pending(client):
    ids = _send(client, [_item(1)])
    with db_module.SessionLocal() as s:
        s.get(WonItem, ids["fbaw:1001:2001"]).status = "ignored"
        s.commit()
    html = client.get(f"/orders/fb-wins/{ids['fbaw:1001:2001']}/cart").text
    assert "Nothing from this sale is waiting" in html
    assert "<form" not in html


# ── Candidates ────────────────────────────────────────────────────────────


def test_candidates_rank_unordered_then_new_since_the_sale(client):
    old_owned = _card("Charizard ex", "sv3pt5-199", number="199/165", created=dt.date(2026, 1, 1))
    _purchase(old_owned, purchase_id=4)
    old_free = _card("Charizard ex", "sv3-125", number="125/197", created=dt.date(2026, 1, 1))
    new_free = _card("Charizard ex", "sv3-223", number="223/197", created=dt.date(2026, 10, 4))
    _card("Pikachu", "sv1-1", created=dt.date(2026, 10, 4))
    ids = _send(client, [_item(1, label="1. Charizard ex 199/165")])
    html = client.get(f"/orders/fb-wins/{ids['fbaw:1001:2001']}/cart").text

    boxes = re.findall(r'<input type="checkbox" class="fb-cand" value="(\d+)">', html)
    # No acquired transaction first (newest since the sale before older), the
    # one already on an order last, even though its number matches.
    assert boxes == [str(new_free), str(old_free), str(old_owned)]
    assert "Pikachu" not in html
    assert "already on order #4" in html
    assert "new since the sale" in html
    # Suggestions only: never ticked, never named (not form data).
    assert "checked" not in html.split("data-imported-sale", 1)[1].split("<form", 1)[0]
    assert 'class="fb-cand" value' in html and 'name="fb' not in html


def test_candidates_tolerate_a_typo_and_skip_generic_words(db_session):
    db_session.add_all(
        [
            Card(card_id="a", name="Charizard ex", qty=1),
            Card(card_id="b", name="Mew ex", qty=1),
            Card(card_id="c", name="Gengar", qty=1),
        ]
    )
    item = WonItem(
        id=1, external_ref="fbaw:1:1", source="fbaw", sale_type="auction", post_url=POST, lot_url=POST,
        label="3. Charizrd EX NM", status="pending",
        first_seen_at=dt.datetime(2026, 10, 4), last_seen_at=dt.datetime(2026, 10, 4),
    )
    db_session.add(item)
    db_session.commit()
    found = won_inbox.candidates(db_session, [item], ("purchase", "ripped", "trade"))[1]
    # "ex" alone doesn't make Mew ex a candidate.
    assert [c.card.name for c in found] == ["Charizard ex"]


def test_empty_candidates_explain_the_lag(client):
    _card("Pikachu", "sv1-1")
    ids = _send(client, [_item(1, label="1. Umbreon VMAX alt art")])
    html = client.get(f"/orders/fb-wins/{ids['fbaw:1001:2001']}/cart").text
    assert (
        "No matching card yet — it appears after you raise its qty in Dex (tag it Incoming until it arrives) "
        "and the daily sync runs." in html
    )
    # The item-scoped fallback search knows which item it's for.
    item_id = ids["fbaw:1001:2001"]
    assert f'hx-get="/orders/fb-wins/{item_id}/search"' in html
    assert f'hx-target="#fb-import-search-{item_id}"' in html
    assert 'placeholder="Not listed? Search…"' in html


def test_item_search_returns_checkboxes_for_that_item(client):
    pika = _card("Pikachu", "sv1-1")
    _purchase(pika, purchase_id=9)
    ids = _send(client, [_item(1)])
    html = client.get(f"/orders/fb-wins/{ids['fbaw:1001:2001']}/search", params={"q": "pika"}).text
    assert f'<input type="checkbox" class="fb-cand" value="{pika}">' in html
    assert "already on order #9" in html
    assert "No card matches" in client.get(f"/orders/fb-wins/{ids['fbaw:1001:2001']}/search", params={"q": "zzz"}).text


# ── Link selected: the cart rows ──────────────────────────────────────────


def _row_inputs(html: str) -> dict[str, list[str]]:
    return {
        name: re.findall(rf'name="{name}"[^>]*?value="([^"]*)"', html)
        for name in ("card_id", "won_item_id", "note")
    }


def test_add_row_with_an_item_carries_ref_note_and_price(client):
    card = _card("Charizard ex", "sv3-223")
    ids = _send(client, [_item(1, label="1. Charizard ex")])
    item_id = ids["fbaw:1001:2001"]
    html = client.get("/transactions/purchase/add-row", params={"card_id": card, "won_item_id": item_id, "price": "33.34"}).text
    assert _row_inputs(html) == {"card_id": [str(card)], "won_item_id": [str(item_id)], "note": ["1. Charizard ex · Seller One"]}
    assert 'value="33.34"' in html.split('name="price"', 1)[1].split(">", 1)[0]
    assert f'data-won-item="{item_id}"' in html


def test_plain_add_row_still_renders_blank_ref_and_note(client):
    card = _card("Pikachu", "sv1-1")
    html = client.get("/transactions/purchase/add-row", params={"card_id": card}).text
    # Present but blank, so the lists stay index-aligned with card_id/price.
    assert _row_inputs(html) == {"card_id": [str(card)], "won_item_id": [""], "note": [""]}
    assert 'value=""' in html.split('name="price"', 1)[1].split(">", 1)[0]


# ── Register ──────────────────────────────────────────────────────────────


def test_register_links_items_and_leaves_the_rest_pending(client):
    a, b, c = _card("A", "a"), _card("B", "b"), _card("Other", "o")
    ids = _send(client, [_item(1), _item(2), _item(3)])
    i1, i2, i3 = ids["fbaw:1001:2001"], ids["fbaw:1001:2002"], ids["fbaw:1001:2003"]
    r = _register(
        client,
        [
            (a, "101", "1. Card 1 · Seller One", str(i1)),
            (b, "102", "2. Card 2 · Seller One", str(i2)),
            (c, "5", "", ""),  # a card added from the normal search
        ],
        purchase_total="306",
    )
    assert r.status_code == 303, r.text

    with db_module.SessionLocal() as s:
        txs = s.query(Transaction).order_by(Transaction.card_id).all()
        pid = txs[0].purchase_id
        assert {t.purchase_id for t in txs} == {pid}
        assert [(t.card_id, t.price, t.note) for t in txs] == [
            (a, 101, "1. Card 1 · Seller One"),
            (b, 102, "2. Card 2 · Seller One"),
            (c, 5, None),
        ]
        assert all(t.date == SALE_END and t.platform == "Facebook" for t in txs)
    assert (_won(i1).status, _won(i1).purchase_id) == ("registered", pid)
    assert (_won(i2).status, _won(i2).purchase_id) == ("registered", pid)
    # Not linked: untouched, still in the inbox.
    assert (_won(i3).status, _won(i3).purchase_id) == ("pending", None)
    html = client.get("/orders/purchased").text
    assert "3. Card 3" in html and "1. Card 1</a>" not in html


def test_partial_lot_can_be_kept_pending_then_completed(client):
    a = _card("A", "a")
    ids = _send(client, [_item(1, label="1. Lot of 2")])
    i1 = ids["fbaw:1001:2001"]
    r = _register(client, [(a, "101", "1. Lot of 2 · Seller One", str(i1))], keep_pending=str(i1))
    assert r.status_code == 303, r.text
    row = _won(i1)
    assert row.status == "pending" and row.purchase_id is not None

    html = client.get("/orders/purchased").text
    assert f"lot not complete · <a href=\"/orders/purchased?open_order={row.purchase_id}" in html
    assert ">Lot complete</button>" in html

    r = client.post(f"/orders/fb-wins/{i1}/complete", headers={"HX-Request": "true"})
    assert r.status_code == 200
    assert _won(i1).status == "registered"
    assert "1. Lot of 2" not in client.get("/orders/purchased").text


def test_item_pointing_at_a_missing_order_shows_order_missing(client):
    ids = _send(client, [_item(1)])
    i1 = ids["fbaw:1001:2001"]
    with db_module.SessionLocal() as s:
        s.get(WonItem, i1).purchase_id = 99  # order 99 has no transactions
        s.commit()
    html = client.get("/orders/purchased").text
    assert "1. Card 1" in html
    assert "order #99 missing" in html
    assert ">Lot complete</button>" not in html
    # Lot complete refuses it: it stays pending (and visible).
    client.post(f"/orders/fb-wins/{i1}/complete")
    assert _won(i1).status == "pending"
    assert "order #99 missing" in client.get(f"/orders/fb-wins/{i1}/cart").text


def test_register_refuses_misaligned_ref_and_note_lists(client):
    a, b = _card("A", "a"), _card("B", "b")
    ids = _send(client, [_item(1)])
    r = client.post(
        "/transactions/purchase",
        data={
            "type": "purchase", "date": "2026-10-03",
            "card_id": [str(a), str(b)], "price": ["1", "2"],
            "note": ["x"], "won_item_id": [str(ids["fbaw:1001:2001"]), ""],
        },
    )
    assert r.status_code == 422
    assert "don't line up" in r.text
    with db_module.SessionLocal() as s:
        assert s.query(Transaction).count() == 0
    assert _won(ids["fbaw:1001:2001"]).status == "pending"


@pytest.mark.parametrize("status", ["registered", "ignored"])
def test_register_refuses_an_item_no_longer_pending(client, status):
    a = _card("A", "a")
    ids = _send(client, [_item(1)])
    i1 = ids["fbaw:1001:2001"]
    with db_module.SessionLocal() as s:
        s.get(WonItem, i1).status = status
        s.commit()
    r = _register(client, [(a, "101", "n", str(i1))])
    assert r.status_code == 422
    assert f"already {status}" in r.text
    with db_module.SessionLocal() as s:
        assert s.query(Transaction).count() == 0


def test_register_without_ref_lists_still_works(client):
    """A cart opened before this change posts neither list."""
    a = _card("A", "a")
    r = client.post("/transactions/purchase", data={"type": "purchase", "date": "2026-10-03", "card_id": [str(a)], "price": ["5"]}, follow_redirects=False)
    assert r.status_code == 303
    with db_module.SessionLocal() as s:
        (tx,) = s.query(Transaction).all()
        assert tx.note is None


def test_remaining_after_register_is_the_unlinked_items(client):
    """The prefilled Total (known prices) plus shipping, minus the linked
    prices and shipping, leaves exactly the unlinked item's price as the
    order's Remaining (Order history: Total − prices − shipping)."""
    a = _card("A", "a")
    ids = _send(client, [_item(1, price=100), _item(2, price=60)])
    i1 = ids["fbaw:1001:2001"]
    # Prefill 160; shipping 50 typed while untouched -> the cart makes it 210.
    r = _register(client, [(a, "100", "n", str(i1))], purchase_total="210", purchase_shipping="50")
    assert r.status_code == 303
    with db_module.SessionLocal() as s:
        (tx,) = s.query(Transaction).all()
        remaining = tx.purchase_total - tx.price - tx.purchase_shipping
    assert remaining == 60
