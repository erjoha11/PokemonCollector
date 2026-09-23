import httpx

import card_images


class _FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("boom", request=None, response=self)

    def json(self):
        return self._payload


def test_fetch_image_url_returns_the_small_image_of_the_first_match(monkeypatch):
    captured = {}

    def fake_get(url, params, timeout):
        captured["url"] = url
        captured["params"] = params
        return _FakeResponse({"data": [{"images": {"small": "https://example.com/a.png", "large": "https://example.com/a_hi.png"}}]})

    monkeypatch.setattr(card_images.httpx, "get", fake_get)

    result = card_images.fetch_image_url("Pikachu", "Base Set", "58/102")

    assert result == "https://example.com/a.png"
    assert captured["url"] == card_images._API_URL
    assert 'name:"Pikachu"' in captured["params"]["q"]
    assert 'set.name:"Base Set"' in captured["params"]["q"]
    # Dex's "58/102" is (this card)/(set size) -- only the printed number
    # is meaningful to the API.
    assert "number:58" in captured["params"]["q"]


def test_fetch_image_url_returns_none_when_no_match(monkeypatch):
    monkeypatch.setattr(card_images.httpx, "get", lambda *a, **kw: _FakeResponse({"data": []}))
    assert card_images.fetch_image_url("Not A Real Card", None, None) is None


def test_fetch_image_url_returns_none_on_network_error_instead_of_raising(monkeypatch):
    def raising_get(*args, **kwargs):
        raise httpx.ConnectError("no network")

    monkeypatch.setattr(card_images.httpx, "get", raising_get)
    assert card_images.fetch_image_url("Pikachu", "Base Set", "58/102") is None


def test_fetch_image_url_returns_none_on_http_error_status(monkeypatch):
    monkeypatch.setattr(card_images.httpx, "get", lambda *a, **kw: _FakeResponse({}, status_code=500))
    assert card_images.fetch_image_url("Pikachu", "Base Set", "58/102") is None


def test_fetch_image_url_skips_the_lookup_entirely_for_a_nameless_card(monkeypatch):
    def fail_if_called(*args, **kwargs):
        raise AssertionError("should not have made a request")

    monkeypatch.setattr(card_images.httpx, "get", fail_if_called)
    assert card_images.fetch_image_url("", None, None) is None


def test_fetch_image_url_omits_set_and_number_when_not_provided(monkeypatch):
    captured = {}

    def fake_get(url, params, timeout):
        captured["params"] = params
        return _FakeResponse({"data": []})

    monkeypatch.setattr(card_images.httpx, "get", fake_get)

    card_images.fetch_image_url("Pikachu", None, None)

    assert captured["params"]["q"] == 'name:"Pikachu"'


def test_fetch_card_data_returns_image_and_price_from_one_call(monkeypatch):
    monkeypatch.setattr(
        card_images.httpx,
        "get",
        lambda *a, **kw: _FakeResponse(
            {
                "data": [
                    {
                        "name": "Pikachu",
                        "number": "58",
                        "images": {"small": "https://example.com/a.png"},
                        "tcgplayer": {"prices": {"holofoil": {"market": 12.5}}},
                    }
                ]
            }
        ),
    )

    result = card_images.fetch_card_data("Pikachu", "Base Set", "58/102")

    assert result.image_url == "https://example.com/a.png"
    # The API returns USD; this app displays everything in NOK (see
    # _USD_TO_NOK), so the raw 12.5 must come back converted, not verbatim.
    assert result.tcgplayer_price == round(12.5 * card_images._USD_TO_NOK, 2)
    assert result.low_confidence_match is False


def test_fetch_card_data_price_is_none_when_no_tcgplayer_data(monkeypatch):
    monkeypatch.setattr(
        card_images.httpx,
        "get",
        lambda *a, **kw: _FakeResponse(
            {"data": [{"name": "Pikachu", "number": "58", "images": {"small": "https://example.com/a.png"}}]}
        ),
    )

    result = card_images.fetch_card_data("Pikachu", "Base Set", "58/102")

    assert result.image_url == "https://example.com/a.png"
    assert result.tcgplayer_price is None


def test_fetch_card_data_returns_nones_on_no_match(monkeypatch):
    monkeypatch.setattr(card_images.httpx, "get", lambda *a, **kw: _FakeResponse({"data": []}))

    result = card_images.fetch_card_data("Not A Real Card", None, None)

    assert result.image_url is None
    assert result.tcgplayer_price is None


