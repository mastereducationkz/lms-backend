#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import text

CURRENT_FILE = Path(__file__).resolve()
BACKEND_ROOT = CURRENT_FILE.parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from src.config import SessionLocal


@dataclass
class ReportConfig:
    course_id: int
    period_start: datetime
    period_end: datetime
    include_optional_steps: bool
    output_path: Path


def parse_date(value: str, *, end_of_day: bool = False) -> datetime:
    parsed = datetime.strptime(value, "%Y-%m-%d")
    if end_of_day:
        return parsed.replace(hour=23, minute=59, second=59, microsecond=999999, tzinfo=timezone.utc)
    return parsed.replace(tzinfo=timezone.utc)


def calculate_program_week(schedule_config: dict[str, Any] | None, today: date) -> int | None:
    if not schedule_config:
        return None

    start_date = schedule_config.get("start_date")
    if not start_date:
        return None

    try:
        start = date.fromisoformat(start_date)
    except ValueError:
        return None

    delta_days = (today - start).days
    if delta_days < 0:
        return None

    return delta_days // 7 + 1


def calculate_total_weeks(schedule_config: dict[str, Any] | None) -> int | None:
    if not schedule_config:
        return None

    lessons_count = schedule_config.get("lessons_count")
    schedule_items = schedule_config.get("schedule_items") or []
    weeks_count = schedule_config.get("weeks_count")

    if lessons_count:
        lessons_per_week = max(len(schedule_items), 1)
        return int((lessons_count + lessons_per_week - 1) / lessons_per_week)

    if weeks_count:
        return int(weeks_count)

    return None


def build_finished_groups(conn: Any, cfg: ReportConfig) -> dict[str, Any]:
    groups_rows = conn.execute(
        text(
            """
            SELECT
                g.id AS group_id,
                g.name AS group_name,
                g.is_active AS group_is_active,
                g.schedule_config AS schedule_config,
                t.name AS teacher_name,
                c.name AS curator_name
            FROM course_group_access cga
            JOIN groups g ON g.id = cga.group_id
            LEFT JOIN users t ON t.id = g.teacher_id
            LEFT JOIN users c ON c.id = g.curator_id
            WHERE
                cga.course_id = :course_id
                AND cga.is_active = TRUE
                AND g.is_special = FALSE
            ORDER BY g.id
            """
        ),
        {"course_id": cfg.course_id},
    ).mappings().all()

    today = datetime.now(timezone.utc).date()
    groups = []
    finished_groups = []

    for row in groups_rows:
        schedule_config = row["schedule_config"] or {}
        program_week = calculate_program_week(schedule_config, today)
        total_weeks = calculate_total_weeks(schedule_config)
        is_finished_by_program = bool(program_week and total_weeks and program_week > total_weeks)

        group_item = {
            "group_id": row["group_id"],
            "group_name": row["group_name"],
            "group_is_active": row["group_is_active"],
            "teacher_name": row["teacher_name"],
            "curator_name": row["curator_name"],
            "start_date": schedule_config.get("start_date"),
            "program_week": program_week,
            "total_weeks": total_weeks,
            "is_finished_by_program": is_finished_by_program,
        }
        groups.append(group_item)
        if is_finished_by_program:
            finished_groups.append(group_item)

    return {
        "total_groups_in_course": len(groups),
        "finished_groups_count": len(finished_groups),
        "finished_groups_share_pct": round((len(finished_groups) / len(groups)) * 100, 2) if groups else 0,
        "groups": groups,
        "finished_groups": finished_groups,
        "definition": "program_week > total_weeks from groups.schedule_config",
    }


