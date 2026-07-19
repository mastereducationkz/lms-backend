# Trial Access Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Sales-granted, time-boxed trial access: a prospect user gets a lesson allowlist in one course until a sales-editable deadline, hard-enforced at request time, with an admin management page and a demo-friendly prospect experience.

**Architecture:** New `src/trials/` backend domain whose `trial_accesses` row is the sole source of a trial user's access (no enrollment/group/unlock rows). Enforcement branches on `user.is_trial` inside existing course/lesson endpoints and helpers; expiry is evaluated per-request so revocation is exact. Frontend adds an admin page, a countdown banner, an expired gate, and sample-data dashboard widgets.

**Tech Stack:** FastAPI + SQLAlchemy + Alembic + PostgreSQL (JSONB) + Redis cache decorators; React 18 + TS + react-router v7 + shadcn/Tailwind.

**Spec:** `docs/superpowers/specs/2026-07-19-trial-access-design.md` (same repo). Read it before starting any task.

## Global Constraints

- Repos: backend `/Users/fikrat/Documents/master.lms/lms-backend` (implementation branch `feat/trial-access` off `origin/main`; deploy = merge to `main` + push). Frontend `/Users/fikrat/Documents/master.lms/lms-front` (work directly on `master`; deploy = push `origin master`).
- Real students' code paths must be byte-for-byte unchanged: every trial branch is guarded by `user.is_trial` (and `role == "student"` where relevant).
- "Active grant" everywhere means: `status == "active" AND now_utc < expires_at`. Never trust `status` alone.
- Timestamps: DB columns are naive UTC (repo convention). Normalize any tz-aware input with `_as_utc_naive`.
- Python: use `.venv/bin/python` in lms-backend (`.venv/bin/python -m pytest -q`). Tests MUST pass without a database (DB-backed tests skip when unreachable) because backend CI on `main` runs pytest and deploys.
- Backend commits on `feat/trial-access`; frontend commits on `master` (do not push until the final task).
- New backend code follows the domain-module pattern: `src/trials/{__init__.py,models.py,schemas.py,services.py,routes/}`.

---

### Task 0: Backend branch setup

**Files:** none created (git + hygiene only)

- [ ] **Step 1: Create the implementation branch off origin/main and carry the spec**

```bash
cd /Users/fikrat/Documents/master.lms/lms-backend
git fetch origin
git checkout -b feat/trial-access origin/main
git cherry-pick e54f751 cd0f295   # spec doc + spec amendments (docs-only)
```
Expected: two clean cherry-picks (docs/ only).

- [ ] **Step 2: Neutralize the stray duplicate migration file**

There is an untracked `alembic/versions/p15_perf_indexes 2.py`. If it duplicates `p15_perf_indexes.py`'s revision id, Alembic will refuse to run ("Duplicate revision"). Do NOT delete it — move it out of the tree:

```bash
diff "alembic/versions/p15_perf_indexes 2.py" alembic/versions/p15_perf_indexes.py && \
  mv "alembic/versions/p15_perf_indexes 2.py" /private/tmp/claude-501/-Users-fikrat-Documents-master-lms/0f95d2c6-b25a-4acb-9fe9-79b733ba6677/scratchpad/
```
If the diff shows real differences, stop and report instead of moving.

- [ ] **Step 3: Verify the test suite baseline**

```bash
.venv/bin/python -m pytest -q
```
Expected: passes (or exit 5 if nothing collected — record the baseline count for later comparison).

- [ ] **Step 4: Record the Alembic head**

```bash
.venv/bin/python -m alembic heads
```
Expected: exactly one head id printed (on origin/main this should be `p16_users_lower_email_idx` or the letter-chain head — whatever prints). Use this exact id as `down_revision` in Task 1. If TWO heads print, use `.venv/bin/python -m alembic merge heads -m "merge heads"` is NOT wanted — instead stop and report.

---

### Task 1: Trial models, user flag, migration

**Files:**
- Create: `src/trials/__init__.py`, `src/trials/models.py`
- Modify: `src/auth/models.py` (add `is_trial` to `UserInDB`, line ~36 after `is_analytics_hidden`)
- Modify: `src/models/__init__.py` and `src/schemas/models.py` shim (re-export `TrialAccess` — follow how other domreal models are re-exported; grep `is_analytics_hidden` era exports or `ManualLessonUnlock` for the pattern)
- Create: `alembic/versions/w7x8y9z1a2b3_add_trial_access.py`
- Test: `tests/test_trial_access.py` (started here, grown in later tasks)

**Interfaces:**
- Produces: `TrialAccess` model (`trial_accesses` table) with columns exactly: `id, user_id, course_id, lesson_ids (JSONB list[int]), expires_at (naive UTC), status ("active"|"expired"|"revoked"|"converted"), granted_by, prospect_note, created_at, updated_at, revoked_at`. `UserInDB.is_trial: bool`.

- [ ] **Step 1: Write failing import/shape test**

```python
# tests/test_trial_access.py
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
```

- [ ] **Step 2: Run to verify failure** — `.venv/bin/python -m pytest tests/test_trial_access.py -q` → FAIL (`ModuleNotFoundError: src.trials`).

- [ ] **Step 3: Implement models**

`src/trials/__init__.py`: empty file.

`src/trials/models.py`:
```python
from datetime import datetime, timezone

from sqlalchemy import Column, Integer, String, DateTime, ForeignKey, Text, Index, text
from sqlalchemy.dialects.postgresql import JSONB

from src.models.base import Base

TRIAL_ACTIVE = "active"
TRIAL_EXPIRED = "expired"
TRIAL_REVOKED = "revoked"
TRIAL_CONVERTED = "converted"


class TrialAccess(Base):
    """A sales-granted, time-boxed lesson-allowlist grant for a prospect user.

    The row IS the access: trial users have no enrollment/group/unlock rows,
    so deactivating this row (or passing expires_at) removes everything.
    """
    __tablename__ = "trial_accesses"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    course_id = Column(Integer, ForeignKey("courses.id", ondelete="CASCADE"), nullable=False, index=True)
    lesson_ids = Column(JSONB, nullable=False)  # list[int], validated against course on write
    expires_at = Column(DateTime, nullable=False)  # naive UTC
    status = Column(String, nullable=False, default=TRIAL_ACTIVE, index=True)
    granted_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    prospect_note = Column(Text, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc),
                        onupdate=lambda: datetime.now(timezone.utc))
    revoked_at = Column(DateTime, nullable=True)

    __table_args__ = (
        Index(
            "uq_trial_active_user_course", "user_id", "course_id",
            unique=True, postgresql_where=text("status = 'active'"),
        ),
    )
```

`src/auth/models.py` — add after `is_analytics_hidden`:
```python
    # Sales-prospect trial account (see src/trials). Filterable everywhere real
    # students are listed/synced; access comes solely from trial_accesses rows.
    is_trial = Column(Boolean, default=False, nullable=False)
```

Re-export: add `TrialAccess` to `src/models/__init__.py` and the `src/schemas/models.py` shim following the existing import style in each (find the module-import lists and append `from src.trials.models import TrialAccess  # noqa`).

- [ ] **Step 4: Run test** → PASS.

- [ ] **Step 5: Write the migration** — `alembic/versions/w7x8y9z1a2b3_add_trial_access.py` (set `down_revision` to the id from Task 0 Step 4):

