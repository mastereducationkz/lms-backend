# Trial Access for Sales Prospects — Design Spec

**Date:** 2026-07-19
**Status:** Approved by product owner (design review in Claude Code session)
**Scope:** `lms-backend` (primary) + `lms-front` (admin page, prospect UX). CRM integration explicitly out of scope (see §10).

## 1. Problem & goals

The sales team consults prospects who are not yet clients. They need to grant a prospect
time-boxed access to a **selected set of lessons ("units") of one course**, after which access
is revoked automatically.

**Requirements (confirmed in design review):**

- Sales selects the lessons per grant — 1, 2, or any selection within one course.
- Grants are created from the **LMS admin UI** by `admin`/`head_curator` (sales requests it or holds head_curator accounts). CRM-side granting comes later.
- Sales sets a **custom deadline** (form pre-fills now + 24h) and can edit it any time — extend, shorten, or set to the past (which is an immediate revoke).
- After expiry, **login keeps working but content locks**; the prospect sees a "trial ended — contact us" screen. This preserves an upsell surface.
- Trial users must be **distinguishable from real students** everywhere (lists, analytics, sync, curator workflows).
- Prospects get the **full student experience** while the trial is active (normal dashboard and sidebar) — not a stripped-down shell. Dashboard widgets that would be empty for a prospect show **clearly-labeled placeholder/sample content** instead of empty states, so the platform demos well.
- Trial users never enter the Assignment-Zero funnel.

**Non-goals:** public/unauthenticated preview, self-serve signup, CRM UI, payment/conversion
automation, notifying sales on expiry (future).

## 2. Chosen approach

A dedicated **`TrialAccess` entity**: one row per trial that *is* the access. No `Enrollment`
row, no group membership, no `ManualLessonUnlock` rows — revocation can never leave residue.
Rejected alternatives: (A) stretching `ManualLessonUnlock` + enrollment with expiry columns —
trial state smeared across three tables, weak audit, still needs custom hard enforcement;
(C) accountless magic-link preview — requires a parallel auth/UI path, far too big for a sales tool.

Key property: **expiry is evaluated at request time**, so revocation is exact to the second and
needs no background job (`get_current_user_dependency` already re-resolves the user from the DB
on every request, so the still-valid 24h JWT is irrelevant).

## 3. Data model (new domain module `src/trials/`)

Follows the existing domain-module pattern (`models.py`, `schemas.py`, `services.py`, `routes/`).

### 3.1 `users` — one new column

| column | type | notes |
|---|---|---|
| `is_trial` | boolean, NOT NULL, default `false`, indexed | Trial prospects are real users with `role="student"`; the flag makes them filterable wherever real students are listed, counted, or synced. |

### 3.2 `trial_accesses` — the grant

| column | type | notes |
|---|---|---|
| `id` | int PK | |
| `user_id` | FK → `users.id`, NOT NULL, indexed | the prospect |
| `course_id` | FK → `courses.id`, NOT NULL | one course per grant |
| `lesson_ids` | JSONB array of lesson ids, NOT NULL | the allowlist sales selected; validated on write to belong to `course_id`; non-empty |
| `expires_at` | timestamptz, NOT NULL | sales-set deadline, editable |
| `status` | varchar: `active` / `expired` / `revoked` / `converted` | `expired` is bookkeeping only (see §7); enforcement never trusts `status` alone |
| `granted_by` | FK → `users.id` | audit |
| `prospect_note` | text, nullable | phone / CRM ref / context |
| `created_at`, `updated_at`, `revoked_at` | timestamps | |

- **Partial unique index**: one `active` grant per `(user_id, course_id)`. A prospect may hold
  grants on different courses; a new grant on the same course is allowed once the old one is
  inactive (re-trial).
- JSONB allowlist instead of a child table: the list is small, always read whole, never queried
  from the lesson side, and editing the selection is a single-row update.
- **Definition of "active" used by all enforcement:** `status == "active" AND now() < expires_at`.

### 3.3 Migration

One Alembic revision: add `users.is_trial`, create `trial_accesses` + indexes. Mirror in models
so `create_all()` stays consistent (repo convention). Export models via the `src/schemas/models`
shim like other domains.

## 4. Access enforcement (backend)

Today, lesson-level locks are **advisory**: `GET /courses/lessons/{id}/check-access`
(`src/courses/routes/courses.py:1124`) computes `{accessible, reason}` for the UI, but the
content-serving endpoints (`/courses/lessons/{id}/steps` at `courses.py:1480`,
`/courses/steps/{id}` at `courses.py:1603`) gate only on course-level access. Trial users get a
**hard gate**.

