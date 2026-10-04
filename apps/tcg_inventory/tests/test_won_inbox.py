"""The Facebook wins inbox (#309): POST /inbox/fb-wins, won_items, and the
Purchased tab's "Facebook wins to register" list with Ignore."""
from __future__ import annotations

import copy
import datetime as dt
import json

import pytest

import auth
import db as db_module
import won_inbox
from models import Transaction, WonItem

TOKEN = "test-inbox-token-0123456789"
POST = "https://www.facebook.com/groups/somegroup/posts/1001/"


def _item(n: int, **over) -> dict:
    item = {
        "external_ref": f"fbaw:1001:{2000 + n}",
        "seller": "Seller One",
        "sale_type": "auction",
        "ended_on": "2026-10-03",
        "post_url": POST,
        "lot_url": f"{POST}?comment_id={2000 + n}",
        "label": f"{n}. Card {n}",
        "price": 100 + n,
        "shipping_text": "50 kr tracked",
        "payment_text": "Vipps",
        "paid_at": None,
        "received_at": None,
    }
    item.update(over)
    return item


def _payload(items: list[dict], **over) -> dict:
    data = {"format": "fbaw-won", "version": 1, "sent_at": "2026-10-04T10:00:00.000Z", "items": items}
    data.update(over)
    return data


def _send(client, data, token: str | None = TOKEN, **kwargs):
    headers = {"Authorization": f"Bearer {token}"} if token is not None else {}
    body = data if isinstance(data, (bytes, str)) else json.dumps(data)
    return client.post("/inbox/fb-wins", content=body, headers={"Content-Type": "application/json", **headers}, **kwargs)


def _rows() -> list[WonItem]:
    with db_module.SessionLocal() as s:
        rows = s.query(WonItem).order_by(WonItem.external_ref).all()
        s.expunge_all()
        return rows


@pytest.fixture()
def token(monkeypatch):
    monkeypatch.setenv("INBOX_TOKEN", TOKEN)


@pytest.fixture()
def configured_auth(monkeypatch):
    monkeypatch.setattr(auth, "SUPABASE_URL", "https://x.supabase.co")
    monkeypatch.setattr(auth, "SUPABASE_ANON_KEY", "anon")


# ── Writing ───────────────────────────────────────────────────────────────


def test_send_creates_pending_rows(client, token):
    r = _send(client, _payload([_item(1), _item(2, price=None)]))
    assert r.status_code == 200, r.text
    assert r.json() == {"status": "ok", "received": 2, "added": 2, "updated": 0, "unchanged": 0, "kept": 0}
    rows = _rows()
    assert [x.status for x in rows] == ["pending", "pending"]
    assert rows[0].source == "fbaw"
    assert rows[0].ended_on == dt.date(2026, 10, 3)
    assert rows[0].price == 101
    assert rows[1].price is None
    assert rows[0].first_seen_at == rows[0].last_seen_at


def test_resend_is_idempotent(client, token):
    data = _payload([_item(1), _item(2)])
    _send(client, data)
    before = _rows()
    r = _send(client, data)
    assert r.json()["added"] == 0 and r.json()["unchanged"] == 2
    after = _rows()
    assert len(after) == 2
    assert [x.id for x in after] == [x.id for x in before]
    assert [x.first_seen_at for x in after] == [x.first_seen_at for x in before]


def test_resend_refreshes_pending_rows(client, token):
    _send(client, _payload([_item(1, price=None, paid_at=None)]))
    r = _send(client, _payload([_item(1, price=250, paid_at="2026-10-04T08:00:00Z")]))
    assert r.json()["updated"] == 1
    (row,) = _rows()
    assert row.price == 250
    assert row.paid_at == dt.datetime(2026, 10, 4, 8, 0)


