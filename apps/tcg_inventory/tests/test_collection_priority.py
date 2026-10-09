"""A card's collections are listed in priority order, primary first, with
"On the way" leading when the card is in transit (issue #389; README
"Business rules" #1 and #3)."""
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
    ranks = [constants.priority_rank_for(n) for n in ("Incoming", ILLUSTRATOR, VINTAGE, GENERIC, UNKNOWN)]
    assert ranks == sorted(ranks) and len(set(ranks)) == 5


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


def test_leftover_incoming_collection_ranks_first(db_session):
    # Prod's dead pre-#382 "Incoming" collection (stored rank 99).
    card = _card_with(db_session, [ILLUSTRATOR, "Incoming"], ranks={"Incoming": 99})
    assert card.primary_collection.name == "Incoming"


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


def test_inventory_lists_collections_in_priority_order_primary_marked(client):
    _seed(client)
    cell = _collections_cell(client.get("/inventory").text, "Charizard")
    assert _order(cell, [ILLUSTRATOR, VINTAGE, GENERIC, UNKNOWN])
    assert re.search(rf'<strong class="primary-collection"[^>]*><a [^>]*>{ILLUSTRATOR}</a></strong>', cell)
    assert cell.count("primary-collection") == 1
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
    assert 'class="primary-collection"' in dd


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
