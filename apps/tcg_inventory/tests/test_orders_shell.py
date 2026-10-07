"""Orders page shell (issue #255): one "Orders" nav item with route-based
tabs /orders/purchased, /orders/sold, /orders/listings; 308 redirects from
the old URLs; one function (app.order_tab) deciding which tab an order is
on, used by both the tab lists and every post-write redirect."""
import datetime as dt

import pytest

from conftest import make_csv, seed_import


def _seed(client, rows=None):
    """Import cards and return {dex id: Card.id}."""
    import db as db_module
    from models import Card

    rows = rows or [
        {"id": "a", "name": "Pikachu", "price": "100"},
        {"id": "b", "name": "Charizard", "price": "50"},
        {"id": "c", "name": "Blastoise", "price": "20"},
    ]
    seed_import(client, [("files", ("main.csv", make_csv("My Collection", rows), "text/csv"))])
    db = db_module.SessionLocal()
    ids = {c.card_id: c.id for c in db.query(Card).all()}
    db.close()
    return ids


def _tx(card_id, purchase_id, price=10, type="purchase", **kw):
    from models import Transaction

    return Transaction(
        card_id=card_id, type=type, date=dt.date(2026, 1, 1), price=price, purchase_id=purchase_id, **kw
    )


def _add(*txs):
    import db as db_module

    db = db_module.SessionLocal()
    db.add_all(list(txs))
    db.commit()
    db.close()


def _types_in_order(purchase_id):
    import db as db_module
    from models import Transaction

    db = db_module.SessionLocal()
    try:
        return sorted(t for (t,) in db.query(Transaction.type).filter(Transaction.purchase_id == purchase_id))
    finally:
        db.close()


HTMX_FROM = lambda tab: {"HX-Request": "true", "HX-Current-URL": f"http://testserver/orders/{tab}"}  # noqa: E731


# --- Tab membership -------------------------------------------------------


@pytest.mark.parametrize(
    "types, tab",
    [
        (["sale"], "sold"),
        (["sale", "sale"], "sold"),
        (["purchase"], "purchased"),
        (["trade"], "purchased"),
        (["ripped"], "purchased"),
        (["sale", "purchase"], "purchased"),  # mixed stays on Purchased
        (["sale", "trade"], "purchased"),
        ([], "purchased"),
    ],
)
def test_order_tab_is_sold_only_when_every_row_is_a_sale(types, tab):
    from app import order_tab

    assert order_tab(types) == tab


def test_mixed_order_is_listed_on_purchased_and_not_on_sold(client):
    ids = _seed(client)
    _add(_tx(ids["a"], 1), _tx(ids["b"], 1, type="sale"), _tx(ids["c"], 2, type="sale"))

    purchased = client.get("/orders/purchased").text
    sold = client.get("/orders/sold").text
    assert 'id="order-1"' in purchased and 'id="order-1"' not in sold
    assert 'id="order-2"' in sold and 'id="order-2"' not in purchased


# --- Redirects from the old URLs -----------------------------------------


@pytest.mark.parametrize(
    "old, new",
    [
        ("/transactions", "/orders/purchased"),
        ("/transactions?open_order=5", "/orders/purchased?open_order=5"),
        ("/transactions?open_order=5&tsort=price&tdir=asc&pick=all", "/orders/purchased?open_order=5&tsort=price&tdir=asc&pick=all"),
        ("/listings", "/orders/listings"),
        ("/listings?sold_only=true", "/orders/listings?sold_only=true"),
        ("/listings?show_delisted=true&sold_only=true", "/orders/listings?show_delisted=true&sold_only=true"),
        ("/analyse", "/orders/purchased"),
        ("/transactions/charts?metric=unique&period=1y", "/orders/charts?metric=unique&period=1y"),
        ("/orders", "/orders/purchased"),
    ],
)
def test_old_urls_308_to_the_orders_tabs_keeping_the_query_string(client, old, new):
    response = client.get(old, follow_redirects=False)
    assert response.status_code == 308
    assert response.headers["location"] == new


def test_old_transactions_deep_link_still_opens_the_order(client):
    ids = _seed(client)
    _add(_tx(ids["a"], 4))
    response = client.get("/transactions?open_order=4")
    assert str(response.url).endswith("/orders/purchased?open_order=4")
    assert 'id="order-4" open' in response.text


