"""Display rule for SAT/NUET scaled scores (lead's decision, 2026-09-03).

The SAT platform predicts section scaled scores from raw correct counts and ships them as
``scaledScoreEstimate`` / ``scaledMathEstimate`` / ``scaledVerbalEstimate`` / ``scaledTotalEstimate``
next to the raw ``correctCount`` / ``totalQuestions``. They are ESTIMATES, never official scores:

* staff (curators, teachers, head roles, admins) see correct/total only — no scaled value anywhere
  in staff-facing views, and never in the curator leaderboard;
* students and parents see scaled + correct/total, always with :data:`ESTIMATE_NOTE`.
"""

from __future__ import annotations

import copy
from typing import Any

ESTIMATE_NOTE = (
    "Scaled scores are estimates predicted from the number of correct answers "
    "(SAT sections 200–800 rounded to 10, NUET 0–120). They are not official scores."
)

STAFF_ROLES = frozenset({"curator", "teacher", "admin", "head_curator", "head_teacher"})
SCORE_VIEWER_ROLES = frozenset({"student", "parent"})


def sees_estimates(role: str | None) -> bool:
    return (role or "").strip().lower() in SCORE_VIEWER_ROLES


def _strip(value: Any) -> Any:
    """Remove every key that carries a scaled estimate or its note, recursively."""
    if isinstance(value, dict):
        return {k: _strip(v) for k, v in value.items() if "scaled" not in k.lower()}
    if isinstance(value, list):
        return [_strip(v) for v in value]
    return value


def sanitize_sat_scores(data: Any, role: str | None) -> Any:
    """Return a copy of a SAT test-results payload shaped for ``role``."""
    if sees_estimates(role):
        out = copy.deepcopy(data) if isinstance(data, (dict, list)) else data
        if isinstance(out, dict):
            out["scaled_note"] = ESTIMATE_NOTE
        return out
    return _strip(copy.deepcopy(data))
