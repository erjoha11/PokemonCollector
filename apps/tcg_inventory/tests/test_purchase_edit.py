from conftest import make_csv, seed_import


def _seed_two_card_order(client, purchase_id=5):
    import db as db_module
    from models import Card

    main = make_csv(
        "My Collection",
        [{"id": "a", "name": "Pikachu"}, {"id": "b", "name": "Charizard"}],
    )
    seed_import(client, [("files", ("main.csv", main, "text/csv"))])

    db = db_module.SessionLocal()
    ids = {c.card_id: c.id for c in db.query(Card).all()}
    db.close()

    for card_id, price in ((ids["a"], "10"), (ids["b"], "20")):
        client.post(
            "/transactions",
            data={"card_id": card_id, "type": "purchase", "date": "2026-01-01", "price": price, "purchase_id": str(purchase_id)},
        )
    return ids


def _tx_ids(purchase_id):
    import db as db_module
    from models import Transaction

    db = db_module.SessionLocal()
    try:
        txs = db.query(Transaction).filter(Transaction.purchase_id == purchase_id).order_by(Transaction.price).all()
        return [t.id for t in txs]
    finally:
        db.close()


def test_purchase_edit_form_renders_all_rows_of_the_order(client):
    _seed_two_card_order(client, purchase_id=5)

    response = client.get("/transactions/purchase/5/edit")
    assert response.status_code == 200
    assert "Pikachu" in response.text
    assert "Charizard" in response.text


def test_purchase_edit_agreed_total_defaults_to_shipping_plus_priced_cards(client):
    _seed_two_card_order(client, purchase_id=5)
    client.post("/transactions/purchase/5/total", data={"purchase_shipping": "5"})

    response = client.get("/transactions/purchase/5/edit")
    assert response.status_code == 200
    # No agreed total saved yet -- defaults to shipping (5) + Pikachu (10) +
    # Charizard (20) = 35, not blank/zero, so the field starts from a real
    # number rather than requiring the user to do the math themselves.
    assert 'name="purchase_total" id="purchase_total"\n             value="35' in response.text


def test_purchase_edit_agreed_total_keeps_a_saved_value_instead_of_recalculating(client):
    _seed_two_card_order(client, purchase_id=5)
    client.post("/transactions/purchase/5/total", data={"purchase_total": "999", "purchase_shipping": "5"})

    response = client.get("/transactions/purchase/5/edit")
    assert response.status_code == 200
    # A saved agreed total is a real value the user typed -- it stays
    # exactly as saved, never silently recalculated back to the sum.
    assert 'value="999.0"' in response.text


def test_purchase_edit_updates_multiple_rows_in_one_atomic_commit(client):
    import db as db_module
    from models import Transaction

    ids = _seed_two_card_order(client, purchase_id=5)
    tx_ids = _tx_ids(5)

    response = client.post(
        "/transactions/purchase/5/edit",
        data={
            "tx_id": [str(t) for t in tx_ids],
            "card_id": [str(ids["a"]), str(ids["b"])],
            "type": ["purchase", "sale"],
            "date": ["2026-02-01", "2026-02-02"],
            "price": ["11", "21"],
            "platform": ["Tise", "Cardmarket"],
            "note": ["from a friend", ""],
            "new_purchase_id": ["5", "5"],
            "purchase_total": "32",
            "purchase_shipping": "1",
        },
        follow_redirects=True,
    )
    assert response.status_code == 200

    db = db_module.SessionLocal()
    try:
        txs = sorted(db.query(Transaction).filter(Transaction.purchase_id == 5).all(), key=lambda t: t.price)
        assert [t.price for t in txs] == [11, 21]
        assert txs[1].type == "sale"
        assert txs[0].note == "from a friend"
        assert txs[0].purchase_total == 32
        assert txs[0].purchase_shipping == 1
    finally:
        db.close()


def test_purchase_edit_relinks_a_row_to_a_different_card(client):
    import db as db_module
    from models import Card, Transaction

    ids = _seed_two_card_order(client, purchase_id=5)
    tx_ids = _tx_ids(5)

    db = db_module.SessionLocal()
    try:
        eevee = Card(card_id="c", name="Eevee", qty=1)
        db.add(eevee)
        db.commit()
        eevee_id = eevee.id
    finally:
        db.close()

    response = client.post(
        "/transactions/purchase/5/edit",
        data={
            "tx_id": [str(tx_ids[0]), str(tx_ids[1])],
            "card_id": [str(eevee_id), str(ids["b"])],
            "type": ["purchase", "purchase"],
            "date": ["2026-01-01", "2026-01-01"],
            "price": ["10", "20"],
            "platform": ["", ""],
            "note": ["", ""],
            "new_purchase_id": ["5", "5"],
        },
        follow_redirects=True,
    )
    assert response.status_code == 200

    db = db_module.SessionLocal()
    try:
        tx = db.query(Transaction).filter(Transaction.id == tx_ids[0]).one()
        assert tx.card_id == eevee_id
        assert db.query(Card).filter(Card.id == eevee_id).one().name == "Eevee"
    finally:
        db.close()


