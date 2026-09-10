"""Save who was in each lesson's Meet room — every join and every leave.

Meet reports each call held in a room (``conferenceRecords``), the people in it
(``participants``: a Google account id and display name, or a guest's typed name) and each of
their stretches in the call (``participantSessions``: joined, left). A reconnect is a new
session. The robot account created every lesson room, so its existing
``meetings.space.created`` grant already reads all of this — no new permission (verified on
lesson 14156, 2026-09-10: 12 people, 11 signed in, 1 guest, times to the second).

Google keeps conference records for about 30 days, so this module copies them into
``meet_conferences`` / ``meet_participants`` / ``meet_participant_sessions``. A call is read
once, after it has ended, and never again (``synced_at``). What the record *means* — who is
who, who was late, which marks disagree — is worked out when it is read, in
``meet_presence``, so a later confirmation of an account fixes every past lesson at once.

Runs as a step of the recordings worker (it lists the same conferences), behind its own
switch ``ENABLE_MEET_ATTENDANCE``. ``python -m src.services.meet_attendance --days 30`` saves
everything Google still has, switch or not.
"""
import argparse
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

from src.schemas.models import MeetConference, MeetParticipant, MeetParticipantSession
from src.services import google_workspace, meet_recordings

logger = logging.getLogger(__name__)

# Longer than the recordings poller's 24 h: a worker that was down over a weekend should
# still find Friday's lessons. The unique conference name makes re-listing harmless.
LOOKBACK_HOURS = 72

# A call's people are read this long after it ends, so the last leave has been written.
SETTLE = timedelta(minutes=5)

PAGE_SIZE = 100


def enabled() -> bool:
    return os.getenv("ENABLE_MEET_ATTENDANCE", "").strip().lower() in ("1", "true", "yes", "on")


def _utc_naive(value: Optional[str]) -> Optional[datetime]:
    """Meet's RFC 3339 timestamp → naive UTC, the way every lesson time is stored."""
    parsed = meet_recordings._rfc3339(value)
    if parsed is None:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def _all_pages(list_call, key: str, **kwargs) -> list:
    items, token = [], None
    while True:
        response = list_call(pageSize=PAGE_SIZE, pageToken=token, **kwargs).execute()
        items.extend(response.get(key, []))
        token = response.get("nextPageToken")
        if not token:
            return items


def fetch_people(conference_record: str) -> list:
    """Everyone in one call, each with their sessions. Google API only — no database."""
    records = google_workspace.meet_client().conferenceRecords()
    people = []
    for participant in _all_pages(records.participants().list, "participants", parent=conference_record):
        kind, who = next(((k, participant[key]) for key, k in (
            ("signedinUser", "signed_in"), ("anonymousUser", "guest"), ("phoneUser", "phone"),
        ) if key in participant), ("guest", {}))
        sessions = _all_pages(records.participants().participantSessions().list, "participantSessions",
                              parent=participant["name"])
        people.append({
            "participant_name": participant["name"],
            "kind": kind,
            "google_user": who.get("user") if kind == "signed_in" else None,
            "display_name": who.get("displayName"),
            "sessions": [
                {"session_name": s["name"], "joined_at": _utc_naive(s.get("startTime")),
                 "left_at": _utc_naive(s.get("endTime"))}
                for s in sessions if s.get("startTime")
            ],
        })
    return people


def save_people(db, conference_id: int, event_id: int, people: list, now: datetime) -> int:
    """Write one call's people and sessions, then mark the call done. Idempotent by Meet's names."""
    names = [p["participant_name"] for p in people]
    existing = {p.participant_name: p for p in
                db.query(MeetParticipant).filter(MeetParticipant.participant_name.in_(names or [""]))}
    session_names = [s["session_name"] for p in people for s in p["sessions"]]
    seen_sessions = {n for (n,) in db.query(MeetParticipantSession.session_name)
                     .filter(MeetParticipantSession.session_name.in_(session_names or [""]))}

    for person in people:
        row = existing.get(person["participant_name"])
        if row is None:
            row = MeetParticipant(conference_id=conference_id, event_id=event_id,
                                  participant_name=person["participant_name"], kind=person["kind"],
                                  google_user=person["google_user"], display_name=person["display_name"])
            db.add(row)
            db.flush()
        for session in person["sessions"]:
            if session["session_name"] in seen_sessions:
                continue
            db.add(MeetParticipantSession(participant_id=row.id, **session))

    conference = db.get(MeetConference, conference_id)
    conference.synced_at = now
    db.commit()
    return len(people)


def _conference_row(db, conference: dict) -> Optional[MeetConference]:
    """The saved row for a listed call, creating it when the call is one of our lessons."""
    name = conference.get("name")
    row = db.query(MeetConference).filter(MeetConference.conference_record == name).first()
    if row is not None:
        return row
    # A Meet API call happens here; nothing is open on the database while it runs.
    db.commit()
    lesson = meet_recordings.match_lesson(db, meet_recordings.space_meet_code(conference.get("space") or ""))
    if lesson is None:
        return None  # not a lesson room (a test space, or a room from before the pilot)
    row = MeetConference(event_id=lesson.id, conference_record=name,
                         started_at=_utc_naive(conference.get("startTime")),
                         ended_at=_utc_naive(conference.get("endTime")))
    db.add(row)
    db.commit()
    return row


def sync(db, lookback_hours: int = LOOKBACK_HOURS, now: Optional[datetime] = None) -> int:
    """Save every ended, not-yet-saved lesson call from the lookback window. Returns calls saved.

    One call failing (Google hiccup, a deleted lesson) is logged and skipped; the next tick
    tries it again because its ``synced_at`` is still empty.
    """
    now = now or datetime.now(timezone.utc).replace(tzinfo=None)
    saved = 0
    for conference in meet_recordings.list_recent_conferences(lookback_hours):
        name = conference.get("name")
        if not name:
            continue
        try:
            row = _conference_row(db, conference)
            if row is None or row.synced_at is not None:
                continue
            ended = row.ended_at or _utc_naive(conference.get("endTime"))
            if ended is None or ended > now - SETTLE:
                continue  # still running, or only just over
            row.ended_at = ended
            conference_id, event_id = row.id, row.event_id
            db.commit()  # the Google calls below must not hold a transaction open (pgbouncer kills it at 60 s)
            people = fetch_people(name)
            save_people(db, conference_id, event_id, people, now)
            saved += 1
            logger.info("lesson %s: saved %s people from %s", event_id, len(people), name)
        except Exception as e:
            db.rollback()
            logger.warning("meet attendance %s: %s", name, e)
    return saved


def sync_if_enabled(db) -> int:
    """The recordings worker's step: nothing at all while the switch is off."""
    return sync(db) if enabled() else 0


def main(argv: Optional[list] = None) -> None:
    parser = argparse.ArgumentParser(description="Save Meet attendance for recent lessons.")
    parser.add_argument("--days", type=int, default=30, help="how far back (Google keeps about 30)")
    args = parser.parse_args(argv)
    from src.config import SessionLocal

    db = SessionLocal()
    try:
        print(f"saved {sync(db, lookback_hours=args.days * 24)} call(s)")
    finally:
        db.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    main()
