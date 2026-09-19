import datetime as dt

import card_images
from backfill_images import backfill_missing_images
from models import Card


def _make_card(db, **overrides):
    defaults = dict(
        card_id="1",
        name="Pikachu",
        set="Base Set",
        number="58/102",
        variant=None,
        created_at=dt.datetime.utcnow(),
        image_url=None,
    )
    defaults.update(overrides)
    card = Card(**defaults)
    db.add(card)
    db.commit()
    return card


def test_backfill_fills_in_a_confident_match(db_session, monkeypatch):
    _make_card(db_session, card_id="1", name="Pikachu")

    def fake_fetch_card_data(name, set_name, number, variant=None):
        return card_images.CardApiData(image_url="https://example.com/pikachu.png", tcgplayer_price=None)

    monkeypatch.setattr("backfill_images.card_images.fetch_card_data", fake_fetch_card_data)

    attempted, filled = backfill_missing_images(db_session)

    assert attempted == 1
    assert filled == 1
    card = db_session.query(Card).filter_by(card_id="1").one()
    assert card.image_url == "https://example.com/pikachu.png"


def test_backfill_leaves_unmatched_cards_null_rather_than_guessing(db_session, monkeypatch):
    _make_card(db_session, card_id="1", name="Not A Real Card")

    def fake_fetch_card_data(name, set_name, number, variant=None):
        return card_images.CardApiData(image_url=None, tcgplayer_price=None)

    monkeypatch.setattr("backfill_images.card_images.fetch_card_data", fake_fetch_card_data)

    attempted, filled = backfill_missing_images(db_session)

    assert attempted == 1
    assert filled == 0
    card = db_session.query(Card).filter_by(card_id="1").one()
    assert card.image_url is None


def test_backfill_skips_cards_that_already_have_an_image(db_session, monkeypatch):
    _make_card(db_session, card_id="1", name="Dragonite", image_url="https://example.com/already-there.png")

    def fail_if_called(*args, **kwargs):
        raise AssertionError("should not look up a card that already has an image")

    monkeypatch.setattr("backfill_images.card_images.fetch_card_data", fail_if_called)

    attempted, filled = backfill_missing_images(db_session)

    assert attempted == 0
    assert filled == 0
    card = db_session.query(Card).filter_by(card_id="1").one()
    assert card.image_url == "https://example.com/already-there.png"


def test_backfill_respects_the_limit_budget(db_session, monkeypatch):
    _make_card(db_session, card_id="1", name="Pikachu")
    _make_card(db_session, card_id="2", name="Charizard")
    _make_card(db_session, card_id="3", name="Bulbasaur")

    calls = []

    def fake_fetch_card_data(name, set_name, number, variant=None):
        calls.append(name)
        return card_images.CardApiData(image_url="https://example.com/x.png", tcgplayer_price=None)

    monkeypatch.setattr("backfill_images.card_images.fetch_card_data", fake_fetch_card_data)

    attempted, filled = backfill_missing_images(db_session, limit=2)

    assert attempted == 2
    assert filled == 2
    assert len(calls) == 2
    still_missing = db_session.query(Card).filter(Card.image_url.is_(None)).count()
    assert still_missing == 1
