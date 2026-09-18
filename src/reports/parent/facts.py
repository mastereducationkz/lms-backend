"""Сбор всего, что произошло с учеником за одну неделю.

Единственный модуль пакета, который ходит в базу и на внешние платформы. Он ничего не
решает и не формулирует — только собирает ``WeekFacts``; выбор шаблона живёт в
``template``, проза в ``prose``.
"""
import logging
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy.orm import Session

from src.reports.external import _group_programs, fetch_weekly_tests
from src.reports.parent.week import week_bounds, week_utc_range
from src.schemas.models import (
    Assignment,
    AssignmentSubmission,
    Attendance,
    Event,
    EventGroup,
    Group,
    GroupStudent,
    Lesson,
    UserInDB,
)
from src.progress.models import QuizAttempt
from src.services import meet_talk_stats
from src.services.attendance_status import is_excused, is_marked, normalize_status

logger = logging.getLogger("parent_reports.facts")

#: Квиз выше этого — сильная сторона, ниже нижнего — слабая. Между ними отчёт молчит.
STRENGTH_PCT = 85.0
WEAKNESS_PCT = 60.0

#: Разрыв между секциями, начиная с которого отставшая секция считается слабой зоной.
SECTION_GAP_PP = 15.0


def no_growth_streak(history: List[Dict[str, Any]]) -> int:
    """Сколько последних тестов подряд не дали роста ни по одной секции.

    ``history`` — по возрастанию даты. Рост хотя бы по одной секции обнуляет счётчик:
    отчёт не должен объявлять застой ученику, который вырос по математике.
    """
    streak = 0
    for prev, current in zip(history, history[1:]):
        grew = False
        for key in ("verbal", "math"):
            was = (prev.get(key) or {}).get("correct")
            now = (current.get(key) or {}).get("correct")
            if was is not None and now is not None and now > was:
                grew = True
        streak = 0 if grew else streak + 1
    return streak


def _pct(side: Optional[Dict[str, Any]]) -> Optional[float]:
    if not side or not side.get("total"):
        return None
    return side["correct"] / side["total"] * 100


def pick_candidates(
    quizzes: List[Dict[str, Any]],
    test: Optional[Dict[str, Any]],
) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """Кандидаты в сильную и слабую сторону — только посчитанные, никогда выдуманные.

    Два источника ярлыка: квизы за неделю (у них есть название темы) и разрыв между
    секциями теста. Третий источник из спеки — одобренный фидбэк преподавателя — сюда
    не входит намеренно: это свободный текст, вытащить из него ярлык темы детерминированно
    нельзя. Он работает иначе — лежит в ``facts["teacher_feedback"]`` и разрешает слот
    ``weakness`` у модели даже без ярлыка (см. ``prose.requested_slots``).

    Не сработало ничего — возвращаем ``None``, и строка в отчёт не попадёт вовсе.
    """
    strength = weakness = None

    best = max(quizzes, key=lambda q: q["average_pct"], default=None)
    worst = min(quizzes, key=lambda q: q["average_pct"], default=None)
    if best and best["average_pct"] >= STRENGTH_PCT:
        strength = {"label": best["lesson_title"], "source": "quiz",
                    "pct": round(best["average_pct"])}
    if worst and worst["average_pct"] <= WEAKNESS_PCT:
        weakness = {"label": worst["lesson_title"], "source": "quiz",
                    "pct": round(worst["average_pct"])}

    if weakness is None and test:
        verbal, math = _pct(test.get("verbal")), _pct(test.get("math"))
        if verbal is not None and math is not None and abs(verbal - math) >= SECTION_GAP_PP:
            label = "Verbal" if verbal < math else "Math"
            weakness = {"label": label, "source": "section_gap",
                        "pct": round(min(verbal, math))}

    return strength, weakness


def _attendance(db: Session, student_id: int, group_ids: List[int],
                start: datetime, end: datetime) -> Dict[str, Any]:
    events = (
        db.query(Event)
        .join(EventGroup, EventGroup.event_id == Event.id)
        .filter(
            EventGroup.group_id.in_(group_ids or [0]),
            Event.event_type == "class",
            Event.is_active == True,  # noqa: E712 — SQLAlchemy comparison
            Event.start_datetime >= start,
            Event.start_datetime < end,
        )
        .distinct()
        .all()
    )
    rows = {
        a.event_id: a
        for a in db.query(Attendance).filter(
            Attendance.user_id == student_id,
            Attendance.event_id.in_([e.id for e in events] or [0]),
        ).all()
    }

    lessons = present = late = 0
    absences: List[Dict[str, Any]] = []
    for event in sorted(events, key=lambda e: e.start_datetime or datetime.min):
        row = rows.get(event.id)
        # ``is_marked``, а не «статус непустой»: ``registered`` пишется в сетку, когда
        # ячейку сохранили, не отметив ученика, и ``normalize_status`` даёт по нему
        # ``unknown``. Без этой проверки такой урок попадал в счётчик занятий, но ни в
        # присутствия, ни в пропуски — отчёт сам себе противоречил: «занятий 1», при
        # этом присутствий 0 и пропусков нет.
        if not row or not is_marked(row.status):
            continue
        status = normalize_status(row.status)
        lessons += 1
        if status == "present":
            present += 1
            # ``late`` не сворачивается в present: отчёт показывает опоздания отдельно.
            if (row.status or "").strip().lower() == "late":
                late += 1
        elif status == "absent":
            absences.append({
                "date": event.start_datetime.date().isoformat(),
                "excused": is_excused(row.status, row.excused),
            })
    return {"lessons": lessons, "present": present, "late": late, "absences": absences}


