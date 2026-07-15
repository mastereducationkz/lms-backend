"""Functional index on lower(users.email) for the per-request auth lookup

Every authenticated request resolves the bearer token to a user via
``WHERE lower(email) = lower(:email)`` (src/auth/user_resolve.py). The existing
plain b-tree index on ``email`` cannot serve that predicate, so each request paid
a sequential scan of ``users``. A functional index makes the hottest query in the
system an index lookup.

Not UNIQUE: the case-insensitive uniqueness of existing rows is not guaranteed
(``ix_users_email`` is unique on the raw value only), and this migration must not
fail on legacy data.

Revision ID: p16_users_lower_email_idx
Revises: p15_perf_indexes
Create Date: 2026-07-15
"""
from alembic import op


revision = 'p16_users_lower_email_idx'
down_revision = 'p15_perf_indexes'
branch_labels = None
depends_on = None


def upgrade():
    if op.get_bind().dialect.name != 'postgresql':
        return
    with op.get_context().autocommit_block():
        op.execute('CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_users_lower_email ON users (lower(email))')


def downgrade():
    if op.get_bind().dialect.name != 'postgresql':
        return
    with op.get_context().autocommit_block():
        op.execute('DROP INDEX CONCURRENTLY IF EXISTS idx_users_lower_email')
