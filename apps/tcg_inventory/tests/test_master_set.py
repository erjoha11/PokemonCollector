"""Master sets (issue #369, epic #366) and Collections as the entry point
(#378): queries.master_set_detail, the home rule (queries.set_homes), the
/collections overview, the master-set block on /collections/{id}, the
/sets/{language}/{set_code} redirect and fallback page, the nav, and the
other entry links. Offline.

The fixture is prod's sv2a shape at small scale (#366 "Verified facts"):
main 1-5 (#3 an ex, no ball prints), secret 6-7, Poké Ball and Master Ball
prints of every non-ex main number. Owned: #4 only as Poké Ball (so main is
4/5 like prod's 164/165), #7 sold (qty 0, so missing), a Master Ball print
(listed, never counted), two Dex rows on the #3 print (spares per master
card, not per row), plus two unmatched cards: a Reverse Holo with no slot
and an unlinked row.
"""
from __future__ import annotations

import re

import pytest

import queries
from models import Card, Collection, MasterCard, SetChecklist, SetChecklistCard

SET_NAME = "Pokémon Card 151"
SERIES = "Scarlet & Violet: 151 JP/KR"
DISPLAY = "Pokémon Card 151 (Korean)"

# (number, variant, track) -- the checklist.
PRINTS = (
    [(n, "holo" if n in (3, 5) else "normal", "main") for n in range(1, 6)]
    + [(n, "holo", "secret") for n in (6, 7)]
    + [(n, "poke_ball_holo", "poke_ball") for n in (1, 2, 4, 5)]
    + [(n, "master_ball_holo", "master_ball") for n in (1, 2, 4, 5)]
)
LABELS = {
    "normal": "Normal",
    "holo": "Holo",
    "poke_ball_holo": "Poké Ball Holo",
    "master_ball_holo": "Master Ball Holo",
    "reverse_holo": "Reverse Holo",
}
# (number, variant, qty, price) -- owned Dex rows, linked to the print's master.
OWNED = [
    (1, "normal", 3, 10.0),
    (2, "normal", 1, 10.0),
    (3, "holo", 2, 100.0),
    (3, "holo", 1, 100.0),  # a second Dex row on the same print (Dex variant "Holo Rare")
    (5, "holo", 1, 20.0),
    (6, "holo", 1, 300.0),
    (7, "holo", 0, 400.0),  # sold: the print is missing
    (1, "poke_ball_holo", 1, 30.0),
    (4, "poke_ball_holo", 2, 50.0),  # #4 only as Poké Ball
    (1, "master_ball_holo", 1, 500.0),  # not collected, never counts
]


def _card(master, n, variant, qty, price, **kw):
    return Card(
        card_id=kw.pop("card_id", f"jpn_sv2a-{n}"),
        name=kw.pop("name", f"Mon {n}"),
        number=f"{n:03d}/165",
        variant=kw.pop("dex_variant", LABELS.get(variant, variant)),
        language="Korean",
        series=SERIES,
        set=SET_NAME,
        qty=qty,
        market_price=price,
        price_flags="",
        master_card=master,
        **kw,
    )


def build(db, with_checklist=True, collection=None):
    masters = {}
    for n, variant, _track in PRINTS:
        masters[(n, variant)] = MasterCard(
            language="ja", set_code="sv2a", number=str(n), variant=variant,
            variant_label=LABELS[variant], name=f"Mon {n}",
        )
    masters[(1, "reverse_holo")] = MasterCard(
        language="ja", set_code="sv2a", number="1", variant="reverse_holo", variant_label="Reverse Holo"
    )
    db.add_all(masters.values())
    cards = [
        _card(masters[(n, v)], n, v, qty, price, **({"dex_variant": "Holo Rare"} if i == 3 else {}))
        for i, (n, v, qty, price) in enumerate(OWNED)
    ]
    cards += [
        _card(masters[(1, "reverse_holo")], 1, "reverse_holo", 1, 5.0),  # unmatched: no slot
        _card(None, 2, "normal", 1, 10.0, name="Unlinked Mon 2", dex_variant="Unlinked"),  # unmatched: never linked
        # Neither is part of ja/sv2a: another set, and the int "sv2a" code.
        _card(None, 1, "normal", 1, 1.0, card_id="jpn_sv2-1", name="Other set"),
        _card(None, 1, "normal", 1, 1.0, card_id="sv2a-1", name="International"),
    ]
    if collection is not None:
        coll = Collection(name=collection, priority_rank=1)
        db.add(coll)
        for c in cards:
            c.collections = [coll]
    db.add_all(cards)
    if with_checklist:
        cl = SetChecklist(language="ja", set_code="sv2a", display_name=DISPLAY, source="tcgdex", source_set_id="SV2a")
        db.add(cl)
        for n, variant, track in PRINTS:
            cl.cards.append(
                SetChecklistCard(master_card=masters[(n, variant)], track=track, counts_toward_completion=track != "master_ball")
            )
    db.commit()
    return cards


