"""The Recordings library lists exactly what its own Watch button would allow — no more.

Run against a real database: the scope is an SQL clause, and the thing to prove is that the
listing, its filters, its paging and its facets all stay inside it.
"""
from datetime import datetime, timedelta

import pytest
from fastapi import HTTPException

from src.events.routes.recording_library import list_recordings
from src.schemas.models import LessonRecording
from tests.test_operational_groups import _user, db, world  # noqa: F401 - fixtures


def _list(db, user, **kw):
    """Call the route as FastAPI would, every parameter explicit (Query defaults are objects)."""
    params = dict(limit=24, cursor=None, q=None, group_id=None, teacher_id=None,
                  period="all", status=None)
    params.update(kw)
    return list_recordings(db=db, current_user=user, **params)


@pytest.fixture
def library(world):
    """Two groups with recorded lessons, one of them still processing, one failed."""
    db = world["db"]
    sat = world["group"](name="July 8 SAT - Gulzada")
    ielts = world["group"](name="IELTS June 14 2026")
    sat_student = world["enrol"](sat)
    ielts_student = world["enrol"](ielts)

    def recorded(group, days_ago, status="ready", poster=True, title=None):
        ev = world["lesson"](group, days_ahead=-days_ago, **({"title": title} if title else {}))
        rec = LessonRecording(
            event_id=ev.id, status=status, drive_file_id=f"drive-{ev.id}",
            hls_url=f"/uploads/videos/recordings/{ev.id}/master.m3u8" if status == "ready" else None,
            poster_url=f"/uploads/videos/recordings/{ev.id}/poster.jpg" if poster and status == "ready" else None,
            duration_seconds=3834 if status == "ready" else None,
        )
        db.add(rec)
        db.flush()
        return ev

    lessons = {
        "sat_old": recorded(sat, 20, title="July 8 SAT - Gulzada: Lesson 12"),
        "sat_new": recorded(sat, 1, title="July 8 SAT - Gulzada: Lesson 29"),
        "sat_processing": recorded(sat, 0.2, status="pending"),
        "ielts": recorded(ielts, 3, title="IELTS June 14 2026: Lesson 7"),
        "ielts_failed": recorded(ielts, 4, status="failed"),
    }
    return {"db": db, "world": world, "sat": sat, "ielts": ielts, "lessons": lessons,
            "sat_student": sat_student, "ielts_student": ielts_student}


def _ids(response):
    return [item["event_id"] for item in response["items"]]


def test_newest_first_with_the_fields_a_card_needs(library):
    admin = _user(library["db"], "admin")
    items = _list(library["db"], admin)["items"]
    starts = [i["start_datetime"] for i in items]
    assert starts == sorted(starts, reverse=True)
    card = next(i for i in items if i["event_id"] == library["lessons"]["sat_new"].id)
    assert card["groups"] == [{"id": library["sat"].id, "name": "July 8 SAT - Gulzada"}]
    assert card["teacher"]["id"] == library["world"]["teacher"].id
    assert card["duration_seconds"] == 3834
    assert card["start_datetime"].endswith("Z"), "UTC with a Z, like the calendar"


def test_a_student_sees_only_their_groups_finished_recordings(library):
    response = _list(library["db"], library["sat_student"])
    lessons = library["lessons"]
    assert set(_ids(response)) == {lessons["sat_old"].id, lessons["sat_new"].id}
    assert all(i["status"] == "ready" for i in response["items"])


def test_a_student_cannot_ask_for_the_unfinished_ones(library):
    response = _list(library["db"], library["sat_student"], status="pending")
    assert library["lessons"]["sat_processing"].id not in _ids(response)


def test_staff_also_see_processing_and_failed(library):
    owner = library["world"]["teacher"]
    ids = _ids(_list(library["db"], owner))
    assert library["lessons"]["sat_processing"].id in ids
    assert library["lessons"]["ielts_failed"].id in ids


def test_another_teacher_sees_nothing(library):
    assert _list(library["db"], _user(library["db"], "teacher"))["items"] == []


def test_filters(library):
    db, lessons = library["db"], library["lessons"]
    admin = _user(db, "admin")
    assert set(_ids(_list(db, admin, group_id=library["ielts"].id))) == {lessons["ielts"].id,
                                                                         lessons["ielts_failed"].id}
    assert set(_ids(_list(db, admin, status="ready"))) == {lessons["sat_old"].id, lessons["sat_new"].id,
                                                           lessons["ielts"].id}
    assert lessons["sat_old"].id not in _ids(_list(db, admin, period="7d"))
    assert set(_ids(_list(db, admin, q="lesson 29"))) == {lessons["sat_new"].id}
    assert set(_ids(_list(db, admin, q="ielts june"))) >= {lessons["ielts"].id}, "matches group names too"
    assert _ids(_list(db, admin, q="100%_")) == [], "LIKE wildcards are literal"


def test_paging_walks_everything_once(library):
    db = library["db"]
    admin = _user(db, "admin")
    seen, cursor = [], None
    while True:
        page = _list(db, admin, limit=2, cursor=cursor)
        seen += _ids(page)
        cursor = page["next_cursor"]
        if not cursor:
            break
    assert sorted(seen) == sorted(e.id for e in library["lessons"].values())
    assert len(seen) == len(set(seen)), "no card twice"


def test_total_and_facets_only_on_the_first_page(library):
    db = library["db"]
    admin = _user(db, "admin")
    first = _list(db, admin, limit=2)
    assert first["total"] == 5
    assert {g["name"] for g in first["facets"]["groups"]} == {"July 8 SAT - Gulzada", "IELTS June 14 2026"}
    assert [t["id"] for t in first["facets"]["teachers"]] == [library["world"]["teacher"].id]
    later = _list(db, admin, limit=2, cursor=first["next_cursor"])
    assert "total" not in later and "facets" not in later


def test_facets_ignore_the_active_filter(library):
    """Choosing one group must not empty the group menu."""
    db = library["db"]
    admin = _user(db, "admin")
    response = _list(db, admin, group_id=library["sat"].id)
    assert len(response["facets"]["groups"]) == 2


def test_previews_are_signed_for_the_viewer_and_only_when_watchable(library):
    db = library["db"]
    student = library["sat_student"]
    items = {i["event_id"]: i for i in _list(db, _user(db, "admin"))["items"]}
    assert items[library["lessons"]["sat_new"].id]["poster_url"].startswith("/uploads/v/")
    assert items[library["lessons"]["sat_processing"].id]["poster_url"] is None
    mine = _list(db, student)["items"][0]["poster_url"]
    theirs = items[_list(db, student)["items"][0]["event_id"]]["poster_url"]
    assert mine != theirs, "tokens are minted per viewer"


def test_a_removed_video_is_reported_as_removed(library):
    db = library["db"]
    rec = db.query(LessonRecording).filter_by(event_id=library["lessons"]["sat_old"].id).one()
    rec.hls_url = None  # retention retired the video, the row stays for payroll
    db.flush()
    items = {i["event_id"]: i for i in _list(db, _user(db, "admin"))["items"]}
    assert items[rec.event_id]["status"] == "removed"
    assert items[rec.event_id]["poster_url"] is None
    assert rec.event_id not in _ids(_list(db, library["sat_student"])), "students only get watchable ones"


def test_a_forged_cursor_is_a_400(library):
    with pytest.raises(HTTPException) as bad:
        _list(library["db"], _user(library["db"], "admin"), cursor="not-a-cursor")
    assert bad.value.status_code == 400
