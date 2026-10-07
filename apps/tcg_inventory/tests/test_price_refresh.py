"""The daily pokemontcg.io price pass: by stored ID, in batches (issue #349).

Most tests drive price_refresh with a FakeClient (like test_tcgdex_prices);
the client's own retry/backoff is tested against fake httpx responses at
the bottom."""
import datetime as dt

import httpx
import pytest

import fx_rates
import masterdata
import pokemontcg_client
import price_refresh
import pricing
from models import Card, FxRate

TODAY = dt.date.today()
UPDATED = TODAY.strftime("%Y/%m/%d")


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
class FakeClient:
    """Answers cards_by_ids from `catalog` (by ID) and search from
    `searches` (by name). `fail` makes every request raise that error."""

    def __init__(self, catalog=(), searches=None, fail=None):
        self.catalog = {card["id"]: card for card in catalog}
        self.searches_by_name = searches or {}
        self.fail = fail
        self.batches: list[list[str]] = []
        self.searched: list[str] = []
        self.calls = self.batch_requests = self.searches = 0

    def cards_by_ids(self, ids):
        assert len(ids) <= pokemontcg_client.CHUNK_SIZE
        self.calls += 1
        self.batch_requests += 1
        self.batches.append(list(ids))
        if self.fail:
            raise self.fail
        return {i: self.catalog[i] for i in ids if i in self.catalog}

    def search(self, name, set_name, number):
        self.calls += 1
        self.searches += 1
        self.searched.append(name)
        if self.fail:
            raise self.fail
        return self.searches_by_name.get(name)

    @property
    def asked(self):
        return [card_id for batch in self.batches for card_id in batch]


def api(card_id, name, number, prices=None, updated=UPDATED):
    prices = {"normal": 1.5} if prices is None else prices
    return {
        "id": card_id,
        "name": name,
        "number": number,
        "tcgplayer": {"updatedAt": updated, "prices": {key: {"market": value} for key, value in prices.items()}},
    }


def add_card(db, card_id="sv2-109", name="Sudowoodo", number="109/193", variant="Normal", **kw):
    card = Card(card_id=card_id, name=name, number=number, variant=variant, qty=1, **kw)
    db.add(card)
    masterdata.link_card(db, card)
    db.commit()
    return card


def row(card):
    return pricing.get_row(card, pricing.SOURCE_POKEMONTCG)


def pokemontcg_id(card):
    return next(r for r in card.master_card.external_ids if r.source == "pokemontcg")


def refresh(db, client, **kw):
    result = price_refresh.refresh_stale_prices(db, client=client, **kw)
    db.expire_all()
    return result


# --------------------------------------------------------------------------
# By ID, in batches
# --------------------------------------------------------------------------
def test_an_international_card_is_priced_by_its_stored_id(db_session):
    card = add_card(db_session)
    client = FakeClient([api("sv2-109", "Sudowoodo", "109", {"normal": 0.12, "reverseHolofoil": 0.27})])

    result = refresh(db_session, client)

    assert client.asked == ["sv2-109"] and client.searched == []
    r = row(card)
    assert (r.price, r.currency, r.fx_rate, r.price_nok, r.variant_key) == (0.12, "USD", 10.0, 1.2, "normal")
    assert (r.fetched_at, r.source_updated_at, r.lookup_failed_at, r.flags) == (TODAY, TODAY, None, None)
    assert (card.tcgplayer_price, card.tcgplayer_price_updated_at) == (1.2, TODAY)
    assert (card.market_price, card.market_price_source) == (1.2, "pokemontcg")
    assert (result.cards_checked, result.cards_updated, result.requests, result.batch_requests) == (1, 1, 1, 1)
    assert (result.cards_unmatched, result.transient_errors, result.stopped) == ([], 0, None)


def test_japanese_chinese_and_unlinked_cards_are_never_asked_for(db_session):
    add_card(db_session, card_id="jpn_sv2a-168", name="Charizard ex", number="168/165")
    add_card(db_session, card_id="scn_csv9-79", name="Pikachu", number="79")
    db_session.add(Card(card_id="not a dex id", name="Odd", qty=1))
    db_session.commit()
    client = FakeClient()

    result = refresh(db_session, client)

    assert client.batches == [] and client.searched == []
    assert (result.cards_checked, result.requests) == (0, 0)


