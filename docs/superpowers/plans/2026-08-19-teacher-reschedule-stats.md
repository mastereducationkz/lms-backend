# Teacher Reschedule-Stats Panel + Head-Curator Access — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a per-month "which teachers made ≥2 lesson change requests" statistics panel to the lesson-requests view, and give head curators full access to that view.

**Architecture:** A new backend aggregation service function + a thin FastAPI endpoint expose per-teacher monthly request counts; the existing authorization guard is widened to admit `head_curator` (unscoped, admin-like) alongside `head_teacher` (existing course-scope). The frontend adds a second tab to the shared `LessonRequestManagement` component rendering a `TeacherRescheduleStatsPanel`, and the head-curator role is added to the route + sidebar.

**Tech Stack:** FastAPI + SQLAlchemy (Python) backend; React + TypeScript (Vite) frontend; pytest (Postgres-backed) for backend tests.

## Global Constraints

- Two separate git repos: backend at `lms/backend` (branch `main`), frontend at `lms/frontend`. Commit each repo's changes in that repo.
- Backend tests require a real Postgres database; they `pytest.skip("No database available")` when the engine can't connect. Follow the transactional `db` fixture pattern in `tests/test_lesson_request_self_approve.py` verbatim.
- `LessonRequest.original_datetime` is a **naive** `DateTime` column. Do month-boundary math with naive `datetime` (no tz).
- Request attribution is by `requester_id` (the teacher who made the request). Month bucket is by `original_datetime` (lesson date). All statuses count. Threshold default `min_count=2`.
- Request types are the strings in `VALID_REQUEST_TYPES = ("substitution", "reschedule", "cancel")` (`src/lesson_requests/services.py:27`).
- Head curator gets **full** powers (approve/reject) and sees **all** requests (no group scope).
- Russian UI copy. Type labels already exist in `LessonRequestManagement.tsx:32` (`substitution→Замена`, `reschedule→Перенос`, `cancel→Отмена`).
- Frontend has no test runner; the frontend verification gate is `npm run build` (must succeed) plus the described manual smoke check.

---

## File Structure

**Backend (`lms/backend`):**
- Modify `src/lesson_requests/services.py` — add `head_curator` branch to `user_can_resolve_request`; add `get_teacher_request_stats(...)` aggregation function.
- Modify `src/lesson_requests/schemas.py` — add `TeacherRequestStatsSchema`.
- Modify `src/lesson_requests/routes.py` — add `_require_admin_head_teacher_or_head_curator` guard; widen `list_lesson_requests` / `list_pending_approval` to allow head_curator (unscoped); add `GET /teacher-stats` endpoint.
- Create `tests/test_lesson_request_teacher_stats.py`.
- Create `tests/test_lesson_request_head_curator_access.py`.

**Frontend (`lms/frontend`):**
- Modify `src/services/api/lesson-requests.ts` — add `TeacherRequestStats` type + `getTeacherRequestStats(...)`.
- Create `src/pages/admin/TeacherRescheduleStatsPanel.tsx` — the stats tab body.
- Modify `src/pages/admin/LessonRequestManagement.tsx` — add tab switcher (`Запросы` / `Статистика по учителям`) wrapping the existing list and the new panel.
- Modify `src/routes/Router.tsx:602-608` — allow `head_curator` on `/head-teacher/lesson-requests`.
- Modify `src/components/Sidebar.tsx:105` — show the lesson-requests entry for `head_curator`.

---

## Task 1: Backend — head_curator resolve permission

**Files:**
- Modify: `src/lesson_requests/services.py:120-125`
- Test: `tests/test_lesson_request_head_curator_access.py`

**Interfaces:**
- Consumes: `user_can_resolve_request(db, user, group_id) -> bool` (existing).
- Produces: same signature; now returns `True` for `user.role == "head_curator"`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_lesson_request_head_curator_access.py`:

```python
"""Head curators may resolve any lesson request (full powers, unscoped)."""
import pytest
from sqlalchemy import event
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session as SASession

from src.schemas.models import UserInDB
from src.utils.auth_utils import hash_password
from src.lesson_requests.services import user_can_resolve_request


@pytest.fixture
def db():
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
        session.close(); trans.rollback(); connection.close()


