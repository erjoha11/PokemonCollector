"""The card page as an in-page modal (issue #280): GET /cards/{id}/panel,
the data-card-modal hook on card links/photos, the photo zoom that
replaced the #card-viewer lightbox, and delete-missing's cardDeleted
HX-Trigger."""
import datetime as dt
import json
import re
from pathlib import Path

from conftest import make_csv, seed_import

from models import Card, CardSnapshot

APP_DIR = Path(__file__).resolve().parent.parent


def _session():
    import db as db_module

    return db_module.SessionLocal()


def _seed(client):
    main = make_csv(
        "My Collection",
        [
            {"id": "a", "name": "Pikachu", "qty": 2, "price": "100", "set": "Base Set", "number": "58/102"},
            {"id": "b", "name": "Charizard", "qty": 1, "price": "900", "set": "Base Set", "number": "4/102"},
        ],
    )
    vintage = make_csv("Vintage Collection", [{"id": "a"}, {"id": "b"}])
    seed_import(
        client,
        [("files", ("main.csv", main, "text/csv")), ("files", ("vintage.csv", vintage, "text/csv"))],
    )
    with _session() as db:
        pika = db.query(Card).filter_by(name="Pikachu").one()
        pika.image_url = "https://img.example/pikachu.png"
        db.add(CardSnapshot(card_id=pika.id, date=dt.date(2026, 9, 1), source="cron", qty=2, reference_price=90))
        db.add(CardSnapshot(card_id=pika.id, date=dt.date(2026, 9, 2), source="cron", qty=2, reference_price=100))
        db.commit()
        return {c.name: c.id for c in db.query(Card).all()}


def _flagged_card():
    with _session() as db:
        card = Card(
            card_id="jpn_x", name="Wrongly Registered", qty=1, set="Test Set", number="001/100",
            variant="Normal", flagged_missing_since=dt.date(2026, 10, 1),
        )
        db.add(card)
        db.commit()
        return card.id


# --- /cards/{id}/panel -------------------------------------------------------


def test_panel_returns_the_body_without_an_html_shell(client):
    ids = _seed(client)
    resp = client.get(f"/cards/{ids['Pikachu']}/panel")
    assert resp.status_code == 200
    html = resp.text
    assert "<html" not in html and "<nav" not in html and "<body" not in html
    # Modal header: labelled title, set line, full-page link and close X.
    assert 'id="card-modal-title"' in html and "Pikachu" in html and "#58/102" in html
    assert "Base Set" in html
    assert f'href="/cards/{ids["Pikachu"]}">Open full page' in html
    assert "data-card-modal-close" in html
    # The same body as the full page: KPIs, collections, transactions, chart.
    assert 'class="card-detail"' in html and "Vintage Collection" in html
    assert "No transactions registered" in html
    assert 'id="card-price-history-data"' in html
    # Chart init is guarded (the panel's page may not load Chart.js).
    assert 'window.initTcgChart && initTcgChart("card-price-history")' in html
    # The photo zooms in place -- no second dialog.
    assert "data-card-zoom" in html and "data-card-modal " not in html


def test_panel_404s_for_unknown_card(client):
    assert client.get("/cards/99999/panel").status_code == 404


def test_full_page_and_panel_share_the_body(client):
    ids = _seed(client)
    page = client.get(f"/cards/{ids['Pikachu']}").text
    panel = client.get(f"/cards/{ids['Pikachu']}/panel").text
    assert "<html" in page and 'class="muted breadcrumb"' in page
    assert 'id="card-modal-title"' not in page  # the page has its own <h1>
    for marker in ('class="card-detail"', 'class="card-detail-kpis"', 'id="card-price-history"', "data-card-zoom"):
        assert marker in page and marker in panel
    # Tables scroll horizontally rather than overflowing the modal.
    assert '<div class="table-scroll">' in panel


def test_card_page_photo_zooms_in_place_and_the_viewer_is_gone(client):
    ids = _seed(client)
    page = client.get(f"/cards/{ids['Pikachu']}").text
    assert 'data-card-zoom aria-expanded="false"' in page
    assert "data-card-view" not in page and 'id="card-viewer"' not in page
    assert 'id="card-modal"' in page and 'aria-labelledby="card-modal-title"' in page
    assert "/static/card-modal.js" in page and "card-viewer.js" not in page
    assert not (APP_DIR / "static" / "card-viewer.js").exists()
    assert not (APP_DIR / "templates" / "partials" / "card_viewer.html").exists()


