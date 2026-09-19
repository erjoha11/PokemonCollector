import datetime as dt

from conftest import make_csv, seed_import


def _seed_cards(client):
    import db as db_module
    from models import Card

    main = make_csv(
        "My Collection",
        [
            {"id": "a", "name": "Pikachu", "price": "10"},
            {"id": "b", "name": "Charizard", "price": "50"},
        ],
    )
    seed_import(client, [("files", ("main.csv", main, "text/csv"))])

    db = db_module.SessionLocal()
    try:
        return {c.card_id: c.id for c in db.query(Card).all()}
    finally:
        db.close()


def _mark_listed(client, card_ids, suggested_price="30"):
    return client.post(
        "/sales/mark-listed",
        data={
            "card_id": [str(cid) for cid in card_ids],
            "title": "Pokemon kort - Pikachu",
            "description": "Selger Pikachu.",
            "suggested_price": suggested_price,
        },
    )


def test_listings_page_with_no_listings_shows_empty_state(client):
    response = client.get("/listings")

    assert response.status_code == 200
    assert "No listings recorded yet" in response.text


def test_listings_page_shows_listed_card_and_status(client):
    ids = _seed_cards(client)
    _mark_listed(client, [ids["a"]])

    response = client.get("/listings")

    assert response.status_code == 200
    assert "Pikachu" in response.text
    assert "active" in response.text


def test_listings_page_shows_cost_market_and_listed_price_side_by_side(client):
    import db as db_module
    from models import Transaction

    ids = _seed_cards(client)

    db = db_module.SessionLocal()
    try:
        db.add(
            Transaction(
                card_id=ids["a"], type="purchase", date=dt.date(2026, 1, 5), price=7, fees=0,
            )
        )
        db.commit()
    finally:
        db.close()

    _mark_listed(client, [ids["a"]], suggested_price="30")

    response = client.get("/listings")

    assert response.status_code == 200
    # Cost (7 kr paid), market price (10 kr display_price from the CSV
    # import), and listed price (30 kr suggested_price) must all appear,
    # independently of each other.
    assert "7 kr" in response.text
    assert "10 kr" in response.text
    assert "30 kr" in response.text


def test_listings_page_does_not_change_qty_or_collections(client):
    import db as db_module
    from models import Card

    ids = _seed_cards(client)
    _mark_listed(client, [ids["a"]])

    client.get("/listings")

    db = db_module.SessionLocal()
    try:
        card = db.query(Card).filter(Card.id == ids["a"]).one()
        assert card.qty == 1
        assert card.binder_id is None
    finally:
        db.close()


def test_listings_nav_link_present_on_base_pages(client):
    response = client.get("/listings")

    assert response.status_code == 200
    assert 'href="/listings"' in response.text


def _listing_id(client):
    import db as db_module
    from models import Listing

    db = db_module.SessionLocal()
    try:
        return db.query(Listing).one().id
    finally:
        db.close()


def test_delist_sets_status_delisted(client):
    import db as db_module
    from models import Listing

    ids = _seed_cards(client)
    _mark_listed(client, [ids["a"]])
    listing_id = _listing_id(client)

    response = client.post(f"/listings/{listing_id}/delist")

    assert response.status_code == 200

    db = db_module.SessionLocal()
    try:
        listing = db.query(Listing).filter(Listing.id == listing_id).one()
        assert listing.status == "delisted"
    finally:
        db.close()


def test_delist_does_not_touch_qty_collections_binder_or_transactions(client):
    import db as db_module
    from models import Card, Transaction

    ids = _seed_cards(client)
    _mark_listed(client, [ids["a"]])
    listing_id = _listing_id(client)

    db = db_module.SessionLocal()
    try:
        tx_count_before = db.query(Transaction).count()
    finally:
        db.close()

    client.post(f"/listings/{listing_id}/delist")

    db = db_module.SessionLocal()
    try:
        card = db.query(Card).filter(Card.id == ids["a"]).one()
        assert card.qty == 1
        assert card.binder_id is None
        assert list(card.collections) == []
        assert db.query(Transaction).count() == tx_count_before
    finally:
        db.close()