def _homework(db: Session, student_id: int, group_ids: List[int],
              start: datetime, end: datetime) -> Optional[Dict[str, Any]]:
    """None означает «за неделю ничего не задавали» — секция выпадает из отчёта."""
    assignments = db.query(Assignment).filter(
        Assignment.group_id.in_(group_ids or [0]),
        Assignment.is_active == True,  # noqa: E712
        Assignment.is_hidden == False,  # noqa: E712
        Assignment.due_date >= start,
        Assignment.due_date < end,
    ).all()
    if not assignments:
        return None

    submitted_ids = {
        s.assignment_id
        for s in db.query(AssignmentSubmission).filter(
            AssignmentSubmission.user_id == student_id,
            AssignmentSubmission.assignment_id.in_([a.id for a in assignments]),
            AssignmentSubmission.is_current == True,  # noqa: E712
        ).all()
    }
    missing = [(a.title or "").strip() for a in assignments if a.id not in submitted_ids]
    return {
        "assigned": len(assignments),
        "submitted": len(submitted_ids),
        "missing": missing,
    }


def _quizzes(db: Session, student_id: int, start: datetime, end: datetime) -> List[Dict[str, Any]]:
    attempts = db.query(QuizAttempt).filter(
        QuizAttempt.user_id == student_id,
        QuizAttempt.is_draft == False,  # noqa: E712
        QuizAttempt.completed_at >= start,
        QuizAttempt.completed_at < end,
    ).all()
    if not attempts:
        return []
    titles = {
        lesson.id: (lesson.title or "").strip()
        for lesson in db.query(Lesson).filter(
            Lesson.id.in_({a.lesson_id for a in attempts if a.lesson_id} or [0])
        ).all()
    }
    by_lesson: Dict[int, List[float]] = {}
    for a in attempts:
        if a.lesson_id:
            by_lesson.setdefault(a.lesson_id, []).append(a.score_percentage)
    return [
        {"lesson_title": titles.get(lesson_id) or f"Lesson {lesson_id}",
         "average_pct": sum(scores) / len(scores)}
        for lesson_id, scores in by_lesson.items()
        if titles.get(lesson_id)
    ]


