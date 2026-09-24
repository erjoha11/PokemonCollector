import datetime as dt
import re

import pytest

from conftest import make_csv, seed_import


# --- Transactions "Order history" helpers ---------------------------------
# An order renders as <details id="order-N"> with a grid <summary> whose
# cells are the Qty/Value/Shipping/Agreed-total/Remaining columns. Anchor on
# that id rather than on the text "Order #N": the card picker's "Adding to"
# <select> also lists every order by that label, so a plain text split lands
# in the wrong part of the page.


def order_section(text: str, purchase_id: int) -> str:
    """Everything rendered for one order -- its summary row plus the
    expanded body (agreed-total form, its cards, add-cards button)."""
    start = text.index(f'id="order-{purchase_id}"')
    end = text.find('<details class="order-item"', start + 1)
    if end == -1:
        end = text.find('<section class="ungrouped-section"', start)
    return text[start:end if end != -1 else len(text)]


def order_summary(text: str, purchase_id: int) -> str:
    """Just the collapsed summary row for one order."""
    section = order_section(text, purchase_id)
    return section[: section.index("</summary>")]


def summary_cell(text: str, purchase_id: int, column: str) -> str:
    """Visible text of one summary column, tags stripped and whitespace
    collapsed, e.g. summary_cell(text, 4, "value") -> '60 kr'. `column` is
    the oc-* class suffix used in transactions.html (order/date/qty/value/
    shipping/total/remaining/platform). Handles the columns that wrap their
    value in a nested <span> (Remaining's diff flag, Platform's badge)."""
    summary = order_summary(text, purchase_id)
    after = summary.split(f'class="oc-{column}', 1)[1]
    # Walk to the matching close of this cell's own <span>.
    depth = 0
    out = []
    i = after.index(">") + 1
    while i < len(after):
        if after.startswith("<span", i):
            depth += 1
            i = after.index(">", i) + 1
            continue
        if after.startswith("</span>", i):
            if depth == 0:
                break
            depth -= 1
            i += len("</span>")
            continue
        out.append(after[i])
        i += 1
    return " ".join("".join(out).split())


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
    seed_import(
        client,
        [
            ("files", ("main.csv", main, "text/csv")),
            ("files", ("single.csv", single, "text/csv")),
            ("files", ("duped.csv", duped, "text/csv")),
        ],
    )

    dashboard = client.get("/")
    text = dashboard.text
    # Scope to the KPI card itself -- both collection names also appear in
    # the Inventory breakdown table further down the page.
    card = text.split("<h3>Most valuable collection ", 1)[1].split("<h3>", 1)[0]
    assert "Single Card Collection" in card
    assert "Duplicated Collection" not in card  # not picked -- lower unique value
    assert "100 kr" in card  # the unique value shown, not 500 kr (its total_value)


def test_import_page_shows_sync_log_history(client):
    main = make_csv("My Collection", [{"id": "a", "name": "Pikachu", "qty": 2, "price": "150"}])
    seed_import(client, [("files", ("main.csv", main, "text/csv"))])

    response = client.get("/import")
    assert response.status_code == 200
    assert "Sync Log" in response.text
    assert "main.csv" in response.text
    assert "manual" in response.text


def test_import_sync_log_table_scrolls_instead_of_widening_the_page(client):
    main = make_csv("My Collection", [{"id": "a", "name": "Pikachu", "qty": 2, "price": "150"}])
    seed_import(client, [("files", ("main.csv", main, "text/csv"))])

    response = client.get("/import")
    log_section = response.text.split('id="sync-log"', 1)[1]
    assert '<div class="table-scroll">' in log_section


def test_import_log_table_can_be_sorted_by_column(client):
    zebra = make_csv("My Collection", [{"id": "a", "name": "Pikachu"}])
    abra = make_csv("My Collection", [{"id": "a", "name": "Pikachu"}])
    seed_import(client, [("files", ("zzz.csv", zebra, "text/csv"))])
    seed_import(client, [("files", ("aaa.csv", abra, "text/csv"))])

    def _log_body(html: str) -> str:
        section = html.split('id="sync-log"', 1)[1]
        return section.split("<tbody>", 1)[1].split("</tbody>", 1)[0]

    asc = _log_body(client.get("/import?lsort=files&ldir=asc").text)
    assert asc.index("aaa.csv") < asc.index("zzz.csv")

    desc = _log_body(client.get("/import?lsort=files&ldir=desc").text)
    assert desc.index("zzz.csv") < desc.index("aaa.csv")


def test_import_redirects_to_releases_sync_log(client):
    response = client.get("/import", follow_redirects=False)
    assert response.status_code == 308
    assert response.headers["location"] == "/releases#sync-log"


def test_import_redirect_preserves_sort_query_string(client):
    response = client.get("/import?lsort=files&ldir=asc", follow_redirects=False)
    assert response.status_code == 308
    assert response.headers["location"] == "/releases?lsort=files&ldir=asc#sync-log"


def test_sync_log_sort_partial_swaps_just_that_section(client):
    zebra = make_csv("My Collection", [{"id": "a", "name": "Pikachu"}])
    abra = make_csv("My Collection", [{"id": "a", "name": "Pikachu"}])
    seed_import(client, [("files", ("zzz.csv", zebra, "text/csv"))])
    seed_import(client, [("files", ("aaa.csv", abra, "text/csv"))])

    response = client.get("/releases/sync-log?lsort=files&ldir=asc")
    assert response.status_code == 200
    assert 'id="sync-log"' in response.text
    assert "Release Notes" not in response.text
    assert response.text.index("aaa.csv") < response.text.index("zzz.csv")


def test_dashboard_kpi_tiles_and_section_headings_have_info_tooltips(client):
    main = make_csv("My Collection", [{"id": "a", "name": "Pikachu"}])
    seed_import(client, [("files", ("main.csv", main, "text/csv"))])

    text = client.get("/").text
    # One per KPI tile (Total, Value, Most valuable card/collection/series)
    # plus one per Dashboard section heading (Inventory, Topp 10, Serie,
    # Rarity, Pokemon) -- a generous floor, not an exact count, so this
    # doesn't need updating every time another tooltip is added.
    assert text.count('class="info-icon') >= 10
    assert "info-tooltip" in text


def test_dashboard_shows_a_market_value_chart_left_of_topp_10_and_inventory_below(client):
    import datetime as dt

    import db as db_module
    import snapshots
    from models import Card

    main = make_csv("My Collection", [{"id": "a", "name": "Pikachu", "price": "50"}])
    seed_import(client, [("files", ("main.csv", main, "text/csv"))])

    db = db_module.SessionLocal()
    db.query(Card).update({"created_at": dt.datetime(2026, 1, 10)})
    db.commit()
    snapshots.record_daily_snapshot(db, as_of=dt.date(2026, 1, 10))
    db.close()

    text = client.get("/").text
    assert 'id="dashboard-market-value-card"' in text
    # The Market Value chart is paired with "Most valuable cards"
    # (both come before Inventory, which gets its own full-width row below).
    first_pair = text.split('id="dashboard-market-value-card"', 1)[1].split("Inventory", 1)[0]
    assert "Most valuable cards" in first_pair
    assert "viz-chart-wrap" in first_pair
    assert "50 kr" in first_pair  # the chart's "View as table" value
    # Net invested / Current value / Gain-loss now live in the chart itself.
    assert "Net invested" in first_pair
    assert "Current value" in first_pair


def test_dashboard_market_value_chart_mirrors_transactions_metric_filter(client):
    import datetime as dt

    import db as db_module
    import snapshots

    main = make_csv(
        "My Collection",
        [{"id": "a", "name": "Pikachu", "qty": 3, "price": "50"}],
    )
    seed_import(client, [("files", ("main.csv", main, "text/csv"))])
    db = db_module.SessionLocal()
    snapshots.record_daily_snapshot(db, as_of=dt.date(2026, 1, 10))
    db.close()

    # Pills preserve every other table's sort state, not just the metric --
    # same "keep everything else as-is" idiom sort_th links already use.
    text = client.get("/?csort=name&cdir=asc").text
    assert "Unique collection" in text
    assert "Duplicates" in text
    assert "Total" in text
    assert 'class="viz-filter-pill active">Total</a>' in text  # total selected by default
    assert "Value (Total)" in text
    assert "csort=name" in text
    assert "metric=total" in text

    total_page = client.get("/?metric=total&csort=name&cdir=asc")
    assert total_page.status_code == 200
    assert "Value (Total)" in total_page.text
    assert "150 kr" in total_page.text  # 3 * 50, the "total" metric's value

    fallback_page = client.get("/?metric=not-a-real-metric")
    assert fallback_page.status_code == 200
    assert "Value (Total)" in fallback_page.text


@pytest.mark.parametrize("path", ["/", "/transactions/charts"])
def test_market_value_key_figures_follow_the_selected_metric(client, path):
    import datetime as dt

    import db as db_module
    import snapshots
    from models import Card

    # unique 100, duplicates 200 (two extra copies), total 300; paid 250.
    main = make_csv("My Collection", [{"id": "a", "name": "Pikachu", "qty": 3, "price": "100"}])
    seed_import(client, [("files", ("main.csv", main, "text/csv"))])
    db = db_module.SessionLocal()
    card_id = db.query(Card).one().id
    snapshots.record_daily_snapshot(db, as_of=dt.date.today() - dt.timedelta(days=3))
    db.close()
    client.post("/transactions", data={"card_id": card_id, "type": "purchase", "date": "2026-01-01", "price": "250"})

    def stats(metric):
        html = client.get(f"{path}?metric={metric}").text
        bar = html[html.index('class="chart-kpi-bar"'):]
        return bar[: bar.index("</div>\n  </div>")]

    # Gain / loss has one definition (total value - net invested), so it's
    # only shown on Total -- Unique shows "–" rather than a second "gain".
    unique = stats("unique")
    assert "250 kr" in unique and "100 kr" in unique and "-150 kr" not in unique
    assert unique.count(">–</span>") == 1

    total = stats("total")
    assert "250 kr" in total and "300 kr" in total and "50 kr" in total

    duplicates = stats("duplicates")
    assert "200 kr" in duplicates
    assert duplicates.count(">–</span>") == 2  # no per-copy cost: Net invested and Gain / loss unknown


def test_market_value_chart_shows_price_vs_card_count_breakdown(client):
    import datetime as dt

    import db as db_module
    import snapshots
    from models import Card

    main = make_csv("My Collection", [{"id": "a", "name": "Pikachu", "qty": 1, "price": "100"}])
    seed_import(client, [("files", ("main.csv", main, "text/csv"))])
    db = db_module.SessionLocal()
    snapshots.record_daily_snapshot(db, as_of=dt.date.today() - dt.timedelta(days=2))
    card = db.query(Card).one()
    card.qty = 2
    db.commit()
    db.close()

    html = client.get("/?metric=total").text
    assert "Price development" in html
    assert "More cards" in html and "(+1 card)" in html
    assert "Card count changed that day" in html
    assert '"cardCounts": [1, 2]' in html


def test_market_value_period_pills_switch_and_keep_other_params(client):
    import datetime as dt

    import db as db_module
    import snapshots

    main = make_csv("My Collection", [{"id": "a", "name": "Pikachu", "qty": 1, "price": "100"}])
    seed_import(client, [("files", ("main.csv", main, "text/csv"))])
    db = db_module.SessionLocal()
    old = dt.date.today() - dt.timedelta(days=100)
    snapshots.record_daily_snapshot(db, as_of=old)
    db.close()

    html = client.get("/?metric=unique&period=1m").text
    for key, label in [("1w", "1U"), ("1m", "1M"), ("3m", "3M"), ("6m", "6M"), ("1y", "1Å"), ("all", "Alt")]:
        assert f"period={key}" in html and f">{label}</a>" in html
    assert 'class="viz-filter-pill active">1M</a>' in html
    assert "metric=unique&amp;period=1w" in html  # period links keep the metric
    assert old.isoformat() not in html  # outside the 1M window

    all_time = client.get("/?period=all").text
    assert old.isoformat() in all_time
    assert "timeSeries" in all_time
    assert client.get("/?period=bogus").status_code == 200


