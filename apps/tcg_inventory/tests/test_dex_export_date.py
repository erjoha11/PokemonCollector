"""Issue #351: Dex prices are dated at the export, not at the sync that
re-read it, and a category in several files is read from the newest one."""
import datetime as dt

import dropbox_client
import pricing
from conftest import make_csv
from importer import import_dex_csv_files
from models import Card, CardPrice
from test_dropbox_client import FakeDropbox, FakeListFolderResult, _file_entry

EXPORTED = dt.date(2026, 9, 1)


def _dex_row(db, card_id="a"):
    return (
        db.query(CardPrice)
        .join(Card, Card.id == CardPrice.card_id)
        .filter(Card.card_id == card_id, CardPrice.source == pricing.SOURCE_DEX)
        .one()
    )


def _card(db, card_id="a"):
    db.expire_all()
    return db.query(Card).filter(Card.card_id == card_id).one()


def test_same_export_synced_a_month_apart_goes_stale_and_a_newer_one_refreshes_it(db_session):
    csv = make_csv("My Collection", [{"id": "a", "name": "Pikachu", "price": "150"}])

    import_dex_csv_files(db_session, [("main.csv", csv)], today=EXPORTED, file_dates={"main.csv": EXPORTED})
    card = _card(db_session)
    assert _dex_row(db_session).fetched_at == EXPORTED
    assert (card.market_price, card.market_price_source, card.price_flags) == (150.0, pricing.SOURCE_DEX, None)

    # A live source priced it in between, fresh on the second sync's day.
    month_later = EXPORTED + dt.timedelta(days=30)
    pricing.record_price(card, pricing.SOURCE_POKEMONTCG, price_nok=99.0, fetched_at=month_later - dt.timedelta(days=2))
    db_session.commit()

    # The cron re-reads the same, unchanged export a month later.
    import_dex_csv_files(db_session, [("main.csv", csv)], today=month_later, file_dates={"main.csv": EXPORTED})
    card = _card(db_session)
    assert _dex_row(db_session).fetched_at == EXPORTED  # not re-stamped as fresh
    assert not pricing.is_fresh(EXPORTED, month_later)
    assert (card.market_price, card.market_price_source) == (99.0, pricing.SOURCE_POKEMONTCG)

    # A new export from Dex makes the dex price fresh again. The live
    # TCGplayer price still ranks ahead of it while fresh (#386) ...
    exported_again = month_later - dt.timedelta(days=1)
    newer = make_csv("My Collection", [{"id": "a", "name": "Pikachu", "price": "160"}])
    import_dex_csv_files(db_session, [("main.csv", newer)], today=month_later, file_dates={"main.csv": exported_again})
    card = _card(db_session)
    assert _dex_row(db_session).fetched_at == exported_again
    assert (card.market_price, card.market_price_source) == (99.0, pricing.SOURCE_POKEMONTCG)

    # ... and once that one goes stale, the fresh export wins.
    later = exported_again + dt.timedelta(days=pricing.FRESH_DAYS)
    assert not pricing.is_fresh(month_later - dt.timedelta(days=2), later)
    pricing.resolve_cards(db_session, today=later)
    db_session.commit()
    card = _card(db_session)
    assert (card.market_price, card.market_price_source, card.market_price_as_of) == (
        160.0,
        pricing.SOURCE_DEX,
        exported_again,
    )


def test_stale_export_with_no_fresh_live_source_keeps_its_price_flagged_stale(db_session):
    csv = make_csv("My Collection", [{"id": "a", "name": "Pikachu", "price": "150"}])
    today = EXPORTED + dt.timedelta(days=pricing.FRESH_DAYS + 1)
    import_dex_csv_files(db_session, [("main.csv", csv)], today=today, file_dates={"main.csv": EXPORTED})
    card = _card(db_session)
    assert (card.market_price, card.market_price_source) == (150.0, pricing.SOURCE_DEX)
    assert pricing.FLAG_STALE in pricing.flag_list(card.price_flags)


def test_no_file_date_keeps_today(db_session):
    today = dt.date(2026, 10, 7)
    csv = make_csv("My Collection", [{"id": "a", "price": "150"}])
    import_dex_csv_files(db_session, [("main.csv", csv)], today=today)
    assert _dex_row(db_session).fetched_at == today


def test_a_file_date_after_today_is_capped_at_today(db_session):
    today = dt.date(2026, 10, 7)
    csv = make_csv("My Collection", [{"id": "a", "price": "150"}])
    import_dex_csv_files(
        db_session, [("main.csv", csv)], today=today, file_dates={"main.csv": today + dt.timedelta(days=1)}
    )
    assert _dex_row(db_session).fetched_at == today


def test_a_category_in_two_dated_files_is_read_from_the_newest_only(db_session):
    # Listed oldest first on purpose: before #351 the last file read won.
    old = make_csv("My Collection", [{"id": "a", "name": "Pikachu", "qty": 1, "price": "100"}])
    new = make_csv("My Collection", [{"id": "a", "name": "Pikachu", "qty": 3, "price": "200"}])
    newer_date = EXPORTED + dt.timedelta(days=5)
    result = import_dex_csv_files(
        db_session,
        [("new.csv", new), ("old.csv", old)],
        today=newer_date,
        file_dates={"new.csv": newer_date, "old.csv": EXPORTED},
    )
    card = _card(db_session)
    assert (card.qty, card.market_price) == (3, 200.0)
    assert _dex_row(db_session).fetched_at == newer_date
    assert any("My Collection" in w and "old.csv" in w and "new.csv" in w for w in result.warnings)


def test_a_category_in_two_undated_files_is_merged_as_before(db_session):
    first = make_csv("My Collection", [{"id": "a", "name": "Pikachu"}])
    second = make_csv("My Collection", [{"id": "b", "name": "Eevee"}])
    result = import_dex_csv_files(db_session, [("one.csv", first), ("two.csv", second)])
    assert {c.card_id for c in db_session.query(Card)} == {"a", "b"}
    assert not any("more than one file" in w for w in result.warnings)


def test_cron_sync_dates_dex_prices_at_the_files_client_modified(client, monkeypatch):
    monkeypatch.setenv("CRON_SECRET", "s3cr3t")
    csv_bytes = make_csv("My Collection", [{"id": "a", "name": "Pikachu", "qty": 2, "price": "150"}])
    exported_at = dt.datetime(2026, 9, 20, 22, 30)
    fake = FakeDropbox(
        pages=[FakeListFolderResult([_file_entry("main.csv", exported_at)])],
        download_bytes={"/exports/main.csv": csv_bytes},
    )
    monkeypatch.setattr(dropbox_client, "build_client_from_env", lambda: fake)

    response = client.get("/cron/dropbox-sync", headers={"Authorization": "Bearer s3cr3t"})
    assert response.status_code == 200

    import db as db_module

    with db_module.SessionLocal() as db:
        assert _dex_row(db).fetched_at == exported_at.date()


def test_dropbox_file_export_date_is_client_modified_as_a_date():
    f = dropbox_client.DropboxFile(
        name="a.csv", path_lower="/a.csv", client_modified=dt.datetime(2026, 9, 20, 23, 59).isoformat(), size=1
    )
    assert f.export_date == dt.date(2026, 9, 20)
