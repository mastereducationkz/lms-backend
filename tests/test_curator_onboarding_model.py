# tests/test_curator_onboarding_model.py
from src.schemas.models import CuratorOnboarding  # re-export path must work


def test_model_shape():
    t = CuratorOnboarding.__table__
    assert t.name == "curator_onboarding"
    cols = set(t.columns.keys())
    assert {"id", "curator_id", "student_id", "group_id", "status",
            "created_at", "updated_at", "completed_at", "completed_by"} <= cols
    # one card per (curator, student)
    uniques = [c for c in t.constraints if c.__class__.__name__ == "UniqueConstraint"]
    pairs = [tuple(sorted(col.name for col in c.columns)) for c in uniques]
    assert ("curator_id", "student_id") in pairs
    assert t.columns["status"].default.arg == "new"