@pytest.mark.parametrize("status", ["registered", "ignored"])
def test_registered_and_ignored_rows_never_change(client, token, status):
    _send(client, _payload([_item(1)]))
    with db_module.SessionLocal() as s:
        row = s.query(WonItem).one()
        row.status = status
        row.purchase_id = 7 if status == "registered" else None
        s.commit()
    (before,) = _rows()
    r = _send(client, _payload([_item(1, price=999, label="changed", seller="Someone else")]))
    assert r.json()["kept"] == 1 and r.json()["updated"] == 0
    (after,) = _rows()
    for f in ("status", "price", "label", "seller", "purchase_id", "last_seen_at"):
        assert getattr(after, f) == getattr(before, f), f


def test_never_writes_transactions(client, token):
    _send(client, _payload([_item(1), _item(2)]))
    with db_module.SessionLocal() as s:
        assert s.query(Transaction).count() == 0


# ── Refusals: each writes nothing ────────────────────────────────────────


@pytest.mark.parametrize("given", [None, "wrong-token", ""])
def test_missing_or_wrong_token_is_refused(client, token, given):
    r = _send(client, _payload([_item(1)]), token=given)
    assert r.status_code == 401
    assert _rows() == []


def test_query_string_secret_is_not_accepted(client, token):
    r = client.post(f"/inbox/fb-wins?secret={TOKEN}&token={TOKEN}", json=_payload([_item(1)]))
    assert r.status_code == 401
    assert _rows() == []


def test_token_required_with_auth_configured_too(client, token, configured_auth):
    assert _send(client, _payload([_item(1)]), token="nope").status_code == 401
    # Not redirected to /login: the route skips the session login.
    r = _send(client, _payload([_item(1)]), follow_redirects=False)
    assert r.status_code == 200, r.text
    assert len(_rows()) == 1


def test_token_unset_with_auth_configured_refuses(client, configured_auth):
    r = _send(client, _payload([_item(1)]), token=None, follow_redirects=False)
    assert r.status_code == 503
    assert "INBOX_TOKEN" in r.json()["error"]
    assert _rows() == []


def test_token_unset_without_auth_is_open_for_local_dev(client):
    r = _send(client, _payload([_item(1)]), token=None)
    assert r.status_code == 200, r.text
    assert len(_rows()) == 1


def test_token_set_locally_is_still_required(client, token):
    # Local (no auth configured) but a token set: it's required.
    assert _send(client, _payload([_item(1)]), token=None).status_code == 401
    assert _rows() == []


def test_unknown_major_version_is_refused_readably(client, token):
    r = _send(client, _payload([_item(1)], version=2))
    assert r.status_code == 422
    assert "version 2" in r.json()["error"] and "version 1" in r.json()["error"]
    assert _rows() == []


def test_oversized_body_is_refused(client, token):
    big = _payload([_item(1, shipping_text="x" * 1000)])
    body = json.dumps(big) + " " * (won_inbox.MAX_BODY_BYTES + 1)
    r = _send(client, body)
    assert r.status_code == 413
    assert _rows() == []


def test_not_json_is_refused(client, token):
    r = _send(client, b"{not json")
    assert r.status_code == 400
    assert _rows() == []


def test_one_bad_item_writes_nothing(client, token):
    r = _send(client, _payload([_item(1), _item(2, lot_url="javascript:alert(1)")]))
    assert r.status_code == 422
    assert "lot_url" in r.json()["error"]
    assert _rows() == []