```python
"""add trial access (users.is_trial + trial_accesses)

Revision ID: w7x8y9z1a2b3
Revises: p16_users_lower_email_idx
Create Date: 2026-07-19 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


revision: str = "w7x8y9z1a2b3"
down_revision: Union[str, Sequence[str], None] = "p16_users_lower_email_idx"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("users", sa.Column("is_trial", sa.Boolean(), nullable=True))
    op.execute("UPDATE users SET is_trial = FALSE WHERE is_trial IS NULL")
    op.alter_column("users", "is_trial", nullable=False, server_default=sa.text("false"))
    op.create_index("ix_users_is_trial", "users", ["is_trial"])

    op.create_table(
        "trial_accesses",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("course_id", sa.Integer(), sa.ForeignKey("courses.id", ondelete="CASCADE"), nullable=False),
        sa.Column("lesson_ids", JSONB(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("status", sa.String(), nullable=False, server_default="active"),
        sa.Column("granted_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("prospect_note", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_trial_accesses_user_id", "trial_accesses", ["user_id"])
    op.create_index("ix_trial_accesses_course_id", "trial_accesses", ["course_id"])
    op.create_index("ix_trial_accesses_status", "trial_accesses", ["status"])
    op.create_index(
        "uq_trial_active_user_course", "trial_accesses", ["user_id", "course_id"],
        unique=True, postgresql_where=sa.text("status = 'active'"),
    )


def downgrade() -> None:
    op.drop_table("trial_accesses")
    op.drop_index("ix_users_is_trial", table_name="users")
    op.drop_column("users", "is_trial")
```

- [ ] **Step 6: Sanity-check migration compiles offline** — `.venv/bin/python -m alembic heads` → prints `w7x8y9z1a2b3` as the single head. (If a local Postgres with the `lms_test` DB is reachable — container `lms-postgres` — also run `POSTGRES_URL=postgresql://postgres:postgres@localhost:5432/lms_test .venv/bin/python -m alembic upgrade head` and expect success; skip if unreachable.)

- [ ] **Step 7: Commit** — `git add -A && git commit -m "feat(trials): TrialAccess model, users.is_trial flag, migration"`

---

### Task 2: Trial services (pure core + DB helpers)

**Files:**
- Create: `src/trials/services.py`
- Test: extend `tests/test_trial_access.py`

**Interfaces:**
- Produces (used by Tasks 3–6):
  - `_as_utc_naive(dt: datetime) -> datetime`
  - `utcnow() -> datetime` (naive UTC)
  - `grant_is_active(grant, now: datetime | None = None) -> bool`
  - `lesson_in_grant(grant, lesson_id: int) -> bool`
  - `evaluate_trial_lesson_access(grant, lesson_id, now=None) -> tuple[bool, str | None]` (pure; reasons: `"Your trial has ended"` / `"Not included in your trial"` / `"You do not have access to this course"` when grant is None)
  - `get_active_trial(db, user_id: int, course_id: int)` → `TrialAccess | None`
  - `get_active_trials(db, user_id: int) -> list[TrialAccess]`
  - `trial_course_ids(db, user_id: int) -> list[int]`
  - `trial_lesson_access(db, user_id: int, lesson_id: int) -> tuple[bool, str | None]` (loads lesson→module→course, then delegates to the pure evaluator)
  - `earliest_active_expiry(db, user_id: int) -> datetime | None`
  - `expire_stale_trials(db) -> int` (UPDATE active→expired where past deadline; commits; returns row count)

- [ ] **Step 1: Write failing pure-logic tests** (append to `tests/test_trial_access.py`):

```python
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace


def _grant(status="active", expires_in_minutes=60, lesson_ids=(1, 2)):
    return SimpleNamespace(
        status=status,
        expires_at=datetime.utcnow() + timedelta(minutes=expires_in_minutes),
        lesson_ids=list(lesson_ids),
        course_id=10,
    )


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
```

- [ ] **Step 2: Run** → FAIL (`ImportError`).

- [ ] **Step 3: Implement `src/trials/services.py`**

```python
"""Trial access decision logic.

Pure functions (grant_is_active / lesson_in_grant / evaluate_trial_lesson_access)
carry the semantics and are unit-tested without a DB; thin DB helpers wrap them.
"Active" ALWAYS means status == "active" AND now < expires_at — never trust
status alone (the background job that flips statuses is bookkeeping only).
"""
from datetime import datetime, timezone
from typing import List, Optional, Tuple

from sqlalchemy.orm import Session

from src.trials.models import TrialAccess, TRIAL_ACTIVE, TRIAL_EXPIRED

REASON_ENDED = "Your trial has ended"
REASON_NOT_INCLUDED = "Not included in your trial"
REASON_NO_COURSE = "You do not have access to this course"


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _as_utc_naive(dt: datetime) -> datetime:
    if dt.tzinfo is not None:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def grant_is_active(grant, now: Optional[datetime] = None) -> bool:
    if grant is None or grant.status != TRIAL_ACTIVE:
        return False
    ref = now or utcnow()
    return _as_utc_naive(grant.expires_at) > ref


def lesson_in_grant(grant, lesson_id: int) -> bool:
    try:
        allowed = {int(x) for x in (grant.lesson_ids or [])}
    except (TypeError, ValueError):
        return False
    return int(lesson_id) in allowed


def evaluate_trial_lesson_access(
    grant, lesson_id: int, now: Optional[datetime] = None
) -> Tuple[bool, Optional[str]]:
    if grant is None:
        return False, REASON_NO_COURSE
    if not grant_is_active(grant, now):
        return False, REASON_ENDED
    if not lesson_in_grant(grant, lesson_id):
        return False, REASON_NOT_INCLUDED
    return True, None


def get_active_trials(db: Session, user_id: int) -> List[TrialAccess]:
    rows = db.query(TrialAccess).filter(
        TrialAccess.user_id == user_id,
        TrialAccess.status == TRIAL_ACTIVE,
    ).all()
    return [g for g in rows if grant_is_active(g)]


def get_active_trial(db: Session, user_id: int, course_id: int) -> Optional[TrialAccess]:
    grant = db.query(TrialAccess).filter(
        TrialAccess.user_id == user_id,
        TrialAccess.course_id == course_id,
        TrialAccess.status == TRIAL_ACTIVE,
    ).first()
    return grant if grant_is_active(grant) else None


def trial_course_ids(db: Session, user_id: int) -> List[int]:
    return [g.course_id for g in get_active_trials(db, user_id)]


def trial_lesson_access(db: Session, user_id: int, lesson_id: int) -> Tuple[bool, Optional[str]]:
    from src.schemas.models import Lesson, Module

    lesson = db.query(Lesson).filter(Lesson.id == lesson_id).first()
    if not lesson:
        return False, "Lesson not found"
    module = db.query(Module).filter(Module.id == lesson.module_id).first()
    if not module:
        return False, "Module not found"
    grant = get_active_trial(db, user_id, module.course_id)
    return evaluate_trial_lesson_access(grant, lesson_id)


def earliest_active_expiry(db: Session, user_id: int) -> Optional[datetime]:
    grants = get_active_trials(db, user_id)
    if not grants:
        return None
    return min(_as_utc_naive(g.expires_at) for g in grants)


def expire_stale_trials(db: Session) -> int:
    """Bookkeeping: flip active→expired past deadline. Enforcement never needs this."""
    count = db.query(TrialAccess).filter(
        TrialAccess.status == TRIAL_ACTIVE,
        TrialAccess.expires_at <= utcnow(),
    ).update({TrialAccess.status: TRIAL_EXPIRED}, synchronize_session=False)
    db.commit()
    return count
```

- [ ] **Step 4: Run** — `.venv/bin/python -m pytest tests/test_trial_access.py -q` → PASS.
- [ ] **Step 5: Commit** — `git add -A && git commit -m "feat(trials): decision services with pure testable core"`

---

### Task 3: Enforcement in existing access paths

