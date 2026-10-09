"""A card's collections are listed in priority order, primary first, with
"Incoming" leading when the card is in transit (issue #389; README
"Business rules" #1 and #3). No bold on the primary (#393)."""
from __future__ import annotations

import re

from conftest import make_csv, seed_import

import constants
import db as db_module
from models import Card, Collection

ILLUSTRATOR = "Yuka Morii Collection"
VINTAGE = constants.VINTAGE_COLLECTION_NAME
GENERIC = constants.GENERIC_COLLECTION_NAME
UNKNOWN = "Aardvark Folder"  # alphabetically first, lowest priority


def _card_with(db, names, ranks=None):
    card = Card(card_id="jpn_x-1", name="Pikachu", qty=1)
    for name in names:
        rank = (ranks or {}).get(name, constants.priority_rank_for(name))
        card.collections.append(Collection(name=name, priority_rank=rank))
    db.add(card)
    db.flush()
    return card


# ── Ranking and the shared helper ────────────────────────────────────────


def test_priority_rank_for_order():
    ranks = [constants.priority_rank_for(n) for n in (ILLUSTRATOR, VINTAGE, GENERIC, UNKNOWN)]
    assert ranks == sorted(ranks) and len(set(ranks)) == 4


def test_collections_by_priority_illustrator_vintage_collection_unknown(db_session):
    card = _card_with(db_session, [UNKNOWN, GENERIC, VINTAGE, ILLUSTRATOR])
    assert [c.name for c in card.collections_by_priority] == [ILLUSTRATOR, VINTAGE, GENERIC, UNKNOWN]
    assert card.primary_collection.name == ILLUSTRATOR


def test_equal_rank_falls_back_to_name(db_session):
    card = _card_with(db_session, ["Zeta Folder", "beta Folder", UNKNOWN])
    assert [c.name for c in card.collections_by_priority] == [UNKNOWN, "beta Folder", "Zeta Folder"]


def test_no_collections(db_session):
    card = _card_with(db_session, [])
    assert card.collections_by_priority == [] and card.primary_collection is None


def test_stale_stored_rank_is_ignored(db_session):
    # collections.priority_rank is written once at creation and never
    # re-synced: an old row whose stored rank disagrees with the constant
    # must still sort by the constant.
    card = _card_with(db_session, [GENERIC, VINTAGE], ranks={GENERIC: 1, VINTAGE: 99})
    assert [c.name for c in card.collections_by_priority] == [VINTAGE, GENERIC]
    assert card.primary_collection.name == VINTAGE


def test_leftover_incoming_collection_gets_default_rank(db_session):
    # Prod's dead pre-#382 "Incoming" collection: no special rank, and
    # since #393 left out of a card's listed collections altogether (it's a
    # status, shown by the Incoming badge), so it never wins primary.
    assert constants.priority_rank_for("Incoming") == constants.PRIORITY_RANK_DEFAULT
    card = _card_with(db_session, ["Incoming", GENERIC], ranks={"Incoming": 0})
    assert card.primary_collection.name == GENERIC
    assert [c.name for c in card.collections_by_priority] == [GENERIC]
    only_dead = Card(card_id="jpn_x-2", name="Mew", qty=1)
    only_dead.collections.append(next(c for c in card.collections if c.name == "Incoming"))
    assert only_dead.collections_by_priority == [] and only_dead.primary_collection is None


# ── Pages ────────────────────────────────────────────────────────────────


def _seed(client, incoming=False):
    row = {"id": "a", "name": "Charizard", "qty": 1}
    files = [
        ("files", ("main.csv", make_csv("My Collection", [row, {"id": "b", "name": "Bulbasaur", "qty": 1}]), "text/csv")),
        ("files", ("u.csv", make_csv(UNKNOWN, [row]), "text/csv")),
        ("files", ("g.csv", make_csv(GENERIC, [row]), "text/csv")),
        ("files", ("v.csv", make_csv(VINTAGE, [row]), "text/csv")),
        ("files", ("i.csv", make_csv(ILLUSTRATOR, [row]), "text/csv")),
        ("files", ("solo.csv", make_csv(GENERIC, [{"id": "b", "name": "Bulbasaur", "qty": 1}]), "text/csv")),
    ]
    if incoming:
        files.append(("files", ("in.csv", make_csv("Incoming", [row]), "text/csv")))
    seed_import(client, files)
    with db_module.SessionLocal() as s:
        return {c.card_id: c.id for c in s.query(Card)}


