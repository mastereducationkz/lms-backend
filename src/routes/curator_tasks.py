"""
Curator Tasks API routes.

Endpoints:
  - Curator: view own tasks, update status, upload results
  - Head Curator: view all curators' tasks, stats
  - Admin: CRUD task templates, manually create task instances
"""

from fastapi import APIRouter, Depends, HTTPException, Query, Body
from sqlalchemy.orm import Session, joinedload
from sqlalchemy import func, and_, or_, desc
from typing import List, Optional
from datetime import datetime, timezone
from pydantic import BaseModel

from src.config import get_db
from src.schemas.models import (
    UserInDB, Group, GroupStudent,
    CuratorTaskTemplate, CuratorTaskInstance,
    CuratorTaskTemplateSchema, CuratorTaskTemplateCreateSchema,
    CuratorTaskInstanceSchema, CuratorTaskInstanceUpdateSchema,
)
from src.routes.auth import get_current_user_dependency
from src.utils.permissions import require_role

router = APIRouter()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _instance_to_schema(inst: CuratorTaskInstance) -> dict:
    """Convert a CuratorTaskInstance ORM object to a dict matching CuratorTaskInstanceSchema."""
    return {
        "id": inst.id,
        "template_id": inst.template_id,
        "template_title": inst.template.title if inst.template else None,
        "template_description": inst.template.description if inst.template else None,
        "task_type": inst.template.task_type if inst.template else None,
        "scope": inst.template.scope if inst.template else None,
        "curator_id": inst.curator_id,
        "curator_name": inst.curator.name if inst.curator else None,
        "student_id": inst.student_id,
        "student_name": inst.student.name if inst.student else None,
        "group_id": inst.group_id,
        "group_name": inst.group.name if inst.group else None,
        "status": inst.status,
        "due_date": inst.due_date,
        "completed_at": inst.completed_at,
        "result_text": inst.result_text,
        "screenshot_url": inst.screenshot_url,
        "week_reference": inst.week_reference,
        "program_week": inst.program_week,
        "created_at": inst.created_at,
        "updated_at": inst.updated_at,
    }


def _calc_program_week(group: Group, reference_date=None) -> Optional[int]:
    """Calculate program week (1-based) from group.schedule_config start_date.
    
    Args:
        group: The group to calculate for.
        reference_date: The date to calculate relative to. Defaults to today.
    """
    from datetime import date, timedelta
    try:
        cfg = group.schedule_config or {}
        start_str = cfg.get("start_date")
        if not start_str:
            return None
        start = date.fromisoformat(start_str)
        ref = reference_date if reference_date is not None else date.today()
        delta = (ref - start).days
        if delta < 0:
            return None  # Group hasn't started yet
        return delta // 7 + 1
    except Exception:
        return None


def _calc_total_weeks(group: Group) -> Optional[int]:
    """Calculate total weeks from group.schedule_config."""
    try:
        cfg = group.schedule_config or {}
        lessons_count = cfg.get("lessons_count")
        items = cfg.get("schedule_items", [])
        lessons_per_week = max(len(items), 1)
        if lessons_count:
            import math
            return math.ceil(lessons_count / lessons_per_week)
        return cfg.get("weeks_count")
    except Exception:
        return None


# ============================================================================
# CURATOR ENDPOINTS — own tasks
# ============================================================================

