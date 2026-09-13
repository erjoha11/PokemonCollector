from conftest import make_csv


def test_dashboard_top_collection_ranks_and_shows_unique_value_not_total(client):
    # Duplicated Collection has more raw value (500) once duplicates count,
    # but only 50 kr of *unique* value. Single Card Collection has just one
    # card worth 100 kr unique -- and should win despite its lower total.
    main = make_csv(
        "My Collection",
        [
            {"id": "a", "name": "Onesy", "qty": 1, "price": "100"},
            {"id": "b", "name": "Manysy", "qty": 10, "price": "50"},
        ],
    )
    single = make_csv("Single Card Collection", [{"id": "a"}])
    duped = make_csv("Duplicated Collection", [{"id": "b"}])
    client.post(
        "/import",
        files=[
            ("files", ("main.csv", main, "text/csv")),
            ("files", ("single.csv", single, "text/csv")),
            ("files", ("duped.csv", duped, "text/csv")),
        ],
    )

    dashboard = client.get("/")
    text = dashboard.text
    # Scope to the KPI card itself -- both collection names also appear in
    # the Inventory breakdown table further down the page.
    card = text.split("<h3>Mest verdifulle collection</h3>", 1)[1].split("<h3>", 1)[0]
    assert "Single Card Collection" in card
    assert "Duplicated Collection" not in card  # not picked -- lower unique value
    assert "100 kr" in card  # the unique value shown, not 500 kr (its total_value)


def test_import_page_shows_sync_log_history(client):
    main = make_csv("My Collection", [{"id": "a", "name": "Pikachu", "qty": 2, "price": "150"}])
    client.post("/import", files=[("files", ("main.csv", main, "text/csv"))])

    response = client.get("/import")
    assert response.status_code == 200
    assert "Synk-logg" in response.text
    assert "main.csv" in response.text
    assert "manual" in response.text


def test_all_pages_render(client):
    for path in ["/", "/inventory", "/transactions", "/import", "/added"]:
        response = client.get(path)
        assert response.status_code == 200, path


def test_transactions_page_shows_transaction_id(client):
    import db as db_module
    from models import Card

    main = make_csv("My Collection", [{"id": "a", "name": "Pikachu", "price": "150"}])
    client.post("/import", files=[("files", ("main.csv", main, "text/csv"))])

    db = db_module.SessionLocal()
    pikachu_id = db.query(Card).filter(Card.card_id == "a").one().id
    db.close()

    response = client.post(
        "/transactions",
        data={"card_id": pikachu_id, "type": "kjøp", "date": "2026-01-01", "price": "10"},
        follow_redirects=True,
    )
    assert "Trans ID" in response.text
    assert "<td>1</td>" in response.text  # the first transaction gets id 1


def test_added_page_groups_cards_by_date_added(client):
    main = make_csv("My Collection", [{"id": "a", "name": "Pikachu"}])
    client.post("/import", files=[("files", ("main.csv", main, "text/csv"))])

    response = client.get("/added")
    assert response.status_code == 200
    assert "Pikachu" in response.text
    assert "<h2>Ukjent dato" not in response.text  # freshly imported -- has a known date
    assert "1 av 1" in response.text


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


def test_inventory_card_name_links_to_dex(client):
    main = make_csv("My Collection", [{"id": "ex5-4", "name": "Dark Celebi", "price": "780"}])
    client.post("/import", files=[("files", ("main.csv", main, "text/csv"))])

    inventory = client.get("/inventory")
    assert '<a href="https://app.dextcg.com/cards/ex5-4"' in inventory.text
    assert "target=\"_blank\"" in inventory.text


def test_dashboard_top_cards_link_to_dex(client):
    main = make_csv("My Collection", [{"id": "ex5-4", "name": "Dark Celebi", "price": "780"}])
    client.post("/import", files=[("files", ("main.csv", main, "text/csv"))])

    dashboard = client.get("/")
    assert '<a href="https://app.dextcg.com/cards/ex5-4"' in dashboard.text


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
    # Scope to the results table -- the KPI module above it can also caption
    # a card/series name and would otherwise throw off raw position checks.
    text = response.text.split('id="inventory-results"', 1)[1]
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


def test_dashboard_column_sort_only_reorders_leaf_cards_not_buckets(client):
    main = make_csv(
        "My Collection",
        [
            {"id": "a", "name": "Card A", "qty": 1},
            {"id": "b", "name": "Card B", "qty": 20},
            {"id": "c", "qty": 5},
        ],
    )
    alpha = make_csv("Alpha Collection", [{"id": "a"}, {"id": "b"}])
    zeta = make_csv("Zeta Collection", [{"id": "c"}])
    client.post(
        "/import",
        files=[
            ("files", ("main.csv", main, "text/csv")),
            ("files", ("alpha.csv", alpha, "text/csv")),
            ("files", ("zeta.csv", zeta, "text/csv")),
        ],
    )

    # Scope to the Inventory table itself -- collection names can also appear
    # earlier on the page via the "Mest verdifulle collection" KPI highlight.
    def _inventory_table(html: str) -> str:
        return html.split("<h2>Inventory</h2>", 1)[1]

    default = _inventory_table(client.get("/").text)
    by_qty_desc = _inventory_table(client.get("/?csort=qty&cdir=desc").text)

    # Bucket (collection) order is static -- unaffected by csort.
    assert default.index("Alpha Collection") < default.index("Zeta Collection")
    assert by_qty_desc.index("Alpha Collection") < by_qty_desc.index("Zeta Collection")

    # The cards *inside* Alpha Collection do reorder by qty.
    assert default.index("Card A") < default.index("Card B")
    assert by_qty_desc.index("Card B") < by_qty_desc.index("Card A")


def test_inventory_search_filters_results(client):
    main = make_csv(
        "My Collection",
        [{"id": "a", "name": "Pikachu"}, {"id": "b", "name": "Charizard"}],
    )
    client.post("/import", files=[("files", ("main.csv", main, "text/csv"))])

    response = client.get("/inventory?q=Charizard")
    assert "Charizard" in response.text
    assert "1 kort" in response.text