def _u(db, email, role):
    u = UserInDB(email=email, name=email.split("@")[0], role=role,
                 hashed_password=hash_password("x"), is_active=True)
    db.add(u); db.flush(); return u


def test_head_curator_can_resolve_any_group(db):
    hc = _u(db, "hc-resolve@test.local", "head_curator")
    # group_id 999999 need not exist — head_curator is unscoped
    assert user_can_resolve_request(db, hc, 999999) is True


def test_plain_curator_cannot_resolve(db):
    c = _u(db, "c-resolve@test.local", "curator")
    assert user_can_resolve_request(db, c, 999999) is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd lms/backend && python -m pytest tests/test_lesson_request_head_curator_access.py -v`
Expected: `test_head_curator_can_resolve_any_group` FAILS (returns False), or both SKIP if no DB. If skipped, set up the test DB per `memory: lms-backend-test-db` before continuing.

- [ ] **Step 3: Add the head_curator branch**

In `src/lesson_requests/services.py`, change `user_can_resolve_request` (currently lines 120-125):

```python
def user_can_resolve_request(db: Session, user: UserInDB, group_id: int) -> bool:
    if user.role == "admin":
        return True
    if user.role == "head_curator":
        return True
    if user.role == "head_teacher":
        return head_teacher_can_approve_group(db, user.id, group_id)
    return False
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd lms/backend && python -m pytest tests/test_lesson_request_head_curator_access.py -v`
Expected: PASS (or SKIP if no DB).

- [ ] **Step 5: Commit**

```bash
cd lms/backend
git add src/lesson_requests/services.py tests/test_lesson_request_head_curator_access.py
git commit -m "feat(lesson-requests): head curators can resolve any request"
```

---

## Task 2: Backend — teacher stats aggregation function

**Files:**
- Modify: `src/lesson_requests/services.py` (add function near the other query helpers, after `get_group_ids_in_head_teacher_scope`)
- Test: `tests/test_lesson_request_teacher_stats.py`

**Interfaces:**
- Consumes: `LessonRequest`, `UserInDB` models; `VALID_REQUEST_TYPES`.
- Produces:
  ```python
  def get_teacher_request_stats(
      db: Session,
      year: int,
      month: int,
      min_count: int = 2,
      scope_group_ids: Optional[list[int]] = None,
  ) -> list[dict]:
      # each dict: {"teacher_id": int, "teacher_name": str, "total": int,
      #             "by_type": {"substitution": int, "reschedule": int, "cancel": int}}
      # only teachers with total >= min_count; sorted by total desc, then teacher_name asc.
      # scope_group_ids=None means no group filter (admin / head_curator).
      # scope_group_ids=[] means empty scope → returns [].
  ```

- [ ] **Step 1: Write the failing test**

Create `tests/test_lesson_request_teacher_stats.py`:

```python
"""get_teacher_request_stats: per-teacher monthly request counts, threshold ≥ min_count."""
import pytest
from datetime import datetime
from sqlalchemy import event
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session as SASession

from src.schemas.models import UserInDB, Group, LessonRequest
from src.utils.auth_utils import hash_password
from src.lesson_requests.services import get_teacher_request_stats


@pytest.fixture
def db():
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
        session.close(); trans.rollback(); connection.close()


def _u(db, email, role="teacher"):
    u = UserInDB(email=email, name=email.split("@")[0], role=role,
                 hashed_password=hash_password("x"), is_active=True)
    db.add(u); db.flush(); return u


def _g(db, teacher_id):
    g = Group(name="G", is_active=True, is_over=False,
              teacher_id=teacher_id, program_type="sat")
    db.add(g); db.flush(); return g


def _req(db, requester_id, group_id, rtype, dt, status="pending"):
    r = LessonRequest(request_type=rtype, status=status, requester_id=requester_id,
                      group_id=group_id, original_datetime=dt)
    db.add(r); db.flush(); return r


def test_counts_all_types_threshold_two(db):
    t = _u(db, "stats-a@test.local")
    g = _g(db, t.id)
    _req(db, t.id, g.id, "reschedule", datetime(2026, 8, 3, 10, 0))
    _req(db, t.id, g.id, "substitution", datetime(2026, 8, 20, 10, 0))
    rows = get_teacher_request_stats(db, 2026, 8, min_count=2)
    mine = [r for r in rows if r["teacher_id"] == t.id]
    assert len(mine) == 1
    assert mine[0]["total"] == 2
    assert mine[0]["by_type"] == {"substitution": 1, "reschedule": 1, "cancel": 0}


