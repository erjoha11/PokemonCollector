"""Cross-app sync: the rules finn_ad_scraper copies from tcg_inventory.

Apps never import from each other (CLAUDE.md), so finn_ad_scraper keeps
literal copies of a few tcg_inventory rules -- enough to build the same
master-card key (language, set_code, number, variant) from what's printed on
a card, and the same condition vocabulary:

    finn_ad_scraper/card_ids.py   <->  tcg_inventory/masterdata.py
    finn_ad_scraper/models.py     <->  tcg_inventory/constants.py
      CONDITIONS                         CARD_CONDITIONS

This test (repo root, not inside either app) is the only place both are
loaded side by side, and it compares them instead of hardcoding expected
values, so editing one side without the other fails here.

KNOWN_DRIFT below records divergences that existed when this test was added
(#238). They're reported, not silently fixed: deciding which side is right
is a product call. Resolving one means deleting its entry here -- the test
fails if a listed drift disappears, so the list can't go stale.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
APPS_DIR = REPO_ROOT / "apps"
TCG_DIR = APPS_DIR / "tcg_inventory"

# finn_ad_scraper is a package (imported as finn_ad_scraper.*, from apps/);
# tcg_inventory uses flat imports from its own directory, exactly as its
# tests/conftest.py sets up.
for path in (APPS_DIR, TCG_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from finn_ad_scraper import card_ids as finn_ids  # noqa: E402
from finn_ad_scraper import models as finn_models  # noqa: E402

import constants as tcg_constants  # noqa: E402
import masterdata as tcg_masterdata  # noqa: E402

# Variant aliases only finn_ad_scraper has: alias slug -> finn's target.
# tcg_inventory's normalize_variant() leaves these slugs as-is (an unknown
# variant keeps its slug), so e.g. "Reverse" keys as "reverse_holo" in finn
# but "reverse" in tcg_inventory.
KNOWN_DRIFT_FINN_ONLY_ALIASES = {
    "reverse": "reverse_holo",
    "reverseholo": "reverse_holo",
    "non_holo": "normal",
}


def test_language_to_dex_prefix_mapping_agrees():
    # tcg: {"jpn": "ja", "": "int"}  (prefix -> language, no underscore)
    # finn: {"ja": "jpn_", "int": ""} (language -> prefix incl. underscore)
    tcg_inverted = {
        language: (f"{prefix}_" if prefix else "")
        for prefix, language in tcg_masterdata.DEX_LANGUAGE_PREFIXES.items()
    }
    assert finn_ids.DEX_PREFIX_BY_LANGUAGE == tcg_inverted


def test_finn_languages_cover_every_dex_language():
    assert set(finn_ids.DEX_PREFIX_BY_LANGUAGE) <= set(finn_ids.LANGUAGES)


def test_variant_codes_agree():
    assert set(finn_ids.VARIANTS) == set(tcg_masterdata.VARIANT_LABELS)


def test_variant_aliases_agree():
    tcg_aliases = tcg_masterdata._DEX_VARIANT_ALIASES
    finn_aliases = finn_ids._VARIANT_ALIASES

    missing_in_finn = {k: v for k, v in tcg_aliases.items() if finn_aliases.get(k) != v}
    assert not missing_in_finn, f"tcg_inventory aliases missing/different in finn_ad_scraper: {missing_in_finn}"

    finn_only = {k: v for k, v in finn_aliases.items() if k not in tcg_aliases}
    assert finn_only == KNOWN_DRIFT_FINN_ONLY_ALIASES, (
        "finn_ad_scraper-only variant aliases changed. New drift: sync both apps. "
        "Drift resolved: remove it from KNOWN_DRIFT_FINN_ONLY_ALIASES."
    )
    # Every alias target is a real canonical code on both sides.
    for target in {*tcg_aliases.values(), *finn_aliases.values()}:
        assert target in tcg_masterdata.VARIANT_LABELS


def _variant_samples() -> list:
    samples = [None, "", "   ", "Holo", "HOLO", " holo ", "Reverse Holo", "reverse-holo", "Poké Ball Holo",
               "Poke Ball Holo", "Master Ball Holo", "1st Edition", "1st Edition Holo", "Shadowless",
               "Cosmos Holo", "Cracked Ice Holo", "Expansion Stamp", "Normal", "Non-Holo", "Reverse",
               "Some New Variant", "Pokémon Center Stamp", "Pokeball Holo", "Masterball Holo"]
    samples += list(tcg_masterdata.VARIANT_LABELS)  # codes
    samples += list(tcg_masterdata.VARIANT_LABELS.values())  # display labels
    samples += list(tcg_masterdata._DEX_VARIANT_ALIASES)
    samples += list(finn_ids._VARIANT_ALIASES)
    return samples


@pytest.mark.parametrize("raw", _variant_samples())
def test_normalize_variant_agrees(raw):
    tcg = tcg_masterdata.normalize_variant(raw)
    finn = finn_ids.normalize_variant(raw)
    if tcg in KNOWN_DRIFT_FINN_ONLY_ALIASES:
        # Documented drift: finn maps the slug, tcg keeps it.
        assert finn == KNOWN_DRIFT_FINN_ONLY_ALIASES[tcg]
    else:
        assert finn == tcg


def test_conditions_agree():
    assert tuple(finn_models.CONDITIONS) == tuple(tcg_constants.CARD_CONDITIONS)
