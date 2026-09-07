"""Marketing eligibility of exam results.

The sales team may feature a student on two different grounds, and the two carry
different permissions, so they are reported as separate *bases* rather than folded into
one flag:

``score``
    The student's current attempt cleared the exam's marketing threshold
    (``MARKETING_SCORE_THRESHOLDS``, strictly greater than). This is a fact about the
    result and nothing more: no consent has been recorded, so the score may be cited
    but the student's name, photo or words may not be used on the strength of it.

``testimonial``
    An approved, consented, non-revoked :class:`StudentTestimonial` exists. That IS the
    consent record; only this basis permits using the student's name, photo or quote.

Thresholds are table-driven so that adding one for another exam type is a one-line
change. IELTS (0-9 bands) and NUET deliberately have none: for them eligibility comes
from a testimonial alone.
"""
from decimal import Decimal
from typing import Dict, List, Optional

from src.exams.models import ExamResult, StudentTestimonial

# exam_type -> total_score a current attempt must EXCEED (strictly greater than).
MARKETING_SCORE_THRESHOLDS: Dict[str, int] = {"sat": 1400}

BASIS_SCORE = "score"
BASIS_TESTIMONIAL = "testimonial"


def marketing_threshold(exam_type: str) -> Optional[int]:
    """The score a current attempt must exceed, or None when scores never qualify."""
    return MARKETING_SCORE_THRESHOLDS.get((exam_type or "").strip().lower())


def marketing_basis(
    exam_type: str,
    result: Optional[ExamResult],
    testimonial: Optional[StudentTestimonial],
) -> List[str]:
    """Why a row is marketing-eligible; empty when it is not.

    ``result`` is the student's current attempt. A superseded or rejected row never
    qualifies whatever its score - the same rule ``latest_results_by_student`` applies.
    A ``reported`` (unverified) result does count; the row exposes its status, so the
    sales team can see whether it was checked against proof.
    """
    basis: List[str] = []

    threshold = marketing_threshold(exam_type)
    if (
        result is not None
        and threshold is not None
        and not result.is_superseded
        and result.status != "rejected"
        and result.total_score is not None
        and Decimal(result.total_score) > threshold
    ):
        basis.append(BASIS_SCORE)

    if testimonial is not None and testimonial.is_marketing_ready:
        basis.append(BASIS_TESTIMONIAL)

    return basis