def test_variants_sharing_an_id_cost_one_lookup_and_each_picks_its_own_print(db_session):
    normal = add_card(db_session, variant="Normal")
    reverse = add_card(db_session, variant="Reverse Holo")
    client = FakeClient([api("sv2-109", "Sudowoodo", "109", {"normal": 0.12, "reverseHolofoil": 0.27})])

    refresh(db_session, client)

    assert client.asked == ["sv2-109"]
    assert (row(normal).variant_key, row(normal).price) == ("normal", 0.12)
    assert (row(reverse).variant_key, row(reverse).price) == ("reverseHolofoil", 0.27)


def test_ids_are_chunked_into_ceil_n_over_chunk_size_requests(db_session, monkeypatch):
    monkeypatch.setattr(pokemontcg_client, "CHUNK_SIZE", 2)
    catalog = []
    for n in range(1, 6):
        add_card(db_session, card_id=f"sv2-{n}", name=f"Card {n}", number=f"{n}/193")
        catalog.append(api(f"sv2-{n}", f"Card {n}", str(n)))
    client = FakeClient(catalog)

    result = refresh(db_session, client)

    assert [len(batch) for batch in client.batches] == [2, 2, 1]
    assert (result.batch_requests, result.cards_updated) == (3, 5)


def test_a_card_priced_today_is_not_due_but_yesterdays_is(db_session):
    today_card = add_card(db_session, card_id="sv2-1", name="Card 1", number="1/193")
    old_card = add_card(db_session, card_id="sv2-2", name="Card 2", number="2/193")
    pricing.record_price(today_card, "pokemontcg", price_nok=1.0, fetched_at=TODAY)
    pricing.record_price(old_card, "pokemontcg", price_nok=1.0, fetched_at=TODAY - dt.timedelta(days=1))
    db_session.commit()
    client = FakeClient([api("sv2-2", "Card 2", "2", {"normal": 3.0})])

    refresh(db_session, client)

    assert client.asked == ["sv2-2"]
    assert row(old_card).price_nok == 30.0


def test_a_ball_pattern_card_is_never_asked_for_and_loses_its_old_price(db_session):
    ball = add_card(db_session, card_id="sv8pt5-1", name="Exeggcute", number="1/131", variant="Poké Ball Holo")
    plain = add_card(db_session, card_id="sv8pt5-2", name="Exeggutor", number="2/131", variant="Normal")
    stale = TODAY - dt.timedelta(days=3)
    for card in (ball, plain):
        pricing.record_price(card, "pokemontcg", price_nok=3.0, fetched_at=stale)
        card.tcgplayer_price = 3.0
    db_session.commit()
    client = FakeClient([api("sv8pt5-2", "Exeggutor", "2", {"normal": 0.4})])

    result = refresh(db_session, client)

    assert client.asked == ["sv8pt5-2"]
    assert (result.other_print_dropped, result.cards_checked) == (1, 1)
    ball = db_session.get(Card, ball.id)
    assert ball.prices == [] and ball.tcgplayer_price is None and ball.market_price_source is None


# --------------------------------------------------------------------------
# Verification, upstream age, no usable price
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "hit, reason",
    [
        (api("sv2-109", "Charizard", "109"), "name"),
        (api("sv2-109", "Sudowoodo", "110"), "number"),
    ],
)
def test_a_hit_that_fails_verification_is_stamped_and_listed(db_session, hit, reason):
    card = add_card(db_session)
    pricing.record_price(card, "pokemontcg", price_nok=5.0, fetched_at=TODAY - dt.timedelta(days=2))
    db_session.commit()

    result = refresh(db_session, FakeClient([hit]))

    assert (row(card).price_nok, row(card).lookup_failed_at) == (5.0, TODAY)  # old price kept
    [line] = result.cards_unmatched
    assert line.startswith("Sudowoodo (sv2-109, Normal): ") and reason in line
    assert result.cards_updated == 0


def test_promo_numbers_and_loose_names_still_verify(db_session):
    card = add_card(db_session, card_id="swshp-SWSH050", name="Charizard ex", number="SWSH050")
    refresh(db_session, FakeClient([api("swshp-SWSH050", "Charizard-EX", "SWSH050")]))
    assert row(card).price_nok == 15.0


def test_no_usable_tcgplayer_price_is_stamped(db_session):
    card = add_card(db_session)
    hit = api("sv2-109", "Sudowoodo", "109", prices={})

    result = refresh(db_session, FakeClient([hit]))

    assert (row(card).price_nok, row(card).lookup_failed_at) == (None, TODAY)
    assert "no TCGplayer price" in result.cards_unmatched[0]


