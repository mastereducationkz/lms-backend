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
from typing import Dict, List, Optional, Sequence, Set

from sqlalchemy.orm import Session

from src.exams import services as exam_services
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


def eligible_student_ids(
    db: Session,
    exam_type: str,
    student_ids: Optional[Sequence[int]] = None,
) -> Set[int]:
    """Every student in scope with a non-empty marketing basis.

    Exists so the grid can narrow to eligible students BEFORE limit/offset: paging the
    students first and dropping the ineligible ones afterwards would make the screen
    show "the eligible students among the alphabetically first N", and the export - which
    pages differently - a different set again, with neither saying anything was cut.

    ``student_ids`` of ``None`` means "no row scope", i.e. every student; an empty
    sequence means nobody. The verdict comes from :func:`marketing_basis` over the same
    current attempt :func:`latest_results_by_student` picks, so a student this returns
    always passes the per-row check too.
    """
    if student_ids is not None and not student_ids:
        return set()

    eligible: Set[int] = set()

    if marketing_threshold(exam_type) is not None:
        for sid, result in exam_services.latest_results_by_student(
            db, student_ids, exam_type
        ).items():
            if BASIS_SCORE in marketing_basis(exam_type, result, None):
                eligible.add(sid)

    # ``is_marketing_ready`` is a Python property (approved + consented + not revoked),
    # so the rows are read and judged in Python rather than restating that rule in SQL:
    # one definition, and there are only ever as many testimonials as approved quotes.
    testimonial_query = db.query(StudentTestimonial)
    if student_ids is not None:
        testimonial_query = testimonial_query.filter(
            StudentTestimonial.student_id.in_(list(student_ids))
        )
    for testimonial in testimonial_query.all():
        if testimonial.is_marketing_ready:
            eligible.add(testimonial.student_id)

    return eligible
