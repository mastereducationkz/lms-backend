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


def test_grant_is_active_accepts_aware_now():
    from src.trials.services import grant_is_active
    assert grant_is_active(_grant(), now=datetime.now(timezone.utc)) is True


def test_grant_is_active_false_at_exact_expiry_instant():
    from src.trials.services import grant_is_active
    g = _grant()
    assert grant_is_active(g, now=g.expires_at) is False


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


def test_cached_decorator_bypasses_trial_users():
    from src.services import cache_service

    calls = {"n": 0}

    @cache_service.cached(namespace="t:x", ttl=60)
    def handler(current_user=None):
        calls["n"] += 1
        return {"n": calls["n"]}

    trial_user = SimpleNamespace(id=1, role="student", is_trial=True)
    # Even with a client present, trial users must never be served from cache.
    # _cache_key returning None guarantees the wrapped function runs every time.
    key = handler.__wrapped__ is not None  # decorator applied
    assert key
    r1 = handler(current_user=trial_user)
    r2 = handler(current_user=trial_user)
    assert (r1["n"], r2["n"]) == (1, 2)


def test_check_course_access_trial_student(monkeypatch):
    from src.utils import permissions

    trial_user = SimpleNamespace(id=7, role="student", is_trial=True)
    monkeypatch.setattr(
        "src.trials.services.get_active_trial", lambda db, uid, cid: _grant() if cid == 10 else None
    )
    # Course existence check happens before role branches:
    fake_db = SimpleNamespace(query=lambda model: SimpleNamespace(
        filter=lambda *a, **k: SimpleNamespace(first=lambda: object())
    ))
    assert permissions.check_course_access(10, trial_user, fake_db) is True
    assert permissions.check_course_access(11, trial_user, fake_db) is False


def test_trial_hard_gate_blocks_and_passes(monkeypatch):
    import pytest
    from fastapi import HTTPException
    from src.courses.routes import courses as courses_routes

    trial_user = SimpleNamespace(id=7, role="student", is_trial=True)
    monkeypatch.setattr(courses_routes, "trial_lesson_access", lambda db, uid, lid: (False, "Your trial has ended"))
    with pytest.raises(HTTPException) as exc:
        courses_routes._trial_hard_gate(None, trial_user, 1)
    assert exc.value.status_code == 403

    monkeypatch.setattr(courses_routes, "trial_lesson_access", lambda db, uid, lid: (True, None))
    courses_routes._trial_hard_gate(None, trial_user, 1)  # no raise

    real_student = SimpleNamespace(id=8, role="student", is_trial=False)
    monkeypatch.setattr(courses_routes, "trial_lesson_access", lambda db, uid, lid: (False, "x"))
    courses_routes._trial_hard_gate(None, real_student, 1)  # no-op for real students


def test_media_trial_gate_blocks_and_passes(monkeypatch):
    import pytest
    from fastapi import HTTPException
    from src.admin.routes import media

    trial_user = SimpleNamespace(id=7, role="student", is_trial=True)
    monkeypatch.setattr(media, "trial_lesson_access", lambda db, uid, lid: (False, "Your trial has ended"))
    with pytest.raises(HTTPException) as exc:
        media._media_trial_gate(None, trial_user, 1)
    assert exc.value.status_code == 403

    monkeypatch.setattr(media, "trial_lesson_access", lambda db, uid, lid: (True, None))
    media._media_trial_gate(None, trial_user, 1)  # no raise

    real_student = SimpleNamespace(id=8, role="student", is_trial=False)
    monkeypatch.setattr(media, "trial_lesson_access", lambda db, uid, lid: (False, "x"))
    media._media_trial_gate(None, real_student, 1)  # no-op for real students

    # Fails closed when a trial user's material can't be resolved to a lesson
    monkeypatch.setattr(media, "trial_lesson_access", lambda db, uid, lid: (True, None))
    with pytest.raises(HTTPException) as exc:
        media._media_trial_gate(None, trial_user, None)
    assert exc.value.status_code == 403


