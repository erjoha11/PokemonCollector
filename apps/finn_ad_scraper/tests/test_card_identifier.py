import json
from types import SimpleNamespace

import anthropic
import httpx2
import pytest

from finn_ad_scraper import card_identifier
from finn_ad_scraper.card_identifier import CARD_SCHEMA, CardIdentificationError, identify_cards

CHARIZARD = {
    "name": "Charizard", "set_name": "Base Set", "set_code": "base1", "printed_number": "4/102",
    "language": "int", "variant": "holo", "condition": "Lightly Played", "graded": None,
    "quantity": 1, "photos": [1], "confidence": "high", "notes": "Light edge wear",
}
MEW = {
    "name": "Mew ex", "set_name": "Pokémon Card 151", "set_code": "SV2a", "printed_number": "205/165",
    "language": "ja", "variant": "holo", "condition": "Near Mint", "graded": "PSA 10",
    "quantity": 1, "photos": [2], "confidence": "medium", "notes": "",
}


def response(cards=None, stop_reason="end_turn", stop_details=None):
    text = json.dumps({"cards": cards or []})
    return SimpleNamespace(
        stop_reason=stop_reason, stop_details=stop_details,
        content=[SimpleNamespace(type="thinking", thinking=""), SimpleNamespace(type="text", text=text)],
    )


class FakeClient:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        result = self.responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def test_request_shape_and_parsed_cards():
    client = FakeClient(response([CHARIZARD, MEW]))
    cards = identify_cards(["https://img/1.jpg", "https://img/2.jpg"], ad_context="Selger Charizard", client=client)

    call = client.calls[0]
    assert call["model"] == "claude-opus-5"
    assert call["output_config"] == {"format": {"type": "json_schema", "schema": CARD_SCHEMA}}
    assert call["fallbacks"] == "default" and call["betas"] == ["server-side-fallback-2026-07-01"]
    content = call["messages"][0]["content"]
    assert content[0] == {"type": "text", "text": "Photo 1:"}
    assert content[1] == {"type": "image", "source": {"type": "url", "url": "https://img/1.jpg"}}
    assert "Selger Charizard" in content[-1]["text"]

    charizard, mew = cards
    assert (charizard.number, charizard.dex_id) == ("4", "base1-4")
    assert charizard.master_key == ("int", "base1", "4", "holo")
    assert (mew.set_code, mew.number, mew.dex_id, mew.graded) == ("sv2a", "205", "jpn_sv2a-205", "PSA 10")


def test_no_photos_means_no_request():
    client = FakeClient()
    assert identify_cards([], client=client) == []
    assert client.calls == []


def test_refusal_raises():
    client = FakeClient(response(stop_reason="refusal", stop_details=SimpleNamespace(category="other")))
    with pytest.raises(CardIdentificationError, match="declined"):
        identify_cards(["https://img/1.jpg"], client=client)


def test_many_photos_are_batched_and_duplicates_merged(monkeypatch):
    monkeypatch.setattr(card_identifier, "MAX_PHOTOS_PER_REQUEST", 2)
    later = dict(CHARIZARD, photos=[3])
    client = FakeClient(response([CHARIZARD]), response([later]))
    cards = identify_cards(["a", "b", "c"], client=client)
    assert len(client.calls) == 2
    assert client.calls[1]["messages"][0]["content"][0]["text"] == "Photo 3:"  # numbering continues
    assert len(cards) == 1 and cards[0].quantity == 2 and cards[0].photos == [1, 3]


def test_unreachable_image_url_retries_inline(monkeypatch):
    monkeypatch.setattr(card_identifier, "_image_source", lambda url, inline: {"inline": inline})
    error = anthropic.BadRequestError(
        "Could not fetch image from URL",
        response=httpx2.Response(400, request=httpx2.Request("POST", "https://api.anthropic.com/v1/messages")),
        body=None,
    )
    client = FakeClient(error, response([CHARIZARD]))
    cards = identify_cards(["https://img/1.jpg"], client=client)
    assert len(cards) == 1
    assert client.calls[1]["messages"][0]["content"][1]["source"] == {"inline": True}
