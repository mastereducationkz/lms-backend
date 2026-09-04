"""Platform-test assignments (Platform Integration Pack §6.3, E1): one regular Assignment per
(weekly set, IELTS-track group), synced from weekly_set events and the nightly job.
SQLite + JSONB shim, same style as the other platform tests."""

import json
from datetime import datetime

import pytest
from sqlalchemy import ARRAY, create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker

import src.schemas.models  # noqa: F401 - register models
from src.assignments.models import Assignment, AssignmentSubmission
from src.auth.models import UserInDB
from src.courses.models import Group, GroupStudent
from src.integrations import platform_assignments as pa
from src.integrations.ingest import ingest_batch
from src.integrations.models import (
    PlatformEvent, PlatformResult, PlatformTestAssignment, PlatformWeeklySet,
)


@compiles(JSONB, "sqlite")
def _jsonb_as_json(type_, compiler, **kw):
    return "JSON"


@compiles(ARRAY, "sqlite")
def _array_as_json(type_, compiler, **kw):
    # assignments.allowed_file_types is a Postgres ARRAY; unused here.
    return "JSON"


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    for model in (UserInDB, Group, GroupStudent, Assignment, AssignmentSubmission,
                  PlatformEvent, PlatformResult, PlatformWeeklySet, PlatformTestAssignment):
        model.__table__.create(bind=engine)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture(autouse=True)
def _flag_on(monkeypatch):
    monkeypatch.setenv("PLATFORM_ASSIGNMENTS_ENABLED", "true")


NOW = datetime(2026, 9, 3, 10, 0)  # UTC, inside set 13's window
MODULES = [
    {"module": "listening", "test_id": 41, "test_title": "L 41"},
    {"module": "reading", "test_id": 42, "test_title": "R 42"},
    {"module": "writing", "test_id": 7, "test_title": "W 7"},
    {"module": "speaking", "test_id": 9, "test_title": "S 9"},
]


def _set(db, set_id=13, title="29.08-30.08", d_from=datetime(2026, 8, 29, 3, 1), d_to=datetime(2026, 9, 12, 13, 1),
         active=True, modules=None):
    ws = PlatformWeeklySet(platform="ielts", weekly_set_id=set_id, title=title, date_from=d_from, date_to=d_to,
                           is_active=active, track="ielts", modules=MODULES if modules is None else modules)
    db.add(ws)
    db.commit()
    return ws


def _group(db, name="IELTS-A", program="ielts", active=True, special=False, opt_out=False):
    g = Group(name=name, program_type=program, is_active=active, is_special=special, platform_tests_opt_out=opt_out)
    db.add(g)
    db.commit()
    return g


# --- Task 1: storage ---------------------------------------------------------------

def test_link_row_is_unique_per_set_and_group(db):
    g = _group(db)
    a = Assignment(title="t", assignment_type="platform_test", content="{}", group_id=g.id)
    db.add(a)
    db.flush()
    db.add(PlatformTestAssignment(assignment_id=a.id, platform="ielts", weekly_set_id=13, group_id=g.id))
    db.commit()
    assert g.platform_tests_opt_out is False
    b = Assignment(title="t2", assignment_type="platform_test", content="{}", group_id=g.id)
    db.add(b)
    db.flush()
    db.add(PlatformTestAssignment(assignment_id=b.id, platform="ielts", weekly_set_id=13, group_id=g.id))
    with pytest.raises(IntegrityError):
        db.commit()


def test_weekly_set_window_is_a_timestamp(db):
    ws = _set(db)
    assert ws.date_to == datetime(2026, 9, 12, 13, 1)


# --- Task 2: sync ------------------------------------------------------------------

def test_module_paths():
    assert pa.module_path("listening", 41, 13) == "/exam/test/41"
    assert pa.module_path("reading", 42, 13) == "/exam/test/42"
    assert pa.module_path("writing", 7, 13) == "/weekly-sets/13"
    assert pa.module_path("speaking", 9, 13) == "/speaking-ai/setup/9"


