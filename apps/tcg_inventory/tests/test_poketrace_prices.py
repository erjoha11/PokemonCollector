"""eBay sold prices via PokeTrace, reference-only (issue #414): resolve()
never picks a reference source, the matcher, the price/flags, selection and
the refresh's limits, the job, and the card page's display gate.

All offline: a trimmed live PokeTrace response (tests/fixtures/poketrace,
captured 2026-10-14), a fake httpx.get and a fake clock -- no real sleeps,
no network, no key.
"""
import copy
import datetime as dt
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

import jobs
import masterdata
import poketrace_prices as P
import pricing
import sync_status
from conftest import TEST_USD_TO_NOK
from models import Card, CardPrice, ImportLog

FIXTURES = Path(__file__).parent / "fixtures" / "poketrace"
TODAY = dt.date(2026, 10, 14)
KEY = "pt-test-SECRET-key-123"


def fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


CHARIZARD = fixture("cards_search_charizard_ex_199_165.json")["data"][0]
CHARIZARD_ID = CHARIZARD["id"]


def record(
    *,
    id=CHARIZARD_ID,
    name="Charizard ex",
    number="199/165",
    variant="Holofoil",
    game="pokemon",
    tcgplayer_id="517045",
    updated: dt.date | None = TODAY,
    **nm,
) -> dict:
    """The live Charizard record, with any field swapped. `nm` overrides
    the eBay NEAR_MINT tier's fields (None deletes one); `updated` pins the
    lastUpdated dates relative to the test's day."""
    rec = copy.deepcopy(CHARIZARD)
    rec.update(id=id, name=name, cardNumber=number, variant=variant, game=game)
    rec["refs"]["tcgplayerId"] = tcgplayer_id
    tier = rec["prices"]["ebay"]["NEAR_MINT"]
    if updated is not None:
        tier["lastUpdated"] = f"{updated.isoformat()}T00:00:00.000Z"
        rec["lastUpdated"] = f"{updated.isoformat()}T17:30:44.810Z"
    for key, value in nm.items():
        if value is None:
            tier.pop(key, None)
        else:
            tier[key] = value
    return rec


def page(*records, has_more=False, cursor=None) -> dict:
    return {"data": list(records), "pagination": {"hasMore": has_more, "nextCursor": cursor, "count": len(records)}}


# --------------------------------------------------------------------------
# Fakes
# --------------------------------------------------------------------------
class Clock:
    """A monotonic clock that only moves when slept on, or by `per_request`
    seconds per HTTP request (via FakeHttp)."""

    def __init__(self, start=1000.0):
        self.t = start
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.t += seconds


class FakeResponse:
    def __init__(self, status=200, body=None, headers=None):
        self.status_code = status
        self._body = body
        self.headers = {k.lower(): str(v) for k, v in (headers or {}).items()}

    def json(self):
        if self._body is None:
            raise ValueError("no JSON")
        return self._body


class FakeHttp:
    """Stands in for httpx.get. `handler(path, params)` returns a
    FakeResponse, a dict (a 200 body) or raises. Records every call, with
    the clock time it was sent at."""

    def __init__(self, handler, clock: Clock | None = None, per_request: float = 0.0):
        self.handler = handler
        self.clock = clock
        self.per_request = per_request
        self.calls: list[dict] = []

    def __call__(self, url, params=None, headers=None, timeout=None):
        path = url.removeprefix(P.API_BASE)
        self.calls.append({"path": path, "params": dict(params or {}), "headers": dict(headers or {}),
                           "at": self.clock() if self.clock else None})
        if self.clock and self.per_request:
            self.clock.t += self.per_request
        answer = self.handler(path, params or {})
        return answer if isinstance(answer, FakeResponse) else FakeResponse(200, answer)


def sequence(*answers):
    """A handler answering in order; the last answer repeats."""
    queue = list(answers)

    def handler(path, params):
        answer = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(answer, Exception):
            raise answer
        return answer

    return handler


def make_client(handler, *, clock=None, per_request=0.0, max_requests=None, budget=None):
    clock = clock or Clock()
    http = FakeHttp(handler, clock, per_request)
    client = P.Client(KEY, max_requests=max_requests, time_budget_s=budget, http_get=http, clock=clock,
                      sleep=clock.sleep)
    return client, http, clock


