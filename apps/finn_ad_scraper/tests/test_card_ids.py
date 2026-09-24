import pytest

from finn_ad_scraper.card_ids import dex_id, normalize_number, normalize_variant


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("4/102", ("4", "4/102")),
        ("004/165", ("4", "004/165")),
        ("TG05/TG30", ("TG05", "TG05/TG30")),
        ("SWSH050", ("SWSH050", "SWSH050")),
        ("", (None, None)),
        (None, (None, None)),
    ],
)
def test_normalize_number(raw, expected):
    assert normalize_number(raw) == expected


@pytest.mark.parametrize(
    "raw, expected",
    [("Reverse Holo", "reverse_holo"), ("Poké Ball Holo", "poke_ball_holo"), ("1st Edition", "first_edition"), (None, "unspecified")],
)
def test_normalize_variant_matches_masterdata_codes(raw, expected):
    assert normalize_variant(raw) == expected


def test_dex_id():
    assert dex_id("int", "sv2", "109") == "sv2-109"
    assert dex_id("ja", "SV2a", "168") == "jpn_sv2a-168"
    assert dex_id("zh-hans", "csv9", "79") == "scn_csv9-79"
    assert dex_id("ko", "sv2a", "1") is None  # no known Dex prefix
    assert dex_id("int", None, "4") is None
