"""Dashboard "Recently added" slide in the Price movers carousel (issue #279)."""

import datetime as dt

import queries
from models import Card


def _card(card_id, name, created_at, qty=1, **kw):
    return Card(card_id=card_id, name=name, qty=qty, created_at=created_at, **kw)


def test_recently_added_newest_first_limited_to_ten_owned_and_dated(db_session):
    base = dt.datetime(2026, 9, 1, 12, 0)
    dated = [_card(f"c{i}", f"Card {i}", base + dt.timedelta(days=i)) for i in range(12)]
    undated = _card("u", "Legacy", None)  # no known added date: left out
    sold = _card("s", "Sold", base + dt.timedelta(days=99), qty=0)  # not owned: left out
    db_session.add_all([*dated, undated, sold])
    db_session.commit()

    result = queries.recently_added(db_session.query(Card).all())

    assert len(result) == queries.RECENTLY_ADDED_LIMIT == 10
    assert [c.name for c in result] == [f"Card {i}" for i in range(11, 1, -1)]
    assert undated not in result and sold not in result


def test_recently_added_breaks_same_instant_ties_on_id_and_handles_empty(db_session):
    same = dt.datetime(2026, 9, 1, 12, 0)
    first, second = _card("a", "First", same), _card("b", "Second", same)
    db_session.add_all([first, second])
    db_session.commit()

    assert [c.name for c in queries.recently_added([first, second])] == ["Second", "First"]
    assert queries.recently_added([_card("x", "Undated", None)]) == []


def test_dashboard_renders_price_movers_and_recently_added_slides(client):
    import db as db_module

    with db_module.SessionLocal() as db:
        db.add(
            Card(
                card_id="new", variant="Reverse Holo", name="Sprigatito", set="Paldea Evolved",
                number="13", qty=1, market_price=25.0, created_at=dt.datetime(2026, 9, 30, 8, 0),
            )
        )
        db.add(Card(card_id="old", name="Legacy Lapras", qty=1, market_price=10.0, created_at=None))
        db.commit()
        new_id = db.query(Card).filter_by(card_id="new").one().id

    text = client.get("/").text

    tile = text[text.index('data-kpi-carousel="dashboard-movers"'):]
    tile = tile[: tile.index("Most valuable collection")]
    # Price movers is the first slide and visible without JS; Recently added
    # is rendered but hidden until kpi-carousel.js takes over.
    assert '<section class="kpi-carousel-slide" id="kpi-slide-movers" data-slide="movers"' in tile
    assert 'id="kpi-slide-recent" data-slide="recent" aria-labelledby="kpi-slide-recent-title" hidden>' in tile
    assert "Price movers" in tile and "Recently added" in tile
    # Controls: tablist + labelled prev/next buttons, hidden without JS.
    assert '<div class="kpi-carousel-nav" hidden>' in tile
    assert 'role="tablist"' in tile and tile.count('role="tab"') == 2
    assert 'aria-label="Previous slide"' in tile and 'aria-label="Next slide"' in tile
    assert 'aria-live="polite"' in tile
    # Recently added row: link to the card page, set/number/variant, added date.
    recent = tile[tile.index('id="kpi-slide-recent"'):]
    assert f'href="/cards/{new_id}"' in recent
    assert "Paldea Evolved · #13 · Reverse Holo" in recent
    assert "30.09.2026" in recent
    assert "Legacy Lapras" not in recent  # undated: left out
    assert '/static/kpi-carousel.js' in text


def test_dashboard_recently_added_empty_state(client):
    text = client.get("/").text
    assert "No owned cards with a known added date yet." in text