def test_expire_stale_trials_for_targets_only_stale_active_pair():
    from unittest.mock import MagicMock
    from src.trials import services
    from src.trials.models import TrialAccess, TRIAL_ACTIVE, TRIAL_EXPIRED

    db = MagicMock()
    db.query.return_value.filter.return_value.update.return_value = 2

    count = services.expire_stale_trials_for(db, user_id=7, course_id=10)
    assert count == 2

    # Targets TrialAccess rows only
    db.query.assert_called_once_with(TrialAccess)

    # Filter criteria: exactly (user_id, course_id, status=='active', expires_at <= now)
    filter_args = db.query.return_value.filter.call_args[0]
    assert len(filter_args) == 4
    assert filter_args[0].compare(TrialAccess.user_id == 7)
    assert filter_args[1].compare(TrialAccess.course_id == 10)
    assert filter_args[2].compare(TrialAccess.status == TRIAL_ACTIVE)
    deadline = filter_args[3]
    assert deadline.left.name == "expires_at"
    assert deadline.operator.__name__ == "le"  # past-deadline only, boundary inclusive
    assert deadline.right.value.tzinfo is None  # naive UTC, matching the column

    # Flips to expired without loading rows; caller owns the transaction
    update_args, update_kwargs = db.query.return_value.filter.return_value.update.call_args
    assert update_args[0][TrialAccess.status] == TRIAL_EXPIRED
    assert update_kwargs.get("synchronize_session") is False
    db.commit.assert_not_called()


def test_should_rotate_password_only_when_no_other_active_grants():
    from src.trials.services import should_rotate_password
    assert should_rotate_password([]) is True
    assert should_rotate_password([_grant()]) is False


def test_effective_status_computed():
    from src.trials.schemas import effective_status
    assert effective_status(_grant()) == "active"
    assert effective_status(_grant(expires_in_minutes=-1)) == "expired"
    assert effective_status(_grant(status="revoked")) == "revoked"
    assert effective_status(_grant(status="converted")) == "converted"


def test_validate_lesson_ids_pure():
    from src.trials.routes.trials import _validate_lesson_ids
    # lessons that exist in the course: {1, 2, 3}
    assert _validate_lesson_ids([1, 2], {1, 2, 3}) == [1, 2]
    import pytest
    from fastapi import HTTPException
    with pytest.raises(HTTPException):
        _validate_lesson_ids([], {1, 2, 3})          # empty
    with pytest.raises(HTTPException):
        _validate_lesson_ids([1, 99], {1, 2, 3})     # foreign lesson


def test_trial_filter_lesson_payload_strips_steps_outside_allowlist():
    from src.courses.routes.courses import _trial_filter_lesson_payload

    lessons = [
        SimpleNamespace(id=1, title="Allowed lesson", steps=["s1", "s2"], total_steps=2),
        SimpleNamespace(id=2, title="Locked lesson", steps=["s3"], total_steps=1),
    ]
    out = _trial_filter_lesson_payload(lessons, {1})
    assert out is lessons  # mutates and returns the same list
    # Allowlisted lesson keeps its steps
    assert lessons[0].steps == ["s1", "s2"]
    assert lessons[0].total_steps == 2
    # Non-allowlisted lesson: content stripped, metadata preserved
    assert lessons[1].steps == []
    assert lessons[1].total_steps == 0
    assert lessons[0].title == "Allowed lesson"
    assert lessons[1].title == "Locked lesson"
    # If the serialized object carries is_accessible, it is set per allowlist
    with_flag = [
        SimpleNamespace(id=1, title="a", steps=["s"], total_steps=1, is_accessible=None),
        SimpleNamespace(id=2, title="b", steps=["s"], total_steps=1, is_accessible=None),
    ]
    _trial_filter_lesson_payload(with_flag, {1})
    assert with_flag[0].is_accessible is True
    assert with_flag[1].is_accessible is False


def test_auth_me_carries_trial_expiry(monkeypatch):
    from src.auth import user_schema as us

    user = SimpleNamespace(
        id=5, email="p@x.kz", name="P", role="student", is_active=True,
        is_trial=True, assignment_zero_completed=True,
    )
    deadline = datetime.utcnow() + timedelta(hours=3)
    monkeypatch.setattr("src.trials.services.earliest_active_expiry", lambda db, uid: deadline)
    monkeypatch.setattr(us, "student_has_only_special_groups", lambda uid, db: False)
    monkeypatch.setattr(us.UserSchema, "model_validate", classmethod(lambda cls, u: us.UserSchema(
        id=u.id, email=u.email, name=u.name, role=u.role, is_active=u.is_active, is_trial=True,
    )))
    resp = us.build_user_schema_response(user, db=None)
    assert resp.trial_expires_at == deadline


def test_trial_status_job_uses_expire_stale(monkeypatch):
    from src.services import trial_status_job

    called = {}
    monkeypatch.setattr(trial_status_job, "SessionLocal", lambda: SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(trial_status_job, "expire_stale_trials", lambda db: called.setdefault("n", 3))
    trial_status_job.run_once()
    assert called["n"] == 3