# --------------------------------------------------------------------------
# Cards
# --------------------------------------------------------------------------
def add_card(
    db,
    card_id="sv3pt5-199",
    name="Charizard ex",
    number="199/165",
    variant="Holo",
    language="International",
    dex_price=3264.0,
    priced_on=TODAY,
    qty=1,
):
    card = Card(card_id=card_id, name=name, number=number, variant=variant, language=language, qty=qty,
                created_at=dt.datetime(2026, 1, 1))
    db.add(card)
    db.flush()
    masterdata.link_card(db, card)
    if dex_price is not None:
        pricing.record_price(card, pricing.SOURCE_DEX, price_nok=dex_price, fetched_at=priced_on, currency="NOK",
                             fx_rate=1.0)
    pricing.resolve_cards(db, [card.id], today=priced_on)
    db.commit()
    db.refresh(card)
    return card


def store_ids(db, card, poketrace_id=CHARIZARD_ID, tcgplayer_id="517045"):
    masterdata.set_external_id(db, card.master_card, P.SOURCE_ID, poketrace_id, masterdata.MATCHED_VERIFIED)
    if tcgplayer_id:
        masterdata.set_external_id(db, card.master_card, P.SOURCE_TCGPLAYER, tcgplayer_id,
                                   masterdata.MATCHED_VERIFIED)
    db.commit()


def ebay_row(card):
    return pricing.get_row(card, P.SOURCE)


def market_state(db):
    return sorted(
        (c.id, c.market_price, c.market_price_source, c.price_flags) for c in db.query(Card).all()
    )


# --------------------------------------------------------------------------
# resolve(): a reference source can never win (#414)
# --------------------------------------------------------------------------
def _row(source, price, fetched_days_ago, flags=None):
    fetched = TODAY - dt.timedelta(days=fetched_days_ago)
    return SimpleNamespace(source=source, price_nok=price, fetched_at=fetched, flags=flags)


def test_reference_source_is_outside_chain_and_labelled():
    assert P.SOURCE == "poketrace_ebay"
    assert P.SOURCE in pricing.REFERENCE_SOURCES
    assert P.SOURCE not in pricing.CHAIN
    assert pricing.is_reference_source(P.SOURCE) and not pricing.is_reference_source(pricing.SOURCE_DEX)
    assert pricing.SOURCE_LABELS[P.SOURCE] == "eBay sold via PokeTrace"


def test_resolve_a_fresh_ebay_row_is_not_the_fallback_when_no_chain_source_is_fresh():
    # (a) Every CHAIN row stale, the eBay row fresh: the stale CHAIN price wins.
    res = pricing.resolve([_row("dex", 100.0, 30), _row(P.SOURCE, 500.0, 1)], TODAY)
    assert (res.price, res.source) == (100.0, "dex")
    assert pricing.FLAG_STALE in res.flags


def test_resolve_a_more_recent_ebay_row_is_not_the_stale_last_resort():
    # (b) Nothing fresh: the most recent CHAIN row, not the newer eBay one.
    res = pricing.resolve([_row("dex", 100.0, 40), _row(P.SOURCE, 500.0, 20)], TODAY)
    assert (res.price, res.source) == (100.0, "dex")


def test_resolve_an_ebay_only_card_has_no_price():
    # (c) The card's only priced row.
    res = pricing.resolve([_row(P.SOURCE, 500.0, 1)], TODAY)
    assert (res.price, res.source, res.flags) == (None, None, pricing.FLAG_NO_PRICE)


def test_resolve_ignores_any_source_outside_chain():
    res = pricing.resolve([_row("something_new", 7.0, 0)], TODAY)
    assert res.source is None


# --------------------------------------------------------------------------
# Matching (pure)
# --------------------------------------------------------------------------
TARGET = P.Target(game="pokemon", name="Charizard ex", number="199/165", variant="Holo")


def test_match_the_live_record_uniquely():
    match = P.match_candidates([CHARIZARD], TARGET)
    assert match.record is CHARIZARD and match.reason is None


def test_match_ambiguous_when_two_records_agree():
    twin = record(id="another-uuid")
    match = P.match_candidates([record(), twin], TARGET)
    assert match.record is None and match.reason.startswith("ambiguous: 2")


def test_match_picks_the_one_print_among_several():
    reverse = record(id="rev", variant="Reverse_Holofoil")
    other_number = record(id="n2", number="200/165")
    match = P.match_candidates([reverse, other_number, record()], TARGET)
    assert match.record["id"] == CHARIZARD_ID


@pytest.mark.parametrize(
    "rec",
    [
        record(name="Blastoise ex"),
        record(number="199/198"),
        record(number="198/165"),
        record(variant="Reverse_Holofoil"),
        record(game="pokemon-japanese"),
    ],
    ids=["name", "set-size", "number", "variant", "game"],
)
def test_match_mismatch_is_no_match(rec):
    match = P.match_candidates([rec], TARGET)
    assert match.record is None and match.reason.startswith("no PokeTrace card matches")


