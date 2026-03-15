"""
Curator Task Scheduler

Runs automatically every Monday at 09:00 Almaty time.
On startup, also checks if current week's tasks are missing and generates them.

Key logic:
- For each active group with schedule_config.start_date, calculates program_week
- Filters templates by applicable_from_week / applicable_to_week
- Skips groups that haven't started yet (program_week < 1)
- Deduplicates using (template_id, student_id/group_id, week_reference)
"""
import logging
import threading
import time
from datetime import datetime, timedelta, timezone, date as date_type, time as time_type
from typing import Optional
import pytz

from src.config import SessionLocal
from src.schemas.models import (
    CuratorTaskTemplate, CuratorTaskInstance,
    UserInDB, Group, GroupStudent, AssignmentZeroSubmission
)
from src.assignments.exam_dates import (
    parse_sat_target_date,
    resolve_legacy_sat_month,
    get_nearest_sat_date,
)

logger = logging.getLogger(__name__)

TZ = pytz.timezone("Asia/Almaty")
DAY_MAP = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
}

EXAM_COLLECTION_TEMPLATE_TITLE = "Сбор результатов экзамена"


def _ensure_exam_collection_template(db) -> CuratorTaskTemplate:
    template = (
        db.query(CuratorTaskTemplate)
        .filter(CuratorTaskTemplate.title == EXAM_COLLECTION_TEMPLATE_TITLE)
        .first()
    )
    if template:
        return template

    template = CuratorTaskTemplate(
        title=EXAM_COLLECTION_TEMPLATE_TITLE,
        description=(
            "Проверить, сдал ли ученик экзамен, собрать результат и зафиксировать его в LMS. "
            "Если экзамен перенесен, обновить плановую дату."
        ),
        task_type="exam_results_collection",
        scope="student",
        order_index=200,
        is_active=True,
    )
    db.add(template)
    db.commit()
    db.refresh(template)
    return template


def _get_student_groups(db, student_id: int) -> list[Group]:
    return (
        db.query(Group)
        .join(GroupStudent, GroupStudent.group_id == Group.id)
        .filter(
            GroupStudent.student_id == student_id,
            Group.is_active == True,
        )
        .all()
    )


def _get_curator_target(groups: list[Group]) -> tuple[int | None, int | None]:
    for group in groups:
        if group.curator_id:
            return group.curator_id, group.id
    return None, None


def _student_tracks(groups: list[Group], submission: AssignmentZeroSubmission | None) -> tuple[bool, bool]:
    lower_names = [g.name.lower() for g in groups if g.name]
    has_ielts_group = any("ielts" in name for name in lower_names)
    has_sat_group = any("sat" in name for name in lower_names)

    if submission is None:
        if has_ielts_group and not has_sat_group:
            return False, True
        return True, has_ielts_group

    has_ielts_data = bool(
        submission.ielts_target_date
        or submission.ielts_planned_test_date
        or submission.ielts_target_score
        or submission.has_passed_ielts_before
    )
    has_sat_data = bool(submission.sat_target_date)

    sat_track = has_sat_group or has_sat_data or not has_ielts_group
    ielts_track = has_ielts_group or has_ielts_data
    return sat_track, ielts_track


def _resolve_sat_planned_date(
    submission: AssignmentZeroSubmission | None,
    today: date_type,
) -> date_type:
    if submission is None:
        return get_nearest_sat_date(today)

    if submission.sat_planned_test_date:
        return submission.sat_planned_test_date

    sat_target_value = (submission.sat_target_date or "").strip()
    if sat_target_value == "":
        return get_nearest_sat_date(today)

    parsed = parse_sat_target_date(sat_target_value, reference_date=today)
    if parsed:
        return parsed

    return resolve_legacy_sat_month(sat_target_value, reference_date=today)


def _build_exam_due_date(trigger_date: date_type) -> datetime:
    return datetime.combine(trigger_date, time_type(hour=18, minute=0), tzinfo=timezone.utc)


