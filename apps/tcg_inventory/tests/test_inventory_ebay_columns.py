"""The Inventory's eBay reference columns (issue #416): "eBay sold" (the
`poketrace_ebay` 30-day NM median) and "eBay 7d avg" (`poketrace_ebay_7d`),
right of Market price. Cells follow the card page's display gate
(poketrace_prices.shows_value); both sort with no-price and Few-sales rows
last in either direction; the page's rows load in one query. Offline: rows
are written straight into the test DB, no PokeTrace call.
"""
import datetime as dt
import re

import pytest
from sqlalchemy import event

import poketrace_prices as P
import pricing
from models import Card

TODAY = dt.date(2026, 10, 16)
SOURCES = {"ebay_sold": P.SOURCE, "ebay_7d": P.SOURCE_7D}
HTMX = {"HX-Request": "true"}


def _session():
    import db as db_module

    return db_module.SessionLocal()


def seed(client, cards):
    """cards: [(name, {source: dict(price_nok=..., sample_size=..., flags=...) | "failed"})]"""
    with _session() as db:
        for i, (name, prices) in enumerate(cards):
            card = Card(card_id=f"c{i}", name=name, number=str(i + 1), qty=1, created_at=dt.datetime(2026, 1, 1))
            db.add(card)
            db.flush()
            pricing.record_price(card, pricing.SOURCE_DEX, price_nok=10.0, fetched_at=TODAY, currency="NOK", fx_rate=1.0)
            for source, spec in prices.items():
                if spec == "failed":
                    pricing.record_failure(card, source, TODAY)
                    continue
                pricing.record_price(
                    card,
                    source,
                    price_nok=spec["price_nok"],
                    price=spec["price_nok"] / 10,
                    currency="USD",
                    fx_rate=10.0,
                    fetched_at=TODAY,
                    flags=spec.get("flags", ()),
                    sample_size=spec.get("sample_size"),
                    window_days=7 if source == P.SOURCE_7D else 30,
                )
        db.commit()


def cell(html: str, name: str, col: str) -> str:
    """The text of one card's `data-col=col` cell, tags stripped."""
    row = next(r for r in html.split("<tr")[1:] if f">{name}<" in r)
    match = re.search(rf'<td data-col="{col}" class="num">(.*?)</td>', row, re.S)
    return " ".join(re.sub(r"<[^>]+>", " ", match.group(1)).replace("\xa0", " ").split())


def ok(price, n=551, flags=()):
    return {"price_nok": price, "sample_size": n, "flags": flags}


# --------------------------------------------------------------------------
# Header and cells
# --------------------------------------------------------------------------
def test_headers_sit_right_of_market_price_with_tooltips(client):
    seed(client, [("Pikachu", {})])
    html = client.get("/inventory", headers=HTMX).text
    head = html[html.index("<thead>") : html.index("</thead>")]
    cols = re.findall(r'data-col="(\w+)"', head)
    assert cols[cols.index("price") : cols.index("price") + 3] == ["price", "ebay_sold", "ebay_7d"]
    assert 'title="eBay Near Mint median, last 30 days (PokeTrace) · reference only"' in head
    assert 'title="eBay Near Mint average, last 7 days (PokeTrace) · reference only"' in head
    assert "sort=ebay_sold" in head and "sort=ebay_7d" in head
    # In the column chooser too, so either can be hidden.
    assert 'data-col-toggle="ebay_sold"' in html and 'data-col-toggle="ebay_7d"' in html


@pytest.mark.parametrize("col", ["ebay_sold", "ebay_7d"])
def test_cells_priced_few_sales_failed_and_no_row(client, col):
    source = SOURCES[col]
    seed(
        client,
        [
            ("Priced", {source: ok(3427.45, n=551)}),
            ("Thin", {source: ok(500.0, n=2, flags=(pricing.FLAG_FEW_SALES,))}),
            ("Shaky", {source: ok(900.0, n=80, flags=(pricing.FLAG_LOW_CONFIDENCE,))}),
            ("Failed", {source: "failed"}),
            ("Nothing", {}),
        ],
    )
    html = client.get("/inventory", headers=HTMX).text
    window = "7d" if col == "ebay_7d" else "30d"
    assert cell(html, "Priced", col) == f"3 427 kr n=551 · {window}"
    assert cell(html, "Thin", col) == "Few sales"
    assert cell(html, "Shaky", col) == "Few sales"
    assert cell(html, "Failed", col) == "—"
    assert cell(html, "Nothing", col) == "—"


