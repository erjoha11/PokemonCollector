"""TCGdex price source (issue #211): ID resolution/verification, price and
variant choice, and the refresh -- all offline, against trimmed real TCGdex
responses (tests/fixtures/tcgdex, captured 2026-09-30) and a fake httpx.get.
"""
import copy
import datetime as dt
import json
from pathlib import Path

import httpx
import pytest

import masterdata
import pricing
import tcgdex_prices as T
from conftest import TEST_EUR_TO_NOK, TEST_USD_TO_NOK
from models import Card, MasterCardId

FIXTURES = Path(__file__).parent / "fixtures" / "tcgdex"
TODAY = dt.date(2026, 9, 30)


def fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def with_updated(payload: dict, when: str = "2026-09-29T09:52:40.527Z") -> dict:
    """Pin every pricing block's `updated` so MAX_UPSTREAM_AGE_DAYS is
    measured against TODAY, not the real date the fixture was captured."""
    payload = copy.deepcopy(payload)

    def fix(pricing_block):
        for block in (pricing_block or {}).values():
            if isinstance(block, dict) and "updated" in block:
                block["updated"] = when

    fix(payload.get("pricing"))
    for entry in payload.get("variants_detailed") or []:
        fix(entry.get("pricing"))
    return payload


SV2A_080 = with_updated(fixture("ja_SV2a-080.json"))  # Poké Ball / Master Ball products swapped
SWSH3_150 = with_updated(fixture("en_swsh3-150.json"))  # normal + reverse + stamped promo
DP3_63 = with_updated(fixture("en_dp3-63.json"))  # prints listed without per-print pricing

JA_SETS = [{"id": "SV2a", "name": "ポケモンカード151"}]
JA_SV2A = {
    "id": "SV2a",
    "cardCount": {"official": 165, "total": 210},
    "cards": [
        {"id": "SV2a-079", "localId": "079", "name": "ヤドン"},
        {"id": "SV2a-080", "localId": "080", "name": "ヤドラン"},
    ],
}
EN_SETS = [{"id": "swsh3", "name": "Darkness Ablaze"}, {"id": "sv03", "name": "Obsidian Flames"}]
EN_SWSH3 = {
    "id": "swsh3",
    "cardCount": {"official": 189},
    "cards": [{"id": "swsh3-150", "localId": "150", "name": "Bunnelby"}],
}


class Responses:
    """A route answering differently per call; the last answer repeats."""

    def __init__(self, *answers):
        self.answers = list(answers)

    def next(self):
        return self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]


class FakeTcgdex:
    """Stands in for httpx.get: `routes` maps an API path ("ja/sets") to a
    JSON payload, an int status code, an exception to raise, or Responses.
    Anything unrouted is a 404."""

    def __init__(self, routes: dict):
        self.routes = routes
        self.calls: list[str] = []

    def __call__(self, url, timeout=None, **kwargs):
        path = url.split("/v2/", 1)[1]
        self.calls.append(path)
        route = self.routes.get(path, 404)
        if isinstance(route, Responses):
            route = route.next()
        if isinstance(route, Exception):
            raise route
        request = httpx.Request("GET", url)
        if isinstance(route, int):
            return httpx.Response(route, request=request)
        return httpx.Response(200, json=route, request=request)


@pytest.fixture()
def fake_api(monkeypatch):
    def install(routes):
        fake = FakeTcgdex(routes)
        monkeypatch.setattr(T.httpx, "get", fake)
        return fake

    return install


def ja_routes(card_payload=SV2A_080):
    return {"ja/sets": JA_SETS, "ja/sets/SV2a": JA_SV2A, "ja/cards/SV2a-080": card_payload}


def en_routes(card_payload=SWSH3_150):
    return {"en/sets": EN_SETS, "en/sets/swsh3": EN_SWSH3, "en/cards/swsh3-150": card_payload}


def add_card(db, card_id="jpn_sv2a-80", name="Slowbro", number="80/165", variant="Normal", dex_price=None, dex_on=TODAY):
    card = Card(card_id=card_id, name=name, number=number, variant=variant, qty=1)
    db.add(card)
    db.flush()
    masterdata.link_card(db, card)
    if dex_price is not None:
        pricing.record_price(card, pricing.SOURCE_DEX, price_nok=dex_price, fetched_at=dex_on, currency="NOK", fx_rate=1.0)
    pricing.resolve_cards(db, [card.id], today=dex_on)
    db.commit()
    return card


