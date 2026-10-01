"""Issue #228 (a): boundary validation of type/price/date/parallel-list form
inputs, and the one 422 error pattern (plain-text message for htmx, a
re-rendered page with every value re-filled for the plain listing forms)."""

import pytest

from conftest import make_csv, seed_import

HX = {"HX-Request": "true"}
BAD_PRICES = ["nan", "inf", "-inf", "-1", "abc"]


def _seed(client):
    import db as db_module
    from models import Card

    main = make_csv(
        "My Collection",
        [{"id": "a", "name": "Pikachu", "price": "10"}, {"id": "b", "name": "Charizard", "price": "50"}],
    )
    seed_import(client, [("files", ("main.csv", main, "text/csv"))])
    db = db_module.SessionLocal()
    try:
        return {c.card_id: c.id for c in db.query(Card).all()}
    finally:
        db.close()


def _tx_snapshot():
    import db as db_module
    from models import Transaction

    db = db_module.SessionLocal()
    try:
        return sorted(
            (t.id, t.card_id, t.type, t.date, t.price, t.fees, t.purchase_id, t.purchase_total, t.purchase_shipping)
            for t in db.query(Transaction).all()
        )
    finally:
        db.close()


def _order(client, ids, purchase_id=5):
    for cid, price in ((ids["a"], "10"), (ids["b"], "20")):
        r = client.post(
            "/transactions",
            data={"card_id": cid, "type": "purchase", "date": "2026-01-01", "price": price, "purchase_id": str(purchase_id)},
        )
        assert r.status_code == 200
    import db as db_module
    from models import Transaction

    db = db_module.SessionLocal()
    try:
        return [t.id for t in db.query(Transaction).order_by(Transaction.price).all()]
    finally:
        db.close()


def _listing(client, ids, suggested_price="30"):
    import db as db_module
    from models import Listing

    client.post(
        "/sales/mark-listed",
        data={"card_id": [str(c) for c in ids], "title": "Pikachu", "description": "Selger.", "suggested_price": suggested_price},
    )
    db = db_module.SessionLocal()
    try:
        return db.query(Listing).order_by(Listing.id.desc()).first().id
    finally:
        db.close()


def _assert_plain_422(response, *needles):
    assert response.status_code == 422, response.text
    assert response.headers["content-type"].startswith("text/plain")
    for needle in needles:
        assert needle in response.text


# --- Unit: the parsers ----------------------------------------------------


def test_parse_amount_accepts_decimal_comma_and_rejects_non_finite_and_negative():
    from form_validation import FormError, parse_amount, parse_optional_amount

    assert parse_amount("1,5") == 1.5
    assert parse_amount("0") == 0.0
    assert parse_optional_amount("  ", "Fees") is None
    for bad in BAD_PRICES + ["1e999"]:
        with pytest.raises(FormError):
            parse_amount(bad)
    with pytest.raises(FormError, match="Price on row 3"):
        parse_amount("nan", row=3)
    with pytest.raises(FormError, match="required"):
        parse_amount("")


def test_parse_tx_type_and_date():
    from form_validation import FormError, parse_date, parse_tx_type

    assert parse_tx_type("ripped") == "ripped"
    with pytest.raises(FormError, match="Type"):
        parse_tx_type("purchse")
    with pytest.raises(FormError, match="Date"):
        parse_date("2026-13-01")


def test_transaction_types_match_what_the_rest_of_the_app_handles():
    from models import TRANSACTION_TYPES

    assert TRANSACTION_TYPES == ("purchase", "sale", "trade", "ripped")


# --- New Order cart: POST /transactions/purchase -------------------------


def _cart(ids, **overrides):
    data = {
        "type": "purchase",
        "date": "2026-01-01",
        "purchase_id": "9",
        "card_id": [str(ids["a"]), str(ids["b"])],
        "price": ["10", "20"],
    }
    data.update(overrides)
    return data


@pytest.mark.parametrize("bad", BAD_PRICES)
@pytest.mark.parametrize("field", ["price", "purchase_total", "purchase_shipping", "fees"])
def test_cart_rejects_bad_amounts(client, field, bad):
    ids = _seed(client)
    value = ["10", bad] if field == "price" else bad
    r = client.post("/transactions/purchase", data=_cart(ids, **{field: value}), headers=HX)
    label = {"price": "Price on row 2", "purchase_total": "Total", "purchase_shipping": "Shipping", "fees": "Fees"}[field]
    _assert_plain_422(r, label)
    assert _tx_snapshot() == []


