"""Tests for pricing.py -- card_prices, the resolver and its materialized
result on cards (issue #210)."""
import datetime as dt
from types import SimpleNamespace

from conftest import make_csv

import card_images
import pricing
import snapshots
from importer import import_dex_csv_files
from models import Card, CardPrice, CardSnapshot

TODAY = dt.date(2026, 9, 30)


def _row(source, price, fetched_days_ago, flags=None):
    fetched = None if fetched_days_ago is None else TODAY - dt.timedelta(days=fetched_days_ago)
    return SimpleNamespace(source=source, price_nok=price, fetched_at=fetched, flags=flags)


# --- resolve (pure) --------------------------------------------------------


def test_first_fresh_source_in_chain_order_wins():
    res = pricing.resolve([_row("pokemontcg", 110.0, 1), _row("dex", 100.0, 3)], TODAY)
    assert (res.price, res.source, res.flags) == (100.0, "dex", None)
    assert res.as_of == TODAY - dt.timedelta(days=3)


def test_a_stale_source_falls_through_to_the_next_fresh_one():
    res = pricing.resolve([_row("dex", 100.0, pricing.FRESH_DAYS + 1), _row("pokemontcg", 110.0, 2)], TODAY)
    assert (res.price, res.source, res.flags) == (110.0, "pokemontcg", None)


def test_freshness_window_is_inclusive_and_longer_than_the_refresh_cadence():
    import price_refresh

    assert pricing.FRESH_DAYS > price_refresh.PRICE_STALE_AFTER_DAYS
    res = pricing.resolve([_row("dex", 100.0, pricing.FRESH_DAYS)], TODAY)
    assert res.flags is None


def test_all_stale_keeps_the_most_recent_price_flagged_stale():
    res = pricing.resolve([_row("dex", 100.0, 40), _row("pokemontcg", 110.0, 20)], TODAY)
    assert (res.price, res.source, res.flags) == (110.0, "pokemontcg", "stale")
    assert res.as_of == TODAY - dt.timedelta(days=20)


def test_no_price_only_when_nothing_ever_existed():
    assert pricing.resolve([], TODAY).flags == "no_price"
    # A source that only ever failed is still "no price".
    res = pricing.resolve([_row("pokemontcg", None, None)], TODAY)
    assert (res.price, res.source, res.flags) == (None, None, "no_price")


def test_the_winning_rows_flags_are_carried_onto_the_card():
    res = pricing.resolve([_row("pokemontcg", 110.0, 1, flags="variant_price_uncertain")], TODAY)
    assert res.flags == "variant_price_uncertain"
    stale = pricing.resolve([_row("pokemontcg", 110.0, 30, flags="variant_price_uncertain")], TODAY)
    assert stale.flags == "stale,variant_price_uncertain"


def test_a_disqualified_fresh_row_is_skipped():
    res = pricing.resolve([_row("dex", 100.0, 1, flags="low_confidence"), _row("pokemontcg", 110.0, 1)], TODAY)
    assert res.source == "pokemontcg"


# --- writing + resolve_cards --------------------------------------------------


def test_resolve_cards_materializes_the_result_and_only_writes_changes(db_session):
    card = Card(card_id="a", name="Pikachu", qty=1)
    pricing.record_price(card, "dex", price_nok=100.0, price=100.0, currency="NOK", fx_rate=1.0, fetched_at=TODAY)
    db_session.add(card)
    db_session.add(Card(card_id="b", name="Nothing", qty=1))

    assert pricing.resolve_cards(db_session, today=TODAY) == 2
    db_session.commit()
    a = db_session.query(Card).filter_by(card_id="a").one()
    b = db_session.query(Card).filter_by(card_id="b").one()
    assert (a.market_price, a.market_price_source, a.market_price_as_of, a.price_flags) == (100.0, "dex", TODAY, None)
    assert a.display_price == 100.0
    assert (b.market_price, b.price_flags, b.display_price) == (None, "no_price", None)

    assert pricing.resolve_cards(db_session, today=TODAY) == 0  # nothing changed

    later = TODAY + dt.timedelta(days=pricing.FRESH_DAYS + 1)
    assert pricing.resolve_cards(db_session, today=later) == 1
    db_session.commit()
    a = db_session.query(Card).filter_by(card_id="a").one()
    assert (a.market_price, a.price_flags) == (100.0, "stale")  # never dropped to 0/None