def test_delisted_listing_excluded_from_default_listings_view(client):
    ids = _seed_cards(client)
    _mark_listed(client, [ids["a"]])
    listing_id = _listing_id(client)

    client.post(f"/listings/{listing_id}/delist")
    response = client.get("/listings")

    assert response.status_code == 200
    assert "No listings recorded yet" in response.text


def test_show_delisted_toggle_reveals_delisted_listing(client):
    ids = _seed_cards(client)
    _mark_listed(client, [ids["a"]])
    listing_id = _listing_id(client)

    client.post(f"/listings/{listing_id}/delist")
    response = client.get("/listings", params={"show_delisted": "true"})

    assert response.status_code == 200
    assert "Pikachu" in response.text
    assert "delisted" in response.text


def test_delist_htmx_response_omits_row_when_delisted_hidden(client):
    ids = _seed_cards(client)
    _mark_listed(client, [ids["a"]])
    listing_id = _listing_id(client)

    response = client.post(
        f"/listings/{listing_id}/delist",
        headers={"HX-Request": "true"},
    )

    assert response.status_code == 200
    assert response.text.strip() == ""


def test_delete_removes_listing_and_cascades_listing_cards(client):
    import db as db_module
    from models import Listing, listing_cards

    ids = _seed_cards(client)
    _mark_listed(client, [ids["a"], ids["b"]])
    listing_id = _listing_id(client)

    response = client.post(f"/listings/{listing_id}/delete")

    assert response.status_code == 200

    db = db_module.SessionLocal()
    try:
        assert db.query(Listing).filter(Listing.id == listing_id).first() is None
        assert db.query(listing_cards).filter(listing_cards.c.listing_id == listing_id).count() == 0
    finally:
        db.close()


def test_delete_does_not_touch_qty_collections_binder_or_transactions(client):
    import db as db_module
    from models import Card, Transaction

    ids = _seed_cards(client)
    _mark_listed(client, [ids["a"]])
    listing_id = _listing_id(client)

    db = db_module.SessionLocal()
    try:
        tx_count_before = db.query(Transaction).count()
    finally:
        db.close()

    client.post(f"/listings/{listing_id}/delete")

    db = db_module.SessionLocal()
    try:
        card = db.query(Card).filter(Card.id == ids["a"]).one()
        assert card.qty == 1
        assert card.binder_id is None
        assert list(card.collections) == []
        assert db.query(Transaction).count() == tx_count_before
    finally:
        db.close()


def test_delete_removes_listing_from_listings_page(client):
    ids = _seed_cards(client)
    _mark_listed(client, [ids["a"]])
    listing_id = _listing_id(client)

    client.post(f"/listings/{listing_id}/delete")
    response = client.get("/listings")

    assert response.status_code == 200
    assert "No listings recorded yet" in response.text


def test_delete_htmx_response_is_empty(client):
    ids = _seed_cards(client)
    _mark_listed(client, [ids["a"]])
    listing_id = _listing_id(client)

    response = client.post(
        f"/listings/{listing_id}/delete",
        headers={"HX-Request": "true"},
    )

    assert response.status_code == 200
    assert response.text.strip() == ""


def test_delete_nonexistent_listing_returns_404(client):
    response = client.post("/listings/999999/delete")

    assert response.status_code == 404


def test_delete_control_requires_confirmation(client):
    ids = _seed_cards(client)
    _mark_listed(client, [ids["a"]])

    response = client.get("/listings")

    assert response.status_code == 200
    assert "hx-confirm" in response.text


