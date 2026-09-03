"""Structured per-track targets + progress (Platform Integration Pack §6.4, E5).

Targets: ielts bands (0.5 steps, 4.0–9.0), sat {total, math, verbal}, nuet {total}; the legacy
free-text IELTS target migrates when it parses as a band. Progress reads platform_results only
(latest scored band per module within the last 4 weekly sets, all-time best, trend, IELTS
overall rounding); SAT current level comes from the weekly-set scaled scores payload.
"""

from datetime import datetime

import pytest
from sqlalchemy import ARRAY, create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker

import src.schemas.models  # noqa: F401 - register models
from src.assignments.models import AssignmentZeroSubmission
from src.auth.models import UserInDB
from src.integrations import targets as tg
from src.integrations import targets_progress as tp
from src.integrations.models import PlatformDiagnostic, PlatformResult, PlatformWeeklySet, StudentTarget


@compiles(JSONB, "sqlite")
def _jsonb_as_json(type_, compiler, **kw):
    return "JSON"


@compiles(ARRAY, "sqlite")
def _array_as_json(type_, compiler, **kw):
    return "JSON"


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    for model in (UserInDB, AssignmentZeroSubmission, PlatformResult, PlatformWeeklySet, StudentTarget,
                  PlatformDiagnostic):
        model.__table__.create(bind=engine)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()


def _user(db, email="stu@x.io", role="student"):
    u = UserInDB(email=email, name=email.split("@")[0], role=role, hashed_password="h", is_active=True)
    db.add(u)
    db.commit()
    return u


# --- parsing + validation ------------------------------------------------------------

@pytest.mark.parametrize("text, band", [
    ("7.5", 7.5), ("7,5", 7.5), ("7", 7.0), ("8.0", 8.0), (" band 6.5 ", 6.5), ("7.5+", 7.5), ("9", 9.0), ("4.0", 4.0),
    ("6.25", None), ("10", None), ("3.5", None), ("abc", None), ("", None), (None, None), ("7.5-8.0", None),
])
def test_parse_band(text, band):
    assert tg.parse_band(text) == band


@pytest.mark.parametrize("bands, overall", [
    ([6.5, 6.5, 6.0, 6.5], 6.5),   # 6.375 -> .5
    ([6.0, 6.0, 6.0, 6.5], 6.0),   # 6.125 -> .0
    ([7.0, 7.0, 7.0, 7.5], 7.0),   # 7.125 -> .0
    ([6.5, 7.0, 7.0, 7.0], 7.0),   # 6.875 -> next whole
    ([6.0, 6.0, 6.5, 6.5], 6.5),   # 6.25  -> .5 (half up)
    ([6.5, 6.5, 6.5, 7.0], 6.5),   # 6.625 -> .5
    ([7.0, 7.5, 7.5, 7.5], 7.5),   # 7.375 -> .5
    ([5.5, 6.0, 6.0, 6.0], 6.0),   # 5.875 -> next whole
    ([9.0, 9.0, 9.0, 9.0], 9.0),
])
def test_ielts_overall_rounding(bands, overall):
    assert tg.ielts_overall(bands) == overall


@pytest.mark.parametrize("track, payload", [
    ("ielts", {"overall": 7.5}),
    ("ielts", {"overall": 7.0, "listening": 7.5, "reading": 7.0, "writing": 6.5, "speaking": 7.0}),
    ("sat", {"total": 1400}),
    ("sat", {"total": 1400, "math": 720, "verbal": 680}),
    ("nuet", {"total": 120}),
])
def test_validate_targets_accepts(track, payload):
    assert tg.validate_targets(track, payload) == payload


@pytest.mark.parametrize("track, payload", [
    ("ielts", {"overall": 7.25}), ("ielts", {"overall": 3.5}), ("ielts", {"overall": 9.5}),
    ("ielts", {"total": 7}), ("ielts", {}), ("ielts", {"overall": "seven"}),
    ("sat", {"total": 1405}), ("sat", {"total": 1700}), ("sat", {"math": 850}), ("sat", {"total": 390}),
    ("nuet", {"total": 200}), ("nuet", {"total": -1}), ("toefl", {"total": 100}),
])
def test_validate_targets_rejects(track, payload):
    with pytest.raises(tg.TargetsError) as exc:
        tg.validate_targets(track, payload)
    assert exc.value.status_code == 400


