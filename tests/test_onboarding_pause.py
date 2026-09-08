"""A frozen student's onboarding card pauses. It does not close.

The failure these are written against: freezing a student removed their membership of the
frozen group, the reconciler read that as "not your student any more" and closed the cycle,
and the curator's notes and «В работе» status became history. On return the student arrived
as cycle 2 with an empty card, as though nobody had ever onboarded them.

Three things have to hold at once for the pause to be worth having, and each is easy to get
right on its own and wrong together:

* the card is **off the board** while the student is frozen, but still **open**, so nothing
  opens a second cycle beside it;
* the **clock stops** — the days a student spends frozen are not days their curator failed to
  act, so they must not push the card into «просрочено» on return;
* the **same row** comes back, with the same status and the same notes.

The last group of tests is about the order the CRM speaks in. It removes the membership,
calls the LMS's reconcile *synchronously*, and delivers the freeze state on the outbox
afterwards — so the LMS is always asked to re-derive the cards before it is told why they
changed. Whichever message lands first, the end state must be the same paused card.
"""
from datetime import date, datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.config import get_db
from src.curator.freeze_mirror import GROUP_WIDE, upsert_freeze_state
from src.curator.onboarding_core import (
    END_RELATIONSHIP_ENDED,
    IN_PROGRESS_STALE_DAYS,
    NEW_OVERDUE_DAYS,
    STATUS_IN_PROGRESS,
    OnboardingActor,
    OnboardingPermissionError,
    active_cycle,
    add_note,
    close_cycle,
    is_overdue,
    is_paused,
    load_board,
    onboarding_age_days,
    open_cycle,
    reconcile_student,
    serialize_card,
    set_status,
    status_counts,
)
from src.curator.onboarding_pause import (
    CLOSE_REVERSAL_WINDOW,
    pause_cycle,
    resume_cycle,
    sync_pauses_for_students,
)
from src.curator.onboarding_service import reconcile_onboarding
from src.routes.crm_curator_internal import router as curator_router
from src.schemas.models import CuratorOnboarding, GroupStudent
from tests.scoped_freeze_fixtures import (  # noqa: F401 - fixtures used by name
    db,
    freeze_payload,
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


def _actor(user):
    return OnboardingActor.from_user(user)


def _card(world, group):  # noqa: F811
    """One open cycle for the world's pair, on ``group``."""
    row = open_cycle(
        world["db"], world["curator"].id, world["student"].id, group.id, _actor(world["curator"])
    )
    world["db"].flush()
    return row


def _freeze(world, group, **overrides):  # noqa: F811
    """Deliver a CRM freeze for one group, straight into the mirror."""
    upsert_freeze_state(
        world["db"],
        freeze_payload(
            lms_student_id=world["student"].id,
            group_id=(GROUP_WIDE if group is None else group.id),
            **overrides,
        ),
    )
    world["db"].flush()


def _unenrol(world, group):  # noqa: F811
    """What the freeze does to the roster, and the reason the card looks abandoned."""
    world["db"].query(GroupStudent).filter(
        GroupStudent.group_id == group.id,
        GroupStudent.student_id == world["student"].id,
    ).delete()
    world["db"].flush()


# --- the pause itself ---------------------------------------------------------------------


def test_a_frozen_students_card_leaves_the_board_but_stays_open(world):  # noqa: F811
    db = world["db"]
    card = _card(world, world["groups"]["SAT"])
    set_status(db, card, STATUS_IN_PROGRESS, _actor(world["curator"]), commit=False)
    add_note(db, card, "созвонились, ждём оплату", _actor(world["curator"]), commit=False)
    db.flush()

    _freeze(world, world["groups"]["SAT"])
    _unenrol(world, world["groups"]["SAT"])
    sync_pauses_for_students(db, [world["student"].id])
    db.flush()

    assert is_paused(card) is True
    assert card.ended_at is None, "paused is not closed — the row still holds the pair's slot"
    assert card.id not in {r.id for r in load_board(db, curator_ids=[world["curator"].id])}
    assert card.status == STATUS_IN_PROGRESS, "the curator's state survives untouched"
    assert [n.body for n in card.notes] == ["созвонились, ждём оплату"]
    assert status_counts(db, curator_ids=[world["curator"].id])["in_progress"] == 0


def test_a_paused_card_accrues_no_overdue_time(world):  # noqa: F811
    """The part most easily got wrong: the clock must stop, not merely be ignored.

    A card one day into its two-day window, frozen for ten. It must come back with one day
    used — not eleven, which would put it «просрочено» the moment the student returned and
    hand the curator a failure for the days they were told not to work.
    """
    db = world["db"]
    now = datetime.utcnow()
    card = _card(world, world["groups"]["SAT"])
    card.created_at = now - timedelta(days=11)
    db.flush()
    assert is_overdue(card, now) is True, "without the pause this card is long overdue"

    _freeze(world, world["groups"]["SAT"])
    sync_pauses_for_students(db, [world["student"].id])
    # Ten of those eleven days were spent frozen.
    card.paused_at = now - timedelta(days=10)
    db.flush()

    assert is_overdue(card, now) is False
    assert onboarding_age_days(card, now) == 1, "age stops with the clock"

    resume_cycle(db, card)
    db.flush()
    assert is_paused(card) is False
    assert card.paused_seconds == pytest.approx(10 * 86400, abs=60)
    assert is_overdue(card, now) is False, "one day used of two, not eleven"
    assert onboarding_age_days(card, now) == 1
    # And it still goes overdue on its own merits, once two working days have passed.
    assert is_overdue(card, now + timedelta(days=NEW_OVERDUE_DAYS)) is True


def test_a_pause_does_not_re_anchor_the_in_progress_clock(world):  # noqa: F811
    db = world["db"]
    card = _card(world, world["groups"]["SAT"])
    set_status(db, card, STATUS_IN_PROGRESS, _actor(world["curator"]), commit=False)
    moved = datetime.utcnow() - timedelta(days=IN_PROGRESS_STALE_DAYS - 1)
    card.status_changed_at = moved
    db.flush()

    pause_cycle(db, card)
    card.paused_at = datetime.utcnow() - timedelta(days=30)
    resume_cycle(db, card)
    db.flush()

    assert is_overdue(card, datetime.utcnow()) is False, "thirty frozen days are not staleness"


def test_a_paused_card_blocks_a_second_cycle_for_the_pair(world):  # noqa: F811
    """The cycle invariant is unchanged: paused rows are open rows and still hold the slot."""
    db = world["db"]
    card = _card(world, world["groups"]["SAT"])
    pause_cycle(db, card)
    db.flush()

    assert active_cycle(db, world["curator"].id, world["student"].id).id == card.id
    assert (
        open_cycle(db, world["curator"].id, world["student"].id, world["groups"]["SAT"].id)
        is None
    )
    rows = db.query(CuratorOnboarding).filter(
        CuratorOnboarding.student_id == world["student"].id
    ).all()
    assert [r.id for r in rows] == [card.id]


def test_a_paused_card_refuses_a_status_change(world):  # noqa: F811
    db = world["db"]
    card = _card(world, world["groups"]["SAT"])
    pause_cycle(db, card)
    db.flush()

    with pytest.raises(OnboardingPermissionError):
        set_status(db, card, STATUS_IN_PROGRESS, _actor(world["curator"]), commit=False)


def test_pause_and_resume_are_idempotent(world):  # noqa: F811
    """A redelivered freeze must not restart the pause and quietly extend it."""
    db = world["db"]
    card = _card(world, world["groups"]["SAT"])
    assert pause_cycle(db, card) is True
    started = card.paused_at
    assert pause_cycle(db, card) is False
    assert card.paused_at == started

    assert resume_cycle(db, card) is True
    assert resume_cycle(db, card) is False


def test_the_card_says_it_is_paused_on_the_wire(world):  # noqa: F811
    db = world["db"]
    card = _card(world, world["groups"]["SAT"])
    pause_cycle(db, card)
    card.paused_at = datetime.utcnow() - timedelta(days=3)
    db.flush()

    data = serialize_card(card)
    assert data["is_paused"] is True and data["paused_days"] == 3
    assert data["ended_at"] is None


# --- scope --------------------------------------------------------------------------------


def test_a_sat_freeze_leaves_the_ielts_card_working(world):  # noqa: F811
    """The whole point of scoped freezes, asked of onboarding this time."""
    db = world["db"]
    other_curator = world["curator"]
    sat_card = _card(world, world["groups"]["SAT"])
    db.flush()
    # A second curator, so the student can hold two open cycles at once.
    from src.schemas.models import UserInDB

    ielts_curator = UserInDB(
        email="cur-ielts@lms.local", name="Куратор IELTS", role="curator",
        is_active=True, hashed_password="x",
    )
    db.add(ielts_curator)
    db.flush()
    world["groups"]["IELTS"].curator_id = ielts_curator.id
    db.flush()
    ielts_card = open_cycle(
        db, ielts_curator.id, world["student"].id, world["groups"]["IELTS"].id
    )
    db.flush()

    _freeze(world, world["groups"]["SAT"])
    sync_pauses_for_students(db, [world["student"].id])
    db.flush()

    assert is_paused(sat_card) is True
    assert is_paused(ielts_card) is False, "a SAT freeze says nothing about IELTS"
    assert ielts_card.id in {r.id for r in load_board(db, curator_ids=[ielts_curator.id])}
    assert sat_card.curator_id == other_curator.id


def test_the_sweep_pauses_a_frozen_student_who_is_still_on_the_roster(world):  # noqa: F811
    """A freeze the membership removal never followed — the mirror still decides.

    A student-wide freeze removes nothing (the CRM's scope removal names no group and returns
    early), and a scoped one can fail its removal. Either way the CRM has said the enrollment
    is suspended, so the card comes off the board even though the roster still lists them.
    """
    db = world["db"]
    card = _card(world, world["groups"]["SAT"])
    _freeze(world, None)  # student-wide: the roster is left exactly as it is

    result = reconcile_student(db, world["student"].id)
    db.refresh(card)

    assert result["paused"] == 1
    assert is_paused(card) is True and card.ended_at is None


def test_a_freeze_on_one_of_two_groups_of_the_same_curator_keeps_the_card_working(world):  # noqa: F811
    """One card covers the pair, not the group: while any of their groups is running, so is it.

    The card's ``group_id`` is only what it *displays*. Pausing on the strength of the frozen
    one would hide a student who is still turning up to the other product twice a week.
    """
    db = world["db"]
    card = _card(world, world["groups"]["SAT"])
    _freeze(world, world["groups"]["SAT"])
    _unenrol(world, world["groups"]["SAT"])

    reconcile_student(db, world["student"].id)
    db.refresh(card)

    assert is_paused(card) is False
    assert card.group_id == world["groups"]["IELTS"].id, "regrouped to what is still running"
    assert card.id in {r.id for r in load_board(db, curator_ids=[world["curator"].id])}


def test_a_student_wide_freeze_pauses_every_card(world):  # noqa: F811
    db = world["db"]
    card = _card(world, world["groups"]["SAT"])
    _freeze(world, None)  # group_id 0 — the legacy, student-wide scope
    sync_pauses_for_students(db, [world["student"].id])
    db.flush()

    assert is_paused(card) is True


# --- the reconciler -----------------------------------------------------------------------


def test_the_reconciler_pauses_instead_of_closing_a_frozen_students_card(world):  # noqa: F811
    db = world["db"]
    card = _card(world, world["groups"]["SAT"])
    set_status(db, card, STATUS_IN_PROGRESS, _actor(world["curator"]), commit=False)
    db.flush()

    _freeze(world, world["groups"]["SAT"])
    _unenrol(world, world["groups"]["SAT"])
    # The IELTS group keeps the pair alive, so this is specifically about the frozen card.
    _unenrol(world, world["groups"]["IELTS"])

    result = reconcile_student(db, world["student"].id)
    db.refresh(card)

    assert result["paused"] == 1 and result["closed"] == 0
    assert card.ended_at is None and is_paused(card) is True
    assert card.status == STATUS_IN_PROGRESS


def test_the_reconciler_resumes_a_card_once_the_freeze_is_over(world):  # noqa: F811
    """The same row comes back — not a new cycle with an empty page."""
    db = world["db"]
    card = _card(world, world["groups"]["SAT"])
    set_status(db, card, STATUS_IN_PROGRESS, _actor(world["curator"]), commit=False)
    add_note(db, card, "родители в курсе", _actor(world["curator"]), commit=False)
    _freeze(world, world["groups"]["SAT"])
    _unenrol(world, world["groups"]["SAT"])
    _unenrol(world, world["groups"]["IELTS"])
    reconcile_student(db, world["student"].id)
    db.refresh(card)
    assert is_paused(card) is True

    # The CRM confirms the return and places the student back in the same group.
    _freeze(
        world,
        world["groups"]["SAT"],
        status="resumed",
        revision=200,
        actual_resume_date=date.today().isoformat(),
    )
    db.add(GroupStudent(group_id=world["groups"]["SAT"].id, student_id=world["student"].id))
    db.flush()

    result = reconcile_student(db, world["student"].id)
    db.refresh(card)

    assert result["resumed"] == 1 and result["created"] == 0
    assert is_paused(card) is False and card.ended_at is None
    assert card.status == STATUS_IN_PROGRESS, "back in the state the curator left it"
    assert [n.body for n in card.notes] == ["родители в курсе"]
    rows = db.query(CuratorOnboarding).filter(
        CuratorOnboarding.student_id == world["student"].id
    ).all()
    assert [r.id for r in rows] == [card.id], "no second cycle was ever opened"


def test_the_full_sweep_pauses_and_never_closes_a_frozen_card(world):  # noqa: F811
    db = world["db"]
    card = _card(world, world["groups"]["SAT"])
    _freeze(world, world["groups"]["SAT"])
    _unenrol(world, world["groups"]["SAT"])
    _unenrol(world, world["groups"]["IELTS"])

    reconcile_onboarding(db)
    reconcile_onboarding(db)  # twice: nothing may drift on the second pass
    db.refresh(card)

    assert card.ended_at is None and is_paused(card) is True


def test_a_paused_card_still_closes_when_the_student_is_not_frozen_any_more(world):  # noqa: F811
    """Pause is not a way to keep a card forever: a return to nowhere still ends it."""
    db = world["db"]
    card = _card(world, world["groups"]["SAT"])
    _freeze(world, world["groups"]["SAT"])
    _unenrol(world, world["groups"]["SAT"])
    _unenrol(world, world["groups"]["IELTS"])
    reconcile_student(db, world["student"].id)
    db.refresh(card)
    assert is_paused(card) is True

    _freeze(world, world["groups"]["SAT"], status="resumed", revision=200)
    result = reconcile_student(db, world["student"].id)
    db.refresh(card)

    assert result["closed"] == 1
    assert card.ended_at is not None
    assert is_paused(card) is False, "a closed card is not left paused for ever"


# --- the freeze-state door ----------------------------------------------------------------


def test_the_freeze_delivery_pauses_the_card(client, world):  # noqa: F811
    db = world["db"]
    card = _card(world, world["groups"]["SAT"])
    db.commit()

    r = client.post(
        "/internal/crm/curator/students/freeze-state",
        json={
            "items": [
                freeze_payload(
                    lms_student_id=world["student"].id, group_id=world["groups"]["SAT"].id
                )
            ]
        },
        headers=_hdr(),
    )
    assert r.status_code == 200
    assert r.json()["onboarding"] == {"paused": 1, "resumed": 0, "reopened": 0}
    db.refresh(card)
    assert is_paused(card) is True


def test_the_return_delivery_resumes_the_card(client, world):  # noqa: F811
    db = world["db"]
    card = _card(world, world["groups"]["SAT"])
    pause_cycle(db, card)
    _freeze(world, world["groups"]["SAT"])
    db.commit()

    r = client.post(
        "/internal/crm/curator/students/freeze-state",
        json={
            "items": [
                freeze_payload(
                    lms_student_id=world["student"].id,
                    group_id=world["groups"]["SAT"].id,
                    status="resumed",
                    revision=200,
                    actual_resume_date=date.today().isoformat(),
                )
            ]
        },
        headers=_hdr(),
    )
    assert r.status_code == 200
    assert r.json()["onboarding"]["resumed"] == 1
    db.refresh(card)
    assert is_paused(card) is False


# --- the CRM's ordering -------------------------------------------------------------------


def test_a_close_the_freeze_explains_is_reversed_and_paused(world):  # noqa: F811
    """The real sequence: reconcile first, freeze state second.

    The CRM removes the membership, commits, calls ``/reconcile-student`` in the request, and
    only then hands the mirror delivery to the outbox. The card is closed before the LMS is
    told there is a freeze at all — so the delivery has to undo it.
    """
    db = world["db"]
    card = _card(world, world["groups"]["SAT"])
    set_status(db, card, STATUS_IN_PROGRESS, _actor(world["curator"]), commit=False)
    add_note(db, card, "договорились о паузе", _actor(world["curator"]), commit=False)
    db.flush()

    _unenrol(world, world["groups"]["SAT"])
    _unenrol(world, world["groups"]["IELTS"])
    reconcile_student(db, world["student"].id)
    db.refresh(card)
    assert card.ended_at is not None, "closed, exactly as production does today"
    assert card.status == "cancelled"

    _freeze(world, world["groups"]["SAT"])
    result = sync_pauses_for_students(db, [world["student"].id])
    db.flush()
    db.refresh(card)

    assert result == {"paused": 1, "resumed": 0, "reopened": 1}
    assert card.ended_at is None and card.end_reason is None
    assert card.status == STATUS_IN_PROGRESS, "restored to what the history says it was"
    assert is_paused(card) is True
    assert [n.body for n in card.notes] == ["договорились о паузе"]
    assert "cycle.close_reversed" in {e.action for e in card.events}


def test_an_old_close_is_not_reversed_by_a_new_freeze(world):  # noqa: F811
    """A freeze can only explain a close it could plausibly have caused."""
    db = world["db"]
    card = _card(world, world["groups"]["SAT"])
    close_cycle(db, card, END_RELATIONSHIP_ENDED)
    card.ended_at = datetime.utcnow() - CLOSE_REVERSAL_WINDOW - timedelta(hours=1)
    db.flush()

    _freeze(world, world["groups"]["SAT"])
    result = sync_pauses_for_students(db, [world["student"].id])
    db.refresh(card)

    assert result["reopened"] == 0
    assert card.ended_at is not None


def test_a_completed_cycle_is_never_reopened_by_a_freeze(world):  # noqa: F811
    """«Завершено» is the curator's decision, and a freeze does not overrule a decision."""
    db = world["db"]
    from src.curator.onboarding_core import STATUS_DONE

    card = _card(world, world["groups"]["SAT"])
    set_status(db, card, STATUS_DONE, _actor(world["curator"]), commit=False)
    db.flush()
    assert card.ended_at is not None

    _freeze(world, world["groups"]["SAT"])
    result = sync_pauses_for_students(db, [world["student"].id])
    db.refresh(card)

    assert result["reopened"] == 0
    assert card.status == STATUS_DONE and card.ended_at is not None


def test_a_reversal_never_collides_with_an_open_cycle(world):  # noqa: F811
    """The partial unique index permits one open cycle; the reversal must not be the write
    that breaks it."""
    db = world["db"]
    first = _card(world, world["groups"]["SAT"])
    close_cycle(db, first, END_RELATIONSHIP_ENDED)
    db.flush()
    second = open_cycle(db, world["curator"].id, world["student"].id, world["groups"]["SAT"].id)
    db.flush()
    assert second is not None and second.id != first.id

    _freeze(world, world["groups"]["SAT"])
    result = sync_pauses_for_students(db, [world["student"].id])
    db.flush()

    assert result["reopened"] == 0
    db.refresh(first)
    assert first.ended_at is not None
    assert is_paused(second) is True, "the open cycle is the one that pauses"


def test_the_delivery_arriving_first_needs_no_reversal(world):  # noqa: F811
    """The other order — the outbox wins the race — must reach the identical state."""
    db = world["db"]
    card = _card(world, world["groups"]["SAT"])
    set_status(db, card, STATUS_IN_PROGRESS, _actor(world["curator"]), commit=False)
    db.flush()

    _freeze(world, world["groups"]["SAT"])
    _unenrol(world, world["groups"]["SAT"])
    _unenrol(world, world["groups"]["IELTS"])
    first = sync_pauses_for_students(db, [world["student"].id])
    reconcile_student(db, world["student"].id)
    db.refresh(card)

    assert first == {"paused": 1, "resumed": 0, "reopened": 0}
    assert card.ended_at is None and is_paused(card) is True
    assert card.status == STATUS_IN_PROGRESS
