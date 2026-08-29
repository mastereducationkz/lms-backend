"""Render the student results report dict as a branded PDF (Russian, parent-facing).

Typography: PT Sans (fonts-paratype in the Docker image) — a professional Cyrillic-
native face; DejaVu and Arial Unicode are fallbacks so dev machines still render.
Every page carries the Master Education header band with the logo and a footer with
page numbers. ``sections`` controls which blocks are rendered (the export dialog in
the UI lets staff choose); ``include_feedback`` gates the long platform-feedback
texts, which multiply the page count.
"""
import os
from datetime import datetime
from io import BytesIO
from typing import Any, Dict, List, Optional, Set

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    Image, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle,
)

# ── Brand ────────────────────────────────────────────────────────────────────────
BRAND = colors.HexColor("#2563EB")
BRAND_DARK = colors.HexColor("#1E40AF")
INK = colors.HexColor("#0F172A")
MUTED = colors.HexColor("#64748B")
BORDER = colors.HexColor("#D9E2EF")
ZEBRA = colors.HexColor("#F4F7FC")
CARD_BG = colors.HexColor("#EFF4FF")

_LOGO_PATH = os.path.join(os.path.dirname(__file__), "assets", "logo.png")

_FONT = "Helvetica"
_FONT_BOLD = "Helvetica-Bold"