def build_module_completion(conn: Any, cfg: ReportConfig, group_ids: list[int]) -> dict[str, Any]:
    if not group_ids:
        return {"groups": [], "groups_fully_completed_count": 0}

    module_totals_rows = conn.execute(
        text(
            """
            SELECT
                m.id AS module_id,
                m.title AS module_title,
                COUNT(s.id) AS total_steps
            FROM modules m
            JOIN lessons l ON l.module_id = m.id
            JOIN steps s ON s.lesson_id = l.id
            WHERE
                m.course_id = :course_id
                AND (:include_optional OR COALESCE(s.is_optional, FALSE) = FALSE)
            GROUP BY m.id, m.title
            ORDER BY m.id
            """
        ),
        {"course_id": cfg.course_id, "include_optional": cfg.include_optional_steps},
    ).mappings().all()

    module_totals = {row["module_id"]: int(row["total_steps"]) for row in module_totals_rows}
    module_titles = {row["module_id"]: row["module_title"] for row in module_totals_rows}

    if not module_totals:
        return {"groups": [], "groups_fully_completed_count": 0}

    students_rows = conn.execute(
        text(
            """
            SELECT
                gs.group_id,
                u.id AS student_id,
                u.name AS student_name
            FROM group_students gs
            JOIN users u ON u.id = gs.student_id
            WHERE
                gs.group_id = ANY(:group_ids)
                AND u.role = 'student'
                AND u.is_active = TRUE
            """
        ),
        {"group_ids": group_ids},
    ).mappings().all()

    students_by_group: dict[int, list[dict[str, Any]]] = {}
    all_student_ids: set[int] = set()
    for row in students_rows:
        all_student_ids.add(row["student_id"])
        students_by_group.setdefault(row["group_id"], []).append(
            {"student_id": row["student_id"], "student_name": row["student_name"]}
        )

    completed_rows = conn.execute(
        text(
            """
            SELECT
                sp.user_id AS student_id,
                m.id AS module_id,
                COUNT(DISTINCT sp.step_id) AS completed_steps
            FROM step_progress sp
            JOIN steps s ON s.id = sp.step_id
            JOIN lessons l ON l.id = s.lesson_id
            JOIN modules m ON m.id = l.module_id
            WHERE
                m.course_id = :course_id
                AND sp.status = 'completed'
                AND sp.user_id = ANY(:student_ids)
                AND (:include_optional OR COALESCE(s.is_optional, FALSE) = FALSE)
            GROUP BY sp.user_id, m.id
            """
        ),
        {
            "course_id": cfg.course_id,
            "student_ids": list(all_student_ids) if all_student_ids else [-1],
            "include_optional": cfg.include_optional_steps,
        },
    ).mappings().all()

    completed_map: dict[tuple[int, int], int] = {}
    for row in completed_rows:
        completed_map[(row["student_id"], row["module_id"])] = int(row["completed_steps"])

    groups_result = []
    groups_fully_completed_count = 0

    for group_id in group_ids:
        group_students = students_by_group.get(group_id, [])
        student_results = []
        module_students_fully_map = {module_id: 0 for module_id in module_totals}

        for student in group_students:
            student_module_states = []
            is_student_fully_completed = True

            for module_id, total_steps in module_totals.items():
                completed_steps = completed_map.get((student["student_id"], module_id), 0)
                is_module_completed = completed_steps >= total_steps and total_steps > 0
                student_module_states.append(
                    {
                        "module_id": module_id,
                        "module_title": module_titles[module_id],
                        "completed_steps": completed_steps,
                        "total_steps": total_steps,
                        "is_module_completed": is_module_completed,
                    }
                )
                if is_module_completed:
                    module_students_fully_map[module_id] += 1
                else:
                    is_student_fully_completed = False

            student_results.append(
                {
                    "student_id": student["student_id"],
                    "student_name": student["student_name"],
                    "is_fully_completed": is_student_fully_completed,
                    "modules": student_module_states,
                }
            )

        students_count = len(group_students)
        students_fully_completed_count = len([s for s in student_results if s["is_fully_completed"]])
        group_fully_completed = students_count > 0 and students_fully_completed_count == students_count
        if group_fully_completed:
            groups_fully_completed_count += 1

        module_histogram = []
        for module_id, students_completed in module_students_fully_map.items():
            completion_pct = round((students_completed / students_count) * 100, 2) if students_count else 0
            module_histogram.append(
                {
                    "module_id": module_id,
                    "module_title": module_titles[module_id],
                    "students_completed_module": students_completed,
                    "students_total": students_count,
                    "completion_pct": completion_pct,
                }
            )

        groups_result.append(
            {
                "group_id": group_id,
                "students_count": students_count,
                "students_fully_completed_count": students_fully_completed_count,
                "students_fully_completed_pct": round(
                    (students_fully_completed_count / students_count) * 100, 2
                )
                if students_count
                else 0,
                "group_fully_completed_by_strict_rule": group_fully_completed,
                "module_histogram": module_histogram,
                "students": student_results,
            }
        )

    return {
        "definition": "Student is fully completed if all course modules have all required steps completed",
        "include_optional_steps": cfg.include_optional_steps,
        "groups_fully_completed_count": groups_fully_completed_count,
        "groups_total": len(group_ids),
        "groups": groups_result,
    }


