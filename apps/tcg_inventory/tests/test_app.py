import re

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


def test_import_sync_log_table_scrolls_instead_of_widening_the_page(client):
    main = make_csv("My Collection", [{"id": "a", "name": "Pikachu", "qty": 2, "price": "150"}])
    client.post("/import", files=[("files", ("main.csv", main, "text/csv"))])

    response = client.get("/import")
    log_section = response.text.split('id="import-log"', 1)[1]
    assert '<div class="table-scroll">' in log_section


def test_import_log_table_can_be_sorted_by_column(client):
    zebra = make_csv("My Collection", [{"id": "a", "name": "Pikachu"}])
    abra = make_csv("My Collection", [{"id": "a", "name": "Pikachu"}])
    client.post("/import", files=[("files", ("zzz.csv", zebra, "text/csv"))])
    client.post("/import", files=[("files", ("aaa.csv", abra, "text/csv"))])

    def _log_body(html: str) -> str:
        section = html.split('id="import-log"', 1)[1]
        return section.split("<tbody>", 1)[1].split("</tbody>", 1)[0]

    asc = _log_body(client.get("/import?lsort=files&ldir=asc").text)
    assert asc.index("aaa.csv") < asc.index("zzz.csv")

    desc = _log_body(client.get("/import?lsort=files&ldir=desc").text)
    assert desc.index("zzz.csv") < desc.index("aaa.csv")


def test_all_pages_render(client):
    for path in ["/", "/inventory", "/transactions", "/import"]:
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


def test_transactions_can_be_tagged_with_a_shared_purchase_id(client):
    import db as db_module
    from models import Card

    main = make_csv(
        "My Collection",
        [{"id": "a", "name": "Pikachu"}, {"id": "b", "name": "Charizard"}],
    )
    client.post("/import", files=[("files", ("main.csv", main, "text/csv"))])

    db = db_module.SessionLocal()
    ids = {c.card_id: c.id for c in db.query(Card).all()}
    db.close()

    for card_id in (ids["a"], ids["b"]):
        client.post(
            "/transactions",
            data={
                "card_id": card_id,
                "type": "kjøp",
                "date": "2026-01-01",
                "price": "10",
                "purchase_id": "5",
            },
        )

    response = client.get("/transactions")
    assert "Kjøps-ID" in response.text
    assert response.text.count("<td>5</td>") == 2  # both transactions tagged to the same purchase


def test_transactions_page_offers_recently_added_cards_in_a_dropdown(client):
    import datetime as dt

    import db as db_module
    from models import Card

    main = make_csv(
        "My Collection",
        [{"id": "a", "name": "Pikachu"}, {"id": "b", "name": "Charizard"}],
    )
    client.post("/import", files=[("files", ("main.csv", main, "text/csv"))])

    db = db_module.SessionLocal()
    pikachu = db.query(Card).filter(Card.card_id == "a").one()
    pikachu.created_at = dt.datetime(2026, 1, 1)
    charizard = db.query(Card).filter(Card.card_id == "b").one()
    charizard.created_at = None  # simulates a card that predates the created_at column
    db.commit()
    db.close()

    response = client.get("/transactions")
    assert 'id="recent_card_select"' in response.text
    assert "Pikachu" in response.text.split('id="recent_card_select"', 1)[1].split("</select>", 1)[0]
    # Charizard has no created_at -- not a "recently added" card, so it's excluded.
    assert "Charizard" not in response.text.split('id="recent_card_select"', 1)[1].split("</select>", 1)[0]


def test_transactions_page_groups_added_cards_by_date(client):
    main = make_csv("My Collection", [{"id": "a", "name": "Pikachu"}])
    client.post("/import", files=[("files", ("main.csv", main, "text/csv"))])

    response = client.get("/transactions")
    assert response.status_code == 200
    assert "Kort lagt til" in response.text
    assert "Pikachu" in response.text
    assert "1 kort har en kjent dato" in response.text