_FONT_CANDIDATES = [
    # PT Sans: professional Cyrillic-native pair (fonts-paratype).
    ("MasterEd", "MasterEd-Bold",
     "/usr/share/fonts/truetype/paratype/PTS55F.ttf",
     "/usr/share/fonts/truetype/paratype/PTS75F.ttf"),
    ("MasterEd", "MasterEd-Bold",
     "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
     "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
    ("MasterEd", "MasterEd-Bold",
     "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
     "/System/Library/Fonts/Supplemental/Arial Unicode.ttf"),
]

# All report sections, in render order. "summary" is the header card and always on.
SECTION_KEYS = (
    "homework", "weekly", "ielts", "bluebook", "quizzes",
    "courses", "attendance", "activity",
)


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
    "submitted": "На проверке",
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
    return f"{value:.1f}".replace(".", ",") + "%"


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _styles() -> Dict[str, ParagraphStyle]:
    return {
        "title": ParagraphStyle("title", fontName=_FONT_BOLD, fontSize=19, leading=24,
                                textColor=INK),
        "subtitle": ParagraphStyle("subtitle", fontName=_FONT, fontSize=9.5, leading=13,
                                   textColor=MUTED),
        "h2": ParagraphStyle("h2", fontName=_FONT_BOLD, fontSize=12.5, leading=16,
                             textColor=BRAND_DARK, spaceBefore=7 * mm, spaceAfter=2.5 * mm),
        "body": ParagraphStyle("body", fontName=_FONT, fontSize=9.5, leading=13.5,
                               textColor=INK),
        "muted": ParagraphStyle("muted", fontName=_FONT, fontSize=8.5, leading=12,
                                textColor=MUTED),
        "cell": ParagraphStyle("cell", fontName=_FONT, fontSize=8.2, leading=10.5,
                               textColor=INK),
        "cell_b": ParagraphStyle("cell_b", fontName=_FONT_BOLD, fontSize=8.2, leading=10.5,
                                 textColor=colors.white),
        "kpi_value": ParagraphStyle("kpi_value", fontName=_FONT_BOLD, fontSize=14,
                                    leading=17, textColor=BRAND_DARK),
        "kpi_label": ParagraphStyle("kpi_label", fontName=_FONT, fontSize=7.6, leading=10,
                                    textColor=MUTED),
        "feedback": ParagraphStyle("feedback", fontName=_FONT, fontSize=8, leading=11,
                                   textColor=colors.HexColor("#334155"),
                                   backColor=ZEBRA, borderPadding=(4, 6, 4, 6)),
    }


def _table(headers: List[str], rows: List[List[Any]], styles: Dict[str, ParagraphStyle],
           col_widths: Optional[List[float]] = None,
           numeric_cols: Optional[Set[int]] = None) -> Table:
    numeric_cols = numeric_cols or set()
    right = ParagraphStyle("cell_r", parent=styles["cell"], alignment=2)
    data = [[Paragraph(_escape(str(h)), styles["cell_b"]) for h in headers]]
    for row in rows:
        data.append([
            Paragraph(_escape(str(cell)), right if i in numeric_cols else styles["cell"])
            for i, cell in enumerate(row)
        ])
    table = Table(data, colWidths=col_widths, repeatRows=1)
    style = [
        ("BACKGROUND", (0, 0), (-1, 0), BRAND),
        ("LINEBELOW", (0, 0), (-1, 0), 0.6, BRAND_DARK),
        ("GRID", (0, 1), (-1, -1), 0.35, BORDER),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("LEFTPADDING", (0, 0), (-1, -1), 5),
        ("RIGHTPADDING", (0, 0), (-1, -1), 5),
    ]
    for i in range(1, len(data)):
        if i % 2 == 0:
            style.append(("BACKGROUND", (0, i), (-1, i), ZEBRA))
    table.setStyle(TableStyle(style))
    return table


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
    return Paragraph(safe, styles["feedback"])


# ── Page furniture ───────────────────────────────────────────────────────────────

def _page_decorator(student_name: str):
    generated = datetime.now()
    generated_label = f"{generated.day} {_MONTHS_RU[generated.month]} {generated.year}"

    def draw(canvas, doc):
        canvas.saveState()
        width, height = A4

        # Header band
        canvas.setFillColor(colors.white)
        canvas.rect(0, height - 20 * mm, width, 20 * mm, stroke=0, fill=1)
        if os.path.exists(_LOGO_PATH):
            canvas.drawImage(_LOGO_PATH, 16 * mm, height - 16.5 * mm, 11 * mm, 11 * mm,
                             mask="auto")
        canvas.setFillColor(INK)
        canvas.setFont(_FONT_BOLD, 11)
        canvas.drawString(30 * mm, height - 11.5 * mm, "Master Education")
        canvas.setFillColor(MUTED)
        canvas.setFont(_FONT, 7.5)
        canvas.drawString(30 * mm, height - 15 * mm, "Отчёт об успеваемости")
        canvas.drawRightString(width - 16 * mm, height - 11.5 * mm, student_name)
        canvas.drawRightString(width - 16 * mm, height - 15 * mm, generated_label)
        canvas.setStrokeColor(BRAND)
        canvas.setLineWidth(1.4)
        canvas.line(16 * mm, height - 19 * mm, width - 16 * mm, height - 19 * mm)

        # Footer
        canvas.setStrokeColor(BORDER)
        canvas.setLineWidth(0.5)
        canvas.line(16 * mm, 12 * mm, width - 16 * mm, 12 * mm)
        canvas.setFillColor(MUTED)
        canvas.setFont(_FONT, 7)
        canvas.drawString(16 * mm, 8 * mm, "Master Education · lms.mastereducation.kz")
        canvas.drawRightString(width - 16 * mm, 8 * mm, f"Страница {doc.page}")
        canvas.restoreState()

    return draw


# ── Sections ─────────────────────────────────────────────────────────────────────

def _summary_block(report: Dict[str, Any], styles: Dict[str, ParagraphStyle]) -> List[Any]:
    student = report["student"]
    hw = report["homework"]
    att = report["attendance"]
    act = report["activity"]
    quizzes = report.get("quizzes") or []
    quiz_pcts = [c["average_pct"] for c in quizzes if c.get("average_pct") is not None]
    avg_quiz = sum(quiz_pcts) / len(quiz_pcts) if quiz_pcts else None

    story: List[Any] = []
    story.append(Paragraph(_escape(student["name"]), styles["title"]))
    groups = ", ".join(g["name"] for g in student["groups"]) or "—"
    story.append(Paragraph(
        f"{_escape(groups)}", styles["subtitle"]))
    story.append(Spacer(0, 4 * mm))

    def kpi(label: str, value: str, sub: str) -> Table:
        inner = Table(
            [[Paragraph(value, styles["kpi_value"])],
             [Paragraph(_escape(label), styles["kpi_label"])],
             [Paragraph(_escape(sub), styles["kpi_label"])]],
            colWidths=[41 * mm],
        )
        inner.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), CARD_BG),
            ("BOX", (0, 0), (-1, -1), 0.5, BORDER),
            ("LEFTPADDING", (0, 0), (-1, -1), 6),
            ("RIGHTPADDING", (0, 0), (-1, -1), 6),
            ("TOPPADDING", (0, 0), (0, 0), 5),
            ("BOTTOMPADDING", (0, -1), (-1, -1), 5),
            ("TOPPADDING", (0, 1), (-1, -1), 0),
            ("BOTTOMPADDING", (0, 0), (-1, 1), 1),
        ]))
        return inner

    row = Table([[
        kpi("Посещаемость", _pct(att["attendance_pct"]),
            f"{att['attended']} из {att['marked_total']} занятий"),
        kpi("Домашние задания", f"{hw['graded']}/{hw['assigned']}",
            f"{hw['earned_score']} из {hw['max_score']} баллов"),
        kpi("Средний квиз", _pct(avg_quiz),
            f"{sum(c['completed_attempts'] for c in quizzes)} попыток"),
        kpi("Баллы активности", str(act["points_total"]),
            f"{act['daily_questions_completed']} ежедневных заданий"),
    ]], colWidths=[44.5 * mm] * 4)
    row.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 3),
    ]))
    story.append(row)
    return story


