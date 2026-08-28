"""Render the student results report dict as a PDF (Russian, parent-facing).

Uses ReportLab platypus. Cyrillic needs a real TTF: in the Docker image DejaVu is
installed via ``fonts-dejavu-core``; on developer macOS machines Arial Unicode is
picked up instead. If no candidate font exists the report still renders (Helvetica),
which garbles Cyrillic — acceptable only in tests, so registration failure is not
raised as an error.
"""
from datetime import datetime
from io import BytesIO
from typing import Any, Dict, List, Optional

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle,
)

_FONT = "Helvetica"
_FONT_BOLD = "Helvetica-Bold"

_FONT_CANDIDATES = [
    ("StudentReport", "StudentReport-Bold",
     "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
     "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
    ("StudentReport", "StudentReport-Bold",
     "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
     "/System/Library/Fonts/Supplemental/Arial Unicode.ttf"),
]


def _register_fonts() -> None:
    global _FONT, _FONT_BOLD
    if _FONT != "Helvetica":
        return
    for regular_name, bold_name, regular_path, bold_path in _FONT_CANDIDATES:
        try:
            pdfmetrics.registerFont(TTFont(regular_name, regular_path))
            pdfmetrics.registerFont(TTFont(bold_name, bold_path))
            _FONT, _FONT_BOLD = regular_name, bold_name
            return
        except Exception:
            continue


_MONTHS_RU = {
    1: "января", 2: "февраля", 3: "марта", 4: "апреля", 5: "мая", 6: "июня",
    7: "июля", 8: "августа", 9: "сентября", 10: "октября", 11: "ноября", 12: "декабря",
}

_STATUS_RU = {
    "graded": "Проверено",
    "submitted": "Сдано, на проверке",
    "not_submitted": "Не сдано",
}


def _ru_date(iso: Optional[str]) -> str:
    if not iso:
        return "—"
    d = datetime.fromisoformat(iso)
    return f"{d.day} {_MONTHS_RU[d.month]} {d.year}"


def _pct(value: Optional[float]) -> str:
    if value is None:
        return "—"
    return f"{value:.2f}".replace(".", ",") + "%"


def _styles() -> Dict[str, ParagraphStyle]:
    return {
        "title": ParagraphStyle("title", fontName=_FONT_BOLD, fontSize=16, leading=20,
                                spaceAfter=4 * mm),
        "h2": ParagraphStyle("h2", fontName=_FONT_BOLD, fontSize=12, leading=16,
                             spaceBefore=5 * mm, spaceAfter=2 * mm),
        "body": ParagraphStyle("body", fontName=_FONT, fontSize=9.5, leading=13),
        "cell": ParagraphStyle("cell", fontName=_FONT, fontSize=8, leading=10),
        "cell_b": ParagraphStyle("cell_b", fontName=_FONT_BOLD, fontSize=8, leading=10),
    }


def _table(headers: List[str], rows: List[List[str]], styles: Dict[str, ParagraphStyle],
           col_widths: Optional[List[float]] = None) -> Table:
    data = [[Paragraph(h, styles["cell_b"]) for h in headers]]
    for row in rows:
        data.append([Paragraph(str(cell), styles["cell"]) for cell in row])
    table = Table(data, colWidths=col_widths, repeatRows=1)
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#E8EEF7")),
        ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#B9C4D4")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 2),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
    ]))
    return table


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _feedback_paragraph(text: str, styles: Dict[str, ParagraphStyle]) -> Paragraph:
    """Platform feedback is markdown-ish free text; render bold markers and line
    breaks, escape everything else."""
    safe = _escape(text)
    while "**" in safe:
        safe = safe.replace("**", "<b>", 1)
        if "**" in safe:
            safe = safe.replace("**", "</b>", 1)
        else:
            safe += "</b>"
    safe = safe.replace("\n", "<br/>")
    return Paragraph(safe, styles["cell"])