def test_variant_rules_are_exact():
    assert P.variant_matches("Holo", "Holofoil")
    assert P.variant_matches("Reverse Holo", "Reverse_Holofoil")
    assert P.variant_matches("Normal", "Normal") and P.variant_matches("Normal", "Unlimited")
    assert P.variant_matches("1st Edition Holo", "1st_Edition_Holofoil")
    assert P.variant_matches(None, "Holofoil")  # unspecified: name/number pin it
    # Strict: a Dex "Normal" is not PokeTrace's Holofoil print.
    assert not P.variant_matches("Normal", "Holofoil")
    assert not P.variant_matches("Holo", "Reverse_Holofoil")


def test_numbers_match_ignores_leading_zeros_only():
    assert P.numbers_match("062/198", "62/198")
    assert P.numbers_match("TG05/TG30", "TG5/TG30")
    assert not P.numbers_match("199/165", "199/198")
    assert P.numbers_match("199", "199/165")  # no size on one side


def test_search_params_pin_game_number_name_and_variant():
    params = P.search_params(TARGET)
    assert params == {"search": "Charizard ex", "card_number": "199/165", "game": "pokemon", "market": "US",
                      "limit": 20, "variant": "Holofoil"}
    assert "variant" not in P.search_params(P.Target("pokemon", "Pikachu", "58/102", "Normal"))


def test_target_skips_korean_and_prints_poketrace_does_not_list(db_session):
    korean = add_card(db_session, card_id="jpn_sv2a-151", name="Mew ex", number="151/165", variant="Normal",
                      language="Korean")
    ball = add_card(db_session, card_id="jpn_sv2a-25", name="Pikachu", number="25/165",
                    variant="Poké Ball Holo", language="Japanese")
    japanese = add_card(db_session, card_id="jpn_sv2a-205", name="Mew ex", number="205/165", variant="Holo",
                        language="Japanese")
    assert P.target_for(korean) is None
    assert P.target_for(ball) is None
    assert P.target_for(japanese) == P.Target("pokemon-japanese", "Mew ex", "205/165", "Holo")


def test_verify_known_checks_id_and_number():
    assert P.verify_known(CHARIZARD, CHARIZARD_ID, TARGET).record is CHARIZARD
    assert P.verify_known(None, CHARIZARD_ID, TARGET).record is None
    assert P.verify_known(record(number="1/165"), CHARIZARD_ID, TARGET).record is None


# --------------------------------------------------------------------------
# The price and its flags (pure)
# --------------------------------------------------------------------------
def test_price_from_the_live_record():
    price, reason = P.ebay_price(CHARIZARD, TODAY, 5)
    assert reason is None
    assert price.price == 342.745 and price.sample_size == 551
    assert price.updated == dt.date(2026, 10, 9)  # the NM tier's own lastUpdated
    assert price.flags == ()  # no `confidence` field on the live tier: not low


def test_price_falls_back_to_the_card_level_date():
    rec = record(lastUpdated=None)
    rec["lastUpdated"] = "2026-10-12T10:00:00.000Z"
    price, _ = P.ebay_price(rec, TODAY, 5)
    assert price.updated == dt.date(2026, 10, 12)


def test_price_flags_low_confidence_and_few_sales():
    price, _ = P.ebay_price(record(confidence="low", saleCount=3), TODAY, 5)
    assert price.flags == (pricing.FLAG_LOW_CONFIDENCE, pricing.FLAG_FEW_SALES)
    price, _ = P.ebay_price(record(confidence="medium", saleCount=5), TODAY, 5)
    assert price.flags == ()


@pytest.mark.parametrize(
    "rec, reason",
    [
        (record(median30d=None), "no eBay Near Mint 30-day median"),
        (record(median30d=0), "no eBay Near Mint 30-day median"),
        (record(updated=TODAY - dt.timedelta(days=31)), "newest eBay Near Mint data"),
    ],
)
def test_no_usable_price(rec, reason):
    price, why = P.ebay_price(rec, TODAY, 5)
    assert price is None and why.startswith(reason)


def test_no_near_mint_tier():
    rec = record()
    del rec["prices"]["ebay"]["NEAR_MINT"]
    assert P.ebay_price(rec, TODAY, 5) == (None, "no eBay Near Mint sales")


