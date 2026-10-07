"""Which TCGplayer print a Dex variant gets (issue #350):
card_images._match_variant_key / _choose_tcgplayer_price, used by both the
pokemontcg source and tcgdex_prices.choose_tcgplayer.

The key sets below are real: every distinct `tcgplayer.prices` key set on
pokemontcg.io for sv2 (Paldea Evolved), sv8pt5 (Prismatic Evolutions) and
neo1 (Neo Genesis, WotC), fetched 2026-10-07, in the API's own key order
(which varies card to card). TCGdex spells the same keys with hyphens
("reverse-holofoil", "1st-edition-holofoil").
"""
import pytest

import card_images
import masterdata
import tcgdex_prices

# (set, keys) -- the API's own order.
SV2_KEY_SETS = [
    ["normal", "reverseHolofoil"],
    ["holofoil"],
    ["reverseHolofoil", "normal"],
    ["reverseHolofoil", "holofoil"],
    ["holofoil", "reverseHolofoil"],
]
SV8PT5_KEY_SETS = [
    ["holofoil"],
    ["reverseHolofoil", "normal"],
    ["normal", "reverseHolofoil"],
    ["holofoil", "reverseHolofoil"],
    ["reverseHolofoil", "holofoil"],
]
NEO1_KEY_SETS = [
    ["1stEdition", "unlimited"],
    ["1stEditionHolofoil", "unlimitedHolofoil"],
]
TCGDEX_KEY_SETS = [
    ["normal", "reverse-holofoil"],
    ["holofoil", "reverse-holofoil"],
    ["1st-edition", "unlimited"],
    ["1st-edition-holofoil", "unlimited-holofoil"],
]


def _match(variant, keys):
    return card_images._match_variant_key(variant, keys)


# --- each code against real key sets ---------------------------------------


@pytest.mark.parametrize("keys", SV2_KEY_SETS + SV8PT5_KEY_SETS)
def test_normal_is_the_normal_key_only(keys):
    assert _match("Normal", keys) == ("normal" if "normal" in keys else None)


@pytest.mark.parametrize("keys", SV2_KEY_SETS + SV8PT5_KEY_SETS)
def test_holo_is_the_holofoil_key_only(keys):
    # The sv2 {holofoil, reverseHolofoil} cards used to fall back to the
    # first key, right only by dict order.
    assert _match("Holo", keys) == ("holofoil" if "holofoil" in keys else None)


@pytest.mark.parametrize("keys", SV2_KEY_SETS + SV8PT5_KEY_SETS)
def test_reverse_holo_is_the_reverse_holofoil_key_only(keys):
    assert _match("Reverse Holo", keys) == ("reverseHolofoil" if "reverseHolofoil" in keys else None)


def test_wotc_first_edition_and_unlimited():
    plain, holo = NEO1_KEY_SETS
    assert _match("1st Edition", plain) == "1stEdition"
    assert _match("Normal", plain) == "unlimited"
    assert _match("1st Edition Holo", holo) == "1stEditionHolofoil"
    # Dex "1st Edition" on a holo-only card: its one 1st Edition key.
    assert _match("1st Edition", holo) == "1stEditionHolofoil"


def test_wotc_plain_holo_stays_unmatched():
    _plain, holo = NEO1_KEY_SETS
    assert _match("Holo", holo) is None
    assert _match("1st Edition Holo", NEO1_KEY_SETS[0]) is None


def test_tcgdex_hyphenated_keys_match_and_come_back_as_given():
    assert _match("Reverse Holo", TCGDEX_KEY_SETS[0]) == "reverse-holofoil"
    assert _match("Holo", TCGDEX_KEY_SETS[1]) == "holofoil"
    assert _match("1st Edition", TCGDEX_KEY_SETS[2]) == "1st-edition"
    assert _match("Normal", TCGDEX_KEY_SETS[2]) == "unlimited"
    assert _match("1st Edition Holo", TCGDEX_KEY_SETS[3]) == "1st-edition-holofoil"


def test_dex_spellings_go_through_masterdata_codes():
    keys = ["normal", "reverseHolofoil"]
    assert _match("  reverse holo ", keys) == "reverseHolofoil"
    assert _match("REVERSE-HOLO", keys) == "reverseHolofoil"
    # Substrings no longer count: "Poké Ball Holo" isn't "Holo".
    assert _match("Poké Ball Holo", ["holofoil"]) is None


# --- prints TCGplayer has no key for ---------------------------------------

BALL_VARIANTS = ["Poké Ball Holo", "Pokeball Holo", "Master Ball Holo", "Masterball Holo", "Friend Ball Holo"]
OTHER_SPECIALS = ["Cosmos Holo", "Cracked Ice Holo", "Expansion Stamp", "Shadowless"]


@pytest.mark.parametrize("variant", BALL_VARIANTS + OTHER_SPECIALS)
def test_special_prints_have_no_tcgplayer_print(variant):
    assert card_images.has_tcgplayer_print(variant) is False


@pytest.mark.parametrize("variant", [None, "", "Normal", "Holo", "Reverse Holo", "1st Edition", "1st Edition Holo"])
def test_tcgplayer_keyed_prints_and_blank(variant):
    assert card_images.has_tcgplayer_print(variant) is True


def test_every_masterdata_variant_code_is_decided():
    # A new code in masterdata.VARIANT_LABELS defaults to "no TCGplayer
    # price"; this pins the current split so adding one is a conscious choice.
    priceable = {code for code in masterdata.VARIANT_LABELS if card_images.has_tcgplayer_print(code)}
    assert priceable == {"unspecified", "normal", "holo", "reverse_holo", "first_edition", "first_edition_holo"}


def _tcgplayer(keys):
    return {"prices": {key: {"market": 1.0 + i} for i, key in enumerate(keys)}}


@pytest.mark.parametrize("keys", SV8PT5_KEY_SETS)
@pytest.mark.parametrize("variant", ["Poké Ball Holo", "Master Ball Holo"])
def test_ball_pattern_is_never_priced_from_another_print(keys, variant):
    # Includes the 80 sv8pt5 cards whose only key is `holofoil`: the
    # single-print shortcut doesn't apply to a print TCGplayer lacks.
    assert card_images._choose_tcgplayer_price(_tcgplayer(keys), variant) is None


@pytest.mark.parametrize("keys", SV8PT5_KEY_SETS)
@pytest.mark.parametrize("variant", ["Poké Ball Holo", "Master Ball Holo"])
def test_tcgdex_tcgplayer_ball_pattern_is_none(keys, variant):
    tcgdex_keys = [key.replace("reverseHolofoil", "reverse-holofoil") for key in keys]
    payload = {"pricing": {"tcgplayer": {k: {"marketPrice": 1.0 + i} for i, k in enumerate(tcgdex_keys)}}}
    assert tcgdex_prices.choose_tcgplayer(payload, variant) is None


def test_tcgdex_tcgplayer_holo_picks_holofoil_whatever_the_order():
    payload = {"pricing": {"tcgplayer": {"reverse-holofoil": {"marketPrice": 3.0}, "holofoil": {"marketPrice": 9.0}}}}
    choice = tcgdex_prices.choose_tcgplayer(payload, "Holo")
    assert (choice.price, choice.variant_key, choice.uncertain) == (9.0, "holofoil", False)


def test_choose_keeps_the_flagged_first_key_fallback_for_a_blank_variant():
    choice = card_images._choose_tcgplayer_price(_tcgplayer(["reverseHolofoil", "normal"]), None)
    assert (choice.key, choice.uncertain) == ("reverseHolofoil", True)