@router.get("/my-tasks", summary="Get current curator's tasks")
async def get_my_tasks(
    status: Optional[str] = Query(None, description="Filter by status: pending, in_progress, completed, overdue"),
    task_type: Optional[str] = Query(None, description="Filter by type: onboarding, weekly, renewal"),
    student_id: Optional[int] = Query(None),
    group_id: Optional[int] = Query(None),
    week: Optional[str] = Query(None, description="ISO week reference, e.g. 2026-W08"),
    program_week: Optional[int] = Query(None, description="Program week number (relative to group start_date)"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    current_user: UserInDB = Depends(require_role(["curator", "head_curator", "admin"])),
    db: Session = Depends(get_db),
):
    """
    Return task instances assigned to the current curator.
    Head curators and admins also see their own tasks here.
    """
    q = (
        db.query(CuratorTaskInstance)
        .options(
            joinedload(CuratorTaskInstance.template),
            joinedload(CuratorTaskInstance.student),
            joinedload(CuratorTaskInstance.group),
            joinedload(CuratorTaskInstance.curator),
        )
        .filter(CuratorTaskInstance.curator_id == current_user.id)
    )

    if status:
        q = q.filter(CuratorTaskInstance.status == status)
    if task_type:
        q = q.join(CuratorTaskTemplate).filter(CuratorTaskTemplate.task_type == task_type)
    if student_id:
        q = q.filter(CuratorTaskInstance.student_id == student_id)
    if group_id:
        q = q.filter(CuratorTaskInstance.group_id == group_id)
    if program_week is not None:
        q = q.filter(CuratorTaskInstance.program_week == program_week)
    elif week:
        q = q.filter(CuratorTaskInstance.week_reference == week)

    total = q.count()
    instances = q.order_by(CuratorTaskInstance.due_date.asc().nullslast(), CuratorTaskInstance.id.desc()).offset(offset).limit(limit).all()

    return {
        "total": total,
        "tasks": [_instance_to_schema(i) for i in instances],
    }


@router.get("/my-tasks/summary", summary="Get counts per status for current curator")
async def get_my_tasks_summary(
    current_user: UserInDB = Depends(require_role(["curator", "head_curator", "admin"])),
    db: Session = Depends(get_db),
):
    """Quick stats: how many tasks pending / in_progress / completed / overdue."""
    rows = (
        db.query(CuratorTaskInstance.status, func.count(CuratorTaskInstance.id))
        .filter(CuratorTaskInstance.curator_id == current_user.id)
        .group_by(CuratorTaskInstance.status)
        .all()
    )
    counts = {r[0]: r[1] for r in rows}
    return {
        "pending": counts.get("pending", 0),
        "in_progress": counts.get("in_progress", 0),
        "completed": counts.get("completed", 0),
        "overdue": counts.get("overdue", 0),
        "total": sum(counts.values()),
    }


@router.get("/my-groups", summary="Get groups for current curator with program week info")
async def get_my_groups(
    current_user: UserInDB = Depends(require_role(["curator", "head_curator", "admin"])),
    db: Session = Depends(get_db),
):
    """Return curator's active groups with program week, total weeks, and start_date from schedule_config."""
    if current_user.role in ("admin", "head_curator"):
        groups = db.query(Group).filter(Group.curator_id.isnot(None), Group.is_active == True).all()
    else:
        groups = db.query(Group).filter(Group.curator_id == current_user.id, Group.is_active == True).all()

    result = []
    for g in groups:
        cfg = g.schedule_config or {}
        result.append({
            "id": g.id,
            "name": g.name,
            "start_date": cfg.get("start_date"),
            "lessons_count": cfg.get("lessons_count"),
            "program_week": _calc_program_week(g),
            "total_weeks": _calc_total_weeks(g),
            "has_schedule": bool(cfg.get("start_date")),
        })
    return result


class BulkTaskUpdateSchema(BaseModel):
    task_ids: List[int]
    status: str  # 'completed', 'in_progress', 'pending'


@router.patch("/my-tasks/bulk", summary="Bulk update task status (e.g. mark multiple as completed)")
async def bulk_update_my_tasks(
    data: BulkTaskUpdateSchema = Body(...),
    current_user: UserInDB = Depends(require_role(["curator", "head_curator", "admin"])),
    db: Session = Depends(get_db),
):
    """Update multiple tasks in one request. Only tasks assigned to current user."""
    if not data.task_ids:
        return {"updated": 0}
    allowed = {"completed", "in_progress", "pending"}
    if data.status not in allowed:
        raise HTTPException(status_code=400, detail=f"status must be one of {allowed}")
    instances = (
        db.query(CuratorTaskInstance)
        .filter(
            CuratorTaskInstance.id.in_(data.task_ids),
            CuratorTaskInstance.curator_id == current_user.id,
        )
        .all()
    )
    now = datetime.now(timezone.utc)
    for inst in instances:
        inst.status = data.status
        inst.completed_at = now if data.status == "completed" else None
    db.commit()
    return {"updated": len(instances)}


@router.patch("/my-tasks/{task_id}", summary="Update task status / result")
async def update_my_task(
    task_id: int,
    data: CuratorTaskInstanceUpdateSchema,
    current_user: UserInDB = Depends(require_role(["curator", "head_curator", "admin"])),
    db: Session = Depends(get_db),
):
    """
    Curator marks a task as in_progress / completed, attaches result_text or screenshot_url.
    """
    inst = (
        db.query(CuratorTaskInstance)
        .options(
            joinedload(CuratorTaskInstance.template),
            joinedload(CuratorTaskInstance.student),
            joinedload(CuratorTaskInstance.group),
            joinedload(CuratorTaskInstance.curator),
        )
        .filter(
            CuratorTaskInstance.id == task_id,
            CuratorTaskInstance.curator_id == current_user.id,
        )
        .first()
    )
    if not inst:
        raise HTTPException(status_code=404, detail="Task not found or not assigned to you")

    if data.status:
        allowed_transitions = {
            "pending": ["in_progress", "completed"],
            "in_progress": ["completed", "pending"],
            "overdue": ["in_progress", "completed"],
            "completed": ["pending", "in_progress"],
        }
        if data.status not in allowed_transitions.get(inst.status, []):
            raise HTTPException(
                status_code=400,
                detail=f"Cannot transition from '{inst.status}' to '{data.status}'",
            )
        inst.status = data.status
        if data.status == "completed":
            inst.completed_at = datetime.now(timezone.utc)
        else:
            inst.completed_at = None

    if data.result_text is not None:
        inst.result_text = data.result_text
    if data.screenshot_url is not None:
        inst.screenshot_url = data.screenshot_url

    db.commit()
    db.refresh(inst)
    return _instance_to_schema(inst)


# ============================================================================
# HEAD CURATOR ENDPOINTS — monitor all curators
# ============================================================================

@router.get("/all-tasks", summary="[Head Curator / Admin] View all curator tasks")
async def get_all_tasks(
    curator_id: Optional[int] = Query(None, description="Filter by specific curator"),
    status: Optional[str] = Query(None),
    task_type: Optional[str] = Query(None),
    group_id: Optional[int] = Query(None),
    week: Optional[str] = Query(None, description="ISO week reference, e.g. 2026-W08"),
    program_week: Optional[int] = Query(None, description="Program week number (relative to group start_date)"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    current_user: UserInDB = Depends(require_role(["head_curator", "admin"])),
    db: Session = Depends(get_db),
):
    """Head curator sees tasks across ALL curators."""
    q = (
        db.query(CuratorTaskInstance)
        .options(
            joinedload(CuratorTaskInstance.template),
            joinedload(CuratorTaskInstance.student),
            joinedload(CuratorTaskInstance.group),
            joinedload(CuratorTaskInstance.curator),
        )
    )

    if curator_id:
        q = q.filter(CuratorTaskInstance.curator_id == curator_id)
    if status:
        q = q.filter(CuratorTaskInstance.status == status)
    if task_type:
        q = q.join(CuratorTaskTemplate).filter(CuratorTaskTemplate.task_type == task_type)
    if group_id:
        q = q.filter(CuratorTaskInstance.group_id == group_id)
    if program_week is not None:
        q = q.filter(CuratorTaskInstance.program_week == program_week)
    elif week:
        q = q.filter(CuratorTaskInstance.week_reference == week)

    total = q.count()
    instances = q.order_by(CuratorTaskInstance.due_date.asc().nullslast(), CuratorTaskInstance.id.desc()).offset(offset).limit(limit).all()

    return {
        "total": total,
        "tasks": [_instance_to_schema(i) for i in instances],
    }


@router.get("/curators-summary", summary="[Head Curator / Admin] Stats per curator")
async def get_curators_summary(
    week: Optional[str] = Query(None),
    current_user: UserInDB = Depends(require_role(["head_curator", "admin"])),
    db: Session = Depends(get_db),
):
    """
    Returns a summary of task completion per curator:
      { curator_id, curator_name, pending, in_progress, completed, overdue, total }
    """
    q = (
        db.query(
            CuratorTaskInstance.curator_id,
            CuratorTaskInstance.status,
            func.count(CuratorTaskInstance.id),
        )
        .group_by(CuratorTaskInstance.curator_id, CuratorTaskInstance.status)
    )
    if week:
        q = q.filter(CuratorTaskInstance.week_reference == week)

    rows = q.all()

    # Aggregate
    curators_map: dict = {}
    for curator_id, status, cnt in rows:
        if curator_id not in curators_map:
            curators_map[curator_id] = {
                "curator_id": curator_id,
                "curator_name": None,
                "pending": 0,
                "in_progress": 0,
                "completed": 0,
                "overdue": 0,
                "total": 0,
            }
        curators_map[curator_id][status] = cnt
        curators_map[curator_id]["total"] += cnt

    # Fill names
    if curators_map:
        users = db.query(UserInDB).filter(UserInDB.id.in_(curators_map.keys())).all()
        name_map = {u.id: u.name for u in users}
        for cid in curators_map:
            curators_map[cid]["curator_name"] = name_map.get(cid, "Unknown")

    return list(curators_map.values())


# ============================================================================
# ADMIN / TEMPLATE ENDPOINTS
# ============================================================================

@router.get("/templates", summary="List task templates")
async def list_templates(
    task_type: Optional[str] = Query(None),
    current_user: UserInDB = Depends(require_role(["admin", "head_curator"])),
    db: Session = Depends(get_db),
):
    q = db.query(CuratorTaskTemplate)
    if task_type:
        q = q.filter(CuratorTaskTemplate.task_type == task_type)
    templates = q.order_by(CuratorTaskTemplate.task_type, CuratorTaskTemplate.order_index).all()
    return [CuratorTaskTemplateSchema.model_validate(t) for t in templates]


@router.post("/templates", summary="Create a task template")
async def create_template(
    data: CuratorTaskTemplateCreateSchema,
    current_user: UserInDB = Depends(require_role(["admin"])),
    db: Session = Depends(get_db),
):
    template = CuratorTaskTemplate(
        title=data.title,
        description=data.description,
        task_type=data.task_type,
        scope=data.scope,
        recurrence_rule=data.recurrence_rule,
        deadline_rule=data.deadline_rule,
        order_index=data.order_index,
    )
    db.add(template)
    db.commit()
    db.refresh(template)
    return CuratorTaskTemplateSchema.model_validate(template)


@router.put("/templates/{template_id}", summary="Update a task template")
async def update_template(
    template_id: int,
    data: CuratorTaskTemplateCreateSchema,
    current_user: UserInDB = Depends(require_role(["admin"])),
    db: Session = Depends(get_db),
):
    template = db.query(CuratorTaskTemplate).filter(CuratorTaskTemplate.id == template_id).first()
    if not template:
        raise HTTPException(status_code=404, detail="Template not found")

    template.title = data.title
    template.description = data.description
    template.task_type = data.task_type
    template.scope = data.scope
    template.recurrence_rule = data.recurrence_rule
    template.deadline_rule = data.deadline_rule
    template.order_index = data.order_index
    db.commit()
    db.refresh(template)
    return CuratorTaskTemplateSchema.model_validate(template)


@router.delete("/templates/{template_id}", summary="Delete a task template")
async def delete_template(
    template_id: int,
    current_user: UserInDB = Depends(require_role(["admin"])),
    db: Session = Depends(get_db),
):
    template = db.query(CuratorTaskTemplate).filter(CuratorTaskTemplate.id == template_id).first()
    if not template:
        raise HTTPException(status_code=404, detail="Template not found")
    db.delete(template)
    db.commit()
    return {"detail": "Template deleted"}


# ============================================================================
# MANUAL TASK CREATION (admin / head curator)
# ============================================================================

@router.post("/create-instance", summary="Manually create a task instance for a curator")
async def create_task_instance(
    template_id: int = Query(...),
    curator_id: int = Query(...),
    student_id: Optional[int] = Query(None),
    group_id: Optional[int] = Query(None),
    due_date: Optional[datetime] = Query(None),
    week: Optional[str] = Query(None, description="ISO week reference, e.g. 2025-W09"),
    program_week: Optional[int] = Query(None, description="Program week number"),
    current_user: UserInDB = Depends(require_role(["admin", "head_curator"])),
    db: Session = Depends(get_db),
):
    """Create a single task instance manually (e.g. for onboarding a new student)."""
    template = db.query(CuratorTaskTemplate).filter(CuratorTaskTemplate.id == template_id).first()
    if not template:
        raise HTTPException(status_code=404, detail="Template not found")

    curator = db.query(UserInDB).filter(UserInDB.id == curator_id, UserInDB.role.in_(["curator", "head_curator"])).first()
    if not curator:
        raise HTTPException(status_code=404, detail="Curator not found")

    inst = CuratorTaskInstance(
        template_id=template_id,
        curator_id=curator_id,
        student_id=student_id,
        group_id=group_id,
        due_date=due_date,
        status="pending",
    )
    db.add(inst)
    db.commit()
    db.refresh(inst)

    # Reload with relationships
    inst = (
        db.query(CuratorTaskInstance)
        .options(
            joinedload(CuratorTaskInstance.template),
            joinedload(CuratorTaskInstance.student),
            joinedload(CuratorTaskInstance.group),
            joinedload(CuratorTaskInstance.curator),
        )
        .filter(CuratorTaskInstance.id == inst.id)
        .first()
    )
    return _instance_to_schema(inst)


# ============================================================================
# ONBOARDING TRIGGER — create all onboarding tasks for new student
# ============================================================================

@router.post("/trigger-onboarding/{student_id}", summary="Create onboarding tasks for a new student")
async def trigger_onboarding(
    student_id: int,
    current_user: UserInDB = Depends(require_role(["admin", "head_curator"])),
    db: Session = Depends(get_db),
):
    """
    When a student is purchased/enrolled, this endpoint creates all onboarding
    task instances for the curator attached to the student's group.
    """
    # Find student's groups
    group_students = db.query(GroupStudent).filter(GroupStudent.student_id == student_id).all()
    if not group_students:
        raise HTTPException(status_code=404, detail="Student not found in any group")

    # Get onboarding templates
    templates = (
        db.query(CuratorTaskTemplate)
        .filter(CuratorTaskTemplate.task_type == "onboarding", CuratorTaskTemplate.is_active == True)
        .order_by(CuratorTaskTemplate.order_index)
        .all()
    )
    if not templates:
        raise HTTPException(status_code=404, detail="No active onboarding templates found")

    created = []
    now = datetime.now(timezone.utc)

    for gs in group_students:
        group = db.query(Group).filter(Group.id == gs.group_id).first()
        if not group or not group.curator_id:
            continue

        for tmpl in templates:
            # Calculate due date from deadline_rule
            due = None
            if tmpl.deadline_rule and "offset_days" in tmpl.deadline_rule:
                from datetime import timedelta
                due = now + timedelta(days=tmpl.deadline_rule["offset_days"])

            inst = CuratorTaskInstance(
                template_id=tmpl.id,
                curator_id=group.curator_id,
                student_id=student_id,
                group_id=group.id,
                due_date=due,
                status="pending",
            )
            db.add(inst)
            created.append(inst)

    db.commit()
    return {"detail": f"Created {len(created)} onboarding tasks"}


# ============================================================================
# SEED DEFAULT TEMPLATES
# ============================================================================

@router.post("/seed-templates", summary="[Admin] Seed default task templates from specification")
async def seed_templates(
    current_user: UserInDB = Depends(require_role(["admin"])),
    db: Session = Depends(get_db),
):
    """
    Populate the database with the standard curator task templates.
    Safe to call multiple times — skips templates that already exist (by title).
    """
    default_templates = [
        # --- ONBOARDING ---
        {
            "title": "Знакомство с учеником",
            "description": "Отправить приветственное сообщение в Telegram-группу и в чат ученику согласно шаблону из хендбука. Представиться, объяснить роль куратора, описать ожидания. Добавить ученика во все беседы. Прикрепить скриншот в результат.",
            "task_type": "onboarding",
            "scope": "student",
            "deadline_rule": {"offset_days": 1},
            "order_index": 1,
        },
        {
            "title": "Знакомство с родителем (сообщение)",
            "description": "Отправить приветственное сообщение родителю в WhatsApp согласно шаблону хендбука. Представиться, объяснить роль куратора, сообщить рабочее время. Для казахоязычных родителей — использовать казахский шаблон. Прикрепить скриншот в результат.",
            "task_type": "onboarding",
            "scope": "student",
            "deadline_rule": {"offset_days": 1},
            "order_index": 2,
        },
        {
            "title": "Отправить видео для родителей",
            "description": "Отправить ознакомительное видео (https://youtu.be/XTpC98eRZWk) отдельным сообщением. Попросить обязательно посмотреть. Прикрепить скриншот в результат.",
            "task_type": "onboarding",
            "scope": "student",
            "deadline_rule": {"offset_days": 1},
            "order_index": 3,
        },
        {
            "title": "Отправить сообщение с гарантией",
            "description": "Отправить сообщение с гарантиями из договора (Раздел 4). SAT: +150/+200 баллов. IELTS: +1.0/+1.5. Для казахоязычных — казахский шаблон. Прикрепить скриншот в результат.",
            "task_type": "onboarding",
            "scope": "student",
            "deadline_rule": {"offset_days": 1},
            "order_index": 4,
        },
        # --- WEEKLY ---
        {
            "title": "Обратная связь родителям (ОС)",
            "description": "Отправить еженедельный отчёт родителю в WhatsApp: успеваемость, посещаемость, выполнение ДЗ, рекомендации. С 3-й недели — сравнение Initial Score vs текущий балл. Прикрепить скриншот.",
            "task_type": "weekly",
            "scope": "student",
            "recurrence_rule": {"day_of_week": "monday", "time": "09:00", "timezone": "Asia/Almaty"},
            "deadline_rule": {"day_of_week": "tuesday", "time": "21:00", "timezone": "Asia/Almaty"},
            "order_index": 1,
        },
        {
            "title": "Напоминание о weekly practice",
            "description": "Напомнить ученику о необходимости сдать weekly practice. Дедлайн — воскресенье 23:59. Если не сдал — написать повторно, позвонить, сообщить родителю.",
            "task_type": "weekly",
            "scope": "student",
            "recurrence_rule": {"day_of_week": "sunday", "time": "09:00", "timezone": "Asia/Almaty"},
            "deadline_rule": {"day_of_week": "sunday", "time": "21:00", "timezone": "Asia/Almaty"},
            "order_index": 2,
        },
        {
            "title": "Напоминание о вебинаре / уроке",
            "description": "Напомнить ученику о предстоящем уроке. Уточнить, придёт ли. Если не придёт — узнать причину, зафиксировать. Скрин переписки — в результат.",
            "task_type": "weekly",
            "scope": "student",
            "recurrence_rule": {"day_of_week": "monday", "time": "09:00", "timezone": "Asia/Almaty"},
            "deadline_rule": {"offset_hours_before_lesson": 1},
            "order_index": 3,
        },
        {
            "title": "Заполнение лидерборда",
            "description": "Заполнить свою вкладку в Leaderboard (current) 3 раза в неделю. Фиксировать активность, выполнение заданий, успехи. Прикрепить скриншот.",
            "task_type": "weekly",
            "scope": "group",
            "recurrence_rule": {"day_of_week": "monday", "time": "09:00", "timezone": "Asia/Almaty"},
            "deadline_rule": {"day_of_week": "sunday", "time": "21:00", "timezone": "Asia/Almaty"},
            "order_index": 4,
        },
        {
            "title": "Проведение кураторского часа",
            "description": "Провести онлайн-встречу с группой по силлабусу. Разобрать сложные темы. Запись отправить в группу + на Google Диск. Скрин из чата + ссылка на запись — в результат.",
            "task_type": "weekly",
            "scope": "group",
            "recurrence_rule": {"day_of_week": "monday", "time": "09:00", "timezone": "Asia/Almaty"},
            "deadline_rule": {"day_of_week": "sunday", "time": "21:00", "timezone": "Asia/Almaty"},
            "order_index": 5,
        },
        {
            "title": "Пост в беседу 1",
            "description": "Отправить мотивационный контент в Telegram-группу: daily challenge, мотивационная цитата, полезный материал или напоминание. Прикрепить скриншот поста.",
            "task_type": "weekly",
            "scope": "group",
            "recurrence_rule": {"day_of_week": "monday", "time": "09:00", "timezone": "Asia/Almaty"},
            "deadline_rule": {"day_of_week": "monday", "time": "21:00", "timezone": "Asia/Almaty"},
            "order_index": 6,
        },
        {
            "title": "Пост в беседу 2",
            "description": "Мотивационный контент: мем, челлендж, интересный факт или совет по подготовке. Прикрепить скриншот поста.",
            "task_type": "weekly",
            "scope": "group",
            "recurrence_rule": {"day_of_week": "monday", "time": "09:00", "timezone": "Asia/Almaty"},
            "deadline_rule": {"day_of_week": "wednesday", "time": "21:00", "timezone": "Asia/Almaty"},
            "order_index": 7,
        },
        {
            "title": "Пост в беседу 3",
            "description": "Итоги недели, похвала активных учеников, напоминание о weekly practice (дедлайн — воскресенье 23:59). Прикрепить скриншот поста.",
            "task_type": "weekly",
            "scope": "group",
            "recurrence_rule": {"day_of_week": "monday", "time": "09:00", "timezone": "Asia/Almaty"},
            "deadline_rule": {"day_of_week": "friday", "time": "21:00", "timezone": "Asia/Almaty"},
            "order_index": 8,
        },
        {
            "title": "Пост в беседу 4",
            "description": "Мотивация на новую неделю, напоминание о дедлайне weekly practice (сегодня 23:59), поддержка и позитив. Прикрепить скриншот поста.",
            "task_type": "weekly",
            "scope": "group",
            "recurrence_rule": {"day_of_week": "monday", "time": "09:00", "timezone": "Asia/Almaty"},
            "deadline_rule": {"day_of_week": "sunday", "time": "21:00", "timezone": "Asia/Almaty"},
            "order_index": 9,
        },
        {
            "title": "Обратная связь ученику в ЛС 1",
            "description": "Персональное сообщение каждому ученику. Отметить прогресс, позицию в лидерборде, успехи или зоны роста. Если есть пропуски — мягко напомнить. Прикрепить скриншоты переписок.",
            "task_type": "weekly",
            "scope": "student",
            "recurrence_rule": {"day_of_week": "monday", "time": "09:00", "timezone": "Asia/Almaty"},
            "deadline_rule": {"day_of_week": "thursday", "time": "14:00", "timezone": "Asia/Almaty"},
            "order_index": 10,
        },
        {
            "title": "Обратная связь ученику в ЛС 2",
            "description": "Персональное сообщение: мотивация на завершение недели, напоминание о weekly practice, поддержка. Если ученик неактивен — уточнить причину. Прикрепить скриншоты переписок.",
            "task_type": "weekly",
            "scope": "student",
            "recurrence_rule": {"day_of_week": "monday", "time": "09:00", "timezone": "Asia/Almaty"},
            "deadline_rule": {"day_of_week": "sunday", "time": "20:00", "timezone": "Asia/Almaty"},
            "order_index": 11,
        },
        # --- RENEWAL ---
        {
            "title": "Предпродление",
            "description": "Написать или позвонить родителю: промежуточные итоги, конкретные достижения, дорожная карта на следующий месяц (2 цели + mock exam). Напомнить о гарантии. Использовать скрипт предпродления. Прикрепить скриншоты.",
            "task_type": "renewal",
            "scope": "student",
            "deadline_rule": {"offset_days": 1},
            "order_index": 1,
        },
        {
            "title": "Передача статуса о продлении",
            "description": "Заполнить карточку ученика (ФИО, группа, дата окончания, Initial → текущий балл, посещаемость, статус родителя 🟢/🟡/🔴, возражения, рекомендация). Зафиксировать в ЛМС статус родителя.",
            "task_type": "renewal",
            "scope": "student",
            "deadline_rule": {"offset_days": 1},
            "order_index": 2,
        },
        # --- MANUAL (head curator custom tasks) ---
        {
            "title": "Свой",
            "description": "Свой шаблон — введите текст задачи вручную",
            "task_type": "manual",
            "scope": "group",
            "order_index": 999,
        },
    ]

    created_count = 0
    for tmpl_data in default_templates:
        existing = db.query(CuratorTaskTemplate).filter(CuratorTaskTemplate.title == tmpl_data["title"]).first()
        if existing:
            continue
        template = CuratorTaskTemplate(**tmpl_data)
        db.add(template)
        created_count += 1

    db.commit()
    return {"detail": f"Seeded {created_count} new templates ({len(default_templates) - created_count} already existed)"}


# ============================================================================
# GENERATE WEEKLY TASKS ON DEMAND
# ============================================================================

@router.post("/generate-weekly", summary="Generate weekly tasks for current curator's groups for a given week")
async def generate_weekly_tasks(
    week: Optional[str] = Query(None, description="ISO week reference e.g. 2026-W08. Defaults to current week."),
    group_id: Optional[int] = Query(None, description="Generate for a specific group only"),
    current_user: UserInDB = Depends(require_role(["curator", "head_curator", "admin"])),
    db: Session = Depends(get_db),
):
    """
    Schedule-aware task generation:
    - Calculates program_week for each group from schedule_config.start_date
    - Filters templates by applicable_from_week / applicable_to_week
    - Generates tasks for all active task_types (weekly, onboarding, renewal) as appropriate
    - Auto-seeds templates if none exist
    """
    import pytz
    from datetime import timedelta, date as date_type

    tz = pytz.timezone("Asia/Almaty")
    now_almaty = datetime.now(tz)

    if not week:
        year, wk, _ = now_almaty.isocalendar()
        week = f"{year}-W{wk:02d}"

    # Auto-seed templates if none exist
    tmpl_count = db.query(CuratorTaskTemplate).count()
    if tmpl_count == 0:
        seed_default_templates(db)

    templates = (
        db.query(CuratorTaskTemplate)
        .filter(CuratorTaskTemplate.is_active == True)
        .order_by(CuratorTaskTemplate.order_index)
        .all()
    )

    if not templates:
        raise HTTPException(status_code=404, detail="No templates found. Seed them first.")

    # Determine groups for the curator
    if current_user.role in ("admin", "head_curator"):
        q = db.query(Group).filter(Group.curator_id.isnot(None), Group.is_active == True)
    else:
        q = db.query(Group).filter(Group.curator_id == current_user.id, Group.is_active == True)
    if group_id:
        q = q.filter(Group.id == group_id)
    groups = q.all()

    if not groups:
        return {"detail": "No active groups found for this curator", "created": 0}

    day_map = {
        "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
        "friday": 4, "saturday": 5, "sunday": 6
    }

    # Parse ISO week → Monday datetime in Almaty timezone
    year_str, week_part = week.split('-W')
    year_val = int(year_str)
    week_val = int(week_part)
    jan4 = datetime(year_val, 1, 4, tzinfo=tz)
    day_of_week = jan4.isoweekday()
    monday = jan4 - timedelta(days=day_of_week - 1) + timedelta(weeks=week_val - 1)

    def _due_from_rule(rule: dict, monday_dt: datetime) -> Optional[datetime]:
        if not rule:
            return None
        if "day_of_week" in rule:
            target_idx = day_map.get(rule["day_of_week"].lower(), 0)
            due = monday_dt + timedelta(days=target_idx)
            if "time" in rule:
                try:
                    h, m = map(int, rule["time"].split(":"))
                    due = due.replace(hour=h, minute=m, second=0, microsecond=0)
                except Exception:
                    pass
            return due.astimezone(timezone.utc)
        if "offset_days" in rule:
            due = monday_dt + timedelta(days=rule["offset_days"])
            return due.astimezone(timezone.utc)
        return None

    created_count = 0
    skipped_exam_templates = 0
    monday_date = monday.date() if hasattr(monday, 'date') else monday

    for group in groups:
        curator_id = group.curator_id
        prog_week = _calc_program_week(group, reference_date=monday_date)

        # Clean up stale pending tasks whose templates no longer apply for this week's program_week
        for tmpl in templates:
            if tmpl.task_type == "exam_results_collection":
                skipped_exam_templates += 1
                continue
            should_skip = False
            if prog_week is not None:
                if tmpl.applicable_from_week and prog_week < tmpl.applicable_from_week:
                    should_skip = True
                if tmpl.applicable_to_week and prog_week > tmpl.applicable_to_week:
                    should_skip = True
            else:
                if tmpl.applicable_from_week or tmpl.applicable_to_week:
                    should_skip = True
            if should_skip:
                db.query(CuratorTaskInstance).filter(
                    CuratorTaskInstance.template_id == tmpl.id,
                    CuratorTaskInstance.group_id == group.id,
                    CuratorTaskInstance.week_reference == week,
                    CuratorTaskInstance.status == "pending",
                ).delete(synchronize_session=False)

        for tmpl in templates:
            if tmpl.task_type == "exam_results_collection":
                skipped_exam_templates += 1
                continue
            # Skip if template not applicable for this program week
            if prog_week is not None:
                if tmpl.applicable_from_week and prog_week < tmpl.applicable_from_week:
                    continue
                if tmpl.applicable_to_week and prog_week > tmpl.applicable_to_week:
                    continue
            else:
                # No schedule_config: only generate templates with no week restrictions
                if tmpl.applicable_from_week or tmpl.applicable_to_week:
                    continue

            due_date = _due_from_rule(tmpl.deadline_rule or {}, monday)

            if tmpl.scope == "student":
                students = db.query(UserInDB).join(GroupStudent).filter(
                    GroupStudent.group_id == group.id,
                    UserInDB.is_active == True
                ).all()

                for student in students:
                    exists = db.query(CuratorTaskInstance).filter(
                        CuratorTaskInstance.template_id == tmpl.id,
                        CuratorTaskInstance.student_id == student.id,
                        CuratorTaskInstance.week_reference == week
                    ).first()
                    if not exists:
                        db.add(CuratorTaskInstance(
                            template_id=tmpl.id,
                            curator_id=curator_id,
                            student_id=student.id,
                            group_id=group.id,
                            status="pending",
                            due_date=due_date,
                            week_reference=week,
                            program_week=prog_week,
                        ))
                        created_count += 1

            elif tmpl.scope == "group":
                exists = db.query(CuratorTaskInstance).filter(
                    CuratorTaskInstance.template_id == tmpl.id,
                    CuratorTaskInstance.group_id == group.id,
                    CuratorTaskInstance.week_reference == week
                ).first()
                if not exists:
                    db.add(CuratorTaskInstance(
                        template_id=tmpl.id,
                        curator_id=curator_id,
                        group_id=group.id,
                        status="pending",
                        due_date=due_date,
                        week_reference=week,
                        program_week=prog_week,
                    ))
                    created_count += 1

    db.commit()

    logger.info(
        f"[GENERATE_WEEKLY] week={week} group_id={group_id} groups={len(groups)} created={created_count} skipped_exam_templates={skipped_exam_templates}"
    )

    phase_label = ""
    if groups:
        pw = _calc_program_week(groups[0], reference_date=monday_date)
        tw = _calc_total_weeks(groups[0])
        if pw and tw:
            phase_label = f" (Неделя {pw} из {tw})"
        elif pw:
            phase_label = f" (Неделя {pw})"

    return {
        "detail": f"Сгенерировано {created_count} задач на неделю {week}{phase_label}",
        "created": created_count,
        "week": week,
    }


def seed_default_templates(db: Session):
    """Utility to seed templates without auth (called internally)."""
    default_templates = [
        # Weekly tasks (apply every week: applicable_from_week=None, applicable_to_week=None)
        {"title": "Обратная связь родителям (ОС)", "task_type": "weekly", "scope": "student",
         "recurrence_rule": {"day_of_week": "monday", "time": "09:00"},
         "deadline_rule": {"day_of_week": "tuesday", "time": "21:00"}, "order_index": 1,
         "applicable_from_week": None, "applicable_to_week": None},
        {"title": "Напоминание о weekly practice", "task_type": "weekly", "scope": "student",
         "recurrence_rule": {"day_of_week": "sunday", "time": "09:00"},
         "deadline_rule": {"day_of_week": "sunday", "time": "21:00"}, "order_index": 2,
         "applicable_from_week": None, "applicable_to_week": None},
        {"title": "Напоминание о вебинаре / уроке", "task_type": "weekly", "scope": "student",
         "recurrence_rule": {"day_of_week": "monday", "time": "09:00"},
         "deadline_rule": {"day_of_week": "friday", "time": "18:00"}, "order_index": 3,
         "applicable_from_week": None, "applicable_to_week": None},
        {"title": "Заполнение лидерборда", "task_type": "weekly", "scope": "group",
         "recurrence_rule": {"day_of_week": "monday", "time": "09:00"},
         "deadline_rule": {"day_of_week": "sunday", "time": "21:00"}, "order_index": 4,
         "applicable_from_week": None, "applicable_to_week": None},
        {"title": "Проведение кураторского часа", "task_type": "weekly", "scope": "group",
         "recurrence_rule": {"day_of_week": "monday", "time": "09:00"},
         "deadline_rule": {"day_of_week": "sunday", "time": "21:00"}, "order_index": 5,
         "applicable_from_week": None, "applicable_to_week": None},
        {"title": "Пост в беседу 1", "task_type": "weekly", "scope": "group",
         "deadline_rule": {"day_of_week": "monday", "time": "21:00"}, "order_index": 6,
         "applicable_from_week": None, "applicable_to_week": None},
        {"title": "Пост в беседу 2", "task_type": "weekly", "scope": "group",
         "deadline_rule": {"day_of_week": "wednesday", "time": "21:00"}, "order_index": 7,
         "applicable_from_week": None, "applicable_to_week": None},
        {"title": "Пост в беседу 3", "task_type": "weekly", "scope": "group",
         "deadline_rule": {"day_of_week": "friday", "time": "21:00"}, "order_index": 8,
         "applicable_from_week": None, "applicable_to_week": None},
        {"title": "Пост в беседу 4", "task_type": "weekly", "scope": "group",
         "deadline_rule": {"day_of_week": "sunday", "time": "21:00"}, "order_index": 9,
         "applicable_from_week": None, "applicable_to_week": None},
        {"title": "Обратная связь ученику в ЛС 1", "task_type": "weekly", "scope": "student",
         "deadline_rule": {"day_of_week": "thursday", "time": "14:00"}, "order_index": 10,
         "applicable_from_week": None, "applicable_to_week": None},
        {"title": "Обратная связь ученику в ЛС 2", "task_type": "weekly", "scope": "student",
         "deadline_rule": {"day_of_week": "sunday", "time": "20:00"}, "order_index": 11,
         "applicable_from_week": None, "applicable_to_week": None},
        # Onboarding: only week 1
        {"title": "Знакомство с учеником", "task_type": "onboarding", "scope": "student",
         "deadline_rule": {"offset_days": 1}, "order_index": 1,
         "applicable_from_week": 1, "applicable_to_week": 1},
        {"title": "Знакомство с родителем (сообщение)", "task_type": "onboarding", "scope": "student",
         "deadline_rule": {"offset_days": 1}, "order_index": 2,
         "applicable_from_week": 1, "applicable_to_week": 1},
        # Renewal: from week 10 onwards
        {"title": "Предпродление", "task_type": "renewal", "scope": "student",
         "deadline_rule": {"offset_days": 3}, "order_index": 1,
         "applicable_from_week": 10, "applicable_to_week": None},
        {"title": "Передача статуса о продлении", "task_type": "renewal", "scope": "student",
         "deadline_rule": {"offset_days": 5}, "order_index": 2,
         "applicable_from_week": 10, "applicable_to_week": None},
        # Manual: head curator creates custom tasks (scheduler skips this)
        {"title": "Свой", "task_type": "manual", "scope": "group",
         "order_index": 999, "applicable_from_week": None, "applicable_to_week": None},
    ]
    for tmpl_data in default_templates:
        existing = db.query(CuratorTaskTemplate).filter(CuratorTaskTemplate.title == tmpl_data["title"]).first()
        if not existing:
            db.add(CuratorTaskTemplate(**tmpl_data))
        else:
            # Update applicable_from/to_week for existing templates that don't have them set
            if existing.applicable_from_week is None and tmpl_data.get("applicable_from_week") is not None:
                existing.applicable_from_week = tmpl_data["applicable_from_week"]
            if existing.applicable_to_week is None and tmpl_data.get("applicable_to_week") is not None:
                existing.applicable_to_week = tmpl_data["applicable_to_week"]
    db.commit()