# --------------------------------------------------------------------------
# queries.master_set_detail
# --------------------------------------------------------------------------
def test_tracks_master_set_and_master_ball_excluded(db_session):
    build(db_session)
    d = queries.master_set_detail(db_session, "ja", "sv2a")
    tracks = {t.key: (t.owned, t.total, t.counts) for t in d.tracks}
    assert tracks == {
        "main": (4, 5, True),  # #4 owned only as Poké Ball
        "secret": (1, 2, True),  # #7 sold
        "poke_ball": (2, 4, True),
        "master_ball": (1, 4, False),
    }
    assert (d.master_set.owned, d.master_set.total) == (7, 11)  # 5 + 2 + 4, no Master Ball
    assert d.korean_proxy is True
    assert d.checklist.display_name == DISPLAY


def test_track_figures_never_pass_100_percent(db_session):
    """Many copies and rows on one print still make it one owned print."""
    cards = build(db_session)
    for c in cards:
        c.qty = c.qty + 5
    db_session.commit()
    d = queries.master_set_detail(db_session, "ja", "sv2a")
    assert all(t.owned <= t.total and t.pct <= 100 for t in [*d.tracks, d.master_set])
    assert (d.master_set.owned, d.master_set.total) == (8, 11)  # #7 now owned; still missing: main #4, Poké Ball #2 and #5


def test_spares_are_counted_per_master_card(db_session):
    build(db_session)
    d = queries.master_set_detail(db_session, "ja", "sv2a")
    spares = {(s.master.number, s.track): s.spares for s in d.spare_slots}
    # #1: 3-1; #3: two rows (2+1)-1 = 2, not (2-1)+(1-1) = 1; PB #4: 2-1.
    assert spares == {("1", "main"): 2, ("3", "main"): 2, ("4", "poke_ball"): 1}
    assert d.spares == 5
    assert d.spare_value == 2 * 10 + 2 * 100 + 1 * 50


def test_unmatched_cards(db_session):
    build(db_session)
    d = queries.master_set_detail(db_session, "ja", "sv2a")
    assert sorted((c.card_id, c.variant, c.name) for c in d.unmatched) == [
        ("jpn_sv2a-1", "Reverse Holo", "Mon 1"),
        ("jpn_sv2a-2", "Unlinked", "Unlinked Mon 2"),
    ]


def test_missing_in_printed_order_and_number_labels(db_session):
    build(db_session)
    d = queries.master_set_detail(db_session, "ja", "sv2a")
    assert [(s.number_label, s.track) for s in d.missing] == [
        ("#2", "poke_ball"),
        ("#4", "main"),
        ("#5", "poke_ball"),
        ("#7", "secret"),
    ]
    # Printed order: by number, base print before its ball prints.
    assert [(s.master.number, s.track) for s in d.slots[:3]] == [("1", "main"), ("1", "poke_ball"), ("1", "master_ball")]


def test_number_labels_pad_to_the_widest_number(db_session):
    build(db_session)
    extra = MasterCard(language="ja", set_code="sv2a", number="210", variant="holo", name="Mew")
    cl = db_session.query(SetChecklist).one()
    cl.cards.append(SetChecklistCard(master_card=extra, track="secret", counts_toward_completion=True))
    db_session.commit()
    d = queries.master_set_detail(db_session, "ja", "sv2a")
    assert d.slots[0].number_label == "#001"
    assert d.slots[-1].number_label == "#210"


def test_no_checklist_is_none(db_session):
    build(db_session, with_checklist=False)
    assert queries.master_set_detail(db_session, "ja", "sv2a") is None
    assert queries.tracked_sets(db_session) == []
    assert queries.checklist_keys(db_session) == set()


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------
HX = {"HX-Request": "true"}


def _session():
    import db as db_module

    return db_module.SessionLocal()