def build_homework_metrics(conn: Any, cfg: ReportConfig, group_ids: list[int]) -> dict[str, Any]:
    if not group_ids:
        return {"groups": []}

    rows = conn.execute(
        text(
            """
            WITH group_assignments AS (
                SELECT
                    a.id AS assignment_id,
                    a.group_id,
                    a.created_at
                FROM assignments a
                WHERE
                    a.group_id = ANY(:group_ids)
                    AND a.is_active = TRUE
                    AND a.created_at BETWEEN :period_start AND :period_end
            ),
            submissions AS (
                SELECT
                    ga.group_id,
                    s.id AS submission_id,
                    s.is_graded,
                    s.submitted_at,
                    s.graded_at,
                    s.feedback
                FROM group_assignments ga
                LEFT JOIN assignment_submissions s ON s.assignment_id = ga.assignment_id
            )
            SELECT
                ga.group_id,
                COUNT(DISTINCT ga.assignment_id) AS assignments_created,
                COUNT(DISTINCT s.submission_id) AS submissions_total,
                COUNT(DISTINCT CASE WHEN s.is_graded THEN s.submission_id END) AS submissions_graded,
                COUNT(DISTINCT CASE WHEN s.is_graded AND s.feedback IS NOT NULL AND s.feedback <> '' THEN s.submission_id END) AS submissions_with_feedback,
                COUNT(DISTINCT CASE WHEN s.is_graded THEN ga.assignment_id END) AS assignments_with_any_graded_submission,
                AVG(
                    CASE
                        WHEN s.is_graded AND s.graded_at IS NOT NULL AND s.submitted_at IS NOT NULL
                        THEN EXTRACT(EPOCH FROM (s.graded_at - s.submitted_at)) / 3600
                        ELSE NULL
                    END
                ) AS avg_grading_time_hours
            FROM group_assignments ga
            LEFT JOIN submissions s ON s.group_id = ga.group_id
            GROUP BY ga.group_id
            ORDER BY ga.group_id
            """
        ),
        {
            "group_ids": group_ids,
            "period_start": cfg.period_start,
            "period_end": cfg.period_end,
        },
    ).mappings().all()

    result = []
    for row in rows:
        submissions_total = int(row["submissions_total"] or 0)
        submissions_graded = int(row["submissions_graded"] or 0)
        grading_coverage = round((submissions_graded / submissions_total) * 100, 2) if submissions_total else 0
        result.append(
            {
                "group_id": row["group_id"],
                "assignments_created": int(row["assignments_created"] or 0),
                "submissions_total": submissions_total,
                "submissions_graded": submissions_graded,
                "assignments_with_any_graded_submission": int(row["assignments_with_any_graded_submission"] or 0),
                "submissions_with_feedback": int(row["submissions_with_feedback"] or 0),
                "grading_coverage_pct": grading_coverage,
                "avg_grading_time_hours": round(float(row["avg_grading_time_hours"]), 2)
                if row["avg_grading_time_hours"] is not None
                else None,
            }
        )

    return {
        "definition": "Only active assignments created in selected period are counted",
        "period_start": cfg.period_start.isoformat(),
        "period_end": cfg.period_end.isoformat(),
        "groups": result,
    }


