import card_images
from models import Card, CardSnapshot


def _add_card(client, name="Shellder"):
    import db as db_module

    with db_module.SessionLocal() as db:
        db.add(Card(card_id="a", variant=None, name=name))
        db.commit()


def test_cron_price_refresh_requires_secret_when_configured(client, monkeypatch):
    monkeypatch.setenv("CRON_SECRET", "s3cr3t")
    response = client.get("/cron/price-refresh")
    assert response.status_code == 401


def test_cron_price_refresh_accepts_correct_secret_and_refreshes_prices(client, monkeypatch):
    monkeypatch.setenv("CRON_SECRET", "s3cr3t")
    _add_card(client)
    monkeypatch.setattr(
        card_images, "fetch_card_data", lambda name, set_name, number, variant=None: card_images.CardApiData(None, 9.99)
    )

    response = client.get("/cron/price-refresh", headers={"Authorization": "Bearer s3cr3t"})

    assert response.status_code == 200
    body = response.json()
    assert body["cards_checked"] == 1
    assert body["cards_updated"] == 1
    assert body["cards_snapshotted"] == 1

    import db as db_module

    with db_module.SessionLocal() as db:
        card = db.query(Card).filter(Card.card_id == "a").one()
        assert card.tcgplayer_price == 9.99
        snap = db.query(CardSnapshot).one()
        assert snap.source == "cron"  # the day's one scheduled slot (issue #209)
    assert body["usd_to_nok"] == 10.0  # conftest's fixed rate
    assert body["fx_source"] == "live"


def test_scheduled_price_refresh_overwrites_the_syncs_cron_snapshot(client, monkeypatch):
    """Daily order is Dex sync (05:00, writes the "cron" slot) -> price
    refresh (06:00) -> snapshot: the refresh overwrites that same slot with
    post-refresh prices instead of adding a second scheduled point."""
    import datetime as dt

    import db as db_module
    import snapshots

    monkeypatch.setenv("CRON_SECRET", "s3cr3t")
    _add_card(client)
    with db_module.SessionLocal() as db:
        db.query(Card).one().tcgplayer_price = 1.0
        db.commit()
        snapshots.record_daily_snapshot(db, source="cron")  # what the 05:00 sync writes

    monkeypatch.setattr(
        card_images, "fetch_card_data", lambda name, set_name, number, variant=None: card_images.CardApiData(None, 9.99)
    )
    response = client.get("/cron/price-refresh", headers={"Authorization": "Bearer s3cr3t"})
    assert response.status_code == 200

    with db_module.SessionLocal() as db:
        snaps = db.query(CardSnapshot).all()
        assert [(s.date, s.source, s.reference_price) for s in snaps] == [(dt.date.today(), "cron", 9.99)]


def test_cron_price_refresh_accepts_secret_as_query_param(client, monkeypatch):
    monkeypatch.setenv("CRON_SECRET", "s3cr3t")
    _add_card(client)
    monkeypatch.setattr(
        card_images, "fetch_card_data", lambda name, set_name, number, variant=None: card_images.CardApiData(None, 9.99)
    )

    response = client.get("/cron/price-refresh?secret=s3cr3t")

    assert response.status_code == 200

    import db as db_module

    with db_module.SessionLocal() as db:
        snap = db.query(CardSnapshot).one()
        assert snap.source == "manual"  # off-schedule trigger, not the real cron header


def test_cron_price_refresh_reports_low_confidence_matches(client, monkeypatch):
    monkeypatch.delenv("CRON_SECRET", raising=False)
    _add_card(client)
    monkeypatch.setattr(
        card_images,
        "fetch_card_data",
        lambda name, set_name, number, variant=None: card_images.CardApiData(
            image_url=None, tcgplayer_price=None, low_confidence_match=True
        ),
    )

    response = client.get("/cron/price-refresh")

    assert response.status_code == 200
    body = response.json()
    assert body["cards_updated"] == 0
    assert body["cards_low_confidence"] == ["Shellder (? ?)"]