def _seed(with_checklist=True, collection=None):
    with _session() as db:
        build(db, with_checklist=with_checklist, collection=collection)


def _coll_id(name):
    with _session() as db:
        return db.query(Collection).filter_by(name=name).one().id


def _active_nav(html):
    nav = html.split("<nav>", 1)[1].split("</nav>", 1)[0]
    return re.findall(r'<a href="([^"]+)" class="active">([^<]+)</a>', nav)


def _grid(html, key="ja-sv2a"):
    return html.split(f'id="set-grid-{key}"', 1)[1].split(f'id="missing-list-{key}"', 1)[0]


def _tag(db, name, cards, rank=1):
    coll = Collection(name=name, priority_rank=rank)
    db.add(coll)
    for card in cards:
        card.collections.append(coll)
    db.commit()
    return coll


def _add_second_set(db, collection_name):
    """A second checklisted set (ja/s12a, two main prints, #1 owned twice)
    filed in the same collection under its own Dex set name."""
    coll = db.query(Collection).filter_by(name=collection_name).one()
    masters = [
        MasterCard(language="ja", set_code="s12a", number=str(n), variant="normal", variant_label="Normal", name=f"Uni {n}")
        for n in (1, 2)
    ]
    cl = SetChecklist(language="ja", set_code="s12a", display_name="VSTAR Universe", source="tcgdex", source_set_id="S12a")
    for m in masters:
        cl.cards.append(SetChecklistCard(master_card=m, track="main", counts_toward_completion=True))
    card = Card(
        card_id="jpn_s12a-1", name="Uni 1", number="001/172", variant="Normal", language="Japanese",
        series="Sword & Shield", set="VSTAR Universe", qty=2, market_price=5.0, price_flags="", master_card=masters[0],
    )
    card.collections = [coll]
    db.add_all([*masters, cl, card])
    db.commit()


# --------------------------------------------------------------------------
# Nav and the old Sets & lists URL
# --------------------------------------------------------------------------
def test_nav_shows_collections_between_inventory_and_orders(client):
    html = client.get("/").text
    nav = html.split("<nav>", 1)[1].split("</nav>", 1)[0]
    assert nav.index('href="/inventory"') < nav.index('href="/collections"') < nav.index('href="/orders/purchased"')
    assert ">Collections</a>" in nav
    assert "Sets &amp; lists" not in html and "/collecting" not in html
    assert _active_nav(html) == [("/", "Dashboard")]


def test_exactly_one_nav_item_active_on_collections_pages(client):
    _seed(collection="151 Collection")
    coll_id = _coll_id("151 Collection")
    resp = client.post("/lists", data={"name": "Wants", "kind": "want"}, headers=HX)
    list_id = int(resp.headers["HX-Redirect"].rsplit("/", 1)[1])
    for path in ("/collections", f"/collections/{coll_id}", f"/lists/{list_id}"):
        assert _active_nav(client.get(path).text) == [("/collections", "Collections")], path
    assert _active_nav(client.get("/inventory").text) == [("/inventory", "Inventory")]


