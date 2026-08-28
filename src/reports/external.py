"""Weekly practice-test results from the external exam platforms.

Weekly Tests live on sat/nuet/ielts.mastereducation.kz, not in the LMS database —
the LMS reads them over the platforms' batch APIs exactly like the curator
leaderboard does. Each platform is fetched independently and a failure degrades to
an entry in ``errors`` instead of killing the report: the LMS-resident sections must
still render when a platform is down.
"""
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from src.schemas.models import Group, GroupStudent, UserInDB

# Walking backwards through IELTS weekly sets / NUET week numbers stops here — a
# hard bound so one report never turns into an unbounded crawl of the platform.
_MAX_WEEKS = 40


def _group_programs(db: Session, student_id: int) -> Dict[str, List[Group]]:
    """The student's groups bucketed by exam program (sat / ielts / nuet)."""
    rows = (
        db.query(Group)
        .join(GroupStudent, GroupStudent.group_id == Group.id)
        .filter(GroupStudent.student_id == student_id)
        .all()
    )
    buckets: Dict[str, List[Group]] = {"sat": [], "ielts": [], "nuet": []}
    for group in rows:
        program = (getattr(group, "program_type", None) or "").lower()
        name = (group.name or "").lower()
        for key in buckets:
            if program == key or key in name:
                buckets[key].append(group)
                break
    return buckets


def _feedback_text(test: Optional[Dict[str, Any]]) -> Optional[str]:
    """Approved teacher feedback for one SAT platform test, if any."""
    feedback = (test or {}).get("teacherFeedback")
    if isinstance(feedback, dict) and feedback.get("status") == "approved":
        return feedback.get("feedbackText") or None
    return None


