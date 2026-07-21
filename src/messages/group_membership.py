"""Membership enumeration + provisioning for group chat channels."""
from sqlalchemy.orm import Session

from src.schemas.models import (
    UserInDB, Group, GroupStudent, ParentStudent,
    GroupConversation, GroupConversationMember,
)

ADMIN_ROLES = ("admin", "head_curator")


def _active_student_ids(db: Session, group_id: int) -> set:
    rows = (db.query(GroupStudent.student_id)
              .join(UserInDB, UserInDB.id == GroupStudent.student_id)
              .filter(GroupStudent.group_id == group_id,
                      UserInDB.is_active == True))  # noqa: E712
    return {r[0] for r in rows}


def _admin_ids(db: Session) -> set:
    rows = db.query(UserInDB.id).filter(UserInDB.role.in_(ADMIN_ROLES),
                                        UserInDB.is_active == True)  # noqa: E712
    return {r[0] for r in rows}


def _staff_ids(group: Group) -> set:
    return {i for i in (group.teacher_id, group.curator_id) if i}


def class_member_ids(db: Session, group: Group) -> set:
    return _active_student_ids(db, group.id) | _staff_ids(group) | _admin_ids(db)


def parent_member_ids(db: Session, group: Group) -> set:
    student_ids = _active_student_ids(db, group.id)
    if student_ids:
        prows = db.query(ParentStudent.parent_id).filter(ParentStudent.student_id.in_(student_ids))
        parents = {r[0] for r in prows}
    else:
        parents = set()
    return parents | _staff_ids(group) | _admin_ids(db)


def _sync_members(db: Session, conv: GroupConversation, desired_ids: set) -> None:
    existing = {m.user_id: m for m in
                db.query(GroupConversationMember).filter_by(conversation_id=conv.id).all()}
    for uid in desired_ids - set(existing):
        db.add(GroupConversationMember(conversation_id=conv.id, user_id=uid))
    for uid in set(existing) - desired_ids:
        db.delete(existing[uid])


def ensure_group_conversations(db: Session, group: Group) -> dict:
    result = {}
    for kind, ids in (("class", class_member_ids(db, group)),
                      ("parents", parent_member_ids(db, group))):
        conv = (db.query(GroupConversation)
                  .filter_by(group_id=group.id, kind=kind).first())
        if conv is None:
            conv = GroupConversation(group_id=group.id, kind=kind)
            db.add(conv); db.flush()
        _sync_members(db, conv, ids)
        result[kind] = conv
    return result


def sync_group_conversation_members(db: Session, group_id: int) -> None:
    group = db.query(Group).filter_by(id=group_id).first()
    if group is None:
        return
    ensure_group_conversations(db, group)


def sync_groups_for_students(db: Session, student_ids) -> None:
    """Resync group-chat channels for every group the given students belong to.

    Used after parent<->child links change: a newly-linked parent must appear in the
    parents channel of each of their children's groups, and an unlinked parent must be
    removed (when they no longer have a child in that group). Does not commit — the
    caller owns the transaction boundary.
    """
    ids = list(student_ids or [])
    if not ids:
        return
    rows = db.query(GroupStudent.group_id).filter(GroupStudent.student_id.in_(ids)).all()
    for gid in {r[0] for r in rows}:
        sync_group_conversation_members(db, gid)


def provision_all_groups(db: Session) -> int:
    groups = db.query(Group).filter(Group.is_active == True).all()  # noqa: E712
    for g in groups:
        ensure_group_conversations(db, g)
    db.commit()
    return len(groups)
