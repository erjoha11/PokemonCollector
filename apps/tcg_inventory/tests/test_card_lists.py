"""Want and sale lists (issue #370, epic #366): card_lists.py, the Lists
section (on /collections since #378), /lists/{id}, and a master-set block's
bulk "Add to list". Offline.

Reuses test_master_set's sv2a fixture. Its checklist state, as it bears on
lists:
- missing master-set prints: Poké Ball #2, main #4, Poké Ball #5, secret #7
  (#7's only card is sold, qty 0, price 400); Master Ball #2, #4, #5 are
  missing but never count;
- unmatched owned cards on numbers 1 and 2 (a Reverse Holo #1 and an
  unlinked #2), so a missing #2 print is "possibly owned";
- spares: main #1 = 2 (10 kr), main #3 = 2 across two Dex rows (100 kr),
  Poké Ball #4 = 1 (50 kr): 5 spares worth 270 kr.
"""
from __future__ import annotations

import datetime as dt
import re

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.orm import sessionmaker

import card_lists
import db as db_module
import queries
from form_validation import FormError
from models import Card, CardList, CardListItem, Listing, MasterCard
from test_master_set import DISPLAY, build

HX = {"HX-Request": "true"}


def _master(db, number, variant):
    return db.query(MasterCard).filter_by(language="ja", set_code="sv2a", number=str(number), variant=variant).one()


def _statuses(detail):
    return {(v.master.number, v.master.variant): v.status_label for v in detail.items}


def _missing_entries(db, track="all", show="all"):
    detail = queries.master_set_detail(db, "ja", "sv2a")
    return [(s.master.id, 1) for s in queries.filter_master_set_slots(detail, track, show) if not s.owned]


def _spare_entries(db):
    detail = queries.master_set_detail(db, "ja", "sv2a")
    return [(s.master.id, s.spares) for s in detail.slots if s.spares]


def _card_state(db):
    return sorted((c.id, c.qty, c.binder_id, tuple(sorted(x.id for x in c.collections))) for c in db.query(Card))


# --------------------------------------------------------------------------
# Status computation
# --------------------------------------------------------------------------
def test_want_statuses_including_unmatched(db_session):
    build(db_session)
    wants = card_lists.create_list(db_session, "Wants", "want")
    card_lists.add_items(db_session, wants, _missing_entries(db_session), source="missing")
    card_lists.add_items(
        db_session, wants, [(_master(db_session, 1, "normal").id, 1), (_master(db_session, 3, "holo").id, 5)]
    )
    db_session.commit()

    detail = card_lists.list_detail(db_session, wants)
    assert _statuses(detail) == {
        ("1", "normal"): "Got it",  # 3 owned, 1 wanted
        ("2", "poke_ball_holo"): "Possibly owned (unmatched)",  # an unmatched #2 is owned
        ("3", "holo"): "Got 3 of 5",  # two Dex rows, 2 + 1
        ("4", "normal"): "Missing",  # #4 owned only as Poké Ball: no unmatched #4
        ("5", "poke_ball_holo"): "Missing",
        ("7", "holo"): "Missing",  # its only card is sold
    }
    # Sorted by set, then number, base print before ball prints.
    assert [(v.master.number, v.slot.track) for v in detail.items] == [
        ("1", "main"), ("2", "poke_ball"), ("3", "main"), ("4", "main"), ("5", "poke_ball"), ("7", "secret"),
    ]
    assert detail.summary == "3 missing · 1 possibly owned · 1 partly got · 1 got it"
    assert [v.master.number for v in detail.got_items] == ["1"]


def test_want_status_follows_a_dex_sync_with_no_list_edits(db_session):
    build(db_session)
    wants = card_lists.create_list(db_session, "Wants", "want")
    master4 = _master(db_session, 4, "normal")
    card_lists.add_items(db_session, wants, [(master4.id, 1)])
    db_session.commit()
    assert _statuses(card_lists.list_detail(db_session, wants)) == {("4", "normal"): "Missing"}

    # The next Dex sync brings in a #4 and links it to the master card.
    db_session.add(
        Card(card_id="jpn_sv2a-4", name="Mon 4", variant="Normal", qty=1, language="Korean",
             set="Pokémon Card 151", master_card=master4, market_price=12.0, price_flags="")
    )
    db_session.commit()
    db_session.expire_all()
    assert _statuses(card_lists.list_detail(db_session, wants)) == {("4", "normal"): "Got it"}