def test_inventory_rarity_column_sorts_by_tier_not_alphabetically(client):
    main = make_csv(
        "My Collection",
        [
            {"id": "a", "name": "PromoCard", "rarity": "Promo"},
            {"id": "b", "name": "TripleCard", "rarity": "Triple Rare"},
            {"id": "c", "name": "CommonCard", "rarity": "Common"},
            {"id": "d", "name": "SirCard", "rarity": "Special Illustration Rare"},
            {"id": "e", "name": "AmazingCard", "rarity": "Amazing Rare"},
        ],
    )
    seed_import(client, [("files", ("main.csv", main, "text/csv"))])

    def order(direction):
        html = client.get(f"/inventory?sort=rarity&direction={direction}").text
        names = ["CommonCard", "AmazingCard", "TripleCard", "SirCard", "PromoCard"]
        return sorted(names, key=html.index)

    assert order("asc") == ["CommonCard", "AmazingCard", "TripleCard", "SirCard", "PromoCard"]
    assert order("desc") == ["PromoCard", "SirCard", "TripleCard", "AmazingCard", "CommonCard"]


def test_dashboard_most_valuable_cards_is_a_ranked_list_with_photos(client):
    import db as db_module
    from models import Card

    main = make_csv(
        "My Collection",
        [
            {"id": "a", "name": "Charizard", "price": "400", "qty": 2, "rarity": "Ultra Rare"},
            {"id": "b", "name": "Pikachu", "price": "200"},
        ],
    )
    seed_import(client, [("files", ("main.csv", main, "text/csv"))])
    db = db_module.SessionLocal()
    db.query(Card).filter(Card.card_id == "a").one().image_url = "https://img.example/charizard.png"
    db.commit()
    db.close()

    html = client.get("/").text
    card = html[html.index('id="dashboard-top-cards-card"'):]
    card = card[: card.index('id="dashboard-inventory-card"')]
    assert '<ol class="top-cards-scroll top-card-list">' in card
    assert card.index("Charizard") < card.index("Pikachu")
    assert 'src="https://img.example/charizard.png"' in card
    assert "top-card-item-thumb-empty" in card  # Pikachu has no photo yet
    assert "×2" in card and "Ultra Rare" in card
    assert "top-card-item-bar" not in card  # no price bar (removed on request)
    assert "tsort=name" in card  # sort pills
    # Photo and name open the card viewer, with the card's Dex page as its button.
    assert card.count("data-card-view") == 3  # Charizard photo+name, Pikachu name only
    assert 'data-dex="https://app.dextcg.com/cards/a"' in card
    assert 'id="card-viewer"' in html and "/static/card-viewer.js" in html


def test_dashboard_most_valuable_cards_show_gain_when_cost_is_registered(client):
    import datetime as dt

    import db as db_module
    from models import Card, Transaction

    main = make_csv(
        "My Collection",
        [
            {"id": "a", "name": "Charizard", "price": "400", "qty": 2},
            {"id": "b", "name": "Pikachu", "price": "200"},
            {"id": "c", "name": "Mew", "price": "100"},
        ],
    )
    seed_import(client, [("files", ("main.csv", main, "text/csv"))])
    db = db_module.SessionLocal()
    ids = {c.card_id: c.id for c in db.query(Card).all()}
    db.add_all(
        [
            Transaction(card_id=ids["a"], type="purchase", date=dt.date(2026, 1, 1), price=500),
            Transaction(card_id=ids["b"], type="purchase", date=dt.date(2026, 1, 1), price=250),
        ]
    )
    db.commit()
    db.close()

    html = client.get("/").text
    card = html[html.index('id="dashboard-top-cards-card"'):]
    card = card[: card.index('id="dashboard-inventory-card"')]
    assert "+300 kr" in card  # Charizard: 2 copies x 400 - 500 paid
    assert "viz-delta-loss" in card and "-50 kr" in card  # Pikachu: 200 - 250
    assert card.count("top-card-item-gain") == 2  # Mew has no registered cost


def test_dashboard_tables_show_net_invested_and_colored_gain(client):
    import datetime as dt

    import db as db_module
    from models import Card, Transaction

    main = make_csv(
        "My Collection",
        [
            {"id": "a", "name": "Charizard", "price": "400", "rarity": "Ultra Rare"},
            {"id": "b", "name": "Pikachu", "price": "200", "rarity": "Common"},
        ],
    )
    seed_import(client, [("files", ("main.csv", main, "text/csv"))])
    db = db_module.SessionLocal()
    card_a = db.query(Card).filter(Card.card_id == "a").one()
    db.add(Transaction(card_id=card_a.id, type="purchase", date=dt.date(2026, 1, 1), price=150))
    db.commit()
    db.close()

    html = client.get("/").text
    rarity = html[html.index('id="dashboard-rarity-card"'):]
    rarity = rarity[: rarity.index("</table>")]
    assert "Net invested" in rarity and "Gain/loss" in rarity
    assert "viz-delta-gain" in rarity and "+250 kr" in rarity  # Ultra Rare: 400 - 150
    # Common has no registered purchase: no cost known, so no "gain" either.
    body = rarity[rarity.index("<tbody>"):]
    common_row = body[body.index("Common"):]
    common_row = common_row[: common_row.index("</tr>")]
    assert common_row.count('<td class="num muted">-</td>') == 2
    for card_id in ("dashboard-inventory-card", "dashboard-series-card", "dashboard-pokemon-cards"):
        section = html[html.index(f'id="{card_id}"'):]
        assert "money-gain" in section[: section.index("</table>")]


def test_card_image_large_derives_each_hosts_big_image():
    import app as app_module

    large = app_module._card_image_large
    assert large("https://images.pokemontcg.io/base1/26.png") == "https://images.pokemontcg.io/base1/26_hires.png"
    assert large("https://images.scrydex.com/pokemon/me2pt5-171/small") == "https://images.scrydex.com/pokemon/me2pt5-171/large"
    assert large("https://assets.tcgdex.net/ja/S/S12a/173/low.webp") == "https://assets.tcgdex.net/ja/S/S12a/173/high.webp"
    assert large("https://img.example/x.png") == "https://img.example/x.png"
    assert large(None) is None


def test_image_backfill_route_is_secret_gated_and_reports_progress(client, monkeypatch):
    import card_images

    main = make_csv("My Collection", [{"id": "ex5-4", "name": "Celebi", "number": "4/101"}])
    seed_import(client, [("files", ("main.csv", main, "text/csv"))])
    monkeypatch.setattr(card_images, "fetch_image_by_card_id", lambda card_id, name, number: "https://img.example/celebi.png")

    monkeypatch.setenv("CRON_SECRET", "s3cret")
    assert client.get("/cron/image-backfill").status_code == 401

    body = client.get("/cron/image-backfill?secret=s3cret").json()
    assert body["filled"] == 1 and body["remaining"] == 0 and body["cards_with_image"] == 1


def test_all_pages_render(client):
    for path in ["/", "/inventory", "/transactions", "/import", "/wiki"]:
        response = client.get(path)
        assert response.status_code == 200, path


def test_analyse_redirects_to_transactions(client):
    response = client.get("/analyse", follow_redirects=False)
    assert response.status_code == 308
    assert response.headers["location"] == "/transactions"


def test_transactions_page_shows_economic_kpi_strip(client):
    import datetime as dt

    import db as db_module
    from models import Card, Transaction

    main = make_csv("My Collection", [{"id": "a", "name": "Pikachu", "price": "100"}])
    seed_import(client, [("files", ("main.csv", main, "text/csv"))])

    db = db_module.SessionLocal()
    card = db.query(Card).filter(Card.card_id == "a").one()
    card.created_at = dt.datetime(2026, 1, 15)
    db.add(Transaction(card_id=card.id, type="purchase", date=dt.date(2026, 1, 15), price=80, fees=5))
    db.commit()
    db.close()

    response = client.get("/transactions")
    assert response.status_code == 200
    text = response.text
    assert "Net invested" in text
    assert "85 kr" in text  # 80 purchase price + 5 fee
    assert "Paper gain" in text or "Paper loss" in text or "Loss" in text or "Gain" in text
    # "Current value" deliberately no longer appears here: it was the same
    # number the Market Value KPI card already shows as "Unique value",
    # under a third name, a few hundred pixels away from that card's own
    # (duplicate-inclusive) "Market Value" total.
    assert "Current value" not in text
    assert "View charts" in text
    # The charts themselves are lazy-loaded, not rendered on the initial page.
    assert "viz-chart-wrap" not in text


def test_transactions_charts_endpoint_shows_growth_and_cash_flow_charts(client):
    import datetime as dt

    import db as db_module
    from models import Card, Transaction

    main = make_csv("My Collection", [{"id": "a", "name": "Pikachu", "price": "100"}])
    seed_import(client, [("files", ("main.csv", main, "text/csv"))])

    db = db_module.SessionLocal()
    card = db.query(Card).filter(Card.card_id == "a").one()
    card.created_at = dt.datetime(2026, 1, 15)
    db.add(Transaction(card_id=card.id, type="purchase", date=dt.date(2026, 1, 15), price=80, fees=5))
    db.commit()
    db.close()

    response = client.get("/transactions/charts")
    assert response.status_code == 200
    text = response.text
    assert "viz-chart-wrap" in text
    assert "View as table" in text
    assert "2026-01" in text


def test_transactions_charts_endpoint_handles_no_transactions_or_dated_cards(client):
    main = make_csv("My Collection", [{"id": "a", "name": "Pikachu"}])
    seed_import(client, [("files", ("main.csv", main, "text/csv"))])

    response = client.get("/transactions/charts")
    assert response.status_code == 200
    assert "No data yet" in response.text


def test_transactions_charts_endpoint_has_a_metric_filter_that_switches_the_chart(client):
    import datetime as dt

    import db as db_module
    import snapshots

    main = make_csv("My Collection", [{"id": "a", "name": "Pikachu", "qty": 3, "price": "10"}])
    seed_import(client, [("files", ("main.csv", main, "text/csv"))])
    db = db_module.SessionLocal()
    snapshots.record_daily_snapshot(db, as_of=dt.date(2026, 1, 10))
    db.close()

    default_page = client.get("/transactions/charts")
    assert "Unique collection" in default_page.text
    assert "Duplicates" in default_page.text
    assert "Total" in default_page.text
    assert 'href="/transactions/charts?metric=unique"' in default_page.text
    assert 'href="/transactions/charts?metric=duplicates"' in default_page.text
    assert 'href="/transactions/charts?metric=total"' in default_page.text
    assert 'class="viz-filter-pill active">Total</a>' in default_page.text  # total selected by default
    assert "Value (Total)" in default_page.text

    total_page = client.get("/transactions/charts?metric=total")
    assert total_page.status_code == 200
    assert "Value (Total)" in total_page.text
    # unique_value=10, total_value=30 for this card -- the chosen metric
    # changes which one shows up as the chart's cumulative total.
    assert "30 kr" in total_page.text

    # An unknown metric falls back to the default instead of erroring.
    fallback_page = client.get("/transactions/charts?metric=not-a-real-metric")
    assert fallback_page.status_code == 200
    assert "Value (Total)" in fallback_page.text


def test_wiki_page_documents_the_main_features(client):
    response = client.get("/wiki")
    assert response.status_code == 200
    text = response.text
    for heading in ["Dashboard", "Pokemon folders", "Sorting", "Inventory", "Transactions", "Sync Log"]:
        assert heading in text
    assert 'href="/wiki"' in text  # linked from the nav


def test_transactions_page_shows_transaction_id(client):
    import db as db_module
    from models import Card

    main = make_csv("My Collection", [{"id": "a", "name": "Pikachu", "price": "150"}])
    seed_import(client, [("files", ("main.csv", main, "text/csv"))])

    db = db_module.SessionLocal()
    pikachu_id = db.query(Card).filter(Card.card_id == "a").one().id
    db.close()

    response = client.post(
        "/transactions",
        data={"card_id": pikachu_id, "type": "purchase", "date": "2026-01-01", "price": "10"},
        follow_redirects=True,
    )
    # The transaction id is shown de-emphasized next to the date rather than
    # its own column -- the first transaction gets id 1.
    assert 'title="Transaction ID">#1</span>' in response.text


