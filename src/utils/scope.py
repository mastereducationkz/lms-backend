"""Centralized row-scope service: which groups and students a staff user may see.

Why this exists
---------------
Group scope used to be re-derived inline at every list endpoint - roughly 40 distinct
``Group.teacher_id == me`` derivations and 45 ``Group.curator_id == me`` ones, in seven
mutually-inconsistent shapes. Some filtered ``Group.is_active``; some did not, so an
archived group could still leak rows. New endpoints must call this module instead of
adding another variant.

Semantics
---------
``visible_group_ids`` returns either :data:`UNRESTRICTED` (admin / head_curator see
everything) or a concrete list of group ids. Callers MUST distinguish the two: an empty
list means "this user may see nothing", which is the opposite of unrestricted. Use
:func:`apply_group_scope` to avoid getting that backwards.

Head-teacher scope is COURSE-LINK scope (``CourseHeadTeacher`` -> ``CourseGroupAccess``),
matching ``check_group_access`` and ``check_student_access``. The wider program_type
scope used by the leaderboard and lesson-requests is deliberately NOT used here: it
grants every active group of a whole program from a single managed course, which is too
broad for endpoints that carry student contact details.

Fails closed: any unrecognised role gets an empty list, never UNRESTRICTED.
"""
from typing import List, Optional, Sequence, Union

from sqlalchemy.orm import Session

from src.auth.models import UserInDB
from src.courses.models import Group, GroupStudent

# Sentinel meaning "no row restriction applies". Identity-compared (`is UNRESTRICTED`),
# never truthiness-compared - an empty list is falsy too and means the opposite.
UNRESTRICTED = None

ScopeResult = Union[None, List[int]]

# Roles that see every row. head_curator is global by design across the codebase
# (permissions.py:61/187/351/376 short-circuit it).
_UNRESTRICTED_ROLES = frozenset({"admin", "head_curator"})


def _active_groups_query(db: Session):
    """Base query for groups that are live: not archived and not finished."""
    return db.query(Group.id).filter(
        Group.is_active == True,  # noqa: E712 - SQLAlchemy column comparison
        Group.is_over == False,  # noqa: E712
    )


def visible_group_ids(user: UserInDB, db: Session) -> ScopeResult:
    """Group ids this user may see, or :data:`UNRESTRICTED` for global roles.

    Only live groups (``is_active`` and not ``is_over``) are ever returned.
    """
    role = (user.role or "").strip().lower()

    if role in _UNRESTRICTED_ROLES:
        return UNRESTRICTED

    if role == "teacher":
        rows = _active_groups_query(db).filter(Group.teacher_id == user.id).all()
        return [r[0] for r in rows]

    if role == "curator":
        rows = _active_groups_query(db).filter(Group.curator_id == user.id).all()
        return [r[0] for r in rows]

    if role == "head_teacher":
        # Course-link scope. Intersected with the live-group filter, because
        # get_head_teacher_group_ids does not apply one itself.
        from src.utils.permissions import get_head_teacher_group_ids

        linked_ids = get_head_teacher_group_ids(user, db)
        if not linked_ids:
            return []
        rows = _active_groups_query(db).filter(Group.id.in_(linked_ids)).all()
        return [r[0] for r in rows]

    # student, parent, and anything unrecognised: no staff row scope. Fail closed.
    return []


def visible_student_ids(user: UserInDB, db: Session) -> ScopeResult:
    """Student ids enrolled in the groups this user may see, deduplicated.

    Returns :data:`UNRESTRICTED` for global roles.
    """
    group_ids = visible_group_ids(user, db)
    if group_ids is UNRESTRICTED:
        return UNRESTRICTED
    if not group_ids:
        return []

    rows = (
        db.query(GroupStudent.student_id)
        .filter(GroupStudent.group_id.in_(group_ids))
        .distinct()
        .all()
    )
    return [r[0] for r in rows]


def can_view_group(user: UserInDB, db: Session, group_id: int) -> bool:
    """Whether this user may see the given group under the rules above."""
    group_ids = visible_group_ids(user, db)
    if group_ids is UNRESTRICTED:
        return True
    return group_id in set(group_ids)


def apply_group_scope(query, column, user: UserInDB, db: Session,
                      group_ids: Optional[Sequence[int]] = None):
    """Narrow ``query`` so ``column`` (a group-id column) stays inside the user's scope.

    Handles the empty-scope case correctly: a user with no groups gets a query that
    matches nothing, rather than an unfiltered one. Pass ``group_ids`` to reuse a scope
    already computed for this request instead of re-querying.

    Example::

        q = apply_group_scope(db.query(ExamResult), ExamResult.group_id, user, db)
    """
    scope = visible_group_ids(user, db) if group_ids is None else list(group_ids)
    if scope is UNRESTRICTED:
        return query
    if not scope:
        return query.filter(False)
    return query.filter(column.in_(scope))
