from datetime import datetime, timedelta

from src.utils.homework_status import is_submission_late

DUE = datetime(2026, 7, 20, 12, 0, 0)


def test_on_time_is_not_late():
    assert is_submission_late(DUE - timedelta(hours=1), DUE) is False


def test_exactly_on_deadline_is_not_late():
    assert is_submission_late(DUE, DUE) is False


def test_after_deadline_is_late():
    assert is_submission_late(DUE + timedelta(minutes=1), DUE) is True


def test_extension_overrides_due_date():
    ext = DUE + timedelta(days=2)
    # Submitted after the original due date but before the extended one -> on time.
    assert is_submission_late(DUE + timedelta(hours=5), DUE, ext) is False
    # Submitted after even the extension -> late.
    assert is_submission_late(ext + timedelta(minutes=1), DUE, ext) is True


def test_no_submission_is_not_late():
    assert is_submission_late(None, DUE) is False


def test_no_deadline_is_not_late():
    assert is_submission_late(DUE + timedelta(days=1), None) is False