def _nuet_week_label(group: Optional[Group], week_start: date) -> Optional[str]:
    """Какой «Week N» платформы соответствует отчётной неделе.

    NUET-наборы адресуются номером недели курса: ``_fetch_nuet`` не возвращает
    ``completed_at`` вовсе. Без пересчёта пришлось бы брать последнюю запись истории —
    то есть на отчёте за прошлую неделю показать родителю свежие цифры под старой датой.

    **Точного соответствия не существует.** Платформа режет курс на семидневки от
    момента создания группы (``_fetch_nuet``: ``(now - started).days // 7``), а отчётная
    неделя — это понедельник-воскресенье по Алматы. Сетки совпадают только если группу
    завели в понедельник в полночь; в остальных случаях отчётная неделя накрывает две
    недели платформы. Поэтому берём ту, на которую приходится большая часть отчётной
    недели, — отсчёт от её середины. Отсчитывать от края (понедельника или воскресенья)
    значило бы систематически промахиваться на неделю у групп, стартовавших в середине.

    Середина считается в наивном UTC через ``week_utc_range``: ``started`` тоже наивный
    UTC, а смешивать его с алматинской календарной датой — это те же пять часов сдвига,
    ради которых ``week_utc_range`` вообще написан.
    """
    if group is None:
        return None
    started = getattr(group, "created_at", None)
    if started is None:
        return None
    if started.tzinfo is not None:
        started = started.replace(tzinfo=None)
    start_utc, end_utc = week_utc_range(week_start)
    midpoint = start_utc + (end_utc - start_utc) / 2
    offset = getattr(group, "weekly_set_week_offset", 0) or 0
    number = ((midpoint - started).days // 7) + 1 - offset
    return f"Week {number}" if number >= 1 else None


def _select_test(weekly: Dict[str, Any], week_start: date, week_end: date,
                 nuet_label: Optional[str] = None
                 ) -> Tuple[Optional[Dict[str, Any]], List[Dict[str, Any]], Optional[str]]:
    """Тест недели, полная история и фидбэк преподавателя.

    «Прошлая неделя» для дельты — предыдущая запись в истории платформы, а не
    календарный минус семь дней: у SAT и NUET свои week-лейблы, и на пропущенной
    неделе календарный сдвиг сравнил бы результат не с тем тестом.
    """
    # IELTS сюда не входит: платформа отдаёт полосы (bands) по четырём навыкам, а не
    # Verbal/Math correct/total, и в шаблоны родительского отчёта они не ложатся.
    # Ученик IELTS-группы всё равно получает отчёт — без блока с результатами теста.
    for program in ("sat", "nuet"):
        weeks = weekly.get(program) or []
        if not weeks:
            continue
        history = [
            {
                "label": w.get("week_label"),
                "date": (w.get("completed_at") or "")[:10] or None,
                "verbal": {"correct": (w.get("verbal") or {}).get("correct"),
                           "total": (w.get("verbal") or {}).get("total")},
                "math": {"correct": (w.get("math") or {}).get("correct"),
                         "total": (w.get("math") or {}).get("total")},
                "feedback": (w.get("verbal") or {}).get("feedback")
                or (w.get("math") or {}).get("feedback"),
            }
            for w in weeks
        ]
        index = None
        if any(h["date"] for h in history):
            for i in range(len(history) - 1, -1, -1):
                day = history[i]["date"]
                if day and week_start.isoformat() <= day <= week_end.isoformat():
                    index = i
                    break
        elif nuet_label:
            # Сопоставляем по номеру недели курса, а не берём последнюю запись: последняя
            # запись — это «сейчас», а отчёт могут перегенерировать за любую прошлую неделю.
            for i, item in enumerate(history):
                if item["label"] == nuet_label:
                    index = i
                    break
        if index is None:
            # Теста за эту неделю нет. Историю отдаём пустой, чтобы ``no_growth_streak``
            # не посчитался по записям, которых на отчётной неделе ещё не существовало.
            return None, [], None
        current = history[index]
        prev = history[index - 1] if index > 0 else None
        delta = None
        if prev:
            delta = {}
            for key in ("verbal", "math"):
                was, now = prev[key].get("correct"), current[key].get("correct")
                delta[key] = (now - was) if (was is not None and now is not None) else None
        test = {
            "program": program,
            "label": current["label"],
            "date": current["date"],
            "verbal": current["verbal"],
            "math": current["math"],
            "prev": {"label": prev["label"], "verbal": prev["verbal"], "math": prev["math"]}
            if prev else None,
            "delta": delta,
        }
        return test, history[: index + 1], current.get("feedback")
    return None, [], None


async def build_week_facts(
    db: Session,
    student_id: int,
    week_start: date,
    group_id: Optional[int] = None,
    curator_note: Optional[str] = None,
) -> Dict[str, Any]:
    """Собрать ``WeekFacts`` для одного ученика за одну неделю."""
    student = db.query(UserInDB).filter(
        UserInDB.id == student_id, UserInDB.role == "student"
    ).first()
    if not student:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Student not found")

    _, week_end = week_bounds(week_start)
    start, end = week_utc_range(week_start)

    group_rows = (
        db.query(Group).join(GroupStudent, GroupStudent.group_id == Group.id)
        .filter(GroupStudent.student_id == student_id).all()
    )
    group_ids = [g.id for g in group_rows]
    group = next((g for g in group_rows if g.id == group_id), None) or (
        group_rows[0] if group_rows else None
    )

    weekly = await fetch_weekly_tests(db, student)
    # Классификацию программы берём у ``external._group_programs``, а не повторяем:
    # ярлык недели обязан считаться по той же группе, по которой платформу и опрашивали.
    nuet_groups = _group_programs(db, student_id).get("nuet") or []
    nuet_label = _nuet_week_label(nuet_groups[0] if nuet_groups else None, week_start)
    test, history, teacher_feedback = _select_test(weekly, week_start, week_end, nuet_label)

    quizzes = _quizzes(db, student_id, start, end)
    strength, weakness = pick_candidates(quizzes, test)

    talk = meet_talk_stats.student_talk(db, student_id, date_from=start, date_to=end)

    return {
        "student": {"id": student.id, "name": student.name},
        "group": {"id": group.id if group else None, "name": group.name if group else None},
        "week": {"start": week_start.isoformat(), "end": week_end.isoformat()},
        "test": test,
        "test_unavailable": bool(weekly.get("errors")) and test is None,
        "homework": _homework(db, student_id, group_ids, start, end),
        "attendance": _attendance(db, student_id, group_ids, start, end),
        "talk": (talk or {}).get("totals") if talk else None,
        "strength": strength,
        "weakness": weakness,
        "teacher_feedback": teacher_feedback,
        "no_growth_streak": no_growth_streak(history),
        "curator_note": curator_note,
    }