def _homework_flowables(report, styles) -> List[Any]:
    hw = report["homework"]
    story: List[Any] = [Paragraph("Домашние задания", styles["h2"])]
    story.append(Paragraph(
        f"Назначено: <b>{hw['assigned']}</b> · Сдано: <b>{hw['submitted']}</b> · "
        f"Проверено: <b>{hw['graded']}</b> · Баллы: "
        f"<b>{hw['earned_score']} из {hw['max_score']}</b>", styles["body"]))
    story.append(Spacer(0, 2 * mm))
    if hw["items"]:
        story.append(_table(
            ["Задание", "Срок сдачи", "Макс.", "Балл", "Статус"],
            [[i["title"], _ru_date(i["due_date"]), i["max_score"],
              i["score"] if i["score"] is not None else "—",
              _STATUS_RU.get(i["status"], i["status"])] for i in hw["items"]],
            styles, col_widths=[76 * mm, 30 * mm, 15 * mm, 15 * mm, 28 * mm],
            numeric_cols={2, 3},
        ))
    return story


def _weekly_flowables(report, styles, include_feedback: bool) -> List[Any]:
    weekly = report.get("weekly_tests") or {}
    story: List[Any] = []

    def _side(side) -> str:
        if not side or side.get("correct") is None:
            return "—"
        cell = f"{side['correct']}/{side['total']}"
        if side.get("pct") is not None:
            cell += f" — {_pct(side['pct'])}"
        return cell

    for key, title, source in (("sat", "Еженедельные SAT Practice", "sat.mastereducation.kz"),
                               ("nuet", "Еженедельные NUET тесты", "nuet.mastereducation.kz")):
        weeks = weekly.get(key) or []
        if not weeks:
            continue
        story.append(Paragraph(title, styles["h2"]))
        story.append(Paragraph(f"Источник: {source}", styles["muted"]))
        story.append(Spacer(0, 1.5 * mm))
        story.append(_table(
            ["Неделя", "Math", "Verbal"],
            [[w["week_label"], _side(w.get("math")), _side(w.get("verbal"))] for w in weeks],
            styles, col_widths=[56 * mm, 54 * mm, 54 * mm], numeric_cols={1, 2},
        ))
        if include_feedback:
            feedback_rows = [
                (f"{w['week_label']} — {label}", side["feedback"])
                for w in weeks
                for label, side in (("Math", w.get("math")), ("Verbal", w.get("verbal")))
                if side and side.get("feedback")
            ]
            if feedback_rows:
                story.append(Spacer(0, 2 * mm))
                story.append(Paragraph("Обратная связь по тестам", styles["h2"]))
                for label, text in feedback_rows:
                    story.append(Paragraph(f"<b>{_escape(label)}</b>", styles["body"]))
                    story.append(Spacer(0, 1 * mm))
                    story.append(_feedback_paragraph(text, styles))
                    story.append(Spacer(0, 2.5 * mm))
    return story


