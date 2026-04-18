"""backfill course_type and groups.program_type for legacy records

Revision ID: u5v6w7x8y9z1
Revises: t4u5v6w7x8y9z0
Create Date: 2026-04-18 17:30:00.000000
"""

from typing import Sequence, Union

from alembic import op


revision: str = "u5v6w7x8y9z1"
down_revision: Union[str, Sequence[str], None] = "t4u5v6w7x8y9z0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1) Backfill courses.course_type by title/description for old rows
    op.execute(
        """
        UPDATE courses
        SET course_type = 'ielts'
        WHERE course_type = 'general_english'
          AND (
            coalesce(title, '') ~* '\\mielts\\M'
            OR coalesce(description, '') ~* '\\mielts\\M'
          )
        """
    )

    op.execute(
        """
        UPDATE courses
        SET course_type = 'sat'
        WHERE course_type = 'general_english'
          AND (
            coalesce(title, '') ~* '\\msat\\M'
            OR coalesce(description, '') ~* '\\msat\\M'
          )
        """
    )

    # 2) Backfill groups.program_type from linked active course access first
    op.execute(
        """
        WITH linked AS (
            SELECT
                cga.group_id,
                CASE
                    WHEN bool_or(c.course_type = 'ielts') THEN 'ielts'
                    WHEN bool_or(c.course_type = 'sat') THEN 'sat'
                    ELSE 'general_english'
                END AS inferred_program_type
            FROM course_group_access cga
            JOIN courses c ON c.id = cga.course_id
            WHERE cga.is_active = true
            GROUP BY cga.group_id
        )
        UPDATE groups g
        SET program_type = linked.inferred_program_type
        FROM linked
        WHERE g.id = linked.group_id
          AND g.program_type = 'general_english'
        """
    )

    # 3) For groups still defaulted, infer from group name
    op.execute(
        """
        UPDATE groups
        SET program_type = 'ielts'
        WHERE program_type = 'general_english'
          AND coalesce(name, '') ~* '\\mielts\\M'
        """
    )

    op.execute(
        """
        UPDATE groups
        SET program_type = 'sat'
        WHERE program_type = 'general_english'
          AND coalesce(name, '') ~* '\\msat\\M'
        """
    )


def downgrade() -> None:
    # Best-effort rollback: return inferred/legacy values to default.
    op.execute("UPDATE groups SET program_type = 'general_english'")
    op.execute("UPDATE courses SET course_type = 'general_english'")
