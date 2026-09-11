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


def test_inventory_search_filters_results(client):
    main = make_csv(
        "My Collection",
        [{"id": "a", "name": "Pikachu"}, {"id": "b", "name": "Charizard"}],
    )
    client.post("/import", files=[("files", ("main.csv", main, "text/csv"))])

    response = client.get("/inventory?q=Charizard")
    assert "Charizard" in response.text
    assert "1 kort" in response.text