def _ielts_flowables(report, styles, include_feedback: bool) -> List[Any]:
    weeks = (report.get("weekly_tests") or {}).get("ielts") or []
    if not weeks:
        return []

    def _band(value) -> str:
        return f"{value:.1f}".replace(".", ",") if value is not None else "—"

    story: List[Any] = [Paragraph("Еженедельные IELTS тесты", styles["h2"])]
    story.append(Paragraph("Источник: ielts.mastereducation.kz", styles["muted"]))
    story.append(Spacer(0, 1.5 * mm))
    story.append(_table(
        ["Неделя", "Listening", "Reading", "Writing", "Speaking", "Overall"],
        [[w["week_label"], _band(w.get("listening_band")), _band(w.get("reading_band")),
          _band(w.get("writing_band")), _band(w.get("speaking_band")),
          _band(w.get("overall_band"))] for w in weeks],
        styles, numeric_cols={1, 2, 3, 4, 5},
    ))
    if include_feedback:
        for w in weeks:
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
                story.append(Spacer(0, 2 * mm))
                story.append(Paragraph(
                    f"<b>Неделя {_escape(str(w['week_label']))}</b>", styles["body"]))
                story.append(Spacer(0, 1 * mm))
                for skill, text in parts:
                    story.append(_feedback_paragraph(f"**{skill}:** {text}", styles))
                    story.append(Spacer(0, 1.5 * mm))
    return story


def _bluebook_flowables(report, styles) -> List[Any]:
    story: List[Any] = []
    if report.get("bluebook"):
        story.append(Paragraph("Bluebook Practice Tests", styles["h2"]))
        story.append(_table(
            ["Тест", "Дата", "Общий балл", "Verbal", "Math"],
            [[f"Practice Test {b['test_number']}",
              _ru_date(b["taken_at"]) if b["taken_at"] else "входной",
              b["total"], b["verbal"], b["math"]] for b in report["bluebook"]],
            styles, col_widths=[46 * mm, 40 * mm, 27 * mm, 26 * mm, 25 * mm],
            numeric_cols={2, 3, 4},
        ))
    exams = report.get("exams") or {}
    story.append(Paragraph("Официальные экзамены", styles["h2"]))
    if exams.get("results"):
        story.append(_table(
            ["Экзамен", "Дата", "Балл", "Verbal", "Math", "Статус"],
            [[r["exam_type"].upper(), _ru_date(r["test_date"]), r["total_score"],
              r["verbal_score"] or "—", r["math_score"] or "—", r["status"]]
             for r in exams["results"]],
            styles, numeric_cols={2, 3, 4},
        ))
    else:
        story.append(Paragraph("Результаты в LMS не зарегистрированы.", styles["body"]))
    for key, label in (("sat_planned_date", "Запланированная дата SAT"),
                       ("ielts_planned_date", "Запланированная дата IELTS")):
        if exams.get(key):
            story.append(Spacer(0, 1 * mm))
            story.append(Paragraph(f"{label}: <b>{_ru_date(exams[key])}</b>", styles["body"]))
    return story


def _quiz_flowables(report, styles) -> List[Any]:
    story: List[Any] = []
    for course in report.get("quizzes") or []:
        story.append(Paragraph(f"Квизы: {_escape(course['course_title'])}", styles["h2"]))
        story.append(Paragraph(
            f"Попыток: <b>{course['total_attempts']}</b> · Завершено: "
            f"<b>{course['completed_attempts']}</b> · Средний результат: "
            f"<b>{_pct(course['average_pct'])}</b>", styles["body"]))
        story.append(Spacer(0, 2 * mm))
        if course["sections"]:
            story.append(_table(
                ["Раздел", "Попыток", "Средний", "Лучший"],
                [[s["lesson_title"], s["attempts"], _pct(s["average_pct"]),
                  _pct(s["best_pct"])] for s in course["sections"]],
                styles, col_widths=[92 * mm, 20 * mm, 26 * mm, 26 * mm],
                numeric_cols={1, 2, 3},
            ))
    return story


def _course_flowables(report, styles) -> List[Any]:
    if not report.get("courses"):
        return []
    story: List[Any] = [Paragraph("Прогресс в курсах", styles["h2"])]
    story.append(_table(
        ["Курс", "Шаги", "Прогресс", "Учебное время", "Последняя активность"],
        [[c["course_title"],
          f"{c['completed_steps']} из {c['total_steps']}",
          _pct(c["completion_pct"]),
          f"{c['time_spent_minutes'] // 60} ч {c['time_spent_minutes'] % 60} мин",
          _ru_date(c["last_activity_at"])] for c in report["courses"]],
        styles, col_widths=[58 * mm, 26 * mm, 22 * mm, 27 * mm, 31 * mm],
        numeric_cols={1, 2},
    ))
    return story