def test_below_threshold_excluded(db):
    t = _u(db, "stats-b@test.local")
    g = _g(db, t.id)
    _req(db, t.id, g.id, "reschedule", datetime(2026, 8, 3, 10, 0))
    rows = get_teacher_request_stats(db, 2026, 8, min_count=2)
    assert all(r["teacher_id"] != t.id for r in rows)


def test_month_boundary_uses_original_datetime(db):
    t = _u(db, "stats-c@test.local")
    g = _g(db, t.id)
    _req(db, t.id, g.id, "reschedule", datetime(2026, 8, 31, 23, 0))   # in August
    _req(db, t.id, g.id, "reschedule", datetime(2026, 9, 1, 0, 30))    # in September
    aug = [r for r in get_teacher_request_stats(db, 2026, 8, min_count=1) if r["teacher_id"] == t.id]
    sep = [r for r in get_teacher_request_stats(db, 2026, 9, min_count=1) if r["teacher_id"] == t.id]
    assert aug and aug[0]["total"] == 1
    assert sep and sep[0]["total"] == 1


def test_december_rolls_over_to_january(db):
    t = _u(db, "stats-d@test.local")
    g = _g(db, t.id)
    _req(db, t.id, g.id, "reschedule", datetime(2026, 12, 15, 10, 0))
    _req(db, t.id, g.id, "reschedule", datetime(2027, 1, 2, 10, 0))
    dec = [r for r in get_teacher_request_stats(db, 2026, 12, min_count=1) if r["teacher_id"] == t.id]
    assert dec and dec[0]["total"] == 1


def test_scope_filter_excludes_out_of_scope_group(db):
    t = _u(db, "stats-e@test.local")
    g_in = _g(db, t.id)
    g_out = _g(db, t.id)
    _req(db, t.id, g_in.id, "reschedule", datetime(2026, 8, 3, 10, 0))
    _req(db, t.id, g_out.id, "reschedule", datetime(2026, 8, 4, 10, 0))
    rows = get_teacher_request_stats(db, 2026, 8, min_count=1, scope_group_ids=[g_in.id])
    mine = [r for r in rows if r["teacher_id"] == t.id]
    assert mine and mine[0]["total"] == 1


def test_empty_scope_returns_nothing(db):
    rows = get_teacher_request_stats(db, 2026, 8, min_count=1, scope_group_ids=[])
    assert rows == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd lms/backend && python -m pytest tests/test_lesson_request_teacher_stats.py -v`
Expected: FAIL with `ImportError: cannot import name 'get_teacher_request_stats'` (or SKIP if no DB).

- [ ] **Step 3: Implement the aggregation function**

In `src/lesson_requests/services.py`, add after `get_group_ids_in_head_teacher_scope` (after line 113). Note `Optional` is already imported (line 6):

```python
def get_teacher_request_stats(
    db: Session,
    year: int,
    month: int,
    min_count: int = 2,
    scope_group_ids: Optional[list[int]] = None,
) -> list[dict]:
    """Per-teacher lesson-request counts for a month, keyed by requester.

    Month bucket is by ``original_datetime`` (the lesson date). All statuses count.
    ``scope_group_ids=None`` → no group filter (admin / head_curator). ``[]`` → empty.
    Returns teachers with ``total >= min_count``, sorted by total desc then name asc.
    """
    if scope_group_ids is not None and len(scope_group_ids) == 0:
        return []

    month_start = datetime(year, month, 1)
    if month == 12:
        next_month = datetime(year + 1, 1, 1)
    else:
        next_month = datetime(year, month + 1, 1)

    query = db.query(LessonRequest).filter(
        LessonRequest.original_datetime >= month_start,
        LessonRequest.original_datetime < next_month,
    )
    if scope_group_ids is not None:
        query = query.filter(LessonRequest.group_id.in_(scope_group_ids))

    rows = query.all()

    # aggregate by requester
    agg: dict[int, dict] = {}
    for r in rows:
        entry = agg.setdefault(
            r.requester_id,
            {"teacher_id": r.requester_id, "total": 0,
             "by_type": {t: 0 for t in VALID_REQUEST_TYPES}},
        )
        entry["total"] += 1
        if r.request_type in entry["by_type"]:
            entry["by_type"][r.request_type] += 1

    result = [e for e in agg.values() if e["total"] >= min_count]
    if not result:
        return []

    # attach names
    teacher_ids = [e["teacher_id"] for e in result]
    names = {
        uid: name
        for uid, name in db.query(UserInDB.id, UserInDB.name).filter(
            UserInDB.id.in_(teacher_ids)
        ).all()
    }
    for e in result:
        e["teacher_name"] = names.get(e["teacher_id"]) or f"#{e['teacher_id']}"

    result.sort(key=lambda e: (-e["total"], e["teacher_name"]))
    return result
