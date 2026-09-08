"""The completion grace window must not put finished students back into onboarding.

On 2026-09-08 the grace period shipped: a finished group keeps ``is_over = False`` until the
first Wednesday 23:59:59 Asia/Almaty after its last lesson, so teachers do not lose it off
their list mid-lesson. The onboarding reconciler read ``is_over`` directly, so thirteen groups
that had finished *before* the deploy — and whose cycles it had already closed days earlier —
read as live again, and it opened 71 fresh cycles in two sweeps. «Новые» went from 11 to 78.

Two independent defences, tested separately because either alone leaves a hole:

* :func:`_finished_group_ids` — a group that has taught out is finished *for onboarding* the
  moment its last lesson ends, grace window or not.
* :func:`already_onboarded_into_group` — a relationship that reappears without the roster
  changing is a blip, not a return. This one also covers the older flap where a lesson dragged
  into the future re-opens a group that genuinely finished.
"""
from datetime import datetime, timedelta

import pytest

from src.curator.onboarding_core import (
    END_OPENED_IN_ERROR,
    END_RELATIONSHIP_ENDED,
    STATUS_DONE,
    STATUS_NEW,
    OnboardingActor,
    _finished_group_ids,
    already_onboarded_into_group,
    close_cycle,
    compute_active_pairs,
    curator_student_ids,
    open_cycle,
    set_status,
)
from src.curator.onboarding_service import reconcile_onboarding
from src.schemas.models import (
    CuratorOnboarding,
    Event,
    EventGroup,
    Group,
    GroupStudent,
    UserInDB,
)
from src.services.group_completion_service import compute_close_deadline
from src.utils.auth_utils import hash_password
from tests.onboarding_fixtures import db  # noqa: F401

_seq = 0


def _uniq() -> int:
    global _seq
    _seq += 1
    return _seq


def _user(db, role: str) -> UserInDB:
    u = UserInDB(
        email=f"grace-onb-{role}{_uniq()}@test.local",
        name=f"Grace {role}",
        role=role,
        hashed_password=hash_password("x"),
        is_active=True,
    )
    db.add(u)
    db.flush()
    return u


def _group(db, curator, *, lessons=(), lessons_count=None, is_over=False) -> Group:
    """A curated group with one active class event per ``(start, end)`` pair."""
    config = {"schedule_items": []}
    if lessons_count is not None:
        config["lessons_count"] = lessons_count
    group = Group(
        name=f"Grace Onb G{_uniq()}",
        is_active=True,
        is_over=is_over,
        curator_id=curator.id,
        program_type="sat",
        schedule_config=config,
    )
    db.add(group)
    db.flush()
    for start, end in lessons:
        ev = Event(
            title=f"{group.name}: Lesson",
            event_type="class",
            start_datetime=start,
            end_datetime=end,
            is_active=True,
            is_online=True,
            location="Online",
            created_by=curator.id,
        )
        db.add(ev)
        db.flush()
        db.add(EventGroup(event_id=ev.id, group_id=group.id))
    db.flush()
    return group


def _enrol(db, group, student, *, created_at=None) -> GroupStudent:
    row = GroupStudent(group_id=group.id, student_id=student.id)
    if created_at is not None:
        row.created_at = created_at
    db.add(row)
    db.flush()
    return row


def _finished_lessons(*, count=1, ended_minutes_ago=5):
    """Lessons that have all ended, the last one ``ended_minutes_ago`` in the past."""
    last_end = datetime.utcnow() - timedelta(minutes=ended_minutes_ago)
    return [
        (
            last_end - timedelta(days=i, hours=1),
            last_end - timedelta(days=i),
        )
        for i in reversed(range(count))
    ]


def _running_lessons():
    """One lesson taught, one still to come — a group in the middle of its course."""
    now = datetime.utcnow()
    return [
        (now - timedelta(days=7), now - timedelta(days=7) + timedelta(hours=1)),
        (now + timedelta(days=3), now + timedelta(days=3, hours=1)),
    ]


def _cards(db, curator_id):
    return (
        db.query(CuratorOnboarding)
        .filter(CuratorOnboarding.curator_id == curator_id)
        .order_by(CuratorOnboarding.cycle_no)
        .all()
    )


# ── the completion reading ────────────────────────────────────────────────────────────────


