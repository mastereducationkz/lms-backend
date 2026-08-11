"""Onboarding cycle lifecycle, reconciliation and visibility rules.

These are the rules the CRM workspace depends on, so they are tested here, at the layer
that owns them, rather than through the CRM's HTTP surface.
"""
from datetime import datetime, timedelta, timezone

import pytest

from src.curator.onboarding_core import (
    END_RELATIONSHIP_ENDED,
    IN_PROGRESS_STALE_DAYS,
    NEW_OVERDUE_DAYS,
    STATUS_CANCELLED,
    STATUS_DONE,
    STATUS_IN_PROGRESS,
    STATUS_NEW,
    OnboardingActor,
    OnboardingPermissionError,
    active_cycle,
    add_note,
    close_cycle,
    curator_student_ids,
    get_card,
    is_overdue,
    load_board,
    open_cycle,
    reconcile_onboarding,
    reconcile_student,
    set_next_action,
    set_status,
)
from src.schemas.models import (
    CuratorOnboarding,
    CuratorOnboardingEvent,
    CuratorOnboardingNote,
    Group,
    GroupStudent,
    UserInDB,
)


@pytest.fixture
def db():
    """A session whose work is discarded, in which real ``commit()`` calls still work.

    The reconciler commits — that is part of what is under test — so the older
    ``begin_nested`` + ``after_transaction_end`` recipe used elsewhere in this suite is not
    usable here: an application commit tears down the savepoint the recipe is rebuilding.
    ``join_transaction_mode="create_savepoint"`` is SQLAlchemy 2.0's supported answer: the
    session's commits land in a savepoint on a connection-level transaction that this
    fixture always rolls back.
    """
    from sqlalchemy.exc import OperationalError
    from sqlalchemy.orm import Session as SASession

    from src.config import engine

    try:
        connection = engine.connect()
    except OperationalError:
        pytest.skip("No database available")
    trans = connection.begin()
    session = SASession(bind=connection, join_transaction_mode="create_savepoint")
    try:
        yield session
    finally:
        session.close()
        trans.rollback()
        connection.close()


_seq = [0]


def _uniq(prefix: str) -> str:
    _seq[0] += 1
    return f"{prefix}-{_seq[0]}-{datetime.now().timestamp():.6f}"


def _user(db, role: str, name: str = "U") -> UserInDB:
    from src.utils.auth_utils import hash_password

    u = UserInDB(
        email=f"{_uniq('ob')}@test.local",
        name=name,
        role=role,
        hashed_password=hash_password("x"),
        is_active=True,
    )
    db.add(u)
    db.flush()
    return u


def _group(db, curator: UserInDB | None, name: str = "G", **kw) -> Group:
    g = Group(
        name=_uniq(name),
        curator_id=curator.id if curator else None,
        is_active=kw.pop("is_active", True),
        is_over=kw.pop("is_over", False),
        program_type=kw.pop("program_type", "SAT"),
        **kw,
    )
    db.add(g)
    db.flush()
    return g


def _enrol(db, group: Group, student: UserInDB) -> GroupStudent:
    gs = GroupStudent(
        group_id=group.id, student_id=student.id, created_at=datetime.utcnow()
    )
    db.add(gs)
    db.flush()
    return gs


def _actor(user: UserInDB) -> OnboardingActor:
    return OnboardingActor.from_user(user)


# --- cycles -------------------------------------------------------------------------------


def test_open_cycle_is_idempotent(db):
    curator, student = _user(db, "curator"), _user(db, "student")
    g = _group(db, curator)
    first = open_cycle(db, curator.id, student.id, g.id)
    db.flush()
    assert first is not None and first.cycle_no == 1 and first.ended_at is None
    assert open_cycle(db, curator.id, student.id, g.id) is None


