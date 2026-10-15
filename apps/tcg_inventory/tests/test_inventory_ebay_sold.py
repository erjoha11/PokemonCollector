"""Inventory's eBay sold column (issue #416): the PokeTrace eBay NM median
from `card_prices` source `poketrace_ebay`, reference only, next to Market
price. Same display gate as the card page (poketrace_prices.shows_value),
loaded in one query per page, and sortable with "no value" cards last.

All offline: rows are written straight through pricing.record_price.
"""
import datetime as dt
import re

import pytest

import masterdata
import poketrace_prices as P
import pricing
from models import Card, CardPrice

TODAY = dt.date.today()
USD_TO_NOK = 10.0


def _session():
    import db as db_module

    return db_module.SessionLocal()


def _add(db, name, number, *, ebay_usd=None, sample_size=551, flags=(), failed=False, market=100.0):
    card = Card(card_id=f"sv3pt5-{number}", name=name, number=f"{number}/165", variant="Holo",
                language="International", qty=1, created_at=dt.datetime(2026, 1, 1))
    db.add(card)
    db.flush()
    masterdata.link_card(db, card)
    pricing.record_price(card, pricing.SOURCE_DEX, price_nok=market, fetched_at=TODAY, currency="NOK", fx_rate=1.0)
    if failed:
        pricing.record_failure(card, P.SOURCE, TODAY)
    elif ebay_usd is not None:
        pricing.record_price(card, P.SOURCE, price_nok=round(ebay_usd * USD_TO_NOK, 2), price=ebay_usd,
                             currency="USD", fx_rate=USD_TO_NOK, fetched_at=TODAY, flags=flags,
                             sample_size=sample_size, window_days=P.WINDOW_DAYS)
    db.flush()
    return card


def _seed(**cases):
    """name -> kwargs for _add; returns name -> card id."""
    with _session() as db:
        ids = {}
        for i, (name, kwargs) in enumerate(cases.items(), start=1):
            ids[name] = _add(db, name, i, **kwargs).id
        pricing.resolve_cards(db, today=TODAY)
        db.commit()
        return ids


def _ebay_cell(html, name):
    """The eBay sold <td> of the row whose card link says `name`."""
    row_start = html.index(f">{name}<")
    row_end = html.index("</tr>", row_start)
    row = html[row_start:row_end]
    match = re.search(r'<td data-col="ebay_sold" class="num">(.*?)</td>', row, re.S)
    assert match, row
    return match.group(1).strip()


def _names_in_order(html, names):
    return sorted(names, key=lambda n: html.index(f">{n}<"))


def test_header_sits_right_of_market_price_with_tooltip(client):
    _seed(Alpha={"ebay_usd": 12.0})
    html = client.get("/inventory").text
    market = html.index('<th data-col="price"')
    ebay = html.index('<th data-col="ebay_sold"')
    net_paid = html.index('<th data-col="net_paid"')
    assert market < ebay < net_paid
    assert 'title="eBay Near Mint median, last 30 days (PokeTrace) · reference only"' in html
    assert 'data-col-toggle="ebay_sold"' in html  # in the column chooser
    assert "sort=ebay_sold" in html  # sortable header


def test_priced_card_shows_nok_and_sale_count(client):
    _seed(Alpha={"ebay_usd": 342.745})
    cell = _ebay_cell(client.get("/inventory").text, "Alpha")
    assert "3" in cell and "427" in cell  # 3427.45 NOK, via the kr filter
    assert "n=551 · 30d" in cell
    assert "Few sales" not in cell and "—" not in cell


@pytest.mark.parametrize("kwargs", [
    {"ebay_usd": 50.0, "sample_size": 4},
    {"ebay_usd": 50.0, "flags": (pricing.FLAG_LOW_CONFIDENCE,)},
    {"ebay_usd": 50.0, "sample_size": None},
])
def test_few_sales_hides_the_value(client, kwargs):
    _seed(Alpha=kwargs)
    cell = _ebay_cell(client.get("/inventory").text, "Alpha")
    assert "Few sales" in cell and "ebay-sold-few" in cell
    assert "too thin to compare" in cell  # the card page's tooltip
    assert "500" not in cell and "n=" not in cell