def test_target_groups_are_active_non_special_ielts_not_opted_out(db):
    _group(db, "keep")
    _group(db, "sat", program="sat")
    _group(db, "inactive", active=False)
    _group(db, "special", special=True)
    _group(db, "opted", opt_out=True)
    assert [g.name for g in pa.target_groups(db)] == ["keep"]


def test_publish_creates_one_assignment_per_group(db):
    g1 = _group(db, "G1")
    g2 = _group(db, "G2")
    ws = _set(db)

    out = pa.sync_weekly_set(db, ws, now=NOW)

    assert out == {"created": 2, "updated": 0, "deactivated": 0}
    rows = db.query(Assignment).order_by(Assignment.group_id).all()
    assert [r.group_id for r in rows] == [g1.id, g2.id]
    a = rows[0]
    assert a.assignment_type == "platform_test" and a.is_active is True and not a.is_hidden
    assert a.title == "IELTS Weekly Test · 29.08-30.08"
    assert a.due_date == datetime(2026, 9, 12, 13, 1)          # date_to exactly, naive UTC
    content = json.loads(a.content)
    assert content["platform"] == "ielts" and content["weekly_set_id"] == 13
    assert content["date_from"] == "2026-08-29T03:01:00+00:00" and content["date_to"] == "2026-09-12T13:01:00+00:00"
    assert [m["module"] for m in content["modules"]] == ["listening", "reading", "writing", "speaking"]
    assert content["modules"][0]["path"] == "/exam/test/41"
    assert content["modules"][2]["path"] == "/weekly-sets/13"
    assert content["modules"][3]["path"] == "/speaking-ai/setup/9"
    assert db.query(PlatformTestAssignment).count() == 2


def test_sync_is_idempotent_and_update_recomputes_due_and_modules(db):
    _group(db)
    ws = _set(db)
    pa.sync_weekly_set(db, ws, now=NOW)
    ws.date_to = datetime(2026, 9, 19, 13, 1)
    ws.title = "29.08-30.08 (ext)"
    ws.modules = MODULES[:2]
    db.commit()

    out = pa.sync_weekly_set(db, ws, now=NOW)

    assert out == {"created": 0, "updated": 1, "deactivated": 0}
    a = db.query(Assignment).one()
    assert a.due_date == datetime(2026, 9, 19, 13, 1) and a.title.endswith("(ext)")
    assert len(json.loads(a.content)["modules"]) == 2


def test_unpublished_deactivates_without_deleting(db):
    _group(db)
    ws = _set(db)
    pa.sync_weekly_set(db, ws, now=NOW)
    ws.is_active = False
    db.commit()

    out = pa.sync_weekly_set(db, ws, now=NOW)

    assert out["deactivated"] == 1
    a = db.query(Assignment).one()
    assert a.is_active is False and db.query(PlatformTestAssignment).count() == 1


def test_republished_set_reactivates_the_same_row(db):
    _group(db)
    ws = _set(db)
    pa.sync_weekly_set(db, ws, now=NOW)
    ws.is_active = False
    db.commit()
    pa.sync_weekly_set(db, ws, now=NOW)
    ws.is_active = True
    db.commit()

    out = pa.sync_weekly_set(db, ws, now=NOW)

    assert out == {"created": 0, "updated": 1, "deactivated": 0}
    assert db.query(Assignment).count() == 1 and db.query(Assignment).one().is_active is True


def test_past_set_is_recomputed_but_never_created(db):
    _group(db, "early")
    ws = _set(db, set_id=12, title="22.08-23.08", d_from=datetime(2026, 8, 22, 3, 1), d_to=datetime(2026, 8, 29, 13, 1))
    assert pa.sync_weekly_set(db, ws, now=NOW) == {"created": 0, "updated": 0, "deactivated": 0}
    assert db.query(Assignment).count() == 0
    assert pa.sync_weekly_set(db, ws, now=NOW, include_past=True)["created"] == 1
    _group(db, "late")
    ws.title = "22.08-23.08 (edited)"
    db.commit()
    out = pa.sync_weekly_set(db, ws, now=NOW)            # admins edit titles of past sets
    assert out == {"created": 0, "updated": 1, "deactivated": 0}
    assert db.query(Assignment).one().title.endswith("(edited)")