def test_transactions_page_puts_unknown_date_cards_in_a_collapsed_section(client):
    import datetime as dt

    import db as db_module
    from models import Card

    main = make_csv(
        "My Collection",
        [{"id": "a", "name": "Pikachu"}, {"id": "b", "name": "Charizard"}],
    )
    client.post("/import", files=[("files", ("main.csv", main, "text/csv"))])

    db = db_module.SessionLocal()
    db.query(Card).filter(Card.card_id == "b").update({"created_at": None})
    db.commit()
    db.close()

    response = client.get("/transactions")
    text = response.text
    # Collapsed by default (no `open` attribute) so the old back-catalog
    # doesn't dominate the page -- Pikachu (known date) sits in the always-
    # visible "Kort lagt til" section, Charizard (no date) is tucked away.
    assert "<details class=\"collapsible\">" in text
    assert "Resten av samlingen uten kjent dato (1 kort)" in text
    collapsed_section = text.split("<details class=\"collapsible\">", 1)[1]
    assert "Charizard" in collapsed_section
    assert "Pikachu" not in collapsed_section
    # No inline purchase form for the old back-catalog -- only "Kort lagt
    # til" (the actually-new cards) gets the quick-register button.
    assert "Legg til" not in collapsed_section


def test_transactions_history_table_scrolls_instead_of_widening_the_page(client):
    main = make_csv("My Collection", [{"id": "a", "name": "Pikachu"}])
    client.post("/import", files=[("files", ("main.csv", main, "text/csv"))])

    response = client.get("/transactions")
    assert '<div class="table-scroll">' in response.text


def test_added_cards_section_has_an_inline_form_to_register_a_purchase(client):
    import db as db_module
    from models import Card, Transaction

    main = make_csv("My Collection", [{"id": "a", "name": "Pikachu"}])
    client.post("/import", files=[("files", ("main.csv", main, "text/csv"))])

    db = db_module.SessionLocal()
    pikachu_id = db.query(Card).filter(Card.card_id == "a").one().id
    db.close()

    response = client.get("/transactions")
    added_section = response.text.split("Kort lagt til", 1)[1]
    assert f'value="{pikachu_id}"' in added_section
    assert 'name="price"' in added_section

    # Submitting that inline form is just a normal /transactions POST.
    client.post(
        "/transactions",
        data={"card_id": pikachu_id, "type": "kjøp", "date": "2026-01-01", "price": "25"},
    )
    db = db_module.SessionLocal()
    tx = db.query(Transaction).filter(Transaction.card_id == pikachu_id).one()
    assert tx.price == 25
    db.close()

    # And the "Kort lagt til" row now shows that already-registered price,
    # so a second visit doesn't risk double-registering the same card.
    response = client.get("/transactions")
    added_section = response.text.split("Kort lagt til", 1)[1].split("Historikk", 1)[0]
    assert "25 kr" in added_section


def test_inline_buy_form_prefills_and_updates_the_single_existing_price(client):
    import db as db_module
    from models import Card, Transaction

    main = make_csv("My Collection", [{"id": "a", "name": "Pikachu"}])
    client.post("/import", files=[("files", ("main.csv", main, "text/csv"))])

    db = db_module.SessionLocal()
    pikachu_id = db.query(Card).filter(Card.card_id == "a").one().id
    db.close()

    client.post(
        "/transactions",
        data={"card_id": pikachu_id, "type": "kjøp", "date": "2026-01-01", "price": "15", "upsert": "1"},
    )

    # The row now offers to update that price, not add a second one.
    response = client.get("/transactions")
    added_section = response.text.split("Kort lagt til", 1)[1].split("Historikk", 1)[0]
    assert 'value="15.0"' in added_section or 'value="15"' in added_section
    assert "Oppdater" in added_section

    # Re-submitting through the same upsert form corrects the price in
    # place -- exactly the "skrive over" the user expects -- instead of
    # creating a second "kjøp" transaction for the same card.
    client.post(
        "/transactions",
        data={"card_id": pikachu_id, "type": "kjøp", "date": "2026-01-02", "price": "0", "upsert": "1"},
    )
    db = db_module.SessionLocal()
    txs = db.query(Transaction).filter(Transaction.card_id == pikachu_id).all()
    assert len(txs) == 1
    assert txs[0].price == 0
    db.close()


