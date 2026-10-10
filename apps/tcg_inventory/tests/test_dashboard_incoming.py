"""Dashboard Inventory breakdown: in-transit cards listed under an
"Incoming" row with a "shared with <their collections>" badge (issue #393),
driven by the in-transit status (#382), never by a collection. Prod's dead
pre-#382 "Incoming" collection (id 10) is kept in the database but never
produces a row or a "shared with Incoming" badge."""
from __future__ import annotations

import re

from conftest import make_csv, seed_import

import constants
import db as db_module
import queries
from models import Card, Collection

VINTAGE = constants.VINTAGE_COLLECTION_NAME
GENERIC = constants.GENERIC_COLLECTION_NAME


# ── Query ────────────────────────────────────────────────────────────────


def _card(card_id, qty=1, in_transit=None, collections=()):
    card = Card(card_id=card_id, name=card_id, qty=qty, in_transit_qty=in_transit, market_price=10.0, price_flags="")
    card.collections.extend(collections)
    return card


def test_breakdown_incoming_bucket_follows_the_status_not_the_collection(db_session):
    generic = Collection(name=GENERIC, priority_rank=constants.priority_rank_for(GENERIC))
    dead = Collection(name=constants.INCOMING_CATEGORY, priority_rank=99)
    tagged = _card("tagged", qty=2, in_transit=2, collections=[generic])
    bulk = _card("bulk", in_transit=1)
    arrived = _card("arrived", collections=[generic, dead])  # frozen dead tag
    only_dead = _card("only_dead", collections=[dead])
    cards = [tagged, bulk, arrived, only_dead]
    db_session.add_all(cards)
    db_session.flush()

    b = queries.collection_membership_breakdown(db_session, cards)

    assert [c.card_id for c in b["incoming"].cards] == ["bulk", "tagged"]
    assert b["incoming"].qty == 3 and b["incoming"].unique_count == 2
    # The dead collection never gets a row of its own.
    assert [c.name for c in b["children"]] == [GENERIC]
    # Every-row rule: an in-transit card also counts in its own rows.
    assert {c.card_id for c in b["children"][0].cards} == {"tagged", "arrived"}
    # A card tagged only with the dead collection is Bulk.
    assert {c.card_id for c in b["bulk"].cards} == {"bulk", "only_dead"}
    assert {c.card_id for c in b["collections"].cards} == {"tagged", "arrived"}
    # Total still counts every card once.
    assert b["total"].qty == 5 and b["total"].unique_count == 4


def test_status_named_collection_is_flagged():
    assert Collection(name=constants.INCOMING_CATEGORY, priority_rank=99).is_status
    assert not Collection(name=GENERIC, priority_rank=1).is_status


# ── Page ─────────────────────────────────────────────────────────────────


def _seed(client, incoming=True):
    """Charizard: Vintage + generic, in transit. Pikachu: Bulk, in transit.
    Bulbasaur: generic, in hand, still carrying the dead Incoming tag."""
    chari = {"id": "a", "name": "Charizard", "qty": 1, "price": "100"}
    pika = {"id": "b", "name": "Pikachu", "qty": 2, "price": "10"}
    bulba = {"id": "c", "name": "Bulbasaur", "qty": 1, "price": "5"}
    files = [
        ("files", ("main.csv", make_csv("My Collection", [chari, pika, bulba]), "text/csv")),
        ("files", ("g.csv", make_csv(GENERIC, [chari, bulba]), "text/csv")),
        ("files", ("v.csv", make_csv(VINTAGE, [chari]), "text/csv")),
    ]
    if incoming:
        files.append(("files", ("in.csv", make_csv("Incoming", [{"id": "a", "qty": 1}, {"id": "b", "qty": 2}]), "text/csv")))
    seed_import(client, files)
    with db_module.SessionLocal() as s:
        dead = Collection(name=constants.INCOMING_CATEGORY, priority_rank=99)
        s.add(dead)
        for card in s.query(Card).filter(Card.card_id.in_(["a", "c"])):
            card.collections.append(dead)
        s.commit()
        return {c.name: c.id for c in s.query(Collection)}


def _inventory_card(html):
    start = html.index('id="dashboard-inventory-card"')
    return html[start: html.index('id="dashboard-series-card"', start)]


def _leaf_rows(html, row_id):
    return re.findall(rf'<tr class="grandchild-row[^"]*" data-group="{row_id}" hidden>.*?</tr>', html, re.S)


def _row_for(rows, card_name):
    found = [r for r in rows if f">{card_name}</a>" in r]
    assert len(found) == 1, card_name
    return found[0]


def _collection_row_id(html, name):
    return re.search(
        rf'data-row-id="(coll-\d+)"[^>]*><span class="arrow">[^<]*</span> {re.escape(name)}</button>', html
    ).group(1)


def test_dashboard_lists_in_transit_cards_under_incoming(client):
    ids = _seed(client)
    html = _inventory_card(client.get("/").text)

    # One Incoming row, the status one: the dead collection makes none.
    assert len(re.findall(r'</span> Incoming</button>', html)) == 1
    assert 'data-row-id="coll-incoming"' in html
    assert not re.search(r'data-row-id="coll-\d+"[^>]*><span class="arrow">[^<]*</span> Incoming<', html)
    assert '<a href="/inventory?transit=1">3</a>' in html

    incoming = _leaf_rows(html, "coll-incoming")
    assert len(incoming) == 2
    chari = _row_for(incoming, "Charizard")
    # "shared with" names its real collections, in priority order.
    badge = re.search(r'<span class="tx-platform-badge shared-badge" title="Also in: ([^"]*)">(.*?)</span>', chari, re.S)
    assert badge.group(1) == f"{VINTAGE}, {GENERIC}"
    assert f'shared with <a href="/collections/{ids[VINTAGE]}">{VINTAGE}</a>, <a href="/collections/{ids[GENERIC]}">{GENERIC}</a>' in chari
    # A Bulk card in transit has no collection to share with.
    assert "shared-badge" not in _row_for(incoming, "Pikachu")
    assert ">Bulbasaur</a>" not in "".join(incoming)


def test_dashboard_never_says_shared_with_incoming(client):
    ids = _seed(client)
    html = _inventory_card(client.get("/").text)
    assert f'href="/collections/{ids[constants.INCOMING_CATEGORY]}"' not in html
    assert ">Incoming</a>" not in html

    # The in-transit card still counts in its own collection rows (every-row
    # rule), just without "Incoming" in its badge.
    generic_rows = _leaf_rows(html, _collection_row_id(html, GENERIC))
    chari = _row_for(generic_rows, "Charizard")
    assert re.search(r'title="Also in: ([^"]*)"', chari).group(1) == VINTAGE
    # The arrived card's frozen dead tag doesn't make it "shared".
    assert "shared-badge" not in _row_for(generic_rows, "Bulbasaur")


def test_dead_incoming_collection_hidden_from_collection_cells_and_badges(client):
    ids = _seed(client)
    inventory = client.get("/inventory").text
    assert f'href="/collections/{ids[constants.INCOMING_CATEGORY]}"' not in inventory
    page = client.get(f"/collections/{ids[GENERIC]}").text
    assert re.search(r'title="Also in: ([^"]*)"', page).group(1) == VINTAGE


def test_no_incoming_row_when_nothing_is_in_transit(client):
    _seed(client, incoming=False)
    html = _inventory_card(client.get("/").text)
    assert "coll-incoming" not in html
    assert "</span> Incoming</button>" not in html