def test_transaction_can_be_edited_in_place(client):
    import db as db_module
    from models import Card, Transaction

    main = make_csv("My Collection", [{"id": "a", "name": "Pikachu", "price": "150"}])
    seed_import(client, [("files", ("main.csv", main, "text/csv"))])

    db = db_module.SessionLocal()
    pikachu_id = db.query(Card).filter(Card.card_id == "a").one().id
    db.close()

    client.post(
        "/transactions",
        data={"card_id": pikachu_id, "type": "purchase", "date": "2026-01-01", "price": "10"},
    )
    db = db_module.SessionLocal()
    tx_id = db.query(Transaction).one().id
    db.close()

    edit_form = client.get(f"/transactions/{tx_id}/edit")
    assert edit_form.status_code == 200
    assert f'hx-post="/transactions/{tx_id}"' in edit_form.text
    assert 'value="10.0"' in edit_form.text

    response = client.post(
        f"/transactions/{tx_id}",
        data={"date": "2026-02-15", "type": "purchase", "price": "12.5", "platform": "Tradera", "fees": "2"},
    )
    assert response.status_code == 200
    assert "Tradera" in response.text

    db = db_module.SessionLocal()
    tx = db.query(Transaction).one()
    assert tx.platform == "Tradera"
    assert tx.price == 12.5
    assert tx.fees == 2
    assert tx.date.isoformat() == "2026-02-15"
    db.close()

    # Cancel just re-renders the row unchanged (no write).
    cancel = client.get(f"/transactions/{tx_id}/row")
    assert cancel.status_code == 200
    assert "Tradera" in cancel.text


def test_transactions_can_be_tagged_with_a_shared_purchase_id(client):
    import db as db_module
    from models import Card

    main = make_csv(
        "My Collection",
        [{"id": "a", "name": "Pikachu"}, {"id": "b", "name": "Charizard"}],
    )
    seed_import(client, [("files", ("main.csv", main, "text/csv"))])

    db = db_module.SessionLocal()
    ids = {c.card_id: c.id for c in db.query(Card).all()}
    db.close()

    for card_id in (ids["a"], ids["b"]):
        client.post(
            "/transactions",
            data={
                "card_id": card_id,
                "type": "purchase",
                "date": "2026-01-01",
                "price": "10",
                "purchase_id": "5",
            },
        )

    response = client.get("/transactions")
    # The shared purchase_id shows as the group's own heading (with a
    # subtotal), not a repeated per-row column -- see the grouping test below.
    assert "Order #5" in response.text
    assert "2 cards" in response.text


def test_transactions_history_groups_transactions_sharing_a_purchase_id(client):
    import db as db_module
    from models import Card

    main = make_csv(
        "My Collection",
        [{"id": "a", "name": "Pikachu"}, {"id": "b", "name": "Charizard"}, {"id": "c", "name": "Eevee"}],
    )
    seed_import(client, [("files", ("main.csv", main, "text/csv"))])

    db = db_module.SessionLocal()
    ids = {c.card_id: c.id for c in db.query(Card).all()}
    db.close()

    for card_id, price in ((ids["a"], "10"), (ids["b"], "15")):
        client.post(
            "/transactions",
            data={"card_id": card_id, "type": "purchase", "date": "2026-01-01", "price": price, "purchase_id": "7"},
        )
    # Eevee is registered on its own -- no purchase_id, so it should not be
    # folded into the "Order #7" group below.
    client.post(
        "/transactions",
        data={"card_id": ids["c"], "type": "purchase", "date": "2026-01-02", "price": "20"},
    )

    response = client.get("/transactions")
    text = response.text
    assert 'id="order-7"' in text
    assert summary_cell(text, 7, "qty") == "2"
    assert summary_cell(text, 7, "value") == "25 kr"  # 10 + 15, the group's subtotal
    assert "Individually registered" in text
    group_section = order_section(text, 7)
    assert "Pikachu" in group_section
    assert "Charizard" in group_section
    assert "Eevee" not in group_section
    ungrouped_section = text.split("Individually registered", 1)[1]
    assert "Eevee" in ungrouped_section


def test_purchase_groups_rank_items_by_price_and_order_groups_by_purchase_id(client):
    import db as db_module
    from models import Card

    main = make_csv(
        "My Collection",
        [{"id": "a", "name": "Pikachu"}, {"id": "b", "name": "Charizard"}, {"id": "c", "name": "Bulbasaur"}],
    )
    seed_import(client, [("files", ("main.csv", main, "text/csv"))])

    db = db_module.SessionLocal()
    ids = {c.card_id: c.id for c in db.query(Card).all()}
    db.close()

    # Purchase 2 has the earlier date (2026-01-01) and purchase 1 the later
    # one (2026-02-01) -- deliberately reversed from date order, since Ordre
    # #<n> ordering must follow purchase_id descending, not the date.
    for card_id, price in ((ids["a"], "10"), (ids["b"], "50")):
        client.post(
            "/transactions",
            data={"card_id": card_id, "type": "purchase", "date": "2026-01-01", "price": price, "purchase_id": "2"},
        )
    client.post(
        "/transactions",
        data={"card_id": ids["c"], "type": "purchase", "date": "2026-02-01", "price": "5", "purchase_id": "1"},
    )

    text = client.get("/transactions").text
    assert text.index('id="order-2"') < text.index('id="order-1"')

    # Within order #2, the pricier card (Charizard, 50) ranks above the
    # cheaper one (Pikachu, 10) regardless of registration order.
    group_section = order_section(text, 2)
    assert group_section.index("Charizard") < group_section.index("Pikachu")


def test_purchase_cart_records_a_declared_total_and_shows_the_diff(client):
    import db as db_module
    from models import Card, Transaction

    main = make_csv(
        "My Collection",
        [{"id": "a", "name": "Pikachu"}, {"id": "b", "name": "Charizard"}],
    )
    seed_import(client, [("files", ("main.csv", main, "text/csv"))])

    db = db_module.SessionLocal()
    ids = {c.card_id: c.id for c in db.query(Card).all()}
    db.close()

    response = client.post(
        "/transactions/purchase",
        data={
            "type": "purchase",
            "date": "2026-01-01",
            "purchase_id": "4",
            "purchase_total": "100",
            "card_id": [str(ids["a"]), str(ids["b"])],
            "price": ["10", "50"],
        },
        follow_redirects=True,
    )
    assert response.status_code == 200

    db = db_module.SessionLocal()
    txs = db.query(Transaction).filter(Transaction.purchase_id == 4).all()
    assert all(t.purchase_total == 100 for t in txs)
    db.close()

    # Value (60, registered card prices) vs. the agreed Total (100) --
    # the normal-print cards not priced individually yet are the
    # still-unaccounted-for 40 kr, flagged as Remaining since it's nonzero.
    text = client.get("/transactions").text
    assert summary_cell(text, 4, "value") == "60 kr"
    assert summary_cell(text, 4, "total") == "100 kr"
    assert summary_cell(text, 4, "remaining") == "40 kr"
    assert "tx-diff-flag" in order_summary(text, 4)


def test_purchase_shipping_is_subtracted_from_the_diff(client):
    import db as db_module
    from models import Card, Transaction

    main = make_csv("My Collection", [{"id": "a", "name": "Pikachu"}])
    seed_import(client, [("files", ("main.csv", main, "text/csv"))])

    db = db_module.SessionLocal()
    card_id = db.query(Card).filter(Card.card_id == "a").one().id
    db.close()

    # 1000 kr for the card + 76 kr shipping = 1076 kr avtalt. Once shipping
    # is accounted for, the remaining amount should be 0, not 76 -- shipping
    # isn't a missing card.
    response = client.post(
        "/transactions/purchase",
        data={
            "type": "purchase",
            "date": "2026-01-01",
            "purchase_id": "1",
            "purchase_total": "1076",
            "purchase_shipping": "76",
            "card_id": [str(card_id)],
            "price": ["1000"],
        },
        follow_redirects=True,
    )
    assert response.status_code == 200

    db = db_module.SessionLocal()
    tx = db.query(Transaction).filter(Transaction.purchase_id == 1).one()
    assert tx.purchase_shipping == 76
    db.close()

    text = client.get("/transactions").text
    assert summary_cell(text, 1, "value") == "1 000 kr"
    assert summary_cell(text, 1, "shipping") == "76 kr"
    assert summary_cell(text, 1, "total") == "1 076 kr"
    # Remaining is 0 once shipping is accounted for -- a settled order shows
    # the ✓ marker, not a red diff flag. (It is no longer rendered as
    # *nothing*: blank could equally mean "no agreed total set yet", which
    # made settled and untouched orders indistinguishable at a glance.)
    assert "tx-diff-flag" not in order_summary(text, 1)
    assert "oc-settled" in order_summary(text, 1)


def test_trade_row_price_does_not_leak_into_a_mixed_orders_total(client):
    import datetime as dt

    import db as db_module
    from models import Card, Transaction

    main = make_csv(
        "My Collection", [{"id": "a", "name": "Pikachu"}, {"id": "b", "name": "Charizard"}]
    )
    seed_import(client, [("files", ("main.csv", main, "text/csv"))])

    db = db_module.SessionLocal()
    ids = {c.card_id: c.id for c in db.query(Card).all()}
    # One real purchase row (300 kr) plus a trade row (given away, no cash --
    # its 9999 kr "price" is a red herring that should never be summed into
    # the order's registered total or its diff against the agreed total).
    db.add_all(
        [
            Transaction(card_id=ids["a"], type="purchase", date=dt.date(2026, 1, 1), price=300, purchase_id=9,
                        purchase_total=300),
            Transaction(card_id=ids["b"], type="trade", date=dt.date(2026, 1, 1), price=9999, purchase_id=9),
        ]
    )
    db.commit()
    db.close()

    text = client.get("/transactions").text
    assert summary_cell(text, 9, "value") == "300 kr"
    summary_section = order_summary(text, 9)
    assert "9 999 kr" not in summary_section
    # Remaining is 0 (the agreed total matches the real purchase row
    # exactly) -- settled, so the ✓ marker rather than a red diff flag.
    assert "tx-diff-flag" not in summary_section
    assert "oc-settled" in summary_section


