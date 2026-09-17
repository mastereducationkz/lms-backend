"""Alembic migration graph must have exactly one head.

A second head means two revision chains exist that neither depends on the
other. ``alembic upgrade head`` then refuses to pick one, and
``scripts/start.sh`` / the Dockerfile run it under ``set -e`` — so a forked
graph crash-loops the container on deploy instead of failing a merge.

This test is intentionally dependency-free: it only walks the revision
files on disk via ``alembic.script.ScriptDirectory`` and never opens a
database connection, so it runs anywhere ``alembic.ini`` is checked out,
without Postgres.
"""
import os

from alembic.config import Config
from alembic.script import ScriptDirectory

BACKEND_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ALEMBIC_INI = os.path.join(BACKEND_ROOT, "alembic.ini")


def test_exactly_one_alembic_head():
    config = Config(ALEMBIC_INI)
    script = ScriptDirectory.from_config(config)
    heads = script.get_heads()

    assert len(heads) == 1, (
        "Alembic migration graph has forked into multiple heads: "
        f"{heads}. `alembic upgrade head` cannot resolve this and the "
        "deploy will crash-loop. Check the `down_revision` of each head's "
        "revision file and repoint one chain onto the other."
    )
