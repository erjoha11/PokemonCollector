"""The "On the way" status (issue #382): Dex's "Incoming" folder routes to
Card.in_transit_qty / in_transit_since instead of a collection, and an
in-transit copy counts as owned but is never available to sell."""
from __future__ import annotations

import datetime as dt
import re

from conftest import make_csv, seed_import

import db as db_module
import importer
import models
import queries
import won_inbox
from importer import import_dex_csv_files
from models import Binder, Card, Collection, Listing, WonItem

DAY1 = dt.date(2026, 10, 1)
DAY2 = dt.date(2026, 10, 2)
DAY3 = dt.date(2026, 10, 3)


def _main(rows=None):
    return make_csv(
        "My Collection",
        rows if rows is not None else [{"id": "a", "name": "Charizard", "qty": 1}, {"id": "b", "name": "Pikachu", "qty": 2}],
    )


def _incoming(rows):
    return make_csv("Incoming", rows)


def _card(db, card_id):
    return db.query(Card).filter(Card.card_id == card_id).one()


# ── Import: routing ──────────────────────────────────────────────────────


def test_incoming_sets_status_never_a_collection_or_binder(db_session):
    result = import_dex_csv_files(
        db_session, [("main.csv", _main()), ("in.csv", _incoming([{"id": "a", "qty": 1}]))], today=DAY1
    )

    a, b = _card(db_session, "a"), _card(db_session, "b")
    assert (a.in_transit_qty, a.in_transit_since) == (1, DAY1)
    assert a.in_transit == 1 and a.in_hand_qty == 0 and a.fully_in_transit
    assert (b.in_transit_qty, b.in_transit_since) == (None, None)
    assert b.in_hand_qty == 2 and not b.fully_in_transit
    assert db_session.query(Collection).count() == 0
    assert db_session.query(Binder).count() == 0
    assert result.warnings == []
    assert result.collections_touched == set() and result.binders_touched == set()


def test_in_transit_qty_is_clamped_to_the_cards_qty(db_session):
    import_dex_csv_files(
        db_session, [("main.csv", _main()), ("in.csv", _incoming([{"id": "b", "qty": 5}]))], today=DAY1
    )
    assert _card(db_session, "b").in_transit_qty == 2


def test_qty0_incoming_rows_are_ignored_and_never_create_cards(db_session):
    # Won, not paid yet: qty 0 in Dex. Never a status, never a card.
    result = import_dex_csv_files(
        db_session,
        [("main.csv", _main()), ("in.csv", _incoming([{"id": "a", "qty": 0}, {"id": "zzz", "qty": 0}]))],
        today=DAY1,
    )
    assert _card(db_session, "a").in_transit_qty is None
    assert db_session.query(Card).filter(Card.card_id == "zzz").count() == 0
    assert result.warnings == []


def test_in_transit_since_is_kept_across_syncs_and_qty_changes(db_session):
    files = [("main.csv", _main()), ("in.csv", _incoming([{"id": "b", "qty": 2}]))]
    import_dex_csv_files(db_session, files, today=DAY1)

    # Next day: qty went 2 -> 3, still tagged.
    main = _main([{"id": "a", "qty": 1}, {"id": "b", "name": "Pikachu", "qty": 3}])
    import_dex_csv_files(db_session, [("main.csv", main), ("in.csv", _incoming([{"id": "b", "qty": 3}]))], today=DAY2)

    b = _card(db_session, "b")
    assert b.qty == 3
    assert (b.in_transit_qty, b.in_transit_since) == (3, DAY1)


def test_tag_removed_clears_the_status(db_session):
    import_dex_csv_files(
        db_session,
        [("main.csv", _main()), ("in.csv", _incoming([{"id": "a", "qty": 1}, {"id": "b", "qty": 2}]))],
        today=DAY1,
    )
    # b arrived: its Incoming tag was removed, a is still on the way.
    result = import_dex_csv_files(
        db_session, [("main.csv", _main()), ("in.csv", _incoming([{"id": "a", "qty": 1}]))], today=DAY2
    )

    a, b = _card(db_session, "a"), _card(db_session, "b")
    assert (a.in_transit_qty, a.in_transit_since) == (1, DAY1)
    assert (b.in_transit_qty, b.in_transit_since) == (None, None)
    assert importer.INCOMING_CLEARED_WARNING not in result.warnings


