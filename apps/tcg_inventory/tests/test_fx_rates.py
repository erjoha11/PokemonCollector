import copy
import datetime as dt

import httpx
import pytest

import card_images
import fx_rates

# Trimmed copy of a real Norges Bank response (2026-09-29), keeping the
# structure parse_sdmx_rates depends on. Series "0:1:0:0" is EUR because
# BASE_CUR's values list is [USD, EUR] -- decoded, not assumed.
SAMPLE = {
    "data": {
        "dataSets": [
            {
                "series": {
                    "0:0:0:0": {"attributes": [0, 0, 0, 0], "observations": {"0": ["9.576"]}},
                    "0:1:0:0": {"attributes": [0, 0, 0, 0], "observations": {"0": ["10.8735"]}},
                }
            }
        ],
        "structure": {
            "dimensions": {
                "dataset": [],
                "series": [
                    {"id": "FREQ", "keyPosition": 0, "values": [{"id": "B"}]},
                    {"id": "BASE_CUR", "keyPosition": 1, "values": [{"id": "USD"}, {"id": "EUR"}]},
                    {"id": "QUOTE_CUR", "keyPosition": 2, "values": [{"id": "NOK"}]},
                    {"id": "TENOR", "keyPosition": 3, "values": [{"id": "SP"}]},
                ],
                "observation": [
                    {"id": "TIME_PERIOD", "role": "time", "values": [{"id": "2026-09-29", "name": "2026-09-29"}]}
                ],
            },
            "attributes": {
                "dataset": [],
                "series": [
                    {"id": "DECIMALS", "values": [{"id": "4"}]},
                    {"id": "CALCULATED", "values": [{"id": "false"}]},
                    {"id": "UNIT_MULT", "values": [{"id": "0"}]},
                    {"id": "COLLECTION", "values": [{"id": "C"}]},
                ],
                "observation": [],
            },
        },
    }
}


class _FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("boom", request=None, response=self)

    def json(self):
        return self._payload


@pytest.fixture(autouse=True)
def real_fx_lookup():
    """Undo conftest's fixed rate so these tests exercise the real lookup."""
    fx_rates.reset_cache()
    yield
    fx_rates.reset_cache()


def _counting_get(monkeypatch, responses):
    """Patch httpx.get to return/raise `responses` in order (last one
    repeats); returns the list of URLs requested."""
    calls = []

    def fake_get(url, *args, **kwargs):
        calls.append(url)
        item = responses[min(len(calls) - 1, len(responses) - 1)]
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(fx_rates.httpx, "get", fake_get)
    return calls


def test_parse_decodes_series_keys_against_the_dimension_values():
    rates = fx_rates.parse_sdmx_rates(SAMPLE)
    assert rates.rates == {"USD": 9.576, "EUR": 10.8735}
    assert rates.as_of == dt.date(2026, 9, 29)
    assert rates.source == "live"


def test_parse_does_not_assume_currency_order():
    payload = copy.deepcopy(SAMPLE)
    payload["data"]["structure"]["dimensions"]["series"][1]["values"] = [{"id": "EUR"}, {"id": "USD"}]
    rates = fx_rates.parse_sdmx_rates(payload)
    assert rates.rates == {"EUR": 9.576, "USD": 10.8735}


def test_parse_applies_unit_multiplier():
    payload = copy.deepcopy(SAMPLE)
    # e.g. JPY is quoted per 100 units: UNIT_MULT=2.
    payload["data"]["structure"]["attributes"]["series"][2]["values"].append({"id": "2"})
    payload["data"]["dataSets"][0]["series"]["0:0:0:0"]["attributes"][2] = 1
    rates = fx_rates.parse_sdmx_rates(payload)
    assert rates.rates["USD"] == pytest.approx(0.09576)


def test_parse_rejects_a_payload_with_no_rates():
    payload = copy.deepcopy(SAMPLE)
    payload["data"]["dataSets"][0]["series"] = {}
    with pytest.raises(ValueError):
        fx_rates.parse_sdmx_rates(payload)


def test_get_rates_fetches_once_and_caches(monkeypatch):
    calls = _counting_get(monkeypatch, [_FakeResponse(SAMPLE)])

    first = fx_rates.get_rates()
    for _ in range(5):
        fx_rates.usd_to_nok()

    assert first.source == "live"
    assert first.to_nok("USD") == 9.576
    assert fx_rates.eur_to_nok() == 10.8735
    assert calls == [fx_rates.NORGES_BANK_URL]  # one request for the whole run


def test_get_rates_falls_back_to_the_old_constant_when_never_fetched(monkeypatch):
    calls = _counting_get(monkeypatch, [httpx.ConnectError("down")])

    rates = fx_rates.get_rates()

    assert rates.source == "fallback"
    assert rates.to_nok("USD") == 10.5
    assert rates.as_of is None
    fx_rates.get_rates()
    assert len(calls) == 2  # one attempt + one retry, then the failure is cached too