```

Also confirm `datetime` is imported at the top of `services.py` — it is (`from datetime import datetime, timezone` at line 5).

- [ ] **Step 4: Run test to verify it passes**

Run: `cd lms/backend && python -m pytest tests/test_lesson_request_teacher_stats.py -v`
Expected: PASS (or SKIP if no DB).

- [ ] **Step 5: Commit**

```bash
cd lms/backend
git add src/lesson_requests/services.py tests/test_lesson_request_teacher_stats.py
git commit -m "feat(lesson-requests): monthly per-teacher request-stats aggregation"
```

---

## Task 3: Backend — schema + endpoint + widened guard

**Files:**
- Modify: `src/lesson_requests/schemas.py` (append schema)
- Modify: `src/lesson_requests/routes.py` (guard + endpoints)
- Test: append to `tests/test_lesson_request_teacher_stats.py`

**Interfaces:**
- Consumes: `get_teacher_request_stats`, `get_group_ids_in_head_teacher_scope`.
- Produces: `GET /lesson-requests/teacher-stats?year=&month=&min_count=` → `List[TeacherRequestStatsSchema]`; guard `_require_admin_head_teacher_or_head_curator`.

- [ ] **Step 1: Add the response schema**

In `src/lesson_requests/schemas.py`, append (the file already imports `BaseModel` and `Optional`):

```python
class TeacherRequestStatsSchema(BaseModel):
    teacher_id: int
    teacher_name: str
    total: int
    by_type: dict   # keys: substitution, reschedule, cancel
```

- [ ] **Step 2: Write the failing endpoint test**

Append to `tests/test_lesson_request_teacher_stats.py`. This drives the route directly through the function (the module gates auth via dependency; here we test the query-layer wiring by calling the endpoint coroutine with a fake user):

```python
import asyncio
from src.lesson_requests import routes as lr_routes


class _FakeUser:
    def __init__(self, role, uid=1):
        self.role = role
        self.id = uid


def test_endpoint_head_curator_sees_all(db):
    t = _u(db, "ep-hc@test.local")
    g = _g(db, t.id)
    _req(db, t.id, g.id, "reschedule", datetime(2026, 8, 3, 10, 0))
    _req(db, t.id, g.id, "cancel", datetime(2026, 8, 9, 10, 0))
    out = asyncio.get_event_loop().run_until_complete(
        lr_routes.teacher_request_stats(
            year=2026, month=8, min_count=2, db=db,
            current_user=_FakeUser("head_curator"),
        )
    )
    mine = [r for r in out if r["teacher_id"] == t.id]
    assert mine and mine[0]["total"] == 2


def test_endpoint_head_teacher_empty_scope_returns_empty(db):
    # a head_teacher with no managed courses has empty scope → []
    ht = _u(db, "ep-ht@test.local", "head_teacher")
    out = asyncio.get_event_loop().run_until_complete(
        lr_routes.teacher_request_stats(
            year=2026, month=8, min_count=1, db=db,
            current_user=_FakeUser("head_teacher", uid=ht.id),
        )
    )
    assert out == []
