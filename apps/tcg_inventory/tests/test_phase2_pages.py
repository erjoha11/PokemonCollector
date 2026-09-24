"""Phase 2: card detail page, collection pages, Inventory's Bulk/duplicates
filters, pagination and column chooser."""
import datetime as dt

from conftest import make_csv, seed_import

import queries
from models import Card, CardSnapshot, Collection, Set


def _seed(client, n_bulk=0):
    main = make_csv(
        "My Collection",
        [
            {"id": "a", "name": "Pikachu", "qty": 2, "price": "100", "set": "Base Set", "series": "Original", "number": "58/102"},
            {"id": "b", "name": "Charizard", "qty": 1, "price": "900", "set": "Base Set", "series": "Original", "number": "4/102"},
            {"id": "c", "name": "Bulbasaur", "qty": 1, "price": "10", "set": "Jungle", "series": "Original", "number": "1/64"},
        ]
        + [{"id": f"bulk{i}", "name": f"Bulk{i:03d}", "qty": 1, "price": "1"} for i in range(n_bulk)],
    )
    vintage = make_csv("Vintage Collection", [{"id": "a"}, {"id": "b"}])
    komiya = make_csv("Tomokazu Komiya Collection", [{"id": "a"}])
    seed_import(
        client,
        [
            ("files", ("main.csv", main, "text/csv")),
            ("files", ("vintage.csv", vintage, "text/csv")),
            ("files", ("komiya.csv", komiya, "text/csv")),
        ],
    )


def _session():
    import db as db_module

    return db_module.SessionLocal()


def _card(db, name):
    return db.query(Card).filter_by(name=name).one()


# --------------------------------------------------------------------------
# /cards/{id}
# --------------------------------------------------------------------------
def test_card_page_shows_collections_binder_transactions_and_price_history(client):
    _seed(client)
    db = _session()
    pikachu = _card(db, "Pikachu")
    pk = pikachu.id
    db.add(CardSnapshot(card_id=pk, date=dt.date(2026, 9, 1), source="cron", qty=2, reference_price=80))
    db.add(CardSnapshot(card_id=pk, date=dt.date(2026, 9, 2), source="cron", qty=2, reference_price=90))
    db.add(CardSnapshot(card_id=pk, date=dt.date(2026, 9, 2), source="manual", qty=2, reference_price=95))
    db.commit()
    db.close()
    client.post("/transactions", data={"card_id": pk, "type": "purchase", "date": "2026-01-01", "price": "150"})

    resp = client.get(f"/cards/{pk}")
    assert resp.status_code == 200
    html = resp.text
    assert "Pikachu" in html and "58/102" in html
    assert "Vintage Collection" in html and "Tomokazu Komiya Collection" in html
    assert 'href="/collections/' in html
    assert "Purchase" in html and "01.01.2026" in html
    # Gain = total value (2 x 100) - 150
    assert "+50 kr" in html
    # One point per day, the later source winning: 80, then 95 (manual beats cron).
    assert "card-price-history" in html
    assert "02.09.2026" in html and "95 kr" in html


def test_card_price_history_is_one_point_per_day(db_session):
    card = Card(card_id="x", name="X", variant="Normal", qty=1)
    db_session.add(card)
    db_session.flush()
    for date, source, price in [
        (dt.date(2026, 9, 2), "manual", 12),
        (dt.date(2026, 9, 1), "cron", 10),
        (dt.date(2026, 9, 2), "cron", 11),
        (dt.date(2026, 9, 2), "price-cron", 11.5),
    ]:
        db_session.add(CardSnapshot(card_id=card.id, date=date, source=source, qty=1, reference_price=price))
    db_session.commit()

    history = queries.card_price_history(db_session, card.id)
    assert [(h["date"].day, h["price"]) for h in history] == [(1, 10), (2, 12)]


def test_card_page_404s_for_unknown_card(client):
    assert client.get("/cards/99999").status_code == 404


def test_card_page_without_transactions_shows_no_gain(client):
    _seed(client)
    db = _session()
    pk = _card(db, "Bulbasaur").id
    db.close()
    html = client.get(f"/cards/{pk}").text
    assert "No transactions registered" in html
    assert "None (Bulk)" in html


# --------------------------------------------------------------------------
# /collections, /collections/{id}
# --------------------------------------------------------------------------
def test_collection_page_gallery_value_duplicates_and_completion(client):
    _seed(client)
    db = _session()
    base = db.query(Set).filter_by(name="Base Set").one()
    base.total_cards = 102
    vintage_id = db.query(Collection).filter_by(name="Vintage Collection").one().id
    db.commit()
    db.close()

    resp = client.get(f"/collections/{vintage_id}")
    assert resp.status_code == 200
    html = resp.text
    assert "card-gallery" in html
    assert "Pikachu" in html and "Charizard" in html and "Bulbasaur" not in html
    assert "1 000 kr" in html or "1,000 kr" in html or "1000 kr" in html  # unique value 100 + 900
    assert "×2" in html  # Pikachu's duplicate
    # 2 of Base Set's 102 numbers
    assert "2% complete" in html
    # Pikachu is also in Komiya.
    assert "shared" in html