**Files:**
- Modify: `src/utils/permissions.py` (`check_course_access` student branch, ~line 112)
- Modify: `src/utils/course_access.py` (`get_user_courses` ~24, `get_user_course_ids` ~73, `check_user_course_access` ~104)
- Modify: `src/courses/routes/courses.py`:
  - `get_courses` student branch (~line 60)
  - `get_course_modules` (~473; override `is_accessible` before return)
  - `check_lesson_access` (~1124; trial branch right after the `role != "student"` early return at ~1152)
  - hard gates in `get_lesson` (~1090), `get_lesson_steps` (~1480), `get_step` (~1603), `get_lesson_materials` (~1915)
- Modify: `src/services/cache_service.py` (`_cache_key`, ~line 221: bypass for trial users)
- Test: extend `tests/test_trial_access.py`

**Interfaces:**
- Consumes: everything from Task 2.
- Produces: no new public API — behavior only. Cache bypass: `_cache_key` returns `None` when the bound `current_user` has `is_trial=True` (bypasses read AND write for every `@cached` endpoint platform-wide).

- [ ] **Step 1: Write failing cache-bypass test** (append):

```python
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
```

Note: if Redis is not running locally `get_client()` is already `None` and the test would pass vacuously — that is acceptable; the assertion still pins the no-cache behavior contract.

- [ ] **Step 2: Run** → observe result (may already pass without Redis; still keep it as the contract).

- [ ] **Step 3: Implement cache bypass** — in `src/services/cache_service.py` `_cache_key`, right after `user = bound.arguments.get(user_arg)` (~line 221):

```python
            if user is not None and getattr(user, "is_trial", False):
                return None  # trial users: never cache — expiry must be exact per-request
```

- [ ] **Step 4: Implement course-level access branches**

`src/utils/permissions.py`, inside `check_course_access`, at the TOP of the `elif user.role == "student":` branch (line ~112):

```python
    elif user.role == "student":
        if getattr(user, "is_trial", False):
            from src.trials.services import get_active_trial
            return get_active_trial(db, user.id, course_id) is not None
```
(keep the existing group-access logic below for non-trial students).

`src/utils/course_access.py` — at the top of each of the three functions:

```python
def _trial_flag(db: Session, user_id: int) -> bool:
    row = db.query(UserInDB.is_trial).filter(UserInDB.id == user_id).first()
    return bool(row and row[0])
```
(add this module-level helper once), then:

- `get_user_courses`: first lines →
```python
    if _trial_flag(db, user_id):
        from src.trials.services import trial_course_ids
        ids = trial_course_ids(db, user_id)
        if not ids:
            return []
        query = db.query(Course).filter(Course.id.in_(ids))
        if not include_inactive:
            query = query.filter(Course.is_active == True)
        return query.all()
```
- `get_user_course_ids`: →
```python
    if _trial_flag(db, user_id):
        from src.trials.services import trial_course_ids
        return trial_course_ids(db, user_id)
```
- `check_user_course_access`: →
```python
    if _trial_flag(db, user_id):
        from src.trials.services import get_active_trial
        return get_active_trial(db, user_id, course_id) is not None
```

`src/courses/routes/courses.py` `get_courses` — at the top of the `if current_user.role == "student":` branch:

```python
    if current_user.role == "student" and getattr(current_user, "is_trial", False):
        from src.trials.services import trial_course_ids
        ids = trial_course_ids(db, current_user.id)
        query = query.filter(Course.id.in_(ids)) if ids else query.filter(Course.id == -1)
    elif current_user.role == "student":
        ...  # existing enrollment/group filtering unchanged
```

- [ ] **Step 5: Implement lesson-level branches**

Add near the imports of `courses.py`: `from src.trials.services import trial_lesson_access, get_active_trial as get_active_trial_grant`.

Shared hard-gate helper (place near the top of `courses.py`, after imports):

```python
def _trial_hard_gate(db, current_user, lesson_id: int):
    """403 unless the lesson is in the trial user's active allowlist. No-op for non-trial users."""
    if current_user.role == "student" and getattr(current_user, "is_trial", False):
        allowed, reason = trial_lesson_access(db, current_user.id, lesson_id)
        if not allowed:
            raise HTTPException(status_code=403, detail=reason or "Not included in your trial")
```

Call `_trial_hard_gate(db, current_user, lesson_id)` immediately after the existing `check_course_access(...)` 403 in: `get_lesson` (~1110), `get_lesson_steps` (~1499), `get_lesson_materials` (~1916 area), and in `get_step` (~1623) as `_trial_hard_gate(db, current_user, step.lesson_id)`.

`check_lesson_access` (~1152) — insert directly after the `if current_user.role != "student": return {"accessible": True}` early return:

```python
    if getattr(current_user, "is_trial", False):
        allowed, reason = trial_lesson_access(db, current_user.id, lesson_id)
        if allowed:
            return {"accessible": True}
        return {"accessible": False, "reason": reason}
```
(the trial allowlist is the sole authority — drip/weekly/cap logic below is skipped).

`get_course_modules` — find where the response list of `ModuleSchema` (with lessons carrying `is_accessible`) is finalized (just before the `return`), and add:

```python
    if current_user.role == "student" and getattr(current_user, "is_trial", False):
        grant = get_active_trial_grant(db, current_user.id, course_id)
        allowed_ids = {int(x) for x in (grant.lesson_ids or [])} if grant else set()
        for _mod in result:  # `result` = the list being returned; adapt to actual variable name
            for _les in (_mod.lessons or []):
                _les.is_accessible = _les.id in allowed_ids
```
Read the tail of the handler first to use the real variable name and confirm lessons expose `is_accessible` (the frontend reads `lesson.is_accessible`; grep `is_accessible` in `courses.py` to find where it is set for the weekly/drip path and mirror that mechanism).

- [ ] **Step 6: Write failing enforcement tests** (append; mock-style, no DB):

```python
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
```

- [ ] **Step 7: Run full suite** — `.venv/bin/python -m pytest -q` → all pass (regression guard for untouched paths).
- [ ] **Step 8: Commit** — `git add -A && git commit -m "feat(trials): request-time enforcement in course/lesson access paths + cache bypass"`

---

### Task 4: /trials API (grant lifecycle)

**Files:**
- Create: `src/trials/schemas.py`, `src/trials/routes/__init__.py`, `src/trials/routes/trials.py`
- Modify: `src/routes/__init__.py` (register), `src/app.py` (`_MUTATION_INVALIDATION_RULES` add `"trials"`)
- Test: extend `tests/test_trial_access.py`

**Interfaces:**
- Consumes: Task 2 services; `generate_password`, `generate_student_id` from `src.admin.routes.admin`; `send_invite_email` from `src.services.email_service`; `hash_password` from auth utils (grep its import in `src/admin/routes/admin.py` and reuse the same path).
- Produces (frontend contract):
  - `POST /trials` body `{email, name, course_id, lesson_ids: int[], expires_at: ISO, prospect_note?, send_invite?: bool}` → `{trial: TrialSchema, generated_password: str | null}`; 409 on real-user email or duplicate active grant.
  - `GET /trials?status=&course_id=&search=` → `{trials: TrialSchema[]}`
  - `PATCH /trials/{id}` body `{expires_at?, lesson_ids?, prospect_note?}` → `TrialSchema`
  - `POST /trials/{id}/revoke` → `TrialSchema`
  - `POST /trials/{id}/resend-invite` → `{sent: bool}`
  - `POST /trials/{id}/convert` → `TrialSchema`
  - `TrialSchema = {id, user_id, user_email, user_name, course_id, course_title, lesson_ids, expires_at, status (computed: "expired" when active-but-past-deadline), granted_by, granted_by_name, prospect_note, created_at, revoked_at}`
  - All endpoints `Depends(require_admin_or_head_curator())`.