def _attendance_flowables(report, styles) -> List[Any]:
    att = report["attendance"]
    story: List[Any] = [Paragraph("Посещаемость", styles["h2"])]
    if not att["marked_total"]:
        story.append(Paragraph("Данных о посещаемости нет.", styles["body"]))
        return story
    story.append(Paragraph(
        f"Занятий с отметкой: <b>{att['marked_total']}</b> · "
        f"Присутствовал(а): <b>{att['attended']} — {_pct(att['attendance_pct'])}</b> · "
        f"Пропущено: <b>{att['absent']}</b> · Опозданий: <b>{att['late']}</b>",
        styles["body"]))
    for label, rows in (("Пропуски", att["absences"]), ("Опоздания", att["lates"])):
        if rows:
            story.append(Spacer(0, 1.5 * mm))
            dates = "; ".join(f"{_ru_date(r['date'])} ({r['title']})" for r in rows)
            story.append(Paragraph(f"<b>{label}:</b> {_escape(dates)}", styles["muted"]))
    return story


def _activity_flowables(report, styles) -> List[Any]:
    act = report["activity"]
    story: List[Any] = [Paragraph("Дополнительная активность", styles["h2"])]
    story.append(Paragraph(
        f"Выполнено ежедневных заданий: <b>{act['daily_questions_completed']}</b> · "
        f"Баллы активности: <b>{act['points_total']}</b>", styles["body"]))
    reasons = act.get("points_by_reason") or {}
    if reasons:
        labels = {"course_quiz": "Квизы в курсах", "homework": "Домашние задания",
                  "assignment": "Задания", "daily_questions": "Ежедневные вопросы"}
        story.append(Spacer(0, 1.5 * mm))
        story.append(_table(
            ["Источник баллов", "Баллы"],
            [[labels.get(k, k), v] for k, v in sorted(reasons.items(), key=lambda x: -x[1])],
            styles, col_widths=[120 * mm, 44 * mm], numeric_cols={1},
        ))
    return story


# ── Entry point ──────────────────────────────────────────────────────────────────

def render_student_report_pdf(report: Dict[str, Any],
                              sections: Optional[Set[str]] = None,
                              include_feedback: bool = True) -> BytesIO:
    _register_fonts()
    styles = _styles()
    chosen = set(SECTION_KEYS) if not sections else {s for s in sections if s in SECTION_KEYS}

    story: List[Any] = []
    story.extend(_summary_block(report, styles))

    renderers = {
        "homework": lambda: _homework_flowables(report, styles),
        "weekly": lambda: _weekly_flowables(report, styles, include_feedback),
        "ielts": lambda: _ielts_flowables(report, styles, include_feedback),
        "bluebook": lambda: _bluebook_flowables(report, styles),
        "quizzes": lambda: _quiz_flowables(report, styles),
        "courses": lambda: _course_flowables(report, styles),
        "attendance": lambda: _attendance_flowables(report, styles),
        "activity": lambda: _activity_flowables(report, styles),
    }
    for key in SECTION_KEYS:
        if key in chosen:
            story.extend(renderers[key]())

    errors = (report.get("weekly_tests") or {}).get("errors") or []
    if errors and ("weekly" in chosen or "ielts" in chosen):
        story.append(Spacer(0, 3 * mm))
        story.append(Paragraph(
            "Часть данных внешних платформ недоступна: " + _escape("; ".join(errors)),
            styles["muted"]))

    buffer = BytesIO()
    decorator = _page_decorator(report["student"]["name"])
    doc = SimpleDocTemplate(
        buffer, pagesize=A4,
        leftMargin=16 * mm, rightMargin=16 * mm, topMargin=26 * mm, bottomMargin=18 * mm,
        title=f"Отчёт об успеваемости — {report['student']['name']}",
        author="Master Education",
    )
    doc.build(story, onFirstPage=decorator, onLaterPages=decorator)
    buffer.seek(0)
    return buffer