def test_cart_rejects_unknown_type_malformed_date_and_mismatched_lists(client):
    ids = _seed(client)
    _assert_plain_422(client.post("/transactions/purchase", data=_cart(ids, type="purchse"), headers=HX), "Type")
    _assert_plain_422(client.post("/transactions/purchase", data=_cart(ids, date="01.01.2026"), headers=HX), "Date")
    _assert_plain_422(client.post("/transactions/purchase", data=_cart(ids, price=["10"]), headers=HX), "don't line up")
    assert _tx_snapshot() == []


def test_cart_still_registers_a_valid_order(client):
    ids = _seed(client)
    r = client.post("/transactions/purchase", data=_cart(ids, fees="3", purchase_shipping="0"), headers=HX)
    assert r.status_code == 200
    rows = _tx_snapshot()
    assert [row[4] for row in rows] == [10.0, 20.0]
    assert sum(row[5] for row in rows) == pytest.approx(3.0)


# --- Order history total/shipping form -----------------------------------


@pytest.mark.parametrize("bad", BAD_PRICES)
@pytest.mark.parametrize("field", ["purchase_total", "purchase_shipping"])
def test_order_total_form_rejects_bad_amounts(client, field, bad):
    ids = _seed(client)
    _order(client, ids)
    before = _tx_snapshot()
    r = client.post("/transactions/purchase/5/total", data={field: bad}, headers=HX)
    _assert_plain_422(r, "Total" if field == "purchase_total" else "Shipping")
    assert _tx_snapshot() == before


# --- Edit order: POST /transactions/purchase/{id}/edit -------------------


def _edit(ids, tx_ids, **overrides):
    data = {
        "tx_id": [str(t) for t in tx_ids],
        "type": ["purchase", "purchase"],
        "date": ["2026-01-01", "2026-01-01"],
        "price": ["10", "20"],
        "platform": ["", ""],
        "note": ["", ""],
        "card_id": [str(ids["a"]), str(ids["b"])],
        "new_purchase_id": ["5", "5"],
        "purchase_total": "",
        "purchase_shipping": "",
    }
    data.update(overrides)
    return data


@pytest.mark.parametrize("bad", BAD_PRICES)
def test_edit_order_rejects_bad_prices_without_writing_any_row(client, bad):
    ids = _seed(client)
    tx_ids = _order(client, ids)
    before = _tx_snapshot()
    # Row 1 is valid and changed -- it must not be written either.
    r = client.post("/transactions/purchase/5/edit", data=_edit(ids, tx_ids, price=["99", bad]), headers=HX)
    _assert_plain_422(r, "Price on row 2")
    for field, label in (("purchase_total", "Total"), ("purchase_shipping", "Shipping")):
        r = client.post("/transactions/purchase/5/edit", data=_edit(ids, tx_ids, **{field: bad}), headers=HX)
        _assert_plain_422(r, label)
    assert _tx_snapshot() == before


def test_edit_order_rejects_unknown_type_malformed_date_and_mismatched_lists(client):
    ids = _seed(client)
    tx_ids = _order(client, ids)
    before = _tx_snapshot()
    post = lambda **kw: client.post("/transactions/purchase/5/edit", data=_edit(ids, tx_ids, **kw), headers=HX)  # noqa: E731
    _assert_plain_422(post(type=["purchase", "sold"]), "Type on row 2")
    _assert_plain_422(post(date=["2026-02-30", "2026-01-01"]), "Date on row 1")
    _assert_plain_422(post(price=["10"]), "don't line up")
    _assert_plain_422(post(new_purchase_id=["5"]), "don't line up")
    assert _tx_snapshot() == before


