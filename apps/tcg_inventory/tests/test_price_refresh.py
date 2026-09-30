import datetime as dt

import card_images
import price_refresh
from models import Card


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
    card = Card(card_id="a", variant=None, name="Shellder", tcgplayer_price=1.0)
    card.tcgplayer_price_updated_at = dt.date.today()
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
    card = Card(card_id="a", variant=None, name="Shellder", tcgplayer_price=1.0)
    card.tcgplayer_price_updated_at = dt.date.today() - dt.timedelta(days=price_refresh.PRICE_STALE_AFTER_DAYS + 1)
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
    stale = Card(card_id="b", variant=None, name="Stale")
    stale.tcgplayer_price = 1.0
    stale.tcgplayer_price_updated_at = dt.date.today() - dt.timedelta(days=price_refresh.PRICE_STALE_AFTER_DAYS + 1)
    fresh = Card(card_id="c", variant=None, name="Fresh")
    fresh.tcgplayer_price = 2.0
    fresh.tcgplayer_price_updated_at = dt.date.today()
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
    assert card.price_lookup_failed_at == today
    assert card.tcgplayer_price is None and card.tcgplayer_price_updated_at is None


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

    assert db_session.query(Card).one().price_lookup_failed_at == today


def test_refresh_stale_prices_respects_the_retry_window(db_session, monkeypatch):
    failed_on = dt.date(2026, 9, 1)
    card = Card(card_id="a", variant=None, name="Japanese Print")
    card.price_lookup_failed_at = failed_on
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
    assert db_session.query(Card).one().price_lookup_failed_at == past  # re-stamped


def test_stale_priced_cards_win_the_budget_over_never_priceable_ones(db_session, monkeypatch):
    # The prod shape from issue #216: many cards that never price, a few
    # stale priced ones, a budget smaller than the never-priceable pile.
    today = dt.date(2026, 9, 30)
    for i in range(10):
        db_session.add(Card(card_id=f"ja{i}", variant=None, name=f"Never {i}"))
    for i in range(3):
        card = Card(card_id=f"en{i}", variant=None, name=f"Stale {i}", tcgplayer_price=1.0)
        card.tcgplayer_price_updated_at = today - dt.timedelta(days=price_refresh.PRICE_STALE_AFTER_DAYS + 1 + i)
        db_session.add(card)
    db_session.commit()

    def fake(name, set_name, number, variant=None):
        return card_images.CardApiData(None, 2.0 if name.startswith("Stale") else None)

    monkeypatch.setattr(card_images, "fetch_card_data", fake)

    result = price_refresh.refresh_stale_prices(db_session, today=today, budget=5)

    assert result.cards_checked == 5
    assert result.cards_updated == 3
    stale = db_session.query(Card).filter(Card.card_id.like("en%")).all()
    assert all(c.tcgplayer_price_updated_at == today for c in stale)

    # Next day: the 2 that failed are backed off, so the 5 never-tried ones
    # get the budget instead of the same failures again.
    looked_up = []
    monkeypatch.setattr(card_images, "fetch_card_data", _fail_lookup(looked_up))
    price_refresh.refresh_stale_prices(db_session, today=today + dt.timedelta(days=1), budget=5)
    assert len(looked_up) == 5
    stamped_yesterday = {c.name for c in db_session.query(Card) if c.price_lookup_failed_at == today}
    assert stamped_yesterday.isdisjoint(looked_up)


def test_failed_cards_past_their_window_come_after_never_tried_ones(db_session, monkeypatch):
    today = dt.date(2026, 9, 30)
    retried = Card(card_id="a", variant=None, name="Retry")
    retried.price_lookup_failed_at = today - dt.timedelta(days=price_refresh.PRICE_RETRY_AFTER_DAYS + 1)
    db_session.add_all([retried, Card(card_id="b", variant=None, name="Never tried")])
    db_session.commit()
    looked_up = []
    monkeypatch.setattr(card_images, "fetch_card_data", _fail_lookup(looked_up))

    price_refresh.refresh_stale_prices(db_session, today=today, budget=1)

    assert looked_up == ["Never tried"]


def test_a_successful_lookup_clears_the_failure_stamp(db_session, monkeypatch):
    today = dt.date(2026, 9, 30)
    card = Card(card_id="a", variant=None, name="Shellder")
    card.price_lookup_failed_at = today - dt.timedelta(days=price_refresh.PRICE_RETRY_AFTER_DAYS + 1)
    db_session.add(card)
    db_session.commit()
    monkeypatch.setattr(
        card_images, "fetch_card_data", lambda name, set_name, number, variant=None: card_images.CardApiData(None, 7.0)
    )

    price_refresh.refresh_stale_prices(db_session, today=today)

    card = db_session.query(Card).one()
    assert card.tcgplayer_price == 7.0
    assert card.price_lookup_failed_at is None


def test_reprice_all_refetches_every_priced_card_regardless_of_staleness(db_session, monkeypatch):
    fresh = Card(card_id="a", variant=None, name="Fresh", tcgplayer_price=110.0)
    fresh.tcgplayer_price_updated_at = dt.date.today()
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
    card = Card(card_id="a", variant=None, name="Shellder", tcgplayer_price=110.0)
    card.tcgplayer_price_updated_at = old_date
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
    assert refreshed.price_lookup_failed_at is None  # not backed off either


def test_reprice_all_respects_limit_oldest_first(db_session, monkeypatch):
    for i, day in enumerate([5, 1, 3]):
        card = Card(card_id=f"c{i}", variant=None, name=f"Card {day}", tcgplayer_price=1.0)
        card.tcgplayer_price_updated_at = dt.date(2026, 9, day)
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