def test_min_sales_comes_from_the_env(client, monkeypatch):
    monkeypatch.setenv("POKETRACE_MIN_SALES", "600")
    _seed(Alpha={"ebay_usd": 50.0})  # 551 sales
    assert "Few sales" in _ebay_cell(client.get("/inventory").text, "Alpha")


def test_failed_lookup_shows_a_dash(client):
    _seed(Alpha={"failed": True})
    with _session() as db:
        row = db.query(CardPrice).filter_by(source=P.SOURCE).one()
        assert row.price_nok is None and row.lookup_failed_at is not None
    assert _ebay_cell(client.get("/inventory").text, "Alpha") == "—"


def test_no_row_shows_a_dash(client):
    _seed(Alpha={})
    assert _ebay_cell(client.get("/inventory").text, "Alpha") == "—"


def test_market_price_is_untouched(client):
    ids = _seed(Alpha={"ebay_usd": 999.0, "market": 100.0})
    with _session() as db:
        card = db.get(Card, ids["Alpha"])
        assert card.market_price == 100.0 and card.market_price_source == pricing.SOURCE_DEX


CASES = {
    "Cheap": {"ebay_usd": 10.0},
    "Pricey": {"ebay_usd": 300.0},
    "Middle": {"ebay_usd": 50.0},
    "Thin": {"ebay_usd": 9999.0, "sample_size": 2},  # few sales: sorts as no value
    "Shaky": {"ebay_usd": 8888.0, "flags": (pricing.FLAG_LOW_CONFIDENCE,)},
    "Failed": {"failed": True},
    "Norow": {},
}


@pytest.mark.parametrize("direction, priced", [
    ("asc", ["Cheap", "Middle", "Pricey"]),
    ("desc", ["Pricey", "Middle", "Cheap"]),
])
def test_sort_by_ebay_sold_puts_no_value_last(client, direction, priced):
    _seed(**CASES)
    html = client.get(f"/inventory?sort=ebay_sold&direction={direction}").text
    order = _names_in_order(html, list(CASES))
    assert order[:3] == priced
    assert set(order[3:]) == {"Thin", "Shaky", "Failed", "Norow"}


def test_sort_works_with_paging(client):
    _seed(**CASES)
    html = client.get("/inventory?sort=ebay_sold&direction=desc&page=1&page_size=2").text
    assert ">Pricey<" in html and ">Middle<" in html
    assert ">Cheap<" not in html and ">Thin<" not in html


def test_rows_are_loaded_in_one_query_per_page(client, monkeypatch):
    _seed(**CASES)
    calls = []
    real = P.rows_by_card

    def spy(db, card_ids):
        ids = list(card_ids)
        calls.append(ids)
        return real(db, ids)

    monkeypatch.setattr(P, "rows_by_card", spy)
    client.get("/inventory?page=1&page_size=3")
    assert len(calls) == 1 and len(calls[0]) == 3


def test_rows_by_card_and_sql_gate_match_shows_value(db_session):
    ids = {}
    for i, (name, kwargs) in enumerate(CASES.items(), start=1):
        ids[name] = _add(db_session, name, i, **kwargs).id
    db_session.commit()

    rows = P.rows_by_card(db_session, ids.values())
    assert set(rows) == {ids[n] for n in ("Cheap", "Pricey", "Middle", "Thin", "Shaky", "Failed")}
    assert all(r.source == P.SOURCE for r in rows.values())
    assert P.rows_by_card(db_session, []) == {}

    shown = dict(
        db_session.query(CardPrice.card_id, P.shown_price_nok_expr()).filter(CardPrice.source == P.SOURCE).all()
    )
    for card_id, row in rows.items():
        expected = row.price_nok if P.shows_value(row) else None
        assert shown[card_id] == expected
