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
        assert snap.source == "price-cron"
    assert body["usd_to_nok"] == 10.0  # conftest's fixed rate
    assert body["fx_source"] == "live"


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


def test_cron_price_refresh_re_resolves_every_card_before_its_snapshot(client, monkeypatch):
    """The cron's DB-only re-resolve pass is what applies freshness expiry
    to cards nothing re-priced today (issue #210)."""
    import datetime as dt

    import db as db_module
    import pricing

    long_ago = dt.date.today() - dt.timedelta(days=pricing.FRESH_DAYS + 5)
    with db_module.SessionLocal() as db:
        card = Card(card_id="a", variant=None, name="Shellder", qty=1)
        pricing.record_price(card, "dex", price_nok=40.0, fetched_at=long_ago)
        pricing.record_failure(card, "pokemontcg", dt.date.today())  # backed off: no lookup today
        db.add(card)
        pricing.resolve_cards(db, today=long_ago)
        db.commit()
        assert db.query(Card).one().price_flags is None  # fresh when resolved back then

    response = client.get("/cron/price-refresh")
    assert response.status_code == 200 and response.json()["cards_checked"] == 0

    with db_module.SessionLocal() as db:
        card = db.query(Card).one()
        assert (card.market_price, card.market_price_source, card.price_flags) == (40.0, "dex", "stale")
        snap = db.query(CardSnapshot).one()
        assert (snap.reference_price, snap.price_source) == (40.0, "dex")


def test_dashboard_price_movers_caption_counts_source_changes(client):
    import datetime as dt

    import db as db_module

    with db_module.SessionLocal() as db:
        card = Card(card_id="a", variant=None, name="Shellder", qty=1, market_price=50.0, market_price_source="dex")
        db.add(card)
        db.flush()
        db.add(
            CardSnapshot(
                card_id=card.id,
                date=dt.date.today() - dt.timedelta(days=3),
                source="cron",
                qty=1,
                reference_price=40.0,
                price_source="pokemontcg",
            )
        )
        db.commit()

    text = client.get("/").text

    assert "1 source change left out" in text
    assert "No price changes since" in text


def test_cron_price_refresh_reports_degraded_at_the_fx_fallback_rate(client, monkeypatch):
    """Issue #229: live Norges Bank fetch fails + empty fx_rates table ->
    neither price pass writes anything, and the run isn't "ok"."""
    import fx_rates

    _add_card(client)
    fx_rates.reset_cache()
    monkeypatch.setattr(
        card_images, "fetch_card_data", lambda name, set_name, number, variant=None: card_images.CardApiData(None, 9.99)
    )

    response = client.get("/cron/price-refresh")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "degraded"
    assert body["degraded_reason"] == fx_rates.FALLBACK_REASON
    assert body["fx_source"] == "fallback"
    assert (body["cards_checked"], body["cards_updated"], body["cards_skipped"]) == (0, 0, 1)
    assert body["tcgdex"]["status"] == "degraded"
    assert body["tcgdex"]["stopped"] == "fx_unavailable"

    import db as db_module

    with db_module.SessionLocal() as db:
        assert db.query(Card).filter(Card.card_id == "a").one().tcgplayer_price is None


def test_cron_price_refresh_status_ok_with_a_real_rate(client, monkeypatch):
    _add_card(client)
    monkeypatch.setattr(
        card_images, "fetch_card_data", lambda name, set_name, number, variant=None: card_images.CardApiData(None, 9.99)
    )
    body = client.get("/cron/price-refresh").json()
    assert (body["status"], body["degraded_reason"], body["tcgdex"]["status"]) == ("ok", None, "ok")