New helper in `src/trials/services.py`:

```python
get_active_trial(db, user, course_id=None) -> TrialAccess | None
```

Branch on `user.is_trial` — real students never enter trial code paths; their behavior is
byte-for-byte unchanged. Enforcement points:

1. **Course visibility** — `check_course_access` (`src/utils/permissions.py:53`) and
   `get_user_courses` / `check_user_course_access` (`src/utils/course_access.py`): a trial user
   has access to a course iff an active grant exists for it.
2. **`GET /courses/{id}/modules`** (`courses.py:473`): for trial users, set
   `is_accessible=True` only on allowlisted lessons. The existing frontend then renders lock
   icons on everything else with no changes.
3. **`GET /courses/lessons/{id}/check-access`**: non-allowlisted → `{accessible: false,
   reason: "Not included in your trial"}`; past deadline → `{accessible: false, reason:
   "Your trial has ended"}` (localized like existing reasons).
4. **Hard gate** on `GET /courses/lessons/{id}/steps` and `GET /courses/steps/{id}`:
   HTTP 403 unless the lesson is in an active grant's allowlist. This closes the advisory-lock
   hole for trial users.

Auxiliary surfaces (assignments, messages, events, gamification, leaderboards, curator
journals): trial users are naturally absent from group-scoped queries (they have no group).
Where a query enumerates students without a group join, handle `is_trial` explicitly:
`GET /admin/users` gains an `is_trial` filter and the admin table a "Trial" badge;
leaderboard/gamification aggregations and Excel/Sheets exports exclude `is_trial` users.
Trial users **can** record
progress (`StudentProgress`/`StepProgress`) on allowlisted lessons — useful sales signal, and
it survives conversion.

## 5. Grant lifecycle API (`/trials`)

`src/trials/routes/trials.py`, all endpoints gated `require_admin_or_head_curator`. Register in
`src/routes/__init__.py::register_routes()`; add a `"trials"` entry to
`_MUTATION_INVALIDATION_RULES` in `src/app.py`.

| Endpoint | Behavior |
|---|---|
| `POST /trials` | One-shot grant. Body: `email`, `name`, `course_id`, `lesson_ids`, `expires_at`, `prospect_note?`, `send_invite=true`. Creates the user (`role="student"`, `is_trial=true`, generated password via existing `generate_password()`, generated `student_id`, and `assignment_zero_completed=true` so the Assignment-Zero funnel never triggers — no frontend gate change needed) **or** reuses an existing **trial** user with the same email (new course grant). Email belongs to a real (non-trial) user → **409**, never converts a real account. Existing trial user already has an active grant on that course → **409** with "edit the existing grant" hint. Sends credentials + deadline via the existing invite-email machinery (`send_invite_email`), with the deadline stated. Response includes `generated_password` when a user was created (mirrors `POST /admin/users/single`). |
| `GET /trials` | Paginated list with computed status, filters (`status`, `course_id`), search by email/name. |
| `PATCH /trials/{id}` | Edit `expires_at`, `lesson_ids` (validated against course), `prospect_note`. Takes effect on the prospect's next request. |
| `POST /trials/{id}/revoke` | `status="revoked"`, `revoked_at=now()`. Immediate. |
| `POST /trials/{id}/resend-invite` | Regenerates password, re-sends the invite email. |
| `POST /trials/{id}/convert` | Prospect became a client: `status="converted"`, clears `user.is_trial`. Admin then enrolls them through the normal group flow; trial progress history survives. |

Matching frontend client module: `lms-front/src/services/api/trials.ts`, exported through the
`apiClient` aggregate.

## 6. Frontend

### 6.1 Admin: `/trial-access` page

New page for `admin` + `head_curator` (route in `Router.tsx`, nav item in `Sidebar.tsx`),
modeled on `pages/admin/ManualUnlocksPage.tsx` (which already implements the course → module →
lesson tree picker):

- **Trials table**: prospect (name/email), course, lesson count, deadline with live countdown,
  status chip (`active` / `expired` / `revoked` / `converted`), granted-by, note. Row actions:
  edit deadline, edit lessons, revoke now, resend invite, convert.
- **"Grant trial" dialog**: email + name + note → course select → module/lesson tree with
  checkboxes (any selection, ≥1) → deadline picker pre-filled `now + 24h` → submit → show
  generated credentials + confirmation that the invite email was sent.

### 6.2 Prospect experience