def test_incoming_absent_in_a_sync_with_my_collection_clears_with_a_warning(db_session):
    import_dex_csv_files(
        db_session, [("main.csv", _main()), ("in.csv", _incoming([{"id": "a", "qty": 1}]))], today=DAY1
    )
    # The last card left Incoming, so Dex exported no Incoming rows at all.
    result = import_dex_csv_files(db_session, [("main.csv", _main())], today=DAY2)

    a = _card(db_session, "a")
    assert (a.in_transit_qty, a.in_transit_since) == (None, None)
    assert importer.INCOMING_CLEARED_WARNING in result.warnings


def test_incoming_absent_with_nothing_in_transit_adds_no_warning(db_session):
    result = import_dex_csv_files(db_session, [("main.csv", _main())], today=DAY1)
    assert result.warnings == []


def test_incoming_older_than_my_collection_is_cleared(db_session):
    import_dex_csv_files(
        db_session,
        [("main.csv", _main()), ("in.csv", _incoming([{"id": "a", "qty": 1}]))],
        today=DAY1,
        file_dates={"main.csv": DAY1, "in.csv": DAY1},
    )
    # The cron re-reads every CSV in the folder: an Incoming export that was
    # never re-exported must not keep the card on the way forever.
    result = import_dex_csv_files(
        db_session,
        [("main.csv", _main()), ("in.csv", _incoming([{"id": "a", "qty": 1}]))],
        today=DAY3,
        file_dates={"main.csv": DAY3, "in.csv": DAY1},
    )

    assert _card(db_session, "a").in_transit_qty is None
    assert importer.INCOMING_CLEARED_WARNING in result.warnings


def test_incoming_dated_same_day_as_my_collection_counts(db_session):
    import_dex_csv_files(
        db_session,
        [("main.csv", _main()), ("in.csv", _incoming([{"id": "a", "qty": 1}]))],
        today=DAY2,
        file_dates={"main.csv": DAY2, "in.csv": DAY2},
    )
    assert _card(db_session, "a").in_transit_qty == 1


def test_combined_export_shares_my_collections_date(db_session):
    # Dex's combined export: My Collection and Incoming rows in one file.
    combined = _main() + b"\n" + b"\n".join(_incoming([{"id": "a", "qty": 1}]).split(b"\n")[1:])
    import_dex_csv_files(db_session, [("all.csv", combined)], today=DAY2, file_dates={"all.csv": DAY2})
    assert _card(db_session, "a").in_transit_qty == 1


def test_sync_without_my_collection_leaves_the_status_untouched(db_session):
    import_dex_csv_files(
        db_session, [("main.csv", _main()), ("in.csv", _incoming([{"id": "a", "qty": 1}]))], today=DAY1
    )
    # A category-only sync, with an Incoming that no longer lists a ...
    import_dex_csv_files(
        db_session,
        [("v.csv", make_csv("Vintage Collection", [{"id": "b"}])), ("in.csv", _incoming([{"id": "b", "qty": 2}]))],
        today=DAY2,
    )
    a, b = _card(db_session, "a"), _card(db_session, "b")
    assert (a.in_transit_qty, a.in_transit_since) == (1, DAY1)
    assert b.in_transit_qty is None
    # ... and one with no Incoming at all.
    import_dex_csv_files(db_session, [("v.csv", make_csv("Vintage Collection", [{"id": "b"}]))], today=DAY3)
    assert _card(db_session, "a").in_transit_qty == 1


def test_card_sold_down_to_qty0_is_not_in_transit(db_session):
    import_dex_csv_files(
        db_session, [("main.csv", _main()), ("in.csv", _incoming([{"id": "a", "qty": 1}]))], today=DAY1
    )
    main = _main([{"id": "a", "name": "Charizard", "qty": 0}, {"id": "b", "name": "Pikachu", "qty": 2}])
    import_dex_csv_files(db_session, [("main.csv", main), ("in.csv", _incoming([{"id": "a", "qty": 1}]))], today=DAY2)
    a = _card(db_session, "a")
    assert a.in_transit_qty is None and a.in_transit == 0 and not a.fully_in_transit


