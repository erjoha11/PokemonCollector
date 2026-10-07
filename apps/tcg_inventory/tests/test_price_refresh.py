import datetime as dt

import card_images
import price_refresh
import pricing
from models import Card


def _priced(card, price, fetched_at):
    """Give `card` a pokemontcg price (what used to be tcgplayer_price +
    tcgplayer_price_updated_at on the card itself, before issue #210)."""
    pricing.record_price(card, pricing.SOURCE_POKEMONTCG, price_nok=price, fetched_at=fetched_at)
    card.tcgplayer_price, card.tcgplayer_price_updated_at = price, fetched_at
    return card


def _failed(card, on):
    pricing.record_failure(card, pricing.SOURCE_POKEMONTCG, on)
    return card


def _row(card):
    return pricing.get_row(card, pricing.SOURCE_POKEMONTCG)


def test_refresh_stale_prices_updates_never_priced_cards(db_session, monkeypatch):
    card = Card(card_id="a", variant=None, name="Shellder")
    db_session.add(card)
    db_session.commit()

    monkeypatch.setattr(
        card_images, "fetch_card_data", lambda name, set_name, number, variant=None: card_images.CardApiData(None, 9.99)
    )

    result = price_refresh.refresh_stale_prices(db_session)

    refreshed = db_session.query(Card).filter(Card.card_id == "a").one()
    assert refreshed.tcgplayer_price == 9.99
    assert refreshed.tcgplayer_price_updated_at == dt.date.today()
    assert result.cards_checked == 1
    assert result.cards_updated == 1
    assert result.cards_low_confidence == []


def test_refresh_stale_prices_skips_a_fresh_price(db_session, monkeypatch):
    card = _priced(Card(card_id="a", variant=None, name="Shellder"), 1.0, dt.date.today())
    db_session.add(card)
    db_session.commit()

    calls = []
    monkeypatch.setattr(
        card_images,
        "fetch_card_data",
        lambda name, set_name, number, variant=None: calls.append(1) or card_images.CardApiData(None, 42.0),
    )

    result = price_refresh.refresh_stale_prices(db_session)

    assert calls == []
    assert result.cards_checked == 0
    refreshed = db_session.query(Card).filter(Card.card_id == "a").one()
    assert refreshed.tcgplayer_price == 1.0


def test_refresh_stale_prices_refetches_a_stale_price(db_session, monkeypatch):
    card = _priced(
        Card(card_id="a", variant=None, name="Shellder"),
        1.0,
        dt.date.today() - dt.timedelta(days=price_refresh.PRICE_STALE_AFTER_DAYS + 1),
    )
    db_session.add(card)
    db_session.commit()

    monkeypatch.setattr(
        card_images, "fetch_card_data", lambda name, set_name, number, variant=None: card_images.CardApiData(None, 42.0)
    )

    result = price_refresh.refresh_stale_prices(db_session)

    refreshed = db_session.query(Card).filter(Card.card_id == "a").one()
    assert refreshed.tcgplayer_price == 42.0
    assert refreshed.tcgplayer_price_updated_at == dt.date.today()
    assert result.cards_updated == 1


def test_refresh_stale_prices_flags_low_confidence_matches_without_pricing(db_session, monkeypatch):
    card = Card(card_id="a", variant=None, name="Shellder")
    db_session.add(card)
    db_session.commit()

    monkeypatch.setattr(
        card_images,
        "fetch_card_data",
        lambda name, set_name, number, variant=None: card_images.CardApiData(
            image_url=None, tcgplayer_price=None, low_confidence_match=True
        ),
    )

    result = price_refresh.refresh_stale_prices(db_session)

    refreshed = db_session.query(Card).filter(Card.card_id == "a").one()
    assert refreshed.tcgplayer_price is None
    assert result.cards_updated == 0
    assert result.cards_low_confidence == ["Shellder (? ?)"]


