""""Listed" badge on Inventory and /sales, plus /sales' already-listed
warning (issue #257)."""

import datetime as dt
import re

from sqlalchemy import event


def _seed(client):
    """Two cards, one in a collection and a binder so the no-mutation
    assertions below actually have something to lose."""
    import db as db_module
    from models import Binder, Card, Collection

    db = db_module.SessionLocal()
    try:
        binder = Binder(name="Binder A")
        coll = Collection(name="Main", priority_rank=1)
        pika = Card(card_id="t-1", name="Pikachu", qty=2, binder=binder)
        pika.collections = [coll]
        zard = Card(card_id="t-2", name="Charizard", qty=1)
        db.add_all([binder, coll, pika, zard])
        db.commit()
        return {"pika": pika.id, "zard": zard.id}
    finally:
        db.close()


def _add_listing(card_ids, status="active", created_at=None):
    import db as db_module
    from models import Card, Listing

    db = db_module.SessionLocal()
    try:
        listing = Listing(
            created_at=created_at or dt.datetime(2026, 9, 1),
            title="Ad",
            description="Desc",
            suggested_price=100,
            platform="finn.no",
            status=status,
        )
        listing.cards = db.query(Card).filter(Card.id.in_(card_ids)).all()
        db.add(listing)
        db.commit()
        return listing.id
    finally:
        db.close()


def _card_state():
    import db as db_module
    from models import Card

    db = db_module.SessionLocal()
    try:
        return {
            c.id: (c.qty, c.binder_id, sorted(col.id for col in c.collections))
            for c in db.query(Card).all()
        }
    finally:
        db.close()


def _row(html, name):
    """The <tr> containing `name`, so badge assertions are per-card."""
    match = re.search(r"<tr[^>]*>(?:(?!</tr>).)*?" + re.escape(name) + r".*?</tr>", html, re.S)
    assert match, f"no row for {name}"
    return match.group(0)


# --- queries.active_listings_by_card -------------------------------------


def test_helper_maps_only_active_listings_newest_first(client):
    import db as db_module
    import queries

    ids = _seed(client)
    old = _add_listing([ids["pika"]], created_at=dt.datetime(2026, 8, 1))
    new = _add_listing([ids["pika"]], created_at=dt.datetime(2026, 9, 1))
    _add_listing([ids["zard"]], status="delisted")
    _add_listing([ids["zard"]], status="sold")

    db = db_module.SessionLocal()
    try:
        assert queries.active_listings_by_card(db) == {ids["pika"]: [new, old]}
        assert queries.active_listings_by_card(db, [ids["zard"]]) == {}
        assert queries.active_listings_by_card(db, []) == {}
    finally:
        db.close()


def test_helper_runs_a_single_query(client):
    import db as db_module
    import queries

    ids = _seed(client)
    for _ in range(5):
        _add_listing([ids["pika"], ids["zard"]])

    db = db_module.SessionLocal()
    statements = []
    engine = db.get_bind()
    listener = lambda *args: statements.append(args[2])
    event.listen(engine, "before_cursor_execute", listener)
    try:
        result = queries.active_listings_by_card(db)
    finally:
        event.remove(engine, "before_cursor_execute", listener)
        db.close()

    assert len(result[ids["pika"]]) == 5
    assert len(statements) == 1, statements


# --- Inventory ------------------------------------------------------------


def test_inventory_shows_listed_badge_linked_to_listing(client):
    ids = _seed(client)
    listing_id = _add_listing([ids["pika"]])

    html = client.get("/inventory").text

    pika_row = _row(html, "Pikachu")
    assert "listed-badge" in pika_row
    assert f'href="/listings#listing-{listing_id}"' in pika_row
    assert "listed-badge" not in _row(html, "Charizard")