def test_sale_statuses(db_session):
    build(db_session)
    sale = card_lists.create_list(db_session, "Spares", "sale")
    card_lists.add_items(db_session, sale, _spare_entries(db_session), source="spares")
    db_session.commit()
    assert set(_statuses(card_lists.list_detail(db_session, sale)).values()) == {"Available"}

    items = {(i.master_card.number, i.master_card.variant): i for i in sale.items}
    items[("1", "normal")].qty = 3  # more than its 2 spares
    pb4_card = db_session.query(Card).filter_by(card_id="jpn_sv2a-4", variant="Poké Ball Holo").one()
    db_session.add(Listing(created_at=dt.datetime(2026, 10, 1), title="t", description="d", status="active", cards=[pb4_card]))
    for c in db_session.query(Card).filter_by(card_id="jpn_sv2a-3"):
        c.qty = 0  # sold every copy of #3
    db_session.commit()

    detail = card_lists.list_detail(db_session, sale)
    assert _statuses(detail) == {
        ("1", "normal"): "Not enough spares",
        ("3", "holo"): "Sold out",
        ("4", "poke_ball_holo"): "Listed",
    }
    listed = next(v for v in detail.items if v.status == "listed")
    assert listed.listing_ids


def test_delisted_or_sold_listing_is_not_listed(db_session):
    build(db_session)
    sale = card_lists.create_list(db_session, "Spares", "sale")
    card_lists.add_items(db_session, sale, _spare_entries(db_session))
    pb4_card = db_session.query(Card).filter_by(card_id="jpn_sv2a-4", variant="Poké Ball Holo").one()
    for status in ("delisted", "sold"):
        db_session.add(Listing(created_at=dt.datetime(2026, 10, 1), title="t", description="d", status=status, cards=[pb4_card]))
    db_session.commit()
    assert "Listed" not in _statuses(card_lists.list_detail(db_session, sale)).values()


def test_spares_agree_with_the_set_page(db_session):
    build(db_session)
    sale = card_lists.create_list(db_session, "Spares", "sale")
    card_lists.add_items(db_session, sale, _spare_entries(db_session), source="spares")
    db_session.commit()
    set_detail = queries.master_set_detail(db_session, "ja", "sv2a")
    detail = card_lists.list_detail(db_session, sale)

    assert {v.master.id: v.spares for v in detail.items} == {s.master.id: s.spares for s in set_detail.spare_slots}
    assert sum(i.qty for i in sale.items) == set_detail.spares == 5
    # "Est. value of spares" = market price × min(qty, spares) = the set page's spare value.
    assert detail.est_total == set_detail.spare_value == 270


def test_want_total_and_unpriced(db_session):
    build(db_session)
    wants = card_lists.create_list(db_session, "Wants", "want")
    card_lists.add_items(db_session, wants, _missing_entries(db_session))
    card_lists.add_items(db_session, wants, [(_master(db_session, 3, "holo").id, 5)])
    db_session.commit()
    detail = card_lists.list_detail(db_session, wants)
    prices = {v.master.number: v.price for v in detail.items}
    # #7 is unowned but has a sold card's price; #2/#4/#5 have no price data.
    assert prices == {"2": None, "3": 100.0, "4": None, "5": None, "7": 400.0}
    # Market price × copies still missing: #7 400 × 1, #3 100 × (5 - 3).
    assert detail.est_total == 600
    assert detail.unpriced == 3