# --- storage ------------------------------------------------------------------------

def test_set_and_get_targets_upsert_per_track(db):
    u = _user(db)
    staff = _user(db, "cur@x.io", "curator")
    tg.set_target(db, u.id, "ielts", {"overall": 7.0}, source="student", set_by=u.id)
    tg.set_target(db, u.id, "sat", {"total": 1400}, source="student", set_by=u.id)
    tg.set_target(db, u.id, "ielts", {"overall": 7.5, "writing": 7.0}, source="staff", set_by=staff.id)

    rows = tg.get_targets(db, u.id)

    assert set(rows) == {"ielts", "sat"}
    assert rows["ielts"]["targets"] == {"overall": 7.5, "writing": 7.0}
    assert rows["ielts"]["source"] == "staff" and rows["ielts"]["set_by"] == staff.id
    assert rows["sat"]["targets"] == {"total": 1400} and rows["sat"]["source"] == "student"
    assert db.query(StudentTarget).count() == 2


def _az(db, user, ielts_target):
    row = AssignmentZeroSubmission(
        user_id=user.id, full_name=user.name, phone_number="1", parent_phone_number="2", telegram_id="t",
        email=user.email, college_board_email="cb", college_board_password="p", city="Almaty",
        school_type="public", group_name="G", sat_target_date="May", recent_practice_test_score="1200",
        bluebook_practice_test_5_score="1250", ielts_target_score=ielts_target,
    )
    db.add(row)
    db.commit()
    return row


def test_migration_parses_bands_and_keeps_the_rest_as_note(db):
    a = _user(db, "a@x.io"); b = _user(db, "b@x.io"); c = _user(db, "c@x.io"); d = _user(db, "d@x.io")
    _az(db, a, "7.5")
    _az(db, b, "7,0")
    _az(db, c, "как можно выше")
    _az(db, d, None)
    tg.set_target(db, a.id, "ielts", {"overall": 8.0}, source="student", set_by=a.id)   # already set: untouched

    out = tg.migrate_assignment_zero_targets(db)

    assert out == {"migrated": 1, "noted": 1, "skipped": 2}
    rows = {r.user_id: r for r in db.query(StudentTarget).all()}
    assert rows[a.id].targets == {"overall": 8.0}                       # existing wins
    assert rows[b.id].targets == {"overall": 7.0} and rows[b.id].source == "assignment_zero"
    assert rows[c.id].targets == {} and rows[c.id].note == "как можно выше"
    assert d.id not in rows
    assert tg.migrate_assignment_zero_targets(db) == {"migrated": 0, "noted": 0, "skipped": 4}   # idempotent


# --- IELTS progress from platform_results --------------------------------------------

NOW = datetime(2026, 9, 3, 10, 0)


def _set(db, set_id, d_from, d_to):
    db.add(PlatformWeeklySet(platform="ielts", weekly_set_id=set_id, title=f"set {set_id}", date_from=d_from,
                             date_to=d_to, is_active=True, track="ielts", modules=[]))
    db.commit()


def _scored(db, user_id, module, set_id, band, scored_at, ref=None):
    db.add(PlatformResult(user_id=user_id, platform="ielts", track="ielts", module=module,
                          attempt_ref=ref or f"{module}-{set_id}", weekly_set_id=set_id, status="scored",
                          band=band, scored_at=scored_at))
    db.commit()


@pytest.fixture()
def sets(db):
    # five weekly sets; the last four (10..13) are the window; 13 is current
    _set(db, 9, datetime(2026, 8, 1), datetime(2026, 8, 8))
    _set(db, 10, datetime(2026, 8, 8), datetime(2026, 8, 15))
    _set(db, 11, datetime(2026, 8, 15), datetime(2026, 8, 22))
    _set(db, 12, datetime(2026, 8, 22), datetime(2026, 8, 29))
    _set(db, 13, datetime(2026, 8, 29, 3, 1), datetime(2026, 9, 12, 13, 1))
    _set(db, 14, datetime(2026, 9, 12, 13, 1), datetime(2026, 9, 19))    # future: not in the window


def test_window_is_the_last_four_started_sets(db, sets):
    assert tp.window_set_ids(db, "ielts", now=NOW) == [13, 12, 11, 10]