def test_purchase_edit_deletes_a_row(client):
    import db as db_module
    from models import Transaction

    _seed_two_card_order(client, purchase_id=5)
    tx_ids = _tx_ids(5)

    response = client.post(
        "/transactions/purchase/5/edit",
        data={
            "tx_id": [str(tx_ids[0]), str(tx_ids[1])],
            "card_id": ["1", "2"],  # ignored for the deleted row
            "type": ["purchase", "purchase"],
            "date": ["2026-01-01", "2026-01-01"],
            "price": ["10", "20"],
            "platform": ["", ""],
            "note": ["", ""],
            "new_purchase_id": ["5", "5"],
            "delete_tx_id": [str(tx_ids[0])],
        },
        follow_redirects=True,
    )
    assert response.status_code == 200

    db = db_module.SessionLocal()
    try:
        remaining = db.query(Transaction).filter(Transaction.purchase_id == 5).all()
        assert len(remaining) == 1
        assert db.query(Transaction).filter(Transaction.id == tx_ids[0]).one_or_none() is None
    finally:
        db.close()


def test_moving_a_row_to_a_new_purchase_id_clears_its_total_and_shipping(client):
    import db as db_module
    from models import Transaction

    ids = _seed_two_card_order(client, purchase_id=5)
    tx_ids = _tx_ids(5)
    client.post("/transactions/purchase/5/total", data={"purchase_total": "30", "purchase_shipping": "2"})

    # Move the first row (Pikachu, price 10) out to a brand-new order (split).
    response = client.post(
        "/transactions/purchase/5/edit",
        data={
            "tx_id": [str(tx_ids[0]), str(tx_ids[1])],
            "card_id": [str(ids["a"]), str(ids["b"])],
            "type": ["purchase", "purchase"],
            "date": ["2026-01-01", "2026-01-01"],
            "price": ["10", "20"],
            "platform": ["", ""],
            "note": ["", ""],
            "new_purchase_id": ["99", "5"],
            "purchase_total": "20",
            "purchase_shipping": "2",
        },
        follow_redirects=True,
    )
    assert response.status_code == 200

    db = db_module.SessionLocal()
    try:
        moved = db.query(Transaction).filter(Transaction.id == tx_ids[0]).one()
        assert moved.purchase_id == 99
        # Never guessed/carried over -- the user must set the destination
        # order's total/shipping explicitly afterward.
        assert moved.purchase_total is None
        assert moved.purchase_shipping is None

        stayed = db.query(Transaction).filter(Transaction.id == tx_ids[1]).one()
        assert stayed.purchase_id == 5
        assert stayed.purchase_total == 20
        assert stayed.purchase_shipping == 2
    finally:
        db.close()


def test_purchase_edit_add_card_search_returns_matches(client):
    _seed_two_card_order(client, purchase_id=5)

    response = client.get("/transactions/purchase/5/edit/add-card-search", params={"q": "Char"})
    assert response.status_code == 200
    assert "Charizard" in response.text


def test_purchase_edit_add_card_creates_a_row_against_this_order(client):
    import datetime as dt

    import db as db_module
    from models import Card, Transaction

    _seed_two_card_order(client, purchase_id=5)

    db = db_module.SessionLocal()
    try:
        eevee = Card(card_id="c", name="Eevee", qty=1)
        db.add(eevee)
        db.commit()
        eevee_id = eevee.id
    finally:
        db.close()

    response = client.post(f"/transactions/purchase/5/edit/add-card?card_id={eevee_id}")
    assert response.status_code == 200
    assert "Eevee" in response.text

    db = db_module.SessionLocal()
    try:
        tx = db.query(Transaction).filter(Transaction.purchase_id == 5, Transaction.card_id == eevee_id).one()
        assert tx.type == "purchase"
        assert tx.price == 0
        assert tx.date == dt.date.today()
    finally:
        db.close()


def test_purchase_edit_added_card_is_included_in_the_next_save(client):
    import db as db_module
    from models import Card, Transaction

    ids = _seed_two_card_order(client, purchase_id=5)
    tx_ids = _tx_ids(5)

    db = db_module.SessionLocal()
    try:
        eevee = Card(card_id="c", name="Eevee", qty=1)
        db.add(eevee)
        db.commit()
        eevee_id = eevee.id
    finally:
        db.close()

    client.post(f"/transactions/purchase/5/edit/add-card?card_id={eevee_id}")
    new_tx_id = _tx_ids(5)
    added_tx_id = [t for t in new_tx_id if t not in tx_ids][0]

    response = client.post(
        "/transactions/purchase/5/edit",
        data={
            "tx_id": [str(tx_ids[0]), str(tx_ids[1]), str(added_tx_id)],
            "card_id": [str(ids["a"]), str(ids["b"]), str(eevee_id)],
            "type": ["purchase", "purchase", "purchase"],
            "date": ["2026-01-01", "2026-01-01", "2026-03-01"],
            "price": ["10", "20", "5"],
            "platform": ["", "", ""],
            "note": ["", "", ""],
            "new_purchase_id": ["5", "5", "5"],
        },
        follow_redirects=True,
    )
    assert response.status_code == 200

    db = db_module.SessionLocal()
    try:
        tx = db.query(Transaction).filter(Transaction.id == added_tx_id).one()
        assert tx.price == 5
        assert tx.date.isoformat() == "2026-03-01"
    finally:
        db.close()


def test_moving_every_row_out_leaves_no_dangling_empty_order(client):
    ids = _seed_two_card_order(client, purchase_id=5)
    tx_ids = _tx_ids(5)

    response = client.post(
        "/transactions/purchase/5/edit",
        data={
            "tx_id": [str(tx_ids[0]), str(tx_ids[1])],
            "card_id": [str(ids["a"]), str(ids["b"])],
            "type": ["purchase", "purchase"],
            "date": ["2026-01-01", "2026-01-01"],
            "price": ["10", "20"],
            "platform": ["", ""],
            "note": ["", ""],
            "new_purchase_id": ["99", "99"],
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/transactions"