```

- [ ] **Step 3: Run test to verify it fails**

Run: `cd lms/backend && python -m pytest tests/test_lesson_request_teacher_stats.py -v`
Expected: FAIL with `AttributeError: module ... has no attribute 'teacher_request_stats'` (or SKIP if no DB).

- [ ] **Step 4: Add the guard and endpoint**

In `src/lesson_requests/routes.py`:

(a) Import the new pieces. Add `TeacherRequestStatsSchema` to the `from src.schemas.models import (...)` block (line 12-16) — **only if** it's re-exported there; if not, import from the module directly. Verify first with:

Run: `cd lms/backend && python -c "from src.schemas.models import TeacherRequestStatsSchema"`
- If that succeeds, add `TeacherRequestStatsSchema` to the existing import block.
- If it raises `ImportError`, instead add a new import line: `from src.lesson_requests.schemas import TeacherRequestStatsSchema`.

Add `get_teacher_request_stats` to the `from src.lesson_requests.services import (...)` block (line 19-24).

(b) Add the widened guard next to `_require_admin_or_head_teacher` (after line 41):

```python
def _require_admin_head_teacher_or_head_curator(
    current_user: UserInDB = Depends(get_current_user_dependency),
) -> UserInDB:
    if current_user.role not in ("admin", "head_teacher", "head_curator"):
        raise HTTPException(status_code=403, detail="Admin, head teacher, or head curator access required")
    return current_user
```

(c) Add the endpoint after `list_pending_approval` (after line 257):

```python
@router.get("/teacher-stats", response_model=List[TeacherRequestStatsSchema])
async def teacher_request_stats(
    year: int = Query(..., ge=2000, le=2100),
    month: int = Query(..., ge=1, le=12),
    min_count: int = Query(2, ge=1),
    db: Session = Depends(get_db),
    current_user: UserInDB = Depends(_require_admin_head_teacher_or_head_curator),
):
    """Per-teacher lesson-request counts for a month (by lesson date).

    Head teachers are limited to their course scope; admins and head curators see all.
    Returns only teachers with total >= min_count (default 2).
    """
    scope_group_ids = None
    if current_user.role == "head_teacher":
        scope_group_ids = get_group_ids_in_head_teacher_scope(db, current_user.id)
    return get_teacher_request_stats(db, year, month, min_count, scope_group_ids)
```

Note: `Query` is already imported (routes.py:4).

- [ ] **Step 5: Widen the two existing listing guards to allow head_curator (unscoped)**

In `list_lesson_requests` (line 216-237) and `list_pending_approval` (line 240-257), change the dependency from `_require_admin_or_head_teacher` to `_require_admin_head_teacher_or_head_curator`. The existing `if current_user.role == "head_teacher":` scope blocks stay as-is — head_curator falls through them (no filter), exactly like admin. No other change needed in those two functions.

- [ ] **Step 6: Run tests to verify they pass**

Run: `cd lms/backend && python -m pytest tests/test_lesson_request_teacher_stats.py tests/test_lesson_request_head_curator_access.py -v`
Expected: PASS (or SKIP if no DB).

- [ ] **Step 7: Sanity import check**

Run: `cd lms/backend && python -c "from src.routes import __init__" && python -c "import src.lesson_requests.routes"`
Expected: no error (module imports cleanly).

- [ ] **Step 8: Commit**

```bash
cd lms/backend
git add src/lesson_requests/schemas.py src/lesson_requests/routes.py tests/test_lesson_request_teacher_stats.py
git commit -m "feat(lesson-requests): teacher-stats endpoint + head-curator view access"
```

---

## Task 4: Frontend — API client function + type

**Files:**
- Modify: `src/services/api/lesson-requests.ts`

**Interfaces:**
- Produces:
  ```ts
  export type TeacherRequestStats = {
    teacher_id: number;
    teacher_name: string;
    total: number;
    by_type: { substitution: number; reschedule: number; cancel: number };
  };
  export function getTeacherRequestStats(year: number, month: number, minCount?: number): Promise<TeacherRequestStats[]>;
  ```

- [ ] **Step 1: Add the type and function**

In `src/services/api/lesson-requests.ts`, append:

```ts
export type TeacherRequestStats = {
  teacher_id: number;
  teacher_name: string;
  total: number;
  by_type: { substitution: number; reschedule: number; cancel: number };
};