def _create_exam_collection_task_if_needed(
    db,
    template: CuratorTaskTemplate,
    *,
    curator_id: int,
    group_id: int,
    student_id: int,
    exam_type: str,
    planned_date: date_type,
    today: date_type,
) -> int:
    trigger_date = planned_date + timedelta(days=13)
    if today < trigger_date:
        return 0

    dedupe_key = f"exam-result:{exam_type}:{planned_date.isoformat()}"
    exists = (
        db.query(CuratorTaskInstance)
        .filter(
            CuratorTaskInstance.template_id == template.id,
            CuratorTaskInstance.student_id == student_id,
            CuratorTaskInstance.week_reference == dedupe_key,
        )
        .first()
    )
    if exists:
        return 0

    db.add(
        CuratorTaskInstance(
            template_id=template.id,
            curator_id=curator_id,
            student_id=student_id,
            group_id=group_id,
            status="pending",
            due_date=_build_exam_due_date(trigger_date),
            week_reference=dedupe_key,
            custom_title=f"Сбор результата {exam_type.upper()} ({planned_date.isoformat()})",
        )
    )
    return 1


def generate_exam_result_collection_tasks(db, reference_date: date_type | None = None) -> int:
    today = reference_date or date_type.today()
    template = _ensure_exam_collection_template(db)

    students = (
        db.query(UserInDB)
        .filter(
            UserInDB.role == "student",
            UserInDB.is_active == True,
        )
        .all()
    )

    created_count = 0
    has_backfill_updates = False
    for student in students:
        groups = _get_student_groups(db, student.id)
        curator_id, group_id = _get_curator_target(groups)
        if curator_id is None or group_id is None:
            continue

        submission = (
            db.query(AssignmentZeroSubmission)
            .filter(AssignmentZeroSubmission.user_id == student.id)
            .first()
        )
        sat_track, ielts_track = _student_tracks(groups, submission)

        if sat_track:
            sat_planned_date = _resolve_sat_planned_date(submission, today=today)

            if submission and submission.sat_planned_test_date is None:
                submission.sat_planned_test_date = sat_planned_date
                has_backfill_updates = True

            sat_result_missing = (
                submission is None
                or (not submission.sat_result_score and submission.sat_result_test_date is None)
            )
            if sat_result_missing:
                created_count += _create_exam_collection_task_if_needed(
                    db,
                    template,
                    curator_id=curator_id,
                    group_id=group_id,
                    student_id=student.id,
                    exam_type="sat",
                    planned_date=sat_planned_date,
                    today=today,
                )

        if ielts_track and submission and submission.ielts_planned_test_date:
            ielts_result_missing = not submission.ielts_result_score and submission.ielts_result_test_date is None
            if ielts_result_missing:
                created_count += _create_exam_collection_task_if_needed(
                    db,
                    template,
                    curator_id=curator_id,
                    group_id=group_id,
                    student_id=student.id,
                    exam_type="ielts",
                    planned_date=submission.ielts_planned_test_date,
                    today=today,
                )

    if created_count > 0 or has_backfill_updates:
        db.commit()
        if created_count > 0:
            logger.info(f"[SCHEDULER] Generated {created_count} exam-result collection tasks")
    else:
        db.rollback()

    return created_count


def _calc_program_week(group: Group, reference_date: Optional[date_type] = None) -> Optional[int]:
    try:
        cfg = group.schedule_config or {}
        start_str = cfg.get("start_date")
        if not start_str:
            return None
        start = date_type.fromisoformat(start_str)
        ref = reference_date if reference_date is not None else date_type.today()
        delta = (ref - start).days
        if delta < 0:
            return None
        return delta // 7 + 1
    except Exception:
        return None


def _group_has_start_date(group: Group) -> bool:
    cfg = group.schedule_config or {}
    return bool(cfg.get("start_date"))


def _calc_total_weeks(group: Group) -> Optional[int]:
    try:
        import math
        cfg = group.schedule_config or {}
        lessons_count = cfg.get("lessons_count")
        items = cfg.get("schedule_items", [])
        lessons_per_week = max(len(items), 1)
        if lessons_count:
            return math.ceil(lessons_count / lessons_per_week)
        return cfg.get("weeks_count")
    except Exception:
        return None


