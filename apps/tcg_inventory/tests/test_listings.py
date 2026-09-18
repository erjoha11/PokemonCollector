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
