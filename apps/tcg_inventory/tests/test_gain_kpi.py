"""The Gain / loss KPI card -- queries.gain_summary and its render."""
import pytest

import queries
from conftest import make_csv, seed_import
from models import Card


def _card(db, card_id, price, qty=1):
    c = Card(card_id=card_id, name=card_id.upper(), variant="Normal", qty=qty, reference_price=price)
    db.add(c)
    db.flush()
    return c


def test_gain_summary_headline_and_per_card(db_session):
    up = _card(db_session, "up", 100)
    down = _card(db_session, "down", 10)
    free = _card(db_session, "free", 30)  # e.g. ripped: cost 0
    _card(db_session, "unregistered", 50)  # no cost known
    gone = _card(db_session, "gone", 999, qty=0)  # no longer owned
    invested = {up.id: 40, down.id: 25, free.id: 0, gone.id: 5}

    g = queries.gain_summary(db_session.query(Card).all(), invested, net_invested=70)

    assert g["unique_value"] == 190  # 100 + 10 + 30 + 50; qty 0 is worth nothing
    assert g["total_value"] == 190  # one copy each
    assert g["gain"] == 120
    assert g["pct"] == pytest.approx(120 / 70 * 100)
    assert (g["n_up"], g["n_down"]) == (2, 1)
    assert g["best"] == (up, 60)
    assert g["worst"] == (down, -15)


def test_gain_summary_counts_duplicates_against_what_was_paid(db_session):
    # Hero shows the duplicate-inclusive total, so gain is measured from it:
    # 3 copies at 100 = 300 total, paid 120 for all three.
    triple = _card(db_session, "triple", 100, qty=3)

    g = queries.gain_summary(db_session.query(Card).all(), {triple.id: 120}, net_invested=120)

    assert (g["unique_value"], g["total_value"]) == (100, 300)
    assert g["gain"] == 180
    assert g["pct"] == pytest.approx(150)
    assert g["best"] == (triple, 180)


def test_gain_summary_without_anything_paid_has_no_percentage(db_session):
    _card(db_session, "a", 10)
    g = queries.gain_summary(db_session.query(Card).all(), {}, net_invested=0)
    assert g["pct"] is None
    assert g["best"] is None and g["worst"] is None


@pytest.mark.parametrize("path", ["/", "/inventory", "/transactions"])
def test_gain_kpi_card_renders_on_every_kpi_page(client, path):
    main = make_csv("My Collection", [{"id": "a", "name": "Pikachu", "price": "100"}])
    seed_import(client, [("files", ("main.csv", main, "text/csv"))])
    import db as db_module

    db = db_module.SessionLocal()
    card_id = db.query(Card).one().id
    db.close()
    client.post("/transactions", data={"card_id": card_id, "type": "purchase", "date": "2026-09-20", "price": "40"})

    html = client.get(path).text

    assert "kpi-gain kpi-gain-up" in html
    assert "+60 kr" in html
    assert "+150 %" in html


def test_market_value_kpi_equation_uses_the_total(client):
    main = make_csv("My Collection", [{"id": "a", "name": "Pikachu", "qty": 3, "price": "100"}])
    seed_import(client, [("files", ("main.csv", main, "text/csv"))])
    import db as db_module

    db = db_module.SessionLocal()
    card_id = db.query(Card).one().id
    db.close()
    client.post("/transactions", data={"card_id": card_id, "type": "purchase", "date": "2026-09-20", "price": "200"})

    html = client.get("/").text

    assert "Total value <strong>300 kr</strong>" in html
    assert "Paid <strong>200 kr</strong>" in html
    assert "+100 kr" in html and "+50 %" in html