# --------------------------------------------------------------------------
# Selection and due logic
# --------------------------------------------------------------------------
def test_selection_threshold_order_korean_and_missing(db_session):
    cheap = add_card(db_session, card_id="sv3pt5-1", name="Bulbasaur", number="1/165", variant="Normal",
                     dex_price=19.99)
    big = add_card(db_session)
    mid = add_card(db_session, card_id="sv3pt5-25", name="Pikachu", number="25/165", variant="Normal",
                   dex_price=50.0)
    add_card(db_session, card_id="jpn_sv2a-151", name="Mew ex", number="151/165", variant="Normal",
             language="Korean", dex_price=500.0)
    gone = add_card(db_session, card_id="sv3pt5-6", name="Charizard", number="6/165", variant="Holo",
                    dex_price=300.0)
    gone.flagged_missing_since = TODAY
    sold = add_card(db_session, card_id="sv3pt5-9", name="Blastoise", number="9/165", variant="Holo",
                    dex_price=300.0, qty=0)
    db_session.commit()

    selection = P.select_cards(db_session, TODAY, min_price=20.0)
    assert [c.id for c in selection.cards] == [big.id, mid.id]  # most valuable first
    assert selection.korean_skipped == 1
    assert cheap not in selection.cards and gone not in selection.cards and sold not in selection.cards


@pytest.mark.parametrize(
    "fetched_days, failed_days, due",
    [(None, None, True), (3, None, False), (6, None, False), (7, None, True), (None, 2, False), (None, 7, True)],
)
def test_weekly_due(db_session, fetched_days, failed_days, due):
    card = add_card(db_session)
    if fetched_days is not None:
        pricing.record_price(card, P.SOURCE, price_nok=1.0, fetched_at=TODAY - dt.timedelta(days=fetched_days))
    if failed_days is not None:
        pricing.record_failure(card, P.SOURCE, TODAY - dt.timedelta(days=failed_days))
    db_session.commit()
    assert bool(P.select_cards(db_session, TODAY, min_price=20.0).cards) is due


# --------------------------------------------------------------------------
# The refresh
# --------------------------------------------------------------------------
def test_first_match_by_search_stores_ids_price_and_leaves_market_price_alone(db_session):
    card = add_card(db_session)
    before = market_state(db_session)
    client, http, _ = make_client(sequence(page(record())))

    result = P.refresh_poketrace_prices(db_session, client, today=TODAY, min_sales_n=5)

    assert (result.cards_due, result.cards_priced, result.ids_matched, result.requests) == (1, 1, 1, 1)
    assert http.calls[0]["path"] == "/cards"
    assert http.calls[0]["params"]["card_number"] == "199/165"
    row = ebay_row(card)
    assert row.price == 342.745 and row.currency == "USD" and row.fx_rate == TEST_USD_TO_NOK
    assert row.price_nok == round(342.745 * TEST_USD_TO_NOK, 2)
    assert (row.sample_size, row.window_days, row.flags) == (551, 30, None)
    assert row.variant_key == "Holofoil" and row.source_updated_at == TODAY and row.fetched_at == TODAY
    ids = {r.source: (r.external_id, r.matched_by) for r in card.master_card.external_ids}
    assert ids["poketrace"] == (CHARIZARD_ID, "verified")
    assert ids["tcgplayer"] == ("517045", "verified")
    # Reference-only: no card's market price moved, not even after a resolve.
    pricing.resolve_cards(db_session, today=TODAY)
    db_session.commit()
    assert market_state(db_session) == before


def test_flags_and_sample_size_are_stored(db_session):
    card = add_card(db_session)
    client, _, _ = make_client(sequence(page(record(confidence="low", saleCount=2))))

    result = P.refresh_poketrace_prices(db_session, client, today=TODAY, min_sales_n=5)

    row = ebay_row(card)
    assert set(pricing.flag_list(row.flags)) == {pricing.FLAG_LOW_CONFIDENCE, pricing.FLAG_FEW_SALES}
    assert row.sample_size == 2 and row.price == 342.745  # stored regardless
    assert result.cards_thin == 1


def test_unmatched_and_ambiguous_record_a_failure_without_a_price(db_session):
    card = add_card(db_session)
    other = add_card(db_session, card_id="sv3pt5-25", name="Pikachu", number="25/165", variant="Normal",
                     dex_price=50.0)
    answers = sequence(page(record(), record(id="twin")), page())
    client, _, _ = make_client(answers)

    result = P.refresh_poketrace_prices(db_session, client, today=TODAY, min_sales_n=5)

    for c in (card, other):
        row = ebay_row(c)
        assert row.price_nok is None and row.lookup_failed_at == TODAY
    assert len(result.cards_unmatched) == 2
    assert not [r for r in card.master_card.external_ids if r.source == "poketrace"]


