# Group Chat — Backend Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add backend support for per-group chat channels (a "class" channel and a "parents" channel per group) that reuse the existing 1:1 Socket.IO + push + media infrastructure.

**Architecture:** New tables (`group_conversations`, `group_conversation_members`, `group_messages`) plus a membership/provisioning layer and a shared service layer (`src/messages/group_service.py`). Both a REST router and Socket.IO handlers are thin wrappers over the service layer, so all business logic is unit-testable by calling functions directly (the codebase's existing test style). The 1:1 system is untouched and keeps working.

**Tech Stack:** FastAPI, SQLAlchemy (declarative `Base` in `src/models/base.py`), Alembic, python-socketio (ASGI), pytest (savepoint-isolated `db` fixture).

## Global Constraints

- Edit only the **mounted** modular tree `src/<domain>/...`. The flat `src/routes/messages.py`, `src/routes/socket_messages.py`, `src/routes/media.py` are dead for routing — do NOT edit them. (Exception: `get_current_user_dependency` is imported from `src/routes/auth.py` by live routers — reuse that import, don't move it.)
- Channel kinds are exactly `'class'` and `'parents'`.
- **Class channel members** = active `GroupStudent` students of the group + `group.teacher_id` + `group.curator_id` + all users with `role == 'admin'` + all `role == 'head_curator'`.
- **Parents channel members** = `ParentStudent` parents of the group's active students + `group.teacher_id` + `group.curator_id` + all `admin` + all `head_curator`.
- **Posting rule:** any member may post EXCEPT a `student` in a group where `groups.is_special == True` (special-group students are read-only in group channels; this mirrors the existing 1:1 "special-group students may only message admins" rule). Staff/admin/parent always may post.
- Access to a conversation = a row in `group_conversation_members` for that `(conversation_id, user_id)`.
- Socket rooms mirror the existing convention: user rooms are `f"user:{user_id}"`; group rooms are `f"group:{conversation_id}"`.
- Push `data` payload for group messages must use `type: "group_message"` and include `conversationId` (distinct from the 1:1 `type: "message"`).
- Alembic: new migration's `down_revision = 'w7x8y9z1a2b3'` (current head). Any new model file MUST be imported in `src/models/__init__.py` or autogenerate/metadata misses it.
- Commit messages: no `Co-Authored-By` trailer. Never `git add -A` (an untracked `prod_backup_*.dump` must never be committed). Run tests with `venv/bin/python -m pytest ...`.
- Work on branch `feature/group-chat-backend` (create it off `main` before Task 1 if not present).

---

## File Structure

- `src/messages/group_models.py` — NEW: `GroupConversation`, `GroupConversationMember`, `GroupMessage`.
- `src/models/__init__.py` — MODIFY: import + `__all__` the three new models.
- `src/auth/models.py` — MODIFY: add cascade relationships on `UserInDB` (~line 57).
- `alembic/versions/gc1_group_chat_tables.py` — NEW: create the three tables.
- `src/messages/group_membership.py` — NEW: member-enumeration + provisioning + sync.
- `src/messages/group_service.py` — NEW: list/get/post/read service functions (all business rules).
- `src/messages/group_schemas.py` — NEW: Pydantic response/request schemas.
- `src/messages/routes/group_messages.py` — NEW: REST router mounted under `/messages`.
- `src/messages/routes/__init__.py` — MODIFY: export the new router.
- `src/routes/__init__.py` — MODIFY: include the new router (or reuse the existing `messages_router` mount if the router is merged there — see Task 5).
- `src/messages/routes/socket_messages.py` — MODIFY: add `group:*` events + auto-join in `connect`.
- `src/utils/push_notifications.py` — MODIFY: add `send_group_message_push`.
- Admin group-mutation endpoints — MODIFY (Task 7): call `sync_group_conversation_members` after membership changes.
- `tests/test_group_chat_*.py` — NEW test files per task.

---

### Task 1: Group-chat models + registration + cascade relationships

**Files:**
- Create: `src/messages/group_models.py`
- Modify: `src/models/__init__.py`
- Modify: `src/auth/models.py:57` (add relationships)
- Test: `tests/test_group_chat_models.py`

**Interfaces:**
- Produces: `GroupConversation(id, group_id, kind, created_at)`, `GroupConversationMember(id, conversation_id, user_id, last_read_at, created_at)`, `GroupMessage(id, conversation_id, from_user_id, content, file_url, created_at)`; all importable from `src.schemas.models` and `src.models`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_group_chat_models.py` (savepoint `db` fixture identical to `tests/test_account_deletion.py`):
```python
import pytest
from src.schemas.models import (
    UserInDB, Group, GroupConversation, GroupConversationMember, GroupMessage,
)


@pytest.fixture
def db():
    from sqlalchemy import event
    from sqlalchemy.exc import OperationalError
    from sqlalchemy.orm import Session as SASession
    from src.config import engine
    try:
        connection = engine.connect()
    except OperationalError:
        pytest.skip("No database available")
    trans = connection.begin()
    session = SASession(bind=connection)
    session.begin_nested()

    @event.listens_for(session, "after_transaction_end")
    def _restart(sess, transaction):
        if transaction.nested and not transaction._parent.nested:
            sess.begin_nested()

    try:
        yield session
    finally:
        event.remove(session, "after_transaction_end", _restart)
        session.close()
        trans.rollback()
        connection.close()


def test_group_conversation_and_message_roundtrip(db):
    from src.utils.auth_utils import hash_password
    u = UserInDB(email="gc-a@test.local", name="A", role="student",
                 hashed_password=hash_password("x"), is_active=True)
    g = Group(name="GC Group", is_active=True)
    db.add_all([u, g]); db.flush()
    conv = GroupConversation(group_id=g.id, kind="class")
    db.add(conv); db.flush()
    db.add(GroupConversationMember(conversation_id=conv.id, user_id=u.id))
    db.add(GroupMessage(conversation_id=conv.id, from_user_id=u.id, content="hello"))
    db.flush()
    assert db.query(GroupMessage).filter_by(conversation_id=conv.id).count() == 1
    assert db.query(GroupConversationMember).filter_by(conversation_id=conv.id, user_id=u.id).count() == 1


def test_deleting_user_cascades_group_membership_and_messages(db):
    from src.utils.auth_utils import hash_password
    u = UserInDB(email="gc-b@test.local", name="B", role="student",
                 hashed_password=hash_password("x"), is_active=True)
    g = Group(name="GC Group2", is_active=True)
    db.add_all([u, g]); db.flush()
    conv = GroupConversation(group_id=g.id, kind="class"); db.add(conv); db.flush()
    db.add(GroupConversationMember(conversation_id=conv.id, user_id=u.id))
    db.add(GroupMessage(conversation_id=conv.id, from_user_id=u.id, content="x"))
    db.flush()
    db.delete(u); db.flush()
    assert db.query(GroupConversationMember).filter_by(user_id=u.id).count() == 0
    assert db.query(GroupMessage).filter_by(from_user_id=u.id).count() == 0
```

- [ ] **Step 2: Run it to verify it fails**

Run: `venv/bin/python -m pytest tests/test_group_chat_models.py -v`
Expected: FAIL — `ImportError: cannot import name 'GroupConversation'`.

- [ ] **Step 3: Create the models**

Create `src/messages/group_models.py`:
```python
from sqlalchemy import (
    Column, String, Integer, DateTime, Text, ForeignKey, UniqueConstraint, Index,
)
from sqlalchemy.orm import relationship
from datetime import datetime, timezone

from src.models.base import Base


class GroupConversation(Base):
    """A chat channel derived from a Group. kind='class' (students+staff) or 'parents' (parents+staff)."""
    __tablename__ = "group_conversations"
    id = Column(Integer, primary_key=True, index=True)
    group_id = Column(Integer, ForeignKey("groups.id", ondelete="CASCADE"), nullable=False, index=True)
    kind = Column(String(16), nullable=False)  # 'class' | 'parents'
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    members = relationship("GroupConversationMember", back_populates="conversation",
                           cascade="all, delete-orphan")
    messages = relationship("GroupMessage", back_populates="conversation",
                            cascade="all, delete-orphan")

    __table_args__ = (
        UniqueConstraint("group_id", "kind", name="uq_group_conversation_group_kind"),
    )


class GroupConversationMember(Base):
    __tablename__ = "group_conversation_members"
    id = Column(Integer, primary_key=True, index=True)
    conversation_id = Column(Integer, ForeignKey("group_conversations.id", ondelete="CASCADE"),
                             nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    last_read_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    conversation = relationship("GroupConversation", back_populates="members")
    user = relationship("UserInDB", back_populates="group_chat_memberships")

    __table_args__ = (
        UniqueConstraint("conversation_id", "user_id", name="uq_group_conv_member"),
    )


class GroupMessage(Base):
    __tablename__ = "group_messages"
    id = Column(Integer, primary_key=True, index=True)
    conversation_id = Column(Integer, ForeignKey("group_conversations.id", ondelete="CASCADE"),
                             nullable=False, index=True)
    from_user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    content = Column(Text, nullable=False)
    file_url = Column(String, nullable=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    conversation = relationship("GroupConversation", back_populates="messages")
    sender = relationship("UserInDB", back_populates="sent_group_messages")

    __table_args__ = (
        Index("idx_group_messages_conv_created", "conversation_id", "created_at"),
    )
```

- [ ] **Step 4: Register models and add UserInDB relationships**

In `src/models/__init__.py`, add after the `from src.messages.models import Message, Notification` line:
```python
from src.messages.group_models import GroupConversation, GroupConversationMember, GroupMessage
```
and add to `__all__` (in the messages section):
```python
    "GroupConversation", "GroupConversationMember", "GroupMessage",
```

In `src/auth/models.py`, immediately after the `parent_links = relationship(...)` line (~line 57), add:
```python
    group_chat_memberships = relationship("GroupConversationMember", back_populates="user", cascade="all, delete-orphan")
    sent_group_messages = relationship("GroupMessage", back_populates="sender", cascade="all, delete-orphan")
```

- [ ] **Step 5: Run the test to verify it passes**

Run: `venv/bin/python -m pytest tests/test_group_chat_models.py -v`
Expected: 2 passed. (The DB tables don't exist yet, but the savepoint fixture runs against the dev DB — if the tables are missing this fails with a "relation does not exist" error, which is expected until Task 2. If so, run Task 2 first, then return. To keep TDD order, you MAY run Task 2's migration now, then re-run this test.)

- [ ] **Step 6: Commit**

```bash
git add src/messages/group_models.py src/models/__init__.py src/auth/models.py tests/test_group_chat_models.py
git commit -m "feat(chat): add group conversation/member/message models"
```

---

### Task 2: Alembic migration for the three tables

**Files:**
- Create: `alembic/versions/gc1_group_chat_tables.py`

**Interfaces:**
- Consumes: the models from Task 1.
- Produces: DB tables `group_conversations`, `group_conversation_members`, `group_messages`.

- [ ] **Step 1: Write the migration**

Create `alembic/versions/gc1_group_chat_tables.py`:
```python
"""group chat tables

Revision ID: gc1_group_chat_tables
Revises: w7x8y9z1a2b3
Create Date: 2026-07-21
"""
from alembic import op
import sqlalchemy as sa

revision = 'gc1_group_chat_tables'
down_revision = 'w7x8y9z1a2b3'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'group_conversations',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('group_id', sa.Integer(), sa.ForeignKey('groups.id', ondelete='CASCADE'), nullable=False),
        sa.Column('kind', sa.String(length=16), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()')),
        sa.UniqueConstraint('group_id', 'kind', name='uq_group_conversation_group_kind'),
    )
    op.create_index('ix_group_conversations_group_id', 'group_conversations', ['group_id'])

    op.create_table(
        'group_conversation_members',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('conversation_id', sa.Integer(), sa.ForeignKey('group_conversations.id', ondelete='CASCADE'), nullable=False),
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False),
        sa.Column('last_read_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()')),
        sa.UniqueConstraint('conversation_id', 'user_id', name='uq_group_conv_member'),
    )
    op.create_index('ix_group_conversation_members_conversation_id', 'group_conversation_members', ['conversation_id'])
    op.create_index('ix_group_conversation_members_user_id', 'group_conversation_members', ['user_id'])

    op.create_table(
        'group_messages',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('conversation_id', sa.Integer(), sa.ForeignKey('group_conversations.id', ondelete='CASCADE'), nullable=False),
        sa.Column('from_user_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False),
        sa.Column('content', sa.Text(), nullable=False),
        sa.Column('file_url', sa.String(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()')),
    )
    op.create_index('idx_group_messages_conv_created', 'group_messages', ['conversation_id', 'created_at'])


def downgrade():
    op.drop_table('group_messages')
    op.drop_table('group_conversation_members')
    op.drop_table('group_conversations')
```

- [ ] **Step 2: Apply and verify**

Run:
```bash
venv/bin/python -m alembic upgrade head
venv/bin/python -c "
from src.config import engine
from sqlalchemy import inspect
i = inspect(engine)
for t in ['group_conversations','group_conversation_members','group_messages']:
    assert i.has_table(t), f'missing {t}'
print('all tables present')
"
```
Expected: `all tables present`. Confirm `venv/bin/python -m alembic heads` now shows `gc1_group_chat_tables (head)`.

- [ ] **Step 3: Verify downgrade is reversible, then re-upgrade**

Run:
```bash
venv/bin/python -m alembic downgrade -1
venv/bin/python -m alembic upgrade head
```
Expected: both succeed with no error.

- [ ] **Step 4: Commit**

```bash
git add alembic/versions/gc1_group_chat_tables.py
git commit -m "feat(chat): migration for group chat tables"
```

---

### Task 3: Membership enumeration + provisioning

**Files:**
- Create: `src/messages/group_membership.py`
- Test: `tests/test_group_chat_membership.py`

**Interfaces:**
- Produces:
  - `class_member_ids(db, group) -> set[int]`
  - `parent_member_ids(db, group) -> set[int]`
  - `ensure_group_conversations(db, group) -> dict[str, GroupConversation]` (idempotent; creates class+parents conversations and syncs members; returns `{'class': conv, 'parents': conv}`)
  - `sync_group_conversation_members(db, group_id) -> None` (recompute members for both channels of one group; add missing, remove departed)
  - `provision_all_groups(db) -> int` (call `ensure_group_conversations` for every active group; returns count)

- [ ] **Step 1: Write the failing test**

Create `tests/test_group_chat_membership.py` (reuse the savepoint `db` fixture). Build a group with a student, a parent linked to that student, a teacher, a curator, and a standalone admin; assert membership sets and idempotent provisioning:
```python
import pytest
from src.schemas.models import (
    UserInDB, Group, GroupStudent, ParentStudent, GroupConversationMember,
)
from src.messages.group_membership import (
    class_member_ids, parent_member_ids, ensure_group_conversations,
    sync_group_conversation_members,
)

# (paste the same savepoint `db` fixture as Task 1)

def _u(db, email, role):
    from src.utils.auth_utils import hash_password
    u = UserInDB(email=email, name=email.split("@")[0], role=role,
                 hashed_password=hash_password("x"), is_active=True)
    db.add(u); db.flush(); return u


def _setup(db):
    teacher = _u(db, "gc-t@test.local", "teacher")
    curator = _u(db, "gc-c@test.local", "curator")
    admin = _u(db, "gc-adm@test.local", "admin")
    student = _u(db, "gc-s@test.local", "student")
    parent = _u(db, "gc-p@test.local", "parent")
    g = Group(name="GC", is_active=True, teacher_id=teacher.id, curator_id=curator.id)
    db.add(g); db.flush()
    db.add(GroupStudent(group_id=g.id, student_id=student.id))
    db.add(ParentStudent(parent_id=parent.id, student_id=student.id))
    db.flush()
    return dict(g=g, teacher=teacher, curator=curator, admin=admin, student=student, parent=parent)


def test_class_members_are_students_and_staff_and_admin(db):
    s = _setup(db)
    ids = class_member_ids(db, s["g"])
    assert s["student"].id in ids and s["teacher"].id in ids
    assert s["curator"].id in ids and s["admin"].id in ids
    assert s["parent"].id not in ids


def test_parent_members_are_parents_and_staff_and_admin(db):
    s = _setup(db)
    ids = parent_member_ids(db, s["g"])
    assert s["parent"].id in ids and s["teacher"].id in ids and s["admin"].id in ids
    assert s["student"].id not in ids


def test_ensure_is_idempotent(db):
    s = _setup(db)
    ensure_group_conversations(db, s["g"]); db.flush()
    ensure_group_conversations(db, s["g"]); db.flush()
    # exactly two conversations, and the student is in exactly one (class)
    from src.schemas.models import GroupConversation
    convs = db.query(GroupConversation).filter_by(group_id=s["g"].id).all()
    assert {c.kind for c in convs} == {"class", "parents"}
    assert len(convs) == 2
    memberships = db.query(GroupConversationMember).filter_by(user_id=s["student"].id).count()
    assert memberships == 1


def test_sync_removes_departed_student(db):
    s = _setup(db)
    ensure_group_conversations(db, s["g"]); db.flush()
    db.query(GroupStudent).filter_by(group_id=s["g"].id, student_id=s["student"].id).delete()
    db.flush()
    sync_group_conversation_members(db, s["g"].id); db.flush()
    assert db.query(GroupConversationMember).filter_by(user_id=s["student"].id).count() == 0
```

- [ ] **Step 2: Run it to verify it fails**

Run: `venv/bin/python -m pytest tests/test_group_chat_membership.py -v`
Expected: FAIL (ImportError on `group_membership`).

- [ ] **Step 3: Implement the membership module**

Create `src/messages/group_membership.py`:
```python
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


def provision_all_groups(db: Session) -> int:
    groups = db.query(Group).filter(Group.is_active == True).all()  # noqa: E712
    for g in groups:
        ensure_group_conversations(db, g)
    db.commit()
    return len(groups)
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `venv/bin/python -m pytest tests/test_group_chat_membership.py -v`
Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add src/messages/group_membership.py tests/test_group_chat_membership.py
git commit -m "feat(chat): group channel membership enumeration and provisioning"
```

---

### Task 4: Group-chat service layer (all business rules)

**Files:**
- Create: `src/messages/group_service.py`
- Test: `tests/test_group_chat_service.py`

**Interfaces:**
- Consumes: models (Task 1), membership (Task 3), `send_group_message_push` is added in Task 6 — the service takes an optional `on_message` callback (default `None`) so it does not depend on socket/push yet.
- Produces:
  - `list_conversations(db, user_id) -> list[dict]` — `{id, group_id, kind, title, last_message, unread_count}` for the user's conversations.
  - `get_messages(db, user_id, conversation_id, limit=50, before_id=None) -> list[dict]` — raises `PermissionError` if not a member.
  - `post_message(db, user_id, conversation_id, content, file_url=None) -> dict` — enforces membership + posting rule; creates `GroupMessage`; bumps sender `last_read_at`; returns the message dict. Raises `PermissionError` (not a member) or `ValueError` (empty / not allowed to post).
  - `mark_read(db, user_id, conversation_id) -> None`.
  - `_message_dict(msg, sender) -> dict` and `_can_post(db, member_role, group) -> bool` helpers.

- [ ] **Step 1: Write the failing test**

Create `tests/test_group_chat_service.py` (savepoint fixture + the `_setup`/`_u` helpers from Task 3, plus provisioning). Cover: member can post & read; non-member get_messages raises; student in a `is_special` group cannot post; unread count reflects `last_read_at`.
```python
import pytest
from src.schemas.models import UserInDB, Group, GroupStudent, ParentStudent, GroupConversation
from src.messages.group_membership import ensure_group_conversations
from src.messages import group_service

# (savepoint `db` fixture + _u + _setup as before; _setup returns dict incl 'g')

def _conv(db, group_id, kind):
    return db.query(GroupConversation).filter_by(group_id=group_id, kind=kind).first()


def test_member_can_post_and_read(db):
    s = _setup(db); ensure_group_conversations(db, s["g"]); db.flush()
    conv = _conv(db, s["g"].id, "class")
    msg = group_service.post_message(db, s["student"].id, conv.id, "hi"); db.flush()
    assert msg["content"] == "hi" and msg["from_user_id"] == s["student"].id
    msgs = group_service.get_messages(db, s["teacher"].id, conv.id)
    assert any(m["id"] == msg["id"] for m in msgs)


def test_non_member_cannot_read(db):
    s = _setup(db); ensure_group_conversations(db, s["g"]); db.flush()
    conv = _conv(db, s["g"].id, "parents")  # student is NOT in parents channel
    with pytest.raises(PermissionError):
        group_service.get_messages(db, s["student"].id, conv.id)


def test_student_in_special_group_cannot_post(db):
    s = _setup(db)
    s["g"].is_special = True; db.flush()
    ensure_group_conversations(db, s["g"]); db.flush()
    conv = _conv(db, s["g"].id, "class")
    with pytest.raises(ValueError):
        group_service.post_message(db, s["student"].id, conv.id, "hi")
    # staff can still post
    assert group_service.post_message(db, s["teacher"].id, conv.id, "ok")["content"] == "ok"


def test_unread_count_uses_last_read(db):
    s = _setup(db); ensure_group_conversations(db, s["g"]); db.flush()
    conv = _conv(db, s["g"].id, "class")
    group_service.post_message(db, s["teacher"].id, conv.id, "m1"); db.flush()
    convs = {c["id"]: c for c in group_service.list_conversations(db, s["student"].id)}
    assert convs[conv.id]["unread_count"] == 1
    group_service.mark_read(db, s["student"].id, conv.id); db.flush()
    convs = {c["id"]: c for c in group_service.list_conversations(db, s["student"].id)}
    assert convs[conv.id]["unread_count"] == 0
```

- [ ] **Step 2: Run it to verify it fails**

Run: `venv/bin/python -m pytest tests/test_group_chat_service.py -v`
Expected: FAIL (ImportError on `group_service`).

- [ ] **Step 3: Implement the service**

Create `src/messages/group_service.py`:
```python
"""Business logic for group chat: shared by the REST router and socket handlers."""
from datetime import datetime, timezone
from sqlalchemy.orm import Session

from src.schemas.models import (
    UserInDB, Group, GroupConversation, GroupConversationMember, GroupMessage,
)


def _membership(db: Session, user_id: int, conversation_id: int):
    return (db.query(GroupConversationMember)
              .filter_by(conversation_id=conversation_id, user_id=user_id).first())


def _require_member(db: Session, user_id: int, conversation_id: int) -> GroupConversationMember:
    m = _membership(db, user_id, conversation_id)
    if m is None:
        raise PermissionError("Not a member of this conversation")
    return m


def _can_post(db: Session, user: UserInDB, conv: GroupConversation) -> bool:
    # Special-group students are read-only in group channels (mirrors the 1:1 is_special rule).
    if user.role == "student":
        group = db.query(Group).filter_by(id=conv.group_id).first()
        if group is not None and group.is_special:
            return False
    return True


def _message_dict(msg: GroupMessage, sender: UserInDB) -> dict:
    return {
        "id": msg.id,
        "conversation_id": msg.conversation_id,
        "from_user_id": msg.from_user_id,
        "sender_name": sender.name if sender else "Unknown",
        "content": msg.content,
        "file_url": msg.file_url,
        "created_at": msg.created_at.isoformat() if msg.created_at else None,
    }


def _title(db: Session, conv: GroupConversation) -> str:
    group = db.query(Group).filter_by(id=conv.group_id).first()
    gname = group.name if group else f"Group {conv.group_id}"
    return f"{gname} · {'Parents' if conv.kind == 'parents' else 'Class'}"


def list_conversations(db: Session, user_id: int) -> list:
    members = db.query(GroupConversationMember).filter_by(user_id=user_id).all()
    out = []
    for m in members:
        conv = db.query(GroupConversation).filter_by(id=m.conversation_id).first()
        if conv is None:
            continue
        last = (db.query(GroupMessage).filter_by(conversation_id=conv.id)
                  .order_by(GroupMessage.created_at.desc()).first())
        unread_q = db.query(GroupMessage).filter(GroupMessage.conversation_id == conv.id,
                                                 GroupMessage.from_user_id != user_id)
        if m.last_read_at is not None:
            unread_q = unread_q.filter(GroupMessage.created_at > m.last_read_at)
        sender = db.query(UserInDB).filter_by(id=last.from_user_id).first() if last else None
        out.append({
            "id": conv.id,
            "group_id": conv.group_id,
            "kind": conv.kind,
            "title": _title(db, conv),
            "last_message": _message_dict(last, sender) if last else None,
            "unread_count": unread_q.count(),
        })
    out.sort(key=lambda c: (c["last_message"] or {}).get("created_at") or "", reverse=True)
    return out


def get_messages(db: Session, user_id: int, conversation_id: int,
                 limit: int = 50, before_id: int = None) -> list:
    _require_member(db, user_id, conversation_id)
    q = db.query(GroupMessage).filter_by(conversation_id=conversation_id)
    if before_id:
        q = q.filter(GroupMessage.id < before_id)
    rows = q.order_by(GroupMessage.id.desc()).limit(limit).all()
    rows.reverse()
    sender_ids = {r.from_user_id for r in rows}
    senders = {u.id: u for u in db.query(UserInDB).filter(UserInDB.id.in_(sender_ids)).all()} if sender_ids else {}
    return [_message_dict(r, senders.get(r.from_user_id)) for r in rows]


def post_message(db: Session, user_id: int, conversation_id: int,
                 content: str, file_url: str = None) -> dict:
    member = _require_member(db, user_id, conversation_id)
    conv = db.query(GroupConversation).filter_by(id=conversation_id).first()
    user = db.query(UserInDB).filter_by(id=user_id).first()
    content = (content or "").strip()
    if not content and not file_url:
        raise ValueError("Message must have content or an attachment")
    if not _can_post(db, user, conv):
        raise ValueError("You do not have permission to post in this conversation")
    msg = GroupMessage(conversation_id=conversation_id, from_user_id=user_id,
                       content=content, file_url=file_url)
    db.add(msg)
    member.last_read_at = datetime.now(timezone.utc)
    db.commit(); db.refresh(msg)
    return _message_dict(msg, user)


def mark_read(db: Session, user_id: int, conversation_id: int) -> None:
    member = _require_member(db, user_id, conversation_id)
    member.last_read_at = datetime.now(timezone.utc)
    db.commit()


def member_ids(db: Session, conversation_id: int) -> list:
    rows = db.query(GroupConversationMember.user_id).filter_by(conversation_id=conversation_id)
    return [r[0] for r in rows]
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `venv/bin/python -m pytest tests/test_group_chat_service.py -v`
Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add src/messages/group_service.py tests/test_group_chat_service.py
git commit -m "feat(chat): group chat service layer (list/get/post/read + posting rules)"
```

---

### Task 5: REST router + schemas

**Files:**
- Create: `src/messages/group_schemas.py`
- Create: `src/messages/routes/group_messages.py`
- Modify: `src/messages/routes/__init__.py` (export `group_messages_router`)
- Modify: `src/routes/__init__.py` (include it under `/messages`)
- Test: `tests/test_group_chat_api.py`

**Interfaces:**
- Consumes: `group_service` (Task 4); `get_current_user_dependency` from `src.routes.auth`; `get_db` from `src.config` (match the imports `src/messages/routes/messages.py` uses — verify before writing).
- Produces: `GET /messages/groups`, `GET /messages/groups/{conversation_id}`, `POST /messages/groups/{conversation_id}`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_group_chat_api.py`. Call the route functions directly (as other tests do) with a savepoint `db`, passing `current_user` explicitly:
```python
import pytest
from fastapi import HTTPException
from src.schemas.models import GroupConversation
from src.messages.group_membership import ensure_group_conversations
from src.messages.routes.group_messages import (
    list_group_conversations, get_group_messages, post_group_message, PostGroupMessage,
)
# (savepoint db fixture + _u + _setup as before)

def test_post_and_list_via_router(db):
    s = _setup(db); ensure_group_conversations(db, s["g"]); db.flush()
    conv = db.query(GroupConversation).filter_by(group_id=s["g"].id, kind="class").first()
    post_group_message(conv.id, PostGroupMessage(content="hello"), current_user=s["student"], db=db)
    convs = list_group_conversations(current_user=s["student"], db=db)
    assert any(c["id"] == conv.id and c["unread_count"] == 0 for c in convs)


def test_non_member_get_is_403(db):
    s = _setup(db); ensure_group_conversations(db, s["g"]); db.flush()
    conv = db.query(GroupConversation).filter_by(group_id=s["g"].id, kind="parents").first()
    with pytest.raises(HTTPException) as exc:
        get_group_messages(conv.id, current_user=s["student"], db=db)
    assert exc.value.status_code == 403
```

- [ ] **Step 2: Run it to verify it fails**

Run: `venv/bin/python -m pytest tests/test_group_chat_api.py -v`
Expected: FAIL (ImportError).

- [ ] **Step 3: Implement schemas and router**

Create `src/messages/group_schemas.py`:
```python
from pydantic import BaseModel
from typing import Optional


class PostGroupMessage(BaseModel):
    content: str = ""
    file_url: Optional[str] = None
```

Create `src/messages/routes/group_messages.py` (verify the two imports below match `src/messages/routes/messages.py` before writing):
```python
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from typing import Optional

from src.config import get_db
from src.routes.auth import get_current_user_dependency
from src.schemas.models import UserInDB
from src.messages import group_service
from src.messages.group_schemas import PostGroupMessage

router = APIRouter()


@router.get("/groups")
def list_group_conversations(current_user: UserInDB = Depends(get_current_user_dependency),
                             db: Session = Depends(get_db)):
    return group_service.list_conversations(db, current_user.id)


@router.get("/groups/{conversation_id}")
def get_group_messages(conversation_id: int,
                       limit: int = Query(50, le=100),
                       before_id: Optional[int] = None,
                       current_user: UserInDB = Depends(get_current_user_dependency),
                       db: Session = Depends(get_db)):
    try:
        return group_service.get_messages(db, current_user.id, conversation_id, limit, before_id)
    except PermissionError:
        raise HTTPException(status_code=403, detail="Not a member of this conversation")


@router.post("/groups/{conversation_id}")
def post_group_message(conversation_id: int,
                       payload: PostGroupMessage,
                       current_user: UserInDB = Depends(get_current_user_dependency),
                       db: Session = Depends(get_db)):
    try:
        msg = group_service.post_message(db, current_user.id, conversation_id,
                                         payload.content, payload.file_url)
    except PermissionError:
        raise HTTPException(status_code=403, detail="Not a member of this conversation")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    # Fan-out (socket + push) is wired in Task 6 via group_service hooks; REST returns the message.
    return msg
```

- [ ] **Step 4: Mount the router**

In `src/messages/routes/__init__.py`, export the new router (match the existing export style), e.g.:
```python
from src.messages.routes.group_messages import router as group_messages_router
```
In `src/routes/__init__.py`, alongside the existing messages include (line ~35), add:
```python
app.include_router(group_messages_router, prefix="/messages", tags=["Group Chat"])
```
(and add `group_messages_router` to the import from `src.messages.routes`). Confirm the paths resolve to `/messages/groups`.

- [ ] **Step 5: Run the test to verify it passes**

Run: `venv/bin/python -m pytest tests/test_group_chat_api.py -v`
Expected: 2 passed.

- [ ] **Step 6: Commit**

```bash
git add src/messages/group_schemas.py src/messages/routes/group_messages.py src/messages/routes/__init__.py src/routes/__init__.py tests/test_group_chat_api.py
git commit -m "feat(chat): REST endpoints for group conversations"
```

---

### Task 6: Socket events + push fan-out

**Files:**
- Modify: `src/utils/push_notifications.py` (add `send_group_message_push`)
- Modify: `src/messages/routes/socket_messages.py` (add `group:*` events + auto-join rooms)
- Test: `tests/test_group_chat_push.py`

**Interfaces:**
- Consumes: `group_service` (Task 4), `group_service.member_ids`.
- Produces: `send_group_message_push(db, *, member_ids, sender_name, conversation_id, title, message_preview)`; socket events `group:threads:get`, `group:messages:get`, `group:message:send` → `group:message:new`, `group:read`.

- [ ] **Step 1: Write the failing test (push helper — the socket handlers are thin wrappers verified manually)**

Create `tests/test_group_chat_push.py`. Test the fan-out helper builds one Expo message per active token across members except the sender, with `data.type == "group_message"`. Mirror the style of any existing push test; if none, monkeypatch `requests.post`:
```python
import pytest
from src.schemas.models import UserInDB, UserPushToken
from src.utils import push_notifications
# (savepoint db fixture + _u)

def test_group_push_targets_members_except_sender(db, monkeypatch):
    sender = _u(db, "gp-s@test.local", "teacher")
    m1 = _u(db, "gp-1@test.local", "student")
    m2 = _u(db, "gp-2@test.local", "student")
    for u in (m1, m2):
        db.add(UserPushToken(user_id=u.id, token=f"ExponentPushToken[{u.id}]", is_active=True, platform="ios"))
    db.flush()
    captured = {}
    class _Resp:
        status_code = 200
        def json(self): return {"data": [{"status": "ok"}, {"status": "ok"}]}
    def _fake_post(url, json=None, headers=None, timeout=None):
        captured["messages"] = json; return _Resp()
    monkeypatch.setattr(push_notifications.requests, "post", _fake_post)

    push_notifications.send_group_message_push(
        db, member_ids=[m1.id, m2.id, sender.id], sender_name="Teacher",
        conversation_id=42, title="GC · Class", message_preview="hello", sender_id=sender.id,
    )
    tos = {m["to"] for m in captured["messages"]}
    assert tos == {f"ExponentPushToken[{m1.id}]", f"ExponentPushToken[{m2.id}]"}
    assert all(m["data"]["type"] == "group_message" and m["data"]["conversationId"] == 42
               for m in captured["messages"])
```

- [ ] **Step 2: Run it to verify it fails**

Run: `venv/bin/python -m pytest tests/test_group_chat_push.py -v`
Expected: FAIL (`send_group_message_push` undefined).

- [ ] **Step 3: Implement the push helper**

In `src/utils/push_notifications.py`, add (mirroring `send_message_push_to_user`, but for many recipients and excluding the sender):
```python
def send_group_message_push(db, *, member_ids, sender_name, conversation_id,
                            title, message_preview, sender_id) -> int:
    """Fan a group-chat push out to all members' active devices except the sender's."""
    from src.auth.models import UserPushToken
    try:
        recipient_ids = [i for i in member_ids if i != sender_id]
        if not recipient_ids:
            return 0
        rows = db.query(UserPushToken).filter(
            UserPushToken.user_id.in_(recipient_ids),
            UserPushToken.is_active == True,  # noqa: E712
        ).all()
        valid = [r.token for r in rows if r.token and r.token.startswith("ExponentPushToken[")]
        if not valid:
            return 0
        preview = message_preview if len(message_preview) <= 100 else message_preview[:97] + "..."
        messages = [{
            "to": t, "title": title, "body": f"{sender_name}: {preview}",
            "sound": "default", "priority": "high", "badge": 1,
            "data": {"type": "group_message", "conversationId": conversation_id, "title": title},
        } for t in valid]
        response = requests.post(EXPO_PUSH_ENDPOINT, json=messages,
                                 headers={"Accept": "application/json", "Content-Type": "application/json"},
                                 timeout=30)
        if response.status_code != 200:
            logger.error(f"Group push failed: {response.status_code} - {response.text[:200]}")
            return 0
        results = response.json().get("data", [])
        by_token = {r.token: r for r in rows}
        accepted = 0; deactivated = False
        for token, result in zip(valid, results):
            if result.get("status") == "ok":
                accepted += 1; continue
            if (result.get("details") or {}).get("error") == "DeviceNotRegistered":
                row = by_token.get(token)
                if row is not None:
                    row.is_active = False; deactivated = True
        if deactivated:
            db.commit()
        return accepted
    except Exception as e:
        logger.error(f"send_group_message_push failed for conv {conversation_id}: {e}")
        try: db.rollback()
        except Exception: pass
        return 0
```

- [ ] **Step 4: Run the push test to verify it passes**

Run: `venv/bin/python -m pytest tests/test_group_chat_push.py -v`
Expected: 1 passed.

- [ ] **Step 5: Add socket handlers + auto-join (manual verification)**

In `src/messages/routes/socket_messages.py`:
- Add near `USER_ROOM_PREFIX`: `GROUP_ROOM_PREFIX = "group:"`.
- In `connect`, after joining the user room, auto-join the user's group rooms:
```python
    # Auto-join group-chat rooms for this user
    try:
        _gdb = next(get_db())
        try:
            from src.messages.group_service import list_conversations
            for c in list_conversations(_gdb, user_id):
                await sio.enter_room(sid, f"{GROUP_ROOM_PREFIX}{c['id']}")
        finally:
            _gdb.close()
    except Exception as e:
        logger.error(f"group auto-join failed for {user_id}: {e}")
```
- Add handlers mirroring the 1:1 ones (use `group_service` for all logic):
```python
@sio.on('group:threads:get')
async def handle_group_threads_get(sid):
    session = await sio.get_session(sid); db = next(get_db())
    try:
        from src.messages.group_service import list_conversations
        uid = _resolve_user_id(session, db)
        await sio.emit('group:threads', list_conversations(db, uid), to=sid)
    finally:
        db.close()


@sio.on('group:messages:get')
async def handle_group_messages_get(sid, data):
    session = await sio.get_session(sid); db = next(get_db())
    try:
        from src.messages.group_service import get_messages
        uid = _resolve_user_id(session, db)
        conv_id = int(data.get('conversation_id'))
        try:
            msgs = get_messages(db, uid, conv_id, int(data.get('limit') or 50), data.get('before_id'))
        except PermissionError:
            await sio.emit('message:error', {'detail': 'Access denied'}, to=sid); return
        await sio.emit('group:messages', {'conversation_id': conv_id, 'messages': msgs}, to=sid)
    finally:
        db.close()


@sio.on('group:message:send')
async def handle_group_message_send(sid, data):
    session = await sio.get_session(sid); db = next(get_db())
    try:
        from src.messages.group_service import post_message, member_ids
        from src.utils.push_notifications import send_group_message_push
        uid = _resolve_user_id(session, db)
        conv_id = int(data.get('conversation_id'))
        try:
            msg = post_message(db, uid, conv_id, data.get('content') or '', data.get('file_url'))
        except PermissionError:
            await sio.emit('message:error', {'detail': 'Access denied'}, to=sid); return
        except ValueError as e:
            await sio.emit('message:error', {'detail': str(e)}, to=sid); return
        await sio.emit('group:message:new', msg, to=f"{GROUP_ROOM_PREFIX}{conv_id}")
        members = member_ids(db, conv_id)
        for mid in members:
            await sio.emit('group:threads:update', to=f"{USER_ROOM_PREFIX}{mid}")
        send_group_message_push(db, member_ids=members, sender_name=msg['sender_name'],
                                conversation_id=conv_id, title=msg.get('title') or 'Group chat',
                                message_preview=msg['content'] or '📎 Attachment', sender_id=uid)
    except Exception as e:
        logger.error(f"group send error: {e}")
        await sio.emit('message:error', {'detail': 'Internal server error'}, to=sid)
    finally:
        db.close()


@sio.on('group:read')
async def handle_group_read(sid, data):
    session = await sio.get_session(sid); db = next(get_db())
    try:
        from src.messages.group_service import mark_read
        uid = _resolve_user_id(session, db)
        try:
            mark_read(db, uid, int(data.get('conversation_id')))
        except PermissionError:
            return
        await sio.emit('group:unread:update', to=f"{USER_ROOM_PREFIX}{uid}")
    finally:
        db.close()
```
Note: `post_message` returns a dict without `title`; include the title by fetching it or adding it in the emit. To keep the client simple, add `title` to the `post_message` return (extend `_message_dict` call site) OR compute it here via `group_service._title`. Pick one and keep the socket + REST payloads identical.

- [ ] **Step 6: Verify import health and run the full group-chat suite**

Run:
```bash
venv/bin/python -c "import src.app; print('app imports ok')"
venv/bin/python -m pytest tests/test_group_chat_*.py -v
```
Expected: app imports cleanly; all group-chat tests pass.

- [ ] **Step 7: Commit**

```bash
git add src/utils/push_notifications.py src/messages/routes/socket_messages.py tests/test_group_chat_push.py
git commit -m "feat(chat): socket events and push fan-out for group chat"
```

---

### Task 7: Provisioning entrypoint + membership-sync hooks

**Files:**
- Create: `src/scripts/provision_group_chats.py` (one-off idempotent provisioning)
- Modify: the admin group-mutation endpoints to call `sync_group_conversation_members(db, group_id)` after committing membership/staff changes.
- Test: `tests/test_group_chat_sync_hooks.py`

**Interfaces:**
- Consumes: `provision_all_groups`, `sync_group_conversation_members` (Task 3).

- [ ] **Step 1: Write the provisioning script**

Create `src/scripts/provision_group_chats.py`:
```python
"""Idempotently provision class + parents chat channels for every active group.

Usage: venv/bin/python -m src.scripts.provision_group_chats
"""
from src.config import SessionLocal
from src.messages.group_membership import provision_all_groups


def main():
    db = SessionLocal()
    try:
        n = provision_all_groups(db)
        print(f"Provisioned/synced group chats for {n} active groups")
    finally:
        db.close()


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Identify and wire the sync hooks**

Find the admin endpoints that change group membership/staff and add a `sync_group_conversation_members(db, group_id)` call after each commits. Grep to locate them:
```bash
grep -rn "GroupStudent(" src/admin/routes/ | grep -i add
grep -rn "def .*group" src/admin/routes/admin.py | grep -iE "student|teacher|curator|add|remove"
```
Representative targets (verify exact function names before editing):
- add-student-to-group and remove-student-from-group handlers in `src/admin/routes/admin.py`.
- the group update handler that sets `teacher_id`/`curator_id`.
Add, after the existing `db.commit()` in each:
```python
from src.messages.group_membership import sync_group_conversation_members
sync_group_conversation_members(db, group_id)
db.commit()
```

- [ ] **Step 3: Write a test that the hook keeps membership correct**

Create `tests/test_group_chat_sync_hooks.py` — this can test `sync_group_conversation_members` directly against a group where you add a new student after initial provisioning (the endpoint wiring is verified manually):
```python
import pytest
from src.schemas.models import GroupStudent, GroupConversation, GroupConversationMember
from src.messages.group_membership import ensure_group_conversations, sync_group_conversation_members
# (savepoint fixture + _u + _setup)

def test_new_student_added_after_provision_gets_synced_in(db):
    s = _setup(db); ensure_group_conversations(db, s["g"]); db.flush()
    newstud = _u(db, "gc-new@test.local", "student")
    db.add(GroupStudent(group_id=s["g"].id, student_id=newstud.id)); db.flush()
    sync_group_conversation_members(db, s["g"].id); db.flush()
    class_conv = db.query(GroupConversation).filter_by(group_id=s["g"].id, kind="class").first()
    assert db.query(GroupConversationMember).filter_by(
        conversation_id=class_conv.id, user_id=newstud.id).count() == 1
```

- [ ] **Step 4: Run tests**

Run: `venv/bin/python -m pytest tests/test_group_chat_sync_hooks.py -v`
Expected: 1 passed.

- [ ] **Step 5: Provision the dev DB and sanity-check**

Run:
```bash
venv/bin/python -m src.scripts.provision_group_chats
```
Expected: prints a provisioned-count line without error.

- [ ] **Step 6: Commit**

```bash
git add src/scripts/provision_group_chats.py src/admin/routes/admin.py tests/test_group_chat_sync_hooks.py
git commit -m "feat(chat): provisioning script and membership-sync hooks"
```

---

## Self-Review

**Spec coverage** (against §Track 2 of `docs/superpowers/specs/2026-07-21-store-readiness-and-group-chat-design.md`, backend portions):
- Data model (2.1) → Task 1 + Task 2. ✅
- Membership rules (2.2) incl. is_special posting → Task 3 (enumeration) + Task 4 (`_can_post`). ✅
- Provisioning + sync (2.3) → Task 3 functions, Task 7 script + hooks. ✅
- REST API (2.4) → Task 5. ✅
- Socket events (2.5) → Task 6. ✅
- Push (2.6) → Task 6 `send_group_message_push`. ✅
- Mobile (2.7) / Web (2.8) → separate plans (out of scope here).

**Placeholder scan:** the only "verify before writing" notes (Task 5 import lines, Task 7 endpoint names) point the implementer at concrete grep commands — not deferred work. All code steps carry full code.

**Type consistency:** `post_message`/`get_messages`/`list_conversations`/`mark_read`/`member_ids` signatures match between the service (Task 4), the router (Task 5), and the socket handlers (Task 6). `send_group_message_push(db, *, member_ids, sender_name, conversation_id, title, message_preview, sender_id)` matches its test (Task 6) and its socket call site.

## Verification (end-to-end)

- `venv/bin/python -m pytest tests/test_group_chat_*.py -v` → all pass.
- `venv/bin/python -m alembic upgrade head` applies cleanly; `import src.app` succeeds.
- `venv/bin/python -m src.scripts.provision_group_chats` provisions the dev DB.
- Manual socket smoke test (after mobile/web client exists, or via a socket.io test client): two users in the same group's class channel — `group:message:send` from one emits `group:message:new` to the `group:{id}` room and a push to offline members; a user in the parents channel does not receive class-channel messages.
- Next: write the mobile and web group-chat plans against these endpoints/events.
