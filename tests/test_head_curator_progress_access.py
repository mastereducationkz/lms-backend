"""Head curators may read assignment student-progress (read-only monitoring)."""
import inspect

from src.assignments.routes import assignments as assignments_routes


def test_head_curator_granted_access_in_student_progress_source():
    """The access-control block of get_assignment_student_progress must grant
    head_curator access (like admin). Guards against a regression where the
    role passes the initial check but is never granted has_access -> 403."""
    src = inspect.getsource(assignments_routes.get_assignment_student_progress)
    # The role gate already lists head_curator; ensure access is actually granted.
    assert "head_curator" in src
    # An explicit early grant for head_curator/admin must exist before the
    # `raise HTTPException(... Access denied to this assignment ...)` line.
    grant_idx = src.find('current_user.role in ["admin", "head_curator"]')
    deny_idx = src.find("Access denied to this assignment")
    assert grant_idx != -1, "head_curator is not granted has_access"
    assert grant_idx < deny_idx, "grant must precede the access-denied raise"