def test_copy_as_text(db_session):
    build(db_session)
    wants = card_lists.create_list(db_session, "Wants", "want")
    card_lists.add_items(db_session, wants, _missing_entries(db_session))
    card_lists.add_items(db_session, wants, [(_master(db_session, 1, "normal").id, 1)])  # got it: left out
    db_session.commit()
    item7 = next(i for i in wants.items if i.master_card.number == "7")
    item7.target_price = 350
    db_session.commit()
    assert card_lists.list_detail(db_session, wants).text.splitlines() == [
        "Wants (want list)",
        f"{DISPLAY} #2 Mon 2 · Poké Ball ×1",
        f"{DISPLAY} #4 Mon 4 ×1",
        f"{DISPLAY} #5 Mon 5 · Poké Ball ×1",
        f"{DISPLAY} #7 Mon 7 ×1 — 350 kr",  # target price over market (400)
    ]

    sale = card_lists.create_list(db_session, "Spares", "sale")
    card_lists.add_items(db_session, sale, _spare_entries(db_session))
    db_session.commit()
    assert card_lists.list_detail(db_session, sale).text.splitlines() == [
        "Spares (sale list)",
        f"{DISPLAY} #1 Mon 1 ×2 — 10 kr",
        f"{DISPLAY} #3 Mon 3 ×2 — 100 kr",
        f"{DISPLAY} #4 Mon 4 · Poké Ball ×1 — 50 kr",
    ]


def test_copy_as_text_without_a_checklist_uses_set_name_and_corrected_language(db_session):
    master = MasterCard(language="ja", set_code="s12a", number="12", variant="normal", name="Pika", set_name="VSTAR Universe")
    db_session.add(master)
    db_session.add(Card(card_id="jpn_s12a-12", name="Pika", variant="Normal", qty=1, language="Korean",
                        set="VSTAR Universe", master_card=master, market_price=45.0, price_flags=""))
    wants = card_lists.create_list(db_session, "W", "want")
    card_lists.add_items(db_session, wants, [(master.id, 2)])
    db_session.commit()
    detail = card_lists.list_detail(db_session, wants)
    assert detail.items[0].status_label == "Got 1 of 2"
    assert detail.text.splitlines()[1] == "VSTAR Universe (KR) #12 Pika · Normal ×1 — 45 kr"


# --------------------------------------------------------------------------
# Writes
# --------------------------------------------------------------------------
def test_add_items_is_idempotent_and_counts(db_session):
    build(db_session)
    wants = card_lists.create_list(db_session, "Wants", "want")
    entries = _missing_entries(db_session)
    assert card_lists.add_items(db_session, wants, entries, source="missing") == (4, 0)
    first = {i.master_card_id: i.id for i in db_session.query(CardListItem)}
    item = db_session.query(CardListItem).first()
    item.qty, item.note = 3, "keep"
    db_session.commit()

    assert card_lists.add_items(db_session, wants, entries, source="missing") == (0, 4)
    # A duplicate inside one batch is added once (the second is "already on list").
    extra = _master(db_session, 1, "normal").id
    assert card_lists.add_items(db_session, wants, entries + [(extra, 1), (extra, 1)]) == (1, 5)
    db_session.commit()
    rows = db_session.query(CardListItem).all()
    assert len(rows) == 5
    assert {i.master_card_id: i.id for i in rows if i.master_card_id != extra} == first
    db_session.refresh(item)
    assert (item.qty, item.note) == (3, "keep")  # an existing item is never touched


def test_remove_got_it(db_session):
    build(db_session)
    wants = card_lists.create_list(db_session, "Wants", "want")
    card_lists.add_items(db_session, wants, _missing_entries(db_session))
    card_lists.add_items(db_session, wants, [(_master(db_session, n, v).id, 1) for n, v in ((1, "normal"), (2, "normal"))])
    db_session.commit()
    assert card_lists.remove_got_it(db_session, wants) == 2
    db_session.commit()
    db_session.expire_all()
    assert len(wants.items) == 4
    assert card_lists.list_detail(db_session, wants).got_items == []


def test_create_list_validation(db_session):
    with pytest.raises(FormError):
        card_lists.create_list(db_session, "  ", "want")
    with pytest.raises(FormError):
        card_lists.create_list(db_session, "X", "trade")


