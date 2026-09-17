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


def test_sales_review_page_shows_selected_cards(client):
    ids = _seed_cards(client)

    response = client.get("/sales", params={"card_ids": [ids["a"], ids["b"]]})

    assert response.status_code == 200
    assert "Pikachu" in response.text
    assert "Charizard" in response.text


def test_sales_review_page_with_no_selection_shows_empty_state(client):
    response = client.get("/sales")

    assert response.status_code == 200
    assert "No cards selected" in response.text


def test_generate_ad_builds_title_and_description(client):
    ids = _seed_cards(client)

    response = client.post(
        "/sales/generate",
        data={
            "card_id": [str(ids["a"])],
            "qty": ["1"],
            "condition": ["Near Mint"],
            "price": ["10"],
        },
    )

    assert response.status_code == 200
    assert "Pikachu" in response.text
    assert "Near Mint" in response.text


def test_generate_ad_persists_condition_onto_the_card(client):
    import db as db_module
    from models import Card

    ids = _seed_cards(client)

    client.post(
        "/sales/generate",
        data={
            "card_id": [str(ids["a"])],
            "qty": ["1"],
            "condition": ["Lightly Played"],
            "price": ["10"],
        },
    )

    db = db_module.SessionLocal()
    try:
        card = db.query(Card).filter(Card.id == ids["a"]).one()
        assert card.condition == "Lightly Played"
    finally:
        db.close()


def test_mark_listed_creates_listing_without_touching_qty(client):
    import db as db_module
    from models import Card, Listing

    ids = _seed_cards(client)

    response = client.post(
        "/sales/mark-listed",
        data={
            "card_id": [str(ids["a"])],
            "title": "Pokemon kort - Pikachu",
            "description": "Selger Pikachu.",
            "suggested_price": "10",
        },
    )

    assert response.status_code == 200
    assert "Marked as listed" in response.text

    db = db_module.SessionLocal()
    try:
        listing = db.query(Listing).one()
        assert listing.title == "Pokemon kort - Pikachu"
        assert listing.platform == "finn.no"
        assert [c.id for c in listing.cards] == [ids["a"]]

        card = db.query(Card).filter(Card.id == ids["a"]).one()
        assert card.qty == 1  # unchanged -- listing != sold
    finally:
        db.close()
