""""On the way" (issue #382), pass 2: the collection gallery, want and
sale lists (card_lists.py), and the master-set block (#378). Offline.

Reuses test_master_set's sv2a fixture, with two prints put on the way
(Dex's Incoming tag, every copy, as Dex mirrors the card's qty):
- main #1 (qty 3, 10 kr): 0 in hand, 2 duplicates on the way;
- Poké Ball #4 (qty 2, 50 kr): 0 in hand, 1 duplicate on the way.
So of the fixture's 5 spares (270 kr) only main #3's 2 (200 kr) are in
hand, and 3 are on the way. Ownership and completion don't change.
"""
from __future__ import annotations

import datetime as dt
import re

import card_lists
import db as db_module
import queries
from models import Card, Collection, MasterCard
from test_master_set import DISPLAY, build

SINCE = dt.date.today() - dt.timedelta(days=3)


def _put_on_the_way(db):
    for card_id, variant in (("jpn_sv2a-1", "Normal"), ("jpn_sv2a-4", "Poké Ball Holo")):
        card = db.query(Card).filter_by(card_id=card_id, variant=variant).one()
        card.in_transit_qty = card.qty
        card.in_transit_since = SINCE
    db.commit()
    db.expire_all()


def _master(db, number, variant):
    return db.query(MasterCard).filter_by(language="ja", set_code="sv2a", number=str(number), variant=variant).one()


def _statuses(detail):
    return {(v.master.number, v.master.variant): v.status_label for v in detail.items}


def _spare_entries(db):
    detail = queries.master_set_detail(db, "ja", "sv2a")
    return [(s.master.id, s.spares) for s in detail.slots if s.spares]


# --------------------------------------------------------------------------
# queries.master_set_detail: owned, but not spare
# --------------------------------------------------------------------------
def test_master_set_counts_in_transit_as_owned_but_not_as_duplicates(db_session):
    build(db_session)
    _put_on_the_way(db_session)
    d = queries.master_set_detail(db_session, "ja", "sv2a")

    # Ownership and completion are unchanged.
    assert (d.master_set.owned, d.master_set.total) == (7, 11)
    slots = {(s.master.number, s.track): s for s in d.slots}
    one, pb4, three = slots[("1", "main")], slots[("4", "poke_ball")], slots[("3", "main")]
    assert one.owned and one.owned_qty == 3 and one.in_hand_qty == 0 and one.in_transit == 3
    assert (one.spares, one.spares_on_the_way) == (0, 2)
    assert (pb4.spares, pb4.spares_on_the_way) == (0, 1)
    assert (three.spares, three.spares_on_the_way) == (2, 0)
    assert one.transit_card is not None and three.transit_card is None
    assert one.in_hand_card is None and three.in_hand_card is not None

    # Duplicates (KPI, filter) are in hand only.
    assert (d.spares, d.spare_value, d.spares_on_the_way) == (2, 200, 3)
    assert [s.master.number for s in d.spare_slots] == ["3"]
    assert [s.master.number for s in queries.filter_master_set_slots(d, show="duplicates")] == ["3"]
    assert len(queries.filter_master_set_slots(d, show="owned")) == 7


def test_partly_in_transit_print_keeps_the_in_hand_duplicates(db_session):
    build(db_session)
    card = db_session.query(Card).filter_by(card_id="jpn_sv2a-1", variant="Normal").one()
    card.in_transit_qty = 1  # 2 of 3 in hand
    db_session.commit()
    slot = next(s for s in queries.master_set_detail(db_session, "ja", "sv2a").slots if s.master.number == "1" and s.track == "main")
    assert (slot.spares, slot.spares_on_the_way) == (1, 1)
    assert slot.in_hand_card.id == card.id


def test_collections_overview_duplicates_match_the_block(db_session):
    build(db_session, collection="151 Collection")
    _put_on_the_way(db_session)
    row = next(r for r in queries.collections_overview(db_session) if r.collection.name == "151 Collection")
    assert row.duplicates == 2