# ── parse_payload ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "mutate, needle",
    [
        (lambda d: d.update(format="other"), "format"),
        (lambda d: d.update(version="1"), "version"),
        (lambda d: d.update(items={}), "items"),
        (lambda d: d["items"][0].pop("external_ref"), "external_ref"),
        (lambda d: d["items"][0].update(external_ref="other:1"), "external_ref"),
        (lambda d: d["items"][0].update(sale_type="raffle"), "sale_type"),
        (lambda d: d["items"][0].update(post_url="http://www.facebook.com/x"), "post_url"),
        (lambda d: d["items"][0].update(post_url="https://evil.example/x"), "post_url"),
        (lambda d: d["items"][0].update(price=-1), "price"),
        (lambda d: d["items"][0].update(price="100"), "price"),
        (lambda d: d["items"][0].update(price=True), "price"),
        (lambda d: d["items"][0].update(ended_on="03.10.2026"), "ended_on"),
        (lambda d: d["items"][0].update(paid_at="yesterday"), "paid_at"),
        (lambda d: d["items"][0].update(label=""), "label"),
        (lambda d: d["items"].append(copy.deepcopy(d["items"][0])), "more than once"),
    ],
)
def test_parse_payload_rejections(mutate, needle):
    data = _payload([_item(1)])
    mutate(data)
    with pytest.raises(won_inbox.PayloadError, match=needle):
        won_inbox.parse_payload(data)


def test_parse_payload_ignores_unknown_fields_and_allows_nulls():
    data = _payload([_item(1, seller=None, ended_on=None, price=None, shipping_text=None, extra="x")], future="y")
    (item,) = won_inbox.parse_payload(data)
    assert item.seller is None and item.ended_on is None and item.price is None


def test_too_many_items():
    with pytest.raises(won_inbox.PayloadError, match="Too many"):
        won_inbox.parse_payload(_payload([_item(i) for i in range(won_inbox.MAX_ITEMS + 1)]))


# ── The Purchased tab's list and Ignore ───────────────────────────────────


def test_purchased_tab_lists_pending_wins_per_sale(client, token):
    other_post = "https://www.facebook.com/groups/somegroup/posts/1002/"
    _send(
        client,
        _payload(
            [
                _item(1, price=100),
                _item(2, price=None),
                # A second sale from the same seller: its own entry (one order per sale).
                _item(3, external_ref="fbaw:1002:pos1", post_url=other_post, lot_url=other_post, ended_on="2026-10-01", price=40),
            ]
        ),
    )
    html = client.get("/orders/purchased").text
    assert "Facebook wins to register" in html
    assert html.count('class="fb-win-sale"') == 2
    assert "Seller One" in html
    assert "2 items" in html and "1 item<" in html
    assert "+ ?" in html
    assert f'href="{POST}?comment_id=2001"' in html
    assert "ended 2026-10-03" in html and "ended 2026-10-01" in html
    # Newest sale first.
    assert html.index("ended 2026-10-03") < html.index("ended 2026-10-01")


def test_no_list_when_nothing_pending(client):
    html = client.get("/orders/purchased").text
    assert "Facebook wins to register" not in html


def test_ignore_hides_item_and_survives_resend(client, token):
    data = _payload([_item(1), _item(2)])
    _send(client, data)
    first = _rows()[0]
    r = client.post(f"/orders/fb-wins/{first.id}/ignore", headers={"HX-Request": "true"})
    assert r.status_code == 200
    assert first.label not in r.text and _item(2)["label"] in r.text
    _send(client, data)
    rows = {x.external_ref: x.status for x in _rows()}
    assert rows == {first.external_ref: "ignored", _item(2)["external_ref"]: "pending"}
    assert first.label not in client.get("/orders/purchased").text


def test_ignore_without_htmx_redirects(client, token):
    _send(client, _payload([_item(1)]))
    (row,) = _rows()
    r = client.post(f"/orders/fb-wins/{row.id}/ignore", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"].startswith("/orders/purchased")
    assert _rows()[0].status == "ignored"


def test_ignore_leaves_registered_items_alone(client, token):
    _send(client, _payload([_item(1)]))
    with db_module.SessionLocal() as s:
        row = s.query(WonItem).one()
        row.status = "registered"
        s.commit()
        item_id = row.id
    client.post(f"/orders/fb-wins/{item_id}/ignore", follow_redirects=False)
    assert _rows()[0].status == "registered"


def test_ignore_needs_login_when_auth_configured(client, token, configured_auth):
    r = client.post("/orders/fb-wins/1/ignore", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"
