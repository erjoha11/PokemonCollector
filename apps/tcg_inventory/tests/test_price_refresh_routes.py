import masterdata
import pokemontcg_client
from models import Card, CardSnapshot
from test_price_refresh import FakeClient, api


def _add_card(client, name="Shellder"):
    """An international card (so the pokemontcg pass asks for it, issue
    #349) with id "a" kept for the assertions below: its pokemontcg ID is
    stored by hand."""
    import db as db_module

    with db_module.SessionLocal() as db:
        card = Card(card_id="xy1-1", variant=None, name=name, number="1/146", qty=1)
        db.add(card)
        masterdata.link_card(db, card)
        card.card_id = "a"
        db.commit()


def _pokemontcg(monkeypatch, *cards, **kw):
    fake = FakeClient(cards, **kw)
    monkeypatch.setattr(pokemontcg_client, "Client", lambda: fake)
    return fake


def _shellder(usd=0.999):
    return api("xy1-1", "Shellder", "1", {"normal": usd})


def test_cron_price_refresh_requires_secret_when_configured(client, monkeypatch):
    monkeypatch.setenv("CRON_SECRET", "s3cr3t")
    response = client.get("/cron/price-refresh")
    assert response.status_code == 401


def test_cron_price_refresh_accepts_correct_secret_and_refreshes_prices(client, monkeypatch):
    monkeypatch.setenv("CRON_SECRET", "s3cr3t")
    _add_card(client)
    _pokemontcg(monkeypatch, _shellder())

    response = client.get("/cron/price-refresh", headers={"Authorization": "Bearer s3cr3t"})

    assert response.status_code == 200
    body = response.json()
    assert body["cards_checked"] == 1
    assert body["cards_updated"] == 1
    assert body["cards_snapshotted"] == 1
    assert (body["requests"], body["batch_requests"], body["transient_errors"], body["stopped"]) == (1, 1, 0, None)

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
    _pokemontcg(monkeypatch, _shellder())

    response = client.get("/cron/price-refresh?secret=s3cr3t")

    assert response.status_code == 200

    import db as db_module

    with db_module.SessionLocal() as db:
        snap = db.query(CardSnapshot).one()
        assert snap.source == "manual"  # off-schedule trigger, not the real cron header


def test_cron_price_refresh_reports_low_confidence_matches(client, monkeypatch):
    monkeypatch.delenv("CRON_SECRET", raising=False)
    _add_card(client)
    _pokemontcg(monkeypatch, searches={"Shellder": api("xy99-1", "Shellder ex", "1")})  # ID missing

    response = client.get("/cron/price-refresh")

    assert response.status_code == 200
    body = response.json()
    assert body["cards_updated"] == 0
    assert body["cards_low_confidence"] == ["Shellder (a)"]
    assert len(body["cards_unmatched"]) == 1
    assert (body["requests"], body["batch_requests"], body["fallback_searches"]) == (2, 1, 1)


def test_cron_price_refresh_survives_a_pokemontcg_outage(client, monkeypatch):
    """Every pokemontcg.io request fails: nothing written or stamped, the
    pass stops at the error limit, and the cron still resolves and
    snapshots (issue #349)."""
    import db as db_module
    import pricing

    _add_card(client)
    _pokemontcg(monkeypatch, fail=pokemontcg_client.TransientError("HTTP 502"))

    response = client.get("/cron/price-refresh")

    assert response.status_code == 200
    body = response.json()
    assert (body["status"], body["transient_errors"], body["cards_deferred"]) == ("ok", 1, 1)
    assert body["cards_snapshotted"] == 1
    with db_module.SessionLocal() as db:
        assert pricing.get_row(db.query(Card).one(), "pokemontcg") is None


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
    _pokemontcg(monkeypatch, _shellder())

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

    from models import ImportLog

    with db_module.SessionLocal() as db:
        assert db.query(Card).filter(Card.card_id == "a").one().tcgplayer_price is None
        log = db.query(ImportLog).filter(ImportLog.job == "price-refresh").one()
        assert log.status == "degraded"
        assert fx_rates.FALLBACK_REASON in log.message


def test_cron_price_refresh_status_ok_with_a_real_rate(client, monkeypatch):
    _add_card(client)
    _pokemontcg(monkeypatch, _shellder())
    body = client.get("/cron/price-refresh").json()
    assert (body["status"], body["degraded_reason"], body["tcgdex"]["status"]) == ("ok", None, "ok")