def test_a_group_inside_its_grace_window_produces_no_relationship(db):
    """The incident, in one test: finished last week, flag still False, no card."""
    curator, student = _user(db, "curator"), _user(db, "student")
    group = _group(db, curator, lessons_count=1, lessons=_finished_lessons())
    _enrol(db, group, student)

    # Precondition: this is the state the grace window creates — finished, flag not yet set.
    assert group.is_over is False
    assert compute_close_deadline(group.schedule_config, _finished_lessons()) is not None
    assert _finished_group_ids(db, [group.id]) == {group.id}

    assert (curator.id, student.id) not in compute_active_pairs(db)
    reconcile_onboarding(db)
    assert _cards(db, curator.id) == []


def test_a_running_group_still_produces_a_relationship(db):
    curator, student = _user(db, "curator"), _user(db, "student")
    group = _group(db, curator, lessons_count=2, lessons=_running_lessons())
    _enrol(db, group, student)

    assert _finished_group_ids(db, [group.id]) == set()
    assert compute_active_pairs(db).get((curator.id, student.id)) == group.id
    reconcile_onboarding(db)
    rows = _cards(db, curator.id)
    assert len(rows) == 1 and rows[0].status == STATUS_NEW


def test_a_group_with_no_lessons_at_all_still_produces_a_relationship(db):
    """A brand-new group has nothing to finish; it must not be mistaken for a finished one."""
    curator, student = _user(db, "curator"), _user(db, "student")
    group = _group(db, curator)
    _enrol(db, group, student)

    assert _finished_group_ids(db, [group.id]) == set()
    reconcile_onboarding(db)
    assert len(_cards(db, curator.id)) == 1


def test_a_group_past_its_grace_window_closes_the_cycle_as_before(db):
    """Unchanged behaviour: once the deadline passes the relationship ends normally."""
    curator, student = _user(db, "curator"), _user(db, "student")
    group = _group(db, curator, lessons_count=2, lessons=_running_lessons())
    _enrol(db, group, student)
    reconcile_onboarding(db)
    assert len(_cards(db, curator.id)) == 1

    # Thirty days back is longer than any grace window can span, so the group is closed.
    old = datetime.utcnow() - timedelta(days=30)
    for ev in (
        db.query(Event)
        .join(EventGroup, EventGroup.event_id == Event.id)
        .filter(EventGroup.group_id == group.id)
        .all()
    ):
        ev.start_datetime = old
        ev.end_datetime = old + timedelta(hours=1)
    db.flush()

    reconcile_onboarding(db)
    rows = _cards(db, curator.id)
    assert len(rows) == 1, "closing must not open a replacement cycle"
    assert rows[0].ended_at is not None
    assert rows[0].end_reason == END_RELATIONSHIP_ENDED


def test_a_live_group_survives_a_finished_one_under_the_same_curator(db):
    """The display-group trap: filtering after the 'most recent wins' reduction loses this.

    The finished group is the more recently joined one, so it is the pair's display group. A
    filter applied to the reduced mapping would drop the whole relationship even though the
    student is still studying with this curator in the other group.
    """
    curator, student = _user(db, "curator"), _user(db, "student")
    running = _group(db, curator, lessons_count=2, lessons=_running_lessons())
    finished = _group(db, curator, lessons_count=1, lessons=_finished_lessons())
    _enrol(db, running, student, created_at=datetime.utcnow() - timedelta(days=30))
    _enrol(db, finished, student, created_at=datetime.utcnow())

    assert _finished_group_ids(db, [running.id, finished.id]) == {finished.id}
    assert compute_active_pairs(db).get((curator.id, student.id)) == running.id
    assert student.id in curator_student_ids(db, [curator.id])


def test_grace_window_students_leave_the_visibility_scope_too(db):
    """``curator_student_ids`` answers the same question and must not disagree with it."""
    curator, student = _user(db, "curator"), _user(db, "student")
    group = _group(db, curator, lessons_count=1, lessons=_finished_lessons())
    _enrol(db, group, student)

    assert student.id not in curator_student_ids(db, [curator.id])


# ── the re-open guard ─────────────────────────────────────────────────────────────────────