def test_inventory_badge_links_newest_and_shows_count_for_multiple(client):
    ids = _seed(client)
    _add_listing([ids["pika"]], created_at=dt.datetime(2026, 8, 1))
    newest = _add_listing([ids["pika"]], created_at=dt.datetime(2026, 9, 1))

    pika_row = _row(client.get("/inventory").text, "Pikachu")

    assert f'href="/listings#listing-{newest}"' in pika_row
    assert "&times;2" in pika_row


def test_inventory_no_badge_for_delisted_or_sold(client):
    ids = _seed(client)
    _add_listing([ids["pika"]], status="delisted")
    _add_listing([ids["zard"]], status="sold")

    html = client.get("/inventory").text

    assert "listed-badge" not in html


def test_listing_anchor_exists_on_listings_page(client):
    ids = _seed(client)
    listing_id = _add_listing([ids["pika"]])

    assert f'id="listing-{listing_id}"' in client.get("/listings").text


# --- /sales ---------------------------------------------------------------


def test_sales_review_badge_and_warning_only_for_listed_card(client):
    ids = _seed(client)
    listing_id = _add_listing([ids["pika"]])

    html = client.get("/sales", params={"card_ids": [ids["pika"], ids["zard"]]}).text

    assert 'id="already-listed-warning"' in html
    assert "1 of the selected cards" in html
    pika_row = _row(html, "Pikachu")
    assert f'href="/listings#listing-{listing_id}"' in pika_row
    assert "Already in an active listing" in pika_row
    zard_row = _row(html, "Charizard")
    assert "listed-badge" not in zard_row
    assert "Already in an active listing" not in zard_row


def test_sales_review_no_warning_when_nothing_actively_listed(client):
    ids = _seed(client)
    _add_listing([ids["pika"]], status="delisted")
    _add_listing([ids["zard"]], status="sold")

    html = client.get("/sales", params={"card_ids": [ids["pika"], ids["zard"]]}).text

    assert "already-listed-warning" not in html
    assert "listed-badge" not in html
    assert "Already in an active listing" not in html


def test_ad_draft_repeats_warning_next_to_mark_as_listed(client):
    ids = _seed(client)
    _add_listing([ids["pika"]])
    form = {"card_id": [ids["pika"], ids["zard"]], "qty": [1, 1], "condition": ["", ""], "price": ["", ""]}

    html = client.post("/sales/generate", data=form).text

    assert "1 card in this ad is already in another active listing" in html


def test_ad_draft_no_warning_when_not_listed(client):
    ids = _seed(client)
    form = {"card_id": [ids["zard"]], "qty": [1], "condition": [""], "price": [""]}

    html = client.post("/sales/generate", data=form).text

    assert "already in another active listing" not in html


def test_marking_already_listed_card_listed_again_is_allowed(client):
    """Warning, not a block -- the second listing is still created."""
    import db as db_module
    from models import Listing

    ids = _seed(client)
    _add_listing([ids["pika"]])

    response = client.post(
        "/sales/mark-listed",
        data={"card_id": [ids["pika"]], "title": "Again", "description": "D", "suggested_price": "50"},
    )

    assert response.status_code == 200
    db = db_module.SessionLocal()
    try:
        assert db.query(Listing).filter(Listing.status == "active").count() == 2
    finally:
        db.close()


def test_badge_and_warning_flow_never_touch_qty_collections_or_binder(client):
    ids = _seed(client)
    _add_listing([ids["pika"]])
    before = _card_state()

    client.get("/inventory")
    client.get("/sales", params={"card_ids": [ids["pika"], ids["zard"]]})
    client.post(
        "/sales/generate",
        data={"card_id": [ids["pika"]], "qty": [1], "condition": [""], "price": [""]},
    )
    client.post(
        "/sales/mark-listed",
        data={"card_id": [ids["pika"]], "title": "Again", "description": "D", "suggested_price": "50"},
    )

    after = _card_state()
    assert after == before
    assert before[ids["pika"]][0] == 2
    assert before[ids["pika"]][1] is not None
    assert before[ids["pika"]][2]