def test_lists_never_touch_cards(db_session):
    build(db_session)
    before = _card_state(db_session)
    wants = card_lists.create_list(db_session, "Wants", "want")
    sale = card_lists.create_list(db_session, "Spares", "sale")
    card_lists.add_items(db_session, wants, _missing_entries(db_session))
    card_lists.add_items(db_session, sale, _spare_entries(db_session))
    card_lists.add_items(db_session, wants, [(_master(db_session, 1, "normal").id, 1)])
    card_lists.remove_got_it(db_session, wants)
    db_session.delete(sale)
    db_session.commit()
    assert _card_state(db_session) == before
    assert db_session.query(CardListItem).filter_by(list_id=sale.id).count() == 0  # cascade


# --------------------------------------------------------------------------
# Schema
# --------------------------------------------------------------------------
def test_migration_from_v15_creates_list_tables(monkeypatch, tmp_path):
    from sqlalchemy import create_engine

    engine = create_engine(f"sqlite:///{tmp_path / 'm.db'}")
    monkeypatch.setattr(db_module, "engine", engine)
    monkeypatch.setattr(db_module, "SessionLocal", sessionmaker(bind=engine))
    db_module.init_db()
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE card_list_items"))
        conn.execute(text("DROP TABLE card_lists"))
    db_module._set_schema_version(15)

    db_module.init_db()

    inspector = inspect(engine)
    assert inspector.has_table("card_lists") and inspector.has_table("card_list_items")
    assert {"list_id", "master_card_id", "qty", "target_price", "note", "source", "added_at"} <= {
        c["name"] for c in inspector.get_columns("card_list_items")
    }
    assert db_module._get_schema_version() == db_module.CURRENT_SCHEMA_VERSION >= 16


def test_rls_covers_the_list_tables(monkeypatch):
    statements = []

    class _Conn:
        def execute(self, statement, *args):
            statements.append(str(statement))
            return []

    class _Begin:
        def __enter__(self):
            return _Conn()

        def __exit__(self, *exc):
            return False

    class _FakePostgres:
        class dialect:
            name = "postgresql"

        def begin(self):
            return _Begin()

    monkeypatch.setattr(db_module, "engine", _FakePostgres())
    db_module._enable_row_level_security()
    for table in ("card_lists", "card_list_items"):
        assert f'ALTER TABLE public."{table}" ENABLE ROW LEVEL SECURITY' in statements
    assert not any("GRANT" in s.upper() for s in statements)


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------
def _session():
    return db_module.SessionLocal()


def _seed():
    with _session() as db:
        build(db)


def _new_list(client, name, kind):
    resp = client.post("/lists", data={"name": name, "kind": kind}, headers=HX)
    assert resp.status_code == 200
    return int(resp.headers["HX-Redirect"].rsplit("/", 1)[1])


def test_collections_lists_section_and_new_list(client):
    _seed()
    html = client.get("/collections").text
    assert 'id="lists"' in html and "No want lists yet." in html and "No sale lists yet." in html
    assert 'hx-post="/lists"' in html

    list_id = _new_list(client, "Wants", "want")
    resp = client.post("/lists", data={"name": "Spares", "kind": "sale"}, follow_redirects=False)
    assert resp.status_code == 303 and resp.headers["location"].startswith("/lists/")
    client.post("/sets/ja/sv2a/add-to-list", data={"what": "missing", "list_id": str(list_id)}, headers=HX)

    html = client.get("/collections").text
    want_part = html.split('id="lists-want"', 1)[1].split('id="lists-sale"', 1)[0]
    assert f'<a href="/lists/{list_id}">Wants</a>' in want_part
    assert "3 missing · 1 possibly owned" in want_part
    assert ">4</td>" in want_part
    assert "Spares" in html.split('id="lists-sale"', 1)[1]


@pytest.mark.parametrize("data, message", [
    ({"name": " ", "kind": "want"}, "Give the list a name."),
    ({"name": "X", "kind": "trade"}, "Pick a kind"),
])
def test_new_list_rejected(client, data, message):
    resp = client.post("/lists", data=data, headers=HX)
    assert resp.status_code == 422 and message in resp.text
    with _session() as db:
        assert db.query(CardList).count() == 0