def test_fetch_card_data_still_returns_image_but_not_price_on_low_confidence_match(monkeypatch):
    # The API's fuzzy name search can return a same-named-but-different card
    # (or a mismatched printed number) as the top result -- a wrong image is
    # cosmetic, a wrong price silently corrupts the Market Value KPI, so
    # price must be withheld while the image is still trusted.
    monkeypatch.setattr(
        card_images.httpx,
        "get",
        lambda *a, **kw: _FakeResponse(
            {
                "data": [
                    {
                        "name": "Raichu",
                        "number": "26",
                        "images": {"small": "https://example.com/a.png"},
                        "tcgplayer": {"prices": {"holofoil": {"market": 12.5}}},
                    }
                ]
            }
        ),
    )

    result = card_images.fetch_card_data("Pikachu", "Base Set", "58/102")

    assert result.image_url == "https://example.com/a.png"
    assert result.tcgplayer_price is None
    assert result.low_confidence_match is True


def test_fetch_card_data_number_mismatch_is_also_low_confidence(monkeypatch):
    monkeypatch.setattr(
        card_images.httpx,
        "get",
        lambda *a, **kw: _FakeResponse(
            {
                "data": [
                    {
                        "name": "Pikachu",
                        "number": "99",
                        "images": {"small": "https://example.com/a.png"},
                        "tcgplayer": {"prices": {"holofoil": {"market": 12.5}}},
                    }
                ]
            }
        ),
    )

    result = card_images.fetch_card_data("Pikachu", "Base Set", "58/102")

    assert result.tcgplayer_price is None
    assert result.low_confidence_match is True


def _fake_multi_variant_response(**prices):
    return _FakeResponse(
        {
            "data": [
                {
                    "name": "Pikachu",
                    "number": "58",
                    "images": {"small": "https://example.com/a.png"},
                    "tcgplayer": {"prices": {key: {"market": market} for key, market in prices.items()}},
                }
            ]
        }
    )


def test_fetch_card_data_single_priced_variant_is_never_uncertain(monkeypatch):
    # Only one priced print exists -- nothing to disambiguate, regardless of
    # what Dex's own Variant says (or doesn't say).
    monkeypatch.setattr(card_images.httpx, "get", lambda *a, **kw: _fake_multi_variant_response(holofoil=12.5))

    result = card_images.fetch_card_data("Pikachu", "Base Set", "58/102", "Some Unrelated Variant Text")

    assert result.tcgplayer_price == round(12.5 * card_images._USD_TO_NOK, 2)
    assert result.variant_price_uncertain is False


def test_fetch_card_data_matches_reverse_holo_variant_among_several(monkeypatch):
    monkeypatch.setattr(
        card_images.httpx,
        "get",
        lambda *a, **kw: _fake_multi_variant_response(normal=5.0, reverseHolofoil=20.0),
    )

    result = card_images.fetch_card_data("Pikachu", "Base Set", "58/102", "Reverse Holo")

    assert result.tcgplayer_price == round(20.0 * card_images._USD_TO_NOK, 2)
    assert result.variant_price_uncertain is False


def test_fetch_card_data_matches_normal_variant_among_several(monkeypatch):
    monkeypatch.setattr(
        card_images.httpx,
        "get",
        lambda *a, **kw: _fake_multi_variant_response(normal=5.0, reverseHolofoil=20.0),
    )

    result = card_images.fetch_card_data("Pikachu", "Base Set", "58/102", "Normal")

    assert result.tcgplayer_price == round(5.0 * card_images._USD_TO_NOK, 2)
    assert result.variant_price_uncertain is False


def test_fetch_card_data_matches_1st_edition_variant_among_several(monkeypatch):
    monkeypatch.setattr(
        card_images.httpx,
        "get",
        lambda *a, **kw: _fake_multi_variant_response(**{"unlimited": 8.0, "1stEditionHolofoil": 50.0}),
    )

    result = card_images.fetch_card_data("Pikachu", "Base Set", "58/102", "1st Edition Holo")

    assert result.tcgplayer_price == round(50.0 * card_images._USD_TO_NOK, 2)
    assert result.variant_price_uncertain is False


def test_fetch_card_data_ambiguous_variant_among_several_falls_back_but_flags_uncertain(monkeypatch):
    # A plain "Holo" doesn't disambiguate between holofoil/reverseHolofoil/
    # unlimitedHolofoil on purpose (see _VARIANT_HINTS) -- still returns a
    # best-effort price (the first one present) rather than none at all, but
    # flags it so callers can surface it instead of trusting a guess.
    monkeypatch.setattr(
        card_images.httpx,
        "get",
        lambda *a, **kw: _fake_multi_variant_response(holofoil=12.5, reverseHolofoil=20.0),
    )

    result = card_images.fetch_card_data("Pikachu", "Base Set", "58/102", "Holo")

    assert result.tcgplayer_price == round(12.5 * card_images._USD_TO_NOK, 2)
    assert result.variant_price_uncertain is True