def test_korean_and_uncovered_prints_cost_no_request_and_no_failure(db_session):
    korean = add_card(db_session, card_id="jpn_sv2a-151", name="Mew ex", number="151/165", variant="Normal",
                      language="Korean", dex_price=500.0)
    ball = add_card(db_session, card_id="jpn_sv2a-25", name="Pikachu", number="25/165",
                    variant="Master Ball Holo", language="Japanese", dex_price=500.0)
    client, http, _ = make_client(sequence(page()))

    result = P.refresh_poketrace_prices(db_session, client, today=TODAY, min_sales_n=5)

    assert http.calls == []
    assert (result.korean_skipped, result.uncovered_skipped, result.cards_due) == (1, 1, 0)
    assert ebay_row(korean) is None and ebay_row(ball) is None


def test_known_ids_refresh_in_batches_of_twenty_by_tcgplayer_id(db_session):
    cards = []
    for i in range(25):
        card = add_card(db_session, card_id=f"sv3pt5-{100 + i}", name="Charizard ex", number=f"{100 + i}/165",
                        dex_price=1000.0 - i)
        store_ids(db_session, card, poketrace_id=f"pt-{i}", tcgplayer_id=str(500000 + i))
        cards.append(card)

    def handler(path, params):
        assert path == "/cards" and "search" not in params
        ids = params["tcgplayer_ids"].split(",")
        return page(*[
            record(id=f"pt-{int(t) - 500000}", number=f"{100 + int(t) - 500000}/165", tcgplayer_id=t) for t in ids
        ])

    client, http, _ = make_client(handler)
    result = P.refresh_poketrace_prices(db_session, client, today=TODAY, min_sales_n=5)

    assert [len(c["params"]["tcgplayer_ids"].split(",")) for c in http.calls] == [20, 5]
    assert result.requests == 2 and result.cards_priced == 25 and result.ids_matched == 0
    assert all(ebay_row(c).price == 342.745 for c in cards)


def test_second_run_after_a_week_uses_the_stored_ids_not_a_search(db_session):
    card = add_card(db_session)
    client, http, _ = make_client(sequence(page(record())))
    P.refresh_poketrace_prices(db_session, client, today=TODAY, min_sales_n=5)
    assert "search" in http.calls[0]["params"]

    later = TODAY + dt.timedelta(days=7)
    pricing.record_price(card, pricing.SOURCE_DEX, price_nok=3264.0, fetched_at=later, currency="NOK")
    pricing.resolve_cards(db_session, today=later)
    db_session.commit()
    client, http, _ = make_client(sequence(page(record(updated=later, median30d=350.0))))
    result = P.refresh_poketrace_prices(db_session, client, today=later, min_sales_n=5)

    assert http.calls[0]["params"] == {"tcgplayer_ids": "517045", "market": "US", "limit": 20}
    assert result.cards_priced == 1 and ebay_row(card).price == 350.0


def test_known_id_without_tcgplayer_id_is_fetched_by_id(db_session):
    card = add_card(db_session)
    store_ids(db_session, card, tcgplayer_id=None)
    client, http, _ = make_client(sequence({"data": record()}))

    P.refresh_poketrace_prices(db_session, client, today=TODAY, min_sales_n=5)

    assert http.calls[0]["path"] == f"/cards/{CHARIZARD_ID}"
    assert ebay_row(card).price == 342.745


def test_pacing_keeps_at_least_two_seconds_between_requests(db_session):
    for i in range(3):
        add_card(db_session, card_id=f"sv3pt5-{i + 1}", name="Charizard ex", number=f"{i + 1}/165",
                 dex_price=100.0 + i)
    client, http, clock = make_client(sequence(page()), per_request=0.3)

    P.refresh_poketrace_prices(db_session, client, today=TODAY, min_sales_n=5)

    times = [c["at"] for c in http.calls]
    assert len(times) == 3
    assert all(b - a >= 2.0 for a, b in zip(times, times[1:]))
    assert clock.sleeps and all(s <= P.REQUEST_INTERVAL_S + 1e-9 for s in clock.sleeps)


def test_429_is_retried_once_after_retry_after(db_session):
    card = add_card(db_session)
    burst = FakeResponse(429, {"code": "BURST_RATE_LIMIT_EXCEEDED", "retryAfter": 2})
    client, http, clock = make_client(sequence(burst, page(record())))

    result = P.refresh_poketrace_prices(db_session, client, today=TODAY, min_sales_n=5)

    assert 2 in clock.sleeps
    assert result.requests == 2 and result.stopped is None and ebay_row(card).price == 342.745