def build_attendance_coverage(conn: Any, cfg: ReportConfig, group_ids: list[int]) -> dict[str, Any]:
    if not group_ids:
        return {"groups": []}

    rows = conn.execute(
        text(
            """
            WITH past_events AS (
                SELECT
                    e.id AS event_id,
                    eg.group_id
                FROM events e
                JOIN event_groups eg ON eg.event_id = e.id
                WHERE
                    eg.group_id = ANY(:group_ids)
                    AND e.is_active = TRUE
                    AND e.event_type = 'class'
                    AND e.end_datetime BETWEEN :period_start AND :period_end
            ),
            expected AS (
                SELECT
                    pe.event_id,
                    pe.group_id,
                    COUNT(gs.student_id) AS expected_students
                FROM past_events pe
                LEFT JOIN group_students gs ON gs.group_id = pe.group_id
                LEFT JOIN users u ON u.id = gs.student_id
                WHERE u.role = 'student' AND u.is_active = TRUE
                GROUP BY pe.event_id, pe.group_id
            ),
            filled AS (
                SELECT
                    pe.event_id,
                    pe.group_id,
                    COUNT(a.id) AS filled_records
                FROM past_events pe
                LEFT JOIN group_students gs ON gs.group_id = pe.group_id
                LEFT JOIN users u ON u.id = gs.student_id
                LEFT JOIN attendances a ON a.event_id = pe.event_id AND a.user_id = gs.student_id
                WHERE u.role = 'student' AND u.is_active = TRUE
                GROUP BY pe.event_id, pe.group_id
            )
            SELECT
                e.group_id,
                COUNT(e.event_id) AS total_events,
                COUNT(CASE WHEN e.expected_students = f.filled_records THEN 1 END) AS fully_filled_events,
                COUNT(CASE WHEN e.expected_students > f.filled_records THEN 1 END) AS events_with_gaps
            FROM expected e
            JOIN filled f ON f.event_id = e.event_id AND f.group_id = e.group_id
            GROUP BY e.group_id
            ORDER BY e.group_id
            """
        ),
        {
            "group_ids": group_ids,
            "period_start": cfg.period_start,
            "period_end": cfg.period_end,
        },
    ).mappings().all()

    group_stats = []
    for row in rows:
        total_events = int(row["total_events"] or 0)
        fully_filled_events = int(row["fully_filled_events"] or 0)
        group_stats.append(
            {
                "group_id": row["group_id"],
                "total_events": total_events,
                "fully_filled_events": fully_filled_events,
                "events_with_gaps": int(row["events_with_gaps"] or 0),
                "full_attendance_fill_rate_pct": round((fully_filled_events / total_events) * 100, 2)
                if total_events
                else 0,
            }
        )

    return {
        "definition": "Event is fully filled when attendance records count equals active students count for the event group",
        "period_start": cfg.period_start.isoformat(),
        "period_end": cfg.period_end.isoformat(),
        "groups": group_stats,
    }


def build_error_uptime_rum_section(conn: Any, cfg: ReportConfig) -> dict[str, Any]:
    question_error_rows = conn.execute(
        text(
            """
            SELECT
                status,
                COUNT(*) AS reports_count
            FROM question_error_reports
            WHERE created_at BETWEEN :period_start AND :period_end
            GROUP BY status
            ORDER BY status
            """
        ),
        {"period_start": cfg.period_start, "period_end": cfg.period_end},
    ).mappings().all()

    return {
        "question_error_reports": {
            "period_start": cfg.period_start.isoformat(),
            "period_end": cfg.period_end.isoformat(),
            "by_status": [{"status": row["status"], "count": int(row["reports_count"])} for row in question_error_rows],
        },
        "infrastructure_metrics": {
            "backend_error_types": None,
            "site_downtime_last_2_months_minutes": None,
            "average_page_load_seconds": None,
            "required_sources": [
                "Sentry or centralized backend logs for 5xx and exception types",
                "Cloudflare/Uptime monitor for downtime windows",
                "RUM/GA4/Sentry Performance for page load metrics",
            ],
        },
    }


def build_patterns(report: dict[str, Any]) -> list[str]:
    patterns = []

    homework_groups = report["homework_metrics"]["groups"]
    if homework_groups:
        low_coverage = [g for g in homework_groups if g["grading_coverage_pct"] < 60]
        if low_coverage:
            patterns.append(
                f"{len(low_coverage)} groups have grading coverage below 60% for the selected period."
            )

    attendance_groups = report["attendance_coverage"]["groups"]
    if attendance_groups:
        low_attendance_fill = [g for g in attendance_groups if g["full_attendance_fill_rate_pct"] < 80]
        if low_attendance_fill:
            patterns.append(
                f"{len(low_attendance_fill)} groups have attendance fill rate below 80%."
            )

    finished_groups_count = report["finished_groups"]["finished_groups_count"]
    strict_completed = report["module_completion"]["groups_fully_completed_count"]
    if finished_groups_count > 0 and strict_completed == 0:
        patterns.append("There are finished-by-program groups with no strict full module completion.")

    if not patterns:
        patterns.append("No high-risk pattern was automatically detected by current thresholds.")

    return patterns


