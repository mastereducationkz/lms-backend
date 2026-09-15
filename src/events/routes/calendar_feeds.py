"""Calendar subscriptions: the signed group ICS feed, the personal feed, and what the LMS
Calendar page offers (owner, 2026-09-15).

The two ``.ics`` routes are public — a calendar app cannot log in — so each carries its own
secret: the group feed an HMAC signature of the group id, the personal feed a random token the
owner can rotate, which ends every existing subscription at once.
"""
import re
import secrets
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy.orm import Session

from src.config import get_db
from src.events.calendar_models import CalendarFeedToken
from src.routes.auth import get_current_user_dependency
from src.schemas.models import Group, UserInDB
from src.services import calendar_ics, calendar_items, group_calendar

router = APIRouter()

_GROUP_FEED = re.compile(r"^(\d+)-([0-9a-f]{16})$")
ICS_HEADERS = {"Cache-Control": "private, max-age=300"}


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _ics(name: str, items) -> Response:
    return Response(content=calendar_ics.build(name, items), media_type="text/calendar; charset=utf-8",
                    headers=ICS_HEADERS)


@router.get("/feeds/group/{feed}.ics")
def group_feed(feed: str, db: Session = Depends(get_db)):
    match = _GROUP_FEED.match(feed or "")
    if not match or not group_calendar.verify_group_sig(int(match.group(1)), match.group(2)):
        raise HTTPException(status_code=404, detail="Not found")
    group = db.get(Group, int(match.group(1)))
    if group is None:
        raise HTTPException(status_code=404, detail="Not found")
    return _ics(group.name, calendar_items.group_items(db, group))


@router.get("/feeds/me/{token}.ics")
def personal_feed(token: str, db: Session = Depends(get_db)):
    row = db.query(CalendarFeedToken).filter(CalendarFeedToken.token == token).first() if token else None
    user = db.get(UserInDB, row.user_id) if row else None
    if user is None or not user.is_active:
        raise HTTPException(status_code=404, detail="Not found")
    return _ics(f"Master Education — {user.name}", calendar_items.user_items(db, user))


def _token_out(row: CalendarFeedToken) -> dict:
    url = group_calendar.personal_ics_url(row.token)
    return {"ics_url": url, "webcal_url": group_calendar.webcal(url),
            "created_at": row.created_at, "rotated_at": row.rotated_at}


def _token_for(db, user) -> CalendarFeedToken:
    row = db.query(CalendarFeedToken).filter(CalendarFeedToken.user_id == user.id).first()
    if row is None:
        row = CalendarFeedToken(user_id=user.id, token=secrets.token_urlsafe(24), created_at=_now())
        db.add(row)
        db.commit()
    return row


@router.get("/feed-token")
def get_feed_token(db: Session = Depends(get_db), current_user: UserInDB = Depends(get_current_user_dependency)):
    return _token_out(_token_for(db, current_user))


@router.post("/feed-token/rotate")
def rotate_feed_token(db: Session = Depends(get_db),
                      current_user: UserInDB = Depends(get_current_user_dependency)):
    row = _token_for(db, current_user)
    row.token, row.rotated_at = secrets.token_urlsafe(24), _now()
    db.commit()
    return _token_out(row)


@router.get("/subscriptions")
def subscriptions(db: Session = Depends(get_db), current_user: UserInDB = Depends(get_current_user_dependency)):
    """Every calendar this person can add: one per group they study in, teach or curate — with
    the Google link once that group's calendar exists — plus their personal feed."""
    group_ids = calendar_items.user_group_ids(db, current_user)
    groups = db.query(Group).filter(Group.id.in_(group_ids)).order_by(Group.name).all() if group_ids else []
    rows = []
    for group in groups:
        links = group_calendar.subscribe_links(db, group)
        rows.append({"group_id": group.id, "group_name": group.name,
                     "google_url": links["google_url"], "ics_url": links["ics_url"],
                     "webcal_url": group_calendar.webcal(links["ics_url"])})
    return {"groups": rows, "personal": _token_out(_token_for(db, current_user))}
