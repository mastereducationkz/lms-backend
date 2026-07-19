"""Trial access: model shape, pure service logic, route helpers."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace


def _grant(status="active", expires_in_minutes=60, lesson_ids=(1, 2)):
    return SimpleNamespace(
        status=status,
        expires_at=datetime.utcnow() + timedelta(minutes=expires_in_minutes),
        lesson_ids=list(lesson_ids),
        course_id=10,
    )


def test_trial_access_model_shape():
    from src.trials.models import TrialAccess

    cols = {c.name for c in TrialAccess.__table__.columns}
    assert {
        "id", "user_id", "course_id", "lesson_ids", "expires_at", "status",
        "granted_by", "prospect_note", "created_at", "updated_at", "revoked_at",
    } <= cols


def test_user_has_is_trial_flag():
    from src.auth.models import UserInDB

    assert "is_trial" in {c.name for c in UserInDB.__table__.columns}


def test_grant_is_active_true_before_deadline():
    from src.trials.services import grant_is_active
    assert grant_is_active(_grant()) is True


def test_grant_is_active_false_after_deadline():
    from src.trials.services import grant_is_active
    assert grant_is_active(_grant(expires_in_minutes=-1)) is False


def test_grant_is_active_false_for_non_active_statuses():
    from src.trials.services import grant_is_active
    for status in ("expired", "revoked", "converted"):
        assert grant_is_active(_grant(status=status)) is False
    assert grant_is_active(None) is False


def test_grant_is_active_handles_aware_expires_at():
    from src.trials.services import grant_is_active
    g = _grant()
    g.expires_at = datetime.now(timezone.utc) + timedelta(minutes=5)
    assert grant_is_active(g) is True


def test_lesson_in_grant_coerces_types():
    from src.trials.services import lesson_in_grant
    g = _grant(lesson_ids=["3", 4])
    assert lesson_in_grant(g, 3) is True
    assert lesson_in_grant(g, 4) is True
    assert lesson_in_grant(g, 5) is False


def test_evaluate_trial_lesson_access_matrix():
    from src.trials.services import evaluate_trial_lesson_access
    ok, reason = evaluate_trial_lesson_access(_grant(), 1)
    assert ok is True and reason is None
    ok, reason = evaluate_trial_lesson_access(_grant(), 99)
    assert ok is False and reason == "Not included in your trial"
    ok, reason = evaluate_trial_lesson_access(_grant(expires_in_minutes=-1), 1)
    assert ok is False and reason == "Your trial has ended"
    ok, reason = evaluate_trial_lesson_access(None, 1)
    assert ok is False and reason == "You do not have access to this course"
