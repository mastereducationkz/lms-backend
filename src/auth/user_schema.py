from sqlalchemy.orm import Session

from src.auth.schemas import CurrentUserSchema
from src.schemas.models import UserInDB
from src.utils.course_access import student_has_only_special_groups


def build_user_schema_response(user: UserInDB, db: Session) -> CurrentUserSchema:
    """The user's own record.

    Built as ``CurrentUserSchema`` so ``/auth/me`` can include the viewer-only fields.
    The other callers are typed ``UserSchema`` and drop those fields on serialisation,
    which is covered by a test rather than assumed.
    """
    base = CurrentUserSchema.model_validate(user)
    if user.role != "student":
        return base.model_copy(update={"special_group_only_student": False})
    update = {"special_group_only_student": student_has_only_special_groups(user.id, db)}
    if getattr(user, "is_trial", False):
        from src.trials.services import earliest_active_expiry
        update["trial_expires_at"] = earliest_active_expiry(db, user.id)
    return base.model_copy(update=update)
