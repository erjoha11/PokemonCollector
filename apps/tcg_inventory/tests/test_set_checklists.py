"""Set checklists from TCGdex (issue #368): set_checklist_seed.py and
set_checklists.py. Offline, against a trimmed real TCGdex SV2a payload
(fixtures/tcgdex/ja_SV2a_checklist.json): #1 (normal + Poké/Master Ball),
#3 (an ex: holo only, no ball prints), #15 (a holo rare + both balls) and
#201 (a secret rare)."""
from __future__ import annotations

import copy
import datetime as dt
import json
from pathlib import Path

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.orm import sessionmaker

import db as db_module
import masterdata
import set_checklist_seed as seed
import set_checklists
import tcgdex_prices
from conftest import make_csv
from importer import import_dex_csv_files
from models import Card, MasterCard, MasterCardId, Set, SetChecklist, SetChecklistCard

FIXTURE = Path(__file__).parent / "fixtures" / "tcgdex" / "ja_SV2a_checklist.json"
SPEC = seed.KNOWN_SETS["ja:sv2a"]
NOW = dt.datetime(2026, 10, 8, 12, 0)


@pytest.fixture()
def payload():
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return data["set"], data["cards"]


def _import(db, rows):
    defaults = {"series": "Scarlet & Violet: 151 JP/KR", "set": "Pokémon Card 151", "qty": 1}
    csv = make_csv("My Collection", [{**defaults, **row} for row in rows])
    import_dex_csv_files(db, [("my.csv", csv)])


def _run(db, payload, **kwargs):
    set_payload, cards = payload
    return seed.seed_checklist(db, SPEC, set_payload, cards, now=kwargs.pop("now", NOW), **kwargs)


def _members(db):
    rows = db.query(SetChecklistCard).join(MasterCard).all()
    return {(m.master_card.number, m.master_card.variant): (m.track, m.counts_toward_completion) for m in rows}


def _card_state(db):
    """Every column of every card, plus collection links -- must not change."""
    cols = [c.name for c in Card.__table__.columns]
    cards = sorted(tuple(getattr(card, c) for c in cols) for card in db.query(Card).all())
    links = db.execute(text("SELECT card_id, collection_id FROM card_collections ORDER BY 1, 2")).fetchall()
    return cards, links


def test_plan_prints_from_fixture(payload):
    prints, skipped = seed.plan_prints(*payload)
    got = {(p.number, p.track, p.variant) for p in prints}
    assert got == {
        ("1", "main", "normal"),
        ("1", "poke_ball", "poke_ball_holo"),
        ("1", "master_ball", "master_ball_holo"),
        ("3", "main", "holo"),  # ex: no ball prints
        ("15", "main", "holo"),
        ("15", "poke_ball", "poke_ball_holo"),
        ("15", "master_ball", "master_ball_holo"),
        ("201", "secret", "holo"),
    }
    assert skipped == []


def test_plan_prints_skips_prints_outside_the_master_set(payload):
    set_payload, cards = copy.deepcopy(payload)
    cards[0]["variants_detailed"] += [
        {"type": "reverse", "size": "standard"},  # plain reverse
        {"type": "reverse", "size": "standard", "foil": "energy"},
        {"type": "normal", "size": "standard", "stamp": ["pokemon-center"]},
        {"type": "holo", "size": "jumbo"},
    ]
    prints, skipped = seed.plan_prints(set_payload, cards)
    assert len(prints) == 8
    assert len(skipped) == 4
    assert all(line.startswith("SV2a-001 ") for line in skipped)


def test_plan_prints_refuses_a_card_with_two_base_prints(payload):
    set_payload, cards = copy.deepcopy(payload)
    cards[1]["variants_detailed"].append({"type": "normal", "size": "standard"})
    with pytest.raises(seed.SeedError, match="exactly one normal/holo"):
        seed.plan_prints(set_payload, cards)


