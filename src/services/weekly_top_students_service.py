"""Weekly Top Students — composite activity leaderboard for the admin dashboard.

Ranks students for a given ISO week (Asia/Almaty, Monday start) by a single
0–100 **Activity Score** that combines every dimension of weekly activity:

    Homework 35 · Course progress 35 · Study time 10 · Points & streak 20

Each dimension becomes a 0–100 subscore; the composite is their weighted sum.
Because three of the four dimensions are normalized *relative to the cohort*
(against the 95th percentile, to tame outliers), the whole filtered cohort must
be scored in Python before we can sort and take the Top-N — this cannot be a
plain SQL ``ORDER BY … LIMIT``.

Week semantics reuse ``_get_week_monday`` / ``TZ`` from ``src.curator.services``
so "this week" matches the curator scheduler (Almaty, not server-local).
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import func, case
from sqlalchemy.orm import Session

from src.curator.services import TZ, _get_week_monday
from src.schemas.models import (
    UserInDB,
    Group,
    GroupStudent,
    Assignment,
    AssignmentSubmission,
    StepProgress,
    StudentCourseSummary,
    PointHistory,
    DailyQuestionCompletion,
    Course,
)

# Default dimension weights (sum to 1.0). Tunable here; a follow-up could expose
# them as query params. When no homework is due this week, the homework slice
# counts as 0 (not dropped) — so a student with no homework activity cannot top
# the board on the other dimensions alone (max ~65/100).
WEIGHTS = {
    "homework": 0.35,
    "course": 0.35,
    "study": 0.10,
    "engagement": 0.20,
}


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #

def resolve_week(week_start: Optional[date]) -> tuple[date, date]:
    """Return (monday_date, sunday_date) for the target Almaty ISO week.

    If ``week_start`` is given it is snapped to the Monday of its ISO week;
    otherwise the current Almaty week's Monday is used.
    """
    if week_start is None:
        monday = _get_week_monday(datetime.now(TZ)).date()
    else:
        monday = week_start - timedelta(days=week_start.weekday())
    return monday, monday + timedelta(days=6)


def _week_bounds_utc(monday: date) -> tuple[datetime, datetime]:
    """Naive-UTC [start, end) datetimes for the Almaty week starting ``monday``.

    Timestamp columns store naive UTC, so we convert the Almaty Monday 00:00
    boundary into naive UTC for range comparisons.
    """
    monday_almaty = TZ.localize(datetime.combine(monday, time.min))
    start_utc = monday_almaty.astimezone(timezone.utc).replace(tzinfo=None)
    end_utc = (monday_almaty + timedelta(days=7)).astimezone(timezone.utc).replace(tzinfo=None)
    return start_utc, end_utc


def _percentile(values: List[float], pct: float) -> float:
    """Nearest-rank percentile of ``values`` (0 if empty). ``pct`` in [0, 1]."""
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return float(ordered[0])
    idx = int(round(pct * (len(ordered) - 1)))
    return float(ordered[idx])


def _norm(value: float, cap: float) -> float:
    """Normalize a magnitude to 0–100 against a cohort cap (95th pct)."""
    if cap <= 0:
        return 0.0
    return min(value / cap, 1.0) * 100.0


# --------------------------------------------------------------------------- #
# Main entry point
# --------------------------------------------------------------------------- #

def compute_weekly_top_students(
    db: Session,
    week_start: Optional[date] = None,
    *,
    program_type: Optional[str] = None,
    group_id: Optional[int] = None,
    curator_id: Optional[int] = None,
    teacher_id: Optional[int] = None,
    limit: int = 100,
) -> Dict[str, Any]:
    monday, sunday = resolve_week(week_start)
    start_utc, end_utc = _week_bounds_utc(monday)

    # 1) Cohort: active, non-hidden students, scoped by group filters. -------- #
    membership_q = (
        db.query(
            GroupStudent.student_id,
            Group.id.label("group_id"),
            Group.name.label("group_name"),
            Group.teacher_id,
            Group.curator_id,
            Group.program_type,
            Group.is_over,
            Group.created_at,
        )
        .join(Group, Group.id == GroupStudent.group_id)
        .filter(Group.is_active.is_(True))
    )
    if program_type:
        membership_q = membership_q.filter(Group.program_type == program_type)
    if group_id is not None:
        membership_q = membership_q.filter(Group.id == group_id)
    if curator_id is not None:
        membership_q = membership_q.filter(Group.curator_id == curator_id)
    if teacher_id is not None:
        membership_q = membership_q.filter(Group.teacher_id == teacher_id)
    memberships = membership_q.all()

    if not memberships:
        return _empty_result(monday, sunday, limit)

    candidate_ids = {m.student_id for m in memberships}

    # Keep only genuine, active, non-hidden students.
    valid_rows = (
        db.query(UserInDB.id, UserInDB.name, UserInDB.daily_streak)
        .filter(
            UserInDB.id.in_(candidate_ids),
            UserInDB.role == "student",
            UserInDB.is_active.is_(True),
            UserInDB.is_analytics_hidden.is_(False),
        )
        .all()
    )
    student_ids = [r.id for r in valid_rows]
    if not student_ids:
        return _empty_result(monday, sunday, limit)

    student_name = {r.id: r.name for r in valid_rows}
    student_streak = {r.id: (r.daily_streak or 0) for r in valid_rows}
    valid_set = set(student_ids)

    # Primary group per student (for display): prefer active & not-over, then newest.
    # Group scope is driven by the *filtered* memberships, so a program/group
    # filter scopes both the displayed group and the homework to that selection
    # (e.g. an IELTS filter shows only IELTS-group homework).
    primary_group: Dict[int, Any] = {}
    student_group_ids: Dict[int, set] = {}
    for m in memberships:
        if m.student_id not in valid_set:
            continue
        student_group_ids.setdefault(m.student_id, set()).add(m.group_id)
        cur = primary_group.get(m.student_id)
        # Rank candidate: (not is_over) first, then most recent created_at.
        cand_key = (0 if m.is_over else 1, m.created_at or datetime.min)
        if cur is None or cand_key > cur["_key"]:
            primary_group[m.student_id] = {
                "_key": cand_key,
                "group_id": m.group_id,
                "group_name": m.group_name,
                "teacher_id": m.teacher_id,
                "curator_id": m.curator_id,
                "program_type": m.program_type,
            }

    # Teacher / curator display names.
    staff_ids = {
        pid
        for g in primary_group.values()
        for pid in (g["teacher_id"], g["curator_id"])
        if pid is not None
    }
    staff_name = {
        r.id: r.name
        for r in db.query(UserInDB.id, UserInDB.name).filter(UserInDB.id.in_(staff_ids)).all()
    } if staff_ids else {}

    # 2) Homework this week (due for the student's in-scope groups). ---------- #
    # student_group_ids reflects the active filter, so an IELTS filter counts only
    # IELTS-group homework.
    hw = _homework_metrics(db, student_group_ids, start_utc, end_utc)

    # 3) Course steps + study time this week. -------------------------------- #
    steps = _step_metrics(db, student_ids, start_utc, end_utc)

    # 4) Engagement: weekly points + daily-question completions. -------------- #
    engagement = _engagement_metrics(db, student_ids, start_utc, end_utc, monday, sunday)

    # Cohort caps (95th percentile) for magnitude normalization.
    cap_steps = _percentile([steps[s]["steps_week"] for s in student_ids], 0.95)
    cap_study = _percentile([steps[s]["study_minutes"] for s in student_ids], 0.95)
    cap_points = _percentile([engagement[s]["points_week"] for s in student_ids], 0.95)

    # 5) Score every student, then sort + limit. ----------------------------- #
    rows: List[Dict[str, Any]] = []
    needs_attention: List[Dict[str, Any]] = []
    for sid in student_ids:
        pg = primary_group.get(sid)
        if pg is None:
            continue
        h = hw.get(sid, _empty_hw())
        st = steps[sid]
        en = engagement[sid]

        hw_sub = _homework_subscore(h)
        course_sub = _norm(st["steps_week"], cap_steps)
        study_sub = _norm(st["study_minutes"], cap_study)
        points_sub = _norm(en["points_week"], cap_points)
        streak_sub = min(student_streak[sid] / 7.0, 1.0) * 100.0
        engagement_sub = 0.7 * points_sub + 0.3 * streak_sub

        composite = _composite(hw_sub, course_sub, study_sub, engagement_sub)

        teacher_name = staff_name.get(pg["teacher_id"], "—")
        curator_name = staff_name.get(pg["curator_id"], "—")

        rows.append({
            "student_id": sid,
            "student_name": student_name[sid],
            "group_name": pg["group_name"],
            "teacher_name": teacher_name,
            "curator_name": curator_name,
            "program_type": pg["program_type"],
            "homework": {
                "due": h["due"],
                "submitted": h["submitted"],
                "on_time": h["on_time"],
                "late": h["submitted"] - h["on_time"],
                "missing": h["due"] - h["submitted"],
                "avg_pct": round(h["avg_pct"], 1) if h["avg_pct"] is not None else None,
                "subscore": round(hw_sub, 1) if hw_sub is not None else None,
            },
            "steps_week": st["steps_week"],
            "top_course_name": st["top_course_name"],
            "course_pct_week": round(st["course_pct_week"], 1),
            "course_pct_total": round(st["course_pct_total"], 1),
            "study_minutes_week": st["study_minutes"],
            "points_week": en["points_week"],
            "daily_streak": student_streak[sid],
            "daily_questions_week": en["daily_questions"],
            "subscores": {
                "homework": round(hw_sub, 1) if hw_sub is not None else None,
                "course": round(course_sub, 1),
                "study": round(study_sub, 1),
                "engagement": round(engagement_sub, 1),
            },
            "activity_score": round(composite, 1),
        })

        if h["due"] > 0 and (h["submitted"] < h["due"] or h["on_time"] < h["submitted"]):
            needs_attention.append({
                "student_id": sid,
                "student_name": student_name[sid],
                "group_name": pg["group_name"],
                "teacher_name": teacher_name,
                "curator_name": curator_name,
                "due": h["due"],
                "submitted": h["submitted"],
                "on_time": h["on_time"],
            })

    rows.sort(key=lambda r: r["activity_score"], reverse=True)
    total = len(rows)
    top = rows[:limit]
    for i, r in enumerate(top, start=1):
        r["rank"] = i

    needs_attention.sort(
        key=lambda r: (r["due"] - r["submitted"], r["submitted"] - r["on_time"]),
        reverse=True,
    )

    return {
        "week_start": monday.isoformat(),
        "week_end": sunday.isoformat(),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "total_students": total,
        "limit": limit,
        "weights": WEIGHTS,
        "students": top,
        "needs_attention": needs_attention[:50],
    }


# --------------------------------------------------------------------------- #
# Per-dimension aggregation
# --------------------------------------------------------------------------- #

def _empty_hw() -> Dict[str, Any]:
    return {"due": 0, "submitted": 0, "on_time": 0, "avg_pct": None}


def _homework_metrics(
    db: Session,
    student_group_ids: Dict[int, set],
    start_utc: datetime,
    end_utc: datetime,
) -> Dict[int, Dict[str, Any]]:
    """HW due this week (per group) + the student's submissions to them.

    Deadlined homework in this platform is group-scoped directly on the
    ``assignments`` row: ``assignments.group_id`` identifies the group and
    ``assignments.due_date`` the deadline (the ``group_assignments`` binding
    table is unused). So "due this week for the student's active group" =
    assignments whose group_id is one of the student's groups with a due_date in
    the week. Submissions are matched by assignment_id regardless of submission
    time (late still counts as submitted); on-time uses the submission's
    ``is_late`` flag (already computed with any per-student extension applied).
    """
    all_group_ids = {g for gids in student_group_ids.values() for g in gids}
    if not all_group_ids:
        return {}

    # group_id -> set(assignment_id) due this week
    ga_rows = (
        db.query(Assignment.group_id, Assignment.id)
        .filter(
            Assignment.group_id.in_(all_group_ids),
            Assignment.is_active.is_(True),
            Assignment.is_hidden.is_(False),
            Assignment.due_date >= start_utc,
            Assignment.due_date < end_utc,
        )
        .all()
    )
    group_due: Dict[int, set] = {}
    for gid, aid in ga_rows:
        group_due.setdefault(gid, set()).add(aid)

    # Per student: the set of assignments due this week (union over their groups).
    student_due: Dict[int, set] = {}
    all_due_assignments: set = set()
    for sid, gids in student_group_ids.items():
        due = set()
        for gid in gids:
            due |= group_due.get(gid, set())
        if due:
            student_due[sid] = due
            all_due_assignments |= due

    result: Dict[int, Dict[str, Any]] = {
        sid: {"due": len(due), "submitted": 0, "on_time": 0, "avg_pct": None}
        for sid, due in student_due.items()
    }
    if not all_due_assignments:
        return result

    # Submissions to any due assignment by any student in the cohort.
    sub_rows = (
        db.query(
            AssignmentSubmission.user_id,
            AssignmentSubmission.assignment_id,
            AssignmentSubmission.is_late,
            AssignmentSubmission.is_graded,
            AssignmentSubmission.score,
            AssignmentSubmission.max_score,
        )
        .filter(
            AssignmentSubmission.user_id.in_(student_due.keys()),
            AssignmentSubmission.assignment_id.in_(all_due_assignments),
            AssignmentSubmission.is_hidden.is_(False),
        )
        .all()
    )
    # Keep the best/last submission per (user, assignment) — dedupe resubmissions.
    seen: Dict[tuple, bool] = {}
    graded_pcts: Dict[int, List[float]] = {}
    for uid, aid, is_late, is_graded, score, max_score in sub_rows:
        if uid not in student_due or aid not in student_due[uid]:
            continue
        key = (uid, aid)
        if key not in seen:
            seen[key] = True
            result[uid]["submitted"] += 1
            if not is_late:
                result[uid]["on_time"] += 1
        if is_graded and score is not None and max_score:
            graded_pcts.setdefault(uid, []).append(score / max_score * 100.0)

    for uid, pcts in graded_pcts.items():
        if pcts:
            result[uid]["avg_pct"] = sum(pcts) / len(pcts)
    return result


def _step_metrics(
    db: Session,
    student_ids: List[int],
    start_utc: datetime,
    end_utc: datetime,
) -> Dict[int, Dict[str, Any]]:
    """Steps completed this week + study minutes, per student, with top course."""
    result = {
        sid: {
            "steps_week": 0,
            "study_minutes": 0,
            "top_course_name": None,
            "course_pct_week": 0.0,
            "course_pct_total": 0.0,
        }
        for sid in student_ids
    }

    # steps + minutes per (user, course) completed this week
    rows = (
        db.query(
            StepProgress.user_id,
            StepProgress.course_id,
            func.count(StepProgress.id).label("steps"),
            func.coalesce(func.sum(StepProgress.time_spent_minutes), 0).label("minutes"),
        )
        .filter(
            StepProgress.user_id.in_(student_ids),
            StepProgress.status == "completed",
            StepProgress.completed_at >= start_utc,
            StepProgress.completed_at < end_utc,
        )
        .group_by(StepProgress.user_id, StepProgress.course_id)
        .all()
    )
    top_course: Dict[int, tuple] = {}  # user -> (steps, course_id)
    for uid, cid, steps, minutes in rows:
        r = result[uid]
        r["steps_week"] += int(steps or 0)
        r["study_minutes"] += int(minutes or 0)
        if uid not in top_course or steps > top_course[uid][0]:
            top_course[uid] = (int(steps or 0), cid)

    if not top_course:
        return result

    # Course summaries for total step counts / overall completion, and names.
    user_course_pairs = {(uid, cid) for uid, (_, cid) in top_course.items()}
    course_ids = {cid for _, cid in top_course.values()}
    summaries = {
        (s.user_id, s.course_id): s
        for s in db.query(
            StudentCourseSummary.user_id,
            StudentCourseSummary.course_id,
            StudentCourseSummary.total_steps,
            StudentCourseSummary.completion_percentage,
        ).filter(StudentCourseSummary.user_id.in_(top_course.keys()))
        if (s.user_id, s.course_id) in user_course_pairs
    }
    course_title = {
        c.id: c.title
        for c in db.query(Course.id, Course.title).filter(Course.id.in_(course_ids)).all()
    }

    for uid, (steps_in_course, cid) in top_course.items():
        r = result[uid]
        r["top_course_name"] = course_title.get(cid)
        summ = summaries.get((uid, cid))
        total_steps = (summ.total_steps if summ else 0) or 0
        r["course_pct_total"] = float(summ.completion_percentage) if summ and summ.completion_percentage else 0.0
        r["course_pct_week"] = (steps_in_course / total_steps * 100.0) if total_steps else 0.0
    return result


def _engagement_metrics(
    db: Session,
    student_ids: List[int],
    start_utc: datetime,
    end_utc: datetime,
    monday: date,
    sunday: date,
) -> Dict[int, Dict[str, Any]]:
    """Weekly point_history sum + daily-question completions per student."""
    result = {sid: {"points_week": 0, "daily_questions": 0} for sid in student_ids}

    point_rows = (
        db.query(
            PointHistory.user_id,
            func.coalesce(func.sum(PointHistory.amount), 0),
        )
        .filter(
            PointHistory.user_id.in_(student_ids),
            PointHistory.created_at >= start_utc,
            PointHistory.created_at < end_utc,
        )
        .group_by(PointHistory.user_id)
        .all()
    )
    for uid, total in point_rows:
        result[uid]["points_week"] = int(total or 0)

    dq_rows = (
        db.query(
            DailyQuestionCompletion.user_id,
            func.count(DailyQuestionCompletion.id),
        )
        .filter(
            DailyQuestionCompletion.user_id.in_(student_ids),
            DailyQuestionCompletion.completed_date >= monday,
            DailyQuestionCompletion.completed_date <= sunday,
        )
        .group_by(DailyQuestionCompletion.user_id)
        .all()
    )
    for uid, cnt in dq_rows:
        result[uid]["daily_questions"] = int(cnt or 0)
    return result


# --------------------------------------------------------------------------- #
# Scoring
# --------------------------------------------------------------------------- #

def _homework_subscore(h: Dict[str, Any]) -> Optional[float]:
    """0–100 homework subscore, or None when no HW was due this week.

    50% on-time ratio + 30% submitted ratio + 20% avg graded %. If nothing is
    graded, the 20% term is dropped and the split renormalizes to 62.5/37.5.
    """
    due = h["due"]
    if due <= 0:
        return None
    on_time_ratio = h["on_time"] / due
    submitted_ratio = h["submitted"] / due
    if h["avg_pct"] is None:
        score = 62.5 * on_time_ratio + 37.5 * submitted_ratio
    else:
        score = 50.0 * on_time_ratio + 30.0 * submitted_ratio + 20.0 * (h["avg_pct"] / 100.0)
    return min(score, 100.0)


def _composite(
    hw_sub: Optional[float],
    course_sub: float,
    study_sub: float,
    engagement_sub: float,
) -> float:
    """Weighted composite (weights sum to 1.0).

    No homework due this week (``hw_sub is None``) counts as 0 for the homework
    slice — it is NOT dropped/renormalized. So a student with no homework activity
    can reach at most ~65/100 and cannot outrank a student who also did homework.
    """
    hw = hw_sub if hw_sub is not None else 0.0
    return (
        hw * WEIGHTS["homework"]
        + course_sub * WEIGHTS["course"]
        + study_sub * WEIGHTS["study"]
        + engagement_sub * WEIGHTS["engagement"]
    )


def _empty_result(monday: date, sunday: date, limit: int) -> Dict[str, Any]:
    return {
        "week_start": monday.isoformat(),
        "week_end": sunday.isoformat(),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "total_students": 0,
        "limit": limit,
        "weights": WEIGHTS,
        "students": [],
        "needs_attention": [],
    }