def test_group_that_stops_qualifying_is_deactivated_and_reactivated(db):
    g = _group(db)
    ws = _set(db)
    pa.sync_weekly_set(db, ws, now=NOW)
    pa.set_group_opt_out(db, g, True, now=NOW)
    assert db.query(Assignment).one().is_active is False
    pa.set_group_opt_out(db, g, False, now=NOW)
    assert db.query(Assignment).one().is_active is True


def test_flag_off_makes_sync_a_noop(db, monkeypatch):
    monkeypatch.setenv("PLATFORM_ASSIGNMENTS_ENABLED", "false")
    _group(db)
    ws = _set(db)
    assert pa.sync_weekly_set(db, ws, now=NOW) == {"created": 0, "updated": 0, "deactivated": 0}
    assert db.query(Assignment).count() == 0


def test_sync_all_active_covers_only_current_sets_by_default(db):
    _group(db)
    _set(db, set_id=12, title="22.08-23.08", d_from=datetime(2026, 8, 22, 3, 1), d_to=datetime(2026, 8, 29, 13, 1))
    _set(db)
    out = pa.sync_all_active(db, now=NOW)
    assert out["sets"] == 2 and out["created"] == 1 and db.query(Assignment).count() == 1
    out = pa.sync_all_active(db, now=NOW, include_past=True)
    assert out["created"] == 1 and db.query(Assignment).count() == 2


# --- Task 3: hooks -----------------------------------------------------------------

def _set_event(event_type="weekly_set.published", set_id=13, event_id=None, **data_over):
    data = {"weekly_set_id": set_id, "title": "29.08-30.08", "date_from": "2026-08-29T03:01:00+00:00",
            "date_to": "2026-09-12T13:01:00+00:00", "is_active": True, "track": "ielts", "modules": MODULES}
    data.update(data_over)
    return {"event_id": event_id or f"00000000-0000-4000-8000-0000000000{set_id:02d}", "event_type": event_type,
            "platform": "ielts", "schema_version": 1, "occurred_at": "2026-09-03T10:00:00Z",
            "student": None, "data": data}


def test_weekly_set_published_event_creates_assignments(db, monkeypatch):
    monkeypatch.setattr(pa, "_utcnow", lambda: NOW)
    _group(db)
    ingest_batch(db, "ielts", [_set_event()])
    a = db.query(Assignment).one()
    assert a.assignment_type == "platform_test" and a.due_date == datetime(2026, 9, 12, 13, 1)
    assert db.query(PlatformWeeklySet).one().date_from == datetime(2026, 8, 29, 3, 1)


def test_weekly_set_unpublished_event_deactivates(db, monkeypatch):
    monkeypatch.setattr(pa, "_utcnow", lambda: NOW)
    _group(db)
    ingest_batch(db, "ielts", [_set_event()])
    ingest_batch(db, "ielts", [_set_event("weekly_set.unpublished", event_id="00000000-0000-4000-8000-000000000099",
                                          is_active=False)])
    assert db.query(Assignment).one().is_active is False


def test_nightly_sync_creates_for_groups_added_later(db, monkeypatch):
    from src.integrations import reconcile
    monkeypatch.setattr(pa, "_utcnow", lambda: NOW)
    ws = _set(db)
    pa.sync_weekly_set(db, ws, now=NOW)          # no groups yet
    _group(db, "late")
    out = reconcile.sync_platform_assignments(db)
    assert out["created"] == 1 and db.query(Assignment).one().group_id


# --- SAT/NUET parity: track-aware targeting, platform-provided paths, labels -------------