def test_returning_student_gets_a_new_cycle_not_a_revived_row(db):
    """The whole reason the lifetime unique constraint had to go."""
    curator, student = _user(db, "curator"), _user(db, "student")
    g = _group(db, curator)
    first = open_cycle(db, curator.id, student.id, g.id)
    db.flush()
    first_id = first.id
    set_status(db, first, STATUS_DONE, _actor(curator), commit=False)
    close_cycle(db, first, END_RELATIONSHIP_ENDED)
    db.flush()

    second = open_cycle(db, curator.id, student.id, g.id)
    db.flush()
    assert second is not None
    assert second.id != first_id, "must be a new row, not the old one reopened"
    assert second.cycle_no == 2
    assert second.status == STATUS_NEW
    # History preserved intact.
    old = db.query(CuratorOnboarding).filter(CuratorOnboarding.id == first_id).one()
    assert old.status == STATUS_DONE and old.ended_at is not None


def test_close_cycle_preserves_done_but_cancels_in_flight(db):
    curator, s1, s2 = _user(db, "curator"), _user(db, "student"), _user(db, "student")
    g = _group(db, curator)
    done = open_cycle(db, curator.id, s1.id, g.id)
    flight = open_cycle(db, curator.id, s2.id, g.id)
    db.flush()
    set_status(db, done, STATUS_DONE, _actor(curator), commit=False)

    assert close_cycle(db, done, END_RELATIONSHIP_ENDED) is True
    assert close_cycle(db, flight, END_RELATIONSHIP_ENDED) is True
    db.flush()
    assert done.status == STATUS_DONE
    assert flight.status == STATUS_CANCELLED
    # Idempotent.
    assert close_cycle(db, done, END_RELATIONSHIP_ENDED) is False


# --- reconciler ---------------------------------------------------------------------------


def test_reconcile_creates_closes_and_is_idempotent(db):
    curator, student = _user(db, "curator"), _user(db, "student")
    g = _group(db, curator)
    _enrol(db, g, student)

    r1 = reconcile_onboarding(db)
    assert r1["created"] >= 1
    row = active_cycle(db, curator.id, student.id)
    assert row is not None and row.status == STATUS_NEW

    r2 = reconcile_onboarding(db)
    assert r2["created"] == 0, "second run must be a no-op for this pair"

    db.query(GroupStudent).filter(
        GroupStudent.group_id == g.id, GroupStudent.student_id == student.id
    ).delete()
    db.flush()
    reconcile_onboarding(db)
    assert active_cycle(db, curator.id, student.id) is None
    closed = (
        db.query(CuratorOnboarding)
        .filter(
            CuratorOnboarding.curator_id == curator.id,
            CuratorOnboarding.student_id == student.id,
        )
        .one()
    )
    assert closed.status == STATUS_CANCELLED and closed.end_reason == END_RELATIONSHIP_ENDED


def test_completed_group_is_not_a_live_relationship(db):
    """`is_over` groups are finished cohorts — nobody's onboarding queue."""
    curator, student = _user(db, "curator"), _user(db, "student")
    g = _group(db, curator, is_over=True)
    _enrol(db, g, student)
    reconcile_onboarding(db)
    assert active_cycle(db, curator.id, student.id) is None


def test_source_cycle_survives_while_another_source_group_remains(db):
    """Transfer out of *one* of a curator's groups must not end their responsibility."""
    curator, student = _user(db, "curator"), _user(db, "student")
    g1, g2 = _group(db, curator, "A"), _group(db, curator, "B")
    _enrol(db, g1, student)
    _enrol(db, g2, student)
    reconcile_student(db, student.id)
    assert active_cycle(db, curator.id, student.id) is not None

    db.query(GroupStudent).filter(
        GroupStudent.group_id == g1.id, GroupStudent.student_id == student.id
    ).delete()
    db.flush()
    reconcile_student(db, student.id)
    assert active_cycle(db, curator.id, student.id) is not None, (
        "still in group B owned by the same curator"
    )

    db.query(GroupStudent).filter(
        GroupStudent.group_id == g2.id, GroupStudent.student_id == student.id
    ).delete()
    db.flush()
    reconcile_student(db, student.id)
    assert active_cycle(db, curator.id, student.id) is None


