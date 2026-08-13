"""Freezing one enrollment, not one student.

The bug this feature exists to kill: a student froze SAT, kept attending IELTS twice a week,
and the LMS suppressed the IELTS attendance too — because the mirror only knew "this person
is frozen". Every test here is a way that could come back.

The second theme is convergence. The CRM delivers at-least-once and promises no order, and it
now delivers *per scope*, so the revision rule has to be per scope as well: a stale SAT
message must not be able to discard a fresh IELTS one.

The D−3 return reminder is deliberately absent here, and absent from this service: the CRM
owns it (``src/health/notifications.py``), and a second one on this side would put two
notifications in the same curator's list for the same return.
"""
from datetime import date, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.config import get_db
from src.curator.freeze_mirror import (
    GROUP_WIDE,
    StudentFreezeState,
    freeze_index,
    freeze_states,
    upsert_freeze_state,
)
from src.curator.health_facts import health_facts
from src.routes.crm_curator_internal import router as curator_router
from src.schemas.models import GroupStudent
from tests.scoped_freeze_fixtures import (  # noqa: F401 - fixtures used by name
    db,
    freeze_payload,
    lesson,
    world,
)

SERVICE_KEY = "test-crm-service-key"


@pytest.fixture
def app(db, monkeypatch):  # noqa: F811
    monkeypatch.setenv("CRM_INTERNAL_SERVICE_KEY", SERVICE_KEY)
    application = FastAPI()
    application.include_router(curator_router, prefix="/internal/crm/curator")
    application.dependency_overrides[get_db] = lambda: db
    return application


@pytest.fixture
def client(app):
    return TestClient(app)


def _hdr():
    return {"X-CRM-Service-Key": SERVICE_KEY, "X-Crm-Staff-Role": "admin"}


def _facts(world, frozen_windows):  # noqa: F811
    payload = health_facts(
        world["db"], [world["student"].id], frozen_windows=frozen_windows
    )
    return payload["students"][str(world["student"].id)]


def _drop_memberships(world):  # noqa: F811
    """What the CRM does to the LMS when it freezes every one of a student's enrollments."""
    db = world["db"]
    db.query(GroupStudent).filter(
        GroupStudent.student_id == world["student"].id
    ).delete(synchronize_session=False)
    db.commit()


# --- the scoped denominator -----------------------------------------------------------------


def test_a_sat_freeze_leaves_the_ielts_denominator_alone(world):  # noqa: F811
    """The whole feature. The student stopped SAT and kept coming to IELTS."""
    sat, ielts = world["groups"]["SAT"], world["groups"]["IELTS"]
    lesson(world, sat, days_ago=3, status="absent")
    lesson(world, ielts, days_ago=3, status="present")

    facts = _facts(
        world,
        {
            world["student"].id: {
                sat.id: [((date.today() - timedelta(days=6)).isoformat(), None)]
            }
        },
    )

    assert facts["groups"][str(sat.id)]["marked_lessons"] == 0, "SAT lesson suppressed"
    ielts_facts = facts["groups"][str(ielts.id)]
    assert ielts_facts["marked_lessons"] == 1, "IELTS lesson must survive a SAT freeze"
    assert ielts_facts["attendance_rate"] == 100.0


def test_a_legacy_student_wide_window_still_freezes_every_group(world):  # noqa: F811
    """The flat shape is what an older CRM build sends, and it has always meant «everything»."""
    sat, ielts = world["groups"]["SAT"], world["groups"]["IELTS"]
    lesson(world, sat, days_ago=3, status="absent")
    lesson(world, ielts, days_ago=3, status="absent")

    facts = _facts(
        world,
        {world["student"].id: [((date.today() - timedelta(days=6)).isoformat(), None)]},
    )

    assert facts["groups"][str(sat.id)]["marked_lessons"] == 0
    assert facts["groups"][str(ielts.id)]["marked_lessons"] == 0


