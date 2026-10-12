"""Master-set progress bars and the Copy lists panel (issue #385): the three
plain-text builders in app.py (Owned / Missing / Duplicates for sale), the
bars and "N left" in the block, the panel's place above the grid, the
Show=Duplicates count line, the collapsed Duplicates table and the
/collections overview's mini-bar. Offline.

Reuses test_master_set's sv2a fixture (7 / 11 owned, 5 duplicates in hand:
main #1 ×2 at 10 kr, main #3 ×2 at 100 kr, Poké Ball #4 ×1 at 50 kr) and
test_in_transit_sets_lists' "on the way" helper (#382).
"""
from __future__ import annotations

import re

import pytest

import db as db_module
import queries
from models import Card, Collection
from test_in_transit_sets_lists import _put_on_the_way
from test_master_set import DISPLAY, build

OWNED_LINES = [
    f"{DISPLAY}: owned 7 of 11",
    "#1 Mon 1 ×3",
    "#1 Mon 1 · Poké Ball",
    "#2 Mon 2",
    "#3 Mon 3 ×3",
    "#4 Mon 4 · Poké Ball ×2",
    "#5 Mon 5",
    "#6 Mon 6",
]
MISSING_LINES = [
    f"{DISPLAY}: missing 4 of 11",
    "#2 Mon 2 · Poké Ball",
    "#4 Mon 4",
    "#5 Mon 5 · Poké Ball",
    "#7 Mon 7",
]
DUPLICATES_LINES = [
    f"{DISPLAY}: 5 duplicates for sale",
    "#1 Mon 1 · Main ×2 · 10 kr",
    "#3 Mon 3 · Main ×2 · 100 kr",
    "#4 Mon 4 · Poké Ball ×1 · 50 kr",
]


@pytest.fixture
def texts(client):
    """The three builders, over the fixture after `mutate(db)`."""
    import app as app_module

    def run(mutate=None):
        with db_module.SessionLocal() as db:
            build(db, collection="151 Collection")
            if mutate:
                mutate(db)
                db.commit()
                db.expire_all()
            detail = queries.master_set_detail(db, "ja", "sv2a")
            return detail, {
                "owned": app_module._owned_list_text(detail).splitlines(),
                "missing": app_module._missing_list_text(detail).splitlines(),
                "duplicates": app_module._duplicates_list_text(detail).splitlines(),
            }

    return run


def _card(db, card_id, variant):
    return db.query(Card).filter_by(card_id=card_id, variant=variant).one()


# --------------------------------------------------------------------------
# The text builders
# --------------------------------------------------------------------------
def test_owned_list_counts_the_master_set_x(texts):
    detail, t = texts()
    assert t["owned"] == OWNED_LINES
    # One line per counted owned print: the Master set X. The Master Ball
    # print the fixture owns is left out.
    assert len(t["owned"]) - 1 == detail.master_set.owned
    assert not any("Master Ball" in line for line in t["owned"])


def test_missing_list_is_unchanged(texts):
    _, t = texts()
    assert t["missing"] == MISSING_LINES


def test_duplicates_list(texts):
    detail, t = texts()
    assert t["duplicates"] == DUPLICATES_LINES
    assert sum(int(re.search(r"×(\d+)", line).group(1)) for line in t["duplicates"][1:]) == detail.spares


def test_duplicates_list_counts_only_copies_in_hand(texts):
    """#382: main #1 and Poké Ball #4 are on the way, so their duplicates
    aren't for sale. They're still owned (the Owned list keeps ×N)."""
    _, t = texts(_put_on_the_way)
    assert t["duplicates"] == [f"{DISPLAY}: 2 duplicates for sale", "#3 Mon 3 · Main ×2 · 100 kr"]
    assert t["owned"] == OWNED_LINES


def test_duplicates_list_includes_master_ball_and_drops_unknown_prices(texts):
    def mutate(db):
        _card(db, "jpn_sv2a-1", "Master Ball Holo").qty = 3
        main_three = db.query(Card).filter_by(card_id="jpn_sv2a-3").all()
        for c in main_three:
            c.market_price = None
            c.price_flags = "no_price"

    _, t = texts(mutate)
    assert t["duplicates"] == [
        f"{DISPLAY}: 7 duplicates for sale",
        "#1 Mon 1 · Main ×2 · 10 kr",
        "#1 Mon 1 · Master Ball ×2 · 500 kr",
        "#3 Mon 3 · Main ×2",
        "#4 Mon 4 · Poké Ball ×1 · 50 kr",
    ]
    # Master Ball still never counts toward Owned.
    assert t["owned"] == OWNED_LINES


def test_one_duplicate_header_is_singular(texts):
    def mutate(db):
        _card(db, "jpn_sv2a-1", "Normal").qty = 1
        _card(db, "jpn_sv2a-4", "Poké Ball Holo").qty = 1
        _card(db, "jpn_sv2a-3", "Holo").qty = 1  # + the "Holo Rare" row: 2 copies, 1 spare

    _, t = texts(mutate)
    assert t["duplicates"] == [f"{DISPLAY}: 1 duplicate for sale", "#3 Mon 3 · Main ×1 · 100 kr"]


# --------------------------------------------------------------------------
# The page
# --------------------------------------------------------------------------
def _page(client, query="", mutate=None):
    with db_module.SessionLocal() as db:
        build(db, collection="151 Collection")
        if mutate:
            mutate(db)
            db.commit()
        coll_id = db.query(Collection).filter_by(name="151 Collection").one().id
    return client.get(f"/collections/{coll_id}{query}").text


