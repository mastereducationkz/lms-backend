"""The register's endpoints.

Handlers are plain ``def`` on purpose, never ``async def``: an async handler or dependency that
touches the database runs on the event loop and blocks every other request of its worker — that is
what took the API down on 17.09 (see ``src/routes/auth.py``).
"""
from datetime import date, datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from src.config import get_db
from src.discipline import service
from src.discipline.rules import RULE_START, period_containing, periods_until
from src.discipline.schemas import DecisionIn
from src.routes.auth import get_current_user_dependency
from src.schemas.models import UserInDB

router = APIRouter()

DECIDES = frozenset({"admin", "head_teacher"})
READS = DECIDES | {"teacher"}


def _today() -> date:
    return service.almaty_day(datetime.now(timezone.utc).replace(tzinfo=None))


def _period_or_400(key: str):
    try:
        start = date.fromisoformat(key)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"«{key}» is not a date")
    period = period_containing(start)
    if period is None:
        raise HTTPException(
            status_code=400,
            detail=f"The register starts on {RULE_START.strftime('%d.%m.%Y')}, when the rule took effect.")
    return period


def _may_read(user: UserInDB) -> None:
    if getattr(user, "role", None) not in READS:
        raise HTTPException(status_code=403, detail="The register is for head teachers")


def _may_decide(user: UserInDB) -> None:
    if getattr(user, "role", None) not in DECIDES:
        raise HTTPException(status_code=403, detail="Only a head teacher or an admin decides a fine")


def _scope(user: UserInDB) -> Optional[list[int]]:
    """A teacher sees their own row and nobody else's; a head teacher sees everyone."""
    return None if getattr(user, "role", None) in DECIDES else [user.id]


@router.get("/periods")
def list_periods(db: Session = Depends(get_db),
                 current_user: UserInDB = Depends(get_current_user_dependency)):
    """Every half-month since the rule took effect, newest first."""
    _may_read(current_user)
    closed = {row.period_key for row in service.closed_periods(db)}
    return {"periods": [{"key": period.key, "label": period.label,
                         "start": period.start.isoformat(), "end": period.end.isoformat(),
                         "closed": period.key in closed}
                        for period in reversed(periods_until(_today()))],
            "rule_start": RULE_START.isoformat()}


@router.get("/register")
def read_register(period: str = Query(..., description="the period's first day, e.g. 2026-09-16"),
                  program: Optional[str] = None,
                  db: Session = Depends(get_db),
                  current_user: UserInDB = Depends(get_current_user_dependency)):
    _may_read(current_user)
    return service.register(db, _period_or_400(period), teacher_ids=_scope(current_user),
                            program=program, now=service.now())


@router.get("/day")
def read_day(teacher_id: int, day: date, db: Session = Depends(get_db),
             current_user: UserInDB = Depends(get_current_user_dependency)):
    _may_read(current_user)
    scope = _scope(current_user)
    if scope is not None and teacher_id not in scope:
        raise HTTPException(status_code=403, detail="That is another teacher's day")
    if period_containing(day) is None:
        raise HTTPException(
            status_code=400,
            detail=f"The register starts on {RULE_START.strftime('%d.%m.%Y')}, when the rule took effect.")
    return service.day_detail(db, teacher_id, day, now=service.now())


@router.post("/decisions")
def decide(body: DecisionIn, db: Session = Depends(get_db),
           current_user: UserInDB = Depends(get_current_user_dependency)):
    """Confirm, waive or reprice one finding."""
    _may_decide(current_user)
    try:
        decision = service.apply_decision(
            db, actor=current_user, event_id=body.event_id, teacher_id=body.teacher_id,
            day=body.day, kind=body.kind, amount=body.amount, reason_code=body.reason_code,
            note=body.note, minutes=body.minutes, proposed_amount=body.proposed_amount)
    except service.PeriodClosed:
        raise HTTPException(status_code=409, detail="That period is closed; payroll was paid on it")
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error))
    db.commit()
    return {"id": decision.id, "amount": decision.amount, "kind": decision.kind}


@router.post("/periods/{period_key}/close")
def close(period_key: str, db: Session = Depends(get_db),
          current_user: UserInDB = Depends(get_current_user_dependency)):
    """Freeze the period's totals for payroll."""
    _may_decide(current_user)
    period = _period_or_400(period_key)
    try:
        stored = service.close_period(db, period, current_user, now=service.now())
    except service.PeriodNotReady as error:
        raise HTTPException(status_code=409, detail=str(error))
    db.commit()
    return {"period": period.key, "closed_at": stored.closed_at.isoformat() + "Z",
            "totals": stored.totals}
