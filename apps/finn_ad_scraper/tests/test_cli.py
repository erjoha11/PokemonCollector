import json
from unittest.mock import patch

from finn_ad_scraper.cli import main
from finn_ad_scraper.models import AdAnalysis, FinnAd, IdentifiedCard

RESULT = AdAnalysis(
    ad=FinnAd(url="https://www.finn.no/recommerce/forsale/item/123", title="Pokemon kort", price=1000, currency="NOK",
              images=["https://img/1.jpg"]),
    cards=[IdentifiedCard(name="Charizard", set_name="Base Set", set_code="base1", number="4", printed_number="4/102",
                          language="int", variant="holo", condition="Near Mint", quantity=2, confidence="high",
                          dex_id="base1-4")],
    model="claude-opus-5",
)


@patch("finn_ad_scraper.cli.analyze_ad", return_value=RESULT)
@patch("builtins.input", return_value="https://www.finn.no/recommerce/forsale/item/123")
def test_prompts_for_url_and_prints_summary(mock_input, mock_analyze, capsys):
    assert main([]) == 0
    mock_analyze.assert_called_once()
    out = capsys.readouterr().out
    assert "Pokemon kort" in out and "2x Charizard (Base Set, #4/102)" in out
    assert "[base1-4]" in out and "Price per card: 500 NOK" in out


@patch("finn_ad_scraper.cli.analyze_ad", return_value=RESULT)
def test_json_output_includes_master_key(mock_analyze, capsys):
    assert main(["https://www.finn.no/x", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["cards"][0]["master_key"] == ["int", "base1", "4", "holo"]


@patch("finn_ad_scraper.cli.analyze_ad", side_effect=RuntimeError("boom"))
def test_errors_are_one_line(mock_analyze, capsys):
    assert main(["https://www.finn.no/x"]) == 1
    assert "boom" in capsys.readouterr().err