def test_create_purchase_reopens_the_new_orders_details_via_open_order(client):
    import db as db_module
    from models import Card

    main = make_csv("My Collection", [{"id": "a", "name": "Pikachu"}])
    seed_import(client, [("files", ("main.csv", main, "text/csv"))])

    db = db_module.SessionLocal()
    card_id = db.query(Card).filter(Card.card_id == "a").one().id
    db.close()

    response = client.post(
        "/transactions/purchase",
        data={
            "type": "purchase",
            "date": "2026-01-01",
            "purchase_id": "12",
            "card_id": [str(card_id)],
            "price": ["25"],
        },
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert response.history  # actually redirected, not a bare 200
    assert "open_order=12" in str(response.history[-1].headers["location"])

    order_details = response.text.split('id="order-12"', 1)[1].split(">", 1)[0]
    assert "open" in order_details


def test_purchase_total_can_be_set_on_an_existing_purchase(client):
    import db as db_module
    from models import Card, Transaction

    main = make_csv("My Collection", [{"id": "a", "name": "Pikachu"}])
    seed_import(client, [("files", ("main.csv", main, "text/csv"))])

    db = db_module.SessionLocal()
    card_id = db.query(Card).filter(Card.card_id == "a").one().id
    db.close()

    client.post(
        "/transactions",
        data={"card_id": card_id, "type": "purchase", "date": "2026-01-01", "price": "10", "purchase_id": "6"},
    )

    # No declared total yet -- Total and Remaining both render as an em
    # dash, distinct from a settled order's ✓.
    text = client.get("/transactions").text
    assert summary_cell(text, 6, "total") == "—"
    assert summary_cell(text, 6, "remaining") == "—"
    assert "tx-diff-flag" not in order_summary(text, 6)

    response = client.post(
        "/transactions/purchase/6/total", data={"purchase_total": "10"}, follow_redirects=True
    )
    assert response.status_code == 200

    # The redirect reopens the very <details> group just edited -- a full
    # navigation used to always collapse it back.
    order_details = response.text.split('id="order-6"', 1)[1].split(">", 1)[0]
    assert "open" in order_details

    db = db_module.SessionLocal()
    tx = db.query(Transaction).filter(Transaction.purchase_id == 6).one()
    assert tx.purchase_total == 10
    db.close()

    # Registered equals agreed now -- the order reads as settled, and the
    # newly-declared Total shows in its own column.
    text = client.get("/transactions").text
    assert summary_cell(text, 6, "total") == "10 kr"
    assert "tx-diff-flag" not in order_summary(text, 6)
    assert "oc-settled" in order_summary(text, 6)


def test_platform_can_be_bulk_set_on_an_existing_purchase(client):
    import db as db_module
    from models import Card, Transaction

    main = make_csv(
        "My Collection",
        [{"id": "a", "name": "Pikachu"}, {"id": "b", "name": "Charizard"}],
    )
    seed_import(client, [("files", ("main.csv", main, "text/csv"))])

    db = db_module.SessionLocal()
    ids = {c.card_id: c.id for c in db.query(Card).all()}
    db.close()

    for card_id in (ids["a"], ids["b"]):
        client.post(
            "/transactions",
            data={"card_id": card_id, "type": "purchase", "date": "2026-01-01", "price": "10", "purchase_id": "7"},
        )

    response = client.post(
        "/transactions/purchase/7/total", data={"platform": "Cardmarket"}, follow_redirects=True
    )
    assert response.status_code == 200

    db = db_module.SessionLocal()
    txs = db.query(Transaction).filter(Transaction.purchase_id == 7).all()
    assert all(t.platform == "Cardmarket" for t in txs)
    db.close()

    # The bulk-edit form's platform input reflects the now-uniform value.
    text = client.get("/transactions").text
    group_section = order_section(text, 7)
    platform_input = group_section.split('name="platform"', 1)[1].split(">", 1)[0]
    assert 'value="Cardmarket"' in platform_input
    assert 'placeholder="Platform"' in platform_input  # uniform, so no "Mixed" warning


def test_platform_bulk_edit_blank_submission_clears_it(client):
    import db as db_module
    from models import Card, Transaction

    main = make_csv("My Collection", [{"id": "a", "name": "Pikachu"}])
    seed_import(client, [("files", ("main.csv", main, "text/csv"))])

    db = db_module.SessionLocal()
    card_id = db.query(Card).filter(Card.card_id == "a").one().id
    db.close()

    client.post(
        "/transactions",
        data={
            "card_id": card_id,
            "type": "purchase",
            "date": "2026-01-01",
            "price": "10",
            "platform": "TCGplayer",
            "purchase_id": "8",
        },
    )

    response = client.post("/transactions/purchase/8/total", data={}, follow_redirects=True)
    assert response.status_code == 200

    db = db_module.SessionLocal()
    tx = db.query(Transaction).filter(Transaction.purchase_id == 8).one()
    assert tx.platform is None
    db.close()


def test_platform_bulk_edit_field_is_blank_when_group_rows_disagree(client):
    import db as db_module
    from models import Card

    main = make_csv(
        "My Collection",
        [{"id": "a", "name": "Pikachu"}, {"id": "b", "name": "Charizard"}],
    )
    seed_import(client, [("files", ("main.csv", main, "text/csv"))])

    db = db_module.SessionLocal()
    ids = {c.card_id: c.id for c in db.query(Card).all()}
    db.close()

    client.post(
        "/transactions",
        data={
            "card_id": ids["a"],
            "type": "purchase",
            "date": "2026-01-01",
            "price": "10",
            "platform": "Cardmarket",
            "purchase_id": "9",
        },
    )
    client.post(
        "/transactions",
        data={
            "card_id": ids["b"],
            "type": "purchase",
            "date": "2026-01-01",
            "price": "10",
            "platform": "TCGplayer",
            "purchase_id": "9",
        },
    )

    text = client.get("/transactions").text
    group_section = order_section(text, 9)
    platform_input = group_section.split('name="platform"', 1)[1].split(">", 1)[0]
    # Still blank, so saving can't silently adopt one row's platform...
    assert 'value=""' in platform_input
    # ...but the field now says so, instead of looking innocently empty
    # while a "Cardmarket" badge shows in the summary above it. Submitting
    # this form overwrites platform on every row of the order.
    assert "Mixed" in platform_input


def test_purchase_cart_start_shows_the_next_free_purchase_id(client):
    import datetime as dt

    import db as db_module
    from models import Card, Transaction

    main = make_csv("My Collection", [{"id": "a", "name": "Pikachu"}])
    seed_import(client, [("files", ("main.csv", main, "text/csv"))])

    # No transactions yet -- the cart starts at purchase_id 1.
    response = client.get("/transactions/purchase/start")
    assert response.status_code == 200
    assert "Order ID 1" in response.text
    assert "New Order" in response.text
    assert '<option value="purchase" selected>' in response.text

    db = db_module.SessionLocal()
    card_id = db.query(Card).filter(Card.card_id == "a").one().id
    db.add(Transaction(card_id=card_id, type="purchase", date=dt.date.today(), price=10, purchase_id=7))
    db.commit()
    db.close()

    # One purchase already on record at id 7 -- the next cart reserves 8,
    # not 1, so it never collides with an existing group.
    response = client.get("/transactions/purchase/start?type=sale")
    assert "Order ID 8" in response.text
    assert '<option value="sale" selected>' in response.text


def test_purchase_cart_search_box_guards_against_enter_submitting_the_form(client):
    # The search input lives inside the same <form> as the "Register" submit
    # button, with no other submit button before it in DOM order -- without
    # a guard, pressing Enter while typing a search query would submit the
    # (likely still-empty) order form instead of just searching.
    response = client.get("/transactions/purchase/start")
    assert response.status_code == 200
    assert "onkeydown=\"if (event.key === 'Enter') event.preventDefault();\"" in response.text


def test_purchase_cart_register_guards_against_silent_native_validation_failure(client):
    # `price` (and `date`) are `required` on the cart form -- a blank price
    # blocks the browser's native validation before any request reaches the
    # server, so no server-rendered error can ever show for that case. The
    # form needs its own client-side guard instead.
    response = client.get("/transactions/purchase/start")
    assert response.status_code == 200
    assert 'onsubmit="return confirmRegisterOrder(this)"' in response.text


def test_recently_added_add_to_order_button_warns_instead_of_silently_doing_nothing(client):
    main = make_csv("My Collection", [{"id": "a", "name": "Pikachu"}])
    seed_import(client, [("files", ("main.csv", main, "text/csv"))])

    response = client.get("/transactions")
    assert response.status_code == 200
    # Was hx-get targeting #cart-body directly (silently no-op'd if no cart
    # was open); now routes through addCardToCart(), which alerts instead.
    assert "addCardToCart(" in response.text
    assert 'hx-get="/transactions/purchase/add-row?card_id=' not in response.text


def test_purchase_cart_search_result_adds_a_row_and_final_submit_creates_transactions(client):
    import db as db_module
    from models import Card, Transaction

    main = make_csv(
        "My Collection",
        [{"id": "a", "name": "Charizard ex"}, {"id": "b", "name": "Blastoise ex"}],
    )
    seed_import(client, [("files", ("main.csv", main, "text/csv"))])

    db = db_module.SessionLocal()
    ids = {c.card_id: c.id for c in db.query(Card).all()}
    db.close()

    search = client.get("/transactions/purchase/search?q=charizard")
    assert "Charizard ex" in search.text
    assert f"addCardToCart({ids['a']})" in search.text

    add_row = client.get(f"/transactions/purchase/add-row?card_id={ids['a']}")
    assert add_row.status_code == 200
    assert "Charizard ex" in add_row.text
    assert f'name="card_id" value="{ids["a"]}"' in add_row.text
    assert 'name="price"' in add_row.text

    response = client.post(
        "/transactions/purchase",
        data={
            "type": "purchase",
            "date": "2026-06-01",
            "platform": "Kortmesse",
            "purchase_id": "3",
            "card_id": [str(ids["a"]), str(ids["b"])],
            "price": ["15", "20"],
        },
        follow_redirects=True,
    )
    assert response.status_code == 200

    db = db_module.SessionLocal()
    txs = sorted(db.query(Transaction).filter(Transaction.purchase_id == 3).all(), key=lambda t: t.card_id)
    assert len(txs) == 2
    assert {t.price for t in txs} == {15, 20}
    assert all(t.type == "purchase" and t.platform == "Kortmesse" for t in txs)
    db.close()

    # And History groups them together under that shared purchase_id.
    history = client.get("/transactions").text
    assert "Order #3" in history
    assert "2 cards" in history


def test_browse_unordered_lists_cards_with_no_purchase_transaction(client):
    import datetime as dt

    import db as db_module
    from models import Card, Transaction

    main = make_csv(
        "My Collection",
        [{"id": "a", "name": "Charizard ex"}, {"id": "b", "name": "Blastoise ex"}],
    )
    seed_import(client, [("files", ("main.csv", main, "text/csv"))])

    db = db_module.SessionLocal()
    ids = {c.card_id: c.id for c in db.query(Card).all()}
    db.close()

    # Neither card has an order yet -- both show up.
    response = client.get("/transactions/purchase/browse-unordered")
    assert response.status_code == 200
    assert "Charizard ex" in response.text
    assert "Blastoise ex" in response.text
    assert "2 cards without an order" in response.text

    # Registering a purchase for Charizard removes it from the list --
    # "no order" is about linked transactions, not import date.
    db = db_module.SessionLocal()
    db.add(Transaction(card_id=ids["a"], type="purchase", date=dt.date.today(), price=10))
    db.commit()
    db.close()

    response = client.get("/transactions/purchase/browse-unordered")
    assert "Charizard ex" not in response.text
    assert "Blastoise ex" in response.text
    assert "1 card without an order" in response.text


def test_browse_unordered_caps_and_reports_the_full_count(client):
    cards = [{"id": str(i), "name": f"Card {i}"} for i in range(60)]
    main = make_csv("My Collection", cards)
    seed_import(client, [("files", ("main.csv", main, "text/csv"))])

    response = client.get("/transactions/purchase/browse-unordered")
    assert response.status_code == 200
    assert "Showing 50 of 60 cards without an order." in response.text


def test_purchase_cart_rejects_submitting_with_no_cards(client):
    response = client.post(
        "/transactions/purchase",
        data={"type": "purchase", "date": "2026-06-01", "purchase_id": "1", "card_id": [], "price": []},
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert "search for at least one card" in response.text


def test_card_picker_lists_cards_with_a_filter_for_recently_added(client):
    main = make_csv("My Collection", [{"id": "a", "name": "Pikachu"}])
    seed_import(client, [("files", ("main.csv", main, "text/csv"))])

    response = client.get("/transactions")
    assert response.status_code == 200
    text = response.text
    # "Recently Added" is no longer its own table -- it's one filter of the
    # single merged card picker (which also absorbed the old, separate
    # "Legacy import" table).
    assert 'id="card-picker"' in text
    assert "Recently added (1)" in text
    assert "Pikachu" in text


def test_card_picker_merges_dated_and_undated_cards_into_one_table(client):
    import datetime as dt

    import db as db_module
    from models import Card

    main = make_csv(
        "My Collection",
        [{"id": "a", "name": "Pikachu"}, {"id": "b", "name": "Charizard"}],
    )
    seed_import(client, [("files", ("main.csv", main, "text/csv"))])

    db = db_module.SessionLocal()
    db.query(Card).filter(Card.card_id == "b").update({"created_at": None})
    db.commit()
    db.close()

    # Both cards now live in the one merged picker: the dated card with its
    # date, the undated one marked "no date" rather than shown blank (blank
    # reads as a rendering bug) and sorted to the bottom by _sorted_rows'
    # null-key bucketing, instead of exiled to a separate collapsed table.
    text = client.get("/transactions?pick=all").text
    picker = text.split('id="card-picker"', 1)[1]
    assert "Pikachu" in picker
    assert "Charizard" in picker
    assert "no date" in picker
    assert picker.index("Pikachu") < picker.index("Charizard")
    # One selection model for every row, dated or not.
    assert picker.count('class="card-pick-checkbox"') == 2
    assert "Legacy import" not in text


def test_card_picker_without_an_order_filter_hides_cards_that_have_one(client):
    import datetime as dt

    import db as db_module
    from models import Card, Transaction

    main = make_csv(
        "My Collection",
        [{"id": "a", "name": "Pikachu"}, {"id": "b", "name": "Charizard"}],
    )
    seed_import(client, [("files", ("main.csv", main, "text/csv"))])

    db = db_module.SessionLocal()
    db.query(Card).filter(Card.card_id.in_(["a", "b"])).update({"created_at": None}, synchronize_session=False)
    charizard_id = db.query(Card).filter(Card.card_id == "b").one().id
    db.add(Transaction(card_id=charizard_id, type="purchase", date=dt.date.today(), price=10, purchase_id=1))
    db.commit()
    db.close()

    # Default filter is "without an order" -- the old Legacy table's rule,
    # now an explicit, switchable filter rather than table membership.
    picker = client.get("/transactions").text.split('id="card-picker"', 1)[1]
    assert "Pikachu" in picker
    assert "Charizard" not in picker  # already on order #1

    # ...and switching to "All" brings it back, which the old two-table
    # split had no way to express.
    picker_all = client.get("/transactions?pick=all").text.split('id="card-picker"', 1)[1]
    assert "Pikachu" in picker_all
    assert "Charizard" in picker_all
    # The order it's already on is named, so merging the two tables doesn't
    # lose the "does this card still need an order?" signal that used to be
    # encoded by which table the row appeared in.
    assert 'href="/transactions?open_order=1#order-1"' in picker_all


def test_card_picker_offers_one_target_selector_covering_new_and_existing_orders(client):
    # The old page had two mutually-exclusive control groups whose
    # visibility JS toggled on whether a cart happened to be open, and
    # clicking "add" with no cart open did nothing at all. Now there is one
    # always-valid target selector listing "New order" plus every existing
    # order, so the no-cart-open dead end can't be reached.
    import datetime as dt

    import db as db_module
    from models import Card, Transaction

    main = make_csv("My Collection", [{"id": "a", "name": "Pikachu"}, {"id": "b", "name": "Charizard"}])
    seed_import(client, [("files", ("main.csv", main, "text/csv"))])

    db = db_module.SessionLocal()
    charizard_id = db.query(Card).filter(Card.card_id == "b").one().id
    db.add(Transaction(card_id=charizard_id, type="purchase", date=dt.date.today(), price=10, purchase_id=3))
    db.commit()
    db.close()

    text = client.get("/transactions").text
    assert 'id="picker-target"' in text
    target_select = text.split('id="picker-target"', 1)[1].split("</select>", 1)[0]
    assert '<option value="new">New order</option>' in target_select
    assert '<option value="3">' in target_select
    # The two dead control groups and their toggle are gone for good.
    assert "legacy-open-order-controls" not in text
    assert "legacy-existing-order-select" not in text
    assert "updateLegacyOrderControls" not in text


def test_add_cards_to_existing_order_creates_rows_and_redirects(client):
    import datetime as dt

    import db as db_module
    from models import Card, Transaction

    main = make_csv(
        "My Collection",
        [{"id": "a", "name": "Pikachu"}, {"id": "b", "name": "Charizard"}, {"id": "c", "name": "Blastoise"}],
    )
    seed_import(client, [("files", ("main.csv", main, "text/csv"))])

    db = db_module.SessionLocal()
    ids = {c.card_id: c.id for c in db.query(Card).all()}
    db.add(Transaction(card_id=ids["a"], type="purchase", date=dt.date.today(), price=10, purchase_id=3))
    db.commit()
    db.close()

    response = client.post(
        "/transactions/purchase/add-existing-cards",
        data={"card_id": [str(ids["b"]), str(ids["c"])], "purchase_id": "3"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/transactions?open_order=3"

    db = db_module.SessionLocal()
    try:
        for card_id in (ids["b"], ids["c"]):
            tx = db.query(Transaction).filter(Transaction.card_id == card_id).one()
            assert tx.purchase_id == 3
            assert tx.type == "purchase"
            assert tx.price == 0
    finally:
        db.close()


def test_add_cards_to_existing_order_rejects_an_order_id_that_does_not_exist(client):
    import db as db_module
    from models import Card, Transaction

    main = make_csv("My Collection", [{"id": "a", "name": "Pikachu"}])
    seed_import(client, [("files", ("main.csv", main, "text/csv"))])

    db = db_module.SessionLocal()
    card_id = db.query(Card).filter(Card.card_id == "a").one().id
    db.close()

    response = client.post(
        "/transactions/purchase/add-existing-cards",
        data={"card_id": [str(card_id)], "purchase_id": "999"},
    )
    assert response.status_code == 200
    assert "exist yet" in response.text
    assert "Order #999" in response.text

    db = db_module.SessionLocal()
    try:
        assert db.query(Transaction).filter(Transaction.card_id == card_id).count() == 0
    finally:
        db.close()


def test_add_cards_to_existing_order_rejects_with_no_cards_selected(client):
    import datetime as dt

    import db as db_module
    from models import Card, Transaction

    main = make_csv("My Collection", [{"id": "a", "name": "Pikachu"}])
    seed_import(client, [("files", ("main.csv", main, "text/csv"))])

    db = db_module.SessionLocal()
    card_id = db.query(Card).filter(Card.card_id == "a").one().id
    db.add(Transaction(card_id=card_id, type="purchase", date=dt.date.today(), price=10, purchase_id=3))
    db.commit()
    db.close()

    response = client.post("/transactions/purchase/add-existing-cards", data={"purchase_id": "3"})
    assert response.status_code == 200
    assert "Select at least one card" in response.text


def test_transactions_history_table_scrolls_instead_of_widening_the_page(client):
    main = make_csv("My Collection", [{"id": "a", "name": "Pikachu"}])
    seed_import(client, [("files", ("main.csv", main, "text/csv"))])

    response = client.get("/transactions")
    assert '<div class="table-scroll">' in response.text


def test_added_cards_section_shows_the_registered_price_once_bought(client):
    import db as db_module
    from models import Card, Transaction

    main = make_csv("My Collection", [{"id": "a", "name": "Pikachu"}])
    seed_import(client, [("files", ("main.csv", main, "text/csv"))])

    db = db_module.SessionLocal()
    pikachu_id = db.query(Card).filter(Card.card_id == "a").one().id
    db.close()

    client.post(
        "/transactions",
        data={"card_id": pikachu_id, "type": "purchase", "date": "2026-01-01", "price": "25"},
    )
    db = db_module.SessionLocal()
    tx = db.query(Transaction).filter(Transaction.card_id == pikachu_id).one()
    assert tx.price == 25
    db.close()

    # And the card picker's "Paid" column now shows that already-registered
    # price, so a second visit doesn't risk double-registering the same
    # card. The card has a purchase now, so it only appears under "All".
    picker = client.get("/transactions?pick=all").text.split('id="card-picker"', 1)[1]
    assert "25 kr" in picker


def test_upsert_corrects_the_single_existing_price_instead_of_adding_a_second_one(client):
    import db as db_module
    from models import Card, Transaction

    main = make_csv("My Collection", [{"id": "a", "name": "Pikachu"}])
    seed_import(client, [("files", ("main.csv", main, "text/csv"))])

    db = db_module.SessionLocal()
    pikachu_id = db.query(Card).filter(Card.card_id == "a").one().id
    db.close()

    client.post(
        "/transactions",
        data={"card_id": pikachu_id, "type": "purchase", "date": "2026-01-01", "price": "15", "upsert": "1"},
    )

    # Re-posting with upsert=1 corrects the price in place -- exactly the
    # overwrite the user expects -- instead of creating a second "purchase"
    # transaction for the same card.
    client.post(
        "/transactions",
        data={"card_id": pikachu_id, "type": "purchase", "date": "2026-01-02", "price": "0", "upsert": "1"},
    )
    db = db_module.SessionLocal()
    txs = db.query(Transaction).filter(Transaction.card_id == pikachu_id).all()
    assert len(txs) == 1
    assert txs[0].price == 0
    db.close()


def test_upsert_carries_and_updates_a_purchase_id_in_place(client):
    import db as db_module
    from models import Card, Transaction

    main = make_csv("My Collection", [{"id": "a", "name": "Pikachu"}])
    seed_import(client, [("files", ("main.csv", main, "text/csv"))])

    db = db_module.SessionLocal()
    pikachu_id = db.query(Card).filter(Card.card_id == "a").one().id
    db.close()

    client.post(
        "/transactions",
        data={
            "card_id": pikachu_id,
            "type": "purchase",
            "date": "2026-01-01",
            "price": "15",
            "purchase_id": "7",
            "upsert": "1",
        },
    )
    db = db_module.SessionLocal()
    tx = db.query(Transaction).filter(Transaction.card_id == pikachu_id).one()
    assert tx.purchase_id == 7
    db.close()

    # Correcting the price again through upsert updates the purchase_id in
    # place too, instead of leaving the old value stuck.
    client.post(
        "/transactions",
        data={
            "card_id": pikachu_id,
            "type": "purchase",
            "date": "2026-01-02",
            "price": "20",
            "purchase_id": "9",
            "upsert": "1",
        },
    )
    db = db_module.SessionLocal()
    tx = db.query(Transaction).filter(Transaction.card_id == pikachu_id).one()
    assert tx.price == 20
    assert tx.purchase_id == 9
    db.close()


def test_upsert_does_not_guess_which_purchase_to_update_when_ambiguous(client):
    import db as db_module
    from models import Card, Transaction

    main = make_csv("My Collection", [{"id": "a", "name": "Pikachu"}])
    seed_import(client, [("files", ("main.csv", main, "text/csv"))])

    db = db_module.SessionLocal()
    pikachu_id = db.query(Card).filter(Card.card_id == "a").one().id
    db.close()

    # Two genuine prior purchases already on record (e.g. bought twice).
    for price in ("10", "20"):
        client.post(
            "/transactions",
            data={"card_id": pikachu_id, "type": "purchase", "date": "2026-01-01", "price": price},
        )

    client.post(
        "/transactions",
        data={"card_id": pikachu_id, "type": "purchase", "date": "2026-01-03", "price": "5", "upsert": "1"},
    )
    db = db_module.SessionLocal()
    prices = sorted(t.price for t in db.query(Transaction).filter(Transaction.card_id == pikachu_id).all())
    assert prices == [5, 10, 20]  # inserted alongside, nothing overwritten
    db.close()


def test_card_picker_does_not_show_a_paid_price_for_unpriced_cards(client):
    main = make_csv("My Collection", [{"id": "a", "name": "Pikachu"}])
    seed_import(client, [("files", ("main.csv", main, "text/csv"))])

    picker = client.get("/transactions").text.split('id="card-picker"', 1)[1]
    assert "Paid" in picker
    # The Paid cell is empty (unlike Market price, which does show a value)
    # -- no purchase has been registered for this card yet.
    assert '<td class="num"></td>' in picker


def test_transactions_table_can_be_sorted_by_column(client):
    import db as db_module
    from models import Card

    main = make_csv(
        "My Collection",
        [{"id": "a", "name": "Zebra"}, {"id": "b", "name": "Abra"}],
    )
    seed_import(client, [("files", ("main.csv", main, "text/csv"))])

    db = db_module.SessionLocal()
    ids = {c.card_id: c.id for c in db.query(Card).all()}
    db.close()

    for card_id, price in ((ids["a"], "100"), (ids["b"], "50")):
        client.post(
            "/transactions",
            data={"card_id": card_id, "type": "purchase", "date": "2026-01-01", "price": price},
        )

    def _table_body(html: str) -> str:
        # The historikk table is the only one wrapped in .table-scroll --
        # the "Recently Added" and collapsed unknown-date tables above/below
        # it have their own separately-sortable <tbody> blocks (gsort/usort).
        history = html.split('<div class="table-scroll">', 1)[1]
        return history.split("<tbody>", 1)[1].split("</tbody>", 1)[0]

    text = _table_body(client.get("/transactions?tsort=name&tdir=asc").text)
    assert text.index("Abra") < text.index("Zebra")

    text_desc = _table_body(client.get("/transactions?tsort=price&tdir=desc").text)
    assert text_desc.index("Zebra") < text_desc.index("Abra")  # 100 kr before 50 kr


def _picker_body(html: str) -> str:
    section = html.split('id="card-picker"', 1)[1]
    return section.split("<tbody>", 1)[1].split("</tbody>", 1)[0]


def test_card_picker_can_be_sorted_by_column(client):
    main = make_csv(
        "My Collection",
        [{"id": "a", "name": "Zebra"}, {"id": "b", "name": "Abra"}],
    )
    seed_import(client, [("files", ("main.csv", main, "text/csv"))])

    asc = _picker_body(client.get("/transactions?gsort=name&gdir=asc").text)
    assert asc.index("Abra") < asc.index("Zebra")

    desc = _picker_body(client.get("/transactions?gsort=name&gdir=desc").text)
    assert desc.index("Zebra") < desc.index("Abra")


def test_card_picker_sorts_undated_cards_with_the_same_column_params(client):
    # The old "Unknown date" table had its own usort/udir pair. Merged into
    # the one picker, undated rows sort by the same gsort/gdir as every
    # other row -- one table, one sort contract.
    import db as db_module
    from models import Card

    main = make_csv(
        "My Collection",
        [{"id": "a", "name": "Zebra"}, {"id": "b", "name": "Abra"}],
    )
    seed_import(client, [("files", ("main.csv", main, "text/csv"))])

    db = db_module.SessionLocal()
    db.query(Card).update({"created_at": None})  # both are now undated
    db.commit()
    db.close()

    asc = _picker_body(client.get("/transactions?gsort=name&gdir=asc").text)
    assert asc.index("Abra") < asc.index("Zebra")

    desc = _picker_body(client.get("/transactions?gsort=name&gdir=desc").text)
    assert desc.index("Zebra") < desc.index("Abra")


def test_retired_usort_param_is_still_accepted(client):
    # usort/udir no longer drive anything (the table they sorted is gone),
    # but an old bookmark carrying them must still render, not 422.
    main = make_csv("My Collection", [{"id": "a", "name": "Zebra"}])
    seed_import(client, [("files", ("main.csv", main, "text/csv"))])

    assert client.get("/transactions?usort=name&udir=desc").status_code == 200


def test_import_then_dashboard_reflects_the_sync(client):
    main = make_csv("My Collection", [{"id": "a", "name": "Pikachu", "qty": 2, "price": "150"}])
    seed_import(client, [("files", ("main.csv", main, "text/csv"))])
    # The Sync Log page is log-only -- the sync shows up as a new row there.
    log_page = client.get("/import")
    assert log_page.status_code == 200
    assert "main.csv" in log_page.text

    dashboard = client.get("/")
    assert dashboard.status_code == 200
    assert "150 kr" in dashboard.text  # reference price shows up in top-10 valuable cards
    inventory = client.get("/inventory")
    assert "Pikachu" in inventory.text


def test_inventory_card_name_links_to_dex(client):
    main = make_csv("My Collection", [{"id": "ex5-4", "name": "Dark Celebi", "price": "780"}])
    seed_import(client, [("files", ("main.csv", main, "text/csv"))])

    inventory = client.get("/inventory")
    assert '<a href="https://app.dextcg.com/cards/ex5-4"' in inventory.text
    assert "target=\"_blank\"" in inventory.text


def test_dashboard_top_cards_link_to_dex(client):
    main = make_csv("My Collection", [{"id": "ex5-4", "name": "Dark Celebi", "price": "780"}])
    seed_import(client, [("files", ("main.csv", main, "text/csv"))])

    dashboard = client.get("/")
    assert '<a href="https://app.dextcg.com/cards/ex5-4"' in dashboard.text


def test_inventory_default_sort_is_release_order_with_numeric_tiebreak(client):
    import db as db_module
    from models import SetReleaseOrder

    main = make_csv(
        "My Collection",
        [
            {"id": "new1", "name": "NewCard", "series": "XY", "set": "XY", "number": "1/165"},
            {"id": "old10", "name": "OldCard10", "series": "Original", "set": "Base Set", "number": "10/102"},
            {"id": "old2", "name": "OldCard2", "series": "Original", "set": "Base Set", "number": "2/102"},
        ],
    )
    seed_import(client, [("files", ("main.csv", main, "text/csv"))])

    db = db_module.SessionLocal()
    db.add(SetReleaseOrder(series="Original", set="Base Set", release_rank=1))
    db.add(SetReleaseOrder(series="Scarlet & Violet", set="151", release_rank=50))
    db.commit()
    db.close()
    # The sort itself reads Card.set_id -> Set.release_rank, not
    # SetReleaseOrder directly -- re-run init_db() so its _backfill_sets()
    # step get-or-creates the matching Set rows (carrying release_rank over
    # from the SetReleaseOrder rows just added) and links the seeded cards,
    # same as would happen automatically on the next real app startup.
    db_module.init_db()

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


def test_inventory_release_sort_falls_back_for_unlinked_or_unranked_sets(client):
    import db as db_module
    from models import Card, Set

    main = make_csv(
        "My Collection",
        [
            {"id": "ranked1", "name": "RankedCard", "series": "Original", "set": "Base Set", "number": "1/102"},
            {"id": "norank1", "name": "NoRankCard", "series": "Original", "set": "Jungle", "number": "1/64"},
        ],
    )
    seed_import(client, [("files", ("main.csv", main, "text/csv"))])

    db = db_module.SessionLocal()
    # Since issue #134, the import above already get-or-created the
    # ("Original", "Base Set") Set row inline (unranked) -- update it in
    # place rather than adding a new row, which would collide with the
    # unique (series, name) constraint.
    db.query(Set).filter_by(series="Original", name="Base Set").update({Set.release_rank: 1})
    # A card with no series/set at all (e.g. bad/incomplete Dex data) --
    # neither the importer nor _backfill_sets() has a (series, set) pair to
    # link it to, unlike NoRankCard below, which does get a
    # (linked-but-unranked) Set row.
    # qty=1: Card.qty defaults to 0, and qty == 0 cards are hidden from the
    # default /inventory view (issue #132), so it wouldn't render otherwise.
    db.add(Card(card_id="nolink1", variant=None, name="NoLinkCard", series=None, set=None, number="1/1", qty=1))
    db.commit()
    db.close()
    # NoRankCard's (Original, Jungle) pair still gets its own Set row from
    # this -- just with a null release_rank, since none was researched (see
    # Set's docstring) -- it's "linked but unranked", not "unlinked".
    # NoLinkCard has no series/set at all, so _backfill_sets() has no pair
    # to link it to -- it's the "actually unlinked" (set_id IS NULL) case.
    db_module.init_db()

    db = db_module.SessionLocal()
    ranked = db.query(Card).filter_by(name="RankedCard").one()
    unranked = db.query(Card).filter_by(name="NoRankCard").one()
    unlinked = db.query(Card).filter_by(name="NoLinkCard").one()
    assert ranked.set_id is not None
    assert unranked.set_id is not None
    assert ranked.linked_set.release_rank == 1
    assert unranked.linked_set.release_rank is None
    assert unlinked.set_id is None  # no series/set -- nothing to link to
    db.close()

    text = client.get("/inventory").text.split('id="inventory-results"', 1)[1]
    # The known/ranked set sorts before both fallback cases (unranked-but-
    # linked, and fully unlinked), which both land on UNKNOWN_RELEASE_RANK --
    # same semantics as before, sourced from the Card.set_id -> Set.release_rank
    # FK now instead of a string match.
    assert text.index("RankedCard") < text.index("NoRankCard")
    assert text.index("RankedCard") < text.index("NoLinkCard")


def _seed_two_series_and_link_sets(client, old_rank, new_rank):
    """Shared setup for the Dashboard/Inventory-KPI/Transactions-KPI
    release-order tests below: two series, each linked to a `Set` row via
    `init_db()`'s `_backfill_sets()`, ranked per the caller's args.
    """
    import db as db_module

    main = make_csv(
        "My Collection",
        [
            {"id": "old1", "name": "OldSeriesCard", "series": "Original", "set": "Base Set"},
            {"id": "new1", "name": "NewSeriesCard", "series": "XY", "set": "XY"},
        ],
    )
    seed_import(client, [("files", ("main.csv", main, "text/csv"))])
    db_module.init_db()  # link Card.set_id -> Set for both (series, set) pairs

    from models import Set

    db = db_module.SessionLocal()
    db.query(Set).filter_by(series="Original", name="Base Set").update({"release_rank": old_rank})
    db.query(Set).filter_by(series="XY", name="XY").update({"release_rank": new_rank})
    db.commit()
    db.close()


def test_dashboard_series_breakdown_reflects_set_release_rank_edits(client):
    """Issue #138: the Dashboard's series breakdown must source release
    order from `Set.release_rank` (via `Card.set_id`), not the superseded
    `SetReleaseOrder` table -- editing one rank re-orders the row without
    touching `set_release_order` at all.
    """
    _seed_two_series_and_link_sets(client, old_rank=1, new_rank=50)

    text = client.get("/").text.split('id="dashboard-series-card"', 1)[1]
    assert text.index("Original") < text.index("XY")

    import db as db_module
    from models import Set

    db = db_module.SessionLocal()
    # Flip the ranks -- no set_release_order row involved anywhere.
    db.query(Set).filter_by(series="Original", name="Base Set").update({"release_rank": 99})
    db.query(Set).filter_by(series="XY", name="XY").update({"release_rank": 1})
    db.commit()
    db.close()

    text = client.get("/").text.split('id="dashboard-series-card"', 1)[1]
    assert text.index("XY") < text.index("Original")


def test_inventory_kpi_series_breakdown_reflects_set_release_rank_edits(client):
    """Same source as the Dashboard, exercised through Inventory's
    collapsed KPI module (a full, non-htmx page load only, per app.py)."""
    _seed_two_series_and_link_sets(client, old_rank=1, new_rank=50)

    text = client.get("/inventory").text
    assert "Original" in text  # sanity: KPI module rendered on full load

    import db as db_module
    from models import Set

    db = db_module.SessionLocal()
    db.query(Set).filter_by(series="Original", name="Base Set").update({"release_rank": 99})
    db.query(Set).filter_by(series="XY", name="XY").update({"release_rank": 1})
    db.commit()
    db.close()

    # Re-fetching after the rank flip doesn't 500 and still renders both
    # series -- the KPI module itself doesn't expose ordering as directly
    # testable text (it highlights only the single top series by value),
    # so this exercises the same by_series_breakdown() call path Dashboard's
    # ordering test above already verifies more precisely.
    text = client.get("/inventory").text
    assert "Original" in text and "XY" in text


def test_transactions_kpi_series_breakdown_reflects_set_release_rank_edits(client):
    """Same source as Dashboard/Inventory, exercised through Transactions'
    KPI module."""
    _seed_two_series_and_link_sets(client, old_rank=1, new_rank=50)

    text = client.get("/transactions").text
    assert "Original" in text

    import db as db_module
    from models import Set

    db = db_module.SessionLocal()
    db.query(Set).filter_by(series="Original", name="Base Set").update({"release_rank": 99})
    db.query(Set).filter_by(series="XY", name="XY").update({"release_rank": 1})
    db.commit()
    db.close()

    text = client.get("/transactions").text
    assert "Original" in text and "XY" in text


def test_inventory_dup_filter_shows_only_cards_with_duplicates(client):
    main = make_csv(
        "My Collection",
        [
            {"id": "a", "name": "Pikachu", "qty": 3},
            {"id": "b", "name": "Charizard", "qty": 1},
        ],
    )
    seed_import(client, [("files", ("main.csv", main, "text/csv"))])

    response = client.get("/inventory?dup=1")
    assert "Pikachu" in response.text
    assert "Charizard" not in response.text
    assert "1 card" in response.text


def test_inventory_accepts_an_empty_dup_query_value(client):
    # The filter form's hidden "dup" input submits an empty string when
    # unchecked, and hx-include="closest form" on every other dropdown
    # (series/set/collection/binder/language) resubmits it too -- so ?dup=
    # (empty, not absent) must not 422.
    main = make_csv("My Collection", [{"id": "a", "name": "Pikachu"}])
    seed_import(client, [("files", ("main.csv", main, "text/csv"))])

    response = client.get("/inventory?dup=&series=&language=")
    assert response.status_code == 200
    assert "Pikachu" in response.text


def test_inventory_hides_qty_zero_cards_by_default_and_unowned_toggle_reveals_them(client):
    main = make_csv(
        "My Collection",
        [
            {"id": "a", "name": "Pikachu", "qty": 1},
            {"id": "b", "name": "Charizard", "qty": 0},
        ],
    )
    seed_import(client, [("files", ("main.csv", main, "text/csv"))])

    default_response = client.get("/inventory")
    assert "Pikachu" in default_response.text
    assert "Charizard" not in default_response.text
    assert "1 card" in default_response.text

    unowned_response = client.get("/inventory?unowned=1")
    assert "Pikachu" in unowned_response.text
    assert "Charizard" in unowned_response.text
    assert "2 cards" in unowned_response.text
    # 0-qty row gets the dimmed/badged treatment.
    assert "card-row-unowned" in unowned_response.text
    assert "0 owned" in unowned_response.text


def test_inventory_accepts_an_empty_unowned_query_value(client):
    main = make_csv("My Collection", [{"id": "a", "name": "Pikachu"}])
    seed_import(client, [("files", ("main.csv", main, "text/csv"))])

    response = client.get("/inventory?unowned=&series=&language=")
    assert response.status_code == 200
    assert "Pikachu" in response.text


def test_inventory_shows_and_filters_by_language(client):
    main = make_csv(
        "My Collection",
        [
            {"id": "a", "name": "Pikachu", "locale": "ENG"},
            {"id": "b", "name": "Charizard", "locale": "JPN"},
        ],
    )
    seed_import(client, [("files", ("main.csv", main, "text/csv"))])

    full = client.get("/inventory").text
    assert "Language" in full
    assert "ENG" in full and "JPN" in full

    eng_only = client.get("/inventory?language=ENG").text
    assert "Pikachu" in eng_only
    assert "Charizard" not in eng_only


def test_inventory_can_be_sorted_by_language(client):
    main = make_csv(
        "My Collection",
        [
            {"id": "a", "name": "Zubat", "locale": "JPN"},
            {"id": "b", "name": "Abra", "locale": "ENG"},
        ],
    )
    seed_import(client, [("files", ("main.csv", main, "text/csv"))])

    def _rows(html: str) -> str:
        return html.split("<tbody>", 1)[1].split("</tbody>", 1)[0]

    asc = _rows(client.get("/inventory?sort=language&direction=asc").text)
    assert asc.index("Abra") < asc.index("Zubat")  # ENG before JPN


def test_inventory_price_sort_keeps_unpriced_cards_last(client):
    main = make_csv(
        "My Collection",
        [
            {"id": "priced", "name": "Priced", "price": "25"},
            {"id": "unpriced", "name": "Unpriced", "price": ""},
        ],
    )
    seed_import(client, [("files", ("main.csv", main, "text/csv"))])

    def _rows(html: str) -> str:
        return html.split("<tbody>", 1)[1].split("</tbody>", 1)[0]

    asc = _rows(client.get("/inventory?sort=reference_price&direction=asc").text)
    desc = _rows(client.get("/inventory?sort=reference_price&direction=desc").text)
    assert asc.index("Priced") < asc.index("Unpriced")
    assert desc.index("Priced") < desc.index("Unpriced")


def test_inventory_shows_net_paid_and_per_print_gain(client):
    import db as db_module
    from models import Card, Transaction

    main = make_csv(
        "My Collection",
        [
            {"id": "priced", "name": "Priced", "price": "25"},
            {"id": "unpriced", "name": "Unpriced", "price": ""},
        ],
    )
    seed_import(client, [("files", ("main.csv", main, "text/csv"))])
    db = db_module.SessionLocal()
    priced = db.query(Card).filter(Card.card_id == "priced").one()
    db.add(Transaction(card_id=priced.id, type="purchase", date=dt.date(2026, 1, 1), price=10, fees=2))
    db.commit()
    db.close()

    response = client.get("/inventory")
    rows = response.text.split("<tbody>", 1)[1].split("</tbody>", 1)[0]
    assert "Net paid" in response.text
    assert "Gain" in response.text
    assert "12 kr" in rows
    assert "13 kr" in rows
    assert rows.count(">-</td>") >= 2  # Net paid + Gain for the unpurchased card


def test_inventory_value_sorts_treat_missing_cost_as_less_than_zero(client):
    import db as db_module
    from models import Card, Transaction

    main = make_csv(
        "My Collection",
        [
            {"id": "priced", "name": "Priced", "price": "25"},
            {"id": "unpriced", "name": "Unpriced", "price": ""},
        ],
    )
    seed_import(client, [("files", ("main.csv", main, "text/csv"))])
    db = db_module.SessionLocal()
    priced = db.query(Card).filter(Card.card_id == "priced").one()
    db.add(Transaction(card_id=priced.id, type="purchase", date=dt.date(2026, 1, 1), price=10))
    db.commit()
    db.close()

    for sort in ("net_invested", "gain_loss"):
        rows = client.get(f"/inventory?sort={sort}&direction=desc").text.split("<tbody>", 1)[1]
        assert rows.index("Priced") < rows.index("Unpriced")


def test_dashboard_totalt_column_links_to_inventory_filtered_by_dup(client):
    from urllib.parse import quote

    main = make_csv("My Collection", [{"id": "a", "name": "Pikachu", "qty": 2}])
    seed_import(client, [("files", ("main.csv", main, "text/csv"))])

    dashboard = client.get("/")
    # The "which cards" link lives on Total (not Duplicates) -- same filter,
    # different column: /inventory?series=...&dup=1.
    expected_href = f"/inventory?series={quote('Test Series')}&dup=1"
    assert expected_href in dashboard.text


def test_dashboard_series_name_is_the_drilldown_trigger_not_a_link(client):
    main = make_csv("My Collection", [{"id": "a", "series": "Original", "set": "Base Set"}])
    seed_import(client, [("files", ("main.csv", main, "text/csv"))])

    dashboard = client.get("/")
    text = dashboard.text
    # Scope to the Series card itself -- the Pokemon card's own drill-down
    # rows legitimately link to Inventory by series/set (see the Set/Series
    # columns), so "Original</a>" can validly appear elsewhere on the page.
    series_card = text.split("<h2>Series ", 1)[1].split("<h2>", 1)[0]
    assert 'class="row-toggle-name"' in series_card
    assert ">Original</a>" not in series_card  # no longer a plain link to Inventory
    assert "Base Set" in series_card  # the nested set row is rendered (hidden until expanded)


def test_dashboard_collection_row_drills_down_to_individual_cards(client):
    main = make_csv("My Collection", [{"id": "a", "name": "Pikachu"}])
    collection = make_csv("My Binder Collection", [{"id": "a"}])
    seed_import(
        client,
        [
            ("files", ("main.csv", main, "text/csv")),
            ("files", ("collection.csv", collection, "text/csv")),
        ],
    )

    dashboard = client.get("/")
    text = dashboard.text
    assert "My Binder Collection" in text
    assert "Pikachu" in text  # the leaf card row, hidden until the collection is expanded
    assert 'data-group="coll-1"' in text


def test_dashboard_bulk_row_is_not_nested_under_collections(client):
    # Bulk (no collection) is the complement of "Collections", not a member
    # of it -- it must not render with the same indentation/class as a
    # named collection's child-row.
    main = make_csv("My Collection", [{"id": "a", "name": "Pikachu"}])
    collection = make_csv("My Named Collection", [{"id": "a"}])
    seed_import(
        client,
        [
            ("files", ("main.csv", main, "text/csv")),
            ("files", ("collection.csv", collection, "text/csv")),
        ],
    )
    main2 = make_csv("My Collection", [{"id": "b", "name": "Magikarp"}])
    seed_import(client, [("files", ("main2.csv", main2, "text/csv"))])

    text = client.get("/").text
    inventory_card = text.split("<h2>Inventory", 1)[1].split("<h2>", 1)[0]
    bulk_row = inventory_card.split(">Bulk<", 1)[0].rsplit("<tr", 1)[1]
    assert 'class="child-row"' not in bulk_row
    assert 'data-group="coll-bulk"' in inventory_card


def test_dashboard_rarity_row_drills_down_to_individual_cards(client):
    main = make_csv("My Collection", [{"id": "a", "name": "Pikachu", "rarity": "Rare"}])
    seed_import(client, [("files", ("main.csv", main, "text/csv"))])

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
    seed_import(client, [("files", ("main.csv", main, "text/csv"))])

    dashboard = client.get("/")
    text = dashboard.text
    assert "Pokemon" in text
    pokemon_section = text.split("<h2>Pokemon ", 1)[1]
    assert "Sableye" in pokemon_section
    assert "Magikarp" in pokemon_section
    # Both Sableye prints (Normal + Holo, two different sets) count under one
    # "Sableye" bucket -- 2 unique, not two separate one-card rows. Look only
    # at the "Top 10" table itself, since the merge form's <datalist> also
    # lists raw card names earlier in the same card.
    top10_section = pokemon_section.split("Top 10", 1)[1]
    row = top10_section.split("Sableye", 1)[1].split("</tr>", 1)[0]
    assert "<td class=\"num\">2</td>" in row


def test_dashboard_pokemon_table_shows_set_and_series_for_a_single_print(client):
    main = make_csv(
        "My Collection",
        [{"id": "a", "name": "Magikarp", "series": "Scarlet & Violet", "set": "Paldea Evolved"}],
    )
    seed_import(client, [("files", ("main.csv", main, "text/csv"))])

    dashboard = client.get("/")
    pokemon_section = dashboard.text.split("<h2>Pokemon ", 1)[1]
    top10_section = pokemon_section.split("Top 10", 1)[1]
    row = top10_section.split("Magikarp", 1)[1].split("</tr>", 1)[0]
    assert "<td>Paldea Evolved</td>" in row or "Paldea Evolved</a>" in row
    assert "<td>Scarlet &amp; Violet</td>" in row or "Scarlet &amp; Violet</a>" in row


def test_dashboard_pokemon_table_shows_multiple_when_bucket_spans_multiple_sets(client):
    main = make_csv(
        "My Collection",
        [
            {"id": "a", "name": "Sableye", "set": "Vivid Voltage", "variant": "Normal"},
            {"id": "b", "name": "Sableye", "set": "Triplet Beat", "variant": "Holo"},
        ],
    )
    seed_import(client, [("files", ("main.csv", main, "text/csv"))])

    dashboard = client.get("/")
    pokemon_section = dashboard.text.split("<h2>Pokemon ", 1)[1]
    top10_section = pokemon_section.split("Top 10", 1)[1]
    bucket_row = top10_section.split("Sableye", 1)[1].split("</tr>", 1)[0]
    assert "Multiple" in bucket_row
    # But drilling down into the individual prints still shows each one's own set.
    assert "Vivid Voltage" in top10_section
    assert "Triplet Beat" in top10_section


def test_dashboard_pokemon_table_caps_at_top_10_by_unique_count(client):
    rows = []
    for i in range(11):
        # Pokemon 0 has 3 unique prints, Pokemon 1-10 have 1 each -- Pokemon 0
        # should always make the cut regardless of tie-breaking among the rest.
        prints = 3 if i == 0 else 1
        for p in range(prints):
            rows.append({"id": f"p{i}-{p}", "name": f"Species{i}", "number": f"{i}{p}/999"})
    main = make_csv("My Collection", rows)
    seed_import(client, [("files", ("main.csv", main, "text/csv"))])

    dashboard = client.get("/")
    pokemon_section = dashboard.text.split("<h2>Pokemon ", 1)[1]
    # 11 distinct species exist, but only 10 rows show -- Species0 (3 unique)
    # always makes it in, so exactly one of Species1..10 is excluded.
    shown = sum(1 for i in range(11) if f">Species{i}<" in pokemon_section)
    assert shown == 10
    assert ">Species0<" in pokemon_section


def test_pokemon_favorite_can_be_toggled_on_and_off(client):
    main = make_csv("My Collection", [{"id": "a", "name": "Sableye"}])
    seed_import(client, [("files", ("main.csv", main, "text/csv"))])

    response = client.post("/pokemon/favorite", data={"name": "Sableye"}, follow_redirects=True)
    pokemon_section = response.text.split("<h2>Pokemon ", 1)[1]
    assert 'class="favorite-star active"' in pokemon_section

    # Toggling again removes it.
    response = client.post("/pokemon/favorite", data={"name": "Sableye"}, follow_redirects=True)
    pokemon_section = response.text.split("<h2>Pokemon ", 1)[1]
    assert 'class="favorite-star active"' not in pokemon_section
    assert 'class="favorite-star"' in pokemon_section


def test_pokemon_search_finds_a_name_to_favorite(client):
    main = make_csv(
        "My Collection",
        [{"id": "a", "name": "Sableye"}, {"id": "b", "name": "Slowbro"}, {"id": "c", "name": "Onix"}],
    )
    seed_import(client, [("files", ("main.csv", main, "text/csv"))])

    response = client.get("/pokemon/search?q=slow")
    assert "Slowbro" in response.text
    assert "Sableye" not in response.text
    assert "Onix" not in response.text
    # A search result is itself a one-click favorite form.
    assert '<form method="post" action="/pokemon/favorite">' in response.text or "action=\"/pokemon/favorite\"" in response.text


def test_favorited_pokemon_shows_even_when_not_in_the_top_10(client):
    # 10 other species each with more unique prints than Celebi (1), so
    # Celebi would never make the "Top 10 (unique)" cutoff on its own.
    rows = [{"id": "celebi", "name": "Celebi"}]
    for i in range(10):
        for p in range(2):
            rows.append({"id": f"filler{i}-{p}", "name": f"Filler{i}", "number": f"{i}{p}/999"})
    main = make_csv("My Collection", rows)
    seed_import(client, [("files", ("main.csv", main, "text/csv"))])

    client.post("/pokemon/favorite", data={"name": "Celebi"})

    dashboard = client.get("/")
    pokemon_card = dashboard.text.split("<h2>Pokemon ", 1)[1]
    assert "Favorites" in pokemon_card
    favorites_section = pokemon_card.split("Favorites", 1)[1].split("Top 10", 1)[0]
    assert "Celebi" in favorites_section

    top10_section = pokemon_card.split("Top 10", 1)[1]
    assert "Celebi" not in top10_section  # confirms it really was excluded from the cutoff


def test_pokemon_topp10_table_can_be_sorted_by_column(client):
    main = make_csv(
        "My Collection",
        [{"id": "a", "name": "Abra"}, {"id": "b", "name": "Zubat"}],
    )
    seed_import(client, [("files", ("main.csv", main, "text/csv"))])

    def _topp10(html: str) -> str:
        # Scope to the Pokemon card's own "Top 10" table -- "Topp 10 mest
        # verdifulle kort" appears earlier on the page too.
        pokemon_section = html.split("<h2>Pokemon ", 1)[1]
        return pokemon_section.split("Top 10", 1)[1]

    asc = _topp10(client.get("/?psort=name&pdir=asc").text)
    assert asc.index("Abra") < asc.index("Zubat")

    desc = _topp10(client.get("/?psort=name&pdir=desc").text)
    assert desc.index("Zubat") < desc.index("Abra")


def test_pokemon_favoritter_table_can_be_sorted_by_column(client):
    main = make_csv(
        "My Collection",
        [{"id": "a", "name": "Abra"}, {"id": "b", "name": "Zubat"}],
    )
    seed_import(client, [("files", ("main.csv", main, "text/csv"))])
    client.post("/pokemon/favorite", data={"name": "Abra"})
    client.post("/pokemon/favorite", data={"name": "Zubat"})

    def _favoritter(html: str) -> str:
        return html.split("Favorites", 1)[1].split("Top 10", 1)[0]

    asc = _favoritter(client.get("/?fsort=name&fdir=asc").text)
    assert asc.index("Abra") < asc.index("Zubat")

    desc = _favoritter(client.get("/?fsort=name&fdir=desc").text)
    assert desc.index("Zubat") < desc.index("Abra")


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
    seed_import(client, [("files", ("main.csv", main, "text/csv"))])

    client.post("/pokemon/merge", data={"name": "Slowpoke", "canonical": "Slowbro"})
    client.post("/pokemon/merge", data={"name": "Slowking", "canonical": "Slowbro"})

    dashboard = client.get("/")
    pokemon_card = dashboard.text.split("<h2>Pokemon ", 1)[1]
    top10_section = pokemon_card.split("Top 10", 1)[1]
    assert top10_section.count('class="row-toggle-name"') == 1
    assert "Slowbro</button>" in top10_section
    assert '<td class="num">3</td>' in top10_section  # all three species, one bucket


def test_merging_pokemon_groups_them_into_one_bucket(client):
    main = make_csv(
        "My Collection",
        [{"id": "a", "name": "Celebi"}, {"id": "b", "name": "Dark Celebi"}],
    )
    seed_import(client, [("files", ("main.csv", main, "text/csv"))])

    client.post("/pokemon/merge", data={"name": "Dark Celebi", "canonical": "Celebi"})

    dashboard = client.get("/")
    pokemon_card = dashboard.text.split("<h2>Pokemon ", 1)[1]
    top10_section = pokemon_card.split("Top 10", 1)[1]
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
    seed_import(client, [("files", ("main.csv", main, "text/csv"))])

    client.post("/pokemon/favorite", data={"name": "Dark Celebi"})
    client.post("/pokemon/merge", data={"name": "Dark Celebi", "canonical": "Celebi"})

    dashboard = client.get("/")
    pokemon_card = dashboard.text.split("<h2>Pokemon ", 1)[1]
    assert "Favorites" in pokemon_card
    favorites_section = pokemon_card.split("Favorites", 1)[1].split("Top 10", 1)[0]
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
    seed_import(client, [("files", ("main.csv", main, "text/csv"))])

    client.post("/pokemon/merge", data={"name": "Alolan Sandslash", "canonical": "Sandslash"})
    client.post("/pokemon/merge", data={"name": "Sandslash", "canonical": "Sand Rat"})

    dashboard = client.get("/")
    pokemon_card = dashboard.text.split("<h2>Pokemon ", 1)[1]
    top10_section = pokemon_card.split("Top 10", 1)[1]
    # Only one bucket now -- neither alias name surfaces as its own top-level row.
    assert top10_section.count('class="row-toggle-name"') == 1
    assert "Sand Rat</button>" in top10_section
    assert "Sandslash</button>" not in top10_section
    assert "Alolan Sandslash</button>" not in top10_section

    aliases_html = pokemon_card.split("Put Pokemon in the same folder", 1)[1].split("Favorites", 1)[0]
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
    seed_import(client, [("files", ("main.csv", main, "text/csv"))])

    client.post("/pokemon/merge", data={"name": "Celebi", "canonical": "Dark Celebi"})
    # Attempting the reverse now would create a 2-cycle; it must no-op.
    response = client.post(
        "/pokemon/merge", data={"name": "Dark Celebi", "canonical": "Celebi"}, follow_redirects=True
    )
    assert response.status_code == 200

    dashboard = client.get("/")
    pokemon_card = dashboard.text.split("<h2>Pokemon ", 1)[1]
    top10_section = pokemon_card.split("Top 10", 1)[1]
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
    seed_import(client, [("files", ("main.csv", main, "text/csv"))])

    client.post("/pokemon/merge", data={"name": "Dark Celebi", "canonical": "Celebi"})
    client.post("/pokemon/unmerge", data={"name": "Dark Celebi"})

    dashboard = client.get("/")
    pokemon_card = dashboard.text.split("<h2>Pokemon ", 1)[1]
    top10_section = pokemon_card.split("Top 10", 1)[1]
    assert "Dark Celebi" in top10_section


def test_merging_a_pokemon_reopens_the_folder_details_after_redirect(client):
    # Full-page reloads used to always collapse the "Legg Pokemon i samme
    # mappe" <details> -- merge/unmerge now redirect with a flag telling the
    # dashboard to render it open, so the section you just used stays open.
    main = make_csv(
        "My Collection",
        [{"id": "a", "name": "Celebi"}, {"id": "b", "name": "Dark Celebi"}],
    )
    seed_import(client, [("files", ("main.csv", main, "text/csv"))])

    response = client.post(
        "/pokemon/merge", data={"name": "Dark Celebi", "canonical": "Celebi"}, follow_redirects=True
    )
    folder_details = response.text.split("Put Pokemon in the same folder", 1)[0].rsplit("<details", 1)[1]
    assert "open" in folder_details

    response = client.post("/pokemon/unmerge", data={"name": "Dark Celebi"}, follow_redirects=True)
    folder_details = response.text.split("Put Pokemon in the same folder", 1)[0].rsplit("<details", 1)[1]
    assert "open" in folder_details


def test_favoriting_an_already_merged_alias_name_favorites_the_canonical_bucket(client):
    main = make_csv(
        "My Collection",
        [{"id": "a", "name": "Celebi"}, {"id": "b", "name": "Dark Celebi"}],
    )
    seed_import(client, [("files", ("main.csv", main, "text/csv"))])

    client.post("/pokemon/merge", data={"name": "Dark Celebi", "canonical": "Celebi"})
    # Favoriting via the old, now-merged-away name should favorite "Celebi".
    client.post("/pokemon/favorite", data={"name": "Dark Celebi"})

    dashboard = client.get("/")
    pokemon_card = dashboard.text.split("<h2>Pokemon ", 1)[1]
    assert "Favorites" in pokemon_card
    favorites_section = pokemon_card.split("Favorites", 1)[1].split("Top 10", 1)[0]
    assert "Celebi" in favorites_section


def test_dashboard_series_set_row_drills_down_to_individual_cards(client):
    main = make_csv(
        "My Collection", [{"id": "a", "name": "Pikachu", "series": "Original", "set": "Base Set"}]
    )
    seed_import(client, [("files", ("main.csv", main, "text/csv"))])

    dashboard = client.get("/")
    text = dashboard.text
    assert 'class="row-toggle-name" data-row-id="series-1-set-1"' in text
    assert 'data-group="series-1-set-1"' in text  # Pikachu's leaf row nests under the set, not the series


def test_dashboard_top_cards_show_card_number(client):
    main = make_csv("My Collection", [{"id": "a", "name": "Pikachu", "number": "58/102", "price": "150"}])
    seed_import(client, [("files", ("main.csv", main, "text/csv"))])

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
    seed_import(
        client,
        [
            ("files", ("main.csv", main, "text/csv")),
            ("files", ("alpha.csv", alpha, "text/csv")),
            ("files", ("zeta.csv", zeta, "text/csv")),
        ],
    )

    # Scope to the Inventory table itself -- collection names can also appear
    # earlier on the page via the "Most valuable collection" KPI highlight.
    def _inventory_table(html: str) -> str:
        return html.split("<h2>Inventory ", 1)[1]

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
    seed_import(client, [("files", ("main.csv", main, "text/csv"))])

    response = client.get("/inventory?q=Charizard")
    assert "Charizard" in response.text
    assert "1 card" in response.text
