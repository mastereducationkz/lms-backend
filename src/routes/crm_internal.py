"""Internal CRM ↔ LMS routes (service-key auth). Extend for HTTP-based LMS writes (phase 2)."""
import os

from fastapi import APIRouter, Depends, Header, HTTPException
from typing import Annotated

router = APIRouter()


def _require_crm_internal_key(
    x_crm_service_key: Annotated[str | None, Header(alias="X-CRM-Service-Key")] = None,
) -> None:
    expected = os.getenv("CRM_INTERNAL_SERVICE_KEY", "").strip()
    if not expected:
        raise HTTPException(
            status_code=503,
            detail="LMS: set CRM_INTERNAL_SERVICE_KEY to enable /internal/crm routes",
        )
    if not x_crm_service_key or x_crm_service_key != expected:
        raise HTTPException(status_code=401, detail="Invalid or missing X-CRM-Service-Key")


@router.get("/health", dependencies=[Depends(_require_crm_internal_key)])
async def crm_internal_health() -> dict[str, str]:
    """CRM can probe with GET ``/internal/crm/health`` + ``X-CRM-Service-Key``."""
    return {"status": "ok", "service": "lms_crm_internal"}
