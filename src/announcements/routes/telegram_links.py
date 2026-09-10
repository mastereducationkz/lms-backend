"""Which Telegram chat is which LMS group's — the setup behind lesson invitations.

The owner's choice (2026-09-10): the LMS suggests matches between approved Telegram chats and
LMS groups by name, and a person confirms each one. Same gate as Telegram announcements
(admin, head curator, head teacher): a link decides where a message lands.
"""
from datetime import datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from src.announcements.models import TelegramGroupLink, TelegramLessonInvitation
from src.config import get_db
from src.schemas.models import Group, UserInDB
from src.services import support_client, telegram_invitations
from src.services.operational_groups import operational_group_clause
from src.utils.permissions import require_role

router = APIRouter()

LINKER_ROLES = ["admin", "head_curator", "head_teacher"]


def _approved_chats(current_user: UserInDB) -> list:
    """Approved, active chats from Support's registry: [{id, title}]."""
    data = support_client.call(
        "GET", "/telegram/groups",
        actor_email=current_user.email, actor_name=current_user.name,
        params={"status_filter": "approved"},
    )
    items = data if isinstance(data, list) else (data or {}).get("items") or (data or {}).get("groups") or []
    return [
        {"id": item["id"], "title": item.get("title") or f"Chat {item['id']}"}
        for item in items
        if item.get("status", "approved") == "approved" and item.get("is_active", True)
    ]


@router.get("")
def list_links(db: Session = Depends(get_db), current_user: UserInDB = Depends(require_role(LINKER_ROLES))):
    links = {link.lms_group_id: link for link in db.query(TelegramGroupLink).all()}
    groups = (
        db.query(Group.id, Group.name)
        # Running groups, plus any already linked (so a stopped group's link can be removed).
        .filter(or_(operational_group_clause(), Group.id.in_(list(links) or [-1])))
        .order_by(Group.name)
        .all()
    )

    chats, chats_error = [], None
    try:
        chats = _approved_chats(current_user)
    except HTTPException as exc:
        chats_error = str(exc.detail)
    titles = {c["id"]: c["title"] for c in chats}

    suggestions = telegram_invitations.suggest_links(
        [(g.id, g.name) for g in groups], [(c["id"], c["title"]) for c in chats],
        taken_groups=set(links), taken_chats={link.support_group_id for link in links.values()},
    )

    latest = dict(
        db.query(TelegramLessonInvitation.lms_group_id, func.max(TelegramLessonInvitation.id))
        .group_by(TelegramLessonInvitation.lms_group_id).all()
    )
    last_rows = {
        row.lms_group_id: row
        for row in db.query(TelegramLessonInvitation).filter(TelegramLessonInvitation.id.in_(list(latest.values()) or [-1]))
    }

    def _iso(value):
        return value.isoformat() + "Z" if value else None

    return {
        "enabled": telegram_invitations.enabled(),
        "chats_error": chats_error,
        "chats": chats,
        "groups": [
            {
                "id": g.id,
                "name": g.name,
                "link": ({
                    "chat_id": links[g.id].support_group_id,
                    "chat_title": titles.get(links[g.id].support_group_id) or links[g.id].chat_title,
                    "chat_available": links[g.id].support_group_id in titles if not chats_error else None,
                    "linked_at": _iso(links[g.id].linked_at),
                } if g.id in links else None),
                "suggestion": ({"chat_id": suggestions[g.id][0], "chat_title": suggestions[g.id][1],
                                "score": suggestions[g.id][2]} if g.id in suggestions else None),
                "last_invitation": ({
                    "status": last_rows[g.id].status,
                    "at": _iso(last_rows[g.id].sent_at or last_rows[g.id].created_at),
                    "error": last_rows[g.id].error,
                } if g.id in last_rows else None),
            }
            for g in groups
        ],
    }


class LinkBody(BaseModel):
    support_group_id: Optional[int] = None


class Pair(BaseModel):
    lms_group_id: int
    support_group_id: int


class ConfirmBody(BaseModel):
    pairs: List[Pair]


def _upsert(db: Session, lms_group_id: int, chat_id: int, chat_title: Optional[str], user_id: int):
    link = db.query(TelegramGroupLink).filter_by(lms_group_id=lms_group_id).first()
    if link is None:
        link = TelegramGroupLink(lms_group_id=lms_group_id)
        db.add(link)
    link.support_group_id = chat_id
    link.chat_title = chat_title
    link.linked_by = user_id
    link.linked_at = datetime.now(timezone.utc).replace(tzinfo=None)
    return link


def _verified_titles(current_user: UserInDB, chat_ids: set) -> dict:
    """Titles of the requested chats, refusing any that is not an approved, active chat."""
    try:
        chats = {c["id"]: c["title"] for c in _approved_chats(current_user)}
    except HTTPException as exc:
        raise HTTPException(status_code=503, detail=f"Cannot check the Telegram chat right now: {exc.detail}")
    missing = sorted(chat_ids - set(chats))
    if missing:
        raise HTTPException(status_code=422, detail=f"Not an approved Telegram chat: {missing}")
    return chats


@router.put("/{lms_group_id}")
def set_link(lms_group_id: int, body: LinkBody, db: Session = Depends(get_db),
             current_user: UserInDB = Depends(require_role(LINKER_ROLES))):
    if db.get(Group, lms_group_id) is None:
        raise HTTPException(status_code=404, detail="Group not found")
    if body.support_group_id is None:
        db.query(TelegramGroupLink).filter_by(lms_group_id=lms_group_id).delete()
        db.commit()
        return {"lms_group_id": lms_group_id, "link": None}
    titles = _verified_titles(current_user, {body.support_group_id})
    _upsert(db, lms_group_id, body.support_group_id, titles[body.support_group_id], current_user.id)
    db.commit()
    return {"lms_group_id": lms_group_id,
            "link": {"chat_id": body.support_group_id, "chat_title": titles[body.support_group_id]}}


@router.post("/confirm")
def confirm_links(body: ConfirmBody, db: Session = Depends(get_db),
                  current_user: UserInDB = Depends(require_role(LINKER_ROLES))):
    """Confirm several suggestions at once — all or nothing."""
    if not body.pairs:
        return {"linked": 0}
    group_ids = {p.lms_group_id for p in body.pairs}
    found = {gid for (gid,) in db.query(Group.id).filter(Group.id.in_(group_ids)).all()}
    if found != group_ids:
        raise HTTPException(status_code=404, detail=f"Groups not found: {sorted(group_ids - found)}")
    titles = _verified_titles(current_user, {p.support_group_id for p in body.pairs})
    for p in body.pairs:
        _upsert(db, p.lms_group_id, p.support_group_id, titles[p.support_group_id], current_user.id)
    db.commit()
    return {"linked": len(body.pairs)}