def test_sat_set_targets_groups_of_its_track_only(db):
    sat = _group(db, "SAT-A", program="sat")
    _group(db, "NUET-A", program="nuet")
    _group(db, "IELTS-A", program="ielts")
    ws = PlatformWeeklySet(platform="sat", weekly_set_id=59, title="29.08-30.08", date_from=datetime(2026, 8, 29, 0, 0),
                           date_to=datetime(2026, 8, 30, 23, 59), is_active=True, track="sat",
                           modules=[{"module": "math", "test_id": 501, "test_title": "Weekly Test [Math] (29.08-30.08)",
                                     "path": "/tests/501/start"},
                                    {"module": "verbal", "test_id": 502, "test_title": "Weekly Test [Verbal] (29.08-30.08)",
                                     "path": "/tests/502/start"}],
                           set_path="/weekly-sets/59")
    db.add(ws)
    db.commit()

    out = pa.sync_weekly_set(db, ws, now=datetime(2026, 8, 29, 10, 0))

    assert out == {"created": 1, "updated": 0, "deactivated": 0}
    a = db.query(Assignment).one()
    assert a.group_id == sat.id and a.title == "SAT Weekly Test · 29.08-30.08"
    content = json.loads(a.content)
    assert content["track"] == "sat" and content["set_path"] == "/weekly-sets/59"
    assert [m["path"] for m in content["modules"]] == ["/tests/501/start", "/tests/502/start"]


def test_nuet_set_on_the_sat_platform_targets_nuet_groups(db):
    _group(db, "SAT-A", program="sat")
    nuet = _group(db, "NUET-A", program="nuet")
    ws = PlatformWeeklySet(platform="sat", weekly_set_id=60, title="Week 7", date_from=datetime(2026, 8, 29),
                           date_to=datetime(2026, 8, 30, 23, 59), is_active=True, track="nuet",
                           modules=[{"module": "nuet", "test_id": 700, "test_title": "NUET Week 7", "path": "/tests/700/start"}])
    db.add(ws)
    db.commit()
    pa.sync_weekly_set(db, ws, now=datetime(2026, 8, 29, 10, 0))
    a = db.query(Assignment).one()
    assert a.group_id == nuet.id and a.title == "NUET Weekly Test · Week 7"
    assert pa.target_groups(db, "nuet")[0].id == nuet.id


def test_platform_paths_default_to_ielts_rules_only_for_ielts(db):
    _group(db, "SAT-A", program="sat")
    ws = PlatformWeeklySet(platform="sat", weekly_set_id=61, title="x", date_from=datetime(2026, 8, 29),
                           date_to=datetime(2026, 8, 30), is_active=True, track="sat",
                           modules=[{"module": "math", "test_id": 1, "test_title": "M"}])   # no path given
    db.add(ws)
    db.commit()
    content = pa.build_content(ws)
    assert content["modules"][0]["path"] is None and content["set_path"] is None
    assert pa.module_path("listening", 41, 13, platform="ielts") == "/exam/test/41"
    assert pa.module_path("math", 1, 61, platform="sat") is None


# --- sets without a window (NUET "Week N" curriculum sets opened per group on the platform) -------

def test_set_without_a_window_never_creates_assignments(db):
    _group(db, "NUET-A", program="nuet")
    ws = _set(db, set_id=50, title="Weekly Set [NUET] (Week 2)", d_from=None, d_to=None)
    ws.track = "nuet"
    db.commit()
    assert pa.sync_weekly_set(db, ws, now=NOW) == {"created": 0, "updated": 0, "deactivated": 0}
    assert pa.sync_weekly_set(db, ws, now=NOW, include_past=True)["created"] == 0
    assert db.query(Assignment).count() == 0


def test_existing_assignment_of_a_set_that_lost_its_window_is_deactivated(db):
    _group(db, "NUET-A", program="nuet")
    ws = _set(db, set_id=50, title="Weekly Set [NUET] (Week 2)")
    ws.track = "nuet"
    db.commit()
    assert pa.sync_weekly_set(db, ws, now=NOW)["created"] == 1
    ws.date_from = ws.date_to = None
    db.commit()
    assert pa.sync_weekly_set(db, ws, now=NOW) == {"created": 0, "updated": 0, "deactivated": 1}
    assert db.query(Assignment).one().is_active is False