def test_refresh_stale_prices_flags_variant_uncertain_matches_while_still_pricing(db_session, monkeypatch):
    card = Card(card_id="a", variant="Holo", name="Shellder")
    db_session.add(card)
    db_session.commit()

    monkeypatch.setattr(
        card_images,
        "fetch_card_data",
        lambda name, set_name, number, variant=None: card_images.CardApiData(
            image_url=None, tcgplayer_price=42.0, variant_price_uncertain=True
        ),
    )

    result = price_refresh.refresh_stale_prices(db_session)

    refreshed = db_session.query(Card).filter(Card.card_id == "a").one()
    assert refreshed.tcgplayer_price == 42.0
    assert result.cards_updated == 1
    assert result.cards_variant_uncertain == ["Shellder (? ?)"]


def test_refresh_stale_prices_respects_the_budget_and_prioritizes_oldest_first(db_session, monkeypatch):
    never_priced = Card(card_id="a", variant=None, name="Never Priced")
    stale = _priced(
        Card(card_id="b", variant=None, name="Stale"),
        1.0,
        dt.date.today() - dt.timedelta(days=price_refresh.PRICE_STALE_AFTER_DAYS + 1),
    )
    fresh = _priced(Card(card_id="c", variant=None, name="Fresh"), 2.0, dt.date.today())
    db_session.add_all([never_priced, stale, fresh])
    db_session.commit()

    checked_names = []
    monkeypatch.setattr(
        card_images,
        "fetch_card_data",
        lambda name, set_name, number, variant=None: checked_names.append(name) or card_images.CardApiData(None, 5.0),
    )

    result = price_refresh.refresh_stale_prices(db_session, budget=1)

    assert result.cards_checked == 1
    # Already-priced stale cards go first (issue #216); never-priced ones
    # wait for leftover budget, and the fresh one isn't touched.
    assert checked_names == ["Stale"]

    checked_names.clear()
    price_refresh.refresh_stale_prices(db_session, budget=5)
    assert checked_names == ["Never Priced"]


def _fail_lookup(recorder=None):
    def fake(name, set_name, number, variant=None):
        if recorder is not None:
            recorder.append(name)
        return card_images.CardApiData(None, None)

    return fake


def test_refresh_stale_prices_stamps_a_failed_lookup(db_session, monkeypatch):
    db_session.add(Card(card_id="a", variant=None, name="Japanese Print"))
    db_session.commit()
    monkeypatch.setattr(card_images, "fetch_card_data", _fail_lookup())

    today = dt.date(2026, 9, 30)
    result = price_refresh.refresh_stale_prices(db_session, today=today)

    card = db_session.query(Card).one()
    assert result.cards_checked == 1 and result.cards_updated == 0
    assert _row(card).lookup_failed_at == today
    assert _row(card).price_nok is None
    assert card.tcgplayer_price is None and card.tcgplayer_price_updated_at is None
    assert card.price_lookup_failed_at is None  # deprecated column, no longer written


def test_refresh_stale_prices_stamps_a_low_confidence_match_as_failed(db_session, monkeypatch):
    db_session.add(Card(card_id="a", variant=None, name="Shellder"))
    db_session.commit()
    monkeypatch.setattr(
        card_images,
        "fetch_card_data",
        lambda name, set_name, number, variant=None: card_images.CardApiData(None, None, low_confidence_match=True),
    )

    today = dt.date(2026, 9, 30)
    price_refresh.refresh_stale_prices(db_session, today=today)

    assert _row(db_session.query(Card).one()).lookup_failed_at == today


def test_refresh_stale_prices_respects_the_retry_window(db_session, monkeypatch):
    failed_on = dt.date(2026, 9, 1)
    card = _failed(Card(card_id="a", variant=None, name="Japanese Print"), failed_on)
    db_session.add(card)
    db_session.commit()
    looked_up = []
    monkeypatch.setattr(card_images, "fetch_card_data", _fail_lookup(looked_up))

    inside = failed_on + dt.timedelta(days=price_refresh.PRICE_RETRY_AFTER_DAYS)
    assert price_refresh.refresh_stale_prices(db_session, today=inside).cards_checked == 0
    assert looked_up == []

    past = inside + dt.timedelta(days=1)
    assert price_refresh.refresh_stale_prices(db_session, today=past).cards_checked == 1
    assert looked_up == ["Japanese Print"]
    assert _row(db_session.query(Card).one()).lookup_failed_at == past  # re-stamped