def tcgdex_ids(db):
    return {(r.external_id, r.matched_by) for r in db.query(MasterCardId).filter_by(source=T.SOURCE_ID)}


# --------------------------------------------------------------------------
# Pure helpers
# --------------------------------------------------------------------------
def test_set_id_candidates():
    assert T.set_id_candidates("ja", "sv2a") == ["sv2a"]
    assert T.set_id_candidates("int", "swsh3") == ["swsh3", "swsh03"]
    assert "sv03" in T.set_id_candidates("int", "sv3")
    assert "sv03.5" in T.set_id_candidates("int", "sv3pt5")
    assert "sv03.5" in T.set_id_candidates("int", "sv35")  # Dex's spelling
    assert "swsh12.5gg" in T.set_id_candidates("int", "swsh12pt5gg")
    assert "sv10.5b" in T.set_id_candidates("int", "sv105b")
    # The literal code always comes first.
    assert T.set_id_candidates("int", "base5")[0] == "base5"


def test_normalize_local_id():
    assert T.normalize_local_id("080") == T.normalize_local_id("80") == "80"
    assert T.normalize_local_id("TG05") == T.normalize_local_id("tg5") == "TG5"
    assert T.normalize_local_id("") is None


def test_names_match():
    assert T.names_match("Charizard ex", "Charizard-EX")
    assert T.names_match("Flabébé", "Flabebe")
    assert T.names_match("Dark Celebi", "Celebi")
    assert not T.names_match("Bunnelby", "Diggersby")
    assert not T.names_match("Mew", "Mewtwo")  # too short to count as containment


def test_verify_card_japanese_checks_set_number_and_set_size():
    ok = T.verify_card(SV2A_080, language="ja", set_id="SV2a", number="80", dex_number="80/165", dex_name="Slowbro")
    assert ok.ok and ok.matched_by == masterdata.MATCHED_VERIFIED_NUMBER

    assert not T.verify_card(SV2A_080, language="ja", set_id="SV2a", number="81", dex_number="81/165", dex_name="x").ok
    assert not T.verify_card(SV2A_080, language="ja", set_id="SV2a", number="80", dex_number="80/172", dex_name="x").ok
    assert not T.verify_card(SV2A_080, language="ja", set_id="SV1a", number="80", dex_number="80/165", dex_name="x").ok


def test_verify_card_english_also_checks_the_name():
    ok = T.verify_card(SWSH3_150, language="int", set_id="swsh3", number="150", dex_number="150/189", dex_name="Bunnelby")
    assert ok.ok and ok.matched_by == masterdata.MATCHED_VERIFIED
    bad = T.verify_card(SWSH3_150, language="int", set_id="swsh3", number="150", dex_number="150/189", dex_name="Diggersby")
    assert not bad.ok and "name" in bad.reason


# --------------------------------------------------------------------------
# Choosing the Cardmarket / TCGplayer price
# --------------------------------------------------------------------------
def test_cardmarket_normal_uses_trend_of_the_normal_print():
    choice = T.choose_cardmarket(SV2A_080, "Normal")
    assert (choice.price, choice.currency, choice.variant_key, choice.field, choice.uncertain) == (
        0.59, "EUR", "normal", "trend", False,
    )
    assert choice.updated == dt.date(2026, 9, 29)


def test_cardmarket_swapped_ball_products_are_not_trusted():
    # TCGdex has this card's Poké Ball product at 59 EUR and its Master Ball
    # one at 0.85 -- the wrong way round. Neither is used as "the" ball price.
    pokeball = T.choose_cardmarket(SV2A_080, "Poké Ball Holo")
    masterball = T.choose_cardmarket(SV2A_080, "Master Ball Holo")
    for choice in (pokeball, masterball):
        assert choice.uncertain
        assert choice.variant_key == "normal"