def test_the_group_zero_sentinel_means_the_same_as_the_legacy_shape(world):  # noqa: F811
    """Explicit group 0 and a bare list are the same statement written two ways."""
    sat, ielts = world["groups"]["SAT"], world["groups"]["IELTS"]
    lesson(world, sat, days_ago=3, status="absent")
    lesson(world, ielts, days_ago=3, status="absent")

    start = (date.today() - timedelta(days=6)).isoformat()
    facts = _facts(world, {world["student"].id: {GROUP_WIDE: [(start, None)]}})

    assert facts["groups"][str(sat.id)]["marked_lessons"] == 0
    assert facts["groups"][str(ielts.id)]["marked_lessons"] == 0


def test_weeks_before_a_scoped_freeze_are_untouched(world):  # noqa: F811
    """A freeze must never retroactively rewrite a term the student actually studied."""
    sat = world["groups"]["SAT"]
    lesson(world, sat, days_ago=20, status="absent")
    lesson(world, sat, days_ago=19, status="absent")

    facts = _facts(
        world,
        {
            world["student"].id: {
                sat.id: [((date.today() - timedelta(days=5)).isoformat(), None)]
            }
        },
    )
    assert facts["groups"][str(sat.id)]["marked_lessons"] == 2
    assert facts["groups"][str(sat.id)]["attendance_rate"] == 0.0


# --- the wire ---------------------------------------------------------------------------------


def test_the_endpoint_accepts_the_scoped_window_shape(client, world):  # noqa: F811
    """End to end, where both levels of the object arrive as JSON string keys."""
    sat, ielts = world["groups"]["SAT"], world["groups"]["IELTS"]
    lesson(world, sat, days_ago=3, status="absent")
    lesson(world, ielts, days_ago=3, status="absent")

    response = client.post(
        "/internal/crm/curator/students/health-facts",
        json={
            "lms_student_ids": [world["student"].id],
            "frozen_windows": {
                str(world["student"].id): {
                    str(sat.id): [[(date.today() - timedelta(days=6)).isoformat(), None]]
                }
            },
        },
        headers=_hdr(),
    )
    assert response.status_code == 200, response.text
    groups = response.json()["students"][str(world["student"].id)]["groups"]
    assert groups[str(sat.id)]["marked_lessons"] == 0
    assert groups[str(ielts.id)]["marked_lessons"] == 1, "IELTS survives a SAT freeze"


def test_the_endpoint_still_accepts_the_legacy_window_shape(client, world):  # noqa: F811
    """A CRM build predating scoped freezes posts a flat list and must still freeze all."""
    sat, ielts = world["groups"]["SAT"], world["groups"]["IELTS"]
    lesson(world, sat, days_ago=3, status="absent")
    lesson(world, ielts, days_ago=3, status="absent")

    response = client.post(
        "/internal/crm/curator/students/health-facts",
        json={
            "lms_student_ids": [world["student"].id],
            "frozen_windows": {
                str(world["student"].id): [
                    [(date.today() - timedelta(days=6)).isoformat(), None]
                ]
            },
        },
        headers=_hdr(),
    )
    assert response.status_code == 200, response.text
    groups = response.json()["students"][str(world["student"].id)]["groups"]
    assert groups[str(sat.id)]["marked_lessons"] == 0
    assert groups[str(ielts.id)]["marked_lessons"] == 0


def test_the_mirror_endpoint_carries_the_scope(client, db):  # noqa: F811
    """The CRM posts one item per frozen enrollment; two products are two rows."""
    response = client.post(
        "/internal/crm/curator/students/freeze-state",
        json={
            "items": [
                freeze_payload(group_id=10, scope_product="SAT", scope_group_name="SAT-1"),
                freeze_payload(group_id=20, scope_product="IELTS", scope_group_name="IELTS-1"),
            ]
        },
        headers=_hdr(),
    )
    assert response.status_code == 200, response.text
    assert response.json()["applied"] == 2

    rows = {r.group_id: r for r in db.query(StudentFreezeState).all()}
    assert set(rows) == {10, 20}
    assert rows[10].scope_product == "SAT"
    assert rows[20].scope_group_name == "IELTS-1"


