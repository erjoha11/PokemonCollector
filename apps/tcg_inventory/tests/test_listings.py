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