def test_stale_priced_cards_win_the_budget_over_never_priceable_ones(db_session, monkeypatch):
    # The prod shape from issue #216: many cards that never price, a few
    # stale priced ones, a budget smaller than the never-priceable pile.
    today = dt.date(2026, 9, 30)
    for i in range(10):
        db_session.add(Card(card_id=f"ja{i}", variant=None, name=f"Never {i}"))
    for i in range(3):
        card = _priced(
            Card(card_id=f"en{i}", variant=None, name=f"Stale {i}"),
            1.0,
            today - dt.timedelta(days=price_refresh.PRICE_STALE_AFTER_DAYS + 1 + i),
        )
        db_session.add(card)
    db_session.commit()

    def fake(name, set_name, number, variant=None):
        return card_images.CardApiData(None, 2.0 if name.startswith("Stale") else None)

    monkeypatch.setattr(card_images, "fetch_card_data", fake)

    result = price_refresh.refresh_stale_prices(db_session, today=today, budget=5)

    assert result.cards_checked == 5
    assert result.cards_updated == 3
    stale = db_session.query(Card).filter(Card.card_id.like("en%")).all()
    assert all(_row(c).fetched_at == today and c.tcgplayer_price_updated_at == today for c in stale)

    # Next day: the 2 that failed are backed off, so the 5 never-tried ones
    # get the budget instead of the same failures again.
    looked_up = []
    monkeypatch.setattr(card_images, "fetch_card_data", _fail_lookup(looked_up))
    price_refresh.refresh_stale_prices(db_session, today=today + dt.timedelta(days=1), budget=5)
    assert len(looked_up) == 5
    stamped_yesterday = {c.name for c in db_session.query(Card) if _row(c) and _row(c).lookup_failed_at == today}
    assert stamped_yesterday.isdisjoint(looked_up)


def test_failed_cards_past_their_window_come_after_never_tried_ones(db_session, monkeypatch):
    today = dt.date(2026, 9, 30)
    retried = _failed(
        Card(card_id="a", variant=None, name="Retry"),
        today - dt.timedelta(days=price_refresh.PRICE_RETRY_AFTER_DAYS + 1),
    )
    db_session.add_all([retried, Card(card_id="b", variant=None, name="Never tried")])
    db_session.commit()
    looked_up = []
    monkeypatch.setattr(card_images, "fetch_card_data", _fail_lookup(looked_up))

    price_refresh.refresh_stale_prices(db_session, today=today, budget=1)

    assert looked_up == ["Never tried"]


def test_a_successful_lookup_clears_the_failure_stamp(db_session, monkeypatch):
    today = dt.date(2026, 9, 30)
    card = _failed(
        Card(card_id="a", variant=None, name="Shellder"),
        today - dt.timedelta(days=price_refresh.PRICE_RETRY_AFTER_DAYS + 1),
    )
    db_session.add(card)
    db_session.commit()
    monkeypatch.setattr(
        card_images, "fetch_card_data", lambda name, set_name, number, variant=None: card_images.CardApiData(None, 7.0)
    )

    price_refresh.refresh_stale_prices(db_session, today=today)

    card = db_session.query(Card).one()
    assert card.tcgplayer_price == 7.0
    assert _row(card).lookup_failed_at is None
    assert card.market_price == 7.0 and card.market_price_source == "pokemontcg"


def test_reprice_all_refetches_every_priced_card_regardless_of_staleness(db_session, monkeypatch):
    fresh = _priced(Card(card_id="a", variant=None, name="Fresh"), 110.0, dt.date.today())
    never = Card(card_id="b", variant=None, name="Never priced")
    db_session.add_all([fresh, never])
    db_session.commit()

    looked_up = []
    monkeypatch.setattr(
        card_images,
        "fetch_card_data",
        lambda name, set_name, number, variant=None: looked_up.append(name) or card_images.CardApiData(None, 100.0),
    )

    result = price_refresh.reprice_all(db_session)

    assert looked_up == ["Fresh"]  # only cards that already have a price
    assert result.cards_updated == 1
    assert result.usd_to_nok == 10.0 and result.fx_source == "live"
    assert db_session.query(Card).filter(Card.card_id == "a").one().tcgplayer_price == 100.0


