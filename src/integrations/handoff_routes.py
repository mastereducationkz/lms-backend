"""``POST /handoff/mint`` and ``GET /.well-known/handoff-jwks.json`` (Platform Integration Pack §3)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from src.integrations.handoff import HandoffError, build_jwks, mint_handoff
from src.routes.auth import get_current_user_dependency

handoff_router = APIRouter()
wellknown_router = APIRouter()


class MintRequest(BaseModel):
    platform: str = Field(min_length=1, max_length=16)
    return_to: str = Field(min_length=1, max_length=2048)


@handoff_router.post("/mint")
def mint(body: MintRequest, user=Depends(get_current_user_dependency)) -> dict:
    """Mint a 60-second handoff link to ``return_to`` on ``platform`` for the signed-in user."""
    try:
        return mint_handoff(user, body.platform, body.return_to)
    except HandoffError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc


@wellknown_router.get("/.well-known/handoff-jwks.json")
def handoff_jwks() -> JSONResponse:
    """Public verifying keys; platforms cache for 10 minutes (stale fallback on their side)."""
    try:
        jwks = build_jwks()
    except HandoffError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    return JSONResponse(jwks, headers={"Cache-Control": "public, max-age=600"})