def test_inline_buy_form_does_not_guess_which_purchase_to_update_when_ambiguous(client):
    import db as db_module
    from models import Card, Transaction

    main = make_csv("My Collection", [{"id": "a", "name": "Pikachu"}])
    client.post("/import", files=[("files", ("main.csv", main, "text/csv"))])

    db = db_module.SessionLocal()
    pikachu_id = db.query(Card).filter(Card.card_id == "a").one().id
    db.close()

    # Two genuine prior purchases already on record (e.g. bought twice).
    for price in ("10", "20"):
        client.post(
            "/transactions",
            data={"card_id": pikachu_id, "type": "kjøp", "date": "2026-01-01", "price": price},
        )

    client.post(
        "/transactions",
        data={"card_id": pikachu_id, "type": "kjøp", "date": "2026-01-03", "price": "5", "upsert": "1"},
    )
    db = db_module.SessionLocal()
    prices = sorted(t.price for t in db.query(Transaction).filter(Transaction.card_id == pikachu_id).all())
    assert prices == [5, 10, 20]  # inserted alongside, nothing overwritten
    db.close()


def test_added_cards_section_does_not_show_a_price_for_unpriced_cards(client):
    main = make_csv("My Collection", [{"id": "a", "name": "Pikachu"}])
    client.post("/import", files=[("files", ("main.csv", main, "text/csv"))])

    response = client.get("/transactions")
    added_section = response.text.split("Kort lagt til", 1)[1].split("Historikk", 1)[0]
    assert "Registrert pris" in added_section
    # The Registrert pris cell is empty (unlike Referansepris, which does
    # show a value) -- no purchase has been registered for this card yet.
    assert '<td class="num"></td>' in added_section


def test_transactions_table_can_be_sorted_by_column(client):
    import db as db_module
    from models import Card

    main = make_csv(
        "My Collection",
        [{"id": "a", "name": "Zebra"}, {"id": "b", "name": "Abra"}],
    )
    client.post("/import", files=[("files", ("main.csv", main, "text/csv"))])

    db = db_module.SessionLocal()
    ids = {c.card_id: c.id for c in db.query(Card).all()}
    db.close()

    for card_id, price in ((ids["a"], "100"), (ids["b"], "50")):
        client.post(
            "/transactions",
            data={"card_id": card_id, "type": "kjøp", "date": "2026-01-01", "price": price},
        )

    def _table_body(html: str) -> str:
        # The historikk table is the only one wrapped in .table-scroll --
        # the "Kort lagt til" and collapsed unknown-date tables above/below
        # it have their own separately-sortable <tbody> blocks (gsort/usort).
        history = html.split('<div class="table-scroll">', 1)[1]
        return history.split("<tbody>", 1)[1].split("</tbody>", 1)[0]

    text = _table_body(client.get("/transactions?tsort=name&tdir=asc").text)
    assert text.index("Abra") < text.index("Zebra")

    text_desc = _table_body(client.get("/transactions?tsort=price&tdir=desc").text)
    assert text_desc.index("Zebra") < text_desc.index("Abra")  # 100 kr before 50 kr


def test_kort_lagt_til_groups_can_be_sorted_by_column(client):
    main = make_csv(
        "My Collection",
        [{"id": "a", "name": "Zebra"}, {"id": "b", "name": "Abra"}],
    )
    client.post("/import", files=[("files", ("main.csv", main, "text/csv"))])

    def _group_body(html: str) -> str:
        section = html.split("Kort lagt til", 1)[1].split("Historikk", 1)[0]
        return section.split("<tbody>", 1)[1].split("</tbody>", 1)[0]

    asc = _group_body(client.get("/transactions?gsort=name&gdir=asc").text)
    assert asc.index("Abra") < asc.index("Zebra")

    desc = _group_body(client.get("/transactions?gsort=name&gdir=desc").text)
    assert desc.index("Zebra") < desc.index("Abra")


def test_ukjent_dato_table_can_be_sorted_by_column(client):
    import db as db_module
    from models import Card

    main = make_csv(
        "My Collection",
        [{"id": "a", "name": "Zebra"}, {"id": "b", "name": "Abra"}],
    )
    client.post("/import", files=[("files", ("main.csv", main, "text/csv"))])

    db = db_module.SessionLocal()
    db.query(Card).update({"created_at": None})  # move both into "Ukjent dato"
    db.commit()
    db.close()

    def _unknown_body(html: str) -> str:
        section = html.split("<details class=\"collapsible\">", 1)[1]
        return section.split("<tbody>", 1)[1].split("</tbody>", 1)[0]

    asc = _unknown_body(client.get("/transactions?usort=name&udir=asc").text)
    assert asc.index("Abra") < asc.index("Zebra")

    desc = _unknown_body(client.get("/transactions?usort=name&udir=desc").text)
    assert desc.index("Zebra") < desc.index("Abra")


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