def test_a_price_tcgplayer_last_updated_too_long_ago_is_not_used(db_session):
    card = add_card(db_session)
    pricing.record_price(card, "pokemontcg", price_nok=5.0, fetched_at=TODAY - dt.timedelta(days=2))
    db_session.commit()
    old = (TODAY - dt.timedelta(days=price_refresh.MAX_UPSTREAM_AGE_DAYS + 1)).strftime("%Y/%m/%d")

    result = refresh(db_session, FakeClient([api("sv2-109", "Sudowoodo", "109", updated=old)]))

    assert (row(card).price_nok, row(card).lookup_failed_at) == (5.0, TODAY)
    assert "last updated" in result.cards_unmatched[0]


def test_parse_updated_at():
    assert price_refresh.parse_updated_at("2026/10/07") == dt.date(2026, 10, 7)
    assert price_refresh.parse_updated_at("2026-10-07T00:00:00Z") == dt.date(2026, 10, 7)
    assert price_refresh.parse_updated_at("soon") is None
    assert price_refresh.parse_updated_at(None) is None


# --------------------------------------------------------------------------
# Fallback search for an ID that isn't on pokemontcg.io
# --------------------------------------------------------------------------
def test_a_missing_id_falls_back_to_search_and_stores_the_found_id_as_heuristic(db_session):
    card = add_card(db_session, card_id="sv35-27", name="Sandshrew", number="27/165", set="151")
    client = FakeClient(searches={"Sandshrew": api("sv3pt5-27", "Sandshrew", "27", {"normal": 0.2})})

    result = refresh(db_session, client)

    assert client.asked == ["sv35-27"] and client.searched == ["Sandshrew"]
    assert row(card).price_nok == 2.0
    mapping = pokemontcg_id(card)
    assert (mapping.external_id, mapping.matched_by) == ("sv3pt5-27", masterdata.MATCHED_HEURISTIC)
    assert (result.ids_found, result.fallback_searches, result.cards_unmatched) == (1, 1, [])

    # The next day goes by the found ID, no search.
    tomorrow_client = FakeClient([api("sv3pt5-27", "Sandshrew", "27", {"normal": 0.3})])
    refresh(db_session, tomorrow_client, today=TODAY + dt.timedelta(days=1))
    assert (tomorrow_client.asked, tomorrow_client.searched) == (["sv3pt5-27"], [])
    assert row(card).price_nok == 3.0


def test_the_next_dex_sync_does_not_revert_a_found_id(db_session):
    from conftest import make_csv
    from importer import import_dex_csv_files

    card = add_card(db_session, card_id="sv35-27", name="Sandshrew", number="27/165", set="151")
    refresh(db_session, FakeClient(searches={"Sandshrew": api("sv3pt5-27", "Sandshrew", "27")}))

    # A sync with the same card plus its Reverse Holo (a new card, so a new
    # link_card), and a full backfill pass.
    csv = make_csv(
        "My Collection",
        [
            {"id": "sv35-27", "name": "Sandshrew", "number": "27/165", "variant": "Normal", "locale": "EN"},
            {"id": "sv35-27", "name": "Sandshrew", "number": "27/165", "variant": "Reverse Holo", "locale": "EN"},
        ],
    )
    import_dex_csv_files(db_session, [("main.csv", csv)])
    masterdata.backfill_master_cards(db_session)
    db_session.expire_all()

    mapping = pokemontcg_id(db_session.get(Card, card.id))
    assert (mapping.external_id, mapping.matched_by) == ("sv3pt5-27", masterdata.MATCHED_HEURISTIC)


