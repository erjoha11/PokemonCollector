"""Pricing Phase 2, part 2 (issue #210): the card page's source line, flag
chips and per-source table, source-switch tooltip data on the value-history
and card price-history charts, the source in Price movers' row title, and
the `reference_price` -> `market_price` sort key rename (old key kept as an
alias)."""
import datetime as dt
import json
import re

from conftest import make_csv, seed_import

import pricing
import queries
from models import Card, CardPrice, CardSnapshot


def _session():
    import db as db_module

    return db_module.SessionLocal()


def _chart_cfg(html: str, card_id: str) -> dict:
    m = re.search(rf'<script type="application/json" id="{card_id}-data">(.*?)</script>', html, re.S)
    assert m, f"no chart data for {card_id}"
    return json.loads(m.group(1))


# --------------------------------------------------------------------------
# Labels
# --------------------------------------------------------------------------
def test_dex_is_labelled_tcgplayer_via_dex_for_every_language():
    # Owner decision: Dex uses TCGplayer prices for Japanese cards too, so
    # the label is one per source, never per language.
    assert pricing.source_label("dex") == "TCGplayer via Dex"
    assert pricing.source_label("pokemontcg") == "TCGplayer via pokemontcg.io"
    assert pricing.source_label(None) == "–"
    assert pricing.source_label("something_new") == "something_new"
    assert all("TCGPlayer" not in label for label in pricing.SOURCE_LABELS.values())


def test_flag_labels_and_switch_notes():
    assert pricing.flag_list("stale,variant_price_uncertain") == ["stale", "variant_price_uncertain"]
    assert pricing.flag_list(None) == []
    assert pricing.flag_label("no_price") == "No price"
    assert pricing.flag_label("unknown_flag") == "unknown_flag"
    assert pricing.source_switch_note("pokemontcg", "dex") == "Source: TCGplayer via pokemontcg.io → TCGplayer via Dex"
    assert pricing.source_switch_note("dex", "pokemontcg", 3).endswith("(3 cards)")
    assert pricing.source_switch_note("dex", "pokemontcg", 1).endswith("(1 card)")
    # Gaining/losing a price, or no change, is not a switch.
    assert pricing.source_switch_note(None, "dex") is None
    assert pricing.source_switch_note("dex", None) is None
    assert pricing.source_switch_note("dex", "dex") is None


# --------------------------------------------------------------------------
# Card page
# --------------------------------------------------------------------------
def _card_with_prices(flags=None, fetched_ago=2, japanese=False):
    db = _session()
    today = dt.date.today()
    card = Card(
        card_id="jpn_sv1-1" if japanese else "sv1-1",
        name="Sprigatito",
        qty=1,
        language="Japanese" if japanese else "English",
        market_price=120.0,
        market_price_source="dex",
        market_price_as_of=today - dt.timedelta(days=fetched_ago),
        price_flags=flags,
    )
    db.add(card)
    db.flush()
    db.add_all(
        [
            CardPrice(
                card_id=card.id, source="dex", price=120.0, currency="NOK", fx_rate=1.0, price_nok=120.0,
                fetched_at=today - dt.timedelta(days=fetched_ago),
            ),
            CardPrice(
                card_id=card.id, source="pokemontcg", price=10.5, currency="USD", fx_rate=10.0, price_nok=105.0,
                fetched_at=dt.date(2026, 9, 1), lookup_failed_at=dt.date(2026, 9, 20),
                flags="variant_price_uncertain",
            ),
        ]
    )
    db.commit()
    pk = card.id
    db.close()
    return pk


def test_card_page_shows_market_price_source_line(client):
    pk = _card_with_prices()
    html = client.get(f"/cards/{pk}").text
    assert "Market price" in html
    assert re.search(r'card-price-source-label">TCGplayer via Dex</span>\s*·\s*<span[^>]*>2 d ago</span>', html)
    # The old one-line "Prices" entry (and its "TCGPlayer" typo) is gone.
    assert "TCGPlayer" not in html
    assert "<dt>Prices</dt>" not in html