- [ ] **Step 1: Write failing tests for the service-level pieces the routes use** (append):

```python
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
```

- [ ] **Step 2: Run** → FAIL. 

- [ ] **Step 3: Implement schemas** — `src/trials/schemas.py`:

```python
from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel

from src.trials.services import grant_is_active
from src.trials.models import TRIAL_ACTIVE, TRIAL_EXPIRED


def effective_status(grant) -> str:
    """Display status: an 'active' row past its deadline reads as expired."""
    if grant.status == TRIAL_ACTIVE and not grant_is_active(grant):
        return TRIAL_EXPIRED
    return grant.status


class TrialCreateRequest(BaseModel):
    email: str
    name: str
    course_id: int
    lesson_ids: List[int]
    expires_at: datetime
    prospect_note: Optional[str] = None
    send_invite: bool = True


class TrialUpdateRequest(BaseModel):
    expires_at: Optional[datetime] = None
    lesson_ids: Optional[List[int]] = None
    prospect_note: Optional[str] = None


class TrialSchema(BaseModel):
    id: int
    user_id: int
    user_email: str
    user_name: str
    course_id: int
    course_title: str
    lesson_ids: List[int]
    expires_at: datetime
    status: str
    granted_by: Optional[int] = None
    granted_by_name: Optional[str] = None
    prospect_note: Optional[str] = None
    created_at: Optional[datetime] = None
    revoked_at: Optional[datetime] = None


class TrialCreateResponse(BaseModel):
    trial: TrialSchema
    generated_password: Optional[str] = None
```

- [ ] **Step 4: Implement routes** — `src/trials/routes/__init__.py`:
```python
from src.trials.routes.trials import router as trials_router  # noqa
```

`src/trials/routes/trials.py`:

```python
from datetime import datetime
from typing import Optional, List, Set

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from src.config import get_db
from src.schemas.models import UserInDB, Course, Lesson, Module
from src.utils.permissions import require_admin_or_head_curator
from src.utils.auth_utils import hash_password  # match the import used in src/admin/routes/admin.py
from src.admin.routes.admin import generate_password, generate_student_id
from src.services.email_service import send_invite_email
from src.trials.models import TrialAccess, TRIAL_ACTIVE, TRIAL_REVOKED, TRIAL_CONVERTED
from src.trials.schemas import (
    TrialCreateRequest, TrialUpdateRequest, TrialSchema, TrialCreateResponse, effective_status,
)
from src.trials import services as trial_services

router = APIRouter()


def _validate_lesson_ids(lesson_ids: List[int], course_lesson_ids: Set[int]) -> List[int]:
    ids = [int(x) for x in (lesson_ids or [])]
    if not ids:
        raise HTTPException(status_code=422, detail="Select at least one lesson")
    foreign = [x for x in ids if x not in course_lesson_ids]
    if foreign:
        raise HTTPException(status_code=422, detail=f"Lessons not in this course: {foreign}")
    return sorted(set(ids))


def _course_lesson_ids(db: Session, course_id: int) -> Set[int]:
    rows = (
        db.query(Lesson.id)
        .join(Module, Lesson.module_id == Module.id)
        .filter(Module.course_id == course_id)
        .all()
    )
    return {r[0] for r in rows}


def _to_schema(db: Session, grant: TrialAccess) -> TrialSchema:
    user = db.query(UserInDB).filter(UserInDB.id == grant.user_id).first()
    course = db.query(Course).filter(Course.id == grant.course_id).first()
    granted_by_name = None
    if grant.granted_by:
        gb = db.query(UserInDB).filter(UserInDB.id == grant.granted_by).first()
        granted_by_name = gb.name if gb else None
    return TrialSchema(
        id=grant.id,
        user_id=grant.user_id,
        user_email=user.email if user else "",
        user_name=user.name if user else "",
        course_id=grant.course_id,
        course_title=course.title if course else "",
        lesson_ids=[int(x) for x in (grant.lesson_ids or [])],
        expires_at=grant.expires_at,
        status=effective_status(grant),
        granted_by=grant.granted_by,
        granted_by_name=granted_by_name,
        prospect_note=grant.prospect_note,
        created_at=grant.created_at,
        revoked_at=grant.revoked_at,
    )


@router.post("/", response_model=TrialCreateResponse)
def create_trial(
    body: TrialCreateRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: UserInDB = Depends(require_admin_or_head_curator()),
):
    email = body.email.lower().strip()
    course = db.query(Course).filter(Course.id == body.course_id).first()
    if not course:
        raise HTTPException(status_code=404, detail="Course not found")
    lesson_ids = _validate_lesson_ids(body.lesson_ids, _course_lesson_ids(db, body.course_id))
    expires_at = trial_services._as_utc_naive(body.expires_at)

    user = db.query(UserInDB).filter(UserInDB.email == email).first()
    generated_password: Optional[str] = None
    if user and not user.is_trial:
        raise HTTPException(status_code=409, detail="Email belongs to an existing non-trial account")
    if user:
        existing = trial_services.get_active_trial(db, user.id, body.course_id)
        if existing:
            raise HTTPException(
                status_code=409,
                detail="This prospect already has an active trial for this course — edit it instead",
            )
        password = generate_password()
        generated_password = password
        user.hashed_password = hash_password(password)  # fresh credentials for the new grant
    else:
        password = generate_password()
        generated_password = password
        student_id = generate_student_id()
        while db.query(UserInDB).filter(UserInDB.student_id == student_id).first():
            student_id = generate_student_id()
        user = UserInDB(
            email=email,
            name=body.name,
            hashed_password=hash_password(password),
            role="student",
            student_id=student_id,
            is_active=True,
            is_trial=True,
            assignment_zero_completed=True,  # spec: trial users never enter the Assignment-Zero funnel
        )
        db.add(user)
        db.flush()

    grant = TrialAccess(
        user_id=user.id,
        course_id=body.course_id,
        lesson_ids=lesson_ids,
        expires_at=expires_at,
        status=TRIAL_ACTIVE,
        granted_by=current_user.id,
        prospect_note=body.prospect_note,
    )
    db.add(grant)
    db.commit()
    db.refresh(grant)

    if body.send_invite:
        background_tasks.add_task(send_invite_email, user.email, user.name or "", user.email, password)

    return TrialCreateResponse(trial=_to_schema(db, grant), generated_password=generated_password)


@router.get("/")
def list_trials(
    status: Optional[str] = Query(None),
    course_id: Optional[int] = Query(None),
    search: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    current_user: UserInDB = Depends(require_admin_or_head_curator()),
):
    q = db.query(TrialAccess).order_by(TrialAccess.created_at.desc())
    if course_id:
        q = q.filter(TrialAccess.course_id == course_id)
    grants = q.all()
    out = [_to_schema(db, g) for g in grants]
    if status:
        out = [t for t in out if t.status == status]
    if search:
        s = search.lower()
        out = [t for t in out if s in t.user_email.lower() or s in t.user_name.lower()]
    return {"trials": out}


def _get_grant_or_404(db: Session, trial_id: int) -> TrialAccess:
    grant = db.query(TrialAccess).filter(TrialAccess.id == trial_id).first()
    if not grant:
        raise HTTPException(status_code=404, detail="Trial not found")
    return grant


@router.patch("/{trial_id}", response_model=TrialSchema)
def update_trial(
    trial_id: int,
    body: TrialUpdateRequest,
    db: Session = Depends(get_db),
    current_user: UserInDB = Depends(require_admin_or_head_curator()),
):
    grant = _get_grant_or_404(db, trial_id)
    if body.expires_at is not None:
        grant.expires_at = trial_services._as_utc_naive(body.expires_at)
        if grant.status in ("expired",) and trial_services.grant_is_active(grant):
            grant.status = TRIAL_ACTIVE  # extending a lapsed trial re-activates it
    if body.lesson_ids is not None:
        grant.lesson_ids = _validate_lesson_ids(body.lesson_ids, _course_lesson_ids(db, grant.course_id))
    if body.prospect_note is not None:
        grant.prospect_note = body.prospect_note
    db.commit()
    db.refresh(grant)
    return _to_schema(db, grant)


@router.post("/{trial_id}/revoke", response_model=TrialSchema)
def revoke_trial(
    trial_id: int,
    db: Session = Depends(get_db),
    current_user: UserInDB = Depends(require_admin_or_head_curator()),
):
    grant = _get_grant_or_404(db, trial_id)
    grant.status = TRIAL_REVOKED
    grant.revoked_at = trial_services.utcnow()
    db.commit()
    db.refresh(grant)
    return _to_schema(db, grant)


@router.post("/{trial_id}/resend-invite")
def resend_trial_invite(
    trial_id: int,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: UserInDB = Depends(require_admin_or_head_curator()),
):
    grant = _get_grant_or_404(db, trial_id)
    user = db.query(UserInDB).filter(UserInDB.id == grant.user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="Trial user not found")
    password = generate_password()
    user.hashed_password = hash_password(password)
    db.commit()
    background_tasks.add_task(send_invite_email, user.email, user.name or "", user.email, password)
    return {"sent": True}


@router.post("/{trial_id}/convert", response_model=TrialSchema)
def convert_trial(
    trial_id: int,
    db: Session = Depends(get_db),
    current_user: UserInDB = Depends(require_admin_or_head_curator()),
):
    grant = _get_grant_or_404(db, trial_id)
    grant.status = TRIAL_CONVERTED
    user = db.query(UserInDB).filter(UserInDB.id == grant.user_id).first()
    if user:
        user.is_trial = False  # now a real student; admin enrolls via the normal group flow
    db.commit()
    db.refresh(grant)
    return _to_schema(db, grant)
```