def test_collecting_redirects_to_collections(client):
    resp = client.get("/collecting", follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"] == "/collections"


# --------------------------------------------------------------------------
# The home rule (queries.set_homes)
# --------------------------------------------------------------------------
def test_home_is_the_collection_with_most_owned_cards(db_session):
    cards = build(db_session)
    _tag(db_session, "A", cards[0:2])
    b = _tag(db_session, "B", cards[2:5])
    assert queries.set_homes(db_session) == {("ja", "sv2a"): b.id}


def test_home_tie_goes_to_the_lowest_collection_id(db_session):
    cards = build(db_session)
    first = _tag(db_session, "Zeta", cards[3:5])  # created first: lowest id, sorts last by name
    _tag(db_session, "Alpha", cards[0:2])
    assert queries.set_homes(db_session) == {("ja", "sv2a"): first.id}


def test_home_counts_only_owned_cards_of_the_set(db_session):
    cards = build(db_session)
    # Three tags, but on a sold card (qty 0) and two cards of other sets.
    _tag(db_session, "Sold and others", [cards[6], cards[12], cards[13]])
    real = _tag(db_session, "Real", [cards[0]])
    assert queries.set_homes(db_session) == {("ja", "sv2a"): real.id}


def test_no_home_without_tags_or_checklist(db_session):
    cards = build(db_session)
    assert queries.set_homes(db_session) == {}  # nothing tagged
    _tag(db_session, "A", cards[:1])
    assert queries.set_homes(db_session, set()) == {}
    assert queries.set_homes(db_session, {("ja", "s12a")}) == {}


# --------------------------------------------------------------------------
# /collections overview
# --------------------------------------------------------------------------
def test_collections_overview(client):
    _seed(collection="151 Collection")
    with _session() as db:
        cards = db.query(Card).order_by(Card.id).all()
        _tag(db, "Illustrator", [cards[0]], rank=2)  # #1 Normal, qty 3
        _tag(db, "Venter", [], rank=3)  # empty: still listed
    home, illu, venter = _coll_id("151 Collection"), _coll_id("Illustrator"), _coll_id("Venter")
    html = client.get("/collections").text
    assert "<h1>Collections</h1>" in html
    rows = {
        cid: html.split(f'<td><a href="/collections/{cid}">', 1)[1].split("</tr>", 1)[0]
        for cid in (home, illu, venter)
    }
    cells = {cid: re.findall(r'<td class="num">(.*?)</td>', row) for cid, row in rows.items()}
    # 151 Collection: every card (17 copies), home to sv2a, 5 per-print duplicates.
    assert cells[home][0] == "17"
    assert f'<a href="/collections/{home}#set-ja-sv2a" title="{DISPLAY}">7/11</a>' in cells[home][2]
    assert cells[home][3] == "5"
    # Illustrator holds sv2a cards but isn't its home: "–", per-card duplicates (3 - 1).
    assert cells[illu][0] == "3" and "–" in cells[illu][2] and cells[illu][3] == "2"
    assert cells[venter][0] == "0" and "–" in cells[venter][2] and cells[venter][3] == "0"
    # The lists section moved here.
    assert 'id="lists"' in html and 'hx-post="/lists"' in html


def test_collections_overview_empty(client):
    html = client.get("/collections").text
    assert "No collections yet" in html
    assert "No want lists yet." in html


# --------------------------------------------------------------------------
# /sets/... redirects and the fallback page
# --------------------------------------------------------------------------
def test_set_page_redirects_to_the_home_collection(client):
    _seed(collection="151 Collection")
    coll_id = _coll_id("151 Collection")
    cases = {
        "/sets/ja/sv2a?show=missing": f"/collections/{coll_id}?set=ja:sv2a&show=missing#set-ja-sv2a",
        "/sets/ja/sv2a": f"/collections/{coll_id}?set=ja:sv2a#set-ja-sv2a",
        "/sets/ja/sv2a?track=poke_ball&show=owned": f"/collections/{coll_id}?set=ja:sv2a&track=poke_ball&show=owned#set-ja-sv2a",
        "/sets/ja/sv2a?at=unmatched": f"/collections/{coll_id}?set=ja:sv2a#unmatched-ja-sv2a",
    }
    for path, location in cases.items():
        resp = client.get(path, follow_redirects=False)
        assert resp.status_code == 302, path
        assert resp.headers["location"] == location, path
    followed = client.get("/sets/ja/sv2a?show=missing")
    assert followed.status_code == 200
    assert _grid(followed.text).count('class="gallery-card"') == 0


@pytest.mark.parametrize("case", ["untagged", "zero owned"])
def test_set_page_fallback_when_no_collection_holds_the_set(client, case):
    _seed(collection=None if case == "untagged" else "151 Collection")
    if case == "zero owned":
        with _session() as db:
            for card in db.query(Card):
                card.qty = 0
            db.commit()
    resp = client.get("/sets/ja/sv2a", follow_redirects=False)
    assert resp.status_code == 200
    html = resp.text
    assert _active_nav(html) == [("/collections", "Collections")]
    assert '<p class="muted breadcrumb"><a href="/collections">Collections</a></p>' in html
    assert f"<h1>{DISPLAY}</h1>" in html
    assert 'id="set-grid-ja-sv2a"' in html
    assert 'hx-get="/sets/ja/sv2a?show=missing"' in html  # pills stay on this URL
    assert ("7 / 11" if case == "untagged" else "0 / 11") in html
    assert 'name="collection_id"' not in html
    assert "/cards/None" not in html


def test_set_page_without_checklist(client):
    _seed(with_checklist=False, collection="151 Collection")
    resp = client.get("/sets/ja/sv2a", follow_redirects=False)
    assert resp.status_code == 200
    assert _active_nav(resp.text) == [("/collections", "Collections")]
    assert "has no checklist yet" in resp.text
    assert "set_checklist_seed.py --set ja:sv2a" in resp.text


# --------------------------------------------------------------------------
# /collections/{id}: the home section's master-set block
# --------------------------------------------------------------------------
def _home_page(client, query=""):
    _seed(collection="151 Collection")
    coll_id = _coll_id("151 Collection")
    return coll_id, client.get(f"/collections/{coll_id}{query}").text


def test_home_collection_shows_the_master_set_block(client):
    coll_id, html = _home_page(client)
    assert '<p class="muted breadcrumb"><a href="/collections">Collections</a></p>' in html
    assert 'id="set-ja-sv2a"' in html
    assert "Master set · all your sv2a cards" in html
    assert "Korean, logged in Dex as Japanese <code>sv2a</code>" in html
    assert "7 / 11" in html and "4 / 5" in html and "1 / 2" in html and "2 / 4" in html
    # Duplicates = #369's spares, per print, with its tooltip.
    assert re.search(r"Duplicates <span class=\"info-icon[^>]*aria-label=\"Every copy beyond the first of each print", html)
    assert re.search(r">5 <span class=\"muted\">· 270 kr", html)
    # Master Ball: not in the KPIs, not in the default grid.
    assert "never part of the master set" not in html
    assert "Master Ball prints hidden" in html
    grid = _grid(html)
    assert grid.count('class="gallery-card gallery-card-missing"') == 4
    assert grid.count('class="gallery-card"') == 7
    assert "/cards/None" not in html
    assert "#2 · Poké Ball</div>" in html
    # Unmatched and duplicates sections, finn.no ad link kept.
    assert 'id="unmatched-ja-sv2a"' in html and "Unlinked Mon 2" in html
    assert 'id="spares-ja-sv2a"' in html and "/sales?card_ids=" in html
    # Cards filed under the same Dex set name but of other sets.
    other = html.split('class="master-set-other"', 1)[1]
    assert "Other set" in other and "International" in other
    # Per-Dex-row figures and the self-referencing link are gone: the only
    # section is a master set.
    assert "Unique cards" not in html and "Completion" not in html and "numbers</span>" not in html
    assert "owned=0" not in html
    assert ">Master set</a>" not in html
    assert "Total value" in html and "Gain / loss" in html


def test_home_block_spares_link_and_missing_text(client):
    _, html = _home_page(client)
    with _session() as db:
        ids = {
            db.query(Card).filter_by(card_id="jpn_sv2a-1", variant="Normal").one().id,
            db.query(Card).filter_by(card_id="jpn_sv2a-4", variant="Poké Ball Holo").one().id,
        }
    url = re.search(r'href="(/sales\?[^"]+)"', html).group(1)
    got = {int(x) for x in re.findall(r"card_ids=(\d+)", url)}
    assert ids <= got and len(got) == 3  # one Card per print with duplicates
    text = re.search(r'<textarea id="missing-list-text-ja-sv2a"[^>]*>(.*?)</textarea>', html, re.S).group(1)
    assert text.splitlines() == [
        f"{DISPLAY}: missing 4 of 11",
        "#2 Mon 2 · Poké Ball",
        "#4 Mon 4",
        "#5 Mon 5 · Poké Ball",
        "#7 Mon 7",
    ]
    assert "document.getElementById('missing-list-text-ja-sv2a')" in html


@pytest.mark.parametrize(
    "query, ghosts, owned",
    [
        ("?set=ja:sv2a&show=missing", 4, 0),
        ("?set=ja:sv2a&show=owned", 0, 7),
        ("?set=ja:sv2a&show=duplicates", 0, 3),  # main #1, main #3, Poké Ball #4
        ("?set=ja:sv2a&track=main", 1, 4),
        ("?set=ja:sv2a&track=poke_ball&show=missing", 2, 0),
        ("?set=ja:sv2a&track=master_ball", 3, 1),
        ("?set=ja:sv2a&track=bogus&show=bogus", 4, 7),  # unknown values fall back to the defaults
        ("?set=ja:other&show=missing", 4, 7),  # the filter names another set: this block at defaults
        ("?show=missing", 4, 7),  # no set named: defaults
    ],
)
def test_home_block_filters(client, query, ghosts, owned):
    _, html = _home_page(client, query)
    grid = _grid(html)
    assert grid.count('class="gallery-card gallery-card-missing"') == ghosts
    assert grid.count('class="gallery-card"') == owned
    assert "/cards/None" not in html


def test_home_block_pills_push_collection_urls(client):
    coll_id, html = _home_page(client, "?set=ja:sv2a&track=poke_ball")
    base = f"/collections/{coll_id}"
    # Picking Show keeps the track, and vice versa; defaults drop out of the URL.
    assert f'hx-get="{base}?set=ja:sv2a&amp;track=poke_ball&amp;show=missing"' in html
    assert f'hx-get="{base}?set=ja:sv2a&amp;track=poke_ball&amp;show=duplicates"' in html
    assert f'href="{base}"' in html  # "All" track, default show
    assert "/sets/ja/sv2a?" not in html
    assert html.count('hx-push-url="true"') >= 9
    assert 'hx-select="#set-grid-ja-sv2a" hx-target="#set-grid-ja-sv2a"' in html
    assert re.search(r'class="viz-filter-pill active" aria-current="true">Poké Ball<', html)
    mb = client.get(f"{base}?set=ja:sv2a&track=master_ball").text
    assert "(not collected, never part of the master set)</span>: 1 / 4 owned" in _grid(mb)


def test_home_block_htmx_request_still_has_the_grid(client):
    _seed(collection="151 Collection")
    coll_id = _coll_id("151 Collection")
    html = client.get(f"/collections/{coll_id}?set=ja:sv2a&show=owned", headers=HX).text
    assert 'id="set-grid-ja-sv2a"' in html


def test_home_block_badges_owned_tiles_filed_elsewhere(client):
    _seed(collection="151 Collection")
    with _session() as db:
        home = db.query(Collection).one()
        six = db.query(Card).filter_by(card_id="jpn_sv2a-6").one()
        five = db.query(Card).filter_by(card_id="jpn_sv2a-5").one()
        six.collections = []
        five.collections = []
        db.commit()
        _tag(db, "Binder B", [six], rank=2)
        home_id = home.id
    html = client.get(f"/collections/{home_id}").text
    grid = _grid(html)
    # Still owned (counted set-wide), so never Missing.
    assert "7 / 11" in html
    assert grid.count('class="gallery-card gallery-card-missing"') == 4
    assert re.search(r"elsewhere-badge\" [^>]*>in: Binder B</span>", grid)
    assert re.search(r"elsewhere-badge\" [^>]*>in: no collection</span>", grid)
    assert grid.count("elsewhere-badge") == 2


def test_non_home_collection_shows_its_gallery_and_a_link(client):
    _seed(collection="151 Collection")
    with _session() as db:
        card = db.query(Card).filter_by(card_id="jpn_sv2a-1", variant="Normal").one()
        _tag(db, "Illustrator", [card], rank=2)
    home, illu = _coll_id("151 Collection"), _coll_id("Illustrator")
    html = client.get(f"/collections/{illu}").text
    assert 'id="set-grid-' not in html and "master-set-kpis" not in html
    assert f'Master set 7/11 → <a href="/collections/{home}#set-ja-sv2a">151 Collection</a>' in html
    assert "Unique cards" in html and "Mon 1" in html
    assert "outside master sets" not in html


def test_home_with_a_plain_section_scopes_the_per_row_kpis_to_it(client):
    _seed(collection="151 Collection")
    with _session() as db:
        coll = db.query(Collection).one()
        extra = Card(card_id="swsh1-5", name="Plain Mon", number="5/202", variant="Normal", language="English",
                     series="Sword & Shield", set="Sword & Shield", qty=3, market_price=2.0, price_flags="")
        extra.collections = [coll]
        db.add(extra)
        db.commit()
        coll_id = coll.id
    html = client.get(f"/collections/{coll_id}").text
    kpis = html.split('class="card collection-kpis"', 1)[1].split("</div>\n\n", 1)[0]
    # Only the plain section's row: 1 unique card, 2 duplicates (3 - 1).
    assert re.search(r'Unique cards</span><span class="tx-kpi-value">1 <span class="muted">outside master sets</span>', kpis)
    assert re.search(r'<span class="tx-kpi-value">2 <span class="muted">outside master sets</span>', kpis)
    assert 'id="set-grid-ja-sv2a"' in html and "Plain Mon" in html
    assert f'href="/collections/{coll_id}?owned=0"' in html


def test_collection_without_a_checklist_renders_as_before(client):
    _seed(with_checklist=False, collection="151 Collection")
    coll_id = _coll_id("151 Collection")
    html = client.get(f"/collections/{coll_id}").text
    assert "Unique cards" in html and "Completion" in html and "Duplicates" in html
    assert 'id="set-grid-' not in html and "Master set" not in html
    assert f'href="/collections/{coll_id}?owned=0"' in html
    assert "/cards/None" not in html


# --------------------------------------------------------------------------
# Two checklisted sets on one page
# --------------------------------------------------------------------------
def _two_sets(client, query=""):
    _seed(collection="151 Collection")
    with _session() as db:
        _add_second_set(db, "151 Collection")
    coll_id = _coll_id("151 Collection")
    return coll_id, client.get(f"/collections/{coll_id}{query}").text


def test_two_checklisted_sections_have_unique_ids(client):
    _, html = _two_sets(client)
    ids = re.findall(r'\sid="([^"]+)"', html)
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    assert dupes == []
    for key in ("ja-sv2a", "ja-s12a"):
        for prefix in ("set", "set-grid", "set-list-add", "set-list-add-status", "missing-list-text", "spares", "unmatched"):
            assert f'id="{prefix}-{key}"' in html, (prefix, key)
        assert f'hx-target="#set-list-add-{key}"' in html
        assert f'hx-select="#set-grid-{key}" hx-target="#set-grid-{key}"' in html
        assert f"document.getElementById('missing-list-text-{key}')" in html
    assert 'action="/sets/ja/s12a/add-to-list"' in html and 'action="/sets/ja/sv2a/add-to-list"' in html


def test_one_active_filter_per_page(client):
    coll_id, html = _two_sets(client, "?set=ja:s12a&show=missing")
    s12a = _grid(html, "ja-s12a")
    assert s12a.count('class="gallery-card gallery-card-missing"') == 1
    assert s12a.count('class="gallery-card"') == 0
    sv2a = _grid(html)  # the other block renders at its defaults
    assert sv2a.count('class="gallery-card gallery-card-missing"') == 4
    assert sv2a.count('class="gallery-card"') == 7
    # sv2a's pills name sv2a and drop the s12a filter.
    assert f'hx-get="/collections/{coll_id}?set=ja:sv2a&amp;show=missing"' in html
    assert f'hx-get="/collections/{coll_id}?set=ja:s12a&amp;show=owned"' in html


def test_add_to_list_acts_on_its_own_section(client):
    from models import CardListItem

    coll_id, _ = _two_sets(client)
    resp = client.post(
        "/sets/ja/s12a/add-to-list",
        data={"what": "missing", "list_id": "new", "new_list_name": "Universe wants", "collection_id": str(coll_id)},
        headers=HX,
    )
    assert resp.status_code == 200
    assert 'id="set-list-add-ja-s12a"' in resp.text and 'id="set-list-add-status-ja-s12a"' in resp.text
    assert f'name="collection_id" value="{coll_id}"' in resp.text
    assert "Added 1: " in resp.text
    with _session() as db:
        assert [(i.master_card.set_code, i.master_card.number) for i in db.query(CardListItem)] == [("s12a", "2")]
    # Without htmx: back to the collection, with the filter and the anchor.
    plain = client.post(
        "/sets/ja/s12a/add-to-list",
        data={"what": "missing", "list_id": "1", "show": "missing", "collection_id": str(coll_id)},
        follow_redirects=False,
    )
    assert plain.status_code == 303
    assert plain.headers["location"] == f"/collections/{coll_id}?set=ja:s12a&show=missing#set-ja-s12a"


# --------------------------------------------------------------------------
# Other entry points
# --------------------------------------------------------------------------
@pytest.mark.parametrize("with_checklist", [True, False])
def test_card_page_set_link_only_with_a_checklist(client, with_checklist):
    _seed(with_checklist=with_checklist)
    with _session() as db:
        card_id = db.query(Card).filter_by(card_id="jpn_sv2a-1", variant="Normal").one().id
        other_id = db.query(Card).filter_by(card_id="jpn_sv2-1").one().id
    for path in (f"/cards/{card_id}", f"/cards/{card_id}/panel"):
        html = client.get(path).text
        assert ('<a href="/sets/ja/sv2a" title="Master set' in html) is with_checklist
    assert "/sets/" not in client.get(f"/cards/{other_id}").text


def test_dashboard_has_no_master_set(client):
    _seed()
    html = client.get("/").text
    assert "/sets/ja/sv2a" not in html
    assert "Master set" not in html