# --- Deep links and tab markup -------------------------------------------


def test_each_tab_deep_links_and_marks_itself_active(client):
    ids = _seed(client)
    _add(_tx(ids["a"], 1), _tx(ids["b"], 2, type="sale"))

    purchased = client.get("/orders/purchased?open_order=1").text
    assert 'id="order-1" open' in purchased
    assert "<title>Orders · Purchased – TCG Inventory</title>" in purchased
    assert '<nav class="tabs" aria-label="Orders">' in purchased
    assert 'href="/orders/purchased" class="tab active" aria-current="page">Purchased</a>' in purchased
    assert 'role="tablist"' not in purchased

    sold = client.get("/orders/sold?open_order=2").text
    assert 'id="order-2" open' in sold
    assert "<title>Orders · Sold – TCG Inventory</title>" in sold
    assert 'href="/orders/sold" class="tab active" aria-current="page">Sold</a>' in sold

    listings = client.get("/orders/listings").text
    assert "<title>Orders · Listings – TCG Inventory</title>" in listings
    assert 'href="/orders/listings" class="tab active" aria-current="page">Listings</a>' in listings


def test_tab_strip_is_inside_main_content(client):
    text = client.get("/orders/sold").text
    main = text.split('<main id="main-content">', 1)[1]
    assert '<nav class="tabs" aria-label="Orders">' in main


@pytest.mark.parametrize(
    "path", ["/orders/purchased", "/orders/sold", "/orders/listings", "/sales", "/transactions/purchase/1/edit"]
)
def test_single_orders_nav_item_is_active_across_the_orders_area(client, path):
    ids = _seed(client)
    _add(_tx(ids["a"], 1))
    text = client.get(path).text
    assert 'href="/orders/purchased" class="active">Orders</a>' in text
    for gone in ('>Transactions</a>', '>Sell on finn.no</a>', 'href="/listings"'):
        assert gone not in text.split("</nav>", 1)[0]


def test_orders_nav_item_is_not_active_elsewhere(client):
    text = client.get("/inventory").text
    assert 'href="/orders/purchased" class="">Orders</a>' in text


def test_sort_links_stay_on_the_current_tab(client):
    ids = _seed(client)
    _add(_tx(ids["a"], None, type="sale"))  # an individually registered sale -> sortable table on Sold
    sold = client.get("/orders/sold").text
    assert 'href="/orders/sold?tsort=date' in sold
    assert 'href="/transactions?' not in sold
    purchased = client.get("/orders/purchased").text
    assert 'href="/orders/purchased?tsort=' in purchased
    assert 'href="/orders/purchased?gsort=' in purchased
    assert 'href="/orders/purchased?pick=all"' in purchased
    assert 'href="/transactions?' not in purchased


# --- Post-write redirects ------------------------------------------------