def test_ielts_progress_latest_best_trend_and_overall_gaps(db, sets):
    u = _user(db)
    _scored(db, u.id, "listening", 9, 7.5, datetime(2026, 8, 5))     # outside the window: best only
    _scored(db, u.id, "listening", 11, 6.5, datetime(2026, 8, 20))
    _scored(db, u.id, "listening", 13, 7.0, datetime(2026, 9, 2))
    _scored(db, u.id, "reading", 9, 6.5, datetime(2026, 8, 5))       # nothing in the window -> now None
    _scored(db, u.id, "speaking", 12, 6.0, datetime(2026, 8, 27))

    p = tp.ielts_progress(db, u.id, now=NOW)

    assert p["window_set_ids"] == [13, 12, 11, 10]
    L = p["modules"]["listening"]
    assert (L["now"], L["best"], L["previous"], L["trend"], L["set_id"]) == (7.0, 7.5, 6.5, 0.5, 13)
    R = p["modules"]["reading"]
    assert (R["now"], R["best"], R["previous"], R["trend"]) == (None, 6.5, None, None)
    W = p["modules"]["writing"]
    assert (W["now"], W["best"]) == (None, None)
    S = p["modules"]["speaking"]
    assert (S["now"], S["best"], S["trend"]) == (6.0, 6.0, None)
    assert p["overall_now"] is None and p["overall_missing"] == ["reading", "writing"]


def test_ielts_overall_when_all_four_latest_bands_exist(db, sets):
    u = _user(db)
    for module, band in (("listening", 6.5), ("reading", 6.5), ("writing", 6.0), ("speaking", 6.5)):
        _scored(db, u.id, module, 13, band, datetime(2026, 9, 2))
    p = tp.ielts_progress(db, u.id, now=NOW)
    assert p["overall_now"] == 6.5 and p["overall_missing"] == []


def test_latest_wins_within_a_set_and_unscored_rows_are_ignored(db, sets):
    u = _user(db)
    _scored(db, u.id, "writing", 13, 6.0, datetime(2026, 9, 1), ref="w-1")
    _scored(db, u.id, "writing", 13, 6.5, datetime(2026, 9, 2), ref="w-2")
    db.add(PlatformResult(user_id=u.id, platform="ielts", track="ielts", module="reading", attempt_ref="r-x",
                          weekly_set_id=13, status="submitted", band=None))
    db.commit()
    p = tp.ielts_progress(db, u.id, now=NOW)
    assert p["modules"]["writing"]["now"] == 6.5 and p["modules"]["reading"]["now"] is None


def test_progress_for_a_student_without_results(db, sets):
    u = _user(db)
    p = tp.ielts_progress(db, u.id, now=NOW)
    assert all(m["now"] is None and m["best"] is None for m in p["modules"].values())
    assert p["overall_now"] is None and p["overall_missing"] == ["listening", "reading", "writing", "speaking"]


# --- SAT current level from the weekly-set scaled payload ---------------------------

def test_sat_current_from_weekly_set_payload():
    payload = {"results": [{"email": "stu@x.io", "weeklySet": {"id": 41, "name": "Week 5", "weekNumber": 5,
                                                              "examType": "SAT", "verbalScaled": 680,
                                                              "mathScaled": 720, "total": 1400, "completed": True,
                                                              "completedAt": "2026-08-30T10:00:00Z"}}]}
    cur = tp.sat_current_from_payload(payload, "stu@x.io")
    assert cur == {"total": 1400, "math": 720, "verbal": 680, "week": 5, "set_name": "Week 5",
                   "completed_at": "2026-08-30T10:00:00Z", "source": "weekly_set"}


def test_sat_current_missing_or_incomplete_returns_none():
    assert tp.sat_current_from_payload({"results": []}, "stu@x.io") is None
    payload = {"results": [{"email": "stu@x.io", "weeklySet": {"id": 41, "completed": False, "total": None}}]}
    assert tp.sat_current_from_payload(payload, "stu@x.io") is None


def test_gap_and_status_lines():
    assert tp.gap(7.5, 6.5) == 1.0 and tp.gap(1400, 1450) == -50 and tp.gap(7.0, None) is None
    assert tp.reached(7.0, 7.0) is True and tp.reached(7.0, 6.5) is False and tp.reached(None, 7.0) is False