def test_few_sales_follows_the_live_gate_not_the_stored_flag(client, monkeypatch):
    # n=4 with no stored few_sales flag: POKETRACE_MIN_SALES=5 (the default)
    # says Few sales, exactly as the card page does.
    seed(client, [("Four", {P.SOURCE: ok(200.0, n=4)})])
    assert cell(client.get("/inventory", headers=HTMX).text, "Four", "ebay_sold") == "Few sales"
    monkeypatch.setenv("POKETRACE_MIN_SALES", "3")
    assert cell(client.get("/inventory", headers=HTMX).text, "Four", "ebay_sold") == "200 kr n=4 · 30d"


def test_columns_are_independent(client):
    seed(client, [("Both", {P.SOURCE: ok(300.0), P.SOURCE_7D: "failed"})])
    html = client.get("/inventory", headers=HTMX).text
    assert cell(html, "Both", "ebay_sold") == "300 kr n=551 · 30d"
    assert cell(html, "Both", "ebay_7d") == "—"


def test_market_price_and_totals_are_untouched(client):
    seed(client, [("Pricey", {P.SOURCE: ok(9999.0), P.SOURCE_7D: ok(8888.0)})])
    with _session() as db:
        pricing.resolve_cards(db, today=TODAY)
        db.commit()
        card = db.query(Card).one()
        assert (card.market_price, card.market_price_source) == (10.0, pricing.SOURCE_DEX)
    html = client.get("/inventory", headers=HTMX).text
    assert cell(html, "Pricey", "price") == "10 kr"
    assert cell(html, "Pricey", "total_value") == "10 kr"


# --------------------------------------------------------------------------
# Sorting
# --------------------------------------------------------------------------
@pytest.mark.parametrize("col", ["ebay_sold", "ebay_7d"])
def test_sort_both_directions_with_no_price_and_few_sales_last(client, col):
    source = SOURCES[col]
    seed(
        client,
        [
            ("Mid", {source: ok(300.0)}),
            ("Low", {source: ok(100.0)}),
            ("Thin", {source: ok(5000.0, n=2, flags=(pricing.FLAG_FEW_SALES,))}),
            ("Shaky", {source: ok(9000.0, flags=(pricing.FLAG_LOW_CONFIDENCE,))}),
            ("Failed", {source: "failed"}),
            ("Nothing", {}),
            ("High", {source: ok(700.0)}),
        ],
    )
    names = ["Mid", "Low", "Thin", "Shaky", "Failed", "Nothing", "High"]

    def order(direction):
        html = client.get(f"/inventory?sort={col}&direction={direction}", headers=HTMX).text
        return sorted(names, key=lambda n: html.index(f">{n}<"))

    tail = ["Thin", "Shaky", "Failed", "Nothing"]  # all NULL: the usual tie-breakers (number)
    assert order("asc") == ["Low", "Mid", "High", *tail]
    assert order("desc") == ["High", "Mid", "Low", *tail]


def test_sort_ignores_the_other_reference_source(client):
    seed(
        client,
        [
            ("A", {P.SOURCE: ok(100.0), P.SOURCE_7D: ok(900.0)}),
            ("B", {P.SOURCE: ok(200.0), P.SOURCE_7D: ok(50.0)}),
        ],
    )

    def first(col):
        html = client.get(f"/inventory?sort={col}&direction=desc", headers=HTMX).text
        return min(["A", "B"], key=lambda n: html.index(f">{n}<"))

    assert first("ebay_sold") == "B"
    assert first("ebay_7d") == "A"


# --------------------------------------------------------------------------
# One query for the page
# --------------------------------------------------------------------------
def test_the_page_loads_its_ebay_rows_in_one_query(client):
    seed(client, [(f"Card{i}", {P.SOURCE: ok(100.0 + i), P.SOURCE_7D: ok(50.0 + i)}) for i in range(30)])
    import db as db_module

    statements = []

    def count(conn, cursor, statement, *args):
        if "card_prices" in statement and "poketrace" in str(args):
            statements.append(statement)

    event.listen(db_module.engine, "before_cursor_execute", count)
    try:
        html = client.get("/inventory?page_size=0", headers=HTMX).text
    finally:
        event.remove(db_module.engine, "before_cursor_execute", count)
    assert cell(html, "Card29", "ebay_7d") == "79 kr n=551 · 7d"
    assert len(statements) == 1


# --------------------------------------------------------------------------
# Card page: the 7d average is a second Reference row
# --------------------------------------------------------------------------
def test_card_page_shows_both_reference_rows_median_first(client):
    seed(client, [("Both", {P.SOURCE_7D: ok(250.0), P.SOURCE: ok(300.0)})])
    with _session() as db:
        card_pk = db.query(Card).one().id
    html = client.get(f"/cards/{card_pk}").text
    median = html.index("eBay sold via PokeTrace (NM median)")
    avg7d = html.index("eBay 7d avg via PokeTrace (NM)")
    assert median < avg7d
    assert html.count('class="price-source-reference"') == 2
