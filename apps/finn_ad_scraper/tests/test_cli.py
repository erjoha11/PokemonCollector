from types import SimpleNamespace
from unittest.mock import patch

from finn_ad_scraper.cli import main


@patch("finn_ad_scraper.cli.identify_cards", return_value=[])
@patch("finn_ad_scraper.cli.fetch_finn_ad")
@patch("builtins.input", return_value="https://www.finn.no/recommerce/forsale/item/123")
def test_prompts_for_url_when_omitted(mock_input, mock_fetch, mock_identify, capsys):
    mock_fetch.return_value = SimpleNamespace(
        title="Pokemon kort",
        url="https://www.finn.no/recommerce/forsale/item/123",
        price=None,
        currency=None,
        images=[],
        description="",
    )

    assert main([]) == 0
    mock_input.assert_called_once_with("Enter finn.no ad URL: ")
    mock_fetch.assert_called_once_with("https://www.finn.no/recommerce/forsale/item/123")
    mock_identify.assert_called_once()
    assert "Pokemon kort" in capsys.readouterr().out