# --- telling a frozen student from a student with no group -----------------------------------


def test_a_fully_frozen_student_is_not_a_student_without_a_group(world):  # noqa: F811
    """Freezing deletes the membership, so the CRM must be able to tell the two apart.

    Without ``has_frozen_scope`` the CRM's ``active_without_group`` rule fires on every
    student the school deliberately froze — the feature would ship as a wave of false cases.
    """
    sat, ielts = world["groups"]["SAT"], world["groups"]["IELTS"]
    _drop_memberships(world)

    start = (date.today() - timedelta(days=5)).isoformat()
    facts = _facts(
        world,
        {world["student"].id: {sat.id: [(start, None)], ielts.id: [(start, None)]}},
    )

    assert facts["groups"] == {}
    assert facts["has_frozen_scope"] is True
    assert facts["frozen_group_ids"] == sorted([sat.id, ielts.id])


def test_a_student_who_simply_lost_their_group_still_looks_like_one(world):  # noqa: F811
    """The other half of the distinction: no freeze, no flag, the rule fires as before."""
    _drop_memberships(world)

    facts = _facts(world, None)
    assert facts["groups"] == {}
    assert facts["has_frozen_scope"] is False
    assert facts["frozen_group_ids"] == []


def test_a_freeze_that_has_already_ended_does_not_hide_a_missing_group(world):  # noqa: F811
    """The flag says «frozen right now», not «was ever frozen»."""
    sat = world["groups"]["SAT"]
    _drop_memberships(world)

    facts = _facts(
        world,
        {
            world["student"].id: {
                sat.id: [
                    (
                        (date.today() - timedelta(days=40)).isoformat(),
                        (date.today() - timedelta(days=10)).isoformat(),
                    )
                ]
            }
        },
    )
    assert facts["has_frozen_scope"] is False


# --- convergence of the mirror ----------------------------------------------------------------


def test_the_revision_rule_is_per_scope(db):  # noqa: F811
    """A stale SAT delivery must not be able to discard a fresh IELTS one.

    Compared per student, the SAT message's lower revision would have looked like an
    out-of-order delivery for the whole student and been dropped — or, worse, applied and
    overwritten IELTS. The two scopes count independently because the CRM counts them on two
    different freeze periods.
    """
    upsert_freeze_state(db, freeze_payload(group_id=10, revision=5, scope_product="SAT"))
    upsert_freeze_state(db, freeze_payload(group_id=20, revision=1, scope_product="IELTS"))
    db.commit()

    assert db.query(StudentFreezeState).count() == 2

    # A replay of the older SAT state is stale for SAT...
    stale = upsert_freeze_state(db, freeze_payload(group_id=10, revision=4, status="resumed"))
    assert stale["applied"] is False
    # ...and says nothing at all about IELTS, whose revision 1 is still current.
    fresh = upsert_freeze_state(
        db,
        freeze_payload(
            group_id=20, revision=2, status="resumed",
            actual_resume_date=date.today().isoformat(),
        ),
    )
    db.commit()
    assert fresh["applied"] is True

    rows = {r.group_id: r for r in db.query(StudentFreezeState).all()}
    assert rows[10].is_frozen is True
    assert rows[20].is_frozen is False


def test_out_of_order_deliveries_converge_per_scope(db):  # noqa: F811
    """Replayed and reordered, in the worst order the outbox can produce."""
    resumed = {"status": "resumed", "actual_resume_date": date.today().isoformat()}
    for delivery in (
        freeze_payload(group_id=10, revision=2, **resumed),
        freeze_payload(group_id=10, revision=1, status="active"),
        freeze_payload(group_id=10, revision=2, **resumed),
        freeze_payload(group_id=20, revision=1, status="active"),
        freeze_payload(group_id=20, revision=1, status="active"),
    ):
        upsert_freeze_state(db, delivery)
    db.commit()

    rows = {r.group_id: r for r in db.query(StudentFreezeState).all()}
    assert set(rows) == {10, 20}
    assert rows[10].is_frozen is False, "the newest state wins however it arrived"
    assert rows[20].is_frozen is True