def test_reprice_all_keeps_the_old_price_and_date_when_the_lookup_fails(db_session, monkeypatch):
    old_date = dt.date(2026, 9, 1)
    card = _priced(Card(card_id="a", variant=None, name="Shellder"), 110.0, old_date)
    db_session.add(card)
    db_session.commit()

    monkeypatch.setattr(
        card_images, "fetch_card_data", lambda name, set_name, number, variant=None: card_images.CardApiData(None, None)
    )

    result = price_refresh.reprice_all(db_session)

    refreshed = db_session.query(Card).one()
    assert result.cards_updated == 0
    assert refreshed.tcgplayer_price == 110.0  # never rescaled, only replaced by a real lookup
    assert refreshed.tcgplayer_price_updated_at == old_date  # still due for the cron
    assert _row(refreshed).fetched_at == old_date and _row(refreshed).price_nok == 110.0
    assert _row(refreshed).lookup_failed_at is None  # not backed off either


def test_reprice_all_respects_limit_oldest_first(db_session, monkeypatch):
    for i, day in enumerate([5, 1, 3]):
        card = _priced(Card(card_id=f"c{i}", variant=None, name=f"Card {day}"), 1.0, dt.date(2026, 9, day))
        db_session.add(card)
    db_session.commit()

    looked_up = []
    monkeypatch.setattr(
        card_images,
        "fetch_card_data",
        lambda name, set_name, number, variant=None: looked_up.append(name) or card_images.CardApiData(None, 2.0),
    )

    price_refresh.reprice_all(db_session, limit=2)

    assert looked_up == ["Card 1", "Card 3"]


# --------------------------------------------------------------------------
# FX fallback guard (issue #229)
# --------------------------------------------------------------------------
import fx_rates
from models import FxRate


def _no_real_rate():
    """Live Norges Bank fetch fails (conftest turns the network off) and the
    fx_rates table is empty -> get_rates() can only return the constant."""
    fx_rates.reset_cache()


def _store_old_rate(db, usd=9.4, eur=10.9):
    """A rate stored days ago (not "recent", so get_rates still tries the
    live fetch, which fails, and lands on "stored")."""
    old = dt.datetime.utcnow() - dt.timedelta(days=3)
    for currency, rate in (("USD", usd), ("EUR", eur)):
        db.add(FxRate(date=dt.date.today() - dt.timedelta(days=3), currency=currency, rate_nok=rate, fetched_at=old))
    db.commit()
    fx_rates.reset_cache()


def test_refresh_writes_nothing_at_the_fx_fallback_rate_and_reports_degraded(db_session, monkeypatch):
    stale = dt.date.today() - dt.timedelta(days=price_refresh.PRICE_STALE_AFTER_DAYS + 1)
    priced = _priced(Card(card_id="a", variant=None, name="Shellder"), 1.0, stale)
    never = Card(card_id="b", variant=None, name="Slowbro")
    db_session.add_all([priced, never])
    db_session.commit()
    _no_real_rate()
    calls = []
    monkeypatch.setattr(
        card_images,
        "fetch_card_data",
        lambda *a, **k: calls.append(1) or card_images.CardApiData(None, 42.0),
    )

    result = price_refresh.refresh_stale_prices(db_session)

    assert result.status == "degraded"
    assert result.fx_source == "fallback"
    assert result.degraded_reason == fx_rates.FALLBACK_REASON
    assert (result.cards_checked, result.cards_updated, result.cards_skipped) == (0, 0, 2)
    assert calls == []
    assert (_row(priced).price_nok, _row(priced).fetched_at, _row(priced).lookup_failed_at) == (1.0, stale, None)
    assert _row(never) is None
    # Both still due, so the next run retries.
    due = db_session.query(Card).filter(price_refresh.due_for_price_lookup_filter(dt.date.today())).count()
    assert due == 2


def test_reprice_all_also_skips_at_the_fx_fallback_rate(db_session, monkeypatch):
    card = _priced(Card(card_id="a", variant=None, name="Shellder"), 1.0, dt.date.today())
    db_session.add(card)
    db_session.commit()
    _no_real_rate()
    monkeypatch.setattr(card_images, "fetch_card_data", lambda *a, **k: card_images.CardApiData(None, 42.0))

    result = price_refresh.reprice_all(db_session)

    assert result.status == "degraded"
    assert _row(card).price_nok == 1.0