def _due_from_rule(rule: dict, monday_dt: datetime) -> Optional[datetime]:
    if not rule:
        return None
    if "day_of_week" in rule:
        target_idx = DAY_MAP.get(rule["day_of_week"].lower(), 0)
        due = monday_dt + timedelta(days=target_idx)
        if "time" in rule:
            try:
                h, m = map(int, rule["time"].split(":"))
                due = due.replace(hour=h, minute=m, second=0, microsecond=0)
            except Exception:
                pass
        return due.astimezone(timezone.utc)
    if "offset_days" in rule:
        return (monday_dt + timedelta(days=rule["offset_days"])).astimezone(timezone.utc)
    return None


def _get_week_monday(now_almaty: datetime) -> datetime:
    """Return Monday 00:00 of the current ISO week in Almaty time."""
    year, week, _ = now_almaty.isocalendar()
    jan4 = datetime(year, 1, 4, tzinfo=TZ)
    iwd = jan4.isoweekday()
    return jan4 - timedelta(days=iwd - 1) + timedelta(weeks=week - 1)


def generate_tasks_for_week(db, week_ref: str, monday_dt: datetime) -> int:
    """
    Core generation logic used by both the scheduler and the startup check.
    Returns number of newly created instances.
    """
    from src.routes.curator_tasks import seed_default_templates

    # Auto-seed if no templates exist
    if db.query(CuratorTaskTemplate).count() == 0:
        seed_default_templates(db)

    templates = (
        db.query(CuratorTaskTemplate)
        .filter(CuratorTaskTemplate.is_active == True)
        .order_by(CuratorTaskTemplate.order_index)
        .all()
    )
    if not templates:
        return 0

    groups = db.query(Group).filter(
        Group.curator_id.isnot(None),
        Group.is_active == True,
    ).all()

    created_count = 0

    for group in groups:
        curator_id = group.curator_id
        monday_date = monday_dt.date() if isinstance(monday_dt, datetime) else monday_dt
        prog_week = _calc_program_week(group, reference_date=monday_date)
        total_weeks = _calc_total_weeks(group)
        has_start = _group_has_start_date(group)

        # Skip groups that haven't started yet
        if has_start and prog_week is None:
            logger.info(f"[SCHEDULER] Skipping group {group.name} (id={group.id}): not started yet for {week_ref}")
            continue

        # Skip groups that have already finished
        if prog_week is not None and total_weeks is not None and prog_week > total_weeks:
            logger.info(f"[SCHEDULER] Skipping group {group.name} (id={group.id}): already finished (week {prog_week}/{total_weeks})")
            continue

        for tmpl in templates:
            if tmpl.task_type == "manual":
                continue
            # Filter by program week applicability
            if prog_week is not None:
                if tmpl.applicable_from_week and prog_week < tmpl.applicable_from_week:
                    continue
                if tmpl.applicable_to_week and prog_week > tmpl.applicable_to_week:
                    continue
            else:
                # No schedule_config — skip week-restricted templates
                if tmpl.applicable_from_week or tmpl.applicable_to_week:
                    continue

            due_date = _due_from_rule(tmpl.deadline_rule or {}, monday_dt)

            if tmpl.scope == "student":
                students = (
                    db.query(UserInDB)
                    .join(GroupStudent)
                    .filter(GroupStudent.group_id == group.id, UserInDB.is_active == True)
                    .all()
                )
                for student in students:
                    exists = db.query(CuratorTaskInstance).filter(
                        CuratorTaskInstance.template_id == tmpl.id,
                        CuratorTaskInstance.student_id == student.id,
                        CuratorTaskInstance.week_reference == week_ref,
                    ).first()
                    if not exists:
                        db.add(CuratorTaskInstance(
                            template_id=tmpl.id,
                            curator_id=curator_id,
                            student_id=student.id,
                            group_id=group.id,
                            status="pending",
                            due_date=due_date,
                            week_reference=week_ref,
                            program_week=prog_week,
                        ))
                        created_count += 1

            elif tmpl.scope == "group":
                exists = db.query(CuratorTaskInstance).filter(
                    CuratorTaskInstance.template_id == tmpl.id,
                    CuratorTaskInstance.group_id == group.id,
                    CuratorTaskInstance.week_reference == week_ref,
                ).first()
                if not exists:
                    db.add(CuratorTaskInstance(
                        template_id=tmpl.id,
                        curator_id=curator_id,
                        group_id=group.id,
                        status="pending",
                        due_date=due_date,
                        week_reference=week_ref,
                        program_week=prog_week,
                    ))
                    created_count += 1

    if created_count > 0:
        db.commit()

    return created_count


