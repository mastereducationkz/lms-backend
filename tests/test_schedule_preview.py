"""«Предпросмотр» of the schedule generator shows exactly what «Generate» then does — LMS mirror.

Mirrors ``crm-master/backend/tests/test_schedule_apply_and_preview.py`` for the LMS: the route
``POST /leaderboard/curator/schedule/preview`` takes the generate body, builds the config the
save would store (lengths inherited from the stored pattern included) and answers with the
lessons the save would keep, move, resize, create and switch off — writing nothing.

``now`` is pinned to this week's Monday 00:00 Almaty for both routes, so «last week's lessons
are taught, this week's are still to come» holds whichever weekday the suite runs on.
"""
from datetime import datetime, time, timedelta, timezone

import pytest

from src.schemas.models import Event, EventGroup, Group, LessonRequest, UserInDB

UTC = timezone.utc
KZ = timezone(timedelta(hours=5))
MON, WED, FRI, SAT, SUN = 0, 2, 4, 5, 6


@pytest.fixture
def db():
    from sqlalchemy import event
    from sqlalchemy.exc import OperationalError
    from sqlalchemy.orm import Session as SASession
    from src.config import engine

    try:
        connection = engine.connect()
    except OperationalError:
        pytest.skip("No database available")
    trans = connection.begin()
    session = SASession(bind=connection)
    session.begin_nested()

    @event.listens_for(session, "after_transaction_end")
    def _restart(sess, transaction):
        if transaction.nested and not transaction._parent.nested:
            sess.begin_nested()

    try:
        yield session
    finally:
        event.remove(session, "after_transaction_end", _restart)
        session.close()
        trans.rollback()
        connection.close()


def _this_monday():
    today = datetime.now(KZ).date()
    return today - timedelta(days=today.weekday())


def _user(db, role):
    user = UserInDB(email=f"preview-{role}-{datetime.utcnow().timestamp()}@test.local",
                    name=role.title(), role=role, hashed_password="x", is_active=True)
    db.add(user); db.flush()
    return user


@pytest.fixture
def world(db, monkeypatch):
    """Four lessons taught last week; the old pattern Mon/Wed/Fri 18:00 x 60 + Sat 19:00 x 90
    has three more weeks ahead, and next week's Friday was cancelled with approval."""
    import src.gamification.routes.leaderboard as leaderboard

    admin, teacher = _user(db, "admin"), _user(db, "teacher")
    monday = _this_monday()
    old_cfg = {
        "start_date": (monday - timedelta(days=7)).isoformat(), "weeks_count": 6, "lessons_count": 16,
        "schedule_items": [{"day_of_week": d, "time_of_day": "18:00", "duration_minutes": 60}
                           for d in (MON, WED, FRI)]
        + [{"day_of_week": SAT, "time_of_day": "19:00", "duration_minutes": 90}],
    }
    group = Group(name=f"Preview G {datetime.utcnow().timestamp()}", teacher_id=teacher.id,
                  is_active=True, schedule_config=old_cfg)
    db.add(group); db.flush()
    base = datetime.combine(monday - timedelta(days=7), time.min, KZ)

    def lesson(week, day, hour, minutes, active=True, seconds=0):
        naive = (base + timedelta(weeks=week, days=day, hours=hour, seconds=seconds)).astimezone(UTC).replace(tzinfo=None)
        ev = Event(title=f"{group.name}: Lesson", event_type="class", start_datetime=naive,
                   end_datetime=naive + timedelta(minutes=minutes), is_active=active,
                   created_by=teacher.id, teacher_id=teacher.id)
        db.add(ev); db.flush(); db.add(EventGroup(event_id=ev.id, group_id=group.id)); db.flush()
        return ev

    for week in (0, 1, 2, 3):  # week 0 is last week: taught
        for day in (MON, WED, FRI):
            lesson(week, day, 18, 60)
        lesson(week, SAT, 19, 90)
    cancelled = next(e for e in _class_events(db, group) if e.start_datetime ==
                     (base + timedelta(weeks=2, days=FRI, hours=18)).astimezone(UTC).replace(tzinfo=None))
    cancelled.is_active = False
    db.add(LessonRequest(request_type="cancel", status="approved", event_id=cancelled.id,
                         group_id=group.id, requester_id=teacher.id,
                         original_datetime=cancelled.start_datetime))
    db.commit()

    now = datetime.combine(monday, time.min, KZ).astimezone(UTC)
    monkeypatch.setattr(leaderboard, "_schedule_now", lambda: now)
    return {"db": db, "admin": admin, "teacher": teacher, "group": group, "old": old_cfg,
            "monday": monday, "base": base, "now": now, "cancelled": cancelled, "lesson": lesson}


