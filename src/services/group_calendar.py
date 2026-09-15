"""A real Google Calendar per live group, owned by the robot account and kept in sync.

Owner, 2026-09-15: a subscribed calendar must match the actual schedule at all times. Google
Calendar refreshes an ICS subscription only every 12–24 hours, so for Google users the group's
entries live in a real calendar the robot owns and shares read-only by link; the worker writes
every change within about a minute. Apple/Outlook take the signed ICS feed of the same entries
(:mod:`src.services.calendar_items` is the one source for both).

Safety: nothing here may break the Meet/Drive recording pipeline. It uses its own client with
the `calendar` scope; a token consented before that scope fails only here (paused for an hour,
logged), and Google quota refusals pause calendar creation without stopping the sync of
calendars that already exist.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

from src.announcements.models import TelegramGroupLink
from src.config import SessionLocal
from src.events.calendar_models import GroupGoogleCalendar
from src.schemas.models import Event, Group, UserInDB
from src.services import calendar_items, google_workspace
from src.utils.auth_utils import SECRET_KEY

logger = logging.getLogger(__name__)

TIMEZONE = "Asia/Almaty"
DESCRIPTION = "Master Education — расписание группы (обновляется автоматически)"
PAUSE = timedelta(hours=1)
_paused_until: Optional[datetime] = None            # no `calendar` scope: everything waits
_creation_paused_until: Optional[datetime] = None   # creation quota spent: existing calendars still sync


class ScopeMissing(RuntimeError):
    """The robot's token was consented without the `calendar` scope."""


class QuotaExceeded(RuntimeError):
    """Google refused for quota (calendar creation or request rate)."""


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def enabled() -> bool:
    return os.getenv("ENABLE_GROUP_CALENDARS") == "true" and google_workspace.oauth_configured()


def _int_env(name: str, default: int) -> int:
    try:
        return max(0, int(os.getenv(name, str(default))))
    except ValueError:
        return default


# ── which groups get a calendar ──────────────────────────────────────────────────────────

def is_live(db, group: Group, now: Optional[datetime] = None) -> bool:
    """A group gets a calendar exactly when its chat is live for the group bot — one rule
    (:func:`group_bot_settings.is_live`): active, not over, its regular teacher connected to a
    Workspace account that is not suspended, a linked chat, and lessons ahead."""
    from src.services import group_bot_settings

    return group_bot_settings.is_live(db, group, now or _now())


def live_groups(db, now: Optional[datetime] = None) -> list[Group]:
    candidates = (db.query(Group)
                  .join(TelegramGroupLink, TelegramGroupLink.lms_group_id == Group.id)
                  .join(UserInDB, UserInDB.id == Group.teacher_id)
                  .filter(Group.is_active.is_(True), Group.is_over.is_(False),
                          UserInDB.workspace_email.isnot(None))
                  .order_by(Group.id).all())
    return [group for group in candidates if is_live(db, group, now)]


# ── links ────────────────────────────────────────────────────────────────────────────────

def api_base() -> str:
    return (os.getenv("LMS_API_PUBLIC_URL") or "https://lmsapi.mastereducation.kz").rstrip("/")


def group_sig(group_id: int) -> str:
    return hmac.new(SECRET_KEY.encode(), f"calendar-feed:group:{group_id}".encode(),
                    hashlib.sha256).hexdigest()[:16]


def verify_group_sig(group_id: int, sig: str) -> bool:
    return hmac.compare_digest(group_sig(group_id), sig or "")


def group_ics_url(group_id: int) -> str:
    return f"{api_base()}/calendar/feeds/group/{group_id}-{group_sig(group_id)}.ics"


def personal_ics_url(token: str) -> str:
    return f"{api_base()}/calendar/feeds/me/{token}.ics"


def webcal(url: str) -> str:
    return "webcal://" + url.split("://", 1)[-1]


def google_add_url(calendar_id: str) -> str:
    cid = base64.urlsafe_b64encode(calendar_id.encode()).decode().rstrip("=")
    return f"https://calendar.google.com/calendar/u/0?cid={cid}"