def _bars(html):
    return re.findall(r'<div class="progress-bar( progress-bar-mini)?" role="progressbar" aria-label="([^"]+)"'
                      r' aria-valuemin="0" aria-valuemax="(\d+)" aria-valuenow="(\d+)"', html)


def test_progress_bars_and_left_per_track(client):
    html = _page(client)
    block = html.split('id="set-ja-sv2a"', 1)[1]
    bars = _bars(block)
    # The master-set bar first, under the heading, equal to the KPI.
    assert bars[0] == ("", f"Master set progress, {DISPLAY}", "11", "7")
    assert block.index('id="master-set-progress-ja-sv2a"') < block.index("master-set-kpis")
    assert "<strong>7 / 11</strong>" in block
    # One mini-bar per counted track, never Master Ball, each with "N left".
    assert bars[1:4] == [
        (" progress-bar-mini", "Main progress", "5", "4"),
        (" progress-bar-mini", "Secret progress", "2", "1"),
        (" progress-bar-mini", "Poké Ball progress", "4", "2"),
    ]
    kpis = block.split("master-set-kpis", 1)[1].split("</div>\n\n", 1)[0]
    assert re.findall(r'<span class="muted track-left">(\d+) left</span>', kpis) == ["1", "1", "2"]
    assert "Master Ball progress" not in html
    # The width is the share owned.
    assert 'style="width: 63.64%"' in block and 'style="width: 80.0%"' in block


def test_copy_lists_panel_sits_above_the_grid(client):
    html = _page(client)
    block = html.split('id="set-ja-sv2a"', 1)[1]
    order = [
        'id="master-set-progress-ja-sv2a"',
        "master-set-kpis",
        'id="copy-lists-ja-sv2a"',
        'id="set-grid-ja-sv2a"',
        'id="spares-ja-sv2a"',
        'id="unmatched-ja-sv2a"',
        'class="master-set-other"',
    ]
    positions = [block.index(marker) for marker in order]
    assert positions == sorted(positions)
    panel = block.split('id="copy-lists-ja-sv2a"', 1)[1].split('id="set-grid-ja-sv2a"', 1)[0]
    labels = re.findall(r'data-copy-list="(\w+)"', panel)
    assert labels == ["owned", "missing", "duplicates"]
    assert "Duplicates for sale" in panel
    assert 'aria-live="polite"' in panel
    assert "<summary>Show text</summary>" in panel and "readonly" in panel
    assert "Make a finn.no ad from these cards" in panel
    # The old stand-alone card is gone, and the copy script is loaded.
    assert "Copy missing list" not in html and "missing-list-" not in html
    assert "navigator.clipboard" not in html
    assert '<script src="/static/master-set.js" defer></script>' in html


def test_copy_script_is_served(client):
    js = client.get("/static/master-set.js").text
    assert "navigator.clipboard.writeText" in js and "Copied" in js and "Ctrl/Cmd+C" in js


def test_duplicates_table_collapsed_by_default(client):
    html = _page(client)
    assert re.search(r'<details class="collapsible master-set-spares" id="spares-ja-sv2a">\s*'
                     r'<summary>Duplicates · 5 · 270 kr</summary>', html)
    assert '<div class="table-scroll">\n  <table class="master-set-spares">' in html


def test_unmatched_only_when_there_is_any(client):
    def mutate(db):
        for c in db.query(Card).filter(Card.name.in_(["Unlinked Mon 2"])).all():
            c.qty = 0
        for c in db.query(Card).filter_by(card_id="jpn_sv2a-1", variant="Reverse Holo").all():
            c.qty = 0

    html = _page(client, mutate=mutate)
    assert 'id="unmatched-' not in html and 'href="#unmatched-' not in html
    assert "None: every card you own" not in html


def test_duplicates_filter_count_line_shows_prints_and_copies(client):
    def mutate(db):
        _card(db, "jpn_sv2a-1", "Master Ball Holo").qty = 2

    html = _page(client, "?set=ja:sv2a&show=duplicates", mutate=mutate)
    count = re.search(r'<p class="muted master-set-count">(.*?)</p>', html, re.S).group(1)
    # Track=All: 3 prints, 5 copies; the Master Ball spare (in the KPI's 6)
    # needs its track picked.
    assert re.sub(r"<[^>]+>", "", count).strip() == "3 prints · 5 duplicate copies (Master Ball prints: pick their track)"
    assert re.search(r">6 <span class=\"muted\">· 770 kr", html)
    html = _page_again(client, "?set=ja:sv2a&track=master_ball&show=duplicates")
    count = re.search(r'<p class="muted master-set-count">(.*?)</p>', html, re.S).group(1)
    assert re.sub(r"<[^>]+>", "", count).startswith("1 print · 1 duplicate copy · Master Ball")


def _page_again(client, query):
    with db_module.SessionLocal() as db:
        coll_id = db.query(Collection).filter_by(name="151 Collection").one().id
    return client.get(f"/collections/{coll_id}{query}").text


def test_collections_overview_has_a_mini_bar(client):
    _page(client)
    html = client.get("/collections").text
    assert _bars(html) == [(" progress-bar-mini", f"Master set progress, {DISPLAY}", "11", "7")]