def test_cross_curator_transfer_opens_destination_cycle_and_ends_source(db):
    src, dst, student = _user(db, "curator"), _user(db, "curator"), _user(db, "student")
    g_src, g_dst = _group(db, src, "S"), _group(db, dst, "D")
    _enrol(db, g_src, student)
    reconcile_student(db, student.id)
    assert active_cycle(db, src.id, student.id) is not None

    db.query(GroupStudent).filter(
        GroupStudent.group_id == g_src.id, GroupStudent.student_id == student.id
    ).delete()
    _enrol(db, g_dst, student)
    db.flush()
    reconcile_student(db, student.id)

    assert active_cycle(db, src.id, student.id) is None
    assert active_cycle(db, dst.id, student.id) is not None


def test_shared_student_has_independent_cycles_per_curator(db):
    c1, c2, student = _user(db, "curator"), _user(db, "curator"), _user(db, "student")
    _enrol(db, _group(db, c1, "one"), student)
    _enrol(db, _group(db, c2, "two"), student)
    reconcile_student(db, student.id)

    r1 = active_cycle(db, c1.id, student.id)
    r2 = active_cycle(db, c2.id, student.id)
    assert r1 is not None and r2 is not None and r1.id != r2.id

    set_status(db, r1, STATUS_DONE, _actor(c1), commit=False)
    db.flush()
    assert r2.status == STATUS_NEW, "one curator finishing must not finish the other"


def test_concurrent_open_cannot_create_two_active_cycles(db):
    """The partial unique index, not application timing, is what guarantees this."""
    from sqlalchemy.exc import IntegrityError

    curator, student = _user(db, "curator"), _user(db, "student")
    g = _group(db, curator)
    open_cycle(db, curator.id, student.id, g.id)
    db.flush()

    # Simulate the racing writer that checked before the winner committed.
    duplicate = CuratorOnboarding(
        curator_id=curator.id, student_id=student.id, group_id=g.id,
        status=STATUS_NEW, cycle_no=2,
    )
    db.add(duplicate)
    with pytest.raises(IntegrityError):
        db.flush()
    db.rollback()


# --- authorization ------------------------------------------------------------------------


def test_curator_cannot_touch_another_curators_card(db):
    owner, other, student = _user(db, "curator"), _user(db, "curator"), _user(db, "student")
    row = open_cycle(db, owner.id, student.id, _group(db, owner).id)
    db.flush()

    with pytest.raises(OnboardingPermissionError):
        set_status(db, row, STATUS_DONE, _actor(other), commit=False)

    # And it must not even confirm the card exists.
    from src.curator.onboarding_core import OnboardingNotFound

    with pytest.raises(OnboardingNotFound):
        get_card(db, row.id, _actor(other))


def test_head_curator_intervention_is_attributed_without_taking_ownership(db):
    owner, head, student = _user(db, "curator"), _user(db, "head_curator"), _user(db, "student")
    row = open_cycle(db, owner.id, student.id, _group(db, owner).id)
    db.flush()

    set_status(db, row, STATUS_IN_PROGRESS, _actor(head), commit=False)
    db.flush()
    assert row.curator_id == owner.id, "oversight is not a transfer of ownership"
    events = (
        db.query(CuratorOnboardingEvent)
        .filter(CuratorOnboardingEvent.onboarding_id == row.id)
        .all()
    )
    assert any(e.action == "intervention" and e.actor_id == head.id for e in events)


def test_notes_record_author_and_belong_to_the_cycle(db):
    owner, head, student = _user(db, "curator"), _user(db, "head_curator"), _user(db, "student")
    row = open_cycle(db, owner.id, student.id, _group(db, owner).id)
    db.flush()
    add_note(db, row, "Позвонил родителям", _actor(owner), commit=False)
    add_note(db, row, "Проверил", _actor(head), commit=False)
    db.flush()

    notes = (
        db.query(CuratorOnboardingNote)
        .filter(CuratorOnboardingNote.onboarding_id == row.id)
        .order_by(CuratorOnboardingNote.id)
        .all()
    )
    assert [n.author_id for n in notes] == [owner.id, head.id]
    assert [n.author_role for n in notes] == ["curator", "head_curator"]


