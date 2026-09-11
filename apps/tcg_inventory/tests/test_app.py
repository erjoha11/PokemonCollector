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


def test_inventory_search_filters_results(client):
    main = make_csv(
        "My Collection",
        [{"id": "a", "name": "Pikachu"}, {"id": "b", "name": "Charizard"}],
    )
    client.post("/import", files=[("files", ("main.csv", main, "text/csv"))])

    response = client.get("/inventory?q=Charizard")
    assert "Charizard" in response.text
    assert "1 kort" in response.text
