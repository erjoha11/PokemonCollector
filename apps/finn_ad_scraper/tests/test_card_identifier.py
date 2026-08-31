import json
from types import SimpleNamespace
from unittest.mock import patch

from finn_ad_scraper.card_identifier import identify_cards


class FakeMessages:
    def __init__(self, response_text):
        self.response_text = response_text
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(content=[SimpleNamespace(text=self.response_text)])


class FakeClient:
    def __init__(self, response_text):
        self.messages = FakeMessages(response_text)


SAMPLE_RESPONSE = json.dumps(
    [
        {
            "name": "Charizard",
            "set_name": "Base Set",
            "card_number": "4/102",
            "is_holo": True,
            "condition": "Lightly Played",
            "quantity": 1,
            "confidence": "high",
            "notes": "Slight whitening on edges",
        }
    ]
)


@patch("finn_ad_scraper.card_identifier._download_image_as_base64", return_value=("image/jpeg", "ZmFrZQ=="))
def test_identify_cards_parses_response(mock_download):
    client = FakeClient(SAMPLE_RESPONSE)
    cards = identify_cards(
        ["https://images.finncdn.no/dynamic/1600w/sample1.jpg"],
        ad_context="Selger Charizard holo",
        client=client,
    )

    assert len(cards) == 1
    card = cards[0]
    assert card.name == "Charizard"
    assert card.set_name == "Base Set"
    assert card.is_holo is True
    assert card.condition == "Lightly Played"
    assert client.messages.calls[0]["model"]


@patch("finn_ad_scraper.card_identifier._download_image_as_base64", return_value=("image/jpeg", "ZmFrZQ=="))
def test_identify_cards_dedupes_across_batches(mock_download):
    client = FakeClient(SAMPLE_RESPONSE)
    urls = [f"https://images.finncdn.no/dynamic/1600w/img{i}.jpg" for i in range(7)]
    cards = identify_cards(urls, client=client)

    # 7 images at MAX_IMAGES_PER_REQUEST=5 -> 2 batches, each "sees" the same
    # Charizard -> should be deduped into a single entry with combined quantity.
    assert len(cards) == 1
    assert cards[0].quantity == 2


def test_identify_cards_returns_empty_for_no_images():
    assert identify_cards([]) == []