def test_card_page_japanese_dex_price_is_tcgplayer_via_dex(client):
    pk = _card_with_prices(japanese=True)
    html = client.get(f"/cards/{pk}").text
    assert 'card-price-source-label">TCGplayer via Dex<' in html


def test_card_page_flag_chips_and_no_price(client):
    pk = _card_with_prices(flags="stale")
    html = client.get(f"/cards/{pk}").text
    assert re.search(r'class="price-flag"[^>]*>Stale</span>', html)
    # Flagged: the per-source table opens by default.
    assert '<details class="price-sources" open>' in html

    db = _session()
    bare = Card(card_id="bare", name="Bare", qty=1, price_flags="no_price")
    db.add(bare)
    db.commit()
    bare_pk = bare.id
    db.close()
    html = client.get(f"/cards/{bare_pk}").text
    assert "No price from any source" in html
    assert re.search(r'class="price-flag price-flag-error"[^>]*>No price</span>', html)
    assert "No source has been asked" in html


def test_card_page_per_source_table(client):
    pk = _card_with_prices()
    html = client.get(f"/cards/{pk}").text
    table = html.split('<table class="price-sources-table">', 1)[1].split("</table>", 1)[0]
    rows = table.split("<tbody>", 1)[1].split("<tr")[1:]
    assert len(rows) == 2
    # Chain order: dex first, then pokemontcg.
    assert "TCGplayer via Dex" in rows[0] and "price-source-used" in rows[0] and ">Used<" in rows[0]
    assert "TCGplayer via pokemontcg.io" in rows[1]
    assert "10.50 USD" in rows[1] and "105 kr" in rows[1]
    assert "01.09.2026" in rows[1]
    assert "Lookup failed 20.09.2026" in rows[1]
    assert "Variant uncertain" in rows[1]
    assert ">Used<" not in rows[1]
    # Not flagged: collapsed by default.
    assert '<details class="price-sources">' in html


# --------------------------------------------------------------------------
# Switch tooltips
# --------------------------------------------------------------------------
def test_card_price_history_marks_source_switch_points(db_session):
    card = Card(card_id="x", name="X", qty=1, market_price=11, market_price_source="dex")
    db_session.add(card)
    db_session.flush()
    for day, source, price in [(1, "pokemontcg", 10), (2, "pokemontcg", 10), (3, "dex", 11), (4, None, None), (5, "dex", 11)]:
        db_session.add(
            CardSnapshot(card_id=card.id, date=dt.date(2026, 9, day), source="cron", qty=1, reference_price=price, price_source=source)
        )
    db_session.commit()

    notes = [p["source_note"] for p in queries.card_price_history(db_session, card.id)]
    assert notes == [None, None, "Source: TCGplayer via pokemontcg.io → TCGplayer via Dex", None, None]


def test_card_price_history_infers_pre_210_sources(db_session):
    # Pre-#210 snapshots have no price_source; like Price movers, a card with a
    # pokemontcg price today is taken to have shown it back then.
    card = Card(card_id="x", name="X", qty=1, market_price=11, market_price_source="dex")
    db_session.add(card)
    db_session.flush()
    db_session.add(CardPrice(card_id=card.id, source="pokemontcg", price_nok=10, fetched_at=dt.date(2026, 9, 1)))
    db_session.add(CardSnapshot(card_id=card.id, date=dt.date(2026, 9, 1), source="cron", qty=1, reference_price=10))
    db_session.add(
        CardSnapshot(card_id=card.id, date=dt.date(2026, 9, 2), source="cron", qty=1, reference_price=11, price_source="dex")
    )
    db_session.commit()

    history = queries.card_price_history(db_session, card.id)
    assert [p["source"] for p in history] == [None, "dex"]  # as recorded
    assert history[1]["source_note"] == "Source: TCGplayer via pokemontcg.io → TCGplayer via Dex"


