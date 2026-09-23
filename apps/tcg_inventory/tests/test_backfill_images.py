import datetime as dt

import pytest

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


def test_backfill_tries_the_card_id_lookup_first(db_session, monkeypatch):
    _make_card(db_session, card_id="ex5-4", name="Dark Celebi", number="4/101")
    monkeypatch.setattr(
        "backfill_images.card_images.fetch_image_by_card_id", lambda card_id, name, number: f"https://img/{card_id}.png"
    )
    monkeypatch.setattr("backfill_images.card_images.fetch_card_data", lambda *a, **kw: pytest.fail("not needed"))

    assert backfill_missing_images(db_session) == (1, 1)
    assert db_session.query(Card).one().image_url == "https://img/ex5-4.png"


def test_backfill_never_uses_the_english_name_search_for_japanese_cards(db_session, monkeypatch):
    _make_card(db_session, card_id="jpn_sv2a-168", name="Charmander")
    monkeypatch.setattr("backfill_images.card_images.fetch_image_by_card_id", lambda *a: None)
    monkeypatch.setattr("backfill_images.card_images.fetch_card_data", lambda *a, **kw: pytest.fail("English API for a JP card"))

    assert backfill_missing_images(db_session) == (1, 0)


def test_backfill_goes_most_valuable_first_and_parks_misses(db_session, monkeypatch):
    _make_card(db_session, card_id="cheap", name="Cheap", reference_price=1, qty=1)
    _make_card(db_session, card_id="pricey", name="Pricey", reference_price=500, qty=1)
    _make_card(db_session, card_id="gone", name="Gone", reference_price=900, qty=0)
    seen = []
    monkeypatch.setattr("backfill_images.card_images.fetch_image_by_card_id", lambda card_id, *a: seen.append(card_id))
    monkeypatch.setattr(
        "backfill_images.card_images.fetch_card_data",
        lambda *a, **kw: card_images.CardApiData(image_url=None, tcgplayer_price=None),
    )
    today = dt.date(2026, 9, 23)

    backfill_missing_images(db_session, today=today)
    assert seen == ["pricey", "cheap", "gone"]  # owned cards by price, then not-owned
    assert {c.image_lookup_failed_at for c in db_session.query(Card)} == {today}

    seen.clear()
    assert backfill_missing_images(db_session, today=today + dt.timedelta(days=5)) == (0, 0)  # parked
    backfill_missing_images(db_session, today=today + dt.timedelta(days=31))
    assert len(seen) == 3  # retried after IMAGE_RETRY_AFTER_DAYS


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