def test_a_second_429_stops_the_run_without_stamping_cards(db_session):
    first = add_card(db_session)
    second = add_card(db_session, card_id="sv3pt5-25", name="Pikachu", number="25/165", variant="Normal",
                      dex_price=50.0)
    burst = FakeResponse(429, {"code": "BURST_RATE_LIMIT_EXCEEDED", "retryAfter": 2})
    client, http, _ = make_client(sequence(burst))

    result = P.refresh_poketrace_prices(db_session, client, today=TODAY, min_sales_n=5)

    assert result.stopped == "rate_limited" and len(http.calls) == 2
    assert ebay_row(first) is None and ebay_row(second) is None
    assert result.cards_left == 2


def test_time_box_is_checked_before_each_request(db_session):
    for i in range(5):
        add_card(db_session, card_id=f"sv3pt5-{i + 1}", name="Charizard ex", number=f"{i + 1}/165",
                 dex_price=100.0 + i)
    client, http, _ = make_client(sequence(page()), per_request=25.0, budget=60.0)

    result = P.refresh_poketrace_prices(db_session, client, today=TODAY, min_sales_n=5)

    assert result.stopped == "time" and len(http.calls) == 3  # 0 s, 27.1 s, 54.2 s; then past 60
    assert result.cards_checked == 3 and result.cards_left == 2


def test_daily_cap_is_a_hard_request_limit(db_session):
    for i in range(3):
        add_card(db_session, card_id=f"sv3pt5-{i + 1}", name="Charizard ex", number=f"{i + 1}/165",
                 dex_price=100.0 + i)
    client, http, _ = make_client(sequence(page()), max_requests=2)

    result = P.refresh_poketrace_prices(db_session, client, today=TODAY, min_sales_n=5)

    assert result.stopped == "daily_cap" and len(http.calls) == 2


def test_ratelimit_remaining_zero_stops_the_run(db_session):
    for i in range(3):
        add_card(db_session, card_id=f"sv3pt5-{i + 1}", name="Charizard ex", number=f"{i + 1}/165",
                 dex_price=100.0 + i)
    last = FakeResponse(200, page(), {"x-ratelimit-limit": "250", "x-ratelimit-remaining": "0"})
    client, http, _ = make_client(sequence(last))

    result = P.refresh_poketrace_prices(db_session, client, today=TODAY, min_sales_n=5)

    assert result.stopped == "quota" and len(http.calls) == 1 and result.rate_remaining == 0


def test_forbidden_stops_the_run(db_session):
    add_card(db_session)
    add_card(db_session, card_id="sv3pt5-25", name="Pikachu", number="25/165", variant="Normal", dex_price=50.0)
    client, http, _ = make_client(sequence(FakeResponse(403, fixture("cardmarket_ids_403.json"))))

    result = P.refresh_poketrace_prices(db_session, client, today=TODAY, min_sales_n=5)

    assert result.stopped == "forbidden" and len(http.calls) == 1 and result.cards_checked == 0


def test_transient_errors_skip_the_card_and_three_in_a_row_stop(db_session):
    for i in range(4):
        add_card(db_session, card_id=f"sv3pt5-{i + 1}", name="Charizard ex", number=f"{i + 1}/165",
                 dex_price=100.0 + i)
    client, http, _ = make_client(sequence(httpx.ConnectTimeout("slow"), FakeResponse(502), FakeResponse(200)))

    result = P.refresh_poketrace_prices(db_session, client, today=TODAY, min_sales_n=5)

    assert result.stopped == "errors" and result.transient_errors == 3 and len(http.calls) == 3
    assert db_session.query(CardPrice).filter_by(source=P.SOURCE).count() == 0


def test_the_key_is_only_ever_in_the_request_header(db_session):
    add_card(db_session)
    client, http, _ = make_client(sequence(page(record())))
    P.refresh_poketrace_prices(db_session, client, today=TODAY, min_sales_n=5)
    assert http.calls[0]["headers"]["X-API-Key"] == KEY
    assert KEY not in repr(client) and KEY not in str(http.calls[0]["params"])


# --------------------------------------------------------------------------
# The job (jobs.run_poketrace_refresh) and its /cron route
# --------------------------------------------------------------------------
def _session():
    import db as db_module

    return db_module.SessionLocal()