- Logs in with emailed credentials via the normal login page. No new auth path; tokens/cookies
  unchanged.
- `/auth/me` payload gains `is_trial` and, when an active grant exists, `trial_expires_at`
  (earliest active deadline).
- **Full student experience**: normal dashboard, sidebar, and student pages. No Assignment-Zero
  funnel (handled at creation time, §5 — no `ProtectedRoute` change needed). Trial-specific
  behaviors:
  - **Placeholder dashboard content**: when `user.is_trial`, `StudentDashboard` widgets that
    would otherwise be empty (assignments, events, streak/points, leaderboard, etc.) render
    curated static sample data instead, each clearly marked with a "Sample data" badge so the
    prospect knows it's illustrative. Frontend-only static content — no backend fake data, no
    effect on real students. The trial course and the prospect's real progress in it are shown
    as-is (never placeholder).
  - A slim persistent banner while a grant is active: "Trial access — ends in 21h 14m"
    (countdown from `trial_expires_at`).
- **After expiry/revoke** (`is_trial` and no active grant): protected pages render a full-screen
  "Your trial has ended — contact us to continue" panel with sales contact details instead of
  page content. Login continues to work. If the deadline passes mid-session, the next API call
  returns 403 / `accessible:false` and the app transitions to the same panel (the 403 handler
  also clears the local API request cache).

## 7. Background job (bookkeeping only)

`TrialStatusJob` following the `CuratorTaskScheduler` pattern (`threading.Thread` daemon loop,
singleton, own `SessionLocal()` per tick, ~5 min interval), hosted **only** in the scheduler
container (`src/services/run_scheduler.py`) so API workers never double-run it. Each tick:
`UPDATE trial_accesses SET status='expired' WHERE status='active' AND expires_at <= now()`.
This keeps list views and audit truthful without computing status on read. If the job is down,
nothing leaks — enforcement is request-time (§2, §4).

## 8. Caching

- **Backend Redis cache**: skip cache decorators for `is_trial` users on the gated course/module
  /lesson endpoints (trial users are a handful of people; caching them risks serving content
  past the deadline for the TTL duration). Real users' caching is untouched.
- **Mutations**: the `"trials"` invalidation rule (§5) plus the skip-for-trial-users rule cover
  staleness; the frontend clears its request cache on the trial-expired transition (§6.2).

## 9. Edge cases

| Case | Behavior |
|---|---|
| Deadline edited into the past | Allowed — it *is* "revoke at time X". |
| Real student's email submitted to `POST /trials` | 409; never converts a real account to trial. |
| Duplicate active grant (same user + course) | 409 (partial unique index + service check); admin edits the existing grant instead. |
| Lesson deleted / moved out of the course while granted | Allowlist entries are validated on write; on read, ids that no longer resolve within the course are ignored (lesson simply shows locked). |
| Trial user tries forgot/reset password | Works — they are normal users. |
| SSO / CRM / sync | Trial users are local-only: excluded (via `is_trial`) from Zitadel provisioning, the student-sync outbox, and any exporter that enumerates students. |
| Scheduler container down | Access still dies exactly at `expires_at` (request-time enforcement); only the `status` column lags. |
| Grant to a course with `release_schedule="weekly"` or special-group caps | Trial branch bypasses drip/cap logic entirely — the allowlist is the sole authority for trial users. |

## 10. Out of scope (future)

- `POST /internal/crm/grant-trial` service-key endpoint so sales grants from the CRM — the
  service layer in `src/trials/services.py` is written so this is a thin wrapper.
- Notifying sales (email/Telegram) when a trial expires or a prospect finishes their lessons.
- Trial analytics dashboard (conversion funnel).

## 11. Testing

Backend pytest (existing savepoint-fixture setup, `lms_test` DB):

- **Enforcement matrix**: allowlisted lesson steps served; non-allowlisted → 403; after
  `expires_at` → 403; revoked → 403; other courses invisible in course list; real student
  behavior unchanged (regression guard); modules response marks only allowlisted lessons
  accessible.
- **Grant API**: role gating (student/curator/teacher → 403); create-user vs reuse-trial-user
  vs real-user 409; duplicate-active 409; created user has `is_trial=true` and
  `assignment_zero_completed=true`; PATCH deadline takes effect (before/after read);
  revoke immediate; convert clears `is_trial` and sets status.
- **Job**: flips `active → expired` only past deadline.
- Frontend: `npm run build` type-checks (`strict`); manual QA of the admin page + prospect flow.
