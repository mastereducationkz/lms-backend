import pytest
from fastapi import HTTPException

from tests.checkpoint_fixtures import (
    make_user, make_group, enroll, make_sat_course, make_quiz_lessons, make_definition,
    complete_lesson_explicit, complete_checkpoint,
)


@pytest.fixture
def db():
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
        session.close(); trans.rollback(); connection.close()


def _world(db, enabled=True, n_blocks=2):
    """`n_blocks` checkpoint blocks (2 verbal + 1 math unit each) plus one unbound tail unit.

    Block i (1-indexed) = v[2i-2], v[2i-1], m[i-1]. E.g. for n_blocks=2: Block 1 = v[0], v[1],
    m[0]; Block 2 = v[2], v[3], m[1]; v[4] belongs to no checkpoint.

    The tail verbal unit is marked initially-unlocked: later blocks' units are deliberately left
    incomplete in these tests (that's the point — an uncleared checkpoint holds them back), so the
    module's own sequential-access chain would otherwise also lock the tail unit behind them for a
    reason that has nothing to do with checkpoints, making it impossible to tell the two apart in
    the listing-level test.
    """
    n_verbal = 2 * n_blocks + 1
    n_math = n_blocks
    admin = make_user(db, role="admin")
    course, v, m = make_sat_course(db, n_verbal=n_verbal, n_math=n_math)
    v[-1].is_initially_unlocked = True
    _, quiz_lessons, _ = make_quiz_lessons(db, course, n_blocks)
    defs = [
        make_definition(db, course, i + 1, v[2 * i:2 * i + 2], m[i], quiz_lessons[i])
        for i in range(n_blocks)
    ]
    group = make_group(db, enabled=enabled)
    student = make_user(db)
    enroll(db, student, group, course, admin)
    db.flush()
    return admin, course, v, m, defs, group, student


def test_block_one_never_blocked_but_block_two_is_with_no_checkpoint_progress(db):
    """No StudentCheckpoint row exists at all yet (checkpoint 1 was never even opened). Block 1's
    own units are never blocked (nothing precedes them); block 2's are, because checkpoint 1 is
    not cleared."""
    from src.checkpoints import service
    _, course, v, m, defs, group, student = _world(db)
    blocked = service.blocked_unit_lesson_ids_for_student(db, student.id)
    assert blocked.isdisjoint({v[0].id, v[1].id, m[0].id})
    assert {v[2].id, v[3].id, m[1].id} <= blocked


def test_skipped_required_unit_never_opens_checkpoint_but_still_blocks_next_block(db):
    """The reported bug: a student who completes the verbal units of a block but skips its math
    unit never opens that block's checkpoint (auto-open needs ALL required units), so nothing is
    ever 'pending' under the old status-based rule and the next block's units ran open unchecked.
    Under the ordinal rule, checkpoint 1 is simply not cleared, so block 2 stays blocked."""
    from src.checkpoints import service
    _, course, v, m, defs, group, student = _world(db)
    complete_lesson_explicit(db, student, course, v[0])
    complete_lesson_explicit(db, student, course, v[1])
    # m[0] (the math unit) is deliberately never done.
    service.sync_student_checkpoints(db, student.id)
    assert service.get_row(db, student.id, group.id, defs[0].id) is None  # checkpoint 1 never opened

    blocked = service.blocked_unit_lesson_ids_for_student(db, student.id)
    assert {v[2].id, v[3].id, m[1].id} <= blocked
    definition = service.blocking_checkpoint_for_student(db, student.id)
    assert definition is not None and definition.id == defs[0].id


def test_open_checkpoint_blocks_later_bound_units_only(db):
    """Checkpoint 1 auto-opens (status 'available') once its units are done, but 'available' is
    not 'cleared' — it still holds block 2 back until it is actually submitted."""
    from src.checkpoints import service
    _, course, v, m, defs, group, student = _world(db)
    for lesson in (v[0], v[1], m[0]):
        complete_lesson_explicit(db, student, course, lesson)
    service.sync_student_checkpoints(db, student.id)
    assert service.get_row(db, student.id, group.id, defs[0].id).status == "available"

    blocked = service.blocked_unit_lesson_ids_for_student(db, student.id)
    # block 2's units are bound to a checkpoint and its predecessor isn't cleared -> blocked
    assert {v[2].id, v[3].id, m[1].id} <= blocked
    # already-completed units stay open for review
    assert blocked.isdisjoint({v[0].id, v[1].id, m[0].id})
    # a unit bound to no checkpoint is never blocked
    assert v[4].id not in blocked
    # the checkpoint's own quiz lesson is not a "unit"
    assert defs[0].quiz_lesson_id not in blocked


def test_completing_the_checkpoint_clears_the_block(db):
    from src.checkpoints import service
    _, course, v, m, defs, group, student = _world(db)
    for lesson in (v[0], v[1], m[0]):
        complete_lesson_explicit(db, student, course, lesson)
    service.sync_student_checkpoints(db, student.id)
    assert service.blocked_unit_lesson_ids_for_student(db, student.id)

    complete_checkpoint(db, student, defs[0])
    assert service.blocked_unit_lesson_ids_for_student(db, student.id) == set()


