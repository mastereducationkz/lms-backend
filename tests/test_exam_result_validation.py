"""Validation rules for exam results and Bluebook practice results.

These are the domain rules the UI must not be able to bypass. Everything here is pure
Pydantic - no database - so it runs green with or without Postgres.
"""
import pytest
from datetime import date
from decimal import Decimal

from pydantic import ValidationError

from src.exams.schemas import (
    BluebookResultInput,
    ExamResultCreate,
    ExamResultUpdate,
)


# --------------------------------------------------------------------------------------
# SAT
# --------------------------------------------------------------------------------------

def _sat(**over):
    # A real, already-past official SAT administration. Future dates are rejected
    # by design (see test_test_date_far_in_the_future_is_rejected).
    payload = dict(student_id=1, exam_type="sat", test_date=date(2026, 6, 6),
                   verbal_score=700, math_score=770)
    payload.update(over)
    return payload


def test_sat_total_is_derived_from_sections_not_supplied():
    """The reference spreadsheet computes Score = Verbal + Math for every row."""
    r = ExamResultCreate(**_sat())
    assert r.total_score == Decimal("1470")


def test_sat_client_supplied_total_is_ignored_in_favour_of_the_derived_one():
    """A client must not be able to record 700 + 770 = 9999."""
    r = ExamResultCreate(**_sat(total_score=Decimal("9999")))
    assert r.total_score == Decimal("1470")


def test_sat_requires_both_sections():
    with pytest.raises(ValidationError):
        ExamResultCreate(**_sat(math_score=None))
    with pytest.raises(ValidationError):
        ExamResultCreate(**_sat(verbal_score=None))


@pytest.mark.parametrize("score", [199, 801, 0, -100, 1600])
def test_sat_section_scores_outside_200_800_are_rejected(score):
    with pytest.raises(ValidationError):
        ExamResultCreate(**_sat(verbal_score=score))


@pytest.mark.parametrize("score", [200, 400, 800])
def test_sat_section_boundaries_are_accepted(score):
    ExamResultCreate(**_sat(verbal_score=score))


@pytest.mark.parametrize("score", [705, 201, 799])
def test_sat_section_scores_must_be_multiples_of_ten(score):
    with pytest.raises(ValidationError):
        ExamResultCreate(**_sat(verbal_score=score))


def test_sat_rejects_ielts_band_fields():
    """Cross-contaminating exam types silently corrupts reporting."""
    with pytest.raises(ValidationError):
        ExamResultCreate(**_sat(listening_band=Decimal("7.0")))


# --------------------------------------------------------------------------------------
# IELTS
# --------------------------------------------------------------------------------------

def _ielts(**over):
    payload = dict(student_id=1, exam_type="ielts", test_date=date(2026, 5, 10),
                   total_score=Decimal("7.5"), listening_band=Decimal("7.5"),
                   reading_band=Decimal("7.0"), writing_band=Decimal("7.0"),
                   speaking_band=Decimal("8.0"))
    payload.update(over)
    return payload


def test_ielts_overall_is_taken_from_the_client_not_derived():
    """IELTS overall is a rounded average with its own rules, not a simple sum."""
    r = ExamResultCreate(**_ielts())
    assert r.total_score == Decimal("7.5")


def test_ielts_requires_an_overall_score():
    with pytest.raises(ValidationError):
        ExamResultCreate(**_ielts(total_score=None))


@pytest.mark.parametrize("band", [Decimal("-0.5"), Decimal("9.5"), Decimal("10")])
def test_ielts_bands_outside_0_9_are_rejected(band):
    with pytest.raises(ValidationError):
        ExamResultCreate(**_ielts(listening_band=band))


@pytest.mark.parametrize("band", [Decimal("6.25"), Decimal("7.1"), Decimal("8.75")])
def test_ielts_bands_must_be_half_steps(band):
    with pytest.raises(ValidationError):
        ExamResultCreate(**_ielts(reading_band=band))


@pytest.mark.parametrize("band", [Decimal("0"), Decimal("6.5"), Decimal("9.0")])
def test_ielts_band_boundaries_and_half_steps_are_accepted(band):
    ExamResultCreate(**_ielts(speaking_band=band))


def test_ielts_rejects_sat_section_fields():
    with pytest.raises(ValidationError):
        ExamResultCreate(**_ielts(math_score=700))


def test_ielts_bands_are_optional_when_only_the_overall_is_known():
    r = ExamResultCreate(student_id=1, exam_type="ielts", test_date=date(2026, 5, 10),
                         total_score=Decimal("6.5"))
    assert r.total_score == Decimal("6.5")


# --------------------------------------------------------------------------------------
# Shared rules
# --------------------------------------------------------------------------------------

def test_unknown_exam_type_is_rejected():
    with pytest.raises(ValidationError):
        ExamResultCreate(student_id=1, exam_type="toefl", test_date=date(2026, 1, 1),
                         total_score=Decimal("100"))


def test_nuet_is_accepted_as_an_exam_type():
    r = ExamResultCreate(student_id=1, exam_type="nuet", test_date=date(2026, 1, 1),
                         total_score=Decimal("150"))
    assert r.exam_type == "nuet"


def test_test_date_far_in_the_future_is_rejected():
    """A result cannot have been earned at a date that has not happened."""
    with pytest.raises(ValidationError):
        ExamResultCreate(**_sat(test_date=date(2099, 1, 1)))


def test_status_must_be_a_known_value():
    with pytest.raises(ValidationError):
        ExamResultUpdate(status="approved-ish")


def test_update_accepts_known_statuses():
    for s in ("reported", "verified", "rejected"):
        assert ExamResultUpdate(status=s).status == s


# --------------------------------------------------------------------------------------
# Bluebook
# --------------------------------------------------------------------------------------

def _bb(**over):
    payload = dict(test_number=7, verbal_score=640, math_score=780)
    payload.update(over)
    return payload


def test_bluebook_total_is_derived():
    """Matches the reference sheet exactly: 640 + 780 = 1420."""
    assert BluebookResultInput(**_bb()).total_score == 1420


@pytest.mark.parametrize("n", [3, 12, 0, -1, 100])
def test_bluebook_test_number_outside_4_to_11_is_rejected(n):
    """Backend must reject even if the UI selector is bypassed."""
    with pytest.raises(ValidationError):
        BluebookResultInput(**_bb(test_number=n))


@pytest.mark.parametrize("n", [4, 5, 8, 11])
def test_bluebook_test_number_boundaries_accepted(n):
    assert BluebookResultInput(**_bb(test_number=n)).test_number == n


@pytest.mark.parametrize("score", [199, 801, 705])
def test_bluebook_section_scores_follow_sat_rules(score):
    with pytest.raises(ValidationError):
        BluebookResultInput(**_bb(verbal_score=score))


def test_bluebook_requires_both_sections():
    with pytest.raises(ValidationError):
        BluebookResultInput(test_number=7, verbal_score=640)


def test_bluebook_screenshot_url_is_optional_at_schema_level():
    """Requiring evidence is a submission-flow rule, not a shape rule - the
    Assignment Zero baseline backfill has no screenshot and must still validate."""
    assert BluebookResultInput(**_bb()).screenshot_url is None