def test_cardmarket_ball_variant_uses_its_own_product_when_consistent():
    payload = copy.deepcopy(SV2A_080)
    for entry in payload["variants_detailed"]:
        block = entry["pricing"]["cardmarket"]
        if entry.get("foil") == "pokeball":
            block["trend-holo"] = 1.5
        if entry.get("foil") == "masterball":
            block["trend-holo"] = 40.0
    pokeball = T.choose_cardmarket(payload, "Poké Ball Holo")
    assert (pokeball.price, pokeball.variant_key, pokeball.field, pokeball.uncertain) == (
        1.5, "reverse-pokeball", "trend-holo", False,
    )
    assert T.choose_cardmarket(payload, "Master Ball Holo").price == 40.0


def test_cardmarket_reverse_holo_uses_the_holo_fields():
    choice = T.choose_cardmarket(SWSH3_150, "Reverse Holo")
    assert (choice.price, choice.variant_key, choice.field, choice.uncertain) == (0.35, "reverse", "trend-holo", False)


def test_cardmarket_reverse_holo_without_a_listed_reverse_print_uses_the_card_level_holo_fields():
    payload = copy.deepcopy(DP3_63)
    payload["variants_detailed"] = [e for e in payload["variants_detailed"] if e["type"] != "reverse"]
    choice = T.choose_cardmarket(payload, "Reverse Holo")
    assert (choice.price, choice.variant_key, choice.uncertain) == (5.01, "reverse", False)


def test_cardmarket_prints_without_own_pricing_borrow_the_card_level_block():
    choice = T.choose_cardmarket(DP3_63, "Normal")
    assert (choice.price, choice.variant_key, choice.uncertain) == (0.67, "normal", False)


def test_cardmarket_stamped_promo_is_ignored():
    # swsh3-150 also lists a stamped reverse (a separate, pricier product).
    assert T.choose_cardmarket(SWSH3_150, "Reverse Holo").price == 0.35


def test_cardmarket_unmatched_variant_falls_back_flagged():
    choice = T.choose_cardmarket(SWSH3_150, "Holo")  # the card has no holo print
    assert choice.uncertain and choice.variant_key == "normal" and choice.price == 0.07


def test_cardmarket_zero_trend_falls_back_to_avg30():
    payload = copy.deepcopy(SWSH3_150)
    payload["variants_detailed"][0]["pricing"]["cardmarket"]["trend"] = 0
    choice = T.choose_cardmarket(payload, "Normal")
    assert (choice.price, choice.field) == (0.09, "avg30")


def test_cardmarket_nothing_priced_is_none():
    assert T.choose_cardmarket({"id": "x", "pricing": {"cardmarket": None}}, "Normal") is None


def test_tcgplayer_matches_dex_variant():
    assert T.choose_tcgplayer(SWSH3_150, "Normal").price == 0.07
    reverse = T.choose_tcgplayer(SWSH3_150, "Reverse Holo")
    assert (reverse.price, reverse.currency, reverse.variant_key, reverse.uncertain) == (
        0.43, "USD", "reverse-holofoil", False,
    )
    # A bare "Holo" is never guessed (card_images' policy).
    assert T.choose_tcgplayer(SWSH3_150, "Holo").uncertain


def test_tcgplayer_absent_for_japanese_cards():
    assert T.choose_tcgplayer(SV2A_080, "Normal") is None


# --------------------------------------------------------------------------
# The refresh
# --------------------------------------------------------------------------
def test_refresh_resolves_verifies_and_prices_a_japanese_card(db_session, fake_api):
    card = add_card(db_session)
    fake = fake_api(ja_routes())

    result = T.refresh_tcgdex_prices(db_session, today=TODAY)

    assert (result.cards_checked, result.cards_priced, result.ids_matched) == (1, 1, 1)
    assert tcgdex_ids(db_session) == {("SV2a-080", masterdata.MATCHED_VERIFIED_NUMBER)}
    row = pricing.get_row(card, T.SOURCE_CM)
    assert (row.price, row.currency, row.fx_rate, row.price_nok) == (0.59, "EUR", TEST_EUR_TO_NOK, round(0.59 * TEST_EUR_TO_NOK, 2))
    assert (row.variant_key, row.fetched_at, row.source_updated_at, row.flags) == ("normal", TODAY, dt.date(2026, 9, 29), None)
    assert pricing.get_row(card, T.SOURCE_TP) is None  # no TCGplayer data, and no noise row
    # No Dex price -> the Cardmarket price is the market price.
    db_session.refresh(card)
    assert (card.market_price, card.market_price_source) == (row.price_nok, T.SOURCE_CM)
    assert fake.calls == ["ja/sets", "ja/sets/SV2a", "ja/cards/SV2a-080"]


