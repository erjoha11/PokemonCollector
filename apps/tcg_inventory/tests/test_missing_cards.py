"""Tests for "Missing from Dex" on /sync-status and the guarded delete of a
card a Dex sync flagged missing (missing_cards.py, POST
/cards/{id}/delete-missing)."""
import datetime as dt
import time

import jwt
import pytest
from sqlalchemy import select

import auth
from models import (
    Card,
    CardPrice,
    CardSnapshot,
    Collection,
    Listing,
    Transaction,
    card_collections,
    listing_cards,
)

FLAG_DATE = dt.date(2026, 10, 1)


def _session():
    import db as db_module

    return db_module.SessionLocal()


def _add_card(db, card_id, name, flagged=True, **extra):
    card = Card(
        card_id=card_id,
        name=name,
        qty=extra.pop("qty", 1),
        set=extra.pop("set", "Test Set"),
        number=extra.pop("number", "001/100"),
        variant=extra.pop("variant", "Normal"),
        flagged_missing_since=FLAG_DATE if flagged else None,
        **extra,
    )
    db.add(card)
    db.flush()
    return card


def _seed_basic():
    """One flagged card with no history (+ collection, snapshot, price),
    one flagged card with an order row, one flagged card in a listing,
    one unflagged card. Returns their ids."""
    with _session() as db:
        coll = Collection(name="Main", priority_rank=1)
        db.add(coll)
        clean = _add_card(db, "jpn_a", "Wrongly Registered", variant="Holo", qty=2)
        clean.collections.append(coll)
        db.add(CardSnapshot(card_id=clean.id, date=FLAG_DATE, qty=2, source="cron"))
        db.add(CardPrice(card_id=clean.id, source="dex", price_nok=10.0))

        with_tx = _add_card(db, "jpn_b", "Has Order")
        db.add(Transaction(card_id=with_tx.id, type="purchase", date=FLAG_DATE, price=5.0))

        in_listing = _add_card(db, "jpn_c", "Is Listed")
        listing = Listing(created_at=dt.datetime(2026, 9, 1), title="t", description="d")
        listing.cards.append(in_listing)
        db.add(listing)

        unflagged = _add_card(db, "jpn_d", "Still In Dex", flagged=False)
        db.commit()
        return {"clean": clean.id, "tx": with_tx.id, "listing": in_listing.id, "unflagged": unflagged.id}


def _exists(card_pk):
    with _session() as db:
        return db.get(Card, card_pk) is not None


# --- list ---------------------------------------------------------------------


def test_sync_status_lists_only_flagged_cards_with_counts(client):
    ids = _seed_basic()
    page = client.get("/sync-status").text

    assert "Missing from Dex" in page
    assert f'id="missing-card-{ids["clean"]}"' in page
    assert f'id="missing-card-{ids["tx"]}"' in page
    assert f'id="missing-card-{ids["listing"]}"' in page
    assert f'id="missing-card-{ids["unflagged"]}"' not in page
    assert "Still In Dex" not in page
    assert f'href="/cards/{ids["clean"]}"' in page
    assert "01.10.2026" in page
    # Only the card without history gets a delete form.
    assert page.count('hx-post="/cards/') == 1
    assert f'hx-post="/cards/{ids["clean"]}/delete-missing"' in page
    assert "Can&#39;t delete: has order rows" in page or "Can't delete: has order rows" in page
    assert "has listings" in page


def test_sync_status_empty_state(client):
    page = client.get("/sync-status").text
    assert "No cards are flagged missing from Dex." in page


# --- delete: success ------------------------------------------------------------


def test_delete_flagged_card_without_history_cascades(client):
    ids = _seed_basic()
    pk = ids["clean"]

    resp = client.post(f"/cards/{pk}/delete-missing", data={"confirm": "1", "return_to": "sync-status"})
    assert resp.status_code == 200
    assert "Deleted Wrongly Registered" in resp.text
    assert 'id="missing-cards"' in resp.text
    assert f'id="missing-card-{pk}"' not in resp.text

    assert not _exists(pk)
    with _session() as db:
        assert db.query(CardSnapshot).filter_by(card_id=pk).count() == 0
        assert db.query(CardPrice).filter_by(card_id=pk).count() == 0
        assert db.execute(select(card_collections).where(card_collections.c.card_id == pk)).first() is None
        # The collection itself and every other card are untouched.
        assert db.query(Collection).count() == 1
        assert db.query(Card).count() == 3