def _closed_done_cycle(db, curator, student, group, *, joined_days_ago=30):
    """A completed onboarding that was then closed — the state before a blip re-opens it."""
    _enrol(db, group, student, created_at=datetime.utcnow() - timedelta(days=joined_days_ago))
    first = open_cycle(db, curator.id, student.id, group.id)
    set_status(db, first, STATUS_DONE, OnboardingActor.from_user(curator), commit=False)
    close_cycle(db, first, END_RELATIONSHIP_ENDED)
    db.flush()
    return first


def test_a_relationship_blip_on_a_finished_group_does_not_create_cycle_two(db):
    """The older flap: a lesson dragged into the future makes a finished group live again."""
    curator, student = _user(db, "curator"), _user(db, "student")
    group = _group(db, curator, lessons_count=1, lessons=_finished_lessons())
    first = _closed_done_cycle(db, curator, student, group)

    assert already_onboarded_into_group(db, curator.id, student.id, group.id) is True
    assert open_cycle(db, curator.id, student.id, group.id) is None

    rows = _cards(db, curator.id)
    assert len(rows) == 1 and rows[0].id == first.id, "history intact, nothing re-opened"


def test_a_genuine_return_to_the_same_group_still_gets_a_cycle(db):
    """«Перекурс»: the student really left and re-enrolled, so the roster row is newer."""
    curator, student = _user(db, "curator"), _user(db, "student")
    group = _group(db, curator, lessons_count=2, lessons=_running_lessons())
    _closed_done_cycle(db, curator, student, group)

    # They left and came back: the membership is rewritten well after the close.
    db.query(GroupStudent).filter(
        GroupStudent.group_id == group.id, GroupStudent.student_id == student.id
    ).delete()
    _enrol(db, group, student, created_at=datetime.utcnow() + timedelta(days=2))
    db.flush()

    assert already_onboarded_into_group(db, curator.id, student.id, group.id) is False
    second = open_cycle(db, curator.id, student.id, group.id)
    assert second is not None and second.cycle_no == 2 and second.status == STATUS_NEW


def test_a_new_group_with_the_same_curator_still_gets_a_cycle(db):
    """Finishing one course and starting another is a real onboarding, not a blip."""
    curator, student = _user(db, "curator"), _user(db, "student")
    finished = _group(db, curator, lessons_count=1, lessons=_finished_lessons())
    _closed_done_cycle(db, curator, student, finished)

    fresh = _group(db, curator, lessons_count=2, lessons=_running_lessons())
    _enrol(db, fresh, student)

    assert already_onboarded_into_group(db, curator.id, student.id, fresh.id) is False
    reconcile_onboarding(db)
    rows = _cards(db, curator.id)
    assert len(rows) == 2 and rows[1].group_id == fresh.id and rows[1].status == STATUS_NEW


def test_a_cancelled_previous_cycle_is_not_a_veto(db):
    """The onboarding never finished, so a second attempt is exactly right."""
    curator, student = _user(db, "curator"), _user(db, "student")
    group = _group(db, curator, lessons_count=2, lessons=_running_lessons())
    _enrol(db, group, student, created_at=datetime.utcnow() - timedelta(days=30))
    first = open_cycle(db, curator.id, student.id, group.id)
    close_cycle(db, first, END_RELATIONSHIP_ENDED)  # in flight -> cancelled
    db.flush()

    assert already_onboarded_into_group(db, curator.id, student.id, group.id) is False
    assert open_cycle(db, curator.id, student.id, group.id) is not None


def test_the_guard_never_vetoes_without_a_membership_row(db):
    """Absence of a roster row is not evidence of a blip; callers may open cycles directly."""
    curator, student = _user(db, "curator"), _user(db, "student")
    group = _group(db, curator, lessons_count=2, lessons=_running_lessons())
    first = open_cycle(db, curator.id, student.id, group.id)
    set_status(db, first, STATUS_DONE, OnboardingActor.from_user(curator), commit=False)
    close_cycle(db, first, END_RELATIONSHIP_ENDED)
    db.flush()

    assert already_onboarded_into_group(db, curator.id, student.id, group.id) is False
    assert open_cycle(db, curator.id, student.id, group.id) is not None