def _logs():
    with _session() as db:
        return [(r.job, r.status, r.source, r.api_requests, r.message) for r in db.query(ImportLog).order_by(ImportLog.id)]


@pytest.fixture()
def fake_api(monkeypatch):
    """Route poketrace_prices' default client through a FakeHttp; tests set
    `.handler`."""
    clock = Clock()
    http = FakeHttp(sequence(page(record(updated=dt.date.today()))), clock)
    monkeypatch.setattr(P, "_http_get", http)
    monkeypatch.setattr(P, "_CLOCK", clock)
    monkeypatch.setattr(P, "_SLEEP", clock.sleep)
    return http


def _seed_card():
    with _session() as db:
        card = add_card(db, priced_on=dt.date.today())
        return card.id


def test_job_without_a_key_is_skipped_and_makes_no_request(client, fake_api, capsys):
    _seed_card()
    with _session() as db:
        result = jobs.run_poketrace_refresh(db, trigger="cron")

    assert (result.status, result.reason) == ("skipped", "no_key")
    assert fake_api.calls == []
    [(job, status, source, requests, message)] = _logs()
    assert (job, status, source, requests) == ("poketrace-refresh", "skipped", "cron", 0)
    assert "no key" in message and "POKETRACE_API_KEY" in message
    assert "skipped" in capsys.readouterr().out


def test_job_prices_records_requests_and_never_logs_the_key(client, fake_api, monkeypatch, capsys):
    monkeypatch.setenv("POKETRACE_API_KEY", KEY)
    card_id = _seed_card()
    with _session() as db:
        before = market_state(db)
        result = jobs.run_poketrace_refresh(db, trigger="manual")
        assert market_state(db) == before

    assert result.status == "ok" and result.requests == 1 and result.day_used == 1
    with _session() as db:
        assert db.get(Card, card_id).prices and ebay_row(db.get(Card, card_id)).price == 342.745
    [(job, status, source, requests, message)] = _logs()
    assert (job, status, source, requests) == ("poketrace-refresh", "ok", "manual", 1)
    assert "priced 1 of 1 due" in message and "1 of 200 requests used today" in message
    assert KEY not in message and KEY not in capsys.readouterr().out
    assert fake_api.calls[0]["headers"]["X-API-Key"] == KEY


def test_job_daily_cap_counts_earlier_runs_today(client, fake_api, monkeypatch):
    monkeypatch.setenv("POKETRACE_API_KEY", KEY)
    monkeypatch.setenv("POKETRACE_DAILY_REQUESTS", "3")
    _seed_card()
    with _session() as db:
        sync_status.record_run(db, job=sync_status.POKETRACE_REFRESH, status="ok", source="cron", api_requests=3)
        # Yesterday's requests don't count.
        sync_status.record_run(db, job=sync_status.POKETRACE_REFRESH, status="ok", source="cron", api_requests=50,
                               ran_at=dt.datetime.utcnow() - dt.timedelta(days=1))
        assert sync_status.api_requests_today(db, sync_status.POKETRACE_REFRESH) == 3
        result = jobs.run_poketrace_refresh(db, trigger="manual")

    assert (result.status, result.reason) == ("skipped", "cap_reached")
    assert fake_api.calls == []
    assert "3 of 3" in _logs()[-1][4]


def test_job_failure_is_recorded_with_its_requests(client, fake_api, monkeypatch):
    monkeypatch.setenv("POKETRACE_API_KEY", KEY)
    _seed_card()
    fake_api.handler = sequence(FakeResponse(401, {"code": "UNAUTHORIZED"}))
    with _session() as db:
        result = jobs.run_poketrace_refresh(db, trigger="cron")

    assert (result.status, result.reason) == ("failed", "forbidden")
    assert _logs()[-1][:4] == ("poketrace-refresh", "failed", "cron", 1)


def test_cron_route_reports_the_run(client, fake_api, monkeypatch):
    monkeypatch.setenv("POKETRACE_API_KEY", KEY)
    _seed_card()
    body = client.get("/cron/poketrace-refresh").json()
    assert body["status"] == "ok" and body["cards_priced"] == 1 and body["requests"] == 1
    assert KEY not in json.dumps(body)


def test_cron_route_without_a_key_is_a_200_skip(client, fake_api):
    response = client.get("/cron/poketrace-refresh")
    assert response.status_code == 200 and response.json()["status"] == "skipped"


def test_vercel_schedules_it_daily_after_the_price_refresh():
    config = json.loads((Path(__file__).parent.parent / "vercel.json").read_text())
    schedules = {c["path"]: c["schedule"] for c in config["crons"]}
    assert schedules["/cron/poketrace-refresh"] == "0 7 * * *"
    assert schedules["/cron/price-refresh"] == "0 6 * * *"


