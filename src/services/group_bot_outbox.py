"""The plumbing the bot's own posts share: which chats are live, quiet hours, and the Support call.

Everything the bot says unprompted — the hello, the pinned timetable, a changed-timetable notice,
the digest, the last-chance reminder — goes through Support's idempotent ``/telegram/messages``
(and ``/telegram/messages/edit`` for the pinned message), exactly like lesson invitations do. Each
job claims its own row before it calls, so a crashed tick can at worst retry under the same
idempotency key, which Support answers with the message it already sent.

**Live** (:func:`group_bot_settings.is_live`) is computed once a tick for every linked chat and
handed to every job, because it reads the teacher, the Workspace directory and the calendar.
**A tick may make at most** :data:`CALLS_PER_TICK` **Support calls** across all jobs — a first
switch-on over ninety chats is paced over a few minutes instead of flooding Telegram.
**Quiet hours** are 23:00–08:00 Almaty (owner, 2026-09-15) for the digest, the last chance and the
timetable notice; lesson invitations and reschedule notices keep their own timing.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, time, timedelta, timezone
from typing import Optional

from fastapi import HTTPException

from src.announcements.models import TelegramGroupLink
from src.schemas.models import Group
from src.services import group_bot_render as render, group_bot_settings, support_client, workspace_directory

logger = logging.getLogger(__name__)

SYSTEM_ACTOR = "lms-group-bot@mastereducation.kz"
ACTOR_NAME = "LMS group bot"
MAX_ATTEMPTS = 3
SEND_TIMEOUT_SECONDS = 45
CALLS_PER_TICK = 20
QUIET_START, QUIET_END = time(23, 0), time(8, 0)


def flag(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in ("1", "true", "yes", "on")


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def in_quiet_hours(moment: datetime) -> bool:
    clock = render.local(moment).time()
    return clock >= QUIET_START or clock < QUIET_END


def quiet_hours_end(moment: datetime) -> datetime:
    """The 08:00 Almaty that ends the quiet hours ``moment`` falls in (naive UTC)."""
    local = render.local(moment)
    day = local.date() if local.time() < QUIET_END else local.date() + timedelta(days=1)
    return datetime.combine(day, QUIET_END) - render.ALMATY_OFFSET


class Budget:
    """How many Support calls this tick may still make."""

    def __init__(self, calls: int = CALLS_PER_TICK):
        self.left = calls

    def take(self) -> bool:
        if self.left <= 0:
            return False
        self.left -= 1
        return True


def live_links(db, now: datetime) -> list[tuple[Group, TelegramGroupLink]]:
    """Every linked chat whose group is live, with its link — the one list each job walks."""
    directory = workspace_directory.accounts_by_email(db)
    out = []
    for link in db.query(TelegramGroupLink).order_by(TelegramGroupLink.lms_group_id).all():
        group = db.get(Group, link.lms_group_id)
        if group is not None and group_bot_settings.is_live(db, group, now, directory=directory):
            out.append((group, link))
    return out


def post(support_group_id: int, text: str, idempotency_key: str, *, silent: bool,
         pin: bool = False, reply_markup: Optional[dict] = None,
         topic_id: Optional[int] = None, reply_to: Optional[int] = None) -> dict:
    """One message → ``{"status": sent|skipped|failed, "telegram_message_id", "error"}``.

    400/404/409/422 are ``skipped`` (the chat can never take it: unknown, unapproved, malformed);
    anything else is ``failed`` and retried by the caller while it has attempts left.
    ``topic_id`` posts into a topic of a forum group; ``reply_to`` answers an earlier message.
    """
    body = {"telegram_group_id": support_group_id, "text": text, "idempotency_key": idempotency_key,
            "silent": silent, "disable_web_page_preview": True, "parse_mode": "HTML"}
    if pin:
        body["pin"] = True
    if reply_markup:
        body["reply_markup"] = reply_markup
    if topic_id:
        body["message_thread_id"] = topic_id
    if reply_to:
        body["reply_to_message_id"] = reply_to
    try:
        result = support_client.call("POST", "/telegram/messages", actor_email=SYSTEM_ACTOR,
                                     actor_name=ACTOR_NAME, json_body=body,
                                     timeout=SEND_TIMEOUT_SECONDS) or {}
        return {"status": "sent", "telegram_message_id": result.get("telegram_message_id"),
                "pinned": result.get("pinned"), "pin_error": result.get("pin_error"), "error": None}
    except HTTPException as exc:
        detail = f"{exc.status_code}: {exc.detail}"[:500]
        status = "skipped" if exc.status_code in (400, 404, 409, 422) else "failed"
        return {"status": status, "telegram_message_id": None, "error": detail}
    except Exception as exc:
        return {"status": "failed", "telegram_message_id": None, "error": str(exc)[:500]}


def _act(path: str, body: dict) -> dict:
    """One Support message verb → its JSON plus ``ok``, ``gone`` and ``status_code``; never raises."""
    try:
        result = support_client.call("POST", path, actor_email=SYSTEM_ACTOR, actor_name=ACTOR_NAME,
                                     json_body=body, timeout=SEND_TIMEOUT_SECONDS) or {}
    except HTTPException as exc:
        return {"ok": False, "gone": False, "status_code": exc.status_code,
                "description": f"{exc.status_code}: {exc.detail}"[:500]}
    except Exception as exc:
        return {"ok": False, "gone": False, "status_code": None, "description": str(exc)[:500]}
    return {**result, "ok": bool(result.get("ok")), "gone": bool(result.get("gone")), "status_code": 200}


def edit(support_group_id: int, message_id: int, text: str, reply_markup: Optional[dict]) -> dict:
    """Edit one of the bot's messages → ``{"ok", "gone", "description"}``; never raises.

    Without ``reply_markup`` the message loses its buttons.
    """
    body = {"telegram_group_id": support_group_id, "message_id": message_id, "text": text,
            "parse_mode": "HTML", "disable_web_page_preview": True}
    if reply_markup:
        body["reply_markup"] = reply_markup
    return _act("/telegram/messages/edit", body)


def delete(support_group_id: int, message_id: int) -> dict:
    return _act("/telegram/messages/delete", {"telegram_group_id": support_group_id, "message_id": message_id})


def unpin(support_group_id: int, message_id: int) -> dict:
    return _act("/telegram/messages/unpin", {"telegram_group_id": support_group_id, "message_id": message_id})


def top_pinned(support_group_id: int) -> dict:
    """The message in the bar at the top of the chat → ``{"ok", "pinned_message_id", ...}``."""
    return _act("/telegram/messages/pinned", {"telegram_group_id": support_group_id})