def test_dashboard_pokemon_row_groups_every_print_of_the_same_name(client):
    main = make_csv(
        "My Collection",
        [
            {"id": "a", "name": "Sableye", "set": "Vivid Voltage", "variant": "Normal"},
            {"id": "b", "name": "Sableye", "set": "Triplet Beat", "variant": "Holo"},
            {"id": "c", "name": "Magikarp", "set": "Paldea Evolved"},
        ],
    )
    client.post("/import", files=[("files", ("main.csv", main, "text/csv"))])

    dashboard = client.get("/")
    text = dashboard.text
    assert "Pokemon" in text
    pokemon_section = text.split("<h2>Pokemon</h2>", 1)[1]
    assert "Sableye" in pokemon_section
    assert "Magikarp" in pokemon_section
    # Both Sableye prints (Normal + Holo, two different sets) count under one
    # "Sableye" bucket -- 2 unique, not two separate one-card rows. Look only
    # at the "Topp 10" table itself, since the merge form's <datalist> also
    # lists raw card names earlier in the same card.
    top10_section = pokemon_section.split("Topp 10", 1)[1]
    row = top10_section.split("Sableye", 1)[1].split("</tr>", 1)[0]
    assert "<td class=\"num\">2</td>" in row


def test_dashboard_pokemon_table_caps_at_top_10_by_unique_count(client):
    rows = []
    for i in range(11):
        # Pokemon 0 has 3 unique prints, Pokemon 1-10 have 1 each -- Pokemon 0
        # should always make the cut regardless of tie-breaking among the rest.
        prints = 3 if i == 0 else 1
        for p in range(prints):
            rows.append({"id": f"p{i}-{p}", "name": f"Species{i}", "number": f"{i}{p}/999"})
    main = make_csv("My Collection", rows)
    client.post("/import", files=[("files", ("main.csv", main, "text/csv"))])

    dashboard = client.get("/")
    pokemon_section = dashboard.text.split("<h2>Pokemon</h2>", 1)[1].split("<h2>", 1)[0]
    # 11 distinct species exist, but only 10 rows show -- Species0 (3 unique)
    # always makes it in, so exactly one of Species1..10 is excluded.
    shown = sum(1 for i in range(11) if f">Species{i}<" in pokemon_section)
    assert shown == 10
    assert ">Species0<" in pokemon_section


def test_pokemon_favorite_can_be_toggled_on_and_off(client):
    main = make_csv("My Collection", [{"id": "a", "name": "Sableye"}])
    client.post("/import", files=[("files", ("main.csv", main, "text/csv"))])

    response = client.post("/pokemon/favorite", data={"name": "Sableye"}, follow_redirects=True)
    pokemon_section = response.text.split("<h2>Pokemon</h2>", 1)[1].split("<h2>", 1)[0]
    assert 'class="favorite-star active"' in pokemon_section

    # Toggling again removes it.
    response = client.post("/pokemon/favorite", data={"name": "Sableye"}, follow_redirects=True)
    pokemon_section = response.text.split("<h2>Pokemon</h2>", 1)[1].split("<h2>", 1)[0]
    assert 'class="favorite-star active"' not in pokemon_section
    assert 'class="favorite-star"' in pokemon_section


def test_pokemon_search_finds_a_name_to_favorite(client):
    main = make_csv(
        "My Collection",
        [{"id": "a", "name": "Sableye"}, {"id": "b", "name": "Slowbro"}, {"id": "c", "name": "Onix"}],
    )
    client.post("/import", files=[("files", ("main.csv", main, "text/csv"))])

    response = client.get("/pokemon/search?q=slow")
    assert "Slowbro" in response.text
    assert "Sableye" not in response.text
    assert "Onix" not in response.text
    # A search result is itself a one-click favorite form.
    assert '<form method="post" action="/pokemon/favorite">' in response.text or "action=\"/pokemon/favorite\"" in response.text


