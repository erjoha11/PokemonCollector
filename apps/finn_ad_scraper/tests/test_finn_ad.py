from pathlib import Path

import pytest
import requests

from finn_ad_scraper import finn_ad
from finn_ad_scraper.finn_ad import FinnAdFetchError, fetch_finn_ad, fetch_html, parse_ad

FIXTURE = Path(__file__).parent / "fixtures" / "sample_ad.html"
URL = "https://www.finn.no/recommerce/forsale/item/999"


def test_parses_json_ld_product():
    ad = parse_ad(FIXTURE.read_text(), URL)
    assert ad.title == "Pokemon kort samling - Charizard m.fl."
    assert "Charizard" in ad.description
    assert (ad.price, ad.currency) == (1500.0, "NOK")
    # og:image repeats sample1 -- merged in, not duplicated
    assert ad.images == [
        "https://images.finncdn.no/dynamic/1600w/sample1.jpg",
        "https://images.finncdn.no/dynamic/1600w/sample2.jpg",
    ]


def test_json_ld_image_objects_graph_and_price_formats():
    html = """<script type="application/ld+json">
    {"@graph": [{"@type": "WebPage", "name": "FINN"},
                {"@type": ["Product"], "name": "Mew ex 151",
                 "image": [{"@type": "ImageObject", "contentUrl": "https://img/a.jpg"}, {"url": "https://img/b.jpg"}],
                 "offers": [{"price": "1 250,50", "priceCurrency": "NOK"}]}]}
    </script>"""
    ad = parse_ad(html, URL)
    assert ad.title == "Mew ex 151"
    assert ad.images == ["https://img/a.jpg", "https://img/b.jpg"]
    assert ad.price == 1250.5


def test_falls_back_to_meta_tags_and_price_in_text():
    html = """<html><head>
      <meta property="og:title" content="Pokemon kort">
      <meta property="og:description" content="Noen kort til salgs">
      <meta property="og:image" content="https://img/one.jpg">
      <meta property="og:image" content="https://img/two.jpg">
    </head><body>Pris 2 500 kr</body></html>"""
    ad = parse_ad(html, URL)
    assert ad.title == "Pokemon kort"
    assert ad.images == ["https://img/one.jpg", "https://img/two.jpg"]
    assert (ad.price, ad.currency) == (2500.0, "NOK")


def test_raises_when_page_has_no_ad():
    with pytest.raises(FinnAdFetchError):
        parse_ad("<html><body>nothing here</body></html>", URL)


class FakeResponse:
    def __init__(self, text, status=200):
        self.text, self.status = text, status

    def raise_for_status(self):
        if self.status >= 400:
            raise requests.HTTPError(str(self.status))


class FakeSession:
    def __init__(self, response):
        self.response = response

    def get(self, *args, **kwargs):
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def test_plain_http_is_enough_when_structured_data_is_present(monkeypatch):
    monkeypatch.setattr(finn_ad, "_fetch_html_via_browser", lambda url: pytest.fail("browser not needed"))
    html = FIXTURE.read_text()
    assert fetch_html(URL, session=FakeSession(FakeResponse(html))) == html


def test_browser_fallback_when_http_lacks_structured_data(monkeypatch):
    monkeypatch.setattr(finn_ad, "_fetch_html_via_browser", lambda url: "<rendered>")
    assert fetch_html(URL, session=FakeSession(FakeResponse("<html>shell</html>"))) == "<rendered>"


def test_no_browser_and_http_failure_raises():
    with pytest.raises(FinnAdFetchError):
        fetch_html(URL, use_browser=False, session=FakeSession(requests.ConnectionError("down")))


def test_fetch_finn_ad_with_given_html_skips_network():
    assert fetch_finn_ad(URL, html=FIXTURE.read_text()).price == 1500.0