def _weekly_tests_flowables(weekly: Dict[str, Any],
                            styles: Dict[str, ParagraphStyle]) -> List[Any]:
    story: List[Any] = []
    body = styles["body"]

    def _side(side: Optional[Dict[str, Any]]) -> str:
        if not side or side.get("correct") is None:
            return "—"
        cell = f"{side['correct']}/{side['total']}"
        if side.get("pct") is not None:
            cell += f" — {_pct(side['pct'])}"
        return cell

    for key, title in (("sat", "Еженедельные SAT Practice"),
                       ("nuet", "Еженедельные NUET тесты")):
        weeks = weekly.get(key) or []
        if not weeks:
            continue
        story.append(Paragraph(title, styles["h2"]))
        story.append(_table(
            ["Неделя", "Math", "Verbal"],
            [[w["week_label"], _side(w.get("math")), _side(w.get("verbal"))] for w in weeks],
            styles, col_widths=[60 * mm, 55 * mm, 55 * mm],
        ))
        feedback_rows = [
            (f"{w['week_label']} — {label}", side["feedback"])
            for w in weeks
            for label, side in (("Math", w.get("math")), ("Verbal", w.get("verbal")))
            if side and side.get("feedback")
        ]
        if feedback_rows:
            story.append(Paragraph("Обратная связь по тестам", styles["h2"]))
            for label, text in feedback_rows:
                story.append(Paragraph(f"<b>{_escape(label)}</b>", body))
                story.append(_feedback_paragraph(text, styles))
                story.append(Spacer(0, 2 * mm))

    ielts_weeks = weekly.get("ielts") or []
    if ielts_weeks:
        def _band(value) -> str:
            return f"{value:.1f}".replace(".", ",") if value is not None else "—"

        story.append(Paragraph("Еженедельные IELTS тесты", styles["h2"]))
        story.append(_table(
            ["Неделя", "Listening", "Reading", "Writing", "Speaking", "Overall"],
            [[w["week_label"], _band(w.get("listening_band")), _band(w.get("reading_band")),
              _band(w.get("writing_band")), _band(w.get("speaking_band")),
              _band(w.get("overall_band"))] for w in ielts_weeks],
            styles,
        ))
        for w in ielts_weeks:
            feedback = w.get("feedback") or {}
            parts = []
            for skill in ("listening", "reading", "writing", "speaking"):
                value = feedback.get(skill)
                if isinstance(value, dict):
                    value = value.get("overall") or " ".join(
                        v for v in value.values() if isinstance(v, str))
                if value:
                    parts.append((skill.capitalize(), value))
            if parts:
                story.append(Paragraph(f"<b>Неделя {_escape(str(w['week_label']))}</b>", body))
                for skill, text in parts:
                    story.append(_feedback_paragraph(f"**{skill}:** {text}", styles))
                story.append(Spacer(0, 2 * mm))

    if weekly.get("errors"):
        story.append(Paragraph(
            "Часть данных внешних платформ недоступна: " + "; ".join(weekly["errors"]),
            body))
    return story