def test_seed_on_empty_database_creates_every_print(db_session, payload):
    result = _run(db_session, payload)

    assert result.checklist_created and result.changed
    assert dict(result.slots) == {"main": 3, "secret": 1, "poke_ball": 2, "master_ball": 2}
    assert result.base_total == 4
    assert result.masters_created == 8
    assert _members(db_session) == {
        ("1", "normal"): ("main", True),
        ("1", "poke_ball_holo"): ("poke_ball", True),
        ("1", "master_ball_holo"): ("master_ball", False),
        ("3", "holo"): ("main", True),
        ("15", "holo"): ("main", True),
        ("15", "poke_ball_holo"): ("poke_ball", True),
        ("15", "master_ball_holo"): ("master_ball", False),
        ("201", "holo"): ("secret", True),
    }
    checklist = db_session.query(SetChecklist).one()
    assert (checklist.language, checklist.set_code) == ("ja", "sv2a")
    assert checklist.display_name == "Pokémon Card 151 (Korean)"
    assert (checklist.source, checklist.source_set_id, checklist.fetched_at) == ("tcgdex", "SV2a", NOW)

    master = db_session.query(MasterCard).filter_by(number="201").one()
    assert master.rarity == "Special illustration rare"
    assert master.image_url == "https://assets.tcgdex.net/ja/SV/SV2a/201/low.webp"
    assert master.printed_number == "201/165"
    assert master.variant_label == "Holo"
    assert [(r.source, r.external_id, r.matched_by) for r in master.external_ids] == [
        ("tcgdex", "SV2a-201", masterdata.MATCHED_VERIFIED_NUMBER)
    ]
    assert db_session.query(Card).count() == 0


def test_dex_normal_and_holo_both_land_on_the_base_slot(db_session, payload):
    """Dex says Holo for #1 (TCGdex: normal) and Normal for #15 (TCGdex:
    holo): the existing master is the base slot either way, no parallel row."""
    _import(db_session, [
        {"id": "jpn_sv2a-1", "variant": "Holo", "name": "Bulbasaur", "number": "001/165"},
        {"id": "jpn_sv2a-1", "variant": "Poké Ball Holo", "name": "Bulbasaur", "number": "001/165"},
        {"id": "jpn_sv2a-3", "variant": "Holo", "name": "Venusaur ex", "number": "003/165"},
        {"id": "jpn_sv2a-15", "variant": "Normal", "name": "Beedrill", "number": "015/165", "qty": 3},
        {"id": "jpn_sv2a-201", "variant": "Holo", "name": "Charizard ex", "number": "201/165"},
    ])
    before = _card_state(db_session)

    result = _run(db_session, payload)

    assert result.unmatched == []
    assert result.owned_rows == 5 and result.owned_copies == 7
    assert dict(result.owned_on_slot) == {"main": 3, "poke_ball": 1, "secret": 1}
    assert result.masters_reused == 5 and result.masters_created == 3
    members = _members(db_session)
    assert members[("1", "holo")] == ("main", True)
    assert members[("15", "normal")] == ("main", True)
    assert ("1", "normal") not in members and ("15", "holo") not in members
    assert db_session.query(MasterCard).filter_by(number="1").count() == 3  # holo, poke ball, master ball
    # Existing names (Dex's English) are kept; empty rarity/image filled in.
    base_1 = db_session.query(MasterCard).filter_by(number="1", variant="holo").one()
    assert base_1.name == "Bulbasaur" and base_1.rarity == "Common"
    assert _card_state(db_session) == before


def test_unmatched_owned_cards_are_reported(db_session, payload):
    _import(db_session, [
        {"id": "jpn_sv2a-1", "variant": "Normal", "number": "001/165"},
        {"id": "jpn_sv2a-1", "variant": "Reverse Holo", "number": "001/165"},
        {"id": "jpn_sv2a-3", "variant": "Poké Ball Holo", "number": "003/165"},  # no such print
        {"id": "jpn_sv2a-201", "variant": "", "number": "201/165"},
    ])
    before = _card_state(db_session)

    result = _run(db_session, payload)

    assert result.unmatched == [
        "jpn_sv2a-1 (Reverse Holo)",
        "jpn_sv2a-3 (Poké Ball Holo)",
        "jpn_sv2a-201 (no variant)",
    ]
    assert sum(result.owned_on_slot.values()) == 1
    lines = seed.format_report(result)
    assert any("3 unmatched" in line for line in lines)
    assert "  unmatched: jpn_sv2a-1 (Reverse Holo)" in lines
    # The odd variants never become members.
    assert len(_members(db_session)) == 8
    assert _card_state(db_session) == before


def test_rerun_changes_nothing(db_session, payload):
    _import(db_session, [{"id": "jpn_sv2a-15", "variant": "Holo", "number": "015/165"}])
    _run(db_session, payload)
    counts = {model: db_session.query(model).count() for model in (MasterCard, MasterCardId, SetChecklistCard)}
    before = _card_state(db_session)

    again = _run(db_session, payload, now=NOW + dt.timedelta(days=1))

    assert not again.changed
    assert (again.masters_created, again.ids_written, again.members_added,
            again.members_updated, again.members_removed) == (0, 0, 0, 0, 0)
    assert again.sets_total_updated == []
    assert {model: db_session.query(model).count() for model in counts} == counts
    assert db_session.query(SetChecklist).one().fetched_at == NOW
    assert _card_state(db_session) == before
    assert seed.format_report(again)[-1] == "  changed: nothing"


