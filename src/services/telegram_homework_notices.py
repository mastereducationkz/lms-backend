"""Telling a group's Telegram chat that new homework was published, with a link to open it
(owner, 2026-09-12).

Same split of duties as the lesson-change notices: the LMS decides *what* changed and
*whether* to say so, Support carries the message. Queued right after
:func:`src.assignments.routes.assignments.create_assignment` commits — best effort, the same
convention that endpoint already uses for the email notification beside it, not the stricter
same-transaction guarantee the lesson-request flow gives :mod:`telegram_lesson_notices`.

**A real hyperlink, not a pasted URL.** Support's sanitizer keeps ``<a href>`` through exactly
like an announcement's toolbar link (:mod:`announcements.formatting` on the Support side), so
the free-text lines are escaped but the anchor tag itself is built, not escaped.

Off unless ``ENABLE_TELEGRAM_HOMEWORK_NOTICES`` is set, exactly like the sibling jobs.
"""
import html
import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError

from src.announcements.models import TelegramGroupLink, TelegramHomeworkNotice
from src.schemas.models import Assignment, Group
from src.courses.models import Lesson
from src.services import support_client
from src.services.recording_watch_links import lms_url
from src.services.telegram_invitations import RU_MONTHS, RU_WEEKDAYS, _almaty

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 3
SEND_TIMEOUT_SECONDS = 45
SYSTEM_ACTOR = "lms-homework-notices@mastereducation.kz"


def enabled() -> bool:
    return os.getenv("ENABLE_TELEGRAM_HOMEWORK_NOTICES", "").strip().lower() in ("1", "true", "yes", "on")


def _due_str(value: Optional[datetime]) -> Optional[str]:
    if not value:
        return None
    a = _almaty(value)
    return f"{RU_WEEKDAYS[a.weekday()]}, {a.day} {RU_MONTHS[a.month - 1]}, {a:%H:%M} (время Алматы)"


_TASK_TYPE_LABELS = {
    "course_unit": "уроки курса",
    "file_task": "загрузка файла",
    "text_task": "текстовый ответ",
    "link_task": "внешняя ссылка",
    "pdf_text_task": "файл + текстовый ответ",
    "audio_task": "аудиоответ",
    "bluebook_task": "Bluebook Test",
}
_ASSIGNMENT_TYPE_LABELS = {
    "single_choice": "выберите правильный ответ",
    "multiple_choice": "выберите все правильные ответы",
    "picture_choice": "выберите правильное изображение",
    "fill_in_blanks": "заполните пропуски",
    "matching": "сопоставьте элементы",
    "matching_text": "сопоставьте элементы",
    "free_text": "напишите текстовый ответ",
    "essay": "напишите текстовый ответ",
    "file_upload": "прикрепите выполненный файл",
    "audio": "запишите аудиоответ",
    "text": "напишите текстовый ответ",
    "file": "прикрепите выполненный файл",
    "pdf": "выполните задания в PDF",
    "quiz": "пройдите тест в LMS",
    "test": "пройдите тест в LMS",
    "homework": "выполните задание в LMS",
    "platform_test": "пройдите еженедельный тест на платформе",
}
_MAX_TASKS_IN_NOTICE = 8
_MAX_DETAIL_LENGTH = 300
_TELEGRAM_TEXT_LIMIT = 4096


def _plain_text(value: Any, limit: int = _MAX_DETAIL_LENGTH) -> str:
    """Make teacher-authored text safe and compact for one Telegram line."""
    if not isinstance(value, str):
        return ""
    compact = " ".join(value.split())
    return compact if len(compact) <= limit else f"{compact[:limit - 1].rstrip()}…"