def test_value_history_source_notes_count_switches_per_day(db_session):
    a = Card(card_id="a", name="A", qty=1, market_price=10, market_price_source="dex")
    b = Card(card_id="b", name="B", qty=1, market_price=20, market_price_source="dex")
    c = Card(card_id="c", name="C", qty=1, market_price=30, market_price_source="pokemontcg")
    new = Card(card_id="n", name="N", qty=1, market_price=5, market_price_source="dex")
    db_session.add_all([a, b, c, new])
    db_session.flush()
    rows = [
        # day 1: a, b on pokemontcg; c on dex
        (a, 1, "cron", "pokemontcg"), (b, 1, "cron", "pokemontcg"), (c, 1, "cron", "dex"),
        # day 2: a and b switch to dex (a only in the day's closing snapshot),
        # c switches to pokemontcg, a new card appears (not a switch).
        (a, 2, "cron", "pokemontcg"), (a, 2, "manual", "dex"), (b, 2, "cron", "dex"),
        (c, 2, "cron", "pokemontcg"), (new, 2, "cron", "dex"),
        # day 3: nothing changes
        (a, 3, "cron", "dex"), (b, 3, "cron", "dex"), (c, 3, "cron", "pokemontcg"), (new, 3, "cron", "dex"),
    ]
    for card, day, snap_source, price_source in rows:
        db_session.add(
            CardSnapshot(card_id=card.id, date=dt.date(2026, 9, day), source=snap_source, qty=1,
                         reference_price=10, price_source=price_source)
        )
    db_session.commit()

    today = dt.date(2026, 9, 3)
    history = queries.real_value_history(db_session, metric="total", today=today)
    notes = queries.history_source_notes(db_session, history, today=today)
    assert notes == [
        [],
        [
            "Source: TCGplayer via pokemontcg.io → TCGplayer via Dex (2 cards)",
            "Source: TCGplayer via Dex → TCGplayer via pokemontcg.io (1 card)",
        ],
        [],
    ]


def test_value_history_source_notes_for_todays_live_point(db_session):
    today = dt.date(2026, 9, 3)
    a = Card(card_id="a", name="A", qty=1, market_price=10, market_price_source="dex")
    db_session.add(a)
    db_session.flush()
    db_session.add(
        CardSnapshot(card_id=a.id, date=dt.date(2026, 9, 2), source="cron", qty=1, reference_price=9, price_source="pokemontcg")
    )
    db_session.commit()

    history = queries.real_value_history(db_session, metric="total", today=today, live=(10.0, 1))
    assert history[-1]["date"] == today
    notes = queries.history_source_notes(db_session, history, live_cards=[a], today=today)
    assert notes == [[], ["Source: TCGplayer via pokemontcg.io → TCGplayer via Dex (1 card)"]]


def test_market_value_chart_carries_source_notes(client):
    db = _session()
    card = Card(card_id="a", name="A", qty=1, market_price=10, market_price_source="dex")
    db.add(card)
    db.flush()
    db.add(CardSnapshot(card_id=card.id, date=dt.date(2026, 9, 1), source="cron", qty=1, reference_price=9, price_source="pokemontcg"))
    db.add(CardSnapshot(card_id=card.id, date=dt.date(2026, 9, 2), source="cron", qty=1, reference_price=10, price_source="dex"))
    db.commit()
    db.close()

    cfg = _chart_cfg(client.get("/").text, "dashboard-market-value-card")
    assert len(cfg["sourceNotes"]) == len(cfg["labels"])
    idx = cfg["labels"].index("2026-09-02")
    assert cfg["sourceNotes"][idx] == ["Source: TCGplayer via pokemontcg.io → TCGplayer via Dex (1 card)"]