def test_edit_order_deleting_a_mistyped_row_is_not_blocked_by_the_typo(client):
    ids = _seed(client)
    tx_ids = _order(client, ids)
    r = client.post(
        "/transactions/purchase/5/edit",
        data=_edit(ids, tx_ids, price=["10", "nan"], delete_tx_id=[str(tx_ids[1])]),
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert [row[0] for row in _tx_snapshot()] == [tx_ids[0]]


# --- Single transaction create / row edit --------------------------------


@pytest.mark.parametrize("bad", BAD_PRICES)
def test_create_transaction_rejects_bad_price_and_fees(client, bad):
    ids = _seed(client)
    base = {"card_id": ids["a"], "type": "purchase", "date": "2026-01-01", "price": "10"}
    _assert_plain_422(client.post("/transactions", data={**base, "price": bad}, headers=HX), "Price")
    _assert_plain_422(client.post("/transactions", data={**base, "fees": bad}, headers=HX), "Fees")
    assert _tx_snapshot() == []


def test_create_transaction_rejects_unknown_type_and_malformed_date(client):
    ids = _seed(client)
    base = {"card_id": ids["a"], "type": "purchase", "date": "2026-01-01", "price": "10"}
    _assert_plain_422(client.post("/transactions", data={**base, "type": "gift"}, headers=HX), "Type")
    _assert_plain_422(client.post("/transactions", data={**base, "date": "yesterday"}, headers=HX), "Date")
    assert _tx_snapshot() == []


@pytest.mark.parametrize("bad", BAD_PRICES)
def test_row_edit_rejects_bad_price_and_fees(client, bad):
    ids = _seed(client)
    tx_ids = _order(client, ids)
    before = _tx_snapshot()
    base = {"date": "2026-01-01", "type": "purchase", "price": "10", "purchase_id": "5"}
    _assert_plain_422(client.post(f"/transactions/{tx_ids[0]}", data={**base, "price": bad}, headers=HX), "Price")
    _assert_plain_422(client.post(f"/transactions/{tx_ids[0]}", data={**base, "fees": bad}, headers=HX), "Fees")
    assert _tx_snapshot() == before


def test_row_edit_rejects_unknown_type_and_malformed_date(client):
    ids = _seed(client)
    tx_ids = _order(client, ids)
    before = _tx_snapshot()
    base = {"date": "2026-01-01", "type": "purchase", "price": "10", "purchase_id": "5"}
    _assert_plain_422(client.post(f"/transactions/{tx_ids[0]}", data={**base, "type": "Purchase "}, headers=HX), "Type")
    _assert_plain_422(client.post(f"/transactions/{tx_ids[0]}", data={**base, "date": ""}, headers=HX), "Date")
    assert _tx_snapshot() == before


# --- FastAPI's own validation errors are plain text for htmx -------------


def test_unparseable_int_field_is_plain_text_for_htmx_and_json_otherwise(client):
    ids = _seed(client)
    data = _cart(ids, purchase_id="abc")
    _assert_plain_422(client.post("/transactions/purchase", data=data, headers=HX), "Order ID", "whole number")
    plain = client.post("/transactions/purchase", data=data)
    assert plain.status_code == 422
    assert plain.headers["content-type"].startswith("application/json")


# --- Sales: ad builder and Mark as listed (htmx) -------------------------


@pytest.mark.parametrize("bad", BAD_PRICES)
def test_sales_generate_rejects_bad_asking_price(client, bad):
    ids = _seed(client)
    data = {"card_id": [str(ids["a"])], "qty": ["1"], "condition": [""], "price": [bad]}
    _assert_plain_422(client.post("/sales/generate", data=data, headers=HX), "Asking price on row 1")


def test_sales_generate_rejects_mismatched_lists_and_allows_blank_price(client):
    ids = _seed(client)
    data = {"card_id": [str(ids["a"]), str(ids["b"])], "qty": ["1"], "condition": ["", ""], "price": ["", ""]}
    _assert_plain_422(client.post("/sales/generate", data=data, headers=HX), "don't line up")
    data["qty"] = ["1", "1"]
    assert client.post("/sales/generate", data=data, headers=HX).status_code == 200


@pytest.mark.parametrize("bad", BAD_PRICES)
def test_mark_listed_rejects_bad_suggested_price(client, bad):
    from models import Listing
    import db as db_module

    ids = _seed(client)
    data = {"card_id": [str(ids["a"])], "title": "t", "description": "d", "suggested_price": bad}
    _assert_plain_422(client.post("/sales/mark-listed", data=data, headers=HX), "Suggested price")
    db = db_module.SessionLocal()
    try:
        assert db.query(Listing).count() == 0
    finally:
        db.close()


# --- Plain-form listing pages: 422 + re-rendered with every value ---------


@pytest.mark.parametrize("bad", BAD_PRICES)
def test_listing_edit_rejects_bad_price_and_refills_the_form(client, bad):
    import db as db_module
    from models import Listing

    ids = _seed(client)
    listing_id = _listing(client, [ids["a"], ids["b"]])
    r = client.post(
        f"/listings/{listing_id}/edit",
        data={"title": "Typed title", "description": "Typed description", "suggested_price": bad,
              "card_id": [str(ids["a"]), str(ids["b"])]},
    )
    assert r.status_code == 422
    assert 'role="alert"' in r.text and "Suggested price" in r.text
    assert 'value="Typed title"' in r.text and "Typed description" in r.text
    assert f'value="{bad}"' in r.text
    assert "Pikachu" in r.text and "Charizard" in r.text
    db = db_module.SessionLocal()
    try:
        listing = db.query(Listing).one()
        assert listing.title == "Pikachu" and listing.suggested_price == 30.0
    finally:
        db.close()


@pytest.mark.parametrize("bad", ["nan", "inf", "-1", "0"])
def test_mark_sold_rejects_bad_prices_and_refills_every_value(client, bad):
    import db as db_module
    from models import Listing, Transaction

    ids = _seed(client)
    listing_id = _listing(client, [ids["a"], ids["b"]])
    r = client.post(
        f"/listings/{listing_id}/mark-sold",
        data={"date": "2026-03-04", "platform": "Tradera", "card_id": [str(ids["a"]), str(ids["b"])],
              "price": ["12.5", bad], "fees": "4", "shipping": "7"},
    )
    assert r.status_code == 422
    assert 'role="alert"' in r.text and "Sold price on row 2" in r.text
    for needle in ('value="2026-03-04"', 'value="Tradera"', 'value="12.5"', f'value="{bad}"', 'value="4"', 'value="7"'):
        assert needle in r.text
    db = db_module.SessionLocal()
    try:
        assert db.query(Transaction).count() == 0
        assert db.query(Listing).one().status == "active"
    finally:
        db.close()


@pytest.mark.parametrize("field,bad,label", [("fees", "nan", "Fees"), ("shipping", "inf", "Shipping"), ("date", "2026-02-31", "Date")])
def test_mark_sold_rejects_bad_fees_shipping_and_date(client, field, bad, label):
    import db as db_module
    from models import Transaction

    ids = _seed(client)
    listing_id = _listing(client, [ids["a"]])
    data = {"date": "2026-03-04", "card_id": [str(ids["a"])], "price": ["12"], field: bad}
    r = client.post(f"/listings/{listing_id}/mark-sold", data=data)
    assert r.status_code == 422 and label in r.text
    db = db_module.SessionLocal()
    try:
        assert db.query(Transaction).count() == 0
    finally:
        db.close()


# --- The shared error slot markup ----------------------------------------

SLOT = 'role="alert" hidden data-form-error'


def test_error_slot_is_on_every_htmx_form_that_saves(client):
    ids = _seed(client)
    tx_ids = _order(client, ids)

    base = client.get("/orders/purchased").text
    assert '<script src="/static/form-errors.js" defer></script>' in base
    assert SLOT in base  # order-history total/shipping form

    cart = client.get("/transactions/purchase/start", headers=HX).text
    assert SLOT in cart and cart.index(SLOT) < cart.index(">Register</button>")

    row_edit = client.get(f"/transactions/{tx_ids[0]}/edit").text
    assert SLOT in row_edit and row_edit.index("<form") < row_edit.index(SLOT) < row_edit.index("</form>")

    edit = client.get("/transactions/purchase/5/edit").text
    assert SLOT in edit
    assert "save-order-error" not in edit and "hx-on::response-error" not in edit

    sales = client.get(f"/sales?card_ids={ids['a']}").text
    assert SLOT in sales


def test_form_errors_js_and_cart_guard_reset_exist(client):
    js = client.get("/static/form-errors.js").text
    assert "htmx:responseError" in js and "htmx:beforeRequest" in js
    assert "[data-form-error]" in js and "xhr.status === 422" in js
    cart_js = client.get("/static/orders-cart.js").text
    assert "leavingDeliberately = false; });" in cart_js