def test_a_missing_id_with_no_confident_search_hit_is_stamped_then_backed_off(db_session):
    card = add_card(db_session, card_id="me5-80", name="Umbreon", number="80/120", set="Pitch Black")
    client = FakeClient(searches={"Umbreon": api("sv8-80", "Umbreon ex", "80")})

    result = refresh(db_session, client)

    assert row(card).lookup_failed_at == TODAY and row(card).price_nok is None
    assert result.cards_low_confidence == ["Umbreon (me5-80, Normal)"]
    assert "not on pokemontcg.io" in result.cards_unmatched[0]
    assert pokemontcg_id(card).matched_by == masterdata.MATCHED_DERIVED  # nothing stored

    # Inside the retry window: still asked for by ID (free), never searched,
    # not re-stamped, not listed again.
    later = TODAY + dt.timedelta(days=3)
    client = FakeClient()
    result = refresh(db_session, client, today=later)
    assert (client.asked, client.searched) == (["me5-80"], [])
    assert (row(card).lookup_failed_at, result.cards_backed_off, result.cards_unmatched) == (TODAY, 1, [])

    # ...and priced as soon as pokemontcg.io has it.
    client = FakeClient([api("me5-80", "Umbreon", "80", {"holofoil": 4.0})])
    refresh(db_session, client, today=later + dt.timedelta(days=1))
    assert (row(card).price_nok, row(card).lookup_failed_at) == (40.0, None)

    # Past the window, the search runs again.
    client = FakeClient()
    refresh(db_session, client, today=later + dt.timedelta(days=price_refresh.PRICE_RETRY_AFTER_DAYS + 5))
    assert client.searched == ["Umbreon"]


def test_a_card_stamped_by_the_old_name_search_is_priced_by_id_right_away(db_session):
    card = add_card(db_session)
    pricing.record_failure(card, "pokemontcg", TODAY - dt.timedelta(days=2))
    db_session.commit()

    refresh(db_session, FakeClient([api("sv2-109", "Sudowoodo", "109")]))

    assert (row(card).price_nok, row(card).lookup_failed_at) == (15.0, None)


def test_a_manual_mapping_missing_from_the_response_is_not_searched(db_session):
    card = add_card(db_session)
    masterdata.set_external_id(db_session, card.master_card, "pokemontcg", "sv2-999", masterdata.MATCHED_MANUAL)
    db_session.commit()
    client = FakeClient(searches={"Sudowoodo": api("sv2-109", "Sudowoodo", "109")})

    result = refresh(db_session, client)

    assert (client.asked, client.searched) == (["sv2-999"], [])
    assert row(card).lookup_failed_at == TODAY
    assert result.cards_unmatched


def test_fallback_searches_are_capped_per_run_and_the_rest_stay_due(db_session, monkeypatch):
    monkeypatch.setattr(price_refresh, "MAX_FALLBACK_SEARCHES_PER_RUN", 1)
    first = add_card(db_session, card_id="me5-1", name="Card 1", number="1/120")
    second = add_card(db_session, card_id="me5-2", name="Card 2", number="2/120")
    client = FakeClient()

    result = refresh(db_session, client)

    assert client.searched == ["Card 1"]
    assert row(first).lookup_failed_at == TODAY
    assert row(second) is None  # not stamped: still due
    assert result.cards_deferred == 1


def test_cards_that_had_a_price_are_searched_first(db_session, monkeypatch):
    """A previously priced card (found by the old search) is the likeliest
    to be found again, so it goes before never-priced ones (often a set
    pokemontcg.io doesn't have yet)."""
    monkeypatch.setattr(price_refresh, "MAX_FALLBACK_SEARCHES_PER_RUN", 1)
    add_card(db_session, card_id="me5-1", name="Never Priced", number="1/120")
    priced = add_card(db_session, card_id="sv35-27", name="Sandshrew", number="27/165", set="151")
    pricing.record_price(priced, "pokemontcg", price_nok=2.0, fetched_at=TODAY - dt.timedelta(days=3))
    db_session.commit()
    client = FakeClient(searches={"Sandshrew": api("sv3pt5-27", "Sandshrew", "27", {"normal": 0.25})})

    refresh(db_session, client)

    assert client.searched == ["Sandshrew"]
    assert row(priced).price_nok == 2.5


def test_a_transient_search_failure_stamps_nothing(db_session):
    card = add_card(db_session, card_id="me5-1", name="Card 1", number="1/120")

    class SearchDown(FakeClient):
        def search(self, name, set_name, number):
            self.calls += 1
            self.searches += 1
            raise pokemontcg_client.TransientError("HTTP 502")

    result = refresh(db_session, SearchDown())

    assert row(card) is None
    assert (result.transient_errors, result.cards_deferred, result.cards_unmatched) == (1, 1, [])


# --------------------------------------------------------------------------
# Outages and budgets
# --------------------------------------------------------------------------
def _all_5xx(monkeypatch, calls):
    def fake_get(url, params=None, timeout=None):
        calls.append(params)
        return httpx.Response(502, request=httpx.Request("GET", url))

    monkeypatch.setattr(pokemontcg_client.httpx, "get", fake_get)


