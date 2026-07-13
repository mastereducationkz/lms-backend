"""Add missing indexes on hot dashboard/query columns

Performance audit found full-table scans on three tables that are filtered on nearly every
student/teacher/curator dashboard load, none of which had covering indexes:

  * student_progress   — filtered by user_id / course_id and ordered by last_accessed on every
                         teacher dashboard (admin/routes/dashboard.py) and student overview.
  * assignments        — filtered by lesson_id.in_ / group_id.in_ and sorted by due_date across
                         dashboard/curator endpoints.
  * course_group_access — filtered by course_id / group_id on nearly every access-resolution call
                         (utils/course_access.py, dashboard, curator).

Indexes are built CONCURRENTLY (inside an autocommit block, so they don't hold a lock on these
live tables during deploy) and IF NOT EXISTS (idempotent / safe to re-run). Postgres-only; skipped
on sqlite (test) runs, matching the other p-series migrations.

Revision ID: p15_perf_indexes
Revises: p14_member_sync_touch
Create Date: 2026-07-13
"""
from alembic import op


revision = 'p15_perf_indexes'
down_revision = 'p14_member_sync_touch'
branch_labels = None
depends_on = None


# (index name, table, column list) — kept declarative so upgrade/downgrade stay in sync.
INDEXES = [
    ('idx_student_progress_user_course', 'student_progress', '(user_id, course_id)'),
    ('idx_student_progress_course_last_accessed', 'student_progress', '(course_id, last_accessed)'),
    ('idx_assignments_lesson_id', 'assignments', '(lesson_id)'),
    ('idx_assignments_group_id', 'assignments', '(group_id)'),
    ('idx_assignments_due_date', 'assignments', '(due_date)'),
    ('idx_course_group_access_course', 'course_group_access', '(course_id)'),
    ('idx_course_group_access_group', 'course_group_access', '(group_id)'),
]


def upgrade():
    if op.get_bind().dialect.name != 'postgresql':
        return
    # CREATE INDEX CONCURRENTLY cannot run inside a transaction; autocommit_block suspends the
    # migration transaction so each index builds without locking writes on the table.
    with op.get_context().autocommit_block():
        for name, table, cols in INDEXES:
            op.execute(f'CREATE INDEX CONCURRENTLY IF NOT EXISTS {name} ON {table} {cols}')


def downgrade():
    if op.get_bind().dialect.name != 'postgresql':
        return
    with op.get_context().autocommit_block():
        for name, _table, _cols in INDEXES:
            op.execute(f'DROP INDEX CONCURRENTLY IF EXISTS {name}')