def test_delete_from_card_page_returns_card_block(client):
    ids = _seed_basic()
    pk = ids["clean"]

    detail = client.get(f"/cards/{pk}").text
    assert 'id="card-delete-missing"' in detail
    assert f'hx-post="/cards/{pk}/delete-missing"' in detail

    resp = client.post(f"/cards/{pk}/delete-missing", data={"confirm": "1", "return_to": "card"})
    assert resp.status_code == 200
    assert 'id="card-delete-missing"' in resp.text
    assert "Deleted Wrongly Registered" in resp.text
    assert 'href="/sync-status"' in resp.text
    assert not _exists(pk)


def test_card_page_has_no_delete_form_for_unflagged_or_history_cards(client):
    ids = _seed_basic()
    assert 'id="card-delete-missing"' not in client.get(f"/cards/{ids['unflagged']}").text
    page = client.get(f"/cards/{ids['tx']}").text
    assert "/delete-missing\"" not in page
    assert "would destroy its order history" in page


# --- delete: refusals -------------------------------------------------------------


@pytest.mark.parametrize(
    "which, data, expected",
    [
        ("clean", {"return_to": "sync-status"}, "tick the confirmation box"),
        ("unflagged", {"confirm": "1"}, "is not flagged missing from Dex"),
        ("tx", {"confirm": "1"}, "has 1 order row"),
        ("listing", {"confirm": "1"}, "has 1 listing"),
    ],
)
def test_delete_refused(client, which, data, expected):
    ids = _seed_basic()
    pk = ids[which]

    resp = client.post(f"/cards/{pk}/delete-missing", data=data)
    assert resp.status_code == 200
    assert expected in resp.text
    assert 'class="warnings"' in resp.text
    assert _exists(pk)
    with _session() as db:
        assert db.query(Card).count() == 4
        assert db.query(Transaction).count() == 1
        assert db.execute(select(listing_cards)).first() is not None
        if which == "clean":
            assert db.query(CardSnapshot).count() == 1


def test_delete_refused_on_card_page_keeps_form(client):
    ids = _seed_basic()
    resp = client.post(f"/cards/{ids['clean']}/delete-missing", data={"return_to": "card"})
    assert resp.status_code == 200
    assert "tick the confirmation box" in resp.text
    assert f'hx-post="/cards/{ids["clean"]}/delete-missing"' in resp.text
    assert _exists(ids["clean"])


def test_delete_unknown_card_is_handled(client):
    resp = client.post("/cards/99999/delete-missing", data={"confirm": "1"})
    assert resp.status_code == 200
    assert "no longer exists" in resp.text


# --- auth ---------------------------------------------------------------------


@pytest.fixture()
def configured_auth(monkeypatch):
    monkeypatch.setattr(auth, "SUPABASE_URL", "https://x.supabase.co")
    monkeypatch.setattr(auth, "SUPABASE_ANON_KEY", "anon")
    monkeypatch.setattr(auth, "SUPABASE_JWT_SECRET", "s3cret-enough-for-hs256-tests")


def test_delete_requires_login_when_auth_configured(client, configured_auth):
    ids = _seed_basic()
    resp = client.post(
        f"/cards/{ids['clean']}/delete-missing", data={"confirm": "1"}, follow_redirects=False
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"
    assert _exists(ids["clean"])


def test_delete_works_with_valid_session_when_auth_configured(client, configured_auth):
    ids = _seed_basic()
    token = jwt.encode(
        {"aud": "authenticated", "sub": "user-1", "exp": int(time.time()) + 3600},
        "s3cret-enough-for-hs256-tests",
        algorithm="HS256",
    )
    client.cookies.set(auth.SESSION_COOKIE, token)
    resp = client.post(f"/cards/{ids['clean']}/delete-missing", data={"confirm": "1"})
    assert resp.status_code == 200
    assert not _exists(ids["clean"])
