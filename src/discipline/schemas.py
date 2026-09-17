"""What the register's endpoints accept."""
from datetime import date
from typing import Optional

from pydantic import BaseModel, Field


class DecisionIn(BaseModel):
    """A head teacher settling one finding: the amount owed, and why."""

    event_id: Optional[int] = None          # None for something Meet never saw
    teacher_id: int
    day: date
    kind: str = Field(pattern="^(late|ended_early|miss)$")
    amount: int = Field(ge=0)               # 0 waives it
    reason_code: Optional[str] = None
    note: Optional[str] = None
    minutes: Optional[int] = None
    proposed_amount: Optional[int] = None
