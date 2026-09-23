"""Ripped cards (pulled from a pack yourself): type "ripped", always 0, and
never counted toward any money figure -- see NON_CASH_TYPES in app.py."""
from conftest import make_csv, seed_import

import queries
from models import Card, Transaction


def _seed_cards(client):
    import db as db_module

    main = make_csv(
        "My Collection",
        [{"id": "a", "name": "Pikachu", "price": "40"}, {"id": "b", "name": "Charizard", "price": "300"}],
    )
    seed_import(client, [("files", ("main.csv", main, "text/csv"))])
    db = db_module.SessionLocal()
    try:
        return {c.card_id: c.id for c in db.query(Card).all()}
    finally:
        db.close()


def _rows():
    import db as db_module

    db = db_module.SessionLocal()
    try:
        return [(t.card_id, t.type, t.price) for t in db.query(Transaction).order_by(Transaction.id).all()]
    finally:
        db.close()


def test_ripped_order_is_stored_as_free(client):
    ids = _seed_cards(client)

    client.post(
        "/transactions/purchase",
        data={
            "type": "ripped",
            "date": "2026-09-20",
            "purchase_id": "20",
            "card_id": [str(ids["a"]), str(ids["b"])],
            "price": ["12", "0"],  # whatever the form sent, ripped is 0
        },
    )

    assert _rows() == [(ids["a"], "ripped", 0.0), (ids["b"], "ripped", 0.0)]


def test_ripped_cards_stay_out_of_money_figures(client):
    import db as db_module

    ids = _seed_cards(client)
    client.post("/transactions", data={"card_id": ids["a"], "type": "purchase", "date": "2026-09-20", "price": "25"})
    client.post("/transactions", data={"card_id": ids["b"], "type": "ripped", "date": "2026-09-20", "price": "0"})

    db = db_module.SessionLocal()
    try:
        assert queries.economic_summary(db)["net_invested"] == 25
        invested = queries.net_invested_by_card(db)
        assert invested[ids["b"]] == 0
    finally:
        db.close()


def test_single_row_edit_to_ripped_zeroes_the_price(client):
    ids = _seed_cards(client)
    client.post("/transactions", data={"card_id": ids["a"], "type": "purchase", "date": "2026-09-20", "price": "25"})
    import db as db_module

    db = db_module.SessionLocal()
    tx_id = db.query(Transaction).one().id
    db.close()

    client.post(f"/transactions/{tx_id}", data={"date": "2026-09-20", "type": "ripped", "price": "25"})

    assert _rows() == [(ids["a"], "ripped", 0.0)]


def test_ripped_cards_are_not_listed_as_missing_an_order(client):
    ids = _seed_cards(client)
    client.post("/transactions", data={"card_id": ids["b"], "type": "ripped", "date": "2026-09-20", "price": "0"})

    html = client.get("/transactions/purchase/browse-unordered").text

    assert "Pikachu" in html
    assert "Charizard" not in html


def test_inventory_flags_ripped_cards(client):
    ids = _seed_cards(client)
    client.post("/transactions", data={"card_id": ids["b"], "type": "ripped", "date": "2026-09-20", "price": "0"})

    html = client.get("/inventory").text

    assert html.count("ripped-flag") == 1
    assert "Ripped" in html


def test_order_value_ignores_ripped_rows(client):
    ids = _seed_cards(client)
    client.post("/transactions", data={"card_id": ids["a"], "type": "purchase", "date": "2026-09-20", "price": "25", "purchase_id": "7"})
    client.post("/transactions", data={"card_id": ids["b"], "type": "ripped", "date": "2026-09-20", "price": "0", "purchase_id": "7"})

    html = client.get("/transactions?open_order=7").text

    assert "tx-badge-ripped" in html


def test_transactions_picker_leaves_out_ripped_and_traded_cards(client):
    main = make_csv(
        "My Collection",
        [
            {"id": "a", "name": "Pikachu"},
            {"id": "b", "name": "Charizard"},
            {"id": "c", "name": "Mewtwo"},
            {"id": "d", "name": "Eevee"},
        ],
    )
    seed_import(client, [("files", ("main.csv", main, "text/csv"))])
    import db as db_module

    db = db_module.SessionLocal()
    ids = {c.card_id: c.id for c in db.query(Card).all()}
    db.close()
    client.post("/transactions", data={"card_id": ids["b"], "type": "ripped", "date": "2026-09-20", "price": "0", "purchase_id": "22"})
    client.post("/transactions", data={"card_id": ids["c"], "type": "trade", "date": "2026-09-20", "price": "0", "purchase_id": "20"})
    client.post("/transactions", data={"card_id": ids["d"], "type": "purchase", "date": "2026-09-20", "price": "5", "purchase_id": "2"})

    picker = client.get("/transactions?pick=unordered").text

    # Only Pikachu has no purchase, ripped or trade row.
    assert "Without an order (1)" in picker

    browse = client.get("/transactions/purchase/browse-unordered").text
    assert "Pikachu" in browse
    assert "Charizard" not in browse and "Mewtwo" not in browse and "Eevee" not in browse