# ── Computed figures ─────────────────────────────────────────────────────


def test_in_hand_figures_are_computed_and_clamped():
    card = Card(card_id="x", name="X", qty=3, in_transit_qty=1)
    assert (card.in_transit, card.in_hand_qty, card.in_hand_spares, card.duplicates) == (1, 2, 1, 2)
    card.in_transit_qty = 9  # a stale stored value never goes negative
    assert (card.in_transit, card.in_hand_qty, card.in_hand_spares) == (3, 0, 0)
    assert card.fully_in_transit
    card.in_transit_qty = None
    assert (card.in_transit, card.in_hand_qty) == (0, 3)


def test_spares_helpers():
    assert [models.in_hand_spares(n) for n in (0, 1, 2, 5)] == [0, 0, 1, 4]
    cards = [Card(card_id="x", name="X", qty=2, in_transit_qty=2), Card(card_id="y", name="X", qty=2)]
    # 4 owned, 2 in hand: one spare in hand (ownership duplicates would be 3).
    assert models.print_in_hand_spares(cards) == 1


def test_overdue_after_21_days():
    today = dt.date.today()
    card = Card(card_id="x", name="X", qty=1, in_transit_qty=1, in_transit_since=today - dt.timedelta(days=20))
    assert card.in_transit_days == 20 and not card.in_transit_overdue
    card.in_transit_since = today - dt.timedelta(days=models.IN_TRANSIT_WARN_DAYS)
    assert card.in_transit_overdue


def test_in_transit_summary_counts_copies_and_value():
    cards = [
        Card(card_id="x", name="X", qty=2, in_transit_qty=2, market_price=100.0, price_flags=""),
        Card(card_id="y", name="Y", qty=1, market_price=50.0, price_flags=""),
    ]
    assert queries.in_transit_summary(cards) == {"count": 2, "value": 200.0}


# ── App: seeded through a real sync ──────────────────────────────────────


def _seed(client, today=None):
    """a: fully on the way (qty 1). b: qty 3, 1 on the way (stored as such,
    though Dex mirrors the total in practice). c: in hand."""
    main = _main(
        [
            {"id": "a", "name": "Charizard", "qty": 1, "price": "100"},
            {"id": "b", "name": "Pikachu", "qty": 3, "price": "10"},
            {"id": "c", "name": "Bulbasaur", "qty": 1, "price": "5"},
        ]
    )
    seed_import(
        client,
        [
            ("files", ("main.csv", main, "text/csv")),
            ("files", ("in.csv", _incoming([{"id": "a", "qty": 1}, {"id": "b", "qty": 1}]), "text/csv")),
        ],
    )
    with db_module.SessionLocal() as s:
        return {c.card_id: c.id for c in s.query(Card)}


def test_sale_items_skip_fully_in_transit_and_clamp_to_in_hand(client):
    ids = _seed(client)
    r = client.post(
        "/sales/generate",
        data={
            "card_id": [str(ids["a"]), str(ids["b"])],
            "qty": ["1", "3"],
            "condition": ["", ""],
            "price": ["", ""],
        },
    )
    assert r.status_code == 200
    assert "Charizard" not in r.text  # on the way: left out of the ad
    assert "Pikachu" in r.text
    # Mark-listed carries only the cards actually in the ad.
    hidden = re.findall(r'name="card_id" value="(\d+)"', r.text)
    assert hidden == [str(ids["b"])]

    import app as app_module

    with db_module.SessionLocal() as s:
        items = app_module._sale_items_from_form(s, [ids["a"], ids["b"]], [1, 3], ["", ""], ["", ""])
    assert [(i.card_id, i.qty) for i in items] == [(ids["b"], 2)]


def test_generate_with_only_in_transit_cards_is_rejected(client):
    ids = _seed(client)
    r = client.post(
        "/sales/generate", data={"card_id": [str(ids["a"])], "qty": ["1"], "condition": [""], "price": [""]}
    )
    assert r.status_code == 400


