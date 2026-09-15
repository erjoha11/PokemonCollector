import datetime as dt

from models import Card, CardSnapshot
import snapshots


def _add_card(db, **kwargs):
    defaults = dict(card_id="a", name="Pikachu", qty=2, reference_price=100.0)
    defaults.update(kwargs)
    card = Card(**defaults)
    db.add(card)
    db.commit()
    db.refresh(card)
    return card


def test_record_daily_snapshot_writes_one_row_per_card(db_session):
    card_a = _add_card(db_session, card_id="a", name="Pikachu", qty=2, reference_price=100.0)
    card_b = _add_card(db_session, card_id="b", name="Charmander", qty=1, reference_price=None)

    count = snapshots.record_daily_snapshot(db_session, as_of=dt.date(2026, 1, 1))

    assert count == 2
    rows = db_session.query(CardSnapshot).order_by(CardSnapshot.card_id).all()
    assert [r.card_id for r in rows] == [card_a.id, card_b.id]
    assert rows[0].qty == 2
    assert rows[0].reference_price == 100.0
    assert rows[1].qty == 1
    assert rows[1].reference_price is None


def test_record_daily_snapshot_is_idempotent_for_the_same_day(db_session):
    card = _add_card(db_session, qty=2, reference_price=100.0)

    snapshots.record_daily_snapshot(db_session, as_of=dt.date(2026, 1, 1))
    card.qty = 5
    db_session.commit()
    count = snapshots.record_daily_snapshot(db_session, as_of=dt.date(2026, 1, 1))

    assert count == 1
    rows = db_session.query(CardSnapshot).all()
    assert len(rows) == 1  # updated in place, not duplicated
    assert rows[0].qty == 5


def test_record_daily_snapshot_creates_a_new_row_for_a_new_day(db_session):
    card = _add_card(db_session, qty=2, reference_price=100.0)

    snapshots.record_daily_snapshot(db_session, as_of=dt.date(2026, 1, 1))
    card.qty = 3
    db_session.commit()
    snapshots.record_daily_snapshot(db_session, as_of=dt.date(2026, 1, 2))

    rows = db_session.query(CardSnapshot).order_by(CardSnapshot.date).all()
    assert [(r.date, r.qty) for r in rows] == [
        (dt.date(2026, 1, 1), 2),
        (dt.date(2026, 1, 2), 3),
    ]


def test_record_daily_snapshot_defaults_to_today(db_session):
    _add_card(db_session, qty=1, reference_price=10.0)

    snapshots.record_daily_snapshot(db_session)

    row = db_session.query(CardSnapshot).one()
    assert row.date == dt.date.today()