export async function getTeacherRequestStats(
  year: number,
  month: number,
  minCount: number = 2,
): Promise<TeacherRequestStats[]> {
  try {
    const response = await api.get('/lesson-requests/teacher-stats', {
      params: { year, month, min_count: minCount },
    });
    return response.data;
  } catch (error) {
    console.error('Failed to get teacher request stats:', error);
    throw error;
  }
}
```

- [ ] **Step 2: Verify it type-checks**

Run: `cd lms/frontend && npx tsc --noEmit -p tsconfig.json`
Expected: no new errors referencing `lesson-requests.ts`. (If the repo has pre-existing unrelated tsc errors, confirm none are newly introduced by this file.)

- [ ] **Step 3: Commit**

```bash
cd lms/frontend
git add src/services/api/lesson-requests.ts
git commit -m "feat(lesson-requests): API client for teacher-stats endpoint"
```

---

## Task 5: Frontend — TeacherRescheduleStatsPanel component

**Files:**
- Create: `src/pages/admin/TeacherRescheduleStatsPanel.tsx`

**Interfaces:**
- Consumes: `getTeacherRequestStats`, `TeacherRequestStats` from `../../services/api/lesson-requests`; UI primitives `Card`, `Table`, `Input` (same import paths used in `LessonRequestManagement.tsx`).
- Produces: `export default function TeacherRescheduleStatsPanel(): JSX.Element`.

- [ ] **Step 1: Create the component**

Create `src/pages/admin/TeacherRescheduleStatsPanel.tsx`:

```tsx
import { useState, useEffect } from 'react';
import { getTeacherRequestStats, type TeacherRequestStats } from '../../services/api/lesson-requests';
import { Input } from '../../components/ui/input';
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '../../components/ui/table';
import { Card, CardContent, CardHeader, CardTitle, CardDescription } from '../../components/ui/card';

/** "2026-08" default = current month, computed without pulling in a date lib. */
function currentYearMonth(): string {
  const now = new Date();
  const y = now.getFullYear();
  const m = String(now.getMonth() + 1).padStart(2, '0');
  return `${y}-${m}`;
}

export default function TeacherRescheduleStatsPanel() {
  const [month, setMonth] = useState<string>(currentYearMonth());
  const [rows, setRows] = useState<TeacherRequestStats[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const [yStr, mStr] = month.split('-');
    const year = Number(yStr);
    const mon = Number(mStr);
    if (!year || !mon) return;
    let cancelled = false;
    setLoading(true);
    setError(null);
    getTeacherRequestStats(year, mon, 2)
      .then(data => { if (!cancelled) setRows(data); })
      .catch(() => { if (!cancelled) setError('Не удалось загрузить статистику'); })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [month]);

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-end gap-3">
        <label className="flex flex-col gap-1 text-xs text-muted-foreground">
          Месяц
          <Input
            type="month"
            className="h-9 w-[180px]"
            value={month}
            onChange={e => setMonth(e.target.value)}
          />
        </label>
      </div>

      {error && (
        <div className="rounded-md border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-800">
          {error}
        </div>
      )}

      <Card>
        <CardHeader className="px-6 py-4 border-b">
          <CardTitle className="text-lg">Учителя с 2+ обращениями за месяц</CardTitle>
          <CardDescription>
            {loading
              ? 'Загрузка…'
              : `Замены, переносы и отмены. Учителей: ${rows.length}`}
          </CardDescription>
        </CardHeader>
        <CardContent className="p-0">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Учитель</TableHead>
                <TableHead className="text-right w-[90px]">Всего</TableHead>
                <TableHead className="text-right w-[110px]">Замена</TableHead>
                <TableHead className="text-right w-[110px]">Перенос</TableHead>
                <TableHead className="text-right w-[110px]">Отмена</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {loading ? (
                <TableRow>
                  <TableCell colSpan={5} className="h-24 text-center text-muted-foreground">
                    Загрузка…
                  </TableCell>
                </TableRow>
              ) : rows.length === 0 ? (
                <TableRow>
                  <TableCell colSpan={5} className="h-24 text-center text-muted-foreground">
                    Нет учителей с 2+ обращениями за этот месяц
                  </TableCell>
                </TableRow>
              ) : (
                rows.map(r => (
                  <TableRow key={r.teacher_id} className="font-medium">
                    <TableCell>{r.teacher_name}</TableCell>
                    <TableCell className="text-right font-bold">{r.total}</TableCell>
                    <TableCell className="text-right">{r.by_type.substitution}</TableCell>
                    <TableCell className="text-right">{r.by_type.reschedule}</TableCell>
                    <TableCell className="text-right">{r.by_type.cancel}</TableCell>
                  </TableRow>
                ))
              )}
            </TableBody>
          </Table>
        </CardContent>
      </Card>
    </div>
  );
}
```

- [ ] **Step 2: Verify it type-checks**

Run: `cd lms/frontend && npx tsc --noEmit -p tsconfig.json`
Expected: no new errors referencing `TeacherRescheduleStatsPanel.tsx`.

- [ ] **Step 3: Commit**

```bash
cd lms/frontend
git add src/pages/admin/TeacherRescheduleStatsPanel.tsx
git commit -m "feat(lesson-requests): TeacherRescheduleStatsPanel stats view"
```

---

## Task 6: Frontend — tab switcher in LessonRequestManagement

**Files:**
- Modify: `src/pages/admin/LessonRequestManagement.tsx`

**Interfaces:**
- Consumes: `TeacherRescheduleStatsPanel` (default export).
- Produces: two tabs `Запросы` / `Статистика по учителям`; the existing list moves under the `Запросы` tab. Active tab persisted in the URL param `tab` (`requests` | `stats`), default `requests`.

- [ ] **Step 1: Import the panel**

At the top of `src/pages/admin/LessonRequestManagement.tsx`, after the existing imports (after line 17), add:

```tsx
import TeacherRescheduleStatsPanel from './TeacherRescheduleStatsPanel';
```

- [ ] **Step 2: Read the active tab from the URL**

The component already uses `useSearchParams` and has a `setParam` helper (used as `setParam('status', ...)`). Immediately after the existing `const inconsistentCount = ...` line (line 253), add:

```tsx
  const activeTab = searchParams.get('tab') === 'stats' ? 'stats' : 'requests';
