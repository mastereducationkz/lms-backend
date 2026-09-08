"""Why a lesson read was refused, in a form both the client and the student can use.

Every lesson-facing GET — the lesson, its steps, its step progress — can refuse for a handful of
distinct reasons. Each raises a `LessonAccessDenied`, which carries a stable `code` the web client
switches on and a Russian `message` the student reads. The app's global 403/404 envelope
(`src/app.py`) forwards both, so the reason survives to the browser instead of being flattened
into a bare "Failed to load lesson data".

A refusal describes only the reader's own situation. It names their own checkpoint and their own
outstanding units, and never another student, a group id, a teacher, or an internal object id.
"""
from typing import Any, Dict, List, Optional

from fastapi import HTTPException

# Stable machine-readable codes. Add to this list; never rename one — the web client switches on
# them, and an old client must keep understanding a code it already knows.
LESSON_NOT_FOUND = "lesson_not_found"
COURSE_ACCESS_DENIED = "course_access_denied"
CHECKPOINT_LOCKED = "checkpoint_locked"
CHECKPOINT_NOT_OPEN = "checkpoint_not_open"
TRIAL_LOCKED = "trial_locked"
ROLE_DENIED = "role_denied"
# Sequential-access reasons. These come back from `check-access` as a 200 with
# `accessible: false` rather than as a refusal, but they are the same set of reasons and share
# their wording with the gates above.
MODULE_NOT_RELEASED = "module_not_released"
GROUP_CAP = "group_cap"
PREVIOUS_LESSON_INCOMPLETE = "previous_lesson_incomplete"
PREVIOUS_MODULE_INCOMPLETE = "previous_module_incomplete"
NOT_IN_SEQUENCE = "not_in_sequence"

# The reason shown when a gate fires but the checkpoint behind it cannot be named.
_UNNAMED_CHECKPOINT = "контрольную работу"


class LessonAccessDenied(HTTPException):
    """An HTTPException that also carries a reason code and optional structured details.

    `detail` stays the Russian sentence so the ~90 places in the web client that already read
    `response.data.detail` keep working; `reason_code` is what new code should branch on.
    """

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        details: Optional[Dict[str, Any]] = None,
    ):
        super().__init__(status_code=status_code, detail=message)
        self.reason_code = code
        self.reason_details = details or {}

    def as_payload(self) -> Dict[str, Any]:
        """The same refusal rendered as a `check-access` body.

        `check-access` answers 200 with `accessible: false` instead of raising, so a locked
        lesson can be greyed out in the sidebar rather than thrown as an error. Building it from
        the exception keeps one wording per reason across both surfaces.
        """
        payload: Dict[str, Any] = {
            "accessible": False,
            "reason": self.detail,
            "reason_code": self.reason_code,
        }
        if self.reason_details:
            payload["reason_details"] = self.reason_details
        return payload


def _checkpoint_details(definition) -> Dict[str, Any]:
    """The bits of a checkpoint the student may see: its number and its title."""
    if definition is None:
        return {}
    out: Dict[str, Any] = {}
    number = getattr(definition, "number", None)
    title = getattr(definition, "title", None)
    if number is not None:
        out["number"] = number
    if title:
        out["title"] = title
    return {"checkpoint": out} if out else {}


def _checkpoint_name(definition) -> str:
    title = getattr(definition, "title", None) if definition is not None else None
    return f"«{title}»" if title else _UNNAMED_CHECKPOINT


def lesson_not_found() -> LessonAccessDenied:
    return LessonAccessDenied(
        404,
        LESSON_NOT_FOUND,
        "Урок не найден. Возможно, его удалили или ссылка устарела.",
    )


def course_access_denied() -> LessonAccessDenied:
    return LessonAccessDenied(
        403,
        COURSE_ACCESS_DENIED,
        "У вас нет доступа к этому курсу. Если это ошибка, напишите куратору.",
    )


def role_denied() -> LessonAccessDenied:
    return LessonAccessDenied(
        403,
        ROLE_DENIED,
        "У вашей роли нет доступа к материалам урока.",
    )


def trial_locked(reason: Optional[str] = None) -> LessonAccessDenied:
    """A trial student outside their allowlist. `reason` comes from the trial service and is
    already student-facing; it is kept when present so an expiry reads as an expiry."""
    return LessonAccessDenied(
        403,
        TRIAL_LOCKED,
        reason or "Этот урок не входит в пробный доступ.",
    )


def checkpoint_locked(definition) -> LessonAccessDenied:
    """A unit that waits for an earlier, still-unsubmitted checkpoint."""
    return LessonAccessDenied(
        403,
        CHECKPOINT_LOCKED,
        f"Сначала пройдите {_checkpoint_name(definition)} — после неё этот юнит откроется.",
        _checkpoint_details(definition),
    )


def checkpoint_not_open(definition, missing_units: Optional[List[str]] = None) -> LessonAccessDenied:
    """The checkpoint quiz itself, opened before the checkpoint is available to this student.

    When the required units are known they are listed, because that is the only thing the student
    can act on: finishing them is what opens the checkpoint.
    """
    name = _checkpoint_name(definition)
    titles = [t for t in (missing_units or []) if t]
    if titles:
        message = (
            f"Контрольная работа {name} ещё не открыта. "
            f"Сначала пройдите: {', '.join(titles)}."
        )
    else:
        message = f"Контрольная работа {name} ещё не открыта."
    details = _checkpoint_details(definition)
    if titles:
        details["missing_units"] = titles
    return LessonAccessDenied(403, CHECKPOINT_NOT_OPEN, message, details)


# ---------------------------------------------------------------- sequential access
# Raised the same way as the gates above so the wording is defined once, but usually rendered
# through `as_payload()` — `check-access` reports these as a 200 the sidebar can grey out.


def module_not_released(module_week: int, current_week: int) -> LessonAccessDenied:
    return LessonAccessDenied(
        403,
        MODULE_NOT_RELEASED,
        f"Этот модуль откроется на {module_week}-й неделе программы — сейчас идёт {current_week}-я.",
        {"module_week": module_week, "current_week": current_week},
    )


def group_cap(reason: Optional[str] = None) -> LessonAccessDenied:
    return LessonAccessDenied(
        403,
        GROUP_CAP,
        reason or "Этот урок пока недоступен для вашей группы.",
    )


def previous_lesson_incomplete(title: Optional[str]) -> LessonAccessDenied:
    """Note that only the lesson's title is named — the module id and list index the old message
    carried were internal and meant nothing to a student."""
    message = (
        f"Сначала пройдите предыдущий урок: «{title}»." if title
        else "Сначала пройдите предыдущий урок."
    )
    return LessonAccessDenied(403, PREVIOUS_LESSON_INCOMPLETE, message)


def previous_module_incomplete(title: Optional[str]) -> LessonAccessDenied:
    message = (
        f"Сначала завершите все уроки модуля «{title}»." if title
        else "Сначала завершите все уроки предыдущего модуля."
    )
    return LessonAccessDenied(403, PREVIOUS_MODULE_INCOMPLETE, message)


def not_in_sequence() -> LessonAccessDenied:
    return LessonAccessDenied(
        403,
        NOT_IN_SEQUENCE,
        "Этот урок не входит в вашу программу — вернитесь к списку курса и продолжите оттуда.",
    )