def test_total_form_redirects_to_each_orders_own_tab(client):
    ids = _seed(client)
    _add(_tx(ids["a"], 1), _tx(ids["b"], 2, type="sale"))

    r = client.post("/transactions/purchase/1/total", data={"purchase_total": "50"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/orders/purchased?open_order=1"
    r = client.post("/transactions/purchase/2/total", data={"purchase_total": "50"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/orders/sold?open_order=2"


def test_total_form_on_a_sale_order_swaps_its_own_details_back_in(client):
    # The form hx-selects #order-N out of the redirect target: if the target
    # tab didn't list the order, the swap would empty it out.
    ids = _seed(client)
    _add(_tx(ids["b"], 7, type="sale"))
    r = client.post("/transactions/purchase/7/total", data={"purchase_total": "45"}, headers=HTMX_FROM("sold"))
    assert r.status_code == 200
    assert str(r.url).endswith("/orders/sold?open_order=7")
    assert 'id="order-7" open' in r.text


def test_cart_register_redirects_by_type(client):
    ids = _seed(client)
    base = {"date": "2026-02-01", "card_id": [str(ids["a"])], "price": ["30"]}

    r = client.post("/transactions/purchase", data={**base, "type": "purchase", "purchase_id": "1"}, follow_redirects=False)
    assert r.headers["location"] == "/orders/purchased?open_order=1"
    r = client.post("/transactions/purchase", data={**base, "type": "sale", "purchase_id": "2"}, follow_redirects=False)
    assert r.headers["location"] == "/orders/sold?open_order=2"
    r = client.post("/transactions/purchase", data={**base, "type": "ripped", "purchase_id": "3"}, follow_redirects=False)
    assert r.headers["location"] == "/orders/purchased?open_order=3"


def test_htmx_write_landing_on_another_tab_answers_with_hx_redirect(client):
    ids = _seed(client)
    data = {"date": "2026-02-01", "card_id": [str(ids["a"])], "price": ["30"], "type": "sale", "purchase_id": "1"}

    # Registered from Purchased, but it's a sale -> navigate, don't swap.
    r = client.post("/transactions/purchase", data=data, headers=HTMX_FROM("purchased"), follow_redirects=False)
    assert r.status_code == 200
    assert r.headers["HX-Redirect"] == "/orders/sold?open_order=1"

    # Same tab -> the usual 303 that htmx follows and hx-selects from.
    data["purchase_id"] = "2"
    r = client.post("/transactions/purchase", data=data, headers=HTMX_FROM("sold"), follow_redirects=False)
    assert r.status_code == 303
    assert "HX-Redirect" not in r.headers
    assert r.headers["location"] == "/orders/sold?open_order=2"


def test_create_transaction_redirects_to_its_tab(client):
    ids = _seed(client)
    data = {"card_id": str(ids["a"]), "date": "2026-02-01", "price": "40"}
    r = client.post("/transactions", data={**data, "type": "sale"}, follow_redirects=False)
    assert r.headers["location"] == "/orders/sold"
    r = client.post("/transactions", data={**data, "type": "purchase", "purchase_id": "9"}, follow_redirects=False)
    assert r.headers["location"] == "/orders/purchased?open_order=9"


def test_edit_order_back_link_points_at_the_orders_tab(client):
    ids = _seed(client)
    _add(_tx(ids["a"], 3, type="sale"))
    text = client.get("/transactions/purchase/3/edit").text
    assert 'href="/orders/sold?open_order=3"' in text
    assert "Back to Sold" in text
    assert "/transactions?open_order" not in text


def test_edit_order_that_empties_the_order_returns_to_the_current_tab(client):
    ids = _seed(client)
    _add(_tx(ids["a"], 3, type="sale"))
    import db as db_module
    from models import Transaction

    db = db_module.SessionLocal()
    tx_id = db.query(Transaction).one().id
    db.close()
    r = client.post(
        "/transactions/purchase/3/edit",
        data={"tx_id": [str(tx_id)], "delete_tx_id": [str(tx_id)]},
        headers=HTMX_FROM("sold"),
        follow_redirects=False,
    )
    assert r.headers["location"] == "/orders/sold"


def test_mark_sold_lands_on_the_new_sale_order_and_cancel_keeps_filters(client):
    import db as db_module
    from models import Listing, Transaction

    ids = _seed(client)
    client.post(
        "/sales/mark-listed",
        data={"card_id": [str(ids["a"])], "title": "Pikachu", "description": "Selger", "suggested_price": "90"},
    )
    db = db_module.SessionLocal()
    listing_id = db.query(Listing).one().id
    db.close()

    form = client.get(f"/listings/{listing_id}/mark-sold?sold_only=true&show_delisted=true").text
    assert 'href="/orders/listings?show_delisted=true&amp;sold_only=true"' in form

    r = client.post(
        f"/listings/{listing_id}/mark-sold",
        data={"date": "2026-03-01", "card_id": [str(ids["a"])], "price": ["90"]},
        follow_redirects=False,
    )
    db = db_module.SessionLocal()
    pid = db.query(Transaction).one().purchase_id
    db.close()
    assert r.status_code == 303
    assert r.headers["location"] == f"/orders/sold?open_order={pid}#order-{pid}"

    # Already sold: back to the Listings tab, filters intact.
    r = client.get(f"/listings/{listing_id}/mark-sold?sold_only=true", follow_redirects=False)
    assert r.headers["location"] == "/orders/listings?sold_only=true"


def test_listing_entry_mark_sold_link_carries_the_filters(client):
    ids = _seed(client)
    client.post(
        "/sales/mark-listed",
        data={"card_id": [str(ids["a"])], "title": "Pikachu", "description": "Selger", "suggested_price": "90"},
    )
    text = client.get("/orders/listings?show_delisted=true").text
    assert "/mark-sold?show_delisted=true" in text


def test_non_htmx_delist_and_delete_return_to_the_filtered_listings_tab(client):
    import db as db_module
    from models import Listing

    ids = _seed(client)
    for _ in range(2):
        client.post(
            "/sales/mark-listed",
            data={"card_id": [str(ids["a"])], "title": "Pikachu", "description": "Selger", "suggested_price": "90"},
        )
    db = db_module.SessionLocal()
    first, second = [lst.id for lst in db.query(Listing).order_by(Listing.id)]
    db.close()

    r = client.post(f"/listings/{first}/delist", data={"sold_only": "true"}, follow_redirects=False)
    assert r.headers["location"] == "/orders/listings?sold_only=true"
    r = client.post(f"/listings/{second}/delete", data={"show_delisted": "true"}, follow_redirects=False)
    assert r.headers["location"] == "/orders/listings?show_delisted=true"


# --- add-existing-cards guard (sale orders) -------------------------------


def test_add_existing_cards_rejects_a_sale_order(client):
    ids = _seed(client)
    _add(_tx(ids["a"], 5, type="sale"))
    r = client.post(
        "/transactions/purchase/add-existing-cards",
        data={"card_id": [str(ids["b"])], "purchase_id": "5"},
        follow_redirects=False,
    )
    assert r.status_code == 200
    assert "Order #5 is a sale order" in r.text
    assert _types_in_order(5) == ["sale"]  # no purchase row slipped in


def test_card_picker_only_offers_purchased_tab_orders(client):
    ids = _seed(client)
    _add(_tx(ids["a"], 1), _tx(ids["b"], 2, type="sale"))
    picker = client.get("/orders/purchased").text.split('id="picker-target"', 1)[1].split("</select>", 1)[0]
    assert '<option value="1">' in picker
    assert '<option value="2">' not in picker


# --- Purchased tab --------------------------------------------------------


def test_purchased_type_pills_count_filter_and_fall_back(client):
    ids = _seed(client)
    _add(
        _tx(ids["a"], 1),
        _tx(ids["b"], 2, type="trade", direction="in"),
        _tx(ids["c"], 3, type="ripped", price=0),
        _tx(ids["c"], 4, type="sale"),  # Sold tab -- not counted here
    )
    all_page = client.get("/orders/purchased").text
    assert ">All (3)</a>" in all_page
    assert ">Purchases (1)</a>" in all_page
    assert ">Trades (1)</a>" in all_page
    assert ">Ripped (1)</a>" in all_page

    trades = client.get("/orders/purchased?type=trade&tsort=price").text
    assert 'id="order-2"' in trades and 'id="order-1"' not in trades and 'id="order-3"' not in trades
    assert 'class="viz-filter-pill active" href="/orders/purchased?tsort=price&amp;type=trade" aria-current="true">Trades (1)</a>' in trades
    # Every pill keeps the other params; All drops type= rather than spelling it out.
    assert 'href="/orders/purchased?tsort=price"' in trades

    unknown = client.get("/orders/purchased?type=bogus")
    assert unknown.status_code == 200
    assert 'id="order-1"' in unknown.text and 'id="order-2"' in unknown.text


def test_purchased_type_filter_with_no_matches_offers_show_all(client):
    ids = _seed(client)
    _add(_tx(ids["a"], 1))
    text = client.get("/orders/purchased?type=trade").text
    assert "No trade orders." in text
    assert '<a href="/orders/purchased">Show all</a>' in text


def test_purchased_header_and_cart(client):
    ids = _seed(client)
    _add(_tx(ids["a"], 1))
    text = client.get("/orders/purchased").text
    assert "paid for cards (shipping included), minus sales received" in text
    assert '<span class="oc-gain num">Paper gain</span>' in text
    assert "sale orders show" not in text
    assert "View charts" in text and 'hx-get="/orders/charts"' in text

    cart = client.get("/transactions/purchase/start").text
    assert '<option value="purchase" selected>' in cart
    assert '<option value="trade"' in cart and '<option value="ripped"' in cart
    assert '<option value="sale"' not in cart
    assert "Show cards without an order" in cart


# --- Sold tab ---------------------------------------------------------------


def test_sold_tab_header_and_contents(client):
    ids = _seed(client)
    _add(
        _tx(ids["a"], 1, price=100, type="sale", fees=10),
        _tx(ids["b"], 1, price=50, type="sale"),
        _tx(ids["c"], 2, price=30, type="sale", purchase_shipping=6),
        _tx(ids["c"], 3, price=5),
    )
    text = client.get("/orders/sold").text
    head = text.split('class="section-summary"', 1)[1].split('<a class="button"', 1)[0]
    assert "Sold for" in head and "180 kr" in head
    # #254's net proceeds: 180 - 10 fees - 6 seller-paid shipping.
    assert "Fees &amp; shipping" in head and "16 kr" in head
    assert "Net received" in head and "164 kr" in head
    assert "2 sales / 3 cards" in text
    assert "Card quantities update at the next Dex sync." in text
    assert "Realized gain" in head  # #256; never Paper gain on Sold
    assert "Paper gain" not in text
    assert '<span class="tx-kpi-label">Net invested' not in text  # no Purchased header here
    assert 'id="card-picker"' not in text
    assert "+ Add cards to this order" not in text
    assert '<a class="button" href="/sales">Sell on finn.no</a>' in text
    assert "+ Record sale without listing" in text and 'hx-get="/transactions/purchase/start?type=sale"' in text
    assert "Received (auto" in text  # the sale order's Total is what was received
    assert "Individually registered" not in text  # hidden while empty
    assert 'id="order-3"' not in text


def test_sold_tab_empty_state(client):
    text = client.get("/orders/sold").text
    assert "No sales yet." in text
    assert "+ Record sale without listing" in text


def test_sold_cart_has_hidden_type_and_no_unordered_browse(client):
    cart = client.get("/transactions/purchase/start?type=sale").text
    assert '<input type="hidden" name="type" value="sale">' in cart
    assert 'name="type" onchange' not in cart
    assert "Show cards without an order" not in cart
    js = client.get("/static/orders-cart.js").text
    assert "form.elements.type" in js
    assert "querySelector('select[name=\"type\"]')" not in js


# --- Listings tab -----------------------------------------------------------


def test_listings_fragment_only_for_the_results_target(client):
    full = client.get("/orders/listings", headers={"HX-Request": "true"}).text
    assert 'id="main-content"' in full  # an hx-select="#main-content" swap still finds it
    fragment = client.get(
        "/orders/listings", headers={"HX-Request": "true", "HX-Target": "listings-results"}
    ).text
    assert fragment.lstrip().startswith('<div id="listings-results">')
    assert 'id="main-content"' not in fragment


def test_listings_tab_head(client):
    ids = _seed(client)
    client.post(
        "/sales/mark-listed",
        data={"card_id": [str(ids["a"])], "title": "Pikachu", "description": "Selger", "suggested_price": "90"},
    )
    text = client.get("/orders/listings").text
    assert '<a class="button" href="/sales">Sell on finn.no</a>' in text
    assert "1 active" in text
    assert 'hx-get="/orders/listings"' in text
    assert "Sold for" not in text and "Net invested" not in text


# --- Single-row edit moving a row to the other tab ---------------------------


def test_single_row_type_edit_says_where_the_row_moved(client):
    import db as db_module
    from models import Transaction

    ids = _seed(client)
    _add(_tx(ids["a"], None))
    db = db_module.SessionLocal()
    tx_id = db.query(Transaction).one().id
    db.close()

    data = {"date": "2026-01-01", "type": "sale", "price": "10"}
    moved = client.post(f"/transactions/{tx_id}", data=data, headers=HTMX_FROM("purchased")).text
    assert 'Moved to <a href="/orders/sold">Sold</a>' in moved

    stayed = client.post(f"/transactions/{tx_id}", data=data, headers=HTMX_FROM("sold")).text
    assert "Moved to" not in stayed