def test_collection_detail_completion_counts_numbers_per_set(db_session):
    base = Set(series="Original", name="Base Set", total_cards=10)
    jungle = Set(series="Original", name="Jungle", total_cards=None)
    db_session.add_all([base, jungle])
    coll = Collection(name="Vintage Collection", priority_rank=2)
    db_session.add(coll)
    db_session.flush()
    for cid, number, s, variant in [
        ("b1", "1/10", base, "Normal"),
        ("b1", "1/10", base, "Reverse Holo"),
        ("b2", "2/10", base, "Normal"),
        ("j1", "1/64", jungle, "Normal"),
    ]:
        c = Card(card_id=cid, name=cid, variant=variant, qty=1, number=number, set=s.name, series=s.series, set_id=s.id)
        c.collections = [coll]
        db_session.add(c)
    db_session.commit()

    detail = queries.collection_detail(db_session, coll.id)
    assert detail["bucket"].unique_count == 4
    assert detail["completion"] == {"pct": 20.0, "owned": 2, "total": 10, "sets_known": 1, "sets": 2}
    assert queries.collection_detail(db_session, 999) is None


def test_collections_index_lists_membership_and_bulk(client):
    _seed(client)
    html = client.get("/collections").text
    assert "Vintage Collection" in html and "Tomokazu Komiya Collection" in html
    assert "/inventory?collection=__none__" in html
    assert "Total" in html


def test_collection_page_404s(client):
    assert client.get("/collections/99999").status_code == 404


# --------------------------------------------------------------------------
# Inventory
# --------------------------------------------------------------------------
def test_inventory_bulk_filter_shows_only_cards_without_a_collection(client):
    _seed(client)
    html = client.get("/inventory?collection=__none__").text
    assert "Bulbasaur" in html
    assert "Pikachu" not in html and "Charizard" not in html
    assert "Bulk / no collection" in html


def test_inventory_duplicates_only_checkbox(client):
    _seed(client)
    page = client.get("/inventory").text
    assert 'name="dup" value="1"' in page and "Duplicates only" in page
    html = client.get("/inventory?dup=1").text
    assert "Pikachu" in html and "Charizard" not in html


def test_inventory_paginates_at_100_and_can_show_all(client):
    _seed(client, n_bulk=150)  # 153 owned cards
    first = client.get("/inventory?sort=name&direction=asc").text
    assert "153 cards" in first and "showing 1–100" in first
    assert first.count('class="sale-select"') == 100

    second = client.get("/inventory?sort=name&direction=asc&page=2").text
    assert second.count('class="sale-select"') == 53
    assert "showing 101–153" in second

    everything = client.get("/inventory?sort=name&direction=asc&page_size=0").text
    assert everything.count('class="sale-select"') == 153

    # A page past the end clamps to the last page rather than showing nothing.
    past = client.get("/inventory?page=9").text
    assert past.count('class="sale-select"') == 53


def test_inventory_sort_links_drop_the_page(client):
    _seed(client, n_bulk=150)
    html = client.get("/inventory?page=2").text
    header = html[html.index("<thead>") : html.index("</thead>")]
    assert "page=2" not in header


def test_inventory_hides_empty_sparse_columns_and_has_a_column_chooser(client):
    _seed(client)
    html = client.get("/inventory").text
    assert "<th data-col=\"classification\">" not in html
    assert "<th data-col=\"location\">" not in html
    assert 'data-col-toggle="rarity"' in html
    assert "/static/inventory-columns.js" in html

    db = _session()
    _card(db, "Pikachu").location = "Shelf 2"
    db.commit()
    db.close()
    html = client.get("/inventory").text
    assert "<th data-col=\"location\">" in html and "Shelf 2" in html
    assert "<th data-col=\"classification\">" not in html


def test_inventory_gain_column_is_total_value_minus_net_paid(client):
    _seed(client)
    db = _session()
    pk = _card(db, "Pikachu").id
    db.close()
    client.post("/transactions", data={"card_id": pk, "type": "purchase", "date": "2026-01-01", "price": "150"})
    html = client.get("/inventory").text
    row = html[html.index(f'data-card-id="{pk}"') :]
    row = row[: row.index("</tr>")]
    assert '<td data-col="gain" class="num">50 kr</td>' in row  # 2 x 100 - 150, not 100 - 150


def test_bucket_reports_value_of_cards_without_purchase_price(client):
    _seed(client)
    db = _session()
    pk = _card(db, "Pikachu").id
    vintage_id = db.query(Collection).filter_by(name="Vintage Collection").one().id
    db.close()
    client.post("/transactions", data={"card_id": pk, "type": "purchase", "date": "2026-01-01", "price": "150"})

    html = client.get(f"/collections/{vintage_id}").text
    # Charizard (900) has no purchase: its whole value is in the gain.
    assert "incl. 900 kr from 1 card with no purchase price" in html
    dashboard = client.get("/").text
    assert "with no purchase price (full value counted as gain)" in dashboard