def subscribe_links(db, group: Optional[Group]) -> Optional[dict]:
    """{"google_url": add-to-Google link or None until the calendar exists and is shared,
    "ics_url": the signed group feed}. The bot's pinned message and the LMS page use this."""
    if group is None:
        return None
    row = db.query(GroupGoogleCalendar).filter(GroupGoogleCalendar.lms_group_id == group.id).first()
    return {"google_url": google_add_url(row.calendar_id) if row is not None and row.public else None,
            "ics_url": group_ics_url(group.id)}


# ── Google ───────────────────────────────────────────────────────────────────────────────

def google_event_id(key: str) -> str:
    """Base32hex of the entry key: only [a-v0-9], unique, and the same on every run."""
    return base64.b32hexencode(key.encode()).decode().lower().rstrip("=")


def _reason(exc: Exception) -> tuple[Optional[int], str]:
    status = getattr(getattr(exc, "resp", None), "status", None) or getattr(exc, "status_code", None)
    content = getattr(exc, "content", b"") or b""
    text = content.decode("utf-8", "replace") if isinstance(content, bytes) else str(content)
    return (int(status) if status else None), f"{text} {exc}"


def _classify(exc: Exception) -> Exception:
    status, text = _reason(exc)
    lowered = text.lower()
    if "invalid_scope" in lowered or "insufficientpermissions" in lowered or "scope_insufficient" in lowered:
        return ScopeMissing(text[:300])
    if status in (403, 429) and any(word in lowered for word in ("usagelimits", "ratelimitexceeded",
                                                                 "quotaexceeded", "userratelimitexceeded")):
        return QuotaExceeded(text[:300])
    return exc


def call(request, *, retries: int = 3, sleep=time.sleep):
    """Execute one request; back off on rate limits, turn scope/quota refusals into our types."""
    for attempt in range(retries + 1):
        try:
            return request.execute()
        except Exception as exc:
            mapped = _classify(exc)
            if isinstance(mapped, QuotaExceeded) and "ratelimit" in str(mapped).lower() and attempt < retries:
                sleep(2 ** attempt)
                continue
            raise mapped


def _body(item) -> dict:
    digest = hashlib.sha1(repr((item.summary, item.description, item.start, item.end, item.day,
                                item.url)).encode()).hexdigest()
    body = {"summary": item.summary, "description": item.description,
            "extendedProperties": {"private": {"lms": "1", "key": item.key, "h": digest}}}
    if item.all_day:
        body["start"] = {"date": item.day.isoformat()}
        body["end"] = {"date": (item.day + timedelta(days=1)).isoformat()}
        body["transparency"] = "transparent"
    else:
        end = item.end or item.start + timedelta(hours=1)
        body["start"] = {"dateTime": f"{item.start:%Y-%m-%dT%H:%M:%S}Z", "timeZone": TIMEZONE}
        body["end"] = {"dateTime": f"{end:%Y-%m-%dT%H:%M:%S}Z", "timeZone": TIMEZONE}
    return body


def ensure_calendar(db, group: Group, service) -> GroupGoogleCalendar:
    row = db.query(GroupGoogleCalendar).filter(GroupGoogleCalendar.lms_group_id == group.id).first()
    if row is None:
        created = call(service.calendars().insert(
            body={"summary": group.name, "timeZone": TIMEZONE, "description": DESCRIPTION}))
        row = GroupGoogleCalendar(lms_group_id=group.id, calendar_id=created["id"])
        db.add(row)
        db.commit()
    if not row.public:
        call(service.acl().insert(calendarId=row.calendar_id,
                                  body={"role": "reader", "scope": {"type": "default"}}))
        row.public = True
        db.commit()
    return row