def test_cardmarket_fills_in_when_no_tcgplayer_source_is_fresh(db_session, fake_api):
    stale_day = TODAY - dt.timedelta(days=pricing.FRESH_DAYS + 5)
    card = add_card(db_session, dex_price=12.0, dex_on=stale_day)
    fake_api(ja_routes())
    T.refresh_tcgdex_prices(db_session, today=TODAY)
    db_session.refresh(card)
    assert card.market_price_source == T.SOURCE_CM


def test_fresh_dex_price_still_wins(db_session, fake_api):
    card = add_card(db_session, dex_price=12.0, dex_on=TODAY)
    fake_api(ja_routes())
    T.refresh_tcgdex_prices(db_session, today=TODAY)
    db_session.refresh(card)
    assert (card.market_price, card.market_price_source) == (12.0, pricing.SOURCE_DEX)
    assert pricing.get_row(card, T.SOURCE_CM).price_nok is not None


def test_refresh_prices_both_sources_for_an_english_card(db_session, fake_api):
    card = add_card(db_session, card_id="swsh3-150", name="Bunnelby", number="150/189", variant="Normal")
    fake_api(en_routes())
    T.refresh_tcgdex_prices(db_session, today=TODAY)
    assert tcgdex_ids(db_session) == {("swsh3-150", masterdata.MATCHED_VERIFIED)}
    tp = pricing.get_row(card, T.SOURCE_TP)
    assert (tp.price, tp.currency, tp.price_nok, tp.variant_key) == (0.07, "USD", round(0.07 * TEST_USD_TO_NOK, 2), "normal")
    assert pricing.get_row(card, T.SOURCE_CM).price == 0.07
    db_session.refresh(card)
    assert card.market_price_source == T.SOURCE_TP  # ahead of Cardmarket in the chain


def test_a_later_refresh_looks_up_by_stored_id_only(db_session, fake_api):
    card = add_card(db_session)
    fake_api(ja_routes())
    T.refresh_tcgdex_prices(db_session, today=TODAY)

    later = TODAY + dt.timedelta(days=T.STALE_AFTER_DAYS + 1)
    fake = fake_api({"ja/cards/SV2a-080": SV2A_080})  # no set routes at all
    result = T.refresh_tcgdex_prices(db_session, today=later)
    assert fake.calls == ["ja/cards/SV2a-080"]
    assert (result.cards_priced, result.ids_matched) == (1, 0)
    assert pricing.get_row(card, T.SOURCE_CM).fetched_at == later


def test_an_unverified_match_stores_no_id_and_no_price(db_session, fake_api):
    card = add_card(db_session, card_id="swsh3-150", name="Diggersby", number="150/189")
    fake_api(en_routes())
    result = T.refresh_tcgdex_prices(db_session, today=TODAY)
    assert tcgdex_ids(db_session) == set()
    row = pricing.get_row(card, T.SOURCE_CM)
    assert row.price_nok is None and row.lookup_failed_at == TODAY
    assert len(result.cards_unmatched) == 1 and "name" in result.cards_unmatched[0]


def test_ambiguous_number_in_the_set_list_is_not_resolved(db_session, fake_api):
    add_card(db_session)
    dup = copy.deepcopy(JA_SV2A)
    dup["cards"].append({"id": "SV2a-080b", "localId": "80", "name": "?"})
    fake = fake_api({**ja_routes(), "ja/sets/SV2a": dup})
    result = T.refresh_tcgdex_prices(db_session, today=TODAY)
    assert "ja/cards/SV2a-080" not in fake.calls
    assert tcgdex_ids(db_session) == set() and len(result.cards_unmatched) == 1