def test_a_payload_without_a_group_id_lands_on_the_student_wide_sentinel(db):  # noqa: F811
    """An older CRM build omits it, and it must keep meaning «every group»."""
    upsert_freeze_state(db, freeze_payload())
    db.commit()
    assert db.query(StudentFreezeState).one().group_id == GROUP_WIDE


def test_pending_placement_is_not_frozen(db):  # noqa: F811
    """They are back and expected to study; only nobody has put them in a group yet.

    Counting them as frozen would hide the single case somebody has to act on. The status is
    also seventeen characters, which the column was two characters too narrow to hold.
    """
    upsert_freeze_state(
        db,
        freeze_payload(
            group_id=10,
            status="pending_placement",
            actual_resume_date=date.today().isoformat(),
        ),
    )
    db.commit()
    row = db.query(StudentFreezeState).one()
    assert row.status == "pending_placement"
    assert row.is_frozen is False
    assert freeze_index(db, [1]).badge_for(1, 10) is None


# --- the read primitive -------------------------------------------------------------------------


def test_the_index_answers_per_group(db):  # noqa: F811
    upsert_freeze_state(
        db, freeze_payload(group_id=10, scope_product="SAT", scope_group_name="SAT-1")
    )
    db.commit()
    index = freeze_index(db, [1])
    day = date.today() - timedelta(days=1)

    assert index.is_frozen_on(1, 10, day) is True
    assert index.is_frozen_on(1, 20, day) is False
    # No group named at all asks the wider question the student's own banner asks.
    assert index.is_frozen_on(1, None, day) is True


def test_a_student_wide_row_matches_every_group(db):  # noqa: F811
    upsert_freeze_state(db, freeze_payload())  # no group_id -> 0
    db.commit()
    index = freeze_index(db, [1])
    day = date.today() - timedelta(days=1)

    assert index.is_frozen_on(1, 10, day) is True
    assert index.is_frozen_on(1, 999, day) is True


def test_staff_see_the_scope_and_students_never_do(db):  # noqa: F811
    """Same asymmetry as the reason code: «SAT-1» is an internal name."""
    upsert_freeze_state(
        db, freeze_payload(group_id=10, scope_product="SAT", scope_group_name="SAT-1")
    )
    db.commit()
    index = freeze_index(db, [1])

    staff = index.badge_for(1, 10)
    student = index.badge_for(1, 10, for_student=True)
    assert staff["scope_product"] == "SAT" and staff["scope_group_name"] == "SAT-1"
    assert "scope_product" not in student and "scope_group_name" not in student
    assert "reason_code" not in student


def test_a_student_wide_badge_reports_the_freeze_that_runs_longest(db):  # noqa: F811
    """Two frozen products, one banner: the earlier date would say they were fully back."""
    upsert_freeze_state(
        db,
        freeze_payload(
            group_id=10,
            planned_resume_date=(date.today() + timedelta(days=10)).isoformat(),
        ),
    )
    upsert_freeze_state(
        db,
        freeze_payload(
            group_id=20,
            planned_resume_date=(date.today() + timedelta(days=40)).isoformat(),
        ),
    )
    db.commit()

    badge = freeze_index(db, [1]).badge_for(1, for_student=True)
    assert badge["planned_resume_date"] == (date.today() + timedelta(days=40)).isoformat()


def test_freeze_states_still_returns_one_row_per_student(db):  # noqa: F811
    """The legacy primitive keeps its shape for callers asking the student-wide question."""
    upsert_freeze_state(db, freeze_payload(lms_student_id=1, group_id=10))
    upsert_freeze_state(db, freeze_payload(lms_student_id=1, group_id=20))
    upsert_freeze_state(db, freeze_payload(lms_student_id=2))
    db.commit()

    states = freeze_states(db, [1, 2])
    assert set(states) == {1, 2}
    assert states[1].user_id == 1