def test_note_from_a_third_curator_is_refused(db):
    owner, other, student = _user(db, "curator"), _user(db, "curator"), _user(db, "student")
    row = open_cycle(db, owner.id, student.id, _group(db, owner).id)
    db.flush()
    with pytest.raises(OnboardingPermissionError):
        add_note(db, row, "не моё", _actor(other), commit=False)


def test_status_change_on_a_closed_cycle_is_refused(db):
    curator, student = _user(db, "curator"), _user(db, "student")
    row = open_cycle(db, curator.id, student.id, _group(db, curator).id)
    db.flush()
    close_cycle(db, row, END_RELATIONSHIP_ENDED)
    db.flush()
    with pytest.raises(OnboardingPermissionError):
        set_status(db, row, STATUS_DONE, _actor(curator), commit=False)


# --- thresholds ---------------------------------------------------------------------------


def test_overdue_thresholds(db):
    curator, s1, s2, s3 = (
        _user(db, "curator"), _user(db, "student"), _user(db, "student"), _user(db, "student")
    )
    g = _group(db, curator)
    now = datetime.utcnow()

    fresh = open_cycle(db, curator.id, s1.id, g.id)
    stale_new = open_cycle(db, curator.id, s2.id, g.id)
    stale_prog = open_cycle(db, curator.id, s3.id, g.id)
    db.flush()

    stale_new.created_at = now - timedelta(days=NEW_OVERDUE_DAYS + 1)
    stale_prog.status = STATUS_IN_PROGRESS
    stale_prog.status_changed_at = now - timedelta(days=IN_PROGRESS_STALE_DAYS + 1)
    db.flush()

    assert is_overdue(fresh, now) is False
    assert is_overdue(stale_new, now) is True
    assert is_overdue(stale_prog, now) is True

    # A finished card is never overdue, however old.
    stale_prog.status = STATUS_DONE
    assert is_overdue(stale_prog, now) is False


def test_next_action_roundtrip(db):
    from datetime import date

    curator, student = _user(db, "curator"), _user(db, "student")
    row = open_cycle(db, curator.id, student.id, _group(db, curator).id)
    db.flush()
    target = date.today() + timedelta(days=3)
    set_next_action(db, row, target, "перезвонить", _actor(curator), commit=False)
    db.flush()
    assert row.next_action_at == target and row.next_action_note == "перезвонить"


# --- board / scope ------------------------------------------------------------------------


def test_board_hides_launch_baseline_rows(db):
    """Synthetic 'done, completed_by NULL' seeds must stay off the board."""
    curator, s1, s2 = _user(db, "curator"), _user(db, "student"), _user(db, "student")
    g = _group(db, curator)
    baseline = open_cycle(db, curator.id, s1.id, g.id)
    real = open_cycle(db, curator.id, s2.id, g.id)
    db.flush()
    baseline.status = STATUS_DONE
    baseline.completed_by = None
    set_status(db, real, STATUS_DONE, _actor(curator), commit=False)
    db.flush()

    ids = {r.id for r in load_board(db, curator_ids=[curator.id])}
    assert real.id in ids
    assert baseline.id not in ids


def test_curator_student_ids_is_the_visibility_primitive(db):
    curator, other, mine, theirs = (
        _user(db, "curator"), _user(db, "curator"), _user(db, "student"), _user(db, "student")
    )
    _enrol(db, _group(db, curator, "mine"), mine)
    _enrol(db, _group(db, other, "theirs"), theirs)
    db.flush()

    visible = curator_student_ids(db, [curator.id])
    assert mine.id in visible
    assert theirs.id not in visible
    assert curator_student_ids(db, []) == set()


def test_completed_group_students_drop_out_of_scope(db):
    curator, student = _user(db, "curator"), _user(db, "student")
    g = _group(db, curator, is_over=True)
    _enrol(db, g, student)
    db.flush()
    assert student.id not in curator_student_ids(db, [curator.id])