def render_student_report_pdf(report: Dict[str, Any]) -> BytesIO:
    _register_fonts()
    styles = _styles()
    story: List[Any] = []
    body = styles["body"]

    student = report["student"]
    story.append(Paragraph("Отчёт об успеваемости", styles["title"]))
    story.append(Paragraph(f"<b>Ученик:</b> {student['name']}", body))
    groups = ", ".join(g["name"] for g in student["groups"]) or "—"
    story.append(Paragraph(f"<b>Группы:</b> {groups}", body))
    story.append(Paragraph(
        f"<b>Отчёт сформирован:</b> {_ru_date(report['generated_at'][:10])}", body))

    # --- Homework ------------------------------------------------------------
    hw = report["homework"]
    story.append(Paragraph("Домашние задания", styles["h2"]))
    story.append(Paragraph(
        f"Назначено: <b>{hw['assigned']}</b> · Сдано: <b>{hw['submitted']}</b> · "
        f"Проверено: <b>{hw['graded']}</b> · Баллы: "
        f"<b>{hw['earned_score']} из {hw['max_score']}</b>", body))
    story.append(Spacer(0, 2 * mm))
    if hw["items"]:
        story.append(_table(
            ["Задание", "Срок сдачи", "Макс. балл", "Балл", "Статус"],
            [[i["title"], _ru_date(i["due_date"]), i["max_score"],
              i["score"] if i["score"] is not None else "—",
              _STATUS_RU.get(i["status"], i["status"])] for i in hw["items"]],
            styles, col_widths=[72 * mm, 32 * mm, 20 * mm, 15 * mm, 31 * mm],
        ))

    # --- Bluebook ------------------------------------------------------------
    if report["bluebook"]:
        story.append(Paragraph("Bluebook Practice Tests", styles["h2"]))
        story.append(_table(
            ["Тест", "Дата", "Общий балл", "Verbal", "Math"],
            [[f"Practice Test {b['test_number']}",
              _ru_date(b["taken_at"]) if b["taken_at"] else "входной",
              b["total"], b["verbal"], b["math"]] for b in report["bluebook"]],
            styles, col_widths=[50 * mm, 40 * mm, 27 * mm, 27 * mm, 26 * mm],
        ))

    # --- Official exams ------------------------------------------------------
    exams = report["exams"]
    story.append(Paragraph("Официальные экзамены", styles["h2"]))
    if exams["results"]:
        story.append(_table(
            ["Экзамен", "Дата", "Балл", "Verbal", "Math", "Статус"],
            [[r["exam_type"].upper(), _ru_date(r["test_date"]), r["total_score"],
              r["verbal_score"] or "—", r["math_score"] or "—", r["status"]]
             for r in exams["results"]],
            styles,
        ))
    else:
        story.append(Paragraph("Результаты в LMS не зарегистрированы.", body))
    if exams["sat_planned_date"]:
        story.append(Paragraph(
            f"Запланированная дата SAT: <b>{_ru_date(exams['sat_planned_date'])}</b>", body))
    if exams["ielts_planned_date"]:
        story.append(Paragraph(
            f"Запланированная дата IELTS: <b>{_ru_date(exams['ielts_planned_date'])}</b>", body))

    # --- Weekly tests from the external exam platforms -----------------------
    story.extend(_weekly_tests_flowables(report.get("weekly_tests") or {}, styles))

    # --- Quizzes -------------------------------------------------------------
    for course in report["quizzes"]:
        story.append(Paragraph(f"Квизы: {course['course_title']}", styles["h2"]))
        story.append(Paragraph(
            f"Попыток: <b>{course['total_attempts']}</b> · Завершено: "
            f"<b>{course['completed_attempts']}</b> · Средний результат: "
            f"<b>{_pct(course['average_pct'])}</b>", body))
        story.append(Spacer(0, 2 * mm))
        if course["sections"]:
            story.append(_table(
                ["Раздел", "Попыток", "Средний результат", "Лучший результат"],
                [[s["lesson_title"], s["attempts"], _pct(s["average_pct"]),
                  _pct(s["best_pct"])] for s in course["sections"]],
                styles, col_widths=[85 * mm, 20 * mm, 32 * mm, 33 * mm],
            ))

    # --- Course progress -----------------------------------------------------
    if report["courses"]:
        story.append(Paragraph("Прогресс в курсах", styles["h2"]))
        story.append(_table(
            ["Курс", "Шаги", "Прогресс", "Учебное время", "Последняя активность"],
            [[c["course_title"],
              f"{c['completed_steps']} из {c['total_steps']}",
              _pct(c["completion_pct"]),
              f"{c['time_spent_minutes']} мин",
              _ru_date(c["last_activity_at"])] for c in report["courses"]],
            styles, col_widths=[60 * mm, 27 * mm, 22 * mm, 27 * mm, 34 * mm],
        ))

    # --- Attendance ----------------------------------------------------------
    att = report["attendance"]
    story.append(Paragraph("Посещаемость", styles["h2"]))
    if att["marked_total"]:
        story.append(Paragraph(
            f"Занятий с отметкой: <b>{att['marked_total']}</b> · "
            f"Присутствовал(а): <b>{att['attended']} — {_pct(att['attendance_pct'])}</b> · "
            f"Пропущено: <b>{att['absent']}</b> · Опозданий: <b>{att['late']}</b>", body))
        for label, rows in (("Пропуски", att["absences"]), ("Опоздания", att["lates"])):
            if rows:
                dates = "; ".join(f"{_ru_date(r['date'])} ({r['title']})" for r in rows)
                story.append(Paragraph(f"{label}: {dates}", body))
    else:
        story.append(Paragraph("Данных о посещаемости нет.", body))

    # --- Activity ------------------------------------------------------------
    act = report["activity"]
    story.append(Paragraph("Дополнительная активность", styles["h2"]))
    story.append(Paragraph(
        f"Выполнено ежедневных заданий: <b>{act['daily_questions_completed']}</b> · "
        f"Баллы активности: <b>{act['points_total']}</b>", body))

    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer, pagesize=A4,
        leftMargin=18 * mm, rightMargin=18 * mm, topMargin=16 * mm, bottomMargin=16 * mm,
        title=f"Отчёт об успеваемости — {student['name']}",
    )
    doc.build(story)
    buffer.seek(0)
    return buffer