def test_overdue_checkpoint_releases_the_block(db):
    """A lapsed checkpoint counts as cleared too — once the 72h deadline passes without a
    submission, the student is free to move on to later checkpoint-bound units rather than being
    stranded; the missed checkpoint itself stays unsubmittable (assert_can_submit) until an admin
    calls reopen_for_students. This is deliberate: waiting out the deadline is a known way past
    the gate, traded off against never leaving a student stuck."""
    from datetime import timedelta
    from src.checkpoints import service
    admin, course, v, m, defs, group, student = _world(db)
    for lesson in (v[0], v[1], m[0]):
        complete_lesson_explicit(db, student, course, lesson)
    opened = service.utcnow() - timedelta(hours=service.DEADLINE_HOURS + 24)
    service.open_for_students(db, group=group, definition=defs[0], student_ids=[student.id],
                              deadline=opened + timedelta(hours=service.DEADLINE_HOURS),
                              actor_id=admin.id, now=opened)
    row = service.get_row(db, student.id, group.id, defs[0].id)
    service.refresh_overdue([row])
    db.flush()
    assert row.status == "overdue"
    assert service.blocked_unit_lesson_ids_for_student(db, student.id) == set()


def test_disabled_group_blocks_nothing(db):
    from src.checkpoints import service
    admin, course, v, m, defs, group, student = _world(db, enabled=False)
    for lesson in (v[0], v[1], m[0]):
        complete_lesson_explicit(db, student, course, lesson)
    service.open_for_students(db, group=group, definition=defs[0], student_ids=[student.id],
                              actor_id=admin.id)
    group.checkpoints_enabled = False
    db.flush()
    assert service.blocked_unit_lesson_ids_for_student(db, student.id) == set()


def test_uncleared_checkpoint_one_blocks_both_later_blocks(db):
    """A gap at the front of the chain holds back every block behind it, not just the next one."""
    from src.checkpoints import service
    _, course, v, m, defs, group, student = _world(db, n_blocks=3)
    for lesson in (v[0], v[1], m[0]):
        complete_lesson_explicit(db, student, course, lesson)
    service.sync_student_checkpoints(db, student.id)  # checkpoint 1 opens but is not submitted

    blocked = service.blocked_unit_lesson_ids_for_student(db, student.id)
    assert {v[2].id, v[3].id, m[1].id} <= blocked   # block 2
    assert {v[4].id, v[5].id, m[2].id} <= blocked   # block 3


def test_clearing_checkpoint_one_opens_block_two_but_block_three_stays_blocked(db):
    """Clearing checkpoint 1 advances the gate by exactly one block: block 2 opens, block 3 (gated
    on checkpoint 2, which is still not cleared) does not. blocking_checkpoint_for_student advances
    to checkpoint 2 accordingly."""
    from src.checkpoints import service
    _, course, v, m, defs, group, student = _world(db, n_blocks=3)
    for lesson in (v[0], v[1], m[0]):
        complete_lesson_explicit(db, student, course, lesson)
    service.sync_student_checkpoints(db, student.id)
    complete_checkpoint(db, student, defs[0])

    blocked = service.blocked_unit_lesson_ids_for_student(db, student.id)
    assert blocked.isdisjoint({v[2].id, v[3].id, m[1].id})   # block 2 now open
    assert {v[4].id, v[5].id, m[2].id} <= blocked            # block 3 still blocked

    definition = service.blocking_checkpoint_for_student(db, student.id)
    assert definition is not None and definition.id == defs[1].id


def test_listing_and_check_access_agree(db):
    from src.courses.routes.courses import get_course_modules, check_lesson_access
    from src.checkpoints import service
    _, course, v, m, defs, group, student = _world(db)
    for lesson in (v[0], v[1], m[0]):
        complete_lesson_explicit(db, student, course, lesson)
    service.sync_student_checkpoints(db, student.id)

    mods = get_course_modules(course.id, include_lessons=True, student_id=None,
                              current_user=student, db=db)
    rows = {l["id"]: l for mod in mods for l in (mod["lessons"] if isinstance(mod, dict) else mod.lessons)}
    assert rows[v[2].id]["is_accessible"] is False
    assert rows[v[4].id]["is_accessible"] is not False      # unbound unit untouched

    out = check_lesson_access(v[2].id, current_user=student, db=db)
    assert out["accessible"] is False and "Checkpoint 1" in out["reason"]


def test_blocked_unit_reads_are_refused(db):
    from src.courses.routes.courses import get_lesson_steps
    from src.checkpoints import service
    _, course, v, m, defs, group, student = _world(db)
    for lesson in (v[0], v[1], m[0]):
        complete_lesson_explicit(db, student, course, lesson)
    service.sync_student_checkpoints(db, student.id)
    with pytest.raises(HTTPException) as e:
        get_lesson_steps(v[2].id, include_content=True, current_user=student, db=db)
    assert e.value.status_code == 403 and "Checkpoint 1" in str(e.value.detail)