def _price_then(db_session, fake_api, routes_after):
    card = add_card(db_session)
    fake_api(ja_routes())
    T.refresh_tcgdex_prices(db_session, today=TODAY)
    before = pricing.get_row(card, T.SOURCE_CM).price_nok
    later = TODAY + dt.timedelta(days=T.STALE_AFTER_DAYS + 1)
    fake_api(routes_after)
    result = T.refresh_tcgdex_prices(db_session, today=later)
    return card, before, later, result


def test_a_server_error_keeps_the_price_and_stamps_nothing(db_session, fake_api):
    card, before, later, result = _price_then(db_session, fake_api, {"ja/cards/SV2a-080": 503})
    row = pricing.get_row(card, T.SOURCE_CM)
    assert (row.price_nok, row.fetched_at, row.lookup_failed_at) == (before, TODAY, None)
    assert result.transient_errors == 1 and result.cards_checked == 0


def test_a_network_error_keeps_the_price(db_session, fake_api):
    card, before, _later, result = _price_then(
        db_session, fake_api, {"ja/cards/SV2a-080": httpx.ConnectTimeout("slow")}
    )
    assert pricing.get_row(card, T.SOURCE_CM).price_nok == before
    assert result.transient_errors == 1


def test_a_card_gone_from_tcgdex_keeps_its_price_and_backs_off(db_session, fake_api):
    card, before, later, result = _price_then(db_session, fake_api, {"ja/cards/SV2a-080": 404})
    row = pricing.get_row(card, T.SOURCE_CM)
    assert (row.price_nok, row.lookup_failed_at) == (before, later)
    assert db_session.query(Card).filter(T.due_filter(later + dt.timedelta(days=1))).count() == 0
    assert db_session.query(Card).filter(T.due_filter(later + dt.timedelta(days=T.RETRY_AFTER_DAYS + 1))).count() == 1


def test_a_payload_without_prices_keeps_the_old_price(db_session, fake_api):
    empty = copy.deepcopy(SV2A_080)
    empty["pricing"] = {"cardmarket": None, "tcgplayer": None}
    empty["variants_detailed"] = []
    empty["variants"] = {}
    card, before, later, _ = _price_then(db_session, fake_api, {"ja/cards/SV2a-080": empty})
    row = pricing.get_row(card, T.SOURCE_CM)
    assert (row.price_nok, row.fetched_at, row.lookup_failed_at) == (before, TODAY, later)


def test_an_upstream_price_too_old_is_not_used(db_session, fake_api):
    card = add_card(db_session)
    fake_api(ja_routes(with_updated(SV2A_080, "2026-06-01T00:00:00Z")))
    result = T.refresh_tcgdex_prices(db_session, today=TODAY)
    assert result.cards_priced == 0
    assert pricing.get_row(card, T.SOURCE_CM).price_nok is None


def test_a_persisting_429_stops_the_run(db_session, fake_api):
    add_card(db_session)
    add_card(db_session, card_id="jpn_sv2a-79", name="Slowpoke", number="79/165")
    fake = fake_api({"ja/sets": 429})
    result = T.refresh_tcgdex_prices(db_session, today=TODAY)
    assert result.stopped == "rate_limited"
    assert fake.calls == ["ja/sets", "ja/sets"]  # one retry, then stop -- the second card isn't tried


def test_a_single_429_is_retried(db_session, fake_api):
    add_card(db_session)
    routes = ja_routes()
    routes["ja/cards/SV2a-080"] = Responses(429, SV2A_080)
    fake_api(routes)
    assert T.refresh_tcgdex_prices(db_session, today=TODAY).cards_priced == 1


def test_repeated_transient_errors_stop_the_run(db_session, fake_api):
    for n in range(1, 6):
        add_card(db_session, card_id=f"jpn_sv2a-{n}", name=f"C{n}", number=f"{n}/165")
    fake_api({"ja/sets": 500})
    result = T.refresh_tcgdex_prices(db_session, today=TODAY)
    assert result.stopped == "errors" and result.transient_errors == T.MAX_CONSECUTIVE_ERRORS