class CuratorTaskScheduler:
    """
    Background scheduler to generate recurring curator tasks.

    - On startup: checks if current week has tasks; generates if missing.
    - Every hour on Mondays: generates tasks for the current week.
    """

    def __init__(self, check_interval: int = 3600):
        self.check_interval = check_interval
        self.running = False
        self.thread = None

    def start(self):
        if self.running:
            logger.warning("Curator task scheduler is already running")
            return
        self.running = True
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()
        logger.info("Curator task scheduler started")

    def stop(self):
        self.running = False
        if self.thread:
            self.thread.join(timeout=5)
        logger.info("Curator task scheduler stopped")

    def _run(self):
        logger.info(f"[SCHEDULER] Curator task scheduler started (interval: {self.check_interval}s)")

        # Startup check: generate current week's tasks if missing
        try:
            self._startup_check()
        except Exception as e:
            logger.error(f"[SCHEDULER] Startup check error: {e}", exc_info=True)

        while self.running:
            time.sleep(self.check_interval)
            if not self.running:
                break
            try:
                self._check_and_create_tasks()
            except Exception as e:
                logger.error(f"[SCHEDULER] Error in curator task check: {e}", exc_info=True)

    def _startup_check(self):
        """On startup, ensure current week's tasks exist for all groups."""
        db = SessionLocal()
        try:
            now_almaty = datetime.now(TZ)
            year, week, _ = now_almaty.isocalendar()
            week_ref = f"{year}-W{week:02d}"
            monday = _get_week_monday(now_almaty)

            # Check if any tasks exist for this week
            existing = db.query(CuratorTaskInstance).filter(
                CuratorTaskInstance.week_reference == week_ref
            ).count()

            if existing == 0:
                logger.info(f"[SCHEDULER] No tasks found for {week_ref}, generating...")
                created = generate_tasks_for_week(db, week_ref, monday)
                logger.info(f"[SCHEDULER] Startup: generated {created} tasks for {week_ref}")
            else:
                logger.info(f"[SCHEDULER] {existing} tasks already exist for {week_ref}, skipping startup generation")

            generate_exam_result_collection_tasks(db, reference_date=now_almaty.date())
        finally:
            db.close()

    def _check_and_create_tasks(self):
        """Generate exam-result tasks daily and weekly tasks on Mondays."""
        db = SessionLocal()
        try:
            now_almaty = datetime.now(TZ)

            generate_exam_result_collection_tasks(db, reference_date=now_almaty.date())

            # Only generate on Mondays
            if now_almaty.weekday() != 0:
                return

            year, week, _ = now_almaty.isocalendar()
            week_ref = f"{year}-W{week:02d}"
            monday = _get_week_monday(now_almaty)

            created = generate_tasks_for_week(db, week_ref, monday)
            if created > 0:
                logger.info(f"[SCHEDULER] Monday run: generated {created} tasks for {week_ref}")
        except Exception as e:
            logger.error(f"[SCHEDULER] Error: {e}", exc_info=True)
            db.rollback()
        finally:
            db.close()


_scheduler: Optional[CuratorTaskScheduler] = None


def get_scheduler() -> CuratorTaskScheduler:
    global _scheduler
    if _scheduler is None:
        _scheduler = CuratorTaskScheduler(check_interval=3600)
    return _scheduler


def start_curator_task_scheduler():
    get_scheduler().start()


def stop_curator_task_scheduler():
    get_scheduler().stop()
