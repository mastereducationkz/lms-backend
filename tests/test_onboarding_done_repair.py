"""The one-off repair for the 312 finished cards that never closed.

``done`` used to be a column rather than a decision: marking a card «Завершено» left the cycle
open until the relationship itself ended, so finished cards piled up in the last column of
every curator's board. :func:`~src.curator.onboarding_core.set_status` now closes them as they
are finished; this command is for the ones already there.

What these tests are really guarding is the *restraint*. A repair that closes more than it was
asked to is worse than no repair at all, so every skip has a test of its own, and the two
properties that make a repair safe to hand to a human — a dry run that writes nothing, and a
second run that does nothing — are pinned separately.
"""
from datetime import datetime, timedelta

import pytest

from src.curator.onboarding_core import (
    END_COMPLETED,
    END_RELATIONSHIP_ENDED,
    STATUS_DONE,
    STATUS_IN_PROGRESS,
    STATUS_NEW,
    OnboardingActor,
    close_cycle,
    load_board,
    open_cycle,
    set_status,
)
from src.curator.onboarding_done_repair import (
    VERDICT_ALREADY_CLOSED,
    VERDICT_BASELINE,
    VERDICT_CLOSE,
    VERDICT_NOT_DONE,
    apply_repair,
    main,
    render_table,
    scan,
)
from src.curator.onboarding_service import reconcile_onboarding
from src.schemas.models import CuratorOnboarding, Group, GroupStudent, UserInDB
from src.utils.auth_utils import hash_password
from tests.onboarding_fixtures import db  # noqa: F401

_seq = 0


def _uniq() -> int:
    global _seq
    _seq += 1
    return _seq


def _user(db, role: str) -> UserInDB:  # noqa: F811
    user = UserInDB(
        email=f"done-repair-{role}{_uniq()}@test.local",
        name=f"Done {role}",
        role=role,
        hashed_password=hash_password("x"),
        is_active=True,
    )
    db.add(user)
    db.flush()
    return user


def _group(db, curator) -> Group:  # noqa: F811
    group = Group(
        name=f"Done Repair G{_uniq()}",
        is_active=True,
        is_over=False,
        curator_id=curator.id,
        program_type="sat",
        schedule_config={"schedule_items": []},
    )
    db.add(group)
    db.flush()
    return group


def _enrol(db, group, student):  # noqa: F811
    db.add(
        GroupStudent(
            group_id=group.id,
            student_id=student.id,
            created_at=datetime.utcnow() - timedelta(days=60),
        )
    )
    db.flush()


def _finished_open_card(db, curator, student, group) -> CuratorOnboarding:  # noqa: F811
    """A card in «Завершено» that stayed open — the shape the 312 rows have.

    Written directly rather than through ``set_status``, which now closes the cycle: this is
    a row created under the *old* rule, which is the only kind this command exists for.
    """
    row = open_cycle(db, curator.id, student.id, group.id)
    row.status = STATUS_DONE
    row.completed_at = datetime.utcnow() - timedelta(days=30)
    row.completed_by = curator.id
    db.flush()
    return row


def _mine(db, card_id, **kwargs):  # noqa: F811
    return [f for f in scan(db, **kwargs) if f.onboarding_id == card_id]


# --- verdicts -----------------------------------------------------------------------------


def test_it_closes_a_finished_open_card(db):  # noqa: F811
    curator, student = _user(db, "curator"), _user(db, "student")
    group = _group(db, curator)
    _enrol(db, group, student)
    card = _finished_open_card(db, curator, student, group)

    mine = _mine(db, card.id)
    assert [f.verdict for f in mine] == [VERDICT_CLOSE]
    assert mine[0].reopens is False, "the card names the group the student is in"

    assert [f.onboarding_id for f in apply_repair(db, mine)] == [card.id]
    db.refresh(card)
    assert card.ended_at is not None
    assert card.end_reason == END_COMPLETED
    assert card.status == STATUS_DONE, "a finished cycle is never rewritten to cancelled"
    assert "cycle.closed" in {e.action for e in card.events}
    assert card.id not in {r.id for r in load_board(db, curator_ids=[curator.id])}


