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
                        "images": {"small": "https://example.com/a.png"},
                        "tcgplayer": {"prices": {"holofoil": {"market": 12.5}}},
                    }
                ]
            }
        ),
    )

    result = card_images.fetch_card_data("Pikachu", "Base Set", "58/102")

    assert result.image_url == "https://example.com/a.png"
    assert result.tcgplayer_price == 12.5


def test_fetch_card_data_price_is_none_when_no_tcgplayer_data(monkeypatch):
    monkeypatch.setattr(
        card_images.httpx,
        "get",
        lambda *a, **kw: _FakeResponse({"data": [{"images": {"small": "https://example.com/a.png"}}]}),
    )

    result = card_images.fetch_card_data("Pikachu", "Base Set", "58/102")

    assert result.image_url == "https://example.com/a.png"
    assert result.tcgplayer_price is None


def test_fetch_card_data_returns_nones_on_no_match(monkeypatch):
    monkeypatch.setattr(card_images.httpx, "get", lambda *a, **kw: _FakeResponse({"data": []}))

    result = card_images.fetch_card_data("Not A Real Card", None, None)

    assert result.image_url is None
    assert result.tcgplayer_price is None