def test_set_page_add_panel_is_filter_aware(client):
    _seed()  # no collection holds the set: /sets/... renders the block itself
    html = client.get("/sets/ja/sv2a").text
    assert 'id="set-list-add-ja-sv2a"' in html and 'id="set-list-add-status-ja-sv2a"' in html
    assert "Add 4 missing" in html  # no Master Ball by default
    assert "Add 3 spares" in html and "(5 copies)" in html
    assert '<option value="new" selected>New list…</option>' in html  # no lists yet

    mb = client.get("/sets/ja/sv2a?track=master_ball").text
    assert "Add 3 missing" in mb and "Add 0 spares" in mb
    assert 'name="track" value="master_ball"' in mb

    owned = client.get("/sets/ja/sv2a?show=owned").text
    assert re.search(r'<button type="submit" class="secondary" disabled>Add 0 missing', owned)


def test_bulk_add_missing_idempotent_with_counts(client):
    _seed()
    resp = client.post("/sets/ja/sv2a/add-to-list", data={"what": "missing", "list_id": "new", "new_list_name": "151 wants"}, headers=HX)
    assert resp.status_code == 200
    assert 'id="set-list-add-status-ja-sv2a"' in resp.text
    assert "Added 4: <a href=" in resp.text
    with _session() as db:
        wl = db.query(CardList).one()
        assert (wl.name, wl.kind) == ("151 wants", "want")
        assert {(i.master_card.number, i.master_card.variant, i.qty, i.source) for i in wl.items} == {
            ("2", "poke_ball_holo", 1, "missing"),
            ("4", "normal", 1, "missing"),
            ("5", "poke_ball_holo", 1, "missing"),
            ("7", "holo", 1, "missing"),
        }
        list_id = wl.id
    # The new list is now in the select, picked.
    assert f'<option value="{list_id}" selected>151 wants</option>' in resp.text

    again = client.post("/sets/ja/sv2a/add-to-list", data={"what": "missing", "list_id": str(list_id)}, headers=HX)
    assert "Added 0, 4 already on list" in again.text
    mb = client.post(
        "/sets/ja/sv2a/add-to-list", data={"what": "missing", "list_id": str(list_id), "track": "master_ball"}, headers=HX
    )
    assert "Added 3: " in mb.text
    with _session() as db:
        assert db.query(CardListItem).count() == 7


def test_bulk_add_spares_uses_the_spare_count(client):
    _seed()
    list_id = _new_list(client, "Spares", "sale")
    resp = client.post("/sets/ja/sv2a/add-to-list", data={"what": "spares", "list_id": str(list_id)}, headers=HX)
    assert "Added 3: " in resp.text
    with _session() as db:
        assert {(i.master_card.number, i.master_card.variant, i.qty, i.source) for i in db.query(CardListItem)} == {
            ("1", "normal", 2, "spares"),
            ("3", "holo", 2, "spares"),
            ("4", "poke_ball_holo", 1, "spares"),
        }


@pytest.mark.parametrize("what, kind, form, message", [
    ("missing", "sale", {}, "is a sale list: missing go on a want list"),
    ("spares", "want", {}, "is a want list: spares go on a sale list"),
    ("missing", None, {"list_id": "new", "new_list_name": ""}, "Give the list a name."),
    ("missing", None, {"list_id": ""}, "Pick a list"),
    ("missing", None, {"list_id": "999"}, "Pick a list"),
])
def test_bulk_add_rejected(client, what, kind, form, message):
    _seed()
    data = {"what": what, **form}
    if kind:
        data["list_id"] = str(_new_list(client, "L", kind))
    resp = client.post("/sets/ja/sv2a/add-to-list", data=data, headers=HX)
    assert resp.status_code == 422 and message in resp.text
    with _session() as db:
        assert db.query(CardListItem).count() == 0