def test_fetch_card_data_missing_variant_with_several_priced_prints_is_uncertain(monkeypatch):
    monkeypatch.setattr(
        card_images.httpx,
        "get",
        lambda *a, **kw: _fake_multi_variant_response(normal=5.0, reverseHolofoil=20.0),
    )

    result = card_images.fetch_card_data("Pikachu", "Base Set", "58/102")

    assert result.tcgplayer_price == round(5.0 * card_images._USD_TO_NOK, 2)
    assert result.variant_price_uncertain is True


# --- fetch_image_by_card_id -------------------------------------------------


def _routes(monkeypatch, responses):
    """Fake httpx.get serving `responses` (url -> payload, or an int status)."""
    calls = []

    def fake_get(url, timeout=None, **kwargs):
        calls.append(url)
        payload = responses.get(url, 404)
        if isinstance(payload, int):
            return _FakeResponse({}, status_code=payload)
        return _FakeResponse(payload)

    monkeypatch.setattr(card_images.httpx, "get", fake_get)
    return calls


def test_international_card_is_fetched_by_its_dex_id(monkeypatch):
    _routes(monkeypatch, {
        "https://api.pokemontcg.io/v2/cards/ex5-4": {"data": {
            "id": "ex5-4", "name": "Celebi", "number": "4",
            "images": {"small": "https://images.pokemontcg.io/ex5/4.png"},
        }},
    })
    assert card_images.fetch_image_by_card_id("ex5-4", "Dark Celebi", "4/101") == "https://images.pokemontcg.io/ex5/4.png"


def test_international_card_with_a_different_number_or_name_is_rejected(monkeypatch):
    card = {"id": "ex5-4", "name": "Celebi", "number": "5", "images": {"small": "x.png"}}
    _routes(monkeypatch, {"https://api.pokemontcg.io/v2/cards/ex5-4": {"data": card}})
    assert card_images.fetch_image_by_card_id("ex5-4", "Dark Celebi", "4/101") is None  # number mismatch

    card.update(number="4", name="Pikachu")
    assert card_images.fetch_image_by_card_id("ex5-4", "Dark Celebi", "4/101") is None  # name mismatch


def test_japanese_card_is_fetched_from_tcgdex_with_its_capitalized_set_code(monkeypatch):
    calls = _routes(monkeypatch, {
        "https://api.tcgdex.net/v2/ja/cards/SV2a-168": {
            "id": "SV2a-168", "localId": "168", "set": {"id": "SV2a"},
            "image": "https://assets.tcgdex.net/ja/SV/SV2a/168",
        },
    })
    url = card_images.fetch_image_by_card_id("jpn_sv2a-168", "Charmander", "168/165")
    assert url == "https://assets.tcgdex.net/ja/SV/SV2a/168/low.webp"
    assert calls == ["https://api.tcgdex.net/v2/ja/cards/SV2a-168"]  # never the English API


def test_japanese_card_tries_a_zero_padded_number(monkeypatch):
    _routes(monkeypatch, {
        "https://api.tcgdex.net/v2/ja/cards/S12a-007": {
            "localId": "007", "set": {"id": "S12a"}, "image": "https://assets.tcgdex.net/ja/S/S12a/007",
        },
    })
    assert card_images.fetch_image_by_card_id("jpn_s12a-7", "Keldeo", "7/172").endswith("/007/low.webp")


def test_japanese_card_from_another_set_is_rejected_not_retried(monkeypatch):
    calls = _routes(monkeypatch, {
        "https://api.tcgdex.net/v2/ja/cards/SV2a-168": {"localId": "168", "set": {"id": "SV3"}, "image": "x"},
    })
    assert card_images.fetch_image_by_card_id("jpn_sv2a-168", "Charmander", "168/165") is None
    assert len(calls) == 1


def test_unknown_id_schemes_and_network_errors_give_none(monkeypatch):
    def raising_get(*a, **kw):
        raise httpx.ConnectError("down")

    monkeypatch.setattr(card_images.httpx, "get", raising_get)
    assert card_images.fetch_image_by_card_id("ex5-4", "Celebi", "4/101") is None
    assert card_images.fetch_image_by_card_id("jpn_sv2a-168", "Charmander", "168/165") is None
    assert card_images.fetch_image_by_card_id("chs_x-1", "Pikachu", "1") is None
    assert card_images.fetch_image_by_card_id(None, "Pikachu", "1") is None