def test_a_run_where_every_request_5xxs_writes_nothing_and_stops_at_the_error_limit(db_session, monkeypatch):
    monkeypatch.setattr(pokemontcg_client, "CHUNK_SIZE", 1)
    cards = [add_card(db_session, card_id=f"sv2-{n}", name=f"Card {n}", number=f"{n}/193") for n in range(1, 6)]
    stale = TODAY - dt.timedelta(days=2)
    pricing.record_price(cards[0], "pokemontcg", price_nok=5.0, fetched_at=stale)
    db_session.commit()
    calls = []
    _all_5xx(monkeypatch, calls)

    result = refresh(db_session, None, time_budget_s=60)

    assert result.stopped == "errors"
    assert result.transient_errors == price_refresh.MAX_CONSECUTIVE_ERRORS
    assert result.requests == len(calls) == 2 * price_refresh.MAX_CONSECUTIVE_ERRORS  # one retry each
    assert result.cards_deferred == 5 and result.cards_checked == 0
    assert (row(cards[0]).price_nok, row(cards[0]).fetched_at, row(cards[0]).lookup_failed_at) == (5.0, stale, None)
    assert all(row(card) is None for card in cards[1:])


def test_a_persisting_429_stops_the_run_at_once(db_session):
    add_card(db_session)
    client = FakeClient(fail=pokemontcg_client.RateLimited("HTTP 429"))

    result = refresh(db_session, client)

    assert (result.stopped, result.transient_errors, result.cards_deferred) == ("rate_limited", 1, 1)


def test_the_time_budget_stops_the_run_and_leaves_cards_due(db_session):
    card = add_card(db_session)
    client = FakeClient([api("sv2-109", "Sudowoodo", "109")])

    result = refresh(db_session, client, time_budget_s=-1)

    assert (client.batches, result.stopped, result.cards_deferred) == ([], "time", 1)
    assert row(card) is None


def test_refresh_writes_nothing_at_the_fx_fallback_rate_and_reports_degraded(db_session):
    card = add_card(db_session)
    fx_rates.reset_cache()  # network off + empty fx_rates table -> the constant
    client = FakeClient([api("sv2-109", "Sudowoodo", "109")])

    result = refresh(db_session, client)

    assert (result.status, result.fx_source, result.degraded_reason) == ("degraded", "fallback", fx_rates.FALLBACK_REASON)
    assert (result.cards_checked, result.cards_skipped, client.calls) == (0, 1, 0)
    assert row(card) is None


def test_refresh_with_a_stored_rate_prices_at_it(db_session):
    card = add_card(db_session)
    old = dt.datetime.utcnow() - dt.timedelta(days=3)
    for currency, rate in (("USD", 9.4), ("EUR", 10.9)):
        db_session.add(FxRate(date=TODAY - dt.timedelta(days=3), currency=currency, rate_nok=rate, fetched_at=old))
    db_session.commit()
    fx_rates.reset_cache()

    result = refresh(db_session, FakeClient([api("sv2-109", "Sudowoodo", "109", {"normal": 2.0})]))

    assert (result.status, result.fx_source, result.usd_to_nok) == ("ok", "stored", 9.4)
    assert (row(card).price_nok, row(card).fx_rate) == (18.8, 9.4)


def test_a_variant_uncertain_price_is_stored_flagged_and_listed(db_session):
    card = add_card(db_session, variant=None)
    hit = api("sv2-109", "Sudowoodo", "109", {"normal": 0.1, "reverseHolofoil": 0.3})

    result = refresh(db_session, FakeClient([hit]))

    assert (row(card).price, row(card).flags) == (0.1, "variant_price_uncertain")
    assert result.cards_variant_uncertain == ["Sudowoodo (sv2-109)"]


# --------------------------------------------------------------------------
# --reprice-all
# --------------------------------------------------------------------------
def test_reprice_all_refetches_every_priced_card_whatever_its_date(db_session):
    fresh = add_card(db_session, card_id="sv2-1", name="Card 1", number="1/193")
    never = add_card(db_session, card_id="sv2-2", name="Card 2", number="2/193")
    pricing.record_price(fresh, "pokemontcg", price_nok=1.0, fetched_at=TODAY)
    db_session.commit()
    client = FakeClient([api("sv2-1", "Card 1", "1", {"normal": 0.5}), api("sv2-2", "Card 2", "2")])

    result = price_refresh.reprice_all(db_session, client=client)
    db_session.expire_all()

    assert client.asked == ["sv2-1"]  # only cards that already have a price
    assert row(fresh).price_nok == 5.0 and row(never) is None
    assert result.cards_updated == 1


