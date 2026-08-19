# Design: Teacher reschedule-stats panel + head-curator access to lesson requests

Date: 2026-08-19
Status: Approved (pending spec review)

## Problem

Head teachers (and admins) can view lesson change requests — `Замена` (substitution),
`Перенос` (reschedule), `Отмена` (cancel) — but there is no way to see, per month,
**which teacher made 2 or more requests**. Management wants this as an oversight signal
(a teacher repeatedly moving/cancelling lessons).

Two gaps to close:

1. No aggregated "teacher × month" statistic exists anywhere.
2. Head **curators** currently have **no access** to the lesson-requests view at all —
   not in the backend guard, the frontend route, or the sidebar — yet they are expected
   to use this oversight feature.

## Decisions (locked with stakeholder)

| Question | Decision |
|---|---|
| What counts toward the ≥2 threshold | **All request types** (substitution + reschedule + cancel) combined. Per-type breakdown always displayed. No viewer toggle to change what counts. |
| Which teacher a request is attributed to | The **requester** (`requester_id`) — the teacher who *made* the request. |
| Month basis | **Lesson date** (`original_datetime`), i.e. the month of the lesson being changed, not when the request was submitted. |
| Threshold | Fixed at **≥ 2** (endpoint exposes `min_count`, default 2). |
| Statuses counted | **All statuses** (pending / pending_teacher / approved / rejected). A submitted request counts regardless of how it was resolved — the point is that the teacher *made* it. |
| Head curator access | **Add** curators to the lesson-requests view. |
| Head curator permissions | **Full** — may approve/reject like a head teacher. |
| Head curator scope | **All requests**, unscoped (admin-like). |
| Panel presentation | A **separate tab** in the lesson-requests view (`Запросы` / `Статистика по учителям`). |
| Curator route | **Reuse** the existing `/head-teacher/lesson-requests` route + sidebar entry (allow `head_curator`). |

## Architecture

The change spans three units, each independently testable.

### Unit A — Backend: broaden authorization to `head_curator`

File: `src/lesson_requests/routes.py`

- Rename/replace the guard so it admits curators. Introduce
  `_require_admin_head_teacher_or_head_curator(current_user)` allowing
  `("admin", "head_teacher", "head_curator")`. Apply it to the listing/stats endpoints
  (`GET /`, `GET /pending-approval`, and the new stats endpoint).
- Scoping rule in each listing query:
  - `head_teacher` → existing `get_group_ids_in_head_teacher_scope(db, user.id)` filter (unchanged).
  - `head_curator` → **no** group filter (sees all), same as `admin`.
- File: `src/lesson_requests/services.py` — `user_can_resolve_request(db, user, group_id)`
  gains a branch: `if user.role == "head_curator": return True` (full approve/reject).

Interface: unchanged request/response shapes for the existing endpoints; only the set of
roles that pass authorization widens.

### Unit B — Backend: new stats endpoint

File: `src/lesson_requests/routes.py`

```
GET /lesson-requests/teacher-stats?year=YYYY&month=MM&min_count=2
```

- Guard: `_require_admin_head_teacher_or_head_curator`.
- Query: `LessonRequest` rows where `original_datetime` is within `[month_start, month_start + 1 month)`.
  For `head_teacher`, additionally filter `group_id.in_(scope_group_ids)` (return `[]` if
  scope empty). `admin` / `head_curator` see all.
- Aggregate in Python (row counts are small per month) keyed by `requester_id`:
  - `teacher_id`, `teacher_name` (resolved via the existing user lookup / enrich helper),
  - `total` (all types),
  - `by_type`: `{ "substitution": n, "reschedule": n, "cancel": n }`.
- Return only teachers with `total >= min_count`, sorted by `total` desc then name asc.
- Response schema (new Pydantic model `TeacherRequestStatsSchema` in
  `src/lesson_requests/schemas.py`):

```python
class TeacherRequestStatsSchema(BaseModel):
    teacher_id: int
    teacher_name: str
    total: int
    by_type: dict[str, int]   # keys: substitution, reschedule, cancel
```

Endpoint returns `List[TeacherRequestStatsSchema]`.

Month-boundary handling: compute `month_start = datetime(year, month, 1)`; `next_month` by
incrementing month with year rollover. Use naive/utc consistent with how `original_datetime`
is stored elsewhere in this module (it is a naive `DateTime`).

### Unit C — Frontend: stats tab + curator wiring

Files:
- `src/services/api/lesson-requests.ts` — add
  `getTeacherRequestStats(year, month, minCount = 2)` → `GET /lesson-requests/teacher-stats?...`.
- `src/pages/admin/LessonRequestManagement.tsx` — introduce a top-level tab switcher:
  - Tab 1 `Запросы` — the existing list UI (unchanged).
  - Tab 2 `Статистика по учителям` — new `TeacherRescheduleStatsPanel`.
- New component `TeacherRescheduleStatsPanel` (co-located or in `src/pages/admin/`):
  - Month picker (`<input type="month">`), default = current month.
  - Table: `Учитель | Всего | Замена | Перенос | Отмена`, sorted by `Всего` desc.
    Only rows with `Всего ≥ 2` (server already filters); each row visually emphasized.
  - Empty state: «Нет учителей с 2+ обращениями за этот месяц».
  - Loading + error states consistent with the existing page.
- `src/routes/Router.tsx` — allow `head_curator` on `/head-teacher/lesson-requests`
  (`allowedRoles={['head_teacher', 'head_curator']}`).
- `src/components/Sidebar.tsx` — show the lesson-requests menu item for `head_curator`
  as well as `head_teacher` (line ~105 role list).

The `variant` prop on `LessonRequestManagement` stays `"head_teacher"` for both head
teachers and curators (both get full powers), so no new variant is required. Confirm in
implementation that no head-teacher-only UI branch would wrongly hide actions from curators.

## Data flow

1. Curator/head-teacher opens `/head-teacher/lesson-requests`.
2. Selects the `Статистика по учителям` tab and a month.
3. Frontend calls `GET /lesson-requests/teacher-stats?year&month`.
4. Backend filters `LessonRequest` by `original_datetime` month (+ head-teacher scope),
   aggregates by `requester_id`, returns teachers with `total ≥ 2`.
5. Panel renders the sorted table with per-type breakdown.

## Error handling

- Invalid/missing `year`/`month` → 422 (FastAPI query validation; constrain `month` 1–12).
- Empty result / head-teacher with empty scope → `200 []`, panel shows empty state.
- Unauthorized role → 403 (existing guard behavior).
- Frontend: network error → inline error message, retryable.

## Testing

Backend (`lms/backend` pytest; needs Postgres per project convention):
- Aggregation: seed requests across two teachers/months/types; assert only `total ≥ 2`
  teachers returned, correct `by_type`, correct month boundary (a lesson on the 1st vs the
  last day of adjacent months lands in the right bucket).
- Attribution by `requester_id` (not lesson teacher).
- Role access: `admin`, `head_teacher` (scoped), `head_curator` (all) → 200; `teacher` → 403.
- Head-teacher scope: teacher's requests outside scope excluded.
- `user_can_resolve_request` returns True for `head_curator`.

Frontend:
- Panel renders rows sorted desc, highlights ≥2, shows empty state, month picker refetches.
- Curator role sees the sidebar entry and can load the page.

## Out of scope (YAGNI)

- Configurable threshold UI (fixed ≥2; only backend `min_count` param exists).
- CSV/XLSX export of the stats.
- Notifications/alerts when a teacher crosses the threshold.
- Per-type threshold toggle (explicitly declined).
- Head-curator-specific narrower scoping (explicitly all-requests).