# --------------------------------------------------------------------------
# Want lists
# --------------------------------------------------------------------------
def test_want_list_on_the_way_status(db_session):
    build(db_session)
    wants = card_lists.create_list(db_session, "Wants", "want")
    card_lists.add_items(
        db_session,
        wants,
        [
            (_master(db_session, 1, "normal").id, 1),  # owned 3, all on the way
            (_master(db_session, 1, "poke_ball_holo").id, 1),  # owned 1, in hand
            (_master(db_session, 4, "poke_ball_holo").id, 3),  # owned 2 (on the way) of 3
            (_master(db_session, 5, "poke_ball_holo").id, 1),  # missing
        ],
    )
    db_session.commit()
    _put_on_the_way(db_session)

    detail = card_lists.list_detail(db_session, wants)
    assert _statuses(detail) == {
        ("1", "normal"): "Incoming",
        ("1", "poke_ball_holo"): "Got it",
        ("4", "poke_ball_holo"): "Got 2 of 3",
        ("5", "poke_ball_holo"): "Missing",
    }
    assert detail.summary == "1 missing · 1 partly got · 1 incoming · 1 got it"
    # Copies on the way count as owned: nothing to pay for #1.
    on_way = next(v for v in detail.items if v.status == "on_the_way")
    assert on_way.counted_qty == 0 and on_way.in_transit == 3 and on_way.in_hand_qty == 0
    # "Copy as text" leaves it out, like "Got it".
    text = detail.text.splitlines()
    assert not any("#1 Mon 1 ×" in line for line in text)
    assert f"{DISPLAY} #4 Mon 4 · Poké Ball ×1 — 50 kr" in text
    # "Remove got it" skips it.
    assert [v.master.variant for v in detail.got_items] == ["poke_ball_holo"]
    assert card_lists.remove_got_it(db_session, wants) == 1
    db_session.commit()
    db_session.expire_all()
    assert ("1", "normal") in _statuses(card_lists.list_detail(db_session, wants))


def test_want_item_turns_got_it_when_it_arrives(db_session):
    build(db_session)
    wants = card_lists.create_list(db_session, "Wants", "want")
    card_lists.add_items(db_session, wants, [(_master(db_session, 1, "normal").id, 1)])
    db_session.commit()
    _put_on_the_way(db_session)
    assert _statuses(card_lists.list_detail(db_session, wants)) == {("1", "normal"): "Incoming"}

    card = db_session.query(Card).filter_by(card_id="jpn_sv2a-1", variant="Normal").one()
    card.in_transit_qty = None
    card.in_transit_since = None
    db_session.commit()
    db_session.expire_all()
    assert _statuses(card_lists.list_detail(db_session, wants)) == {("1", "normal"): "Got it"}


# --------------------------------------------------------------------------
# Sale lists
# --------------------------------------------------------------------------
def test_sale_list_on_the_way_status_and_in_hand_cards(db_session):
    build(db_session)
    sale = card_lists.create_list(db_session, "Spares", "sale")
    card_lists.add_items(db_session, sale, _spare_entries(db_session))  # #1 ×2, #3 ×2, PB #4 ×1
    db_session.commit()
    _put_on_the_way(db_session)

    detail = card_lists.list_detail(db_session, sale)
    assert _statuses(detail) == {
        ("1", "normal"): "Incoming",  # 0 in hand, 2 on the way: enough once they arrive
        ("3", "holo"): "Available",
        ("4", "poke_ball_holo"): "Incoming",
    }
    assert detail.summary == "1 available · 2 incoming"
    # Only copies in hand count toward the value, and go to /sales.
    assert detail.est_total == 200
    three = {c.id for c in db_session.query(Card).filter_by(card_id="jpn_sv2a-3")}
    assert len(detail.sale_card_ids) == 1 and set(detail.sale_card_ids) <= three
    # Still owned, so "Copy as text" keeps them.
    assert len(detail.text_items) == 3

    # More than would be spare even once they arrive: "Not enough spares".
    item = next(i for i in sale.items if i.master_card.number == "1")
    item.qty = 5
    db_session.commit()
    db_session.expire_all()
    assert _statuses(card_lists.list_detail(db_session, sale))[("1", "normal")] == "Not enough spares"