def test_it_skips_a_card_still_in_flight(db):  # noqa: F811
    curator, student = _user(db, "curator"), _user(db, "student")
    group = _group(db, curator)
    _enrol(db, group, student)
    card = open_cycle(db, curator.id, student.id, group.id)
    set_status(db, card, STATUS_IN_PROGRESS, OnboardingActor.from_user(curator), commit=False)
    db.flush()

    assert [f.verdict for f in _mine(db, card.id)] == []  # not even a candidate: not done
    # Asked about by id, it says why rather than vanishing.
    by_id = [f for f in scan(db, only_ids=[card.id])]
    assert [f.verdict for f in by_id] == [VERDICT_NOT_DONE]
    assert apply_repair(db, by_id) == []
    db.refresh(card)
    assert card.ended_at is None


def test_it_skips_a_launch_baseline_row(db):  # noqa: F811
    """``done`` with no actioner is a seed, not an achievement. It is left exactly as it is."""
    curator, student = _user(db, "curator"), _user(db, "student")
    group = _group(db, curator)
    _enrol(db, group, student)
    card = open_cycle(db, curator.id, student.id, group.id)
    card.status = STATUS_DONE
    card.completed_by = None
    db.flush()

    mine = _mine(db, card.id)
    assert [f.verdict for f in mine] == [VERDICT_BASELINE]
    assert apply_repair(db, mine) == []
    db.refresh(card)
    assert card.ended_at is None


def test_it_skips_an_already_closed_card_and_is_idempotent(db):  # noqa: F811
    curator, student = _user(db, "curator"), _user(db, "student")
    group = _group(db, curator)
    _enrol(db, group, student)
    card = _finished_open_card(db, curator, student, group)

    apply_repair(db, _mine(db, card.id))
    db.refresh(card)
    ended_at = card.ended_at
    assert ended_at is not None

    second = _mine(db, card.id)
    assert [f.verdict for f in second] == [VERDICT_ALREADY_CLOSED]
    assert apply_repair(db, second) == []
    db.refresh(card)
    assert card.ended_at == ended_at, "a second run must not rewrite the close"


def test_a_card_closed_for_another_reason_is_left_alone(db):  # noqa: F811
    """Its history says the relationship ended; the repair does not restate it as completed."""
    curator, student = _user(db, "curator"), _user(db, "student")
    group = _group(db, curator)
    _enrol(db, group, student)
    card = _finished_open_card(db, curator, student, group)
    close_cycle(db, card, END_RELATIONSHIP_ENDED)
    db.flush()

    assert [f.verdict for f in _mine(db, card.id)] == [VERDICT_ALREADY_CLOSED]
    assert apply_repair(db, _mine(db, card.id)) == []
    db.refresh(card)
    assert card.end_reason == END_RELATIONSHIP_ENDED


# --- safety -------------------------------------------------------------------------------


def test_a_dry_run_writes_nothing(db, monkeypatch, capsys):  # noqa: F811
    curator, student = _user(db, "curator"), _user(db, "student")
    group = _group(db, curator)
    _enrol(db, group, student)
    card = _finished_open_card(db, curator, student, group)
    db.commit()

    import src.config

    # ``main`` closes the session it opened. The fixture's session outlives the command, so
    # the close is neutralised rather than letting it detach every row from under the test.
    monkeypatch.setattr(db, "close", lambda: None)
    monkeypatch.setattr(src.config, "SessionLocal", lambda: db)
    assert main([]) == 0

    out = capsys.readouterr().out
    assert "DRY RUN" in out and str(card.id) in out
    db.refresh(card)
    assert card.ended_at is None, "the default must never write"