def test_get_rates_prefers_the_last_known_rate_over_the_constant(monkeypatch):
    _counting_get(monkeypatch, [_FakeResponse(SAMPLE), _FakeResponse({}, status_code=503)])
    fx_rates.get_rates()

    rates = fx_rates.get_rates(force_refresh=True)

    assert rates.source == "last-known"
    assert rates.to_nok("USD") == 9.576
    assert rates.as_of == dt.date(2026, 9, 29)


def test_get_rates_treats_a_garbled_response_as_a_failure(monkeypatch):
    _counting_get(monkeypatch, [_FakeResponse({"unexpected": True})])
    assert fx_rates.get_rates().source == "fallback"


def test_card_price_conversion_uses_the_live_rate(monkeypatch):
    def fake_get(url, *args, **kwargs):
        if url == fx_rates.NORGES_BANK_URL:
            return _FakeResponse(SAMPLE)
        return _FakeResponse(
            {"data": [{"name": "Pikachu", "number": "58", "tcgplayer": {"prices": {"normal": {"market": 10.0}}}}]}
        )

    monkeypatch.setattr(card_images.httpx, "get", fake_get)

    result = card_images.fetch_card_data("Pikachu", "Base Set", "58/102")

    assert result.tcgplayer_price == 95.76


def test_card_pricing_never_blocks_on_an_fx_outage(monkeypatch):
    def fake_get(url, *args, **kwargs):
        if url == fx_rates.NORGES_BANK_URL:
            raise httpx.ConnectError("norges bank down")
        return _FakeResponse(
            {"data": [{"name": "Pikachu", "number": "58", "tcgplayer": {"prices": {"normal": {"market": 10.0}}}}]}
        )

    monkeypatch.setattr(card_images.httpx, "get", fake_get)

    result = card_images.fetch_card_data("Pikachu", "Base Set", "58/102")

    # Doesn't block or raise, but never prices at the fallback constant
    # (issue #229): no price, flagged so the caller leaves the card due.
    assert result.tcgplayer_price is None
    assert result.fx_unavailable is True


# --- fx_rates table (issue #210) ---------------------------------------------


def test_a_live_fetch_is_stored_in_the_fx_rates_table(db_session, monkeypatch):
    from models import FxRate

    _counting_get(monkeypatch, [_FakeResponse(SAMPLE)])

    fx_rates.get_rates(db_session.get_bind())

    stored = {(r.date, r.currency): r.rate_nok for r in db_session.query(FxRate)}
    assert stored == {(dt.date(2026, 9, 29), "USD"): 9.576, (dt.date(2026, 9, 29), "EUR"): 10.8735}


def test_an_outage_falls_back_to_the_last_stored_rate_not_the_constant(db_session, monkeypatch):
    from models import FxRate

    old = dt.datetime.utcnow() - dt.timedelta(days=3)
    db_session.add_all(
        [
            FxRate(date=dt.date(2026, 9, 20), currency="USD", rate_nok=9.1, fetched_at=old),
            FxRate(date=dt.date(2026, 9, 25), currency="USD", rate_nok=9.4, fetched_at=old),
        ]
    )
    db_session.commit()
    _counting_get(monkeypatch, [httpx.ConnectError("down")])

    rates = fx_rates.get_rates(db_session.get_bind())

    assert rates.source == "stored"
    assert rates.to_nok("USD") == 9.4  # the latest stored observation
    assert rates.to_nok("EUR") == fx_rates.FALLBACK_RATES["EUR"]  # none stored for EUR
    assert rates.as_of == dt.date(2026, 9, 25)


def test_a_rate_stored_by_another_invocation_is_reused_without_a_request(db_session, monkeypatch):
    from models import FxRate

    db_session.add(FxRate(date=dt.date(2026, 9, 29), currency="USD", rate_nok=9.5, fetched_at=dt.datetime.utcnow()))
    db_session.commit()
    calls = _counting_get(monkeypatch, [_FakeResponse(SAMPLE)])

    rates = fx_rates.get_rates(db_session.get_bind())

    assert calls == []
    assert (rates.source, rates.to_nok("USD")) == ("live", 9.5)


def test_an_old_stored_rate_is_refetched_not_reused(db_session, monkeypatch):
    from models import FxRate

    db_session.add(
        FxRate(date=dt.date(2026, 9, 1), currency="USD", rate_nok=9.9, fetched_at=dt.datetime.utcnow() - dt.timedelta(days=2))
    )
    db_session.commit()
    calls = _counting_get(monkeypatch, [_FakeResponse(SAMPLE)])

    rates = fx_rates.get_rates(db_session.get_bind())

    assert calls == [fx_rates.NORGES_BANK_URL]
    assert rates.to_nok("USD") == 9.576


def test_a_broken_fx_rates_table_never_blocks_pricing(monkeypatch):
    from sqlalchemy import create_engine

    engine = create_engine("sqlite:///:memory:")  # no tables at all
    _counting_get(monkeypatch, [httpx.ConnectError("down")])

    assert fx_rates.get_rates(engine).source == "fallback"
