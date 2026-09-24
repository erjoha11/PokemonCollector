import ads


def _item(**overrides):
    defaults = dict(
        card_id=1,
        name="Pikachu",
        set="Base Set",
        number="58",
        variant=None,
        language="ENG",
        condition="Near Mint",
        qty=1,
        price=100.0,
    )
    defaults.update(overrides)
    return ads.SaleItem(**defaults)


def test_build_listing_requires_at_least_one_item():
    try:
        ads.build_listing([])
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_single_card_title_and_description():
    draft = ads.build_listing([_item()])
    assert draft.card_count == 1
    assert "Pikachu" in draft.title
    assert "Base Set" in draft.title
    assert not draft.truncated_title
    assert "Pikachu" in draft.description
    assert "Near Mint" in draft.description
    assert "100" in draft.description
    assert draft.suggested_price == 100.0


def test_lot_title_uses_count_and_common_set():
    draft = ads.build_listing([_item(card_id=1), _item(card_id=2, name="Charmander")])
    assert "2 stk" in draft.title
    assert "Base Set" in draft.title
    assert draft.suggested_price == 200.0


def test_lot_with_mixed_sets_falls_back_to_generic_title():
    draft = ads.build_listing(
        [_item(card_id=1, set="Base Set"), _item(card_id=2, set="Jungle", name="Charmander")]
    )
    assert "diverse sett" in draft.title
    # Groups by set in the body when more than one set is present.
    assert "Base Set:" in draft.description
    assert "Jungle:" in draft.description


def test_missing_price_is_flagged_not_silently_zero():
    draft = ads.build_listing([_item(price=None)])
    assert draft.suggested_price is None
    assert "ikke satt" in draft.description
    assert "kr 0" not in draft.description.lower()


def test_long_title_is_truncated_with_ellipsis():
    long_name = "A" * 100
    draft = ads.build_listing([_item(name=long_name)])
    assert draft.truncated_title
    assert len(draft.title) <= ads.MAX_TITLE_LENGTH
    assert draft.title.endswith("…")


def test_qty_greater_than_one_is_reflected_in_description():
    draft = ads.build_listing([_item(qty=3, price=10.0)])
    assert "3 stk" in draft.description
    assert draft.suggested_price == 30.0


def test_card_label_shows_short_language_codes_and_skips_english():
    from ads import SaleItem, _card_label

    def label(language):
        return _card_label(SaleItem(card_id=1, name="Mew", set="151", number="151", variant=None,
                                    language=language, condition=None, qty=1, price=None))

    assert label("International") == "Mew 151 #151"
    assert label("ENG") == "Mew 151 #151"
    assert label("Japanese") == "Mew 151 #151 (JP)"
    assert label("Simplified Chinese") == "Mew 151 #151 (CN)"


def test_language_code():
    import constants

    assert constants.language_code("International") == "EN"
    assert constants.language_code("Korean") == "KR"
    assert constants.language_code("Klingon") == "Klingon"
    assert constants.language_code(None) == ""