def _content_dict(content: Any) -> dict[str, Any]:
    if isinstance(content, dict):
        return content
    if isinstance(content, str):
        try:
            decoded = json.loads(content)
            return decoded if isinstance(decoded, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def _task_detail(task_type: str, content: dict[str, Any], lesson_titles: Optional[dict[int, str]] = None) -> str:
    """Describe the student action without sending correct answers or key resources."""
    prompt = _plain_text(content.get("question") or content.get("link_description"))
    if task_type == "course_unit":
        lesson_ids = content.get("lesson_ids") or []
        names = [_plain_text((lesson_titles or {}).get(lesson_id), 100) for lesson_id in lesson_ids]
        names = [name for name in names if name]
        if names:
            return f"Завершите уроки: {', '.join(names)}."
        return f"Завершите выбранные уроки курса{f' ({len(lesson_ids)})' if lesson_ids else ''}."
    if task_type in ("file_task", "pdf_text_task"):
        filename = _plain_text(content.get("teacher_file_name"), 160)
        file_text = f'Файл «{filename}». ' if filename else ""
        return f"{file_text}{prompt}".strip()
    if task_type == "link_task":
        action = {"watch": "Посмотрите материал.", "read": "Прочитайте материал.",
                  "complete": "Выполните задание по ссылке.", "visit": "Откройте ссылку."}.get(
                      content.get("completion_criteria"), "Откройте ссылку.")
        return f"{action} {prompt}".strip()
    if task_type == "bluebook_task":
        number = content.get("test_number")
        return f"Bluebook Practice Test #{number}: загрузите официальный PDF-отчёт." if number else "Загрузите официальный PDF-отчёт Bluebook."
    if task_type == "fill_in_blanks":
        return _plain_text(content.get("text_with_blanks")) or "Заполните пропуски."
    if task_type in ("matching", "matching_text"):
        items = content.get("items_to_match") or content.get("left_items") or []
        return f"Сопоставьте элементы{f' ({len(items)})' if isinstance(items, list) else ''}."
    return prompt or _ASSIGNMENT_TYPE_LABELS.get(task_type, "Выполните задание в LMS.")


def _answer_key_note(tasks: list[dict[str, Any]]) -> Optional[str]:
    policies = {
        key.get("release_policy", "after_submission")
        for task in tasks for key in task.get("answer_keys", [])
        if isinstance(key, dict)
    }
    notes = []
    if "immediate" in policies:
        notes.append("Часть материалов для самопроверки уже доступна в задании.")
    if "after_submission" in policies:
        notes.append("После отправки станет доступен материал для самопроверки.")
    if "after_due_date" in policies:
        notes.append("Материал для самопроверки станет доступен после срока сдачи.")
    if "manual" in policies:
        notes.append("Материал для самопроверки будет опубликован преподавателем.")
    return " ".join(notes) or None


def _task_lines(
    assignment_type: str, content: Any, description: Optional[str], lesson_titles: Optional[dict[int, str]] = None,
) -> list[str]:
    payload = _content_dict(content)
    if assignment_type == "multi_task" and isinstance(payload.get("tasks"), list):
        tasks = [task for task in payload["tasks"] if isinstance(task, dict)]
        if tasks:
            lines = ["Задания:"]
            for index, task in enumerate(tasks[:_MAX_TASKS_IN_NOTICE], start=1):
                task_type = task.get("task_type", "")
                label = _TASK_TYPE_LABELS.get(task_type, "задание")
                title = _plain_text(task.get("title"), 120) or label.capitalize()
                detail = _task_detail(task_type, _content_dict(task.get("content")), lesson_titles)
                optional = " (дополнительно)" if task.get("is_optional") else ""
                lines.append(f"{index}. {title} ({label}){optional}: {detail}")
            remaining = len(tasks) - _MAX_TASKS_IN_NOTICE
            if remaining > 0:
                lines.append(f"…и ещё {remaining} заданий в LMS.")
            key_note = _answer_key_note(tasks)
            if key_note:
                lines.append(key_note)
            return lines

    detail = _task_detail(assignment_type, payload, lesson_titles)
    description_text = _plain_text(description)
    lines = ["Задание:", f"1. {detail}"]
    if description_text and description_text != detail:
        lines.append(description_text)
    return lines


def notice_text(
    title: str, group_name: str, due_date: Optional[datetime], link: str, *,
    assignment_type: str = "", content: Any = None, description: Optional[str] = None,
    lesson_titles: Optional[dict[int, str]] = None,
) -> str:
    """Render a safe, action-oriented homework notice. Every free-text line is escaped;
    the final anchor remains a real Telegram link. The compact interface hides all task-type
    normalization in this module so producers never need to format homework details."""
    header_lines = [
        "Новое домашнее задание",
        html.escape(_plain_text(group_name, 180), quote=False),
        html.escape(_plain_text(title, 220), quote=False),
    ]
    task_lines = [html.escape(line, quote=False) for line in _task_lines(assignment_type, content, description, lesson_titles)]
    due_str = _due_str(due_date)
    due_line = f"Срок: {html.escape(due_str, quote=False)}" if due_str else None
    anchor = f'<a href="{html.escape(link, quote=True)}">Открыть задание</a>'

    # Keep headers, deadline and the final link even for unusually long teacher text.
    # Add whole task lines while they fit, then append a visibly truncated final line.
    reserved = len(anchor) + 1
    fixed_lines = header_lines + ([due_line] if due_line else [])
    budget = _TELEGRAM_TEXT_LIMIT - reserved - len("\n".join(fixed_lines))
    included = []
    for line in task_lines:
        separator = 1 if included else 0
        if len(line) + separator <= budget:
            included.append(line)
            budget -= len(line) + separator
            continue
        if budget >= 1 + (1 if included else 0):
            included.append("…")
        break
    lines = header_lines + included + ([due_line] if due_line else [])
    body = "\n".join(lines)
    return f"{body}\n{anchor}"


def queue_for_assignment(db, assignment: "Assignment", group: Optional["Group"]) -> int:
    """Write one pending row for ``group``'s linked chat, if it has one. Never commits — the
    caller decides when (see the module docstring on the atomicity trade-off here).

    Safe to call more than once for the same assignment: the unique constraint makes a
    second call a no-op, so nothing is ever said twice for one published assignment.
    """
    if not enabled() or group is None:
        return 0
    link_row = db.query(TelegramGroupLink).filter(TelegramGroupLink.lms_group_id == group.id).first()
    if not link_row:
        return 0
    already = db.query(TelegramHomeworkNotice.id).filter(
        TelegramHomeworkNotice.assignment_id == assignment.id,
        TelegramHomeworkNotice.lms_group_id == group.id,
    ).first()
    if already:
        return 0
    row = TelegramHomeworkNotice(
        assignment_id=assignment.id, lms_group_id=group.id, support_group_id=link_row.support_group_id,
    )
    db.add(row)
    try:
        db.flush()
        return 1
    except IntegrityError:
        # A genuine race with another call for the same assignment — the DB-level backstop.
        db.rollback()
        return 0


def due(db) -> list:
    """Every notice still worth trying: never sent, or failed with attempts left."""
    return (db.query(TelegramHomeworkNotice)
            .filter(TelegramHomeworkNotice.status.in_(("pending", "failed")),
                    TelegramHomeworkNotice.attempts < MAX_ATTEMPTS)
            .order_by(TelegramHomeworkNotice.created_at)
            .all())


def _claim(db, notice_id: int) -> bool:
    """True once, for the tick that actually gets to send this row."""
    updated = (db.query(TelegramHomeworkNotice)
               .filter(TelegramHomeworkNotice.id == notice_id,
                       TelegramHomeworkNotice.status.in_(("pending", "failed")))
               .update({"attempts": TelegramHomeworkNotice.attempts + 1}, synchronize_session=False))
    db.commit()
    return bool(updated)


def _text_for(db, notice: TelegramHomeworkNotice) -> Optional[str]:
    assignment = db.get(Assignment, notice.assignment_id)
    group = db.get(Group, notice.lms_group_id)
    if not assignment or not group:
        return None
    content = _content_dict(assignment.content)
    lesson_ids = {
        lesson_id for task in content.get("tasks", []) if isinstance(task, dict)
        for lesson_id in _content_dict(task.get("content")).get("lesson_ids", [])
        if isinstance(lesson_id, int)
    }
    lesson_titles = dict(db.query(Lesson.id, Lesson.title).filter(Lesson.id.in_(lesson_ids)).all()) if lesson_ids else {}
    return notice_text(
        assignment.title, group.name, assignment.due_date, lms_url(f"/homework/{assignment.id}"),
        assignment_type=assignment.assignment_type, content=assignment.content,
        description=assignment.description, lesson_titles=lesson_titles,
    )


def send_due_notices(db, now: Optional[datetime] = None) -> dict:
    """Send every notice still due. Safe to call every minute from any number of places."""
    summary = {"sent": 0, "failed": 0, "skipped": 0}
    if not enabled():
        return summary
    for notice in due(db):
        if not _claim(db, notice.id):
            continue
        text = _text_for(db, notice)
        if not text:
            notice.status, notice.error = "skipped", "assignment or group no longer exists"
            db.commit()
            summary["skipped"] += 1
            continue
        outcome = {}
        try:
            result = support_client.call(
                "POST", "/telegram/messages",
                actor_email=SYSTEM_ACTOR, actor_name="LMS homework notices",
                json_body={
                    "telegram_group_id": notice.support_group_id,
                    "text": text,
                    "idempotency_key": f"homework:{notice.assignment_id}:{notice.lms_group_id}",
                    "silent": False,
                    "disable_web_page_preview": True,
                },
                timeout=SEND_TIMEOUT_SECONDS,
            ) or {}
            outcome = {"status": "sent", "telegram_message_id": result.get("telegram_message_id"),
                      "sent_at": datetime.now(timezone.utc).replace(tzinfo=None), "error": None}
        except HTTPException as exc:
            detail = f"{exc.status_code}: {exc.detail}"
            if exc.status_code in (400, 404, 409, 422):
                outcome = {"status": "skipped", "error": detail}
            else:
                outcome = {"status": "failed", "error": detail}
        except Exception as e:
            outcome = {"status": "failed", "error": str(e)[:500]}

        for key, value in outcome.items():
            setattr(notice, key, value)
        if notice.status == "failed" and notice.attempts >= MAX_ATTEMPTS:
            logger.warning("homework notice %s: giving up after %s attempts: %s",
                           notice.id, notice.attempts, notice.error)
        db.commit()
        summary[outcome["status"]] = summary.get(outcome["status"], 0) + 1
    return summary