def test_bulk_add_without_htmx_redirects_back_with_the_filter(client):
    _seed()
    list_id = _new_list(client, "W", "want")
    resp = client.post(
        "/sets/ja/sv2a/add-to-list",
        data={"what": "missing", "list_id": str(list_id), "track": "poke_ball", "show": "missing"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/sets/ja/sv2a?track=poke_ball&show=missing"
    with _session() as db:
        assert db.query(CardListItem).count() == 2
    # From a collection page (#378): back to that collection's section.
    resp = client.post(
        "/sets/ja/sv2a/add-to-list",
        data={"what": "missing", "list_id": str(list_id), "track": "poke_ball", "show": "missing", "collection_id": "6"},
        follow_redirects=False,
    )
    assert resp.headers["location"] == "/collections/6?set=ja:sv2a&track=poke_ball&show=missing#set-ja-sv2a"


def _want_list(client):
    _seed()
    list_id = _new_list(client, "Wants", "want")
    client.post("/sets/ja/sv2a/add-to-list", data={"what": "missing", "list_id": str(list_id)}, headers=HX)
    with _session() as db:
        wl = db.get(CardList, list_id)
        card_lists.add_items(db, wl, [(_master(db, 1, "normal").id, 1)])
        db.commit()
    return list_id


def _item_id(number, variant):
    with _session() as db:
        return db.query(CardListItem).join(MasterCard).filter(MasterCard.number == number, MasterCard.variant == variant).one().id


def test_want_list_page(client):
    list_id = _want_list(client)
    resp = client.get(f"/lists/{list_id}")
    assert resp.status_code == 200
    html = resp.text
    assert '<a href="/collections" class="active">Collections</a>' in html
    assert '<p class="muted breadcrumb"><a href="/collections">Collections</a></p>' in html
    assert "<h1>Wants" in html and "Want list" in html
    assert "/cards/None" not in html
    assert html.count('class="card-list-row"') == 5
    assert 'list-status list-status-missing">Missing' in html
    # Via /sets/..., which redirects to the home collection's #unmatched-<set>.
    assert 'list-status-unmatched">Possibly owned (unmatched)</span> <a href="/sets/ja/sv2a?at=unmatched"' in html
    assert 'list-status-got">Got it' in html
    assert "Est. cost to complete" in html and "400 kr" in html
    assert "3 items with no price data" in html
    assert "Remove got-it items (1)" in html
    assert 'href="/lists/%d?hide_got=1"' % list_id in html
    # Unowned prints: master card name, plain text; the owned one is a card link.
    assert '<span class="card-list-unowned">Mon 4</span>' in html
    assert "data-card-modal" in html
    assert 'id="list-copy-text"' in html and f"{DISPLAY} #7 Mon 7 ×1 — 400 kr" in html
    # Rows in set, then number order.
    order = [m.group(1) for m in re.finditer(r'<div class="muted card-list-meta">(#\d)', html)]
    assert order == ["#1", "#2", "#4", "#5", "#7"]

    hidden = client.get(f"/lists/{list_id}?hide_got=1").text
    assert hidden.count('class="card-list-row"') == 4
    assert 'class="viz-filter-pill active" aria-pressed="true">Hide got-it' in hidden


def test_sale_list_page_and_ad_handoff(client):
    _seed()
    list_id = _new_list(client, "Spares", "sale")
    client.post("/sets/ja/sv2a/add-to-list", data={"what": "spares", "list_id": str(list_id)}, headers=HX)
    html = client.get(f"/lists/{list_id}").text
    assert "Est. value of spares" in html and "270 kr" in html
    assert html.count('list-status-available">Available') == 3
    assert f'href="/lists/{list_id}/ad"' in html
    assert "Hide got-it" not in html and "Remove got-it" not in html

    resp = client.get(f"/lists/{list_id}/ad", follow_redirects=False)
    assert resp.status_code == 303
    with _session() as db:
        expected = {
            db.query(Card).filter_by(card_id="jpn_sv2a-1", variant="Normal").one().id,
            db.query(Card).filter_by(card_id="jpn_sv2a-4", variant="Poké Ball Holo").one().id,
            db.query(Card).filter_by(card_id="jpn_sv2a-3", variant="Holo").one().id,  # most copies of #3's two rows
        }
    assert resp.headers["location"].startswith("/sales?")
    assert {int(x) for x in re.findall(r"card_ids=(\d+)", resp.headers["location"])} == expected
    sales = client.get(resp.headers["location"])
    assert sales.status_code == 200 and "Mon 3" in sales.text


def test_inline_edit_row(client):
    list_id = _want_list(client)
    item_id = _item_id("7", "holo")
    url = f"/lists/{list_id}/items/{item_id}"

    edit = client.get(url + "?edit=1", headers=HX).text
    assert edit.lstrip().startswith("<tr") and 'name="qty"' in edit and 'name="target_price"' in edit and 'name="note"' in edit

    resp = client.post(url, data={"qty": "2", "target_price": "350,5", "note": "PSA?"}, headers=HX)
    assert resp.status_code == 200
    assert resp.headers["HX-Trigger"] == "list-changed"
    assert resp.text.lstrip().startswith("<tr") and "PSA?" in resp.text and "350 kr" in resp.text
    assert 'name="qty"' not in resp.text
    with _session() as db:
        item = db.get(CardListItem, item_id)
        assert (item.qty, item.target_price, item.note) == (2, 350.5, "PSA?")

    summary = client.get(f"/lists/{list_id}/summary").text
    assert 'id="list-summary"' in summary and 'hx-trigger="list-changed from:body"' in summary
    assert "800 kr" in summary  # 400 × 2 still missing

    cancel = client.get(url, headers=HX).text
    assert 'name="qty"' not in cancel


@pytest.mark.parametrize("data, message", [
    ({"qty": "0"}, "Qty must be a whole number"),
    ({"qty": "1.5"}, "Qty must be a whole number"),
    ({"qty": "1", "target_price": "abc"}, "Target price must be"),
])
def test_inline_edit_rejected(client, data, message):
    list_id = _want_list(client)
    item_id = _item_id("7", "holo")
    resp = client.post(f"/lists/{list_id}/items/{item_id}", data=data, headers=HX)
    assert resp.status_code == 422 and message in resp.text
    with _session() as db:
        assert db.get(CardListItem, item_id).qty == 1


def test_remove_item_rename_and_remove_got_it(client):
    list_id = _want_list(client)
    item_id = _item_id("4", "normal")
    resp = client.post(f"/lists/{list_id}/items/{item_id}/delete", headers=HX)
    assert resp.status_code == 200 and resp.text == "" and resp.headers["HX-Trigger"] == "list-changed"

    title = client.get(f"/lists/{list_id}/title?edit=1").text
    assert 'name="name" value="Wants"' in title
    renamed = client.post(f"/lists/{list_id}/rename", data={"name": "Buy next", "note": "card show"}, headers=HX)
    assert "<h1>Buy next" in renamed.text and "card show" in renamed.text
    assert client.post(f"/lists/{list_id}/rename", data={"name": ""}, headers=HX).status_code == 422

    resp = client.post(f"/lists/{list_id}/remove-got-it", headers=HX)
    assert resp.headers["HX-Redirect"] == f"/lists/{list_id}"
    with _session() as db:
        wl = db.get(CardList, list_id)
        assert wl.name == "Buy next" and wl.note == "card show"
        assert sorted(i.master_card.number for i in wl.items) == ["2", "5", "7"]


def test_delete_list(client):
    list_id = _want_list(client)
    with _session() as db:
        before = _card_state(db)
    html = client.get(f"/lists/{list_id}").text
    assert 'hx-confirm="Delete the list “Wants” and its 5 items?' in html
    resp = client.post(f"/lists/{list_id}/delete", headers=HX)
    assert resp.headers["HX-Redirect"] == "/collections"
    with _session() as db:
        assert db.query(CardList).count() == 0 and db.query(CardListItem).count() == 0
        assert _card_state(db) == before
    assert client.get(f"/lists/{list_id}").status_code == 404


def test_item_of_another_list_is_404(client):
    list_id = _want_list(client)
    other = _new_list(client, "Other", "want")
    item_id = _item_id("7", "holo")
    assert client.get(f"/lists/{other}/items/{item_id}").status_code == 404
    assert client.post(f"/lists/{other}/items/{item_id}/delete", headers=HX).status_code == 404
    with _session() as db:
        assert db.get(CardListItem, item_id).list_id == list_id
