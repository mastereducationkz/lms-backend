"""The buttons under the bot's answers and on its pinned timetable (owner, 2026-09-15).

Three buttons answer in a popup only the person who tapped sees — nothing is posted, so a room of
thirty students can tap all day without a single message (Support turns ``callback`` into
``callback_data`` and asks :mod:`group_bot_popup` for the text). The fourth, 🔗 Урок, is a plain
link: a popup cannot hold a tappable Meet link, and an answer stays in the chat for days, so the
link is not the Meet room itself but ``/tg/l/<group>-<sig>`` on the LMS API, which redirects to
whichever lesson is running or next *at the moment of the tap* (:mod:`src.routes.group_bot_links`).

The signature keeps anyone from walking group ids to collect Meet links: it is an HMAC with the
same secret that signs the LMS's own tokens, and a link is worth exactly what the bot already posts
into that chat five minutes before each lesson.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
import re
from typing import Optional

from src.utils.auth_utils import SECRET_KEY

logger = logging.getLogger(__name__)

API_BASE_DEFAULT = "https://lmsapi.mastereducation.kz"
POPUP_ACTIONS = ("schedule", "lessons", "homework")
_TOKEN = re.compile(r"^(\d{1,10})-([0-9a-f]{16})$")


def api_url(path: str) -> str:
    """The LMS API's public address — Telegram only opens https links in a button."""
    base = (os.getenv("LMS_API_PUBLIC_URL") or "").strip().rstrip("/") or API_BASE_DEFAULT
    return f"{base}{path}"


def lesson_link_sig(group_id: int) -> str:
    message = f"group-lesson-link:{int(group_id)}".encode()
    return hmac.new(str(SECRET_KEY).encode(), message, hashlib.sha256).hexdigest()[:16]


def lesson_link(group_id: int) -> str:
    return api_url(f"/tg/l/{int(group_id)}-{lesson_link_sig(group_id)}")


def parse_lesson_token(token: str) -> Optional[int]:
    """The group id of a well-signed ``<group>-<sig>``, else ``None``."""
    match = _TOKEN.match(token or "")
    if not match:
        return None
    group_id = int(match.group(1))
    return group_id if hmac.compare_digest(match.group(2), lesson_link_sig(group_id)) else None


def keyboard(group_id: int) -> list:
    """Rows of buttons in the shape Support expects: ``callback`` for a popup, ``url`` for a link."""
    return [
        [{"text": "🗓 Расписание", "callback": "gb:schedule"},
         {"text": "📅 Ближайшие", "callback": "gb:lessons"}],
        [{"text": "📝 ДЗ", "callback": "gb:homework"},
         {"text": "🔗 Урок", "url": lesson_link(group_id)}],
    ]


def to_telegram(rows: list) -> dict:
    """The same rows as Telegram's ``reply_markup`` — for messages the LMS posts itself."""
    return {"inline_keyboard": [
        [{"text": button["text"], "callback_data": button["callback"]} if "callback" in button
         else {"text": button["text"], "url": button["url"]} for button in row]
        for row in rows
    ]}


def calendar_links(db, group) -> Optional[dict]:
    """``{"google_url", "ics_url"}`` for the group's calendar, or ``None`` while there is none.

    The calendar is its own feature (:mod:`src.services.group_calendar`); until it exists — or when
    it fails — the bot says a link is coming instead of breaking an answer or the pinned message.
    """
    try:
        from src.services import group_calendar
    except ImportError:
        return None
    try:
        links = group_calendar.subscribe_links(db, group)
    except Exception as exc:          # a calendar hiccup must never cost the timetable
        logger.warning("group bot: calendar links for group %s failed: %s", group.id, str(exc)[:200])
        return None
    return links if isinstance(links, dict) and links.get("ics_url") else None