def _class_events(db, group):
    return sorted(
        db.query(Event).join(EventGroup, EventGroup.event_id == Event.id)
        .filter(EventGroup.group_id == group.id, Event.event_type == "class").all(),
        key=lambda e: (e.start_datetime, e.id),
    )


def _active_future(world):
    naive_now = world["now"].replace(tzinfo=None)
    return [e for e in _class_events(world["db"], world["group"])
            if e.is_active and e.start_datetime >= naive_now]


def _iso(naive):
    return naive.replace(tzinfo=UTC).isoformat()


def _minutes(ev):
    return int((ev.end_datetime - ev.start_datetime).total_seconds() // 60)


def _client(world, user):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from src.config import get_db
    from src.gamification.routes.leaderboard import router
    from src.routes.auth import get_current_user_dependency

    app = FastAPI()
    app.include_router(router, prefix="/leaderboard")
    app.dependency_overrides[get_db] = lambda: world["db"]
    app.dependency_overrides[get_current_user_dependency] = lambda: user
    return TestClient(app)


def _body(world, **overrides):
    """Rauan's shape: Mon/Fri 18:00–19:00, Sat/Sun 19:00–20:30. Saturday sends no length — an
    old cached client — and must inherit the stored 90 minutes, in the preview as in the save."""
    body = {
        "group_id": world["group"].id,
        "start_date": world["old"]["start_date"],
        "lessons_count": 12,  # 4 taught + 8: three of the eleven lessons ahead are switched off
        "schedule_items": [
            {"day_of_week": MON, "time_of_day": "18:00", "duration_minutes": 60},
            {"day_of_week": FRI, "time_of_day": "18:00", "duration_minutes": 60},
            {"day_of_week": SAT, "time_of_day": "19:00"},
            {"day_of_week": SUN, "time_of_day": "19:00", "duration_minutes": 90},
        ],
    }
    body.update(overrides)
    return body


def _preview(world, config, previous=None, now=None):
    from src.services.schedule_preview import preview_group_schedule

    return preview_group_schedule(
        world["db"], world["group"].id, config,
        previous_config=world["old"] if previous is None else previous,
        fallback_start=None, now=world["now"] if now is None else now,
    )


# ── the preview is the save ──────────────────────────────────────────────────────────────


def test_the_preview_route_answers_what_generate_then_does(world):
    db, api = world["db"], _client(world, world["admin"])
    naive_cancelled = world["cancelled"].start_datetime

    response = api.post("/leaderboard/curator/schedule/preview", json=_body(world))

    assert response.status_code == 200, response.text
    preview = response.json()
    assert (preview["started_lessons"], preview["started_minutes"]) == (4, 3 * 60 + 90)
    assert preview["planned_lessons"] == 8 and preview["total_lessons"] == 12
    # Six lessons already sit on a new slot; this and next week's Wednesdays move onto Sundays.
    assert sorted(lesson["change"] for lesson in preview["lessons"]) == ["keep"] * 6 + ["move"] * 2
    assert preview["planned_minutes"] == 4 * 60 + 4 * 90
    assert preview["total_minutes"] == preview["started_minutes"] + preview["planned_minutes"]
    assert preview["first_start"] == preview["lessons"][0]["start"]
    assert preview["last_end"] == preview["lessons"][-1]["end"]
    assert all(lesson["start"].endswith("+00:00") for lesson in preview["lessons"])
    saturdays = [lesson for lesson in preview["lessons"]
                 if datetime.fromisoformat(lesson["start"]).astimezone(KZ).weekday() == SAT]
    assert saturdays and all(lesson["minutes"] == 90 for lesson in saturdays)  # inherited
    assert _iso(naive_cancelled) not in {lesson["start"] for lesson in preview["lessons"]}
    assert len(preview["deactivated"]) == 3
    assert preview["warnings"] == []

    active_before = {e.id: e for e in _class_events(db, world["group"]) if e.is_active}
    spans_before = {i: (_iso(e.start_datetime), _iso(e.end_datetime)) for i, e in active_before.items()}
    saved_response = api.post("/leaderboard/curator/schedule/generate", json=_body(world))
    assert saved_response.status_code == 200, saved_response.text

    db.expire_all()
    saved = _active_future(world)
    assert [(e.id, _iso(e.start_datetime), _minutes(e)) for e in saved] == [
        (lesson["event_id"], lesson["start"], lesson["minutes"]) for lesson in preview["lessons"]
    ]
    assert [_iso(e.end_datetime) for e in saved] == [lesson["end"] for lesson in preview["lessons"]]
    switched_off = set(active_before) - {e.id for e in _class_events(db, world["group"]) if e.is_active}
    assert {gone["event_id"] for gone in preview["deactivated"]} == switched_off
    for gone in preview["deactivated"]:
        assert (gone["start"], gone["end"]) == spans_before[gone["event_id"]]
    assert db.get(Event, world["cancelled"].id).is_active is False
    for lesson in preview["lessons"]:
        if lesson["change"] == "move":
            assert (lesson["previous_start"], lesson["previous_end"]) == spans_before[lesson["event_id"]]


def test_a_changed_length_and_a_new_lesson_are_what_the_save_writes(world):
    """Sunday 120 minutes and a longer course: the preview names the resize and the creations."""
    db, api = world["db"], _client(world, world["admin"])
    body = _body(world, lessons_count=16)
    body["schedule_items"][0] = {"day_of_week": MON, "time_of_day": "18:00", "duration_minutes": 120}

    preview = api.post("/leaderboard/curator/schedule/preview", json=body).json()
    kinds = [lesson["change"] for lesson in preview["lessons"]]
    assert "resize" in kinds and "create" in kinds and preview["deactivated"] == []

    assert api.post("/leaderboard/curator/schedule/generate", json=body).status_code == 200
    db.expire_all()
    saved = _active_future(world)
    created = [lesson for lesson in preview["lessons"] if lesson["change"] == "create"]
    assert all(lesson["event_id"] is None for lesson in created)
    assert [(_iso(e.start_datetime), _minutes(e)) for e in saved] == [
        (lesson["start"], lesson["minutes"]) for lesson in preview["lessons"]
    ]
    kept_ids = [lesson["event_id"] for lesson in preview["lessons"] if lesson["event_id"] is not None]
    assert [e.id for e in saved if e.id in set(kept_ids)] == kept_ids
    assert len(saved) == len(preview["lessons"]) == 16 - 4


def test_a_hand_shortened_lesson_keeps_its_length_in_the_preview_as_in_the_save(world):
    db = world["db"]
    saturday = next(e for e in _active_future(world)
                    if e.start_datetime.replace(tzinfo=UTC).astimezone(KZ).weekday() == SAT)
    saturday.end_datetime = saturday.start_datetime + timedelta(minutes=60)
    db.commit()

    preview = _preview(world, world["old"])

    row = next(lesson for lesson in preview["lessons"] if lesson["event_id"] == saturday.id)
    assert (row["change"], row["minutes"]) == ("keep", 60)


def test_preview_group_schedule_neither_flushes_nor_commits_nor_leaves_a_change(world):
    """The route no longer rolls back: a flush or commit inside the preview would put its moves
    into the request's transaction, and a change left pending would ride on the next flush."""
    from sqlalchemy import event

    db, group = world["db"], world["group"]

    def snapshot():
        return [(e.id, e.start_datetime, e.end_datetime, e.is_active, e.title)
                for e in _class_events(db, group)]

    before = snapshot()
    shorter = {**world["old"], "lessons_count": 6}                       # switches lessons off
    rauan = {**world["old"], "lessons_count": 20, "schedule_items": [    # moves, resizes, creates
        {"day_of_week": MON, "time_of_day": "18:00", "duration_minutes": 120},
        {"day_of_week": FRI, "time_of_day": "18:00", "duration_minutes": 60},
        {"day_of_week": SAT, "time_of_day": "19:00", "duration_minutes": 90},
        {"day_of_week": SUN, "time_of_day": "19:00", "duration_minutes": 90},
    ]}
    writes: list[str] = []
    listeners = {
        "before_flush": lambda *_args: writes.append("flush"),
        "after_commit": lambda *_args: writes.append("commit"),
    }
    for name, listener in listeners.items():
        event.listen(db, name, listener)
    try:
        previews = [_preview(world, shorter), _preview(world, rauan)]
    finally:
        for name, listener in listeners.items():
            event.remove(db, name, listener)

    assert previews[0]["deactivated"] and {"move", "resize", "create"} <= {
        lesson["change"] for lesson in previews[1]["lessons"]
    }
    assert writes == []
    assert not (db.new or db.dirty or db.deleted)
    db.expire_all()
    assert snapshot() == before
    assert db.get(Group, group.id).schedule_config == world["old"]


def test_the_preview_route_writes_nothing(world):
    db, group, api = world["db"], world["group"], _client(world, world["admin"])

    def snapshot():
        db.expire_all()
        return [(e.id, e.start_datetime, e.end_datetime, e.is_active, e.title)
                for e in _class_events(db, group)]

    before = snapshot()
    body = _body(world, lessons_count=20)
    body["schedule_items"][2] = {"day_of_week": SAT, "time_of_day": "19:00", "duration_minutes": 120}

    response = api.post("/leaderboard/curator/schedule/preview", json=body)

    assert response.status_code == 200, response.text
    assert snapshot() == before
    assert db.get(Group, group.id).schedule_config == world["old"]


def test_a_student_cannot_preview(world):
    student = _user(world["db"], "student")
    world["db"].commit()

    response = _client(world, student).post("/leaderboard/curator/schedule/preview", json=_body(world))

    assert response.status_code == 403


def test_an_unknown_group_is_a_404(world):
    response = _client(world, world["admin"]).post(
        "/leaderboard/curator/schedule/preview", json=_body(world, group_id=999_999_999),
    )

    assert response.status_code == 404


def test_an_invalid_preview_request_is_a_422(world):
    body = _body(world, schedule_items=[{"day_of_week": MON, "time_of_day": "25:00"}])

    response = _client(world, world["admin"]).post("/leaderboard/curator/schedule/preview", json=body)

    assert response.status_code == 422


# ── warnings ─────────────────────────────────────────────────────────────────────────────


COUNT_WARNING = "Кол-во уроков ({n}) не больше, чем уже прошло (4) — новых уроков не будет"
START_WARNING = "Дата начала позже сегодняшней"
DUPLICATES_WARNING = "Есть дубли уроков"


@pytest.mark.parametrize("n", [3, 4])
def test_the_count_warning_fires_when_nothing_is_left_to_plan(world, n):
    preview = _preview(world, {**world["old"], "lessons_count": n})

    assert COUNT_WARNING.format(n=n) in preview["warnings"]
    assert preview["planned_lessons"] == 0 and preview["total_lessons"] == 4
    assert preview["first_start"] is None and preview["last_end"] is None
    assert len(preview["deactivated"]) == 11


def test_the_count_warning_is_silent_while_lessons_remain(world):
    warnings = _preview(world, {**world["old"], "lessons_count": 5})["warnings"]

    assert not any(w.startswith("Кол-во уроков") for w in warnings)


def test_a_future_start_date_with_lessons_taught_warns_in_the_crm_words(world):
    cfg = {**world["old"], "start_date": (world["monday"] + timedelta(days=7)).isoformat()}

    warnings = _preview(world, cfg)["warnings"]

    assert (
        "Дата начала позже сегодняшней, но у группы уже прошло 4 урока — они засчитаны "
        "в «Кол-во уроков»; для нового цикла создайте новую группу или увеличьте количество."
    ) in warnings


def test_a_start_date_of_today_says_nothing_about_the_start(world):
    cfg = {**world["old"], "start_date": world["monday"].isoformat()}

    assert not any(w.startswith(START_WARNING) for w in _preview(world, cfg)["warnings"])


def test_a_future_start_date_before_any_lesson_says_nothing(world):
    cfg = {**world["old"], "start_date": (world["monday"] + timedelta(days=7)).isoformat()}

    preview = _preview(world, cfg, now=world["base"].astimezone(UTC))  # last Monday 00:00

    assert preview["started_lessons"] == 0
    assert not any(w.startswith(START_WARNING) for w in preview["warnings"])


def test_duplicate_lessons_are_counted_and_said_not_to_be_merged(world):
    """The LMS save does not merge duplicates, so the preview counts them as the save will."""
    world["lesson"](2, MON, 18, 60, seconds=30)            # same minute as next Monday's lesson
    world["lesson"](3, WED, 18, 60, active=False)           # a switched-off twin is no duplicate
    world["db"].commit()

    warnings = _preview(world, world["old"])["warnings"]

    assert [w for w in warnings if w.startswith(DUPLICATES_WARNING)] == [
        "Есть дубли уроков (1) — сохранение их не объединит, итог посчитан с ними."
    ]


def test_no_duplicates_no_duplicate_warning(world):
    assert not any(w.startswith(DUPLICATES_WARNING) for w in _preview(world, world["old"])["warnings"])