def test_edit_form_prefills_existing_title_description_price_and_cards(client):
    ids = _seed_cards(client)
    _mark_listed(client, [ids["a"]], suggested_price="30")
    listing_id = _listing_id(client)

    response = client.get(f"/listings/{listing_id}/edit")

    assert response.status_code == 200
    assert "Pokemon kort - Pikachu" in response.text
    assert "Selger Pikachu." in response.text
    assert "Pikachu" in response.text


def test_edit_updates_title_description_and_price(client):
    ids = _seed_cards(client)
    _mark_listed(client, [ids["a"]])
    listing_id = _listing_id(client)

    response = client.post(
        f"/listings/{listing_id}/edit",
        data={
            "title": "New title",
            "description": "New description.",
            "suggested_price": "45",
            "card_id": [str(ids["a"])],
        },
        follow_redirects=False,
    )

    assert response.status_code == 303

    import db as db_module
    from models import Listing

    db = db_module.SessionLocal()
    try:
        listing = db.query(Listing).filter(Listing.id == listing_id).one()
        assert listing.title == "New title"
        assert listing.description == "New description."
        assert listing.suggested_price == 45
    finally:
        db.close()


def test_edit_updates_card_set_add_and_remove(client):
    ids = _seed_cards(client)
    _mark_listed(client, [ids["a"]])
    listing_id = _listing_id(client)

    response = client.post(
        f"/listings/{listing_id}/edit",
        data={
            "title": "New title",
            "description": "New description.",
            "suggested_price": "",
            "card_id": [str(ids["b"])],
        },
        follow_redirects=False,
    )

    assert response.status_code == 303

    import db as db_module
    from models import Listing

    db = db_module.SessionLocal()
    try:
        listing = db.query(Listing).filter(Listing.id == listing_id).one()
        card_ids = {c.id for c in listing.cards}
        assert card_ids == {ids["b"]}
    finally:
        db.close()


def test_edit_rejects_empty_card_set(client):
    ids = _seed_cards(client)
    _mark_listed(client, [ids["a"]])
    listing_id = _listing_id(client)

    response = client.post(
        f"/listings/{listing_id}/edit",
        data={
            "title": "New title",
            "description": "New description.",
            "suggested_price": "",
            "card_id": [],
        },
    )

    assert response.status_code == 200
    assert "at least one card" in response.text.lower()

    import db as db_module
    from models import Listing

    db = db_module.SessionLocal()
    try:
        listing = db.query(Listing).filter(Listing.id == listing_id).one()
        assert listing.title != "New title"
        card_ids = {c.id for c in listing.cards}
        assert card_ids == {ids["a"]}
    finally:
        db.close()


def test_edit_does_not_touch_qty_collections_binder_or_transactions(client):
    import db as db_module
    from models import Card, Transaction

    ids = _seed_cards(client)
    _mark_listed(client, [ids["a"]])
    listing_id = _listing_id(client)

    db = db_module.SessionLocal()
    try:
        tx_count_before = db.query(Transaction).count()
    finally:
        db.close()

    client.post(
        f"/listings/{listing_id}/edit",
        data={
            "title": "New title",
            "description": "New description.",
            "suggested_price": "20",
            "card_id": [str(ids["a"])],
        },
    )

    db = db_module.SessionLocal()
    try:
        card = db.query(Card).filter(Card.id == ids["a"]).one()
        assert card.qty == 1
        assert card.binder_id is None
        assert list(card.collections) == []
        assert db.query(Transaction).count() == tx_count_before
    finally:
        db.close()


def test_regenerate_ad_text_reflects_currently_selected_cards(client):
    ids = _seed_cards(client)
    _mark_listed(client, [ids["a"]])
    listing_id = _listing_id(client)

    # Simulate the edit form now having Charizard selected instead of
    # Pikachu (an in-progress, not-yet-saved edit) -- regenerate must use
    # that, not what's still saved in the DB.
    response = client.post(
        f"/listings/{listing_id}/edit/regenerate",
        data={"card_id": [str(ids["b"])]},
    )

    assert response.status_code == 200
    assert "Charizard" in response.text
    assert "Pikachu" not in response.text