```

- [ ] **Step 3: Insert the tab switcher and gate the existing body**

Locate the `return (` at line 255 and the outer `<div className="space-y-6">` at line 256. Insert the tab switcher as the first child of that div (before the existing `<div className="flex flex-wrap items-start justify-between gap-4">` header at line 257):

```tsx
      <div className="flex rounded-md shadow-sm w-fit">
        {([
          ['requests', 'Запросы'],
          ['stats', 'Статистика по учителям'],
        ] as [string, string][]).map(([value, label], idx, arr) => {
          const isActive = activeTab === value;
          return (
            <button
              key={value}
              onClick={() => setParam('tab', value)}
              className={`px-4 py-2 text-sm font-medium border transition-colors
                ${idx === 0 ? 'rounded-l-md' : ''}
                ${idx === arr.length - 1 ? 'rounded-r-md' : ''}
                ${idx !== 0 ? '-ml-px' : ''}
                ${isActive
                  ? 'bg-primary text-primary-foreground border-primary z-10'
                  : 'bg-background text-foreground border-input hover:bg-accent hover:text-accent-foreground'
                }`}
            >
              {label}
            </button>
          );
        })}
      </div>

      {activeTab === 'stats' ? (
        <TeacherRescheduleStatsPanel />
      ) : (
```

Then, at the very end of the existing body — immediately before the closing `</div>` that matches the outer `<div className="space-y-6">` (the last two lines of the JSX return) — add the closing `)}` for the ternary:

```tsx
      )}
    </div>
```

Verification of brace balance is done by the type-check in Step 4. If the existing return's final lines are hard to match, wrap everything from the original header `<div className="flex flex-wrap items-start justify-between gap-4">` through the last `</Card>` in the ternary's `(...)` branch — that entire block is the "requests" view.

- [ ] **Step 4: Verify it type-checks and builds**

Run: `cd lms/frontend && npx tsc --noEmit -p tsconfig.json && npm run build`
Expected: type-check clean (no new errors) and build succeeds.

- [ ] **Step 5: Commit**

```bash
cd lms/frontend
git add src/pages/admin/LessonRequestManagement.tsx
git commit -m "feat(lesson-requests): tabs — requests + teacher stats"
```

---

## Task 7: Frontend — head-curator route + sidebar entry

**Files:**
- Modify: `src/routes/Router.tsx:602-608`
- Modify: `src/components/Sidebar.tsx:105`

**Interfaces:**
- Consumes: existing `HeadTeacherLessonRequestsPage`, `ProtectedRoute`, sidebar item tuple format.
- Produces: `head_curator` may reach `/head-teacher/lesson-requests` and sees its sidebar entry.

- [ ] **Step 1: Allow head_curator on the route**

In `src/routes/Router.tsx`, change line 603 from:

```tsx
            <ProtectedRoute allowedRoles={['head_teacher']}>
