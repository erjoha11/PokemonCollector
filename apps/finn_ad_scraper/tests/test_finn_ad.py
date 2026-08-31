from pathlib import Path

import pytest

from finn_ad_scraper.finn_ad import FinnAdFetchError, fetch_finn_ad

FIXTURE = Path(__file__).parent / "fixtures" / "sample_ad.html"


def test_parses_json_ld_ad():
    html = FIXTURE.read_text()
    ad = fetch_finn_ad("https://www.finn.no/recommerce/forsale/item/999", html=html)

    assert ad.title == "Pokemon kort samling - Charizard m.fl."
    assert "Charizard" in ad.description
    assert ad.price == 1500.0
    assert ad.currency == "NOK"
    assert ad.images == [
        "https://images.finncdn.no/dynamic/1600w/sample1.jpg",
        "https://images.finncdn.no/dynamic/1600w/sample2.jpg",
    ]


def test_falls_back_to_meta_tags_when_no_json_ld():
    html = """
    <html><head>
      <meta property="og:title" content="Pokemon kort">
      <meta property="og:description" content="Noen kort til salgs">
      <meta property="og:image" content="https://images.finncdn.no/dynamic/1600w/only.jpg">
    </head><body></body></html>
    """
    ad = fetch_finn_ad("https://www.finn.no/x", html=html)
    assert ad.title == "Pokemon kort"
    assert ad.images == ["https://images.finncdn.no/dynamic/1600w/only.jpg"]


def test_raises_when_no_content_found():
    with pytest.raises(FinnAdFetchError):
        fetch_finn_ad("https://www.finn.no/x", html="<html><body>nothing here</body></html>")


def test_gallery_images_are_merged_and_deduped():
    # JSON-LD only lists a cover photo; the rendered gallery has more photos,
    # plus a smaller duplicate of the cover photo that should be dropped in
    # favor of the higher-resolution JSON-LD copy.
    html = """
    <html><head>
      <script type="application/ld+json">
      {
        "@type": "Product",
        "name": "Pokemon kort",
        "description": "Noen kort til salgs",
        "image": ["https://images.finncdn.no/dynamic/1600w/cover.jpg"],
        "offers": {"price": "500", "priceCurrency": "NOK"}
      }
      </script>
    </head><body>
      <img src="https://images.finncdn.no/dynamic/320w/cover.jpg">
      <img src="https://images.finncdn.no/dynamic/1600w/extra1.jpg">
      <img src="https://images.finncdn.no/dynamic/1600w/extra2.jpg">
      <img src="https://not-finn.example.com/other.jpg">
    </body></html>
    """
    ad = fetch_finn_ad("https://www.finn.no/x", html=html)

    assert ad.images == [
        "https://images.finncdn.no/dynamic/1600w/cover.jpg",
        "https://images.finncdn.no/dynamic/1600w/extra1.jpg",
        "https://images.finncdn.no/dynamic/1600w/extra2.jpg",
    ]
