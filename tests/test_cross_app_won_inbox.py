"""Cross-app contract: fb_auction_watcher's wins payload -> tcg_inventory's inbox (#309).

Apps never import each other (CLAUDE.md), and the producer is TypeScript, so
the two sides meet only in a committed fixture:

    apps/fb_auction_watcher/tests/won-inbox.test.ts
        buildWonPayload() over invented posts --(vitest file snapshot)-->
    tests/fixtures/won-inbox.v1.json
        --> tcg_inventory/won_inbox.py parse_payload() (this test)

Changing the payload on the extension side rewrites the fixture (or fails
`npm test` in CI until it's updated); if tcg_inventory can no longer read it,
this test fails. The contract itself is documented in
apps/fb_auction_watcher/docs/spec.md "Sending wins to tcg_inventory".
"""
from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
TCG_DIR = REPO_ROOT / "apps" / "tcg_inventory"
FIXTURE = REPO_ROOT / "tests" / "fixtures" / "won-inbox.v1.json"

# tcg_inventory uses flat imports from its own directory, as its tests/conftest.py sets up.
if str(TCG_DIR) not in sys.path:
    sys.path.insert(0, str(TCG_DIR))

import won_inbox  # noqa: E402


@pytest.fixture(scope="module")
def payload() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_fixture_is_the_version_tcg_inventory_reads(payload):
    assert payload["format"] == won_inbox.FORMAT
    assert payload["version"] == won_inbox.SUPPORTED_VERSION


def test_every_item_in_the_fixture_parses(payload):
    items = won_inbox.parse_payload(payload)
    assert len(items) == len(payload["items"]) > 0
    refs = {i.external_ref for i in items}
    # Both ref shapes the producer emits: a lot's comment ID, and the pos<n> fallback.
    assert any(r.split(":")[-1].startswith("pos") for r in refs)
    assert any(not r.split(":")[-1].startswith("pos") for r in refs)


def test_fields_arrive_with_their_types(payload):
    items = won_inbox.parse_payload(payload)
    assert {i.sale_type for i in items} <= won_inbox.SALE_TYPES
    assert all(isinstance(i.ended_on, dt.date) or i.ended_on is None for i in items)
    # Known and unknown prices are both covered, so a null price can't silently break.
    assert any(i.price is None for i in items)
    assert any(isinstance(i.price, float) for i in items)
    assert any(i.paid_at is not None for i in items)


def test_fixture_carries_only_the_contract_fields(payload):
    """Privacy: only your own wins, with exactly the agreed fields -- a new
    field on the producer side (say, bidders or raw text) fails here until
    the contract (spec + this list) is deliberately extended."""
    allowed = {
        "external_ref", "seller", "sale_type", "ended_on", "post_url", "lot_url", "label", "price",
        "shipping_text", "payment_text", "paid_at", "received_at",
    }
    assert set(payload) == {"format", "version", "sent_at", "items"}
    for item in payload["items"]:
        assert set(item) == allowed


def test_a_newer_major_version_is_refused(payload):
    newer = {**payload, "version": won_inbox.SUPPORTED_VERSION + 1}
    with pytest.raises(won_inbox.PayloadError, match="version"):
        won_inbox.parse_payload(newer)