def test_favorited_pokemon_shows_even_when_not_in_the_top_10(client):
    # 10 other species each with more unique prints than Celebi (1), so
    # Celebi would never make the "Topp 10 (unike)" cutoff on its own.
    rows = [{"id": "celebi", "name": "Celebi"}]
    for i in range(10):
        for p in range(2):
            rows.append({"id": f"filler{i}-{p}", "name": f"Filler{i}", "number": f"{i}{p}/999"})
    main = make_csv("My Collection", rows)
    client.post("/import", files=[("files", ("main.csv", main, "text/csv"))])

    client.post("/pokemon/favorite", data={"name": "Celebi"})

    dashboard = client.get("/")
    pokemon_card = dashboard.text.split("<h2>Pokemon</h2>", 1)[1].split("<h2>", 1)[0]
    assert "Favoritter" in pokemon_card
    favorites_section = pokemon_card.split("Favoritter", 1)[1].split("Topp 10", 1)[0]
    assert "Celebi" in favorites_section

    top10_section = pokemon_card.split("Topp 10", 1)[1]
    assert "Celebi" not in top10_section  # confirms it really was excluded from the cutoff


def test_merging_an_evolution_family_into_one_folder(client):
    # Unlike Celebi/Dark Celebi (a name variant of the same species),
    # Slowpoke/Slowbro/Slowking are genuinely different species -- the
    # folder concept groups them together anyway, purely for display.
    main = make_csv(
        "My Collection",
        [
            {"id": "a", "name": "Slowpoke"},
            {"id": "b", "name": "Slowbro"},
            {"id": "c", "name": "Slowking"},
        ],
    )
    client.post("/import", files=[("files", ("main.csv", main, "text/csv"))])

    client.post("/pokemon/merge", data={"name": "Slowpoke", "canonical": "Slowbro"})
    client.post("/pokemon/merge", data={"name": "Slowking", "canonical": "Slowbro"})

    dashboard = client.get("/")
    pokemon_card = dashboard.text.split("<h2>Pokemon</h2>", 1)[1].split("<h2>", 1)[0]
    top10_section = pokemon_card.split("Topp 10", 1)[1]
    assert top10_section.count('class="row-toggle-name"') == 1
    assert "Slowbro</button>" in top10_section
    assert '<td class="num">3</td>' in top10_section  # all three species, one bucket


def test_merging_pokemon_groups_them_into_one_bucket(client):
    main = make_csv(
        "My Collection",
        [{"id": "a", "name": "Celebi"}, {"id": "b", "name": "Dark Celebi"}],
    )
    client.post("/import", files=[("files", ("main.csv", main, "text/csv"))])

    client.post("/pokemon/merge", data={"name": "Dark Celebi", "canonical": "Celebi"})

    dashboard = client.get("/")
    pokemon_card = dashboard.text.split("<h2>Pokemon</h2>", 1)[1].split("<h2>", 1)[0]
    top10_section = pokemon_card.split("Topp 10", 1)[1]
    # "Dark Celebi" no longer has its own bucket -- both cards count under the
    # single "Celebi" bucket, with "Dark Celebi" still visible as a nested
    # physical print (not as its own top-level row).
    assert top10_section.count('class="row-toggle-name"') == 1
    assert "Celebi</button>" in top10_section
    assert "Dark Celebi</button>" not in top10_section
    assert '<td class="num">2</td>' in top10_section


def test_merging_pokemon_migrates_an_existing_favorite(client):
    main = make_csv(
        "My Collection",
        [{"id": "a", "name": "Celebi"}, {"id": "b", "name": "Dark Celebi"}],
    )
    client.post("/import", files=[("files", ("main.csv", main, "text/csv"))])

    client.post("/pokemon/favorite", data={"name": "Dark Celebi"})
    client.post("/pokemon/merge", data={"name": "Dark Celebi", "canonical": "Celebi"})

    dashboard = client.get("/")
    pokemon_card = dashboard.text.split("<h2>Pokemon</h2>", 1)[1].split("<h2>", 1)[0]
    assert "Favoritter" in pokemon_card
    favorites_section = pokemon_card.split("Favoritter", 1)[1].split("Topp 10", 1)[0]
    assert "Celebi" in favorites_section