def test_time_budget_and_card_budget(db_session, fake_api):
    add_card(db_session)
    add_card(db_session, card_id="jpn_sv2a-79", name="Slowpoke", number="79/165")
    fake_api(ja_routes())
    assert T.refresh_tcgdex_prices(db_session, today=TODAY, time_budget_s=-1).stopped == "time"
    assert T.refresh_tcgdex_prices(db_session, today=TODAY, budget=1).cards_checked == 1


def test_uncovered_languages_are_never_due(db_session):
    add_card(db_session, card_id="scn_csv9-79", name="Something", number="79/100")
    assert db_session.query(Card).filter(T.due_filter(TODAY)).count() == 0


def test_a_fresh_tcgdex_price_is_not_due_again(db_session, fake_api):
    add_card(db_session)
    fake_api(ja_routes())
    T.refresh_tcgdex_prices(db_session, today=TODAY)
    assert db_session.query(Card).filter(T.due_filter(TODAY + dt.timedelta(days=3))).count() == 0
    assert db_session.query(Card).filter(T.due_filter(TODAY + dt.timedelta(days=T.STALE_AFTER_DAYS + 1))).count() == 1


def test_english_card_with_only_a_tcgplayer_price_is_not_backed_off(db_session, fake_api):
    # Cardmarket gone, TCGplayer priced: the Cardmarket row gets stamped, but
    # a same-day TCGplayer price means the card isn't in a failure back-off.
    card = add_card(db_session, card_id="swsh3-150", name="Bunnelby", number="150/189")
    fake_api(en_routes())
    T.refresh_tcgdex_prices(db_session, today=TODAY)
    no_cm = copy.deepcopy(SWSH3_150)
    no_cm["pricing"]["cardmarket"] = None
    no_cm["variants_detailed"] = []
    no_cm["variants"] = {}
    later = TODAY + dt.timedelta(days=T.STALE_AFTER_DAYS + 1)
    fake_api({"en/cards/swsh3-150": no_cm})
    T.refresh_tcgdex_prices(db_session, today=later)
    assert pricing.get_row(card, T.SOURCE_CM).lookup_failed_at == later
    assert pricing.get_row(card, T.SOURCE_TP).fetched_at == later
    after = later + dt.timedelta(days=T.STALE_AFTER_DAYS + 1)
    assert db_session.query(Card).filter(T.due_filter(after)).count() == 1


def test_unpriced_cards_go_first(db_session, fake_api):
    priced = add_card(db_session, card_id="jpn_sv2a-79", name="Slowpoke", number="79/165", dex_price=5.0)
    unpriced = add_card(db_session)
    cards = db_session.query(Card).all()
    cards.sort(key=T._priority)
    assert [c.id for c in cards] == [unpriced.id, priced.id]


# --------------------------------------------------------------------------
# /cron/price-refresh
# --------------------------------------------------------------------------
def _seed_cron_card():
    import db as db_module

    with db_module.SessionLocal() as db:
        card = Card(card_id="jpn_sv2a-80", name="Slowbro", number="80/165", variant="Normal", qty=1)
        db.add(card)
        db.flush()
        masterdata.link_card(db, card)
        db.commit()


def test_cron_price_refresh_runs_the_tcgdex_pass_before_its_snapshot(client, fake_api):
    import db as db_module
    from models import CardSnapshot

    _seed_cron_card()
    fake_api(ja_routes(with_updated(SV2A_080, f"{dt.date.today().isoformat()}T00:00:00Z")))

    response = client.get("/cron/price-refresh")

    assert response.status_code == 200
    body = response.json()["tcgdex"]
    assert (body["cards_checked"], body["cards_priced"], body["ids_matched"]) == (1, 1, 1)
    with db_module.SessionLocal() as db:
        snap = db.query(CardSnapshot).one()
        assert snap.price_source == T.SOURCE_CM
        assert snap.reference_price == round(0.59 * TEST_EUR_TO_NOK, 2)


def test_cron_price_refresh_survives_a_tcgdex_crash(client, monkeypatch):
    import db as db_module
    import jobs
    from models import CardSnapshot

    _seed_cron_card()

    def boom(*args, **kwargs):
        raise RuntimeError("tcgdex bug")

    monkeypatch.setattr(jobs.tcgdex_prices, "refresh_tcgdex_prices", boom)
    response = client.get("/cron/price-refresh")
    assert response.status_code == 200
    assert response.json()["tcgdex"] is None
    with db_module.SessionLocal() as db:
        assert db.query(CardSnapshot).count() == 1