# --- the data-card-modal hook ------------------------------------------------


def test_card_link_emits_data_card_modal(client):
    ids = _seed(client)
    for path in ("/inventory", "/"):
        html = client.get(path).text
        assert f'<a href="/cards/{ids["Charizard"]}" data-card-modal>Charizard</a>' in html


def test_card_photo_opens_the_modal_with_placeholder_data(client):
    ids = _seed(client)
    with _session() as db:
        coll_id = db.get(Card, ids["Pikachu"]).collections[0].id
    html = client.get(f"/collections/{coll_id}").text
    # Photo button: modal hook + placeholder data; name via card_link.
    assert f'data-card-modal data-href="/cards/{ids["Pikachu"]}" data-name="Pikachu"' in html
    assert 'data-img="' in html and "data-price=" in html
    assert f'<a href="/cards/{ids["Pikachu"]}" data-card-modal class="gallery-card-name">Pikachu</a>' in html


def test_missing_from_dex_links_use_card_link_not_delete_route(client):
    pk = _flagged_card()
    html = client.get("/sync-status").text
    assert f'<a href="/cards/{pk}" data-card-modal>Wrongly Registered</a>' in html
    # The delete form's action is never a modal hook.
    assert f'hx-post="/cards/{pk}/delete-missing"' in html
    assert "data-missing-count" in html


def test_no_hand_written_card_links_left_in_templates():
    """Every /cards/{id} link goes through card_link (or card_view_attrs),
    so it carries data-card-modal. The panel's own "Open full page" link is
    the one deliberate exception."""
    offenders = []
    for path in (APP_DIR / "templates").rglob("*.html"):
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            hand_written = re.search(r'(?<![-\w])href="/cards/\{\{', line)
            if hand_written and "macro card_link" not in line and "card-modal-full" not in line:
                offenders.append(f"{path.name}:{n}")
    assert offenders == []


# --- delete-missing inside the modal -----------------------------------------


def test_panel_delete_form_posts_in_panel_and_has_error_slot(client):
    pk = _flagged_card()
    html = client.get(f"/cards/{pk}/panel").text
    assert 'id="card-delete-missing"' in html
    assert 'name="in_panel" value="1"' in html
    assert "data-form-error" in html
    # The full page's form doesn't claim to be in the panel, but has the slot.
    page = client.get(f"/cards/{pk}").text
    assert 'name="in_panel"' not in page and "data-form-error" in page


def test_delete_missing_return_to_card_sets_hx_trigger(client):
    pk = _flagged_card()
    resp = client.post(f"/cards/{pk}/delete-missing", data={"confirm": "1", "return_to": "card"})
    assert resp.status_code == 200
    assert json.loads(resp.headers["HX-Trigger"]) == {"cardDeleted": {"id": pk}}
    # Full page keeps its "Back to Sync status" link.
    assert 'href="/sync-status"' in resp.text


def test_delete_missing_in_panel_says_card_deleted_with_close(client):
    pk = _flagged_card()
    resp = client.post(
        f"/cards/{pk}/delete-missing", data={"confirm": "1", "return_to": "card", "in_panel": "1"}
    )
    assert resp.status_code == 200
    assert "Card deleted." in resp.text and "data-card-modal-close" in resp.text
    assert 'href="/sync-status"' not in resp.text
    assert "cardDeleted" in resp.headers["HX-Trigger"]


def test_refused_or_sync_status_delete_sends_no_trigger(client):
    pk = _flagged_card()
    refused = client.post(f"/cards/{pk}/delete-missing", data={"return_to": "card"})
    assert "HX-Trigger" not in refused.headers
    done = client.post(f"/cards/{pk}/delete-missing", data={"confirm": "1", "return_to": "sync-status"})
    assert "HX-Trigger" not in done.headers


def test_card_modal_script_guards():
    """Cheap static checks on behaviour that only a browser can run."""
    js = (APP_DIR / "static" / "card-modal.js").read_text(encoding="utf-8")
    assert "AbortController" in js
    assert "event.button === 0" in js and "ctrlKey" in js and "metaKey" in js
    assert "pushState" not in js.replace("No pushState", "")
    assert '"/login"' in js and "resp.redirected" in js
    assert "htmx.process" in js
    assert "cardDeleted" in js
    assert 'a[href^="/cards/"]' not in js