def test_market_value_chart_has_no_source_notes_without_switches(client):
    db = _session()
    card = Card(card_id="a", name="A", qty=1, market_price=10, market_price_source="dex")
    db.add(card)
    db.flush()
    db.add(CardSnapshot(card_id=card.id, date=dt.date(2026, 9, 1), source="cron", qty=1, reference_price=9, price_source="dex"))
    db.commit()
    db.close()

    assert "sourceNotes" not in _chart_cfg(client.get("/").text, "dashboard-market-value-card")


def test_card_price_chart_carries_source_notes(client):
    db = _session()
    card = Card(card_id="a", name="A", qty=1, market_price=10, market_price_source="dex")
    db.add(card)
    db.flush()
    db.add(CardSnapshot(card_id=card.id, date=dt.date(2026, 9, 1), source="cron", qty=1, reference_price=9, price_source="pokemontcg"))
    db.add(CardSnapshot(card_id=card.id, date=dt.date(2026, 9, 2), source="cron", qty=1, reference_price=10, price_source="dex"))
    db.commit()
    pk = card.id
    db.close()

    cfg = _chart_cfg(client.get(f"/cards/{pk}").text, "card-price-history")
    assert cfg["sourceNotes"] == [None, "Source: TCGplayer via pokemontcg.io → TCGplayer via Dex"]
    assert cfg["datasets"][0]["label"] == "Market price"


# --------------------------------------------------------------------------
# Price movers title
# --------------------------------------------------------------------------
def test_price_movers_row_title_names_the_source(client):
    db = _session()
    card = Card(card_id="a", name="Mover", qty=1, market_price=200, market_price_source="dex")
    db.add(card)
    db.flush()
    db.add(
        CardSnapshot(card_id=card.id, date=dt.date.today() - dt.timedelta(days=30), source="cron", qty=1,
                     reference_price=100, price_source="dex")
    )
    db.commit()
    db.close()

    html = client.get("/").text
    assert 'title="100 kr → 200 kr · Source: TCGplayer via Dex"' in html


# --------------------------------------------------------------------------
# Sort key rename
# --------------------------------------------------------------------------
def _seed_prices(client):
    main = make_csv(
        "My Collection",
        [
            {"id": "cheap", "name": "Cheapmon", "price": "5"},
            {"id": "dear", "name": "Dearmon", "price": "500"},
            {"id": "mid", "name": "Midmon", "price": "50"},
        ],
    )
    seed_import(client, [("files", ("main.csv", main, "text/csv"))])


def _tbody(html: str) -> str:
    return html.split("<tbody>", 1)[1].split("</tbody>", 1)[0]


def test_inventory_market_price_sort_and_reference_price_alias(client):
    _seed_prices(client)
    new = client.get("/inventory?sort=market_price&direction=desc").text
    old = client.get("/inventory?sort=reference_price&direction=desc").text
    body = _tbody(new)
    assert body.index("Dearmon") < body.index("Midmon") < body.index("Cheapmon")
    assert _tbody(old) == body
    # The alias is normalized: the page's own links/hidden field use the new key.
    assert 'name="sort" value="market_price"' in old
    assert "sort=reference_price" not in new
    assert "Market price" in new


def test_dashboard_and_picker_price_sort_alias(client):
    _seed_prices(client)
    html = client.get("/?tsort=reference_price&tdir=asc").text
    top = html.split('id="dashboard-top-cards-card"', 1)[1]
    assert top.index("Cheapmon") < top.index("Midmon") < top.index("Dearmon")
    assert "viz-filter-pill active" in top.split("Market price", 1)[0].rsplit("<a", 1)[1]

    old = client.get("/transactions?pick=all&gsort=reference_price&gdir=desc").text
    new = client.get("/transactions?pick=all&gsort=market_price&gdir=desc").text
    picker_old = old.split("Dearmon", 1)[1]
    assert picker_old.index("Midmon") < picker_old.index("Cheapmon")
    picker_new = new.split("Dearmon", 1)[1]
    assert picker_new.index("Midmon") < picker_new.index("Cheapmon")
