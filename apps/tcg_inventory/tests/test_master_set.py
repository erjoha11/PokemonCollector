"""Sets & lists module and the master-set page (issue #369, epic #366):
queries.master_set_detail, /collecting, /sets/{language}/{set_code} and the
"Master set" entry links. Offline.

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
def _session():
    import db as db_module

    return db_module.SessionLocal()


def _seed(with_checklist=True, collection=None):
    with _session() as db:
        build(db, with_checklist=with_checklist, collection=collection)


def _nav_active(html):
    return '<a href="/collecting" class="active">Sets &amp; lists</a>' in html


def test_nav_item_sits_between_inventory_and_orders(client):
    html = client.get("/").text
    assert html.index('href="/inventory"') < html.index('href="/collecting"') < html.index('href="/orders/purchased"')
    assert not _nav_active(html)


def test_collecting_empty_state(client):
    resp = client.get("/collecting")
    assert resp.status_code == 200
    assert _nav_active(resp.text)
    assert "No set is tracked yet" in resp.text
    assert "set_checklist_seed.py" in resp.text


def test_collecting_lists_tracked_sets(client):
    _seed()
    html = client.get("/collecting").text
    assert DISPLAY in html
    assert 'href="/sets/ja/sv2a"' in html
    assert "7 / 11" in html
    assert re.search(r">5 <span class=\"muted\">· 270 kr", html)


def test_set_page_without_checklist(client):
    _seed(with_checklist=False)
    resp = client.get("/sets/ja/sv2a")
    assert resp.status_code == 200
    assert _nav_active(resp.text)
    assert "has no checklist yet" in resp.text
    assert "set_checklist_seed.py --set ja:sv2a" in resp.text


def test_set_page_default_view(client):
    _seed()
    resp = client.get("/sets/ja/sv2a")
    assert resp.status_code == 200
    html = resp.text
    assert _nav_active(html)
    assert f"<h1>{DISPLAY}</h1>" in html
    assert "Korean, logged in Dex as Japanese <code>sv2a</code>" in html
    assert "7 / 11" in html and "4 / 5" in html and "1 / 2" in html and "2 / 4" in html
    # Master Ball: not in the KPIs, not in the default grid.
    assert "never part of the master set" not in html
    assert "#1 · Master Ball" not in html
    assert "Master Ball prints hidden" in html
    # 11 master-set prints in the grid: 7 owned, 4 ghosted.
    assert html.count('class="gallery-card gallery-card-missing"') == 4
    assert "/cards/None" not in html
    # A ghost has no image here, so it shows the placeholder.
    assert "#2 · Poké Ball</div>" in html
    # Unmatched and spares sections.
    assert "jpn_sv2a-1" in html and "Reverse Holo" in html and "Unlinked Mon 2" in html
    assert "Other set" not in html and "International" not in html
    assert "/sales?card_ids=" in html


def test_set_page_spares_link_and_missing_text(client):
    _seed()
    html = client.get("/sets/ja/sv2a").text
    with _session() as db:
        ids = {
            db.query(Card).filter_by(card_id="jpn_sv2a-1", variant="Normal").one().id,
            db.query(Card).filter_by(card_id="jpn_sv2a-4", variant="Poké Ball Holo").one().id,
        }
    url = re.search(r'href="(/sales\?[^"]+)"', html).group(1)
    got = {int(x) for x in re.findall(r"card_ids=(\d+)", url)}
    assert ids <= got and len(got) == 3  # one Card per spare print
    text = re.search(r'<textarea id="missing-list-text"[^>]*>(.*?)</textarea>', html, re.S).group(1)
    assert text.splitlines() == [
        f"{DISPLAY}: missing 4 of 11",
        "#2 Mon 2 · Poké Ball",
        "#4 Mon 4",
        "#5 Mon 5 · Poké Ball",
        "#7 Mon 7",
    ]


@pytest.mark.parametrize(
    "query, ghosts, owned",
    [
        ("?show=missing", 4, 0),
        ("?show=owned", 0, 7),
        ("?track=main", 1, 4),
        ("?track=poke_ball&show=missing", 2, 0),
        ("?track=master_ball", 3, 1),
        ("?track=bogus&show=bogus", 4, 7),  # unknown values fall back to the defaults
    ],
)
def test_set_page_filters(client, query, ghosts, owned):
    _seed()
    html = client.get("/sets/ja/sv2a" + query).text
    grid = html.split('id="set-grid"', 1)[1].split('id="missing-list"', 1)[0]
    assert grid.count('class="gallery-card gallery-card-missing"') == ghosts
    assert grid.count('class="gallery-card"') == owned
    assert "/cards/None" not in html


def test_set_page_filter_pills_push_the_url(client):
    _seed()
    html = client.get("/sets/ja/sv2a?track=poke_ball").text
    # Picking Show keeps the track, and vice versa; defaults drop out of the URL.
    assert 'hx-get="/sets/ja/sv2a?track=poke_ball&amp;show=missing"' in html
    assert 'hx-get="/sets/ja/sv2a?show=missing"' not in html
    assert 'href="/sets/ja/sv2a"' in html  # "All" track, default show
    assert html.count('hx-push-url="true"') >= 8
    assert 'hx-select="#set-grid"' in html
    assert re.search(r'class="viz-filter-pill active" aria-current="true">Poké Ball<', html)
    # Master Ball's own figure only when its track is picked, inside the
    # swapped region so the pill updates it.
    mb = client.get("/sets/ja/sv2a?track=master_ball").text
    grid = mb.split('id="set-grid"', 1)[1]
    assert "(not collected, never part of the master set)</span>: 1 / 4 owned" in grid


def test_set_page_htmx_request_still_has_the_grid(client):
    """hx-select picks #set-grid out of the full page, so an htmx request
    must get the same page back."""
    _seed()
    html = client.get("/sets/ja/sv2a?show=owned", headers={"HX-Request": "true"}).text
    assert 'id="set-grid"' in html


# --------------------------------------------------------------------------
# Entry points
# --------------------------------------------------------------------------
@pytest.mark.parametrize("with_checklist", [True, False])
def test_collection_page_master_set_link_only_with_a_checklist(client, with_checklist):
    _seed(with_checklist=with_checklist, collection="151 Collection")
    with _session() as db:
        coll_id = db.query(Collection).one().id
    html = client.get(f"/collections/{coll_id}").text
    assert ('<a href="/sets/ja/sv2a">Master set</a>' in html) is with_checklist
    assert "/cards/None" not in html


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
