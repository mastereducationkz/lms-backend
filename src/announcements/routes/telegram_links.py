"""Which Telegram chat is which LMS group's — the setup behind lesson invitations.

The owner's choice (2026-09-10): the LMS suggests matches between approved Telegram chats and
LMS groups by name, and a person confirms each one. Same gate as Telegram announcements
(admin, head curator, head teacher): a link decides where a message lands.
"""
from datetime import datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.orm import Session

from src.announcements.models import TelegramGroupLink, TelegramLessonInvitation
from src.config import get_db
from src.schemas.models import Group, UserInDB
from src.services import group_bot_settings, support_client, telegram_invitations
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


GROUP_RUNNING, GROUP_NOT_STARTED, GROUP_STOPPED, GROUP_FINISHED = "running", "not_started", "stopped", "finished"

# Only these get name suggestions: a finished group has no lessons left to announce, and there
# are two hundred of them, each with an old chat of a matching name.
SUGGESTABLE = {GROUP_RUNNING, GROUP_NOT_STARTED}


def _group_statuses(db: Session, groups: list) -> dict:
    """Each group's state, with "running" decided by the calendar's own rule.

    Every group can be linked (owner, 2026-09-11): a new group before its first student is
    enrolled — so its chat is ready on day one — and an old one, if anyone wants it. Invitations
    still go only to lessons of running groups in LMS Meet rooms; the label says why a linked
    group may not be getting any yet.
    """
    running = {gid for (gid,) in db.query(Group.id).filter(operational_group_clause())}
    out = {}
    for g in groups:
        if g.id in running:
            out[g.id] = GROUP_RUNNING
        elif g.is_over:
            out[g.id] = GROUP_FINISHED
        elif g.is_active is False:
            out[g.id] = GROUP_STOPPED
        else:
            out[g.id] = GROUP_NOT_STARTED  # switched on, not finished, nobody enrolled yet
    return out


@router.get("")
def list_links(db: Session = Depends(get_db), current_user: UserInDB = Depends(require_role(LINKER_ROLES))):
    links = {link.lms_group_id: link for link in db.query(TelegramGroupLink).all()}
    groups = db.query(Group.id, Group.name, Group.is_active, Group.is_over).order_by(Group.name).all()
    status = _group_statuses(db, groups)

    chats, chats_error = [], None
    try:
        chats = _approved_chats(current_user)
    except HTTPException as exc:
        chats_error = str(exc.detail)
    titles = {c["id"]: c["title"] for c in chats}

    suggestions = telegram_invitations.suggest_links(
        [(g.id, g.name) for g in groups if status[g.id] in SUGGESTABLE], [(c["id"], c["title"]) for c in chats],
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
                "status": status[g.id],
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


class GroupBotSettingsBody(BaseModel):
    enabled: Optional[bool] = None
    #: "pilot" — only the chats of teachers with a Workspace account; "all" — every linked chat.
    scope: Optional[str] = None


@router.get("/group-bot/settings")
def get_group_bot_settings(db: Session = Depends(get_db),
                           current_user: UserInDB = Depends(require_role(sorted(group_bot_settings.READERS)))):
    """The switch behind the bot that answers in group chats. The same people who link the chats
    read it; only an admin flips it."""
    return group_bot_settings.describe(db)


@router.put("/group-bot/settings")
def put_group_bot_settings(body: GroupBotSettingsBody, db: Session = Depends(get_db),
                           current_user: UserInDB = Depends(require_role(sorted(group_bot_settings.WRITERS)))):
    """On: the bot answers when a student tags it in a linked chat — by default only in the
    chats of the recording pilot's teachers. Off: it says nothing anywhere."""
    try:
        group_bot_settings.update(db, current_user, enabled=body.enabled, scope=body.scope)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    return group_bot_settings.describe(db)
