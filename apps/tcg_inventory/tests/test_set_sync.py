import httpx

import set_sync
from models import Set


def _api_set(id_, name, series, release_date, total=None, printed_total=None):
    return {
        "id": id_,
        "name": name,
        "series": series,
        "releaseDate": release_date,
        "total": total,
        "printedTotal": printed_total,
    }


def test_sync_matches_by_exact_name_and_writes_rank_and_total_cards(db_session):
    db_session.add(Set(series="Original", name="Base Set"))
    db_session.add(Set(series="Neo", name="Neo Revelation"))
    db_session.commit()

    api_sets = [
        _api_set("base1", "Base Set", "Base", "1999/01/09", total=102, printed_total=102),
        _api_set("neo3", "Neo Revelation", "Neo", "2001/09/21", total=66, printed_total=64),
    ]

    result = set_sync.sync_set_metadata(db_session, api_sets=api_sets)

    assert result.api_call_succeeded
    base = db_session.query(Set).filter_by(name="Base Set").one()
    neo = db_session.query(Set).filter_by(name="Neo Revelation").one()
    # Base Set released earlier -> lower (earlier) rank than Neo Revelation.
    assert base.release_rank == 1
    assert neo.release_rank == 2
    assert base.total_cards == 102
    assert neo.total_cards == 66
    assert set(result.matched) == {"Original / Base Set", "Neo / Neo Revelation"}
    assert result.unmatched == []


def test_sync_falls_back_to_printed_total_when_total_missing(db_session):
    db_session.add(Set(series="Original", name="Base Set"))
    db_session.commit()

    api_sets = [_api_set("base1", "Base Set", "Base", "1999/01/09", total=None, printed_total=102)]
    set_sync.sync_set_metadata(db_session, api_sets=api_sets)

    base = db_session.query(Set).filter_by(name="Base Set").one()
    assert base.total_cards == 102


def test_sync_leaves_unmatched_set_untouched(db_session):
    row = Set(series="Scarlet & Violet", name="151 JP/KR", release_rank=None, total_cards=None)
    db_session.add(row)
    db_session.commit()

    api_sets = [_api_set("sv-151", "151", "Scarlet & Violet", "2023/09/22", total=207)]
    result = set_sync.sync_set_metadata(db_session, api_sets=api_sets)

    refreshed = db_session.query(Set).filter_by(name="151 JP/KR").one()
    assert refreshed.release_rank is None
    assert refreshed.total_cards is None
    assert result.unmatched == ["Scarlet & Violet / 151 JP/KR"]
    assert result.matched == []


def test_sync_does_not_clobber_a_set_it_cannot_confidently_match(db_session):
    """Ambiguous name (two API sets share it) that also can't be resolved
    by series -- must be left alone, never a guessed rank.
    """
    row = Set(series="Made Up Series", name="Base Set", release_rank=5, total_cards=None)
    db_session.add(row)
    db_session.commit()

    api_sets = [
        _api_set("base1", "Base Set", "Base", "1999/01/09", total=102),
        _api_set("base2", "Base Set", "Other Series", "2005/01/01", total=50),
    ]
    result = set_sync.sync_set_metadata(db_session, api_sets=api_sets)

    refreshed = db_session.query(Set).filter_by(name="Base Set").one()
    # Untouched -- kept its previous hand-entered rank, not overwritten by a guess.
    assert refreshed.release_rank == 5
    assert result.unmatched == ["Made Up Series / Base Set"]


def test_sync_resolves_ambiguous_name_via_matching_series(db_session):
    db_session.add(Set(series="Other Series", name="Base Set"))
    db_session.commit()

    api_sets = [
        _api_set("base1", "Base Set", "Base", "1999/01/09", total=102),
        _api_set("base2", "Base Set", "Other Series", "2005/01/01", total=50),
    ]
    result = set_sync.sync_set_metadata(db_session, api_sets=api_sets)

    refreshed = db_session.query(Set).filter_by(series="Other Series", name="Base Set").one()
    assert refreshed.total_cards == 50
    assert result.matched == ["Other Series / Base Set"]


def test_sync_overwrites_a_previously_hand_entered_rank_on_a_confident_match(db_session):
    db_session.add(Set(series="Original", name="Base Set", release_rank=1, total_cards=None))
    db_session.commit()

    api_sets = [_api_set("base1", "Base Set", "Base", "1999/01/09", total=102)]
    set_sync.sync_set_metadata(db_session, api_sets=api_sets)

    refreshed = db_session.query(Set).filter_by(name="Base Set").one()
    assert refreshed.release_rank == 1  # only one set in this fixture -> rank 1
    assert refreshed.total_cards == 102


def test_sync_returns_api_call_failed_flag_and_changes_nothing(db_session, monkeypatch):
    db_session.add(Set(series="Original", name="Base Set", release_rank=7))
    db_session.commit()

    def raising_get(*args, **kwargs):
        raise httpx.ConnectError("no network")

    monkeypatch.setattr(set_sync.httpx, "get", raising_get)

    result = set_sync.sync_set_metadata(db_session)

    assert result.api_call_succeeded is False
    assert result.matched == []
    assert result.unmatched == []
    unchanged = db_session.query(Set).filter_by(name="Base Set").one()
    assert unchanged.release_rank == 7


def test_fetch_api_sets_retries_once_on_flaky_error_then_succeeds(monkeypatch):
    calls = {"count": 0}

    class _FakeResponse:
        def __init__(self, payload):
            self._payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self._payload

    def flaky_get(*args, **kwargs):
        calls["count"] += 1
        if calls["count"] == 1:
            raise httpx.ConnectError("flaky")
        return _FakeResponse({"data": [{"id": "base1", "name": "Base Set"}]})

    monkeypatch.setattr(set_sync.httpx, "get", flaky_get)

    result = set_sync.fetch_api_sets()

    assert calls["count"] == 2
    assert result == [{"id": "base1", "name": "Base Set"}]


def test_release_ranks_orders_by_release_date_ascending():
    api_sets = [
        {"id": "c", "releaseDate": "2020/01/01"},
        {"id": "a", "releaseDate": "1999/01/09"},
        {"id": "b", "releaseDate": "2001/09/21"},
    ]
    ranks = set_sync._release_ranks(api_sets)
    assert ranks == {"a": 1, "b": 2, "c": 3}


def test_release_ranks_puts_missing_date_last():
    api_sets = [
        {"id": "known", "releaseDate": "1999/01/09"},
        {"id": "unknown", "releaseDate": None},
    ]
    ranks = set_sync._release_ranks(api_sets)
    assert ranks["known"] < ranks["unknown"]