def test_mark_listed_skips_fully_in_transit_cards(client):
    ids = _seed(client)
    r = client.post(
        "/sales/mark-listed",
        data={"card_id": [str(ids["a"]), str(ids["c"])], "title": "T", "description": "D"},
    )
    assert r.status_code == 200
    with db_module.SessionLocal() as s:
        listing = s.query(Listing).one()
        assert [c.card_id for c in listing.cards] == ["c"]

    r = client.post("/sales/mark-listed", data={"card_id": [str(ids["a"])], "title": "T", "description": "D"})
    assert r.status_code == 400


def test_sales_page_mutes_in_transit_rows(client):
    ids = _seed(client)
    html = client.get("/sales", params={"card_ids": [ids["a"], ids["b"], ids["c"]]}).text
    # a: no form fields at all; b: qty capped at its 2 in hand.
    assert f'name="card_id" value="{ids["a"]}"' not in html
    assert "can't be sold until it arrives" in html
    assert 'max="2"' in html
    assert html.count('class="sale-row-transit"') == 2


def test_inventory_badges_disables_and_filters(client):
    ids = _seed(client)
    html = client.get("/inventory").text
    assert html.count("transit-badge") == 2
    assert re.search(rf'data-card-id="{ids["a"]}" disabled', html)
    assert not re.search(rf'data-card-id="{ids["b"]}" disabled', html)
    assert "Tagged Incoming in Dex · seen since" in html

    filtered = client.get("/inventory", params={"transit": "1"}).text
    assert "Charizard" in filtered and "Pikachu" in filtered and "Bulbasaur" not in filtered
    # The hand-built Clear-filter link keeps it.
    cleared = client.get("/inventory", params={"transit": "1", "rarity": "Common"}).text
    assert "&transit=1&" in cleared


def test_overdue_badge_shows_its_age(client):
    ids = _seed(client)
    with db_module.SessionLocal() as s:
        s.get(Card, ids["a"]).in_transit_since = dt.date.today() - dt.timedelta(days=24)
        s.commit()
    html = client.get("/inventory").text
    assert "transit-badge-overdue" in html
    assert "On the way · 24 d" in html


def test_card_page_shows_on_the_way(client):
    ids = _seed(client)
    html = client.get(f"/cards/{ids['a']}").text
    assert "(on the way)" in html
    assert "<dt>On the way</dt>" in html


def test_dashboard_line_only_when_something_is_on_the_way(client):
    _seed(client)
    html = client.get("/").text
    assert 'href="/inventory?transit=1"' in html
    assert "2 cards on the way" in html

    with db_module.SessionLocal() as s:
        for card in s.query(Card):
            card.in_transit_qty = None
            card.in_transit_since = None
        s.commit()
    assert "on the way ·" not in client.get("/").text


# ── Facebook wins inbox ──────────────────────────────────────────────────


def test_win_candidates_badge_and_rank_in_transit_since_the_sale(db_session):
    ended = dt.date(2026, 10, 3)
    old = dt.datetime(2026, 1, 1)
    plain = Card(card_id="a", name="Charizard ex", qty=1, created_at=old)
    # A 2nd copy of a card owned since January, tagged Incoming after the sale.
    second_copy = Card(card_id="b", name="Charizard ex", qty=2, created_at=old, in_transit_qty=2, in_transit_since=ended)
    # New since the sale, not on the way.
    new = Card(card_id="c", name="Charizard ex", qty=1, created_at=dt.datetime(2026, 10, 4))
    db_session.add_all([plain, second_copy, new])
    item = WonItem(
        id=1, external_ref="fbaw:1:1", source="fbaw", sale_type="auction", post_url="https://x.y/p",
        lot_url="https://x.y/p", label="Charizard ex", status="pending", ended_on=ended,
        first_seen_at=dt.datetime(2026, 10, 4), last_seen_at=dt.datetime(2026, 10, 4),
    )
    db_session.add(item)
    db_session.commit()

    found = won_inbox.candidates(db_session, [item], ("purchase", "ripped", "trade"))[1]
    assert [c.card.card_id for c in found] == ["b", "c", "a"]
    assert found[0].in_transit and found[0].transit_since_sale
    assert not found[1].in_transit
