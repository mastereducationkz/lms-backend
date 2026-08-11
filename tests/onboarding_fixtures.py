"""A transactional session fixture that tolerates application ``commit()`` calls.

The suite's older recipe (``begin_nested`` plus an ``after_transaction_end`` listener that
rebuilds the savepoint) cannot be used for onboarding: the reconciler commits, and committing
tears down the very savepoint that listener is trying to re-create, so the next statement
raises "Can't operate on closed transaction inside context manager".

``join_transaction_mode="create_savepoint"`` is SQLAlchemy 2.0's supported answer — the
session's commits land inside a savepoint on a connection-level transaction this fixture
always rolls back, so the tests still leave no trace.
"""
import pytest


@pytest.fixture
def db():
    from sqlalchemy.exc import OperationalError
    from sqlalchemy.orm import Session as SASession

    from src.config import engine

    try:
        connection = engine.connect()
    except OperationalError:
        pytest.skip("No database available (requires Postgres); skipping")
    trans = connection.begin()
    session = SASession(bind=connection, join_transaction_mode="create_savepoint")
    try:
        yield session
    finally:
        session.close()
        trans.rollback()
        connection.close()