def _order(html, names):
    positions = [html.index(f">{n}</a>") for n in names]
    return positions == sorted(positions)


def _collections_cell(html, card_name):
    row = re.search(rf"<tr[^>]*>(?:(?!</tr>).)*>{card_name}<(?:(?!</tr>).)*</tr>", html, re.S).group(0)
    return re.search(r'<td data-col="collections">(.*?)</td>', row, re.S).group(1)


def test_inventory_lists_collections_in_priority_order_primary_not_bold(client):
    _seed(client)
    cell = _collections_cell(client.get("/inventory").text, "Charizard")
    assert _order(cell, [ILLUSTRATOR, VINTAGE, GENERIC, UNKNOWN])
    # Order only, no emphasis (#393).
    assert "<strong" not in cell and "primary-collection" not in cell
    assert "transit-badge" not in cell


def test_single_collection_is_not_marked_primary(client):
    _seed(client)
    cell = _collections_cell(client.get("/inventory").text, "Bulbasaur")
    assert f">{GENERIC}</a>" in cell and "primary-collection" not in cell


def test_inventory_in_transit_leads_the_collections_cell(client):
    _seed(client, incoming=True)
    cell = _collections_cell(client.get("/inventory").text, "Charizard")
    assert cell.index("transit-badge") < cell.index(f">{ILLUSTRATOR}</a>")
    # Incoming stays a status (#382), never a collection link.
    assert ">Incoming</a>" not in cell


def test_card_detail_lists_collections_in_priority_order(client):
    ids = _seed(client, incoming=True)
    html = client.get(f"/cards/{ids['a']}").text
    dd = re.search(r"<dt>Collections</dt>\s*<dd>(.*?)</dd>", html, re.S).group(1)
    assert _order(dd, [ILLUSTRATOR, VINTAGE, GENERIC, UNKNOWN])
    assert dd.index("transit-badge") < dd.index(f">{ILLUSTRATOR}</a>")
    assert "<strong" not in dd and "primary-collection" not in dd


def test_card_detail_bulk_card_still_says_none(client):
    seed_import(client, [("files", ("main.csv", make_csv("My Collection", [{"id": "z", "name": "Mew"}]), "text/csv"))])
    with db_module.SessionLocal() as s:
        pk = s.query(Card).one().id
    dd = re.search(r"<dt>Collections</dt>\s*<dd>(.*?)</dd>", client.get(f"/cards/{pk}").text, re.S).group(1)
    assert "None (Bulk)" in dd


def test_collection_page_also_in_title_is_priority_ordered(client):
    _seed(client)
    with db_module.SessionLocal() as s:
        unknown_id = s.query(Collection).filter(Collection.name == UNKNOWN).one().id
    html = client.get(f"/collections/{unknown_id}").text
    assert f'title="Also in: {ILLUSTRATOR}, {VINTAGE}, {GENERIC}"' in html


# ── "Also in" / "shared with" badges name the other collections ──────────
# (PR #390 follow-up): the first SHARED_CAP other collections as visible
# links in priority order, the rest as "+N", the full list in the title,
# never the collection whose page/row it is.

SHARED_CAP = 2
ALL_FOUR = [ILLUSTRATOR, VINTAGE, GENERIC, UNKNOWN]  # Charizard's, in priority order


def _collection_ids():
    with db_module.SessionLocal() as s:
        return {c.name: c.id for c in s.query(Collection)}