def test_refresh_with_a_stored_rate_is_unchanged(db_session, monkeypatch):
    card = Card(card_id="a", variant=None, name="Shellder")
    db_session.add(card)
    db_session.commit()
    _store_old_rate(db_session)
    monkeypatch.setattr(card_images, "fetch_card_data", lambda *a, **k: card_images.CardApiData(None, 42.0))

    result = price_refresh.refresh_stale_prices(db_session)

    assert (result.status, result.degraded_reason, result.fx_source, result.usd_to_nok) == ("ok", None, "stored", 9.4)
    assert (result.cards_checked, result.cards_updated, result.cards_skipped) == (1, 1, 0)
    assert _row(card).price_nok == 42.0


def test_a_stored_set_missing_usd_counts_as_fallback_for_usd(db_session, monkeypatch):
    """The constant filling in a currency the stored rows lack is the same
    fallback, just per currency."""
    old = dt.datetime.utcnow() - dt.timedelta(days=3)
    db_session.add(FxRate(date=dt.date.today(), currency="EUR", rate_nok=10.9, fetched_at=old))
    db_session.add(Card(card_id="a", variant=None, name="Shellder"))
    db_session.commit()
    fx_rates.reset_cache()
    monkeypatch.setattr(card_images, "fetch_card_data", lambda *a, **k: card_images.CardApiData(None, 42.0))

    result = price_refresh.refresh_stale_prices(db_session)

    assert (result.fx_source, result.status, result.cards_updated) == ("stored", "degraded", 0)


def test_fetch_card_data_returns_no_price_at_the_fx_fallback_rate(monkeypatch):
    import httpx

    payload = {
        "data": [
            {
                "name": "Shellder",
                "number": "1",
                "images": {"small": "https://img/shellder.png"},
                "tcgplayer": {"prices": {"normal": {"market": 2.0}}},
            }
        ]
    }
    monkeypatch.setattr(
        card_images.httpx,
        "get",
        lambda *a, **k: httpx.Response(200, json=payload, request=httpx.Request("GET", "https://x")),
    )
    fx_rates.reset_cache()
    fx_rates.set_rates(fx_rates.FxRates(rates=dict(fx_rates.FALLBACK_RATES), as_of=None, source="fallback"))

    data = card_images.fetch_card_data("Shellder", None, "1/100")

    assert data.image_url == "https://img/shellder.png"  # images need no rate
    assert (data.tcgplayer_price, data.tcgplayer_price_usd, data.fx_unavailable) == (None, None, True)

    # ...and applying it writes nothing, not even a failure stamp.
    card = Card(card_id="a", variant=None, name="Shellder")
    assert price_refresh.apply_price_lookup(card, data, dt.date.today()) is False
    assert _row(card) is None


# --- prints TCGplayer has no key for (issue #350) -----------------------------


def test_a_ball_pattern_card_is_never_looked_up_and_loses_its_old_price(db_session, monkeypatch):
    stale = dt.date.today() - dt.timedelta(days=price_refresh.PRICE_STALE_AFTER_DAYS + 1)
    ball = _priced(Card(card_id="a", variant="Poké Ball Holo", name="Eevee"), 0.3, stale)
    plain = _priced(Card(card_id="b", variant="Normal", name="Eevee"), 0.3, stale)
    db_session.add_all([ball, plain])
    db_session.commit()

    looked_up = []
    monkeypatch.setattr(
        card_images,
        "fetch_card_data",
        lambda name, set_name, number, variant=None: looked_up.append(variant) or card_images.CardApiData(None, 4.0),
    )

    assert price_refresh.price_lookup_due(ball, dt.date.today()) is False
    assert price_refresh.price_lookup_due(plain, dt.date.today()) is True

    result = price_refresh.refresh_stale_prices(db_session)

    assert looked_up == ["Normal"]
    assert (result.other_print_dropped, result.cards_checked) == (1, 1)
    db_session.expire_all()
    ball = db_session.query(Card).filter_by(card_id="a").one()
    assert ball.prices == [] and ball.tcgplayer_price is None
    assert ball.market_price_source is None
    assert db_session.query(Card).filter_by(card_id="b").one().tcgplayer_price == 4.0