def sync(db, group: Group, row: GroupGoogleCalendar, service, now: Optional[datetime] = None,
         force: bool = False) -> str:
    """Make the calendar hold exactly the group's entries. "unchanged" | "synced"."""
    now = now or _now()
    items = calendar_items.group_items(db, group, now)
    digest = calendar_items.items_hash(items + [calendar_items.CalendarItem(key="name", summary=group.name)])
    if row.synced_hash == digest and not force:
        return "unchanged"
    desired = {google_event_id(item.key): _body(item) for item in items}
    existing, page = {}, None
    while True:
        response = call(service.events().list(calendarId=row.calendar_id, privateExtendedProperty="lms=1",
                                              maxResults=2500, pageToken=page, showDeleted=False))
        existing.update({event["id"]: event for event in response.get("items", [])})
        page = response.get("nextPageToken")
        if not page:
            break
    for event_id, body in desired.items():
        current = existing.get(event_id)
        if current is None:
            try:
                call(service.events().insert(calendarId=row.calendar_id, body={**body, "id": event_id}))
            except Exception as exc:
                if _reason(exc)[0] != 409:             # a cancelled copy with this id: bring it back
                    raise
                call(service.events().update(calendarId=row.calendar_id, eventId=event_id,
                                             body={**body, "status": "confirmed"}))
        elif ((current.get("extendedProperties") or {}).get("private") or {}).get("h") != \
                body["extendedProperties"]["private"]["h"]:
            call(service.events().update(calendarId=row.calendar_id, eventId=event_id, body=body))
    for event_id in set(existing) - set(desired):
        try:
            call(service.events().delete(calendarId=row.calendar_id, eventId=event_id))
        except Exception as exc:
            if _reason(exc)[0] not in (404, 410):
                raise
    call(service.calendars().patch(calendarId=row.calendar_id, body={"summary": group.name}))
    row.synced_hash, row.synced_at, row.last_error, row.error_at = digest, now, None, None
    db.commit()
    return "synced"


def run_once(db, service=None, now: Optional[datetime] = None) -> dict:
    """One pass: create (paced) calendars for live groups that have none, sync changed ones."""
    global _paused_until, _creation_paused_until
    now = now or _now()
    if _paused_until and now < _paused_until:
        return {"skipped": "paused"}
    service = service or google_workspace.group_calendars_client()
    created = synced = failed = 0
    create_cap = _int_env("GROUP_CALENDAR_CREATE_PER_RUN", 5)
    sync_cap = _int_env("GROUP_CALENDAR_SYNC_PER_RUN", 20)
    for group in live_groups(db, now):
        row = db.query(GroupGoogleCalendar).filter(GroupGoogleCalendar.lms_group_id == group.id).first()
        try:
            if row is None or not row.public:
                if created >= create_cap or (_creation_paused_until and now < _creation_paused_until):
                    continue
                row = ensure_calendar(db, group, service)
                created += 1
            if sync(db, group, row, service, now) == "synced":
                synced += 1
                if synced >= sync_cap:
                    break
        except ScopeMissing as exc:
            # Refused before anything was written (every DB write follows a successful call).
            _paused_until = now + PAUSE
            logger.warning("group calendars: the robot token has no `calendar` scope; paused 1 h (%s)", exc)
            return {"skipped": "scope_missing", "created": created, "synced": synced}
        except QuotaExceeded as exc:
            if row is None or not row.public:
                _creation_paused_until = now + PAUSE
                logger.warning("group calendars: creation quota reached; creating again in 1 h (%s)", exc)
            else:
                failed += 1
        except Exception as exc:
            db.rollback()
            failed += 1
            logger.warning("group calendars: group %s failed: %s", group.id, str(exc)[:300])
            if row is not None and row.id:
                row.last_error, row.error_at = str(exc)[:1000], now
                db.commit()
    return {"created": created, "synced": synced, "failed": failed}


class GroupCalendarWorker:
    """Scheduler-container thread: one pass a minute while ENABLE_GROUP_CALENDARS is on."""

    def __init__(self, poll_interval: int = 60):
        self.poll_interval = max(15, int(poll_interval))
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if not enabled():
            logger.info("group calendars: off (ENABLE_GROUP_CALENDARS != true or no Google OAuth env)")
            return
        self._thread = threading.Thread(target=self._loop, name="group-calendars", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            db = SessionLocal()
            try:
                result = run_once(db)
                if result.get("created") or result.get("synced") or result.get("failed"):
                    logger.info("group calendars: %s", result)
            except Exception:
                logger.exception("group calendars: pass failed")
            finally:
                db.close()
            self._stop.wait(self.poll_interval)
