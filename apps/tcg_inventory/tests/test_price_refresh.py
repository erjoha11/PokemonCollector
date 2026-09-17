import datetime as dt

import card_images
import price_refresh
from models import Card


def test_refresh_stale_prices_updates_never_priced_cards(db_session, monkeypatch):
    card = Card(card_id="a", variant=None, name="Shellder")
    db_session.add(card)
    db_session.commit()

    monkeypatch.setattr(
        card_images, "fetch_card_data", lambda name, set_name, number: card_images.CardApiData(None, 9.99)
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
        lambda name, set_name, number: calls.append(1) or card_images.CardApiData(None, 42.0),
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
        card_images, "fetch_card_data", lambda name, set_name, number: card_images.CardApiData(None, 42.0)
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
        lambda name, set_name, number: card_images.CardApiData(
            image_url=None, tcgplayer_price=None, low_confidence_match=True
        ),
    )

    result = price_refresh.refresh_stale_prices(db_session)

    refreshed = db_session.query(Card).filter(Card.card_id == "a").one()
    assert refreshed.tcgplayer_price is None
    assert result.cards_updated == 0
    assert result.cards_low_confidence == ["Shellder (? ?)"]


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
        lambda name, set_name, number: checked_names.append(name) or card_images.CardApiData(None, 5.0),
    )

    result = price_refresh.refresh_stale_prices(db_session, budget=1)

    assert result.cards_checked == 1
    # Never-priced (oldest, via the NULL-sorts-first-in-Python ordering) is
    # checked before the merely-stale card, and the fresh one isn't touched.
    assert checked_names == ["Never Priced"]