# --------------------------------------------------------------------------
# FX fallback guard (issue #229)
# --------------------------------------------------------------------------
def test_refresh_writes_nothing_at_the_fx_fallback_rate(db_session, fake_api):
    import fx_rates

    card = add_card(db_session)
    fake = fake_api(ja_routes())
    fx_rates.reset_cache()  # live fetch fails, fx_rates table empty

    result = T.refresh_tcgdex_prices(db_session, today=TODAY)

    assert (result.status, result.stopped, result.fx_source) == ("degraded", "fx_unavailable", "fallback")
    assert result.degraded_reason == fx_rates.FALLBACK_REASON
    assert (result.cards_checked, result.cards_priced, result.ids_matched) == (0, 0, 0)
    assert fake.calls == []  # TCGdex not even asked
    assert pricing.get_row(card, T.SOURCE_CM) is None and pricing.get_row(card, T.SOURCE_TP) is None
    assert tcgdex_ids(db_session) == set()
    assert db_session.query(Card).filter(T.due_filter(TODAY)).count() == 1  # still due


def test_lookup_card_refuses_a_fallback_rate(db_session):
    import fx_rates

    card = add_card(db_session)
    rates = fx_rates.FxRates(rates=dict(fx_rates.FALLBACK_RATES), as_of=None, source="fallback")
    with pytest.raises(ValueError):
        T.lookup_card(T.Client(), db_session, card, TODAY, rates, T.TcgdexRefreshResult())
    assert pricing.get_row(card, T.SOURCE_CM) is None


def test_refresh_with_a_stored_rate_is_unchanged(db_session, fake_api):
    import fx_rates
    from models import FxRate

    card = add_card(db_session)
    old = dt.datetime.utcnow() - dt.timedelta(days=3)
    db_session.add_all(
        [
            FxRate(date=TODAY, currency="USD", rate_nok=9.4, fetched_at=old),
            FxRate(date=TODAY, currency="EUR", rate_nok=10.9, fetched_at=old),
        ]
    )
    db_session.commit()
    fx_rates.reset_cache()
    fake_api(ja_routes())

    result = T.refresh_tcgdex_prices(db_session, today=TODAY)

    assert (result.status, result.fx_source, result.cards_priced) == ("ok", "stored", 1)
    row = pricing.get_row(card, T.SOURCE_CM)
    assert (row.fx_rate, row.price_nok) == (10.9, round(0.59 * 10.9, 2))


# --------------------------------------------------------------------------
# Prints TCGplayer has no key for (issue #350)
# --------------------------------------------------------------------------
def test_a_ball_pattern_card_drops_its_old_tcgplayer_prices_and_falls_through_to_cardmarket(db_session, fake_api):
    # Before #350 a Poké Ball print got the base print's TCGplayer price
    # (flagged uncertain, but still winning). The refresh now writes no
    # TCGplayer price for it and drops the stored ones, from both sources.
    card = add_card(db_session, card_id="swsh3-150", name="Bunnelby", number="150/189", variant="Poké Ball Holo")
    for source in (pricing.SOURCE_TCGDEX_TCGPLAYER, pricing.SOURCE_POKEMONTCG):
        pricing.record_price(card, source, price_nok=0.7, fetched_at=TODAY, flags=[pricing.FLAG_VARIANT_UNCERTAIN])
    card.tcgplayer_price, card.tcgplayer_price_updated_at = 0.7, TODAY
    pricing.resolve_cards(db_session, [card.id], today=TODAY)
    db_session.commit()
    db_session.refresh(card)
    assert card.market_price_source == pricing.SOURCE_TCGDEX_TCGPLAYER

    fake_api(en_routes())
    result = T.refresh_tcgdex_prices(db_session, today=TODAY)

    assert (result.other_print_dropped, result.cards_priced) == (1, 1)
    db_session.expire_all()
    card = db_session.get(Card, card.id)
    assert {p.source for p in card.prices} == {T.SOURCE_CM}
    assert card.market_price_source == T.SOURCE_CM
    assert card.tcgplayer_price is None