def test_regenerate_ad_text_with_no_cards_selected_leaves_fields_unchanged(client):
    ids = _seed_cards(client)
    _mark_listed(client, [ids["a"]])
    listing_id = _listing_id(client)

    response = client.post(f"/listings/{listing_id}/edit/regenerate", data={"card_id": []})

    assert response.status_code == 200
    assert "at least one card" in response.text.lower()


def test_edit_nonexistent_listing_redirects_to_listings(client):
    response = client.get("/listings/999999/edit", follow_redirects=False)

    assert response.status_code in (303, 307)
    assert response.headers["location"] == "/listings"


# --------------------------------------------------------------------------
# Mark sold (issue #127)
# --------------------------------------------------------------------------

def test_mark_sold_form_prefills_cards_and_split_price(client):
    ids = _seed_cards(client)
    _mark_listed(client, [ids["a"], ids["b"]], suggested_price="40")
    listing_id = _listing_id(client)

    response = client.get(f"/listings/{listing_id}/mark-sold")

    assert response.status_code == 200
    assert "Pikachu" in response.text
    assert "Charizard" in response.text
    # 40 / 2 cards = 20 kr each, pre-filled as an editable starting guess.
    assert 'value="20.0"' in response.text


def _mark_sold(client, listing_id, ids, prices, date="2026-02-01", platform="finn.no"):
    return client.post(
        f"/listings/{listing_id}/mark-sold",
        data={
            "date": date,
            "platform": platform,
            "card_id": [str(cid) for cid in ids],
            "price": [str(p) for p in prices],
        },
        follow_redirects=False,
    )


def test_mark_sold_creates_one_sale_transaction_per_card_with_listing_id_and_shared_purchase_id(client):
    import db as db_module
    from models import Transaction

    ids = _seed_cards(client)
    _mark_listed(client, [ids["a"], ids["b"]], suggested_price="40")
    listing_id = _listing_id(client)

    response = _mark_sold(client, listing_id, [ids["a"], ids["b"]], [15, 25])

    assert response.status_code == 303

    db = db_module.SessionLocal()
    try:
        txs = db.query(Transaction).filter(Transaction.listing_id == listing_id).all()
        assert len(txs) == 2
        assert all(t.type == "sale" for t in txs)
        assert {t.price for t in txs} == {15.0, 25.0}
        purchase_ids = {t.purchase_id for t in txs}
        assert len(purchase_ids) == 1 and next(iter(purchase_ids)) is not None
    finally:
        db.close()


def test_mark_sold_flips_status_to_sold(client):
    import db as db_module
    from models import Listing

    ids = _seed_cards(client)
    _mark_listed(client, [ids["a"]], suggested_price="30")
    listing_id = _listing_id(client)

    _mark_sold(client, listing_id, [ids["a"]], [30])

    db = db_module.SessionLocal()
    try:
        listing = db.query(Listing).filter(Listing.id == listing_id).one()
        assert listing.status == "sold"
    finally:
        db.close()


def test_mark_sold_does_not_touch_qty_collections_or_binder(client):
    import db as db_module
    from models import Card

    ids = _seed_cards(client)
    _mark_listed(client, [ids["a"]], suggested_price="30")
    listing_id = _listing_id(client)

    _mark_sold(client, listing_id, [ids["a"]], [30])

    db = db_module.SessionLocal()
    try:
        card = db.query(Card).filter(Card.id == ids["a"]).one()
        assert card.qty == 1
        assert card.binder_id is None
        assert list(card.collections) == []
    finally:
        db.close()