def test_reprice_all_keeps_the_old_price_and_date_when_nothing_usable_comes_back(db_session):
    card = add_card(db_session)
    stale = TODAY - dt.timedelta(days=4)
    pricing.record_price(card, "pokemontcg", price_nok=1.0, fetched_at=stale)
    db_session.commit()
    client = FakeClient()  # ID missing

    result = price_refresh.reprice_all(db_session, client=client)
    db_session.expire_all()

    assert (row(card).price_nok, row(card).fetched_at, row(card).lookup_failed_at) == (1.0, stale, None)
    assert client.searched == [] and result.cards_unmatched == []


def test_reprice_all_respects_limit_oldest_first(db_session):
    for n, day in ((1, 5), (2, 1), (3, 3)):
        card = add_card(db_session, card_id=f"sv2-{n}", name=f"Card {n}", number=f"{n}/193")
        pricing.record_price(card, "pokemontcg", price_nok=1.0, fetched_at=dt.date(2026, 9, day))
    db_session.commit()
    client = FakeClient()

    price_refresh.reprice_all(db_session, limit=2, client=client)

    assert client.asked == ["sv2-2", "sv2-3"]


def test_reprice_all_also_skips_at_the_fx_fallback_rate(db_session):
    card = add_card(db_session)
    pricing.record_price(card, "pokemontcg", price_nok=1.0, fetched_at=TODAY)
    db_session.commit()
    fx_rates.reset_cache()

    result = price_refresh.reprice_all(db_session, client=FakeClient([api("sv2-109", "Sudowoodo", "109")]))

    assert result.status == "degraded" and row(card).price_nok == 1.0


# --------------------------------------------------------------------------
# The HTTP client
# --------------------------------------------------------------------------
def _responses(monkeypatch, *responses):
    seen = []
    queue = list(responses)

    def fake_get(url, params=None, timeout=None):
        seen.append(params)
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        status, body = item
        return httpx.Response(status, json=body, request=httpx.Request("GET", url))

    monkeypatch.setattr(pokemontcg_client.httpx, "get", fake_get)
    return seen


def test_client_asks_for_quoted_ids_and_only_the_price_fields(monkeypatch):
    seen = _responses(monkeypatch, (200, {"data": [{"id": "sv2-109", "name": "Sudowoodo"}]}))

    found = pokemontcg_client.Client().cards_by_ids(["sv2-109", "sv3pt5-27"])

    assert list(found) == ["sv2-109"]
    assert seen == [{"q": 'id:"sv2-109" OR id:"sv3pt5-27"', "select": "id,name,number,tcgplayer", "pageSize": 250}]


def test_client_retries_a_5xx_or_timeout_once(monkeypatch):
    sleeps = []
    _responses(monkeypatch, (502, {}), (200, {"data": []}))
    client = pokemontcg_client.Client(sleep=sleeps.append)
    assert client.cards_by_ids(["a-1"]) == {} and client.calls == 2

    _responses(monkeypatch, httpx.ReadTimeout("slow"), httpx.ReadTimeout("slow"))
    with pytest.raises(pokemontcg_client.TransientError):
        pokemontcg_client.Client(sleep=sleeps.append).cards_by_ids(["a-1"])
    assert pokemontcg_client.BACKOFF_S in sleeps


def test_client_raises_rate_limited_on_a_persisting_429(monkeypatch):
    _responses(monkeypatch, (429, {}), (429, {}))
    with pytest.raises(pokemontcg_client.RateLimited):
        pokemontcg_client.Client(sleep=lambda s: None).cards_by_ids(["a-1"])


def test_client_search_uses_the_name_set_number_query(monkeypatch):
    seen = _responses(monkeypatch, (200, {"data": [{"id": "sv3pt5-27"}]}))
    hit = pokemontcg_client.Client().search("Sandshrew", "151", "27/165")
    assert hit == {"id": "sv3pt5-27"}
    assert seen[0]["q"] == 'name:"Sandshrew" set.name:"151" number:27' and seen[0]["pageSize"] == 1