def write_csv_views(report: dict[str, Any], output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)

    finished_path = output_dir / "finished_groups.csv"
    with finished_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "group_id",
                "group_name",
                "group_is_active",
                "teacher_name",
                "curator_name",
                "start_date",
                "program_week",
                "total_weeks",
                "is_finished_by_program",
            ],
        )
        writer.writeheader()
        writer.writerows(report["finished_groups"]["groups"])

    homework_path = output_dir / "homework_metrics.csv"
    with homework_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "group_id",
                "assignments_created",
                "submissions_total",
                "submissions_graded",
                "assignments_with_any_graded_submission",
                "submissions_with_feedback",
                "grading_coverage_pct",
                "avg_grading_time_hours",
            ],
        )
        writer.writeheader()
        writer.writerows(report["homework_metrics"]["groups"])

    attendance_path = output_dir / "attendance_coverage.csv"
    with attendance_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "group_id",
                "total_events",
                "fully_filled_events",
                "events_with_gaps",
                "full_attendance_fill_rate_pct",
            ],
        )
        writer.writeheader()
        writer.writerows(report["attendance_coverage"]["groups"])


def render_markdown_report(report: dict[str, Any]) -> str:
    lines: list[str] = []
    lines.append("# Отчёт по LMS (mastereducation.kz)")
    lines.append("")
    lines.append(f"- Дата генерации: `{report['generated_at']}`")
    lines.append(f"- Курс: `{report['course_id']}`")
    lines.append(f"- Период: `{report['period_start']}` → `{report['period_end']}`")
    lines.append("")

    finished = report["finished_groups"]
    lines.append("## 1) Группы, завершившие программу")
    lines.append("")
    lines.append(
        f"- Всего групп в курсе: **{finished['total_groups_in_course']}**"
    )
    lines.append(
        f"- Групп, завершивших программу по правилу `program_week > total_weeks`: **{finished['finished_groups_count']}** ({finished['finished_groups_share_pct']}%)"
    )
    lines.append("")
    lines.append("| group_id | group_name | start_date | program_week | total_weeks | finished_by_program |")
    lines.append("|---:|---|---|---:|---:|:---:|")
    for item in finished["groups"]:
        lines.append(
            f"| {item['group_id']} | {item['group_name']} | {item['start_date'] or '-'} | {item['program_week'] or '-'} | {item['total_weeks'] or '-'} | {'yes' if item['is_finished_by_program'] else 'no'} |"
        )
    lines.append("")

    module_completion = report["module_completion"]
    lines.append("## 2) Прохождение всех модулей")
    lines.append("")
    lines.append(
        f"- Групп, где все активные студенты полностью прошли все модули (strict): **{module_completion['groups_fully_completed_count']}** из **{module_completion['groups_total']}**"
    )
    lines.append("")
    lines.append("| group_id | students_count | fully_completed_count | fully_completed_pct | group_fully_completed_strict |")
    lines.append("|---:|---:|---:|---:|:---:|")
    for item in module_completion["groups"]:
        lines.append(
            f"| {item['group_id']} | {item['students_count']} | {item['students_fully_completed_count']} | {item['students_fully_completed_pct']}% | {'yes' if item['group_fully_completed_by_strict_rule'] else 'no'} |"
        )
    lines.append("")

    homework = report["homework_metrics"]
    lines.append("## 3) Домашние задания и оценивание")
    lines.append("")
    lines.append("| group_id | assignments_created | submissions_total | submissions_graded | grading_coverage_pct | avg_grading_time_hours |")
    lines.append("|---:|---:|---:|---:|---:|---:|")
    for item in homework["groups"]:
        avg_hours = item["avg_grading_time_hours"] if item["avg_grading_time_hours"] is not None else "-"
        lines.append(
            f"| {item['group_id']} | {item['assignments_created']} | {item['submissions_total']} | {item['submissions_graded']} | {item['grading_coverage_pct']}% | {avg_hours} |"
        )
    lines.append("")

    attendance = report["attendance_coverage"]
    lines.append("## 4) Attendance: полнота проставления")
    lines.append("")
    lines.append("| group_id | total_events | fully_filled_events | events_with_gaps | full_fill_rate_pct |")
    lines.append("|---:|---:|---:|---:|---:|")
    for item in attendance["groups"]:
        lines.append(
            f"| {item['group_id']} | {item['total_events']} | {item['fully_filled_events']} | {item['events_with_gaps']} | {item['full_attendance_fill_rate_pct']}% |"
        )
    lines.append("")

    errors = report["errors_uptime_rum"]["question_error_reports"]
    lines.append("## 5) Ошибки в системе (из БД LMS)")
    lines.append("")
    lines.append("Разбивка по `question_error_reports.status`:")
    lines.append("")
    lines.append("| status | count |")
    lines.append("|---|---:|")
    for item in errors["by_status"]:
        lines.append(f"| {item['status']} | {item['count']} |")
    if not errors["by_status"]:
        lines.append("| - | 0 |")
    lines.append("")

    lines.append("## 6) Downtime за 2 месяца")
    lines.append("")
    lines.append(
        "- В текущей БД LMS нет данных аптайма. Нужен внешний источник: Cloudflare/UptimeRobot/Pingdom/логи хостинга."
    )
    lines.append("")

    lines.append("## 7) Среднее время загрузки страниц")
    lines.append("")
    lines.append(
        "- В текущей кодовой базе нет встроенного RUM/Web Vitals сбора. Нужны Cloudflare Web Analytics, GA4 Core Web Vitals или Sentry Performance."
    )
    lines.append("")

    lines.append("## Паттерны")
    lines.append("")
    for pattern in report["patterns"]:
        lines.append(f"- {pattern}")
    lines.append("")

    return "\n".join(lines)