def test_sale_list_page_shows_spares_on_the_way(client):
    with db_module.SessionLocal() as db:
        build(db)
        sale = card_lists.create_list(db, "Spares", "sale")
        card_lists.add_items(db, sale, _spare_entries(db))
        db.commit()
        _put_on_the_way(db)
        list_id = sale.id
    html = client.get(f"/lists/{list_id}").text
    assert "0 spares +2 on the way" in html
    assert 'class="list-status list-status-on_the_way">Incoming<' in html
    assert "/lists/%d/ad" % list_id in html  # #3 is still in hand


# --------------------------------------------------------------------------
# Pages: the master-set block and the collection gallery
# --------------------------------------------------------------------------
def _collection_page(client, query="", with_checklist=True):
    with db_module.SessionLocal() as db:
        build(db, with_checklist=with_checklist, collection="151 Collection")
        _put_on_the_way(db)
        coll_id = db.query(Collection).filter_by(name="151 Collection").one().id
        ids = {
            "one": db.query(Card).filter_by(card_id="jpn_sv2a-1", variant="Normal").one().id,
            "three": {c.id for c in db.query(Card).filter_by(card_id="jpn_sv2a-3")},
        }
    return ids, client.get(f"/collections/{coll_id}{query}").text


def _grid(html, key="ja-sv2a"):
    return html.split(f'id="set-grid-{key}"', 1)[1].split(f'id="missing-list-{key}"', 1)[0]


def test_master_set_block_in_transit_tiles_and_duplicates(client):
    ids, html = _collection_page(client)
    # Completion unchanged; in-transit tiles look owned, with the pill.
    assert "7 / 11" in html
    grid = _grid(html)
    assert grid.count('class="gallery-card"') == 7
    assert grid.count("transit-badge") == 2
    assert "Tagged Incoming in Dex · seen since" in grid
    # Duplicates KPI: in hand only, the tooltip notes the rest.
    assert re.search(r">2 <span class=\"muted\">· 200 kr", html)
    assert re.search(r'aria-label="Every copy in hand beyond the first of each print\. Value at market price\. \+3 on the way', html)
    spares = html.split('id="spares-ja-sv2a"', 1)[1].split('id="unmatched-ja-sv2a"', 1)[0]
    assert "+3 on the way" in spares
    # The table lists only prints with duplicates in hand.
    assert spares.count("<tr>") == 2  # header + #3
    # The finn.no ad link carries only cards in hand.
    url = re.search(r'href="(/sales\?[^"]+)"', spares).group(1)
    got = {int(x) for x in re.findall(r"card_ids=(\d+)", url)}
    assert got and got <= ids["three"] and ids["one"] not in got


def test_master_set_duplicates_filter_uses_in_hand_spares(client):
    _, html = _collection_page(client, "?set=ja:sv2a&show=duplicates")
    grid = _grid(html)
    assert grid.count('class="gallery-card"') == 1
    # Its "Add N spares" count follows: #3's 2 copies in hand.
    assert 'Add 1 spares <span class="muted">(2 copies)</span> to' in grid


def test_master_set_table_row_note_when_partly_on_the_way(client):
    with db_module.SessionLocal() as db:
        build(db, collection="151 Collection")
        card = db.query(Card).filter_by(card_id="jpn_sv2a-1", variant="Normal").one()
        card.in_transit_qty = 1
        card.in_transit_since = SINCE
        db.commit()
        coll_id = db.query(Collection).filter_by(name="151 Collection").one().id
    html = client.get(f"/collections/{coll_id}").text
    spares = html.split('id="spares-ja-sv2a"', 1)[1].split('id="unmatched-ja-sv2a"', 1)[0]
    assert '<td class="num">1 <span class="muted spares-on-the-way">+1 on the way</span></td>' in spares


def test_collection_gallery_badges_in_transit_cards(client):
    _, html = _collection_page(client, with_checklist=False)
    assert 'id="set-grid-' not in html  # a plain gallery, no master-set block
    assert html.count("transit-badge") == 2
    assert "Tagged Incoming in Dex · seen since" in html
