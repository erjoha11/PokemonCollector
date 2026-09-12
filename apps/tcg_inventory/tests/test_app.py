from conftest import make_csv


def test_all_pages_render(client):
    for path in ["/", "/inventory", "/transactions", "/import"]:
        response = client.get(path)
        assert response.status_code == 200, path


def test_import_then_dashboard_reflects_the_sync(client):
    main = make_csv("My Collection", [{"id": "a", "name": "Pikachu", "qty": 2, "price": "150"}])
    response = client.post("/import", files=[("files", ("main.csv", main, "text/csv"))])
    assert response.status_code == 200
    assert "Kort opprettet</td><td>1" in response.text

    dashboard = client.get("/")
    assert dashboard.status_code == 200
    assert "150 kr" in dashboard.text  # reference price shows up in top-10 valuable cards
    inventory = client.get("/inventory")
    assert "Pikachu" in inventory.text


def test_inventory_default_sort_is_release_order_with_numeric_tiebreak(client):
    import db as db_module
    from models import SetReleaseOrder

    main = make_csv(
        "My Collection",
        [
            {"id": "new1", "name": "NewCard", "series": "Scarlet & Violet", "set": "151", "number": "1/165"},
            {"id": "old10", "name": "OldCard10", "series": "Original", "set": "Base Set", "number": "10/102"},
            {"id": "old2", "name": "OldCard2", "series": "Original", "set": "Base Set", "number": "2/102"},
        ],
    )
    client.post("/import", files=[("files", ("main.csv", main, "text/csv"))])

    db = db_module.SessionLocal()
    db.add(SetReleaseOrder(series="Original", set="Base Set", release_rank=1))
    db.add(SetReleaseOrder(series="Scarlet & Violet", set="151", release_rank=50))
    db.commit()
    db.close()

    response = client.get("/inventory")
    text = response.text
    # Base Set (release rank 1) sorts before the newer set (rank 50), and
    # within Base Set, card #2 sorts before #10 (numeric, not alphabetical).
    pos_old2 = text.index("OldCard2")
    pos_old10 = text.index("OldCard10")
    pos_new1 = text.index("NewCard")
    assert pos_old2 < pos_old10 < pos_new1


def test_inventory_dup_filter_shows_only_cards_with_duplicates(client):
    main = make_csv(
        "My Collection",
        [
            {"id": "a", "name": "Pikachu", "qty": 3},
            {"id": "b", "name": "Charizard", "qty": 1},
        ],
    )
    client.post("/import", files=[("files", ("main.csv", main, "text/csv"))])

    response = client.get("/inventory?dup=1")
    assert "Pikachu" in response.text
    assert "Charizard" not in response.text
    assert "1 kort" in response.text


def test_dashboard_totalt_column_links_to_inventory_filtered_by_dup(client):
    from urllib.parse import quote

    main = make_csv("My Collection", [{"id": "a", "name": "Pikachu", "qty": 2}])
    client.post("/import", files=[("files", ("main.csv", main, "text/csv"))])

    dashboard = client.get("/")
    # The "which cards" link lives on Totalt (not Duplikater) -- same filter,
    # different column: /inventory?series=...&dup=1.
    expected_href = f"/inventory?series={quote('Test Series')}&dup=1"
    assert expected_href in dashboard.text


def test_dashboard_series_name_is_the_drilldown_trigger_not_a_link(client):
    main = make_csv("My Collection", [{"id": "a", "series": "Original", "set": "Base Set"}])
    client.post("/import", files=[("files", ("main.csv", main, "text/csv"))])

    dashboard = client.get("/")
    text = dashboard.text
    assert 'class="row-toggle-name"' in text
    assert ">Original</a>" not in text  # no longer a plain link to Inventory
    assert "Base Set" in text  # the nested set row is rendered (hidden until expanded)


def test_dashboard_collection_row_drills_down_to_individual_cards(client):
    main = make_csv("My Collection", [{"id": "a", "name": "Pikachu"}])
    collection = make_csv("My Binder Collection", [{"id": "a"}])
    client.post(
        "/import",
        files=[
            ("files", ("main.csv", main, "text/csv")),
            ("files", ("collection.csv", collection, "text/csv")),
        ],
    )

    dashboard = client.get("/")
    text = dashboard.text
    assert "My Binder Collection" in text
    assert "Pikachu" in text  # the leaf card row, hidden until the collection is expanded
    assert 'data-group="coll-1"' in text


def test_dashboard_rarity_row_drills_down_to_individual_cards(client):
    main = make_csv("My Collection", [{"id": "a", "name": "Pikachu", "rarity": "Rare"}])
    client.post("/import", files=[("files", ("main.csv", main, "text/csv"))])

    dashboard = client.get("/")
    text = dashboard.text
    assert "Rare" in text
    assert "Pikachu" in text
    assert 'data-group="rarity-1"' in text  # the only rarity bucket in this test


def test_dashboard_series_set_row_drills_down_to_individual_cards(client):
    main = make_csv(
        "My Collection", [{"id": "a", "name": "Pikachu", "series": "Original", "set": "Base Set"}]
    )
    client.post("/import", files=[("files", ("main.csv", main, "text/csv"))])

    dashboard = client.get("/")
    text = dashboard.text
    assert 'class="row-toggle-name" data-row-id="series-1-set-1"' in text
    assert 'data-group="series-1-set-1"' in text  # Pikachu's leaf row nests under the set, not the series


def test_dashboard_top_cards_show_card_number(client):
    main = make_csv("My Collection", [{"id": "a", "name": "Pikachu", "number": "58/102", "price": "150"}])
    client.post("/import", files=[("files", ("main.csv", main, "text/csv"))])

    dashboard = client.get("/")
    assert "58/102" in dashboard.text


def test_dashboard_inventory_table_sortable_by_other_columns(client):
    main = make_csv("My Collection", [{"id": "a", "qty": 1}, {"id": "b", "qty": 20}])
    alpha = make_csv("Alpha Collection", [{"id": "a"}])
    zeta = make_csv("Zeta Collection", [{"id": "b"}])
    client.post(
        "/import",
        files=[
            ("files", ("main.csv", main, "text/csv")),
            ("files", ("alpha.csv", alpha, "text/csv")),
            ("files", ("zeta.csv", zeta, "text/csv")),
        ],
    )

    default = client.get("/")
    assert default.text.index("Alpha Collection") < default.text.index("Zeta Collection")

    by_qty_desc = client.get("/?csort=qty&cdir=desc")
    assert by_qty_desc.text.index("Zeta Collection") < by_qty_desc.text.index("Alpha Collection")


def test_inventory_search_filters_results(client):
    main = make_csv(
        "My Collection",
        [{"id": "a", "name": "Pikachu"}, {"id": "b", "name": "Charizard"}],
    )
    client.post("/import", files=[("files", ("main.csv", main, "text/csv"))])

    response = client.get("/inventory?q=Charizard")
    assert "Charizard" in response.text
    assert "1 kort" in response.text