# --------------------------------------------------------------------------
# /sync-status: glance row and Run-now
# --------------------------------------------------------------------------
def test_sync_status_shows_skipped_no_key(client):
    client.post("/sync-status/run/poketrace-refresh")
    html = client.get("/sync-status").text
    assert "eBay prices (PokeTrace)" in html
    glance = html[html.index('id="glance-poketrace-refresh"'):]
    glance = glance[: glance.index("</section>")]
    assert "Skipped" in glance and "no key" in glance


def test_run_now_uses_the_same_job_and_lock(client, fake_api, monkeypatch):
    monkeypatch.setenv("POKETRACE_API_KEY", KEY)
    _seed_card()
    response = client.post("/sync-status/run/poketrace-refresh")
    assert response.status_code == 200
    assert "eBay prices done (reference only, no market price changed)" in response.text
    assert _logs()[-1][:3] == ("poketrace-refresh", "ok", "manual")
    assert KEY not in response.text


def test_run_now_route_is_behind_the_login():
    import app as app_module

    assert "/sync-status/run/poketrace-refresh" not in app_module._PUBLIC_PATHS
    assert "/cron/poketrace-refresh" in app_module._PUBLIC_PATHS  # own CRON_SECRET gate


# --------------------------------------------------------------------------
# Card page: the reference row and its display gate
# --------------------------------------------------------------------------
def _card_page_with(*, sample_size=551, flags=(), price_usd=342.745, failed_only=False, dex=True):
    with _session() as db:
        card = add_card(db, dex_price=3264.0 if dex else None, priced_on=dt.date.today())
        if failed_only:
            pricing.record_failure(card, P.SOURCE, dt.date.today())
        elif sample_size is not None:
            pricing.record_price(card, P.SOURCE, price_nok=round(price_usd * TEST_USD_TO_NOK, 2), price=price_usd,
                                 currency="USD", fx_rate=TEST_USD_TO_NOK, fetched_at=dt.date.today(), flags=flags,
                                 sample_size=sample_size, window_days=30)
        pricing.resolve_cards(db, today=dt.date.today())
        db.commit()
        return card.id


def _price_sources(client, card_id):
    html = client.get(f"/cards/{card_id}").text
    start = html.index('class="price-sources"')
    return html[start: html.index("</details>", start)]


def test_card_page_reference_row_with_value(client):
    section = _price_sources(client, _card_page_with())
    assert "eBay sold via PokeTrace (NM median)" in section
    assert 'class="price-source-divider"' in section
    assert 'class="price-source-reference"' in section
    assert "342.75 USD" in section
    assert "n=551 · 30d · +5% vs used" in section  # 3427.45 vs 3264.00
    assert 'title="Comparison only, never used for the market price."' in section and ">Reference<" in section
    assert section.count('class="price-used"') == 1  # on the Dex row only
    assert "Few sales" not in section
    assert "Reference, below the line, are for comparison only" in section  # the <summary> tooltip


@pytest.mark.parametrize("kwargs", [{"sample_size": 4}, {"flags": (pricing.FLAG_LOW_CONFIDENCE,)}])
def test_card_page_few_sales_hides_the_number(client, kwargs):
    section = _price_sources(client, _card_page_with(**kwargs))
    assert "eBay sold via PokeTrace (NM median)" in section and "Few sales" in section
    assert "342.75 USD" not in section and "vs used" not in section


def test_card_page_without_a_used_row_omits_the_difference(client):
    section = _price_sources(client, _card_page_with(dex=False))
    assert "342.75 USD" in section and "n=551 · 30d" in section
    assert "vs used" not in section and 'class="price-used"' not in section
    assert 'class="price-source-divider"' not in section  # nothing above it


@pytest.mark.parametrize("kwargs", [{"failed_only": True}, {"sample_size": None}])
def test_card_page_shows_nothing_without_an_ebay_price(client, kwargs):
    section = _price_sources(client, _card_page_with(**kwargs))
    assert "PokeTrace" not in section and "Reference" not in section.replace("Reference, below", "")


def test_used_marker_never_lands_on_a_reference_row(client):
    card_id = _card_page_with()
    with _session() as db:
        # Even if a stale/hand-edited market_price_source named it.
        db.get(Card, card_id).market_price_source = P.SOURCE
        db.commit()
    section = _price_sources(client, card_id)
    assert 'class="price-used"' not in section and "vs used" not in section
