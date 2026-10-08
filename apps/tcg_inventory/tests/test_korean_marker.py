"""Korean marker (issue #367): a "KR" token in Dex's card notes makes
Card.language Korean at import; jpn_sv2a is Korean via a temporary set-level
fallback; notes follow Dex (cleared when removed there); identity and prices
never change; a Korean card priced from its Japanese print is labelled
"(JP price)"."""
import datetime as dt

import pytest
from conftest import make_csv, seed_import

import ads
import constants
import pricing
from importer import import_dex_csv_files
from models import Card, CardPrice, MasterCard, MasterCardId


# --------------------------------------------------------------------------
# The rule (constants.py)
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "notes",
    ["KR", "kr", "Kr", "KR; mint", "bought KR lot", "mint; KR", "(KR)", "KR-lot", "x; kr; y"],
)
def test_kr_token_matches(notes):
    assert constants.has_korean_note(notes)


@pytest.mark.parametrize(
    "notes",
    [None, "", "KRAKEN", "okr", "Kraken lot", "OKR", "KRW 500", "kr500", "NKR", "KR_lot", "K R"],
)
def test_kr_token_false_positives(notes):
    assert not constants.has_korean_note(notes)


def test_physical_language_rule():
    pl = constants.physical_language
    # A KR note wins in any set, Japanese or not.
    assert pl("jpn_s12a-1", "Japanese", "KR") == "Korean"
    assert pl("swsh3-1", "International", "kr") == "Korean"
    # The temporary sv2a fallback, note or not.
    assert pl("jpn_sv2a-12", "Japanese", None) == "Korean"
    assert pl("jpn_sv2a-12", "Japanese", "mint") == "Korean"
    # Everything else: Dex's Locale.
    assert pl("jpn_s12a-1", "Japanese", None) == "Japanese"
    assert pl("jpn_s12a-1", "Japanese", "KRAKEN") == "Japanese"
    assert pl("jpn_sv2", " Japanese ", None) == "Japanese"  # prefix match is exact, not startswith
    assert pl("jpn_s12a-1", "", None) is None


def test_fallback_is_one_documented_entry():
    assert constants.PHYSICAL_LOCALE_OVERRIDES == {"jpn_sv2a": "Korean"}


# --------------------------------------------------------------------------
# Import
# --------------------------------------------------------------------------
def _import(db, rows):
    import_dex_csv_files(db, [("main.csv", make_csv("My Collection", rows))])
    db.flush()


def _card(db, card_id):
    return db.query(Card).filter(Card.card_id == card_id).one()


def test_kr_note_makes_card_korean_and_removal_reverts(db_session):
    row = {"id": "jpn_s12a-10", "locale": "Japanese", "notes": "bought KR lot"}
    _import(db_session, [row])
    card = _card(db_session, "jpn_s12a-10")
    assert card.language == "Korean"
    assert card.notes == "bought KR lot"

    # Removed in Dex: notes cleared, language back to Dex's Locale.
    _import(db_session, [{**row, "notes": []}])
    assert card.notes is None
    assert card.language == "Japanese"


def test_kr_note_in_a_later_note_column(db_session):
    _import(db_session, [{"id": "jpn_s12a-11", "locale": "Japanese", "notes": ["mint", "", "kr"]}])
    card = _card(db_session, "jpn_s12a-11")
    assert card.language == "Korean"
    assert card.notes == "mint; kr"


def test_notes_follow_dex_when_cleared(db_session):
    _import(db_session, [{"id": "swsh3-1", "locale": "International", "notes": "signed"}])
    card = _card(db_session, "swsh3-1")
    assert card.notes == "signed"
    _import(db_session, [{"id": "swsh3-1", "locale": "International", "notes": "signed; PSA"}])
    assert card.notes == "signed; PSA"
    _import(db_session, [{"id": "swsh3-1", "locale": "International"}])
    assert card.notes is None
    assert card.language == "International"


def test_new_card_without_notes_has_none(db_session):
    _import(db_session, [{"id": "swsh3-2", "locale": "International"}])
    assert _card(db_session, "swsh3-2").notes is None


def test_sv2a_fallback_without_note_and_other_jpn_set_unaffected(db_session):
    _import(
        db_session,
        [
            {"id": "jpn_sv2a-25", "locale": "Japanese", "number": "25/165"},
            {"id": "jpn_sv2a-26", "locale": "Japanese", "number": "26/165", "notes": "KR"},
            {"id": "jpn_s12a-25", "locale": "Japanese", "number": "25/172"},
            {"id": "jpn_s12a-26", "locale": "Japanese", "number": "26/172", "notes": "KRAKEN"},
        ],
    )
    assert _card(db_session, "jpn_sv2a-25").language == "Korean"
    assert _card(db_session, "jpn_sv2a-26").language == "Korean"
    assert _card(db_session, "jpn_s12a-25").language == "Japanese"
    assert _card(db_session, "jpn_s12a-26").language == "Japanese"


