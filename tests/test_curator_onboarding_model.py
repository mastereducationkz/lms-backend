# tests/test_curator_onboarding_model.py
from src.schemas.models import CuratorOnboarding  # re-export path must work


def test_model_shape():
    t = CuratorOnboarding.__table__
    assert t.name == "curator_onboarding"
    cols = set(t.columns.keys())
    assert {"id", "curator_id", "student_id", "group_id", "status",
            "created_at", "updated_at", "completed_at", "completed_by"} <= cols
    assert t.columns["status"].default.arg == "new"


def test_lifecycle_columns_present():
    """A card is a *cycle*: it carries its ordinal and how/when it ended."""
    cols = set(CuratorOnboarding.__table__.columns.keys())
    assert {"cycle_no", "ended_at", "end_reason",
            "status_changed_at", "next_action_at", "next_action_note"} <= cols


def test_one_open_cycle_per_pair_not_one_row_forever():
    """Uniqueness moved from the *pair* to the pair's *open* cycle.

    The lifetime UniqueConstraint had to go: it is what made a returning student
    unrepresentable. Its replacement still forbids two live cards for the same pair, so the
    invariant the original constraint protected survives — only the historical rows are
    freed.
    """
    t = CuratorOnboarding.__table__
    uniques = [c for c in t.constraints if c.__class__.__name__ == "UniqueConstraint"]
    pairs = [tuple(sorted(col.name for col in c.columns)) for c in uniques]
    assert ("curator_id", "student_id") not in pairs, (
        "lifetime uniqueness must not come back — it blocks second cycles"
    )