def test_manual_tcgdex_mapping_is_never_overwritten(db_session, payload):
    _import(db_session, [{"id": "jpn_sv2a-1", "variant": "Normal", "number": "001/165"}])
    master = db_session.query(MasterCard).one()
    masterdata.set_external_id(db_session, master, "tcgdex", "SV2a-999", masterdata.MATCHED_MANUAL)
    db_session.commit()

    result = _run(db_session, payload)

    row = next(r for r in master.external_ids if r.source == "tcgdex")
    assert (row.external_id, row.matched_by) == ("SV2a-999", masterdata.MATCHED_MANUAL)
    assert result.ids_manual_kept == 1


def test_sets_total_cards_is_the_base_print_count(db_session, payload):
    _import(db_session, [{"id": "jpn_sv2a-1", "variant": "Normal", "number": "001/165"}])
    _import(db_session, [{"id": "sv2-1", "variant": "Normal", "series": "Scarlet & Violet",
                          "set": "Paldea Evolved", "number": "001/193"}])

    result = _run(db_session, payload)

    totals = {s.name: s.total_cards for s in db_session.query(Set).all()}
    assert totals == {"Pokémon Card 151": 4, "Paldea Evolved": None}
    assert result.sets_total_updated == ["Scarlet & Violet: 151 JP/KR / Pokémon Card 151: None -> 4"]


def test_base_conflict_prefers_the_owned_master(db_session, payload):
    _import(db_session, [{"id": "jpn_sv2a-1", "variant": "Holo", "number": "001/165"}])
    db_session.add(MasterCard(language="ja", set_code="sv2a", number="1", variant="normal"))
    db_session.commit()

    result = _run(db_session, payload)

    assert _members(db_session)[("1", "holo")] == ("main", True)
    assert ("1", "normal") not in _members(db_session)
    assert len(result.base_conflicts) == 1 and "normal" in result.base_conflicts[0]
    assert result.unmatched == []


def test_dry_run_writes_nothing(db_session, payload):
    _import(db_session, [{"id": "jpn_sv2a-1", "variant": "Normal", "number": "001/165"}])
    masters_before = db_session.query(MasterCard).count()

    result = _run(db_session, payload, dry_run=True)

    assert result.dry_run and result.masters_created == 7 and result.unmatched == []
    assert db_session.query(SetChecklist).count() == 0
    assert db_session.query(MasterCard).count() == masters_before
    assert db_session.query(Set).one().total_cards is None
    assert seed.format_report(result)[0].startswith("set_checklist_seed (dry run, nothing written)")


def test_get_checklist(db_session, payload):
    assert set_checklists.get_checklist(db_session, "ja", "sv2a") is None
    _run(db_session, payload)
    checklist = set_checklists.get_checklist(db_session, "ja", "sv2a")
    assert len(checklist.cards) == 8
    assert {m.track for m in checklist.cards} == set(set_checklists.TRACKS)


def test_new_masters_follow_the_sets_number_format(db_session, payload):
    """Dex's sv2a IDs are unpadded ("jpn_sv2a-1"), so new masters are too,
    and a card bought later links to the seeded master, not a parallel one."""
    _run(db_session, payload)
    assert {m.number for m in db_session.query(MasterCard).all()} == {"1", "3", "15", "201"}

    _import(db_session, [{"id": "jpn_sv2a-201", "variant": "Holo", "number": "201/165"}])
    assert db_session.query(MasterCard).filter_by(number="201").count() == 1
    assert _run(db_session, payload).unmatched == []


def test_padded_numbers_when_dex_pads(db_session, payload):
    _import(db_session, [{"id": "jpn_sv2a-001", "variant": "Normal", "number": "001/165"}])
    _run(db_session, payload)
    assert {m.number for m in db_session.query(MasterCard).all()} == {"001", "003", "015", "201"}


def test_parse_set_arg():
    assert seed.parse_set_arg("ja:sv2a").display_name == "Pokémon Card 151 (Korean)"
    assert seed.parse_set_arg("JA:SV2A", "Mine") == seed.SetSpec("ja", "sv2a", "Mine")
    assert seed.parse_set_arg("ja:sv1a").display_name is None
    with pytest.raises(Exception):
        seed.parse_set_arg("sv2a")


