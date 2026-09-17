import datetime as dt

import card_images
from conftest import make_csv, seed_import
from models import Card


def test_refresh_prices_route_requires_secret_when_configured(client, monkeypatch):
    monkeypatch.setenv("CRON_SECRET", "s3cr3t")
    response = client.get("/cron/refresh-prices")
    assert response.status_code == 401


def test_refresh_prices_route_accepts_correct_secret_and_updates_stale_card(client, monkeypatch):
    monkeypatch.setenv("CRON_SECRET", "s3cr3t")
    csv = make_csv("My Collection", [{"id": "a", "name": "Shellder", "price": "kr 0,48"}])
    seed_import(client, [("files", ("main.csv", csv, "text/csv"))])

    import db as db_module

    with db_module.SessionLocal() as db:
        card = db.query(Card).filter(Card.card_id == "a").one()
        card.tcgplayer_price = 1.0
        card.tcgplayer_price_updated_at = dt.date.today() - dt.timedelta(days=8)
        db.commit()

    monkeypatch.setattr(
        card_images,
        "fetch_card_data",
        lambda name, set_name, number: card_images.CardApiData(None, 42.0),
    )

    response = client.get("/cron/refresh-prices", headers={"Authorization": "Bearer s3cr3t"})
    assert response.status_code == 200
    body = response.json()
    assert body["cards_checked"] == 1
    assert body["cards_updated"] == 1
    assert body["api_calls_used"] == 1

    with db_module.SessionLocal() as db:
        refreshed = db.query(Card).filter(Card.card_id == "a").one()
        assert refreshed.tcgplayer_price == 42.0
        assert refreshed.tcgplayer_price_updated_at == dt.date.today()


def test_refresh_prices_route_accepts_secret_as_query_param(client, monkeypatch):
    monkeypatch.setenv("CRON_SECRET", "s3cr3t")
    response = client.get("/cron/refresh-prices?secret=s3cr3t")
    assert response.status_code == 200
    body = response.json()
    assert body["cards_checked"] == 0


def test_refresh_prices_route_rejects_wrong_query_param_secret(client, monkeypatch):
    monkeypatch.setenv("CRON_SECRET", "s3cr3t")
    response = client.get("/cron/refresh-prices?secret=wrong")
    assert response.status_code == 401


def test_refresh_prices_route_works_without_secret_configured(client, monkeypatch):
    monkeypatch.delenv("CRON_SECRET", raising=False)
    response = client.get("/cron/refresh-prices")
    assert response.status_code == 200