def _expected_badge(label, others, ids):
    visible = ", ".join(f'<a href="/collections/{ids[n]}">{n}</a>' for n in others[:SHARED_CAP])
    more = f' <span class="shared-more">+{len(others) - SHARED_CAP}</span>' if len(others) > SHARED_CAP else ""
    return (
        f'<span class="tx-platform-badge shared-badge" title="Also in: {", ".join(others)}">'
        f"{label} {visible}{more}</span>"
    )


def _dashboard_leaf_row(html, collection_name, card_name):
    row_id = re.search(
        rf'data-row-id="(coll-\d+)"[^>]*><span class="arrow">[^<]*</span> {re.escape(collection_name)}</button>', html
    ).group(1)
    for row in re.findall(rf'<tr class="grandchild-row[^"]*" data-group="{row_id}" hidden>.*?</tr>', html, re.S):
        if f">{card_name}</a>" in row:
            return row
    raise AssertionError(f"{card_name} not listed under {collection_name}")


def test_collection_page_also_in_badge_names_others_with_overflow(client):
    _seed(client)
    ids = _collection_ids()
    for here in ALL_FOUR:
        others = [n for n in ALL_FOUR if n != here]
        html = client.get(f"/collections/{ids[here]}").text
        badge = _expected_badge("also in", others, ids)
        assert badge in html, here
        # The current collection is never named, visibly or in the title.
        shown = re.search(r'<span class="tx-platform-badge shared-badge"[^>]*>.*?</span>(?:</span>)?', html, re.S).group(0)
        assert f">{here}</a>" not in shown
        assert here not in re.search(r'title="Also in: ([^"]*)"', shown).group(1).split(", ")
        assert f'href="/collections/{ids[here]}"' not in shown


def test_dashboard_shared_with_badge_names_others_with_overflow(client):
    _seed(client)
    ids = _collection_ids()
    html = client.get("/").text
    for here in ALL_FOUR:
        others = [n for n in ALL_FOUR if n != here]
        row = _dashboard_leaf_row(html, here, "Charizard")
        assert _expected_badge("shared with", others, ids) in row, here
        assert f'href="/collections/{ids[here]}"' not in row
        # Visible names in priority order, "+1" for the third.
        assert _order(row, others[:SHARED_CAP])
        assert '<span class="shared-more">+1</span>' in row
        # The collection links sit next to the card link, not inside it.
        assert re.search(r"<a [^>]*data-card-modal[^>]*>Charizard</a>", row)
        assert not re.search(r"<a [^>]*>[^<]*<a ", row)


def test_shared_badges_without_overflow_and_single_collection(client):
    row = {"id": "c", "name": "Squirtle", "qty": 1}
    seed_import(client, [
        ("files", ("main.csv", make_csv("My Collection", [row, {"id": "d", "name": "Mew", "qty": 1}]), "text/csv")),
        ("files", ("g.csv", make_csv(GENERIC, [row, {"id": "d", "name": "Mew", "qty": 1}]), "text/csv")),
        ("files", ("v.csv", make_csv(VINTAGE, [row]), "text/csv")),
        ("files", ("i.csv", make_csv(ILLUSTRATOR, [row]), "text/csv")),
    ])
    ids = _collection_ids()
    # Exactly SHARED_CAP others: both visible, no "+N".
    coll = client.get(f"/collections/{ids[GENERIC]}").text
    assert _expected_badge("also in", [ILLUSTRATOR, VINTAGE], ids) in coll
    assert "shared-more" not in coll
    dash = client.get("/").text
    assert _expected_badge("shared with", [ILLUSTRATOR, GENERIC], ids) in _dashboard_leaf_row(dash, VINTAGE, "Squirtle")
    # Mew is only in GENERIC: no badge on its row.
    assert "shared-badge" not in _dashboard_leaf_row(dash, GENERIC, "Mew")