# --------------------------------------------------------------------------
# Fetching through tcgdex_prices.Client (httpx stubbed)
# --------------------------------------------------------------------------
class _Response:
    def __init__(self, status, body=None):
        self.status_code = status
        self._body = body
        self.headers = {}

    def json(self):
        return self._body


def _serve(monkeypatch, payload, fail=None):
    set_payload, cards = payload
    by_url = {f"{tcgdex_prices.API_BASE}/ja/sets": [{"id": "SV2", "name": "x"}, {"id": "SV2a", "name": "151"}],
              f"{tcgdex_prices.API_BASE}/ja/sets/SV2a": set_payload}
    by_url.update({f"{tcgdex_prices.API_BASE}/ja/cards/{c['id']}": c for c in cards})
    calls = []

    def fake_get(url, timeout=None):
        calls.append(url)
        if fail and url.endswith(fail):
            return _Response(503)
        return _Response(200, by_url[url]) if url in by_url else _Response(404)

    monkeypatch.setattr(tcgdex_prices.httpx, "get", fake_get)
    return calls


def test_fetch_set_uses_the_tcgdex_client(monkeypatch, payload):
    calls = _serve(monkeypatch, payload)
    client = tcgdex_prices.Client()
    set_payload, cards = seed.fetch_set(client, SPEC)
    assert set_payload["id"] == "SV2a"
    assert [c["id"] for c in cards] == ["SV2a-001", "SV2a-003", "SV2a-015", "SV2a-201"]
    assert len(calls) == 6 and client.calls == 6


def test_fetch_set_failure_raises_before_anything_is_written(monkeypatch, payload):
    _serve(monkeypatch, payload, fail="SV2a-015")
    with pytest.raises(seed.SeedError, match="nothing written"):
        seed.fetch_set(tcgdex_prices.Client(), SPEC)


def test_fetch_set_unknown_set(monkeypatch, payload):
    _serve(monkeypatch, payload)
    with pytest.raises(seed.SeedError, match="not found"):
        seed.fetch_set(tcgdex_prices.Client(), seed.SetSpec("ja", "zz9"))


def test_main_end_to_end(monkeypatch, payload, tmp_path, capsys):
    from sqlalchemy import create_engine

    engine = create_engine(f"sqlite:///{tmp_path / 'seed.db'}")
    monkeypatch.setattr(db_module, "engine", engine)
    monkeypatch.setattr(db_module, "SessionLocal", sessionmaker(bind=engine))
    _serve(monkeypatch, payload)

    assert seed.main(["--set", "ja:sv2a"]) == 0
    out = capsys.readouterr().out
    assert "slots: main 3, secret 1, poke_ball 2, master_ball 2 (base prints 4)" in out
    assert "(6 requests)" in out
    assert seed.main(["--set", "ja:sv2a"]) == 0
    assert "changed: nothing" in capsys.readouterr().out


def test_migration_from_v14_creates_checklist_tables(monkeypatch, tmp_path):
    from sqlalchemy import create_engine

    engine = create_engine(f"sqlite:///{tmp_path / 'm.db'}")
    monkeypatch.setattr(db_module, "engine", engine)
    monkeypatch.setattr(db_module, "SessionLocal", sessionmaker(bind=engine))
    db_module.init_db()
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE set_checklist_cards"))
        conn.execute(text("DROP TABLE set_checklists"))
        conn.execute(text("ALTER TABLE master_cards DROP COLUMN rarity"))
        conn.execute(text("ALTER TABLE master_cards DROP COLUMN image_url"))
    db_module._set_schema_version(14)

    db_module.init_db()

    inspector = inspect(engine)
    assert inspector.has_table("set_checklists") and inspector.has_table("set_checklist_cards")
    assert {"rarity", "image_url"} <= {c["name"] for c in inspector.get_columns("master_cards")}
    assert db_module._get_schema_version() == db_module.CURRENT_SCHEMA_VERSION


def test_rls_covers_the_new_tables(monkeypatch):
    statements = []

    class _Conn:
        def execute(self, statement, *args):
            statements.append(str(statement))
            return []

    class _Begin:
        def __enter__(self):
            return _Conn()

        def __exit__(self, *exc):
            return False

    class _FakePostgres:
        class dialect:
            name = "postgresql"

        def begin(self):
            return _Begin()

    monkeypatch.setattr(db_module, "engine", _FakePostgres())
    db_module._enable_row_level_security()
    for table in ("set_checklists", "set_checklist_cards"):
        assert f'ALTER TABLE public."{table}" ENABLE ROW LEVEL SECURITY' in statements