def test_a_membership_written_within_the_skew_margin_is_not_vetoed(db):
    """The two timestamps come from different clocks; too close to call means do not veto."""
    curator, student = _user(db, "curator"), _user(db, "student")
    group = _group(db, curator, lessons_count=2, lessons=_running_lessons())
    _closed_done_cycle(db, curator, student, group)

    db.query(GroupStudent).filter(
        GroupStudent.group_id == group.id, GroupStudent.student_id == student.id
    ).delete()
    # An hour before the close — well inside the 24h tolerance, so unorderable across clocks.
    _enrol(db, group, student, created_at=datetime.utcnow() - timedelta(hours=1))
    db.flush()

    assert already_onboarded_into_group(db, curator.id, student.id, group.id) is False


# ── the repair command ────────────────────────────────────────────────────────────────────


def _bogus_card(db, curator, student, group):
    """Exactly what the deploy produced: a cycle-2 ``new`` card on a grace-window group."""
    _closed_done_cycle(db, curator, student, group)
    # Written directly, bypassing the guard that now refuses it — reproducing the rows the
    # incident left behind, which is what the repair has to clean up.
    row = CuratorOnboarding(
        curator_id=curator.id,
        student_id=student.id,
        group_id=group.id,
        status=STATUS_NEW,
        cycle_no=2,
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
        status_changed_at=datetime.utcnow(),
    )
    db.add(row)
    db.flush()
    return row


@pytest.fixture
def window():
    """A `since` that includes anything this test module creates."""
    return datetime.utcnow() - timedelta(hours=1)


def test_the_repair_closes_an_eligible_card(db, window):
    from src.curator.onboarding_repair import VERDICT_CLOSE, apply_repair, scan

    curator, student = _user(db, "curator"), _user(db, "student")
    group = _group(db, curator, lessons_count=1, lessons=_finished_lessons())
    card = _bogus_card(db, curator, student, group)

    mine = [f for f in scan(db, since=window) if f.onboarding_id == card.id]
    assert [f.verdict for f in mine] == [VERDICT_CLOSE]

    closed = apply_repair(db, mine, since=window)
    assert [f.onboarding_id for f in closed] == [card.id]

    db.refresh(card)
    assert card.ended_at is not None
    assert card.end_reason == END_OPENED_IN_ERROR
    assert card.status == "cancelled", "an in-flight cycle closes as cancelled, as always"


def test_the_repair_is_idempotent(db, window):
    from src.curator.onboarding_repair import (
        VERDICT_ALREADY_CLOSED,
        apply_repair,
        scan,
        scan_one,
    )

    curator, student = _user(db, "curator"), _user(db, "student")
    group = _group(db, curator, lessons_count=1, lessons=_finished_lessons())
    card = _bogus_card(db, curator, student, group)

    first = [f for f in scan(db, since=window) if f.onboarding_id == card.id]
    assert len(apply_repair(db, first, since=window)) == 1
    ended_at = card.ended_at

    assert scan_one(db, card.id, since=window).verdict == VERDICT_ALREADY_CLOSED
    second = [f for f in scan(db, since=window) if f.onboarding_id == card.id]
    assert apply_repair(db, second, since=window) == []
    db.refresh(card)
    assert card.ended_at == ended_at, "a second run must not rewrite the close"


def test_the_repair_skips_a_card_the_curator_has_started(db, window):
    from src.curator.onboarding_repair import VERDICT_CURATOR_WORKING, apply_repair, scan

    curator, student = _user(db, "curator"), _user(db, "student")
    group = _group(db, curator, lessons_count=1, lessons=_finished_lessons())
    card = _bogus_card(db, curator, student, group)
    set_status(db, card, STATUS_DONE, OnboardingActor.from_user(curator), commit=False)
    db.flush()

    mine = [f for f in scan(db, since=window) if f.onboarding_id == card.id]
    assert [f.verdict for f in mine] == [VERDICT_CURATOR_WORKING]
    assert apply_repair(db, mine, since=window) == []
    db.refresh(card)
    assert card.ended_at is None


def test_the_repair_skips_a_card_a_human_left_a_note_on(db, window):
    from src.curator.onboarding_repair import VERDICT_HUMAN_ACTIVITY, apply_repair, scan
    from src.curator.onboarding_core import add_note

    curator, student = _user(db, "curator"), _user(db, "student")
    group = _group(db, curator, lessons_count=1, lessons=_finished_lessons())
    card = _bogus_card(db, curator, student, group)
    add_note(db, card, "звонила маме", OnboardingActor.from_user(curator), commit=False)
    db.flush()

    mine = [f for f in scan(db, since=window) if f.onboarding_id == card.id]
    assert [f.verdict for f in mine] == [VERDICT_HUMAN_ACTIVITY]
    assert apply_repair(db, mine, since=window) == []
    db.refresh(card)
    assert card.ended_at is None