def test_bulk_record_prices_inserts_then_updates_in_place(db_session):
    card = Card(card_id="a", name="Pikachu", qty=1)
    db_session.add(card)
    db_session.flush()
    pricing.bulk_record_prices(db_session, "dex", {card.id: 10.0}, TODAY - dt.timedelta(days=1))
    pricing.bulk_record_prices(db_session, "dex", {card.id: 12.0}, TODAY)
    db_session.commit()

    row = db_session.query(CardPrice).one()
    assert (row.source, row.price, row.price_nok, row.currency, row.fx_rate, row.fetched_at) == (
        "dex",
        12.0,
        12.0,
        "NOK",
        1.0,
        TODAY,
    )


def test_display_price_falls_back_to_the_legacy_columns_for_an_unresolved_card():
    assert Card(card_id="a", name="x", reference_price=5.0).display_price == 5.0
    assert Card(card_id="a", name="x", reference_price=5.0, tcgplayer_price=6.0).display_price == 6.0
    assert Card(card_id="a", name="x", tcgplayer_price=6.0, price_flags="no_price").display_price is None


# --- importer ------------------------------------------------------------------


def test_import_writes_a_dex_row_and_resolves_the_card(db_session):
    import_dex_csv_files(db_session, [("main.csv", make_csv("My Collection", [{"id": "a", "price": "kr 12,50"}]))])

    card = db_session.query(Card).one()
    row = db_session.query(CardPrice).filter_by(source="dex").one()
    assert (row.price_nok, row.currency, row.fetched_at) == (12.5, "NOK", dt.date.today())
    assert (card.market_price, card.market_price_source, card.reference_price) == (12.5, "dex", 12.5)


def test_an_empty_dex_price_cell_keeps_the_last_known_price(db_session):
    import_dex_csv_files(db_session, [("main.csv", make_csv("My Collection", [{"id": "a", "price": "10"}]))])
    import_dex_csv_files(db_session, [("main.csv", make_csv("My Collection", [{"id": "a", "price": ""}]))])

    card = db_session.query(Card).one()
    row = db_session.query(CardPrice).filter_by(source="dex").one()
    assert row.price_nok == 10.0
    assert card.reference_price == 10.0  # the mirror isn't wiped either
    assert card.market_price == 10.0


def test_import_records_pokemontcg_with_native_price_rate_and_variant_flag(db_session, monkeypatch):
    monkeypatch.setattr(
        card_images,
        "fetch_card_data",
        lambda *a, **kw: card_images.CardApiData(
            image_url=None,
            tcgplayer_price=95.0,
            variant_price_uncertain=True,
            tcgplayer_price_usd=9.5,
            tcgplayer_variant_key="holofoil",
            usd_to_nok=10.0,
        ),
    )
    import_dex_csv_files(db_session, [("main.csv", make_csv("My Collection", [{"id": "a", "price": ""}]))])

    card = db_session.query(Card).one()
    row = db_session.query(CardPrice).filter_by(source="pokemontcg").one()
    assert (row.price, row.currency, row.fx_rate, row.price_nok, row.variant_key) == (9.5, "USD", 10.0, 95.0, "holofoil")
    assert row.flags == "variant_price_uncertain"
    # No Dex price, so TCGplayer via pokemontcg.io wins, flag and all.
    assert (card.market_price, card.market_price_source, card.price_flags) == (95.0, "pokemontcg", "variant_price_uncertain")
    assert card.tcgplayer_price == 95.0  # mirror


def test_dex_outranks_pokemontcg_when_both_are_fresh(db_session, monkeypatch):
    monkeypatch.setattr(
        card_images, "fetch_card_data", lambda *a, **kw: card_images.CardApiData(image_url=None, tcgplayer_price=95.0)
    )
    import_dex_csv_files(db_session, [("main.csv", make_csv("My Collection", [{"id": "a", "price": "80"}]))])

    card = db_session.query(Card).one()
    assert (card.market_price, card.market_price_source) == (80.0, "dex")


def test_full_load_deletes_the_cards_price_rows(db_session):
    import_dex_csv_files(db_session, [("main.csv", make_csv("My Collection", [{"id": "a"}, {"id": "b"}]))])
    import_dex_csv_files(db_session, [("main.csv", make_csv("My Collection", [{"id": "a"}]))], full_load=True)

    assert {r.card.card_id for r in db_session.query(CardPrice)} == {"a"}


# --- snapshots -----------------------------------------------------------------


def test_snapshot_records_the_resolved_price_and_its_source(db_session):
    import_dex_csv_files(db_session, [("main.csv", make_csv("My Collection", [{"id": "a", "price": "10"}]))])
    snapshots.record_daily_snapshot(db_session, as_of=TODAY)

    snap = db_session.query(CardSnapshot).one()
    assert (snap.reference_price, snap.price_source) == (10.0, "dex")
