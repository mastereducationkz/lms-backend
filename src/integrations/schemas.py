"""Event envelope (Platform Integration Pack §2.2). Validation only — no business rules."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

PLATFORMS = ("ielts", "sat")


def to_naive_utc(value: datetime) -> datetime:
    """Every timestamp is stored naive-UTC, like the rest of the LMS schema."""
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


class StudentRef(BaseModel):
    model_config = ConfigDict(extra="allow")

    email: Optional[str] = None
    zitadel_subject: Optional[str] = None
    platform_user_id: Optional[int] = None
    platform_student_id: Optional[str] = None

    @field_validator("email", mode="before")
    @classmethod
    def _lower_email(cls, value):
        if value is None:
            return None
        value = str(value).strip().lower()
        return value or None

    @field_validator("zitadel_subject", mode="before")
    @classmethod
    def _strip_subject(cls, value):
        if value is None:
            return None
        value = str(value).strip()
        return value or None


class Envelope(BaseModel):
    model_config = ConfigDict(extra="allow")

    event_id: str = Field(min_length=1, max_length=64)
    event_type: str = Field(min_length=1, max_length=64)
    platform: Literal["ielts", "sat"]
    schema_version: Literal[1]
    occurred_at: datetime
    student: Optional[StudentRef] = None
    data: dict[str, Any]

    @field_validator("event_id")
    @classmethod
    def _uuid(cls, value: str) -> str:
        try:
            return str(uuid.UUID(value))
        except (ValueError, AttributeError, TypeError) as exc:
            raise ValueError("must be a UUID") from exc

    @field_validator("occurred_at")
    @classmethod
    def _naive_utc(cls, value: datetime) -> datetime:
        return to_naive_utc(value)


class EventsBatch(BaseModel):
    """Request body of ``POST /integrations/events``. Per-event validation happens in the ingest
    service so one bad event never fails the batch; only the batch shape is enforced here."""

    events: list[dict[str, Any]] = Field(max_length=100)


def validation_reason(exc: ValidationError) -> str:
    """One line per failed field: ``field: message``."""
    parts = []
    for err in exc.errors():
        loc = ".".join(str(p) for p in err.get("loc", ())) or "body"
        parts.append(f"{loc}: {err.get('msg', 'invalid')}")
    return "; ".join(parts)[:500]
