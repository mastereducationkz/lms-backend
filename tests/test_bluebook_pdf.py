"""Parsing College Board Bluebook practice score reports.

Fixtures are the TEXT layer captured from three real reports with the student's name
redacted, so the exact College Board layout is exercised without committing a real
student's PDF into the repository.

Pure functions - these run green with or without Postgres.
"""
from datetime import date
from pathlib import Path

import pytest

from src.exams.bluebook_pdf import (
    BluebookReportError,
    extract_pdf_text,
    names_are_similar,
    parse_report_pdf,
    parse_report_text,
)

FIXTURES = Path(__file__).parent / "fixtures" / "bluebook"


def fixture(name: str) -> str:
    return (FIXTURES / f"{name}.txt").read_text(encoding="utf-8")


# --------------------------------------------------------------------------------------
# The three real reports
# --------------------------------------------------------------------------------------

@pytest.mark.parametrize("name,test_number,verbal,math,total,report_date", [
    ("practice_7", 7, 720, 200, 920, date(2025, 4, 19)),
    ("practice_8", 8, 740, 770, 1510, date(2025, 5, 2)),
    ("practice_5", 5, 660, 760, 1420, date(2024, 6, 16)),
])
def test_parses_real_reports(name, test_number, verbal, math, total, report_date):
    r = parse_report_text(fixture(name))
    assert r.test_number == test_number
    assert r.verbal_score == verbal
    assert r.math_score == math
    assert r.total_score == total
    assert r.report_date == report_date


def test_sections_always_sum_to_the_total_in_genuine_reports():
    for name in ("practice_7", "practice_8", "practice_5"):
        assert parse_report_text(fixture(name)).sections_sum_to_total


def test_student_name_is_extracted():
    assert parse_report_text(fixture("practice_7")).student_name == "Test Student"


def test_a_zero_section_is_read_correctly_not_dropped():
    """practice_7 has Math 200 (the floor, i.e. zero correct). A parser that treats a
    floor score as 'missing' would silently inflate the student's total."""
    r = parse_report_text(fixture("practice_7"))
    assert r.math_score == 200
    assert r.total_score == 920


# --------------------------------------------------------------------------------------
# Rejections - a wrong score is worse than a refused upload
# --------------------------------------------------------------------------------------

def test_rejects_a_non_college_board_document():
    with pytest.raises(BluebookReportError) as exc:
        parse_report_text("Invoice #123\nTotal due: 500 KZT\n")
    assert "College Board" in str(exc.value)


def test_rejects_an_edited_report_whose_sections_do_not_sum():
    """The strongest integrity check available offline: a genuine report is always
    internally consistent, so a mismatch means a mis-parse or a doctored file."""
    tampered = fixture("practice_8").replace("740", "800", 1)
    with pytest.raises(BluebookReportError) as exc:
        parse_report_text(tampered)
    assert "inconsistent" in str(exc.value).lower()


def test_rejects_a_test_number_outside_4_to_11():
    out_of_range = fixture("practice_7").replace("SAT Practice 7", "SAT Practice 2")
    with pytest.raises(BluebookReportError) as exc:
        parse_report_text(out_of_range)
    assert "outside the range" in str(exc.value)


def test_rejects_a_section_score_out_of_range():
    bad = fixture("practice_8").replace("740\nScore Range", "940\nScore Range", 1)
    with pytest.raises(BluebookReportError):
        parse_report_text(bad)


def test_rejects_a_report_missing_its_section_scores():
    stripped = fixture("practice_7").replace("Reading and Writing\n720", "Reading and Writing")
    with pytest.raises(BluebookReportError) as exc:
        parse_report_text(stripped)
    assert "Reading and Writing" in str(exc.value)


def test_error_messages_tell_the_student_what_to_do():
    """These surface directly in the UI, so they must be actionable, not internals."""
    with pytest.raises(BluebookReportError) as exc:
        extract_pdf_text(b"\x89PNG\r\n\x1a\n fake image")
    message = str(exc.value)
    assert "PDF" in message and "Screenshot" in message
    assert "Traceback" not in message


# --------------------------------------------------------------------------------------
# PDF layer
# --------------------------------------------------------------------------------------

def test_rejects_an_image_masquerading_as_a_pdf():
    with pytest.raises(BluebookReportError):
        extract_pdf_text(b"\xff\xd8\xff\xe0 JPEG data")


def test_rejects_an_empty_upload():
    with pytest.raises(BluebookReportError) as exc:
        extract_pdf_text(b"")
    assert "empty" in str(exc.value).lower()


def test_rejects_an_oversize_upload():
    with pytest.raises(BluebookReportError) as exc:
        extract_pdf_text(b"%PDF-" + b"0" * (11 * 1024 * 1024))
    assert "too large" in str(exc.value).lower()


def test_rejects_a_corrupt_pdf():
    with pytest.raises(BluebookReportError):
        parse_report_pdf(b"%PDF-1.4\nnot really a pdf at all")


def test_rejects_a_pdf_with_no_text_layer():
    """A scan or photo exported as PDF has no text to read; say so plainly."""
    import io
    from pypdf import PdfWriter
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    buf = io.BytesIO()
    writer.write(buf)
    with pytest.raises(BluebookReportError) as exc:
        extract_pdf_text(buf.getvalue())
    assert "no text" in str(exc.value).lower()


# --------------------------------------------------------------------------------------
# Name comparison - flags, never blocks
# --------------------------------------------------------------------------------------

def test_matching_names_are_similar():
    assert names_are_similar("Alikhan Nurlanov", "Nurlanov Alikhan") is True


def test_partial_name_overlap_counts_as_a_match():
    """Patronymic present in one system and not the other is normal here.

    Note the Cyrillic case returns False: transliteration shares no token, so it IS
    flagged. That is why a mismatch only warns staff and never blocks a submission.
    """
    assert names_are_similar("Aigerim Zhaksylyk", "Жаксылык Айгерим Мураткызы") is False
    assert names_are_similar("Aigerim Zhaksylyk", "Zhaksylyk Aigerim Muratkyzy") is True


def test_completely_different_names_are_flagged():
    assert names_are_similar("Alikhan Nurlanov", "Dana Sultanova") is False


def test_missing_name_never_raises_a_false_alarm():
    assert names_are_similar(None, "Someone") is True
    assert names_are_similar("Someone", None) is True