# ── Dashboard: in-transit cards under "Incoming" (issue #393) ────────────
# Driven by Card.in_transit (#382), never by a collection: an in-transit
# card is counted only in the Incoming row, with a "shared with" badge
# naming its real collections; a leftover "Incoming" collection (prod's
# dead collection 10) adds no second row and no "shared with Incoming".


def _tag_with_dead_incoming(*card_ids):
    with db_module.SessionLocal() as s:
        dead = Collection(name="Incoming", priority_rank=99)
        cards = [s.get(Card, pk) for pk in card_ids]
        for card in cards:
            card.collections.append(dead)
        s.commit()


def _leaf_rows(html, row_id):
    return re.findall(rf'<tr class="grandchild-row[^"]*" data-group="{row_id}" hidden>.*?</tr>', html, re.S)


def test_breakdown_counts_in_transit_card_only_under_incoming(client):
    import queries

    ids = _seed(client, incoming=True)
    _tag_with_dead_incoming(ids["a"], ids["b"])
    with db_module.SessionLocal() as s:
        bd = queries.collection_membership_breakdown(s)
        names = [b.name for b in bd["children"]]
        # The dead collection never becomes a row.
        assert "Incoming" not in names
        # Charizard (in transit) left its four collections; Bulbasaur stays.
        assert [c.name for c in bd["incoming"].cards] == ["Charizard"]
        for b in bd["children"]:
            assert "Charizard" not in [c.name for c in b.cards], b.name
        assert ILLUSTRATOR not in names  # Charizard was its only card
        assert [c.name for c in next(b for b in bd["children"] if b.name == GENERIC).cards] == ["Bulbasaur"]
        # Each card still counted once: Collections + Bulk == Total.
        assert bd["collections"].qty == 2 and bd["bulk"].qty == 0
        assert bd["collections"].qty + bd["bulk"].qty == bd["total"].qty
        # Incoming isn't a "child", so it can't be the KPI's top collection.
        assert bd["incoming"] not in bd["children"]


def test_in_transit_bulk_card_counts_under_incoming_not_bulk(db_session):
    import queries

    card = Card(card_id="jpn_x-9", name="Mew", qty=1, in_transit_qty=1)
    db_session.add(card)
    db_session.flush()
    bd = queries.collection_membership_breakdown(db_session)
    assert bd["incoming"].cards == [card] and bd["bulk"].cards == []
    assert bd["collections"].qty == 1 and bd["total"].qty == 1


def test_dashboard_lists_in_transit_card_under_incoming_with_shared_badge(client):
    ids = _seed(client, incoming=True)
    _tag_with_dead_incoming(ids["a"], ids["b"])
    cids = _collection_ids()
    html = client.get("/").text

    # Exactly one Incoming row, from the status.
    assert html.count('data-row-id="coll-incoming"') == 1
    assert len(re.findall(r'<span class="arrow">[^<]*</span> Incoming</button>', html)) == 1
    rows = _leaf_rows(html, "coll-incoming")
    assert len(rows) == 1 and ">Charizard</a>" in rows[0]
    # "shared with" names its real collections, never Incoming.
    assert _expected_badge("shared with", ALL_FOUR, cids) in rows[0]

    # Not listed under any real collection row, and no "shared with Incoming".
    for row_id in re.findall(r'data-row-id="(coll-\d+)"', html):
        assert not any(">Charizard</a>" in r for r in _leaf_rows(html, row_id)), row_id
    assert f'href="/collections/{cids["Incoming"]}"' not in html
    assert "Also in: Incoming" not in html and ', Incoming"' not in html
    # Bulbasaur (dead tag only, not in transit) stays in its collection.
    assert "shared-badge" not in _dashboard_leaf_row(html, GENERIC, "Bulbasaur")


def test_dashboard_has_no_incoming_row_when_nothing_is_in_transit(client):
    ids = _seed(client)
    _tag_with_dead_incoming(ids["b"])
    html = client.get("/").text
    assert 'data-row-id="coll-incoming"' not in html
    assert not re.search(r'<span class="arrow">[^<]*</span> Incoming</button>', html)