def build_report(cfg: ReportConfig) -> dict[str, Any]:
    with SessionLocal() as session:
        conn = session.connection()
        finished_groups = build_finished_groups(conn, cfg)
        group_ids = [item["group_id"] for item in finished_groups["groups"]]
        module_completion = build_module_completion(conn, cfg, group_ids)
        homework_metrics = build_homework_metrics(conn, cfg, group_ids)
        attendance_coverage = build_attendance_coverage(conn, cfg, group_ids)
        errors_uptime_rum = build_error_uptime_rum_section(conn, cfg)

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "course_id": cfg.course_id,
        "period_start": cfg.period_start.isoformat(),
        "period_end": cfg.period_end.isoformat(),
        "finished_groups": finished_groups,
        "module_completion": module_completion,
        "homework_metrics": homework_metrics,
        "attendance_coverage": attendance_coverage,
        "errors_uptime_rum": errors_uptime_rum,
    }
    report["patterns"] = build_patterns(report)
    return report


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate LMS operations report for one course"
    )
    parser.add_argument("--course-id", type=int, required=True, help="Course ID")
    parser.add_argument(
        "--start-date",
        type=str,
        default=(datetime.now(timezone.utc) - timedelta(days=60)).strftime("%Y-%m-%d"),
        help="Start date in YYYY-MM-DD (default: 60 days ago)",
    )
    parser.add_argument(
        "--end-date",
        type=str,
        default=datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        help="End date in YYYY-MM-DD (default: today)",
    )
    parser.add_argument(
        "--include-optional-steps",
        action="store_true",
        help="Include optional steps in module completion metrics",
    )
    parser.add_argument(
        "--output",
        type=str,
        default="reports/lms_operations_report.json",
        help="Output JSON path (default: reports/lms_operations_report.json)",
    )
    parser.add_argument(
        "--export-csv",
        action="store_true",
        help="Export group-level CSV files near output file",
    )
    parser.add_argument(
        "--markdown-output",
        type=str,
        default=None,
        help="Optional output path for markdown report (.md)",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    cfg = ReportConfig(
        course_id=args.course_id,
        period_start=parse_date(args.start_date),
        period_end=parse_date(args.end_date, end_of_day=True),
        include_optional_steps=args.include_optional_steps,
        output_path=Path(args.output),
    )

    report = build_report(cfg)
    cfg.output_path.parent.mkdir(parents=True, exist_ok=True)
    cfg.output_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    if args.export_csv:
        write_csv_views(report, cfg.output_path.parent)

    if args.markdown_output:
        md_path = Path(args.markdown_output)
        md_path.parent.mkdir(parents=True, exist_ok=True)
        md_content = render_markdown_report(report)
        md_path.write_text(md_content, encoding="utf-8")
        print(f"Markdown report generated: {md_path}")

    print(f"Report generated: {cfg.output_path}")


if __name__ == "__main__":
    main()