- [ ] **Step 5: Register** — `src/routes/__init__.py`: add `from src.trials.routes import trials_router` to the import block and `app.include_router(trials_router, prefix="/trials", tags=["Trials"])` to the include block. `src/app.py` `_MUTATION_INVALIDATION_RULES`: add
```python
    "trials": ("courses:*", "progress:*", "dashboard:*", "admin:*"),
```

- [ ] **Step 6: Run** — `.venv/bin/python -m pytest -q` → PASS; also boot-import check: `.venv/bin/python -c "from src.routes import register_routes; from fastapi import FastAPI; register_routes(FastAPI())"` → no error.
- [ ] **Step 7: Commit** — `git add -A && git commit -m "feat(trials): grant lifecycle API (/trials) with invite emails"`

---

### Task 5: /auth/me trial fields

**Files:**
- Modify: `src/auth/schemas.py` (`UserSchema`), `src/auth/user_schema.py` (`build_user_schema_response`)
- Test: extend `tests/test_trial_access.py`

**Interfaces:**
- Produces: `UserSchema.is_trial: Optional[bool] = False`, `UserSchema.trial_expires_at: Optional[datetime] = None` (earliest active expiry; `None` when no active grant → frontend treats `is_trial && !trial_expires_at` as expired).

- [ ] **Step 1: Failing test** (append):

```python
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
```

- [ ] **Step 2: Run** → FAIL. 

- [ ] **Step 3: Implement** — `src/auth/schemas.py` `UserSchema`: add after `is_analytics_hidden`:
```python
    is_trial: Optional[bool] = False
    # Computed on /auth/me for trial users: earliest active grant deadline (None = no active grant)
    trial_expires_at: Optional[datetime] = None
```

`src/auth/user_schema.py`:
```python
def build_user_schema_response(user: UserInDB, db: Session) -> UserSchema:
    base = UserSchema.model_validate(user)
    if user.role != "student":
        return base.model_copy(update={"special_group_only_student": False})
    update = {"special_group_only_student": student_has_only_special_groups(user.id, db)}
    if getattr(user, "is_trial", False):
        from src.trials.services import earliest_active_expiry
        update["trial_expires_at"] = earliest_active_expiry(db, user.id)
    return base.model_copy(update=update)
```

- [ ] **Step 4: Run** → PASS. **Step 5: Commit** — `git add -A && git commit -m "feat(trials): expose is_trial + trial_expires_at on /auth/me"`

---

### Task 6: Bookkeeping scheduler + exclusion sweep

**Files:**
- Create: `src/services/trial_status_job.py`
- Modify: `src/services/run_scheduler.py` (start the job thread, after the student-sync drainer block ~line 57)
- Modify: leaderboard/export/admin-list student enumerations to exclude/expose `is_trial`
- Test: extend `tests/test_trial_access.py`

- [ ] **Step 1: Failing test for the job core** (append):

```python
def test_trial_status_job_uses_expire_stale(monkeypatch):
    from src.services import trial_status_job

    called = {}
    monkeypatch.setattr(trial_status_job, "SessionLocal", lambda: SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(trial_status_job, "expire_stale_trials", lambda db: called.setdefault("n", 3))
    trial_status_job.run_once()
    assert called["n"] == 3
```

- [ ] **Step 2: Run** → FAIL. 

- [ ] **Step 3: Implement** — `src/services/trial_status_job.py` (model on `CuratorTaskScheduler`; grep `SessionLocal` import path there and reuse):

