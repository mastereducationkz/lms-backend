"""Watch links: the CRM asks for one (service key), anyone holding it watches that one lesson.

The rules — one lesson, three hours, hashed, logged — live in
``src/services/recording_watch_links.py``. The public route is deliberately unauthenticated:
the key in the URL *is* the permission, and the people it exists for have no LMS account.
"""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from src.config import get_db
from src.routes.crm_internal import _require_crm_internal_key
from src.services import recording_watch_links as links

internal_router = APIRouter(dependencies=[Depends(_require_crm_internal_key)])
public_router = APIRouter()


class WatchLinkRequest(BaseModel):
    """Who asked, as the CRM names them — for the link's log, not for access."""
    issued_to: Optional[str] = Field(None, max_length=200)
    issued_role: Optional[str] = Field(None, max_length=50)


@internal_router.post("/recordings/{event_id}/watch-link")
def create_watch_link(event_id: int, body: WatchLinkRequest, db: Session = Depends(get_db)):
    try:
        return links.issue(db, event_id, issued_to=body.issued_to, issued_role=body.issued_role)
    except links.NothingToWatch:
        raise HTTPException(status_code=404, detail="This lesson has no recording to watch")


@public_router.get("/{token}")
def open_watch_link(token: str, db: Session = Depends(get_db)):
    try:
        return links.redeem(db, token)
    except links.LinkExpired:
        # 410, not 404: the page can say "open it again from the CRM" instead of "no such link".
        raise HTTPException(status_code=410, detail="This link has expired")
    except links.NothingToWatch:
        raise HTTPException(status_code=404, detail="Recording not found")