def test_identity_and_prices_unchanged_by_kr_note(db_session):
    rows = [
        {"id": "jpn_s12a-30", "locale": "Japanese", "number": "30/172", "price": "kr 55,00"},
        {"id": "jpn_sv2a-31", "locale": "Japanese", "number": "31/165", "price": "kr 12,00"},
    ]
    _import(db_session, rows)

    def snapshot():
        out = {}
        for card in db_session.query(Card).order_by(Card.card_id).all():
            master = db_session.get(MasterCard, card.master_card_id)
            ext = sorted(
                (m.source, m.external_id)
                for m in db_session.query(MasterCardId).filter(MasterCardId.master_card_id == master.id)
            )
            prices = sorted(
                (p.source, p.price, p.price_nok)
                for p in db_session.query(CardPrice).filter(CardPrice.card_id == card.id)
            )
            out[card.card_id] = (
                card.id,
                (master.id, master.language, master.set_code, master.number, master.variant),
                ext,
                prices,
                card.market_price,
                card.market_price_source,
                card.price_flags,
                card.image_url,
            )
        return out

    before = snapshot()
    _import(db_session, [{**r, "notes": "KR"} for r in rows])
    assert _card(db_session, "jpn_s12a-30").language == "Korean"
    assert snapshot() == before
    # Re-resolving from card_prices also picks the same price.
    pricing.resolve_cards(db_session, today=dt.date.today())
    db_session.flush()
    assert snapshot() == before
    assert before["jpn_s12a-30"][1][1] == "ja"


# --------------------------------------------------------------------------
# "(JP price)" label
# --------------------------------------------------------------------------
def test_card_source_label_jp_price():
    kr = Card(card_id="jpn_sv2a-1", language="Korean")
    jp = Card(card_id="jpn_sv2a-1", language="Japanese")
    kr_intl = Card(card_id="swsh3-1", language="Korean")
    assert pricing.card_source_label("tcgdex_cardmarket", kr) == "Cardmarket via TCGdex (JP price)"
    assert pricing.card_source_label("dex", kr) == "TCGplayer via Dex (JP price)"
    assert pricing.card_source_label("tcgdex_cardmarket", jp) == "Cardmarket via TCGdex"
    assert pricing.card_source_label("dex", kr_intl) == "TCGplayer via Dex"
    assert pricing.card_source_label(None, kr) == "–"
    # Linked master card decides when present.
    kr.master_card = MasterCard(language="ja", set_code="sv2a", number="1", variant="normal")
    assert pricing.is_jp_price_proxy(kr)
    kr_intl.master_card = MasterCard(language="int", set_code="swsh3", number="1", variant="normal")
    assert not pricing.is_jp_price_proxy(kr_intl)


def test_card_page_shows_jp_price_label(client):
    csv = make_csv(
        "My Collection",
        [
            {"id": "jpn_s12a-40", "locale": "Japanese", "name": "Lugia", "notes": "KR", "price": "kr 100,00"},
            {"id": "jpn_s12a-41", "locale": "Japanese", "name": "Hooh", "price": "kr 100,00"},
        ],
    )
    seed_import(client, [("files", ("main.csv", csv, "text/csv"))])
    import db as db_module

    db = db_module.SessionLocal()
    kr_id = db.query(Card).filter(Card.card_id == "jpn_s12a-40").one().id
    jp_id = db.query(Card).filter(Card.card_id == "jpn_s12a-41").one().id
    db.close()

    html = client.get(f"/cards/{kr_id}").text
    assert 'card-price-source-label">TCGplayer via Dex (JP price)<' in html
    assert "<td>TCGplayer via Dex (JP price)</td>" in html
    assert "<dt>Language</dt><dd>KR</dd>" in html
    assert "<dt>Notes</dt><dd>KR</dd>" in html

    html = client.get(f"/cards/{jp_id}").text
    assert 'card-price-source-label">TCGplayer via Dex<' in html
    assert "(JP price)" not in html

    inventory = client.get("/inventory").text
    assert 'value="Korean"' in inventory  # the language filter lists Korean
    assert ">KR<" in inventory


# --------------------------------------------------------------------------
# finn.no ad text
# --------------------------------------------------------------------------
def test_ad_text_says_kr_for_a_korean_card():
    item = ads.SaleItem(
        card_id=1, name="Mew ex", set="151", number="151", variant=None,
        language="Korean", condition="Near Mint", qty=1, price=100.0,
    )
    draft = ads.build_listing([item])
    assert "(KR)" in draft.title or "(KR)" in draft.description
    assert "Mew ex 151 #151 (KR)" in draft.description