def test_the_cards_own_opening_event_is_not_curator_work(db, window):
    """A CRM-opened cycle records ``cycle.opened`` against a real head curator.

    That is the card appearing, not somebody working it, and counting it would make every
    CRM-created card permanently unrepairable for the wrong reason.
    """
    from src.curator.onboarding_core import record_event
    from src.curator.onboarding_repair import VERDICT_CLOSE, scan

    curator, student = _user(db, "curator"), _user(db, "student")
    head = _user(db, "head_curator")
    group = _group(db, curator, lessons_count=1, lessons=_finished_lessons())
    card = _bogus_card(db, curator, student, group)
    record_event(db, card, OnboardingActor.from_user(head), "cycle.opened", after={"cycle_no": 2})
    db.flush()

    mine = [f for f in scan(db, since=window) if f.onboarding_id == card.id]
    assert [f.verdict for f in mine] == [VERDICT_CLOSE]
    assert mine[0].human_events == 0


def test_the_repair_skips_a_first_cycle_card(db, window):
    """September's genuine new students are cycle 1 and must survive the cleanup."""
    from src.curator.onboarding_repair import VERDICT_FIRST_CYCLE, apply_repair, scan

    curator, student = _user(db, "curator"), _user(db, "student")
    # A grace-window group, so only the cycle number separates this from an eligible card.
    group = _group(db, curator, lessons_count=1, lessons=_finished_lessons())
    _enrol(db, group, student)
    card = CuratorOnboarding(
        curator_id=curator.id,
        student_id=student.id,
        group_id=group.id,
        status=STATUS_NEW,
        cycle_no=1,
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    db.add(card)
    db.flush()

    mine = [f for f in scan(db, since=window) if f.onboarding_id == card.id]
    assert [f.verdict for f in mine] == [VERDICT_FIRST_CYCLE]
    assert apply_repair(db, mine, since=window) == []


def test_the_repair_skips_a_card_on_a_running_group(db, window):
    from src.curator.onboarding_repair import VERDICT_GROUP_RUNNING, apply_repair, scan

    curator, student = _user(db, "curator"), _user(db, "student")
    group = _group(db, curator, lessons_count=2, lessons=_running_lessons())
    card = _bogus_card(db, curator, student, group)

    mine = [f for f in scan(db, since=window) if f.onboarding_id == card.id]
    assert [f.verdict for f in mine] == [VERDICT_GROUP_RUNNING]
    assert apply_repair(db, mine, since=window) == []


def test_a_dry_run_writes_nothing(db, window):
    """``scan`` is the dry run: the report exists and the row is untouched."""
    from src.curator.onboarding_repair import VERDICT_CLOSE, render_table, scan

    curator, student = _user(db, "curator"), _user(db, "student")
    group = _group(db, curator, lessons_count=1, lessons=_finished_lessons())
    card = _bogus_card(db, curator, student, group)

    findings = scan(db, since=window)
    mine = [f for f in findings if f.onboarding_id == card.id]
    assert [f.verdict for f in mine] == [VERDICT_CLOSE]
    assert str(card.id) in render_table(findings)

    db.refresh(card)
    assert card.ended_at is None and card.status == STATUS_NEW


def test_the_repair_rechecks_before_writing(db, window):
    """The table a human reads can be minutes stale; the write must not trust it."""
    from src.curator.onboarding_repair import VERDICT_CLOSE, apply_repair, scan

    curator, student = _user(db, "curator"), _user(db, "student")
    group = _group(db, curator, lessons_count=1, lessons=_finished_lessons())
    card = _bogus_card(db, curator, student, group)

    mine = [f for f in scan(db, since=window) if f.onboarding_id == card.id]
    assert [f.verdict for f in mine] == [VERDICT_CLOSE]

    # A curator picks the card up between the report and the --apply.
    set_status(db, card, STATUS_DONE, OnboardingActor.from_user(curator), commit=False)
    db.flush()

    assert apply_repair(db, mine, since=window) == []
    db.refresh(card)
    assert card.ended_at is None