def test_apply_and_dry_run_together_are_refused(db):  # noqa: F811
    assert main(["--apply", "--dry-run"]) == 2


def test_it_rechecks_before_writing(db):  # noqa: F811
    """The table a human reads can be minutes stale; the write must not trust it."""
    curator, student = _user(db, "curator"), _user(db, "student")
    group = _group(db, curator)
    _enrol(db, group, student)
    card = _finished_open_card(db, curator, student, group)

    mine = _mine(db, card.id)
    assert [f.verdict for f in mine] == [VERDICT_CLOSE]

    # Somebody moves the card back to «В работе» between the report and the --apply.
    card.status = STATUS_IN_PROGRESS
    db.flush()

    assert apply_repair(db, mine) == []
    db.refresh(card)
    assert card.ended_at is None
    assert mine[0].verdict == VERDICT_NOT_DONE, "the report says why it was skipped"


def test_the_table_shows_the_evidence_for_each_verdict(db):  # noqa: F811
    curator, student = _user(db, "curator"), _user(db, "student")
    group = _group(db, curator)
    _enrol(db, group, student)
    card = _finished_open_card(db, curator, student, group)

    table = render_table(_mine(db, card.id))
    assert str(card.id) in table
    assert str(curator.id) in table and group.name in table
    assert VERDICT_CLOSE in table
    assert "would close 1 card(s)" in table


# --- what happens next --------------------------------------------------------------------


def test_closing_does_not_hand_the_reconciler_a_fresh_card(db):  # noqa: F811
    """The risk that matters: 312 cards leaving «Завершено» must not arrive in «Новые»."""
    curator, student = _user(db, "curator"), _user(db, "student")
    group = _group(db, curator)
    _enrol(db, group, student)
    card = _finished_open_card(db, curator, student, group)

    apply_repair(db, _mine(db, card.id))
    reconcile_onboarding(db)
    reconcile_onboarding(db)

    rows = (
        db.query(CuratorOnboarding)
        .filter(CuratorOnboarding.curator_id == curator.id)
        .all()
    )
    assert [r.id for r in rows] == [card.id], "no cycle 2 for a student already onboarded"


def test_a_card_naming_a_group_the_student_has_left_is_flagged(db):  # noqa: F811
    """The one shape that *does* produce a new card, reported before it happens.

    Starting a second course with the same curator is a real new onboarding — the re-open
    guard deliberately allows it. That is the right rule and the wrong surprise, so the table
    says «reopens: YES» and the summary counts them.
    """
    curator, student = _user(db, "curator"), _user(db, "student")
    finished, fresh = _group(db, curator), _group(db, curator)
    _enrol(db, fresh, student)
    card = _finished_open_card(db, curator, student, finished)

    mine = _mine(db, card.id)
    assert mine[0].verdict == VERDICT_CLOSE
    assert mine[0].reopens is True
    assert mine[0].live_group_id == fresh.id
    table = render_table(mine)
    assert "YES" in table and "«reopens: YES» rows" in table, "the warning is impossible to miss"

    apply_repair(db, mine)
    reconcile_onboarding(db)

    rows = sorted(
        db.query(CuratorOnboarding)
        .filter(CuratorOnboarding.curator_id == curator.id)
        .all(),
        key=lambda r: r.cycle_no,
    )
    assert len(rows) == 2 and rows[1].status == STATUS_NEW
    assert rows[1].group_id == fresh.id, "the new card is about the course they are on now"


@pytest.mark.parametrize("status", [STATUS_NEW, STATUS_IN_PROGRESS])
def test_only_done_cards_are_ever_candidates(db, status):  # noqa: F811
    curator, student = _user(db, "curator"), _user(db, "student")
    group = _group(db, curator)
    _enrol(db, group, student)
    card = open_cycle(db, curator.id, student.id, group.id)
    card.status = status
    db.flush()

    assert card.id not in {f.onboarding_id for f in scan(db)}
