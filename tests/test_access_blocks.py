"""Windows of «no platform access», mirrored from the CRM for the curator grid.

The CRM decides who is blocked (a student who did not renew has their login turned off);
this table only says *when*, so the leaderboard can leave those lessons out of the
attendance denominator the way it already does for freeze days.
"""
from datetime import date, timedelta

import pytest

from src.curator.access_blocks import AccessBlockIndex, StudentAccessBlock, access_block_index
from tests.onboarding_fixtures import db  # noqa: F401 - transactional session fixture


def _row(user_id, start_days_ago, end_days_ago=None, kind="not_renewed"):
    return StudentAccessBlock(
        user_id=user_id,
        blocked_from=date.today() - timedelta(days=start_days_ago),
        blocked_until=None if end_days_ago is None else date.today() - timedelta(days=end_days_ago),
        kind=kind,
    )


def test_an_open_block_covers_from_its_start_onwards():
    index = AccessBlockIndex([_row(7, start_days_ago=5)])
    assert index.is_blocked_on(7, date.today()) is True
    assert index.is_blocked_on(7, date.today() - timedelta(days=5)) is True
    assert index.is_blocked_on(7, date.today() - timedelta(days=6)) is False


def test_a_closed_block_excludes_its_end_day():
    # blocked_until is the day access came back: that day counts again.
    index = AccessBlockIndex([_row(7, start_days_ago=10, end_days_ago=3)])
    assert index.is_blocked_on(7, date.today() - timedelta(days=4)) is True
    assert index.is_blocked_on(7, date.today() - timedelta(days=3)) is False


def test_other_students_and_missing_days_are_never_blocked():
    index = AccessBlockIndex([_row(7, start_days_ago=5)])
    assert index.is_blocked_on(8, date.today()) is False
    assert index.is_blocked_on(7, None) is False


def test_index_loads_rows_for_the_page_in_one_query(db):  # noqa: F811
    db.add_all([_row(101, start_days_ago=2), _row(102, start_days_ago=30, end_days_ago=20)])
    db.commit()
    index = access_block_index(db, [101, 102, 103])
    assert index.is_blocked_on(101, date.today()) is True
    assert index.is_blocked_on(102, date.today()) is False
    assert index.is_blocked_on(102, date.today() - timedelta(days=25)) is True
    assert index.is_blocked_on(103, date.today()) is False
