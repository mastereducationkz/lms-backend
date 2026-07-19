"""Trial access: model shape, pure service logic, route helpers."""


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