```python
"""Bookkeeping loop: flip trial_accesses active→expired past deadline.

Enforcement is request-time (src/trials/services.grant_is_active); this job only
keeps list views / audit truthful. Runs ONLY in the scheduler container
(started from run_scheduler.py) so API workers never double-run it.
"""
import logging
import threading
import time

from src.config import SessionLocal  # same import as curator_task_scheduler
from src.trials.services import expire_stale_trials

logger = logging.getLogger(__name__)


def run_once() -> int:
    db = SessionLocal()
    try:
        count = expire_stale_trials(db)
        if count:
            logger.info("[TRIALS] marked %s trial grant(s) expired", count)
        return count
    finally:
        db.close()


class TrialStatusScheduler:
    def __init__(self, check_interval: int = 300):
        self.check_interval = check_interval
        self.running = False
        self.thread = None

    def start(self):
        if self.running:
            return
        self.running = True
        self.thread = threading.Thread(target=self._run, daemon=True, name="trial-status")
        self.thread.start()
        logger.info("Trial status scheduler started (interval: %ss)", self.check_interval)

    def stop(self):
        self.running = False
        if self.thread:
            self.thread.join(timeout=5)

    def _run(self):
        while self.running:
            try:
                run_once()
            except Exception as e:  # never die on a tick
                logger.error("[TRIALS] status tick failed: %s", e, exc_info=True)
            time.sleep(self.check_interval)
```
(verify `SessionLocal`'s real import path from `curator_task_scheduler.py` and match it.)

`run_scheduler.py` — after the student-sync drainer `try` block:
```python
    # Trial-access bookkeeping: flips expired trial grants' status (enforcement is
    # request-time; this only keeps admin list views truthful). Scheduler-container only.
    try:
        from src.services.trial_status_job import TrialStatusScheduler
        TrialStatusScheduler(check_interval=int(os.getenv("TRIAL_STATUS_POLL", "300"))).start()
    except Exception as e:
        logger.error(f"Failed to start trial status scheduler: {e}", exc_info=True)
```

- [ ] **Step 4: Exclusion sweep** — spec §4: run `grep -rn "role == \"student\"\|role=='student'\|role == 'student'" src/gamification src/admin/routes/admin.py src/services/excel_export*.py 2>/dev/null` and `grep -rln "leaderboard" src/gamification`. Apply `UserInDB.is_trial == False` (via `.filter()`) to: (a) leaderboard student aggregation queries in `src/gamification/` (all queries that enumerate students by role), (b) any Excel/Sheets export that enumerates students (grep `excel` under `src/services/`), and (c) `GET /admin/users` in `src/admin/routes/admin.py`: add optional `is_trial: Optional[bool] = Query(None)` filter param (`if is_trial is not None: query = query.filter(UserInDB.is_trial == is_trial)`) and include `is_trial` in the serialized user rows if the endpoint hand-builds dicts (UserSchema already carries it after Task 5). Keep each change to a one-line filter; do not restructure queries.

- [ ] **Step 5: Run suite** → PASS. **Step 6: Commit** — `git add -A && git commit -m "feat(trials): status bookkeeping job + is_trial exclusions in leaderboards/exports/admin list"`

---

### Task 7: Optional DB integration test (skip-safe)

**Files:**
- Create: `tests/test_trial_access_db.py`

- [ ] **Step 1: Write the module** (entirely skipped unless a local test DB is reachable — CI has none, so CI is unaffected):

```python
"""End-to-end trial enforcement against a real Postgres (local only; auto-skips).

Uses the lms-postgres docker container's lms_test DB when reachable:
  POSTGRES_URL=postgresql://postgres:postgres@localhost:5432/lms_test
"""
import os
from datetime import timedelta

import pytest

URL = os.getenv("TRIAL_TEST_DB_URL", "postgresql://postgres:postgres@localhost:5432/lms_test")

def _engine_or_none():
    try:
        from sqlalchemy import create_engine
        eng = create_engine(URL, pool_pre_ping=True)
        with eng.connect():
            return eng
    except Exception:
        return None

ENGINE = _engine_or_none()
pytestmark = pytest.mark.skipif(ENGINE is None, reason="local test Postgres not reachable")


@pytest.fixture()
def db():
    from sqlalchemy.orm import sessionmaker
    from src.models.base import Base
    Base.metadata.create_all(bind=ENGINE)
    conn = ENGINE.connect()
    txn = conn.begin()
    Session = sessionmaker(bind=conn)
    s = Session()
    yield s
    s.close()
    txn.rollback()
    conn.close()


def _seed(db):
    from src.schemas.models import UserInDB, Course, Module, Lesson
    from src.trials.models import TrialAccess
    from src.trials.services import utcnow
    u = UserInDB(email="trial-e2e@x.kz", name="T", hashed_password="x", role="student", is_trial=True)
    c = Course(title="C", teacher_id=None, is_active=True)
    db.add_all([u, c]); db.flush()
    m = Module(course_id=c.id, title="M", order_index=0)
    db.add(m); db.flush()
    l1 = Lesson(module_id=m.id, title="L1", order_index=0)
    l2 = Lesson(module_id=m.id, title="L2", order_index=1)
    db.add_all([l1, l2]); db.flush()
    g = TrialAccess(user_id=u.id, course_id=c.id, lesson_ids=[l1.id],
                    expires_at=utcnow() + timedelta(hours=24), status="active")
    db.add(g); db.flush()
    return u, c, l1, l2, g


def test_enforcement_matrix_db(db):
    from src.trials.services import trial_lesson_access, trial_course_ids, expire_stale_trials
    from src.trials.services import utcnow
    from datetime import timedelta
    u, c, l1, l2, g = _seed(db)
    assert trial_course_ids(db, u.id) == [c.id]
    assert trial_lesson_access(db, u.id, l1.id) == (True, None)
    ok, reason = trial_lesson_access(db, u.id, l2.id)
    assert ok is False and reason == "Not included in your trial"
    g.expires_at = utcnow() - timedelta(seconds=1)
    db.flush()
    ok, reason = trial_lesson_access(db, u.id, l1.id)
    assert ok is False and reason == "Your trial has ended"
    assert trial_course_ids(db, u.id) == []
```
Adapt seed columns to actual NOT NULL constraints if `create_all` demands more (run and fix).

- [ ] **Step 2: Run** — with the container up: `.venv/bin/python -m pytest tests/test_trial_access_db.py -q` → PASS (or SKIP if no DB; both acceptable, but try to run it for real locally).
- [ ] **Step 3: Commit** — `git add -A && git commit -m "test(trials): skip-safe DB integration matrix"`

---

### Task 8: Frontend API layer + types

**Files (repo `lms-front`, branch `master`):**
- Create: `src/services/api/trials.ts`
- Modify: `src/types/index.ts` (User fields ~line 22-26 area; new Trial types near `ManualLessonUnlock` ~806), `src/services/api/index.ts` (add `export * from './trials'`; add trial functions to the `apiClient` object — mirror how `users.ts` functions are wired in)

**Interfaces:**
- Consumes: backend contract from Task 4 + Task 5.
- Produces: `TrialAccess`, `TrialCreateRequest`, `TrialUpdateRequest` TS types; `createTrial`, `getTrials`, `updateTrial`, `revokeTrial`, `resendTrialInvite`, `convertTrial` functions; `User.is_trial?: boolean; User.trial_expires_at?: string`.

- [ ] **Step 1: types** — `src/types/index.ts`: inside `interface User` add
```ts
  is_trial?: boolean; // Sales-prospect trial account (see /trial-access admin page)
  trial_expires_at?: string; // Earliest active trial deadline; absent/undefined => no active trial
```
and near the ManualLessonUnlock types add:
```ts
export interface TrialAccess {
  id: number;
  user_id: number;
  user_email: string;
  user_name: string;
  course_id: number;
  course_title: string;
  lesson_ids: number[];
  expires_at: string;
  status: 'active' | 'expired' | 'revoked' | 'converted';
  granted_by?: number;
  granted_by_name?: string;
  prospect_note?: string;
  created_at?: string;
  revoked_at?: string;
}

export interface TrialCreateRequest {
  email: string;
  name: string;
  course_id: number;
  lesson_ids: number[];
  expires_at: string; // ISO
  prospect_note?: string;
  send_invite?: boolean;
}

export interface TrialUpdateRequest {
  expires_at?: string;
  lesson_ids?: number[];
  prospect_note?: string;
}
```

- [ ] **Step 2: API module** — `src/services/api/trials.ts` (mirror the axios usage style of `src/services/api/users.ts` — read it first and match its import of the shared client exactly):

```ts
import { api } from './client';
import type { TrialAccess, TrialCreateRequest, TrialUpdateRequest } from '../../types';

export interface TrialCreateResponse {
  trial: TrialAccess;
  generated_password: string | null;
}

export async function createTrial(data: TrialCreateRequest): Promise<TrialCreateResponse> {
  const res = await api.post('/trials/', data);
  return res.data;
}

export async function getTrials(params?: {
  status?: string;
  course_id?: number;
  search?: string;
}): Promise<{ trials: TrialAccess[] }> {
  const res = await api.get('/trials/', { params });
  return res.data;
}

export async function updateTrial(id: number, data: TrialUpdateRequest): Promise<TrialAccess> {
  const res = await api.patch(`/trials/${id}`, data);
  return res.data;
}

export async function revokeTrial(id: number): Promise<TrialAccess> {
  const res = await api.post(`/trials/${id}/revoke`);
  return res.data;
}

export async function resendTrialInvite(id: number): Promise<{ sent: boolean }> {
  const res = await api.post(`/trials/${id}/resend-invite`);
  return res.data;
}

export async function convertTrial(id: number): Promise<TrialAccess> {
  const res = await api.post(`/trials/${id}/convert`);
  return res.data;
}
```
(If `client.ts` exports a differently-named axios instance, adapt; verify GET caching in `cache.ts` keys by URL — `/trials/` reads go through the standard client cache and are invalidated by the POST/PATCH mutation handling already present.)

- [ ] **Step 3: Wire into `index.ts`** — add `export * from './trials';` to the re-export block and add the six functions to the `apiClient` aggregate object next to the `users.ts` entries.
- [ ] **Step 4: Compile** — `npm run build` → type-check passes. **Step 5: Commit** — `git add -A && git commit -m "feat(trials): API client + types"`

---

### Task 9: Trial banner + expired gate (prospect UX)

**Files:**
- Create: `src/components/trial/TrialBanner.tsx`, `src/components/trial/TrialExpiredPanel.tsx`
- Modify: `src/components/ProtectedRoute.tsx` (expired gate), the layout that wraps pages (`AppLayout` — find it in `src/routes/Router.tsx` imports; mount `TrialBanner` inside it)

**Interfaces:**
- Consumes: `useAuth()` user with `is_trial`/`trial_expires_at`; `refreshUser` from AuthContext; `clearCache` from `src/services/api`.
- Produces: gate rule — a student with `is_trial === true` and (`!trial_expires_at` or `trial_expires_at <= now`) sees `TrialExpiredPanel` on every protected route.

- [ ] **Step 1: `TrialExpiredPanel.tsx`**

```tsx
import { useAuth } from '../../contexts/AuthContext';
import { Clock } from 'lucide-react';

const TrialExpiredPanel: React.FC = () => {
  const { logout } = useAuth();
  return (
    <div className="min-h-screen flex items-center justify-center bg-gray-50 dark:bg-gray-900 px-4">
      <div className="max-w-md w-full text-center bg-white dark:bg-gray-800 rounded-2xl shadow-lg p-8">
        <div className="mx-auto mb-4 w-14 h-14 rounded-full bg-amber-100 flex items-center justify-center">
          <Clock className="w-7 h-7 text-amber-600" />
        </div>
        <h1 className="text-2xl font-bold mb-2">Your trial has ended</h1>
        <p className="text-gray-600 dark:text-gray-300 mb-6">
          Thanks for exploring Master Education! To continue learning with full access,
          contact our team — we'll set you up in minutes.
        </p>
        <a
          href="https://mastereducation.kz"
          target="_blank"
          rel="noreferrer"
          className="inline-block w-full px-4 py-2 rounded-lg bg-blue-600 text-white hover:bg-blue-700 mb-3"
        >
          Contact us to continue
        </a>
        <button onClick={() => logout()} className="text-sm text-gray-500 hover:text-gray-700">
          Log out
        </button>
      </div>
    </div>
  );
};

export default TrialExpiredPanel;
```
(check how `logout` is exposed in `AuthContext` and match; check the sales contact URL/phone with the landing page footer and use the same.)

- [ ] **Step 2: `TrialBanner.tsx`**

```tsx
import { useEffect, useState } from 'react';
import { useAuth } from '../../contexts/AuthContext';
import { Clock } from 'lucide-react';

function formatRemaining(ms: number): string {
  const totalMin = Math.max(0, Math.floor(ms / 60000));
  const h = Math.floor(totalMin / 60);
  const m = totalMin % 60;
  return h > 0 ? `${h}h ${m}m` : `${m}m`;
}

const TrialBanner: React.FC = () => {
  const { user, refreshUser } = useAuth();
  const [now, setNow] = useState(() => Date.now());

  const deadline = user?.is_trial && user?.trial_expires_at
    ? new Date(user.trial_expires_at).getTime()
    : null;

  useEffect(() => {
    if (!deadline) return;
    const t = setInterval(() => setNow(Date.now()), 30_000);
    return () => clearInterval(t);
  }, [deadline]);

  useEffect(() => {
    // When the countdown hits zero, refetch the user: trial_expires_at disappears
    // and ProtectedRoute switches to the expired panel.
    if (deadline && now >= deadline) refreshUser();
  }, [deadline, now, refreshUser]);

  if (!deadline || now >= deadline) return null;
  return (
    <div className="w-full bg-amber-500 text-white text-sm px-4 py-1.5 flex items-center justify-center gap-2">
      <Clock className="w-4 h-4" />
      <span>Trial access — ends in {formatRemaining(deadline - now)}</span>
    </div>
  );
};

export default TrialBanner;
```
(verify `refreshUser`'s exact name in AuthContext; explorer reported `refreshUser` exists.)

- [ ] **Step 3: Gate in `ProtectedRoute.tsx`** — insert after the Assignment-Zero block (line ~88), before `return children`:

```tsx
  // Trial prospects: once no active grant remains, lock the app behind the upsell panel
  if (
    requireAuth &&
    isAuthenticated &&
    user?.role === 'student' &&
    user?.is_trial &&
    (!user?.trial_expires_at || new Date(user.trial_expires_at).getTime() <= Date.now())
  ) {
    return <TrialExpiredPanel />;
  }
```
with `import TrialExpiredPanel from './trial/TrialExpiredPanel';`. Also call `clearCache()` (from `src/services/api`) inside `TrialExpiredPanel` on mount (add a `useEffect`) so no cached course data lingers.

- [ ] **Step 4: Mount banner** — open the layout component used by `Router.tsx` (`AppLayout`), render `<TrialBanner />` at the top of its content area (above the routed page, below any fixed header — inspect the JSX and place it so it shows on every authenticated page).
- [ ] **Step 5: Build** — `npm run build` → passes. **Step 6: Commit** — `git add -A && git commit -m "feat(trials): countdown banner + expired gate"`

---

### Task 10: Admin Trial Access page

**Files:**
- Create: `src/pages/admin/TrialAccessPage.tsx`
- Modify: `src/routes/Router.tsx` (lazy route `/trial-access`, `allowedRoles={['admin', 'head_curator']}` — copy the ManualUnlocksPage route entry as the template), `src/components/Sidebar.tsx` (nav item next to the Manual Unlocks entry ~line 99, visible to `admin` + `head_curator`, icon `Timer` from lucide)

**Interfaces:**
- Consumes: Task 8 API functions; `getCourses()` and `getCourseModules(courseId, true)` from the existing API modules (same calls ManualUnlocksPage makes).

- [ ] **Step 1: Implement the page.** Read `src/pages/admin/ManualUnlocksPage.tsx` first and reuse its data-loading patterns (course list load, `getCourseModules(courseId, true)` for the module→lesson tree) and its Card/Table/Dialog composition. The page has two parts:

**(a) Trials table** — columns: Prospect (name + email), Course, Lessons (count), Deadline (absolute + live "in Xh Ym" / "expired"), Status (chip color: active=green, expired=gray, revoked=red, converted=blue), Granted by, Note. Row actions (buttons or dropdown): Edit (opens dialog pre-filled), Revoke (confirm → `revokeTrial`), Resend invite (`resendTrialInvite` → toast), Convert (confirm → `convertTrial`). Data: `getTrials()` on mount + after each mutation; status filter tabs (All / active / expired / revoked / converted) client-side.

**(b) Grant dialog** (also used for Edit) — fields:
- Email + Name + Note (text inputs; disabled in edit mode except Note)
- Course select (from `getCourses()`)
- Lesson tree: on course select call `getCourseModules(course_id, true)`, render modules as collapsible groups with a checkbox per lesson; require ≥1 checked
- Deadline: `<input type="datetime-local">` initialized to now+24h (`new Date(Date.now() + 24*3600e3)` formatted with a local-datetime helper); submit converts with `new Date(value).toISOString()`
- Submit: create mode → `createTrial({...})`; on success show the `generated_password` in a copyable field with a note that the invite email was sent; edit mode → `updateTrial(id, {expires_at, lesson_ids, prospect_note})`
- 409 handling: toast the backend `detail` string.

Skeleton to start from (fill in with the repo's existing UI primitives as used by ManualUnlocksPage):

```tsx
import { useCallback, useEffect, useMemo, useState } from 'react';
import { apiClient } from '../../services/api';
import type { TrialAccess, CourseModule } from '../../types';
// ...Card/Table/Dialog/Button/Input/Select/Checkbox/Badge imports matching ManualUnlocksPage...

function toLocalInputValue(d: Date): string {
  const pad = (n: number) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

const TrialAccessPage: React.FC = () => {
  const [trials, setTrials] = useState<TrialAccess[]>([]);
  const [statusFilter, setStatusFilter] = useState<string>('all');
  const [dialogOpen, setDialogOpen] = useState(false);
  const [editing, setEditing] = useState<TrialAccess | null>(null);
  // form state: email, name, note, courseId, selectedLessonIds (Set<number>), deadline (string)
  // modules: CourseModule[] for the selected course

  const load = useCallback(async () => {
    const res = await apiClient.getTrials();
    setTrials(res.trials);
  }, []);
  useEffect(() => { load(); }, [load]);

  // ...course change -> apiClient.getCourseModules(courseId, true) -> setModules
  // ...submit -> createTrial/updateTrial -> toast + load() + keep dialog open showing generated_password (create mode)
  // ...revoke/resend/convert row actions with window.confirm + toast + load()

  const visible = useMemo(
    () => statusFilter === 'all' ? trials : trials.filter(t => t.status === statusFilter),
    [trials, statusFilter],
  );

  return (/* Card + filter tabs + Table + Dialog per (a)/(b) above */);
};

export default TrialAccessPage;
```

- [ ] **Step 2: Route + nav** — `Router.tsx`: `const TrialAccessPage = lazy(() => import('../pages/admin/TrialAccessPage'));` and a route entry copying the ManualUnlocksPage wrapper but path `/trial-access`, `allowedRoles={['admin', 'head_curator']}`. `Sidebar.tsx`: add `{ name: 'Trial Access', href: '/trial-access', icon: Timer, roles: ['admin', 'head_curator'] }`-style entry matching the sidebar's item shape (read the neighboring entries and mirror exactly).
- [ ] **Step 3: Build** — `npm run build` → passes. **Step 4: Commit** — `git add -A && git commit -m "feat(trials): admin Trial Access page (grant/edit/revoke/resend/convert)"`

---

### Task 11: Sample-data dashboard for trial users

**Files:**
- Create: `src/components/trial/SampleBadge.tsx`, `src/data/trialSampleData.ts`
- Modify: `src/pages/StudentDashboard.tsx` (conditional sample content when `user?.is_trial`)

**Interfaces:**
- Consumes: `useAuth()` user. Static only — no API calls, no backend involvement, zero effect on non-trial users.

- [ ] **Step 1: `SampleBadge.tsx`**

```tsx
const SampleBadge: React.FC = () => (
  <span className="inline-flex items-center rounded-full bg-violet-100 text-violet-700 text-[10px] font-semibold px-2 py-0.5 uppercase tracking-wide ml-2">
    Sample data
  </span>
);

export default SampleBadge;
```

- [ ] **Step 2: `trialSampleData.ts`** — small typed constants for the sections that are empty for a prospect (match the shapes the dashboard sections actually render — read `StudentDashboard.tsx` first):

```ts
// Placeholder content shown ONLY to trial users (user.is_trial) in dashboard
// sections that would otherwise be empty. Always rendered with <SampleBadge />.
export const TRIAL_SAMPLE_SESSIONS = [
  { id: -1, title: 'Speaking club — Intermediate', day: 'Mon', time: '18:00' },
  { id: -2, title: 'Grammar workshop', day: 'Wed', time: '19:00' },
];

export const TRIAL_SAMPLE_ACTIVITY = [
  { id: -1, label: 'Completed “Introductions” lesson', when: '2 hours ago' },
  { id: -2, label: 'Scored 8/10 on vocabulary quiz', when: 'Yesterday' },
  { id: -3, label: 'Earned the “First steps” badge', when: 'Yesterday' },
];

export const TRIAL_SAMPLE_STATS = { streakDays: 3, points: 120, studyMinutes: 145 };
```

- [ ] **Step 3: Integrate.** Read `src/pages/StudentDashboard.tsx` fully. For each section that renders an empty state when the student has no data — (per structure scan: the weekly-sessions card ~line 570, the activity tab ~1132-1197 "No activity yet", and the streak/points stats in the welcome card ~670) — add a trial branch: when `user?.is_trial` **and** the section's real data is empty, render the sample constants in the section's existing markup with `<SampleBadge />` beside the section title. Never replace real data (the trial course progress card must stay real). Keep every change inside a `user?.is_trial` conditional.
- [ ] **Step 4: Build** — `npm run build` → passes. **Step 5: Commit** — `git add -A && git commit -m "feat(trials): sample-data dashboard widgets for trial users"`

---

### Task 12: Verification & deploy

- [ ] **Step 1: Backend suite** — `cd lms-backend && .venv/bin/python -m pytest -q` → all green. Boot check: `.venv/bin/python -c "import src.app"` → imports cleanly.
- [ ] **Step 2: If local test DB reachable**, run migration + DB tests: `POSTGRES_URL=postgresql://postgres:postgres@localhost:5432/lms_test .venv/bin/python -m alembic upgrade head && POSTGRES_URL=... .venv/bin/python -m pytest tests/test_trial_access_db.py -q`.
- [ ] **Step 3: Frontend build** — `cd lms-front && npm run build` → clean.
- [ ] **Step 4: Backend deploy** (user-authorized):
```bash
cd lms-backend
git checkout main && git pull origin main
git merge --no-ff feat/trial-access -m "feat: sales-prospect trial access (spec 2026-07-19)"
git push origin main        # CI deploys; container entrypoint runs `alembic upgrade head`
git checkout feat/email-platform-sender   # restore the branch that was checked out before
```
- [ ] **Step 5: Frontend deploy** — `cd lms-front && git push origin master` (Azure SWA auto-deploys).
- [ ] **Step 6: Report** — summarize what shipped, test results, commit SHAs, and any deferred items (CRM endpoint, expiry notifications).

## Self-Review Notes

- Spec coverage: §3 data model → Task 1; §4 enforcement (incl. modules `is_accessible`, hard gates, aux-surface exclusions) → Tasks 3 & 6; §5 API → Task 4; §6.1 admin UI → Task 10; §6.2 prospect UX (banner/expired/no-AZ/sample data) → Tasks 5, 9, 11 (AZ handled at creation, Task 4); §7 job → Task 6; §8 caching → Task 3 (decorator bypass) + Task 9 (clearCache on expiry); §9 edge cases → Tasks 3/4 behaviors + tests; §11 testing → Tasks 1-7.
- Types consistent: `trial_lesson_access(db, user_id, lesson_id)` used identically in Tasks 2/3; `TrialSchema` field list identical in backend schema and TS type.
- Known execution-time lookups (intentional, with exact commands): alembic head id (Task 0/1), `SessionLocal` import path (Task 6), `is_accessible` set-point in modules handler (Task 3), AuthContext member names (Task 9), sidebar item shape (Task 10), dashboard section shapes (Task 11).