def _sat_side(test: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not test or test.get("correctCount") is None:
        return None
    return {
        "test_name": test.get("testName"),
        "completed_at": test.get("completedAt"),
        "correct": test.get("correctCount"),
        "total": test.get("totalQuestions"),
        "pct": test.get("percentage"),
        "feedback": _feedback_text(test),
    }


async def _fetch_sat(email: str) -> List[Dict[str, Any]]:
    from src.services.sat_service import SATService

    payload = await SATService.fetch_batch_test_results([email])
    weeks: List[Dict[str, Any]] = []
    for entry in payload.get("results") or []:
        pairs = ((entry.get("data") or {}).get("testPairs")) or []
        for pair in pairs:
            math_side = _sat_side(pair.get("mathTest"))
            verbal_side = _sat_side(pair.get("verbalTest"))
            if not math_side and not verbal_side:
                continue
            name = ((pair.get("mathTest") or pair.get("verbalTest")) or {}).get("testName") or ""
            label = name.rsplit("(", 1)[1].rstrip(")") if "(" in name else name
            weeks.append({
                "week_label": label,
                "completed_at": (math_side or verbal_side)["completed_at"],
                "math": math_side,
                "verbal": verbal_side,
            })
    weeks.sort(key=lambda w: w["completed_at"] or "")
    return weeks


def _ielts_week(payload: Dict[str, Any], email: str) -> Optional[Dict[str, Any]]:
    for item in payload.get("results") or []:
        if (item.get("email") or "").lower() != email:
            continue
        bands = [item.get(k) for k in
                 ("listeningBand", "readingBand", "writingBand", "speakingBand", "overallBand")]
        if all(v is None for v in bands) and item.get("speakingStatus") is None:
            return None
        return {
            "set_id": payload.get("weeklySetId"),
            "week_label": payload.get("weeklySetTitle"),
            "listening_band": item.get("listeningBand"),
            "reading_band": item.get("readingBand"),
            "writing_band": item.get("writingBand"),
            "speaking_band": item.get("speakingBand"),
            "overall_band": item.get("overallBand"),
            "speaking_status": item.get("speakingStatus"),
            "feedback": {
                "listening": item.get("listeningFeedbackRu") or item.get("listeningFeedback"),
                "reading": item.get("readingFeedbackRu") or item.get("readingFeedback"),
                "writing": item.get("writingFeedbackRu") or item.get("writingFeedback"),
                "speaking": item.get("speakingFeedback"),
            },
        }
    return None


async def _fetch_ielts(email: str, joined_at: Optional[datetime]) -> List[Dict[str, Any]]:
    """Walk weekly sets backwards from today until before the student joined.

    The by-date endpoint resolves whichever set covers the probed date and reports
    the set's own window, so each response tells us where the previous set ends.
    """
    from src.services.ielts_service import IELTSService

    join_date = (joined_at.date() if joined_at else date.today() - timedelta(days=180))
    probe = date.today()
    seen: set = set()
    weeks: List[Dict[str, Any]] = []
    misses = 0

    for _ in range(_MAX_WEEKS):
        if probe < join_date - timedelta(days=7):
            break
        payload = await IELTSService.fetch_batch_scores_by_date([email], probe.strftime("%Y-%m-%d"))
        set_id = (payload or {}).get("weeklySetId")
        if not payload or set_id is None:
            misses += 1
            if misses >= 3:
                break
            probe -= timedelta(days=7)
            continue
        misses = 0
        if set_id not in seen:
            seen.add(set_id)
            week = _ielts_week(payload, email)
            if week:
                weeks.append(week)
        raw_from = payload.get("weeklySetDateFrom")
        try:
            probe = datetime.fromisoformat(str(raw_from).replace("Z", "+00:00")).date() - timedelta(days=1)
        except (ValueError, TypeError):
            probe -= timedelta(days=7)
    weeks.sort(key=lambda w: w["week_label"] or "")
    return weeks


async def _fetch_nuet(email: str, groups: List[Group]) -> List[Dict[str, Any]]:
    """NUET sets are addressed by course week number, not date."""
    from src.services.sat_service import SATService

    group = groups[0]
    started = getattr(group, "created_at", None) or datetime.now(timezone.utc)
    if started.tzinfo is not None:
        started = started.replace(tzinfo=None)
    offset = getattr(group, "weekly_set_week_offset", 0) or 0
    current_week = max(1, ((datetime.utcnow() - started).days // 7) + 1 - offset)

    weeks: List[Dict[str, Any]] = []
    for week in range(1, min(current_week, _MAX_WEEKS) + 1):
        payload = await SATService.fetch_batch_scores_by_week([email], week, exam_type="NUET")
        for item in payload.get("results") or []:
            if (item.get("email") or "").lower() != email:
                continue
            scores = SATService.extract_section_scores(item, exam_type="NUET")
            if scores["math_correct"] is None and scores["verbal_correct"] is None:
                continue
            weeks.append({
                "week_label": f"Week {week}",
                "math": {"correct": scores["math_correct"], "total": scores["math_total"]},
                "verbal": {"correct": scores["verbal_correct"], "total": scores["verbal_total"]},
            })
    return weeks


async def fetch_weekly_tests(db: Session, student: UserInDB) -> Dict[str, Any]:
    """Weekly test history for every exam platform the student's groups belong to."""
    result: Dict[str, Any] = {"sat": [], "ielts": [], "nuet": [], "errors": []}
    email = (student.email or "").lower()
    if not email:
        return result

    buckets = _group_programs(db, student.id)

    if buckets["sat"]:
        try:
            result["sat"] = await _fetch_sat(email)
        except Exception as exc:  # pragma: no cover - network failure path
            result["errors"].append(f"sat: {exc}")

    if buckets["ielts"]:
        joined = min(
            (link.created_at for link in db.query(GroupStudent).filter(
                GroupStudent.student_id == student.id,
                GroupStudent.group_id.in_([g.id for g in buckets["ielts"]]),
            ).all() if link.created_at is not None),
            default=None,
        )
        try:
            result["ielts"] = await _fetch_ielts(email, joined)
        except Exception as exc:  # pragma: no cover - network failure path
            result["errors"].append(f"ielts: {exc}")

    if buckets["nuet"]:
        try:
            result["nuet"] = await _fetch_nuet(email, buckets["nuet"])
        except Exception as exc:  # pragma: no cover - network failure path
            result["errors"].append(f"nuet: {exc}")

    return result