```

to:

```tsx
            <ProtectedRoute allowedRoles={['head_teacher', 'head_curator']}>
```

(within the `/head-teacher/lesson-requests` route block at lines 602-608).

- [ ] **Step 2: Show the sidebar entry for head_curator**

In `src/components/Sidebar.tsx`, change line 105 from:

```tsx
    ['/head-teacher/lesson-requests', 'Lesson Requests', ArrowLeftRight, lessonRequestCount, ['head_teacher'], 'head-lesson-requests-nav', 'primary'],
```

to (adds `head_curator` to the allowed-roles array, and localizes the label like sibling curator entries):

```tsx
    ['/head-teacher/lesson-requests', ['head_curator', 'curator'].includes(_userRole || '') ? 'Заявки по урокам' : 'Lesson Requests', ArrowLeftRight, lessonRequestCount, ['head_teacher', 'head_curator'], 'head-lesson-requests-nav', 'primary'],
```

- [ ] **Step 3: Verify it type-checks and builds**

Run: `cd lms/frontend && npx tsc --noEmit -p tsconfig.json && npm run build`
Expected: type-check clean, build succeeds.

- [ ] **Step 4: Manual smoke check**

Run the app (`cd lms/frontend && npm run dev`, backend running separately). Log in as a `head_curator`:
- The sidebar shows "Заявки по урокам".
- Opening it lands on `/head-teacher/lesson-requests` (not redirected/403).
- The `Запросы` tab lists all requests across programs; approve/reject controls are present and work.
- The `Статистика по учителям` tab shows the month picker (current month) and only teachers with ≥2 requests, with the per-type breakdown.
Log in as a `head_teacher`: same view, but request list + stats limited to their course scope.

- [ ] **Step 5: Commit**

```bash
cd lms/frontend
git add src/routes/Router.tsx src/components/Sidebar.tsx
git commit -m "feat(lesson-requests): head curators access lesson-requests view"
```

---

## Self-Review

**Spec coverage:**
- New stats endpoint (spec Unit B) → Task 2 (aggregation) + Task 3 (schema/endpoint). ✓
- Attribution by `requester_id`, month by `original_datetime`, all statuses, `min_count=2` → Task 2 tests + impl. ✓
- Broaden auth to head_curator, unscoped, full powers → Task 1 (resolve) + Task 3 Steps 4-5 (guard + listing). ✓
- Frontend separate tab → Task 6. ✓
- Panel: month picker default current, sorted, ≥2 only, per-type breakdown, empty state → Task 5. ✓
- Reuse `/head-teacher/lesson-requests` route + sidebar for head_curator → Task 7. ✓
- Out-of-scope items (threshold UI, export, alerts, per-type toggle) → not implemented. ✓

**Placeholder scan:** No TBD/TODO; every code step shows full code; commands have expected output. ✓

**Type consistency:** `get_teacher_request_stats(db, year, month, min_count, scope_group_ids)` used identically in Task 2 (def), Task 3 (endpoint call). `TeacherRequestStatsSchema` fields `{teacher_id, teacher_name, total, by_type}` match the dict returned by the service and the TS `TeacherRequestStats` type (Task 4) and the panel's field access (Task 5: `r.by_type.substitution/reschedule/cancel`). `getTeacherRequestStats(year, month, minCount)` defined in Task 4, consumed in Task 5. `_require_admin_head_teacher_or_head_curator` defined in Task 3 Step 4(b), applied in Steps 4(c) and 5. ✓

**Note on frontend variant:** `LessonRequestManagement` keeps `variant="head_teacher"` for curators (both have full powers), so the existing `isHeadTeacher`-gated UI (hides the "Все" status button, changes the subtitle) applies to curators too. This is acceptable per the spec (curators = full head-teacher-like powers). If product later wants curators to see the "Все" button, add `head_curator` handling to the variant then.