def test_mark_sold_rejects_missing_price_for_a_card_and_creates_no_transactions(client):
    import db as db_module
    from models import Listing, Transaction

    ids = _seed_cards(client)
    _mark_listed(client, [ids["a"], ids["b"]], suggested_price="40")
    listing_id = _listing_id(client)

    # Only price the first card -- second card in the lot gets no price row.
    response = client.post(
        f"/listings/{listing_id}/mark-sold",
        data={
            "date": "2026-02-01",
            "platform": "finn.no",
            "card_id": [str(ids["a"])],
            "price": ["20"],
        },
    )

    assert response.status_code == 200

    db = db_module.SessionLocal()
    try:
        assert db.query(Transaction).filter(Transaction.listing_id == listing_id).count() == 0
        listing = db.query(Listing).filter(Listing.id == listing_id).one()
        assert listing.status == "active"
    finally:
        db.close()


def test_mark_sold_rejects_invalid_price_and_creates_no_transactions(client):
    import db as db_module
    from models import Listing, Transaction

    ids = _seed_cards(client)
    _mark_listed(client, [ids["a"]], suggested_price="30")
    listing_id = _listing_id(client)

    response = _mark_sold(client, listing_id, [ids["a"]], ["not-a-number"])

    assert response.status_code == 200

    db = db_module.SessionLocal()
    try:
        assert db.query(Transaction).filter(Transaction.listing_id == listing_id).count() == 0
        listing = db.query(Listing).filter(Listing.id == listing_id).one()
        assert listing.status == "active"
    finally:
        db.close()


def test_rerunning_mark_sold_on_already_sold_listing_is_a_noop(client):
    import db as db_module
    from models import Transaction

    ids = _seed_cards(client)
    _mark_listed(client, [ids["a"]], suggested_price="30")
    listing_id = _listing_id(client)

    _mark_sold(client, listing_id, [ids["a"]], [30])
    _mark_sold(client, listing_id, [ids["a"]], [99])  # second attempt, different price

    db = db_module.SessionLocal()
    try:
        txs = db.query(Transaction).filter(Transaction.listing_id == listing_id).all()
        assert len(txs) == 1
        assert txs[0].price == 30.0
    finally:
        db.close()


def test_mark_sold_form_redirects_when_already_sold(client):
    ids = _seed_cards(client)
    _mark_listed(client, [ids["a"]], suggested_price="30")
    listing_id = _listing_id(client)

    _mark_sold(client, listing_id, [ids["a"]], [30])
    response = client.get(f"/listings/{listing_id}/mark-sold", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/listings"


def test_listings_page_shows_sold_price_after_marking_sold(client):
    ids = _seed_cards(client)
    _mark_listed(client, [ids["a"]], suggested_price="30")
    listing_id = _listing_id(client)

    _mark_sold(client, listing_id, [ids["a"]], [22])
    response = client.get("/listings")

    assert response.status_code == 200
    assert "22 kr" in response.text
    assert "sold" in response.text


def test_mark_sold_transactions_feed_economic_queries_with_zero_special_casing(client):
    import db as db_module
    import queries

    ids = _seed_cards(client)
    _mark_listed(client, [ids["a"]], suggested_price="30")
    listing_id = _listing_id(client)

    _mark_sold(client, listing_id, [ids["a"]], [25])

    db = db_module.SessionLocal()
    try:
        summary = queries.economic_summary(db)
        assert summary["total_sold"] == 25.0

        cash_flow = queries.cash_flow_by_month(db)
        month = next(b for b in cash_flow if b["label"] == "2026-02")
        assert month["sold"] == 25.0

        invested_by_card = queries.net_invested_by_card(db)
        assert invested_by_card[ids["a"]] == -25.0
    finally:
        db.close()


def test_sold_only_filter_shows_only_sold_listings(client):
    ids = _seed_cards(client)
    _mark_listed(client, [ids["a"]], suggested_price="30")
    listing_id_a = _listing_id(client)
    _mark_sold(client, listing_id_a, [ids["a"]], [30])

    _mark_listed(client, [ids["b"]], suggested_price="50")

    response = client.get("/listings", params={"sold_only": "true"})

    assert response.status_code == 200
    assert "Pikachu" in response.text
    assert "Charizard" not in response.text