def test_merging_pokemon_cascades_existing_aliases_to_the_new_root(client):
    # Sandslash and Alolan Sandslash already merged into "Sandslash", then
    # the user decides "Sandslash" itself should be merged into "Sand Rat"
    # -- Alolan Sandslash must follow along to the new root too, so no
    # alias chain is left dangling.
    main = make_csv(
        "My Collection",
        [
            {"id": "a", "name": "Sandslash"},
            {"id": "b", "name": "Alolan Sandslash"},
            {"id": "c", "name": "Sand Rat"},
        ],
    )
    client.post("/import", files=[("files", ("main.csv", main, "text/csv"))])

    client.post("/pokemon/merge", data={"name": "Alolan Sandslash", "canonical": "Sandslash"})
    client.post("/pokemon/merge", data={"name": "Sandslash", "canonical": "Sand Rat"})

    dashboard = client.get("/")
    pokemon_card = dashboard.text.split("<h2>Pokemon</h2>", 1)[1].split("<h2>", 1)[0]
    top10_section = pokemon_card.split("Topp 10", 1)[1]
    # Only one bucket now -- neither alias name surfaces as its own top-level row.
    assert top10_section.count('class="row-toggle-name"') == 1
    assert "Sand Rat</button>" in top10_section
    assert "Sandslash</button>" not in top10_section
    assert "Alolan Sandslash</button>" not in top10_section

    aliases_html = pokemon_card.split("Legg Pokemon i samme mappe", 1)[1].split("Favoritter", 1)[0]
    # A single "Sand Rat" folder, containing both aliased names -- Alolan
    # Sandslash's alias was cascaded onto the new root, not left pointing at
    # "Sandslash" (which is itself now merged away).
    assert aliases_html.count('class="folder-name"') == 1
    assert '<span class="folder-name">Sand Rat</span>' in aliases_html
    assert "Alolan Sandslash" in aliases_html
    assert "Sandslash" in aliases_html


def test_merging_pokemon_into_itself_after_a_reverse_merge_is_a_noop(client):
    main = make_csv(
        "My Collection",
        [{"id": "a", "name": "Celebi"}, {"id": "b", "name": "Dark Celebi"}],
    )
    client.post("/import", files=[("files", ("main.csv", main, "text/csv"))])

    client.post("/pokemon/merge", data={"name": "Celebi", "canonical": "Dark Celebi"})
    # Attempting the reverse now would create a 2-cycle; it must no-op.
    response = client.post(
        "/pokemon/merge", data={"name": "Dark Celebi", "canonical": "Celebi"}, follow_redirects=True
    )
    assert response.status_code == 200

    dashboard = client.get("/")
    pokemon_card = dashboard.text.split("<h2>Pokemon</h2>", 1)[1].split("<h2>", 1)[0]
    top10_section = pokemon_card.split("Topp 10", 1)[1]
    # Still a single bucket, rooted at "Dark Celebi" (the first merge's
    # target) -- the reverse merge attempt changed nothing.
    assert top10_section.count('class="row-toggle-name"') == 1
    bucket_name = re.search(
        r'<span class="arrow">.</span> ([^<]+)</button>', top10_section
    ).group(1)
    assert bucket_name == "Dark Celebi"


def test_unmerging_a_pokemon_restores_its_own_bucket(client):
    main = make_csv(
        "My Collection",
        [{"id": "a", "name": "Celebi"}, {"id": "b", "name": "Dark Celebi"}],
    )
    client.post("/import", files=[("files", ("main.csv", main, "text/csv"))])

    client.post("/pokemon/merge", data={"name": "Dark Celebi", "canonical": "Celebi"})
    client.post("/pokemon/unmerge", data={"name": "Dark Celebi"})

    dashboard = client.get("/")
    pokemon_card = dashboard.text.split("<h2>Pokemon</h2>", 1)[1].split("<h2>", 1)[0]
    top10_section = pokemon_card.split("Topp 10", 1)[1]
    assert "Dark Celebi" in top10_section


def test_favoriting_an_already_merged_alias_name_favorites_the_canonical_bucket(client):
    main = make_csv(
        "My Collection",
        [{"id": "a", "name": "Celebi"}, {"id": "b", "name": "Dark Celebi"}],
    )
    client.post("/import", files=[("files", ("main.csv", main, "text/csv"))])

    client.post("/pokemon/merge", data={"name": "Dark Celebi", "canonical": "Celebi"})
    # Favoriting via the old, now-merged-away name should favorite "Celebi".
    client.post("/pokemon/favorite", data={"name": "Dark Celebi"})

    dashboard = client.get("/")
    pokemon_card = dashboard.text.split("<h2>Pokemon</h2>", 1)[1].split("<h2>", 1)[0]
    assert "Favoritter" in pokemon_card
    favorites_section = pokemon_card.split("Favoritter", 1)[1].split("Topp 10", 1)[0]
    assert "Celebi" in favorites_section


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
