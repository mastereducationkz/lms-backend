"""Родительские отчёты: HTTP-слой.

Доступ тот же, что у отчёта по ученику (``src/reports/routes._require_report_access``):
админ / head_curator / head_teacher — любой ученик, куратор — только свои группы.
"""
from datetime import date
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from src.config import get_db
from src.reports.parent.facts import build_week_facts
from src.reports.parent.models import ParentReport
from src.reports.parent.prose import generate_prose
from src.reports.parent.template import pick_template, render
from src.reports.parent.week import week_bounds
from src.reports.routes import _require_report_access
from src.routes.auth import get_current_user_dependency
from src.schemas.models import Group, GroupStudent, UserInDB

router = APIRouter()

_FULL_ACCESS_ROLES = {"admin", "head_curator", "head_teacher"}


class GenerateBody(BaseModel):
    week: date
    template: Optional[str] = None
    note: Optional[str] = None


class SaveBody(BaseModel):
    week: date
    body: str
    # Заметка приходит вместе с текстом: она стоит в той же карточке, под той же кнопкой,
    # и куратор вправе ожидать, что «Сохранить» сохраняет обе. Клиент присылает её всегда —
    # пустую как null, — поэтому присваивание безусловное.
    note: Optional[str] = None


def _require_group_access(group_id: int, user: UserInDB, db: Session) -> Group:
    group = db.query(Group).filter(Group.id == group_id).first()
    if not group:
        raise HTTPException(status_code=404, detail="Group not found")
    if user.role in _FULL_ACCESS_ROLES:
        return group
    if user.role == "curator" and group.curator_id == user.id:
        return group
    raise HTTPException(status_code=403, detail="Not authorized to view this group")


def _monday(day: date) -> date:
    return week_bounds(day)[0]


def _upsert(
    db: Session,
    *,
    student_id: int,
    week_start: date,
    group_id: Optional[int],
    template_key: str,
    template_auto: bool,
    facts: Dict[str, Any],
    body_generated: str,
    curator_note: Optional[str],
    user_id: int,
) -> ParentReport:
    """Один отчёт на (ученик, неделя). Перегенерация затирает и правку куратора.

    Это осознанно: куратор нажал «перегенерировать» — он просит новый текст, а старый
    остаётся ему виден на экране до перезагрузки, если он передумал.
    """
    row = db.query(ParentReport).filter(
        ParentReport.student_id == student_id,
        ParentReport.week_start == week_start,
    ).first()
    if row is None:
        row = ParentReport(student_id=student_id, week_start=week_start)
        db.add(row)
    row.group_id = group_id
    row.template_key = template_key
    row.template_auto = template_auto
    row.facts_json = facts
    row.body_generated = body_generated
    row.body = body_generated
    row.curator_note = curator_note
    row.created_by = user_id
    db.flush()
    return row


def _serialize(row: Optional[ParentReport]) -> Optional[Dict[str, Any]]:
    if row is None:
        return None
    return {
        "template_key": row.template_key,
        "template_auto": row.template_auto,
        "body": row.body,
        "body_generated": row.body_generated,
        "curator_note": row.curator_note,
        "facts": row.facts_json,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


@router.get("/groups/{group_id}")
def group_overview(
    group_id: int,
    week: date = Query(..., description="Любой день нужной недели"),
    current_user: UserInDB = Depends(get_current_user_dependency),
    db: Session = Depends(get_db),
):
    """Кто в группе и по кому отчёт за неделю уже сделан."""
    _require_group_access(group_id, current_user, db)
    week_start = _monday(week)

    students = (
        db.query(UserInDB)
        .join(GroupStudent, GroupStudent.student_id == UserInDB.id)
        .filter(GroupStudent.group_id == group_id, UserInDB.role == "student")
        .order_by(UserInDB.name)
        .all()
    )
    done = {
        row.student_id: row
        for row in db.query(ParentReport).filter(
            ParentReport.student_id.in_([s.id for s in students] or [0]),
            ParentReport.week_start == week_start,
        ).all()
    }
    return {
        "week_start": week_start.isoformat(),
        "students": [
            {"id": s.id, "name": s.name, "report": _serialize(done.get(s.id))}
            for s in students
        ],
    }


@router.get("/students/{student_id}")
async def student_facts(
    student_id: int,
    week: date = Query(...),
    current_user: UserInDB = Depends(get_current_user_dependency),
    db: Session = Depends(get_db),
):
    """Факты за неделю и сохранённый отчёт. LLM здесь не вызывается."""
    _require_report_access(student_id, current_user, db)
    week_start = _monday(week)
    facts = await build_week_facts(db, student_id, week_start)
    suggested, reason = pick_template(facts)
    row = db.query(ParentReport).filter(
        ParentReport.student_id == student_id, ParentReport.week_start == week_start
    ).first()
    return {
        "week_start": week_start.isoformat(),
        "facts": facts,
        "suggested_template": suggested,
        "suggested_reason": reason,
        "report": _serialize(row),
    }


@router.post("/students/{student_id}")
async def generate(
    student_id: int,
    payload: GenerateBody,
    current_user: UserInDB = Depends(get_current_user_dependency),
    db: Session = Depends(get_db),
):
    """Сгенерировать отчёт и сохранить его."""
    _require_report_access(student_id, current_user, db)
    week_start = _monday(payload.week)
    facts = await build_week_facts(db, student_id, week_start, curator_note=payload.note)

    suggested, reason = pick_template(facts)
    template_key = payload.template or suggested
    if template_key not in {"t1", "t2", "t3", "t4", "t5"}:
        raise HTTPException(status_code=422, detail="Unknown template")

    prose = await generate_prose(facts, template_key)
    body = render(facts, template_key, prose, curator_name=current_user.name)

    row = _upsert(
        db, student_id=student_id, week_start=week_start,
        group_id=(facts.get("group") or {}).get("id"),
        template_key=template_key, template_auto=payload.template is None,
        facts=facts, body_generated=body, curator_note=payload.note,
        user_id=current_user.id,
    )
    db.commit()
    return {
        "week_start": week_start.isoformat(),
        "facts": facts,
        "suggested_template": suggested,
        "suggested_reason": reason,
        # Пустая проза — не ошибка: каркас с числами отрендерен, куратор допишет руками.
        "prose_degraded": not prose,
        "report": _serialize(row),
    }


@router.put("/students/{student_id}")
def save_edit(
    student_id: int,
    payload: SaveBody,
    current_user: UserInDB = Depends(get_current_user_dependency),
    db: Session = Depends(get_db),
):
    """Сохранить правку куратора."""
    _require_report_access(student_id, current_user, db)
    week_start = _monday(payload.week)
    row = db.query(ParentReport).filter(
        ParentReport.student_id == student_id, ParentReport.week_start == week_start
    ).first()
    if row is None:
        raise HTTPException(status_code=404, detail="Report not generated yet")
    row.body = payload.body
    row.curator_note = payload.note
    db.commit()
    return _serialize(row)
