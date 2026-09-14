"""Connecting teachers to the recordings pipeline: the admin surface and the rules.

``users.workspace_email`` is the activation switch for rooms, recording, invitations and the
group bot. These tests pin what the admin routes and the shared validation may and may not
do — who may read, who may write, what an address must look like, and that disconnecting is
a deliberate act, never a side effect of editing something else.
"""
import pytest

from src.announcements.models import TelegramGroupLink
from src.services import teacher_onboarding
from tests.test_operational_groups import _user, db, world  # noqa: F401 - fixtures


# ── suggestion + name rules ───────────────────────────────────────────────────────────────

def test_suggestion_uses_the_given_name_like_the_existing_accounts():
    taken = set()
    assert teacher_onboarding.suggest_workspace_email("Aisha Temirkhan", taken) == \
        "aisha@mastereducation.kz"
    # Kazakh order: Surname Given Patronymic → the middle word is the given name.
    assert teacher_onboarding.suggest_workspace_email("Есен Нұрғалы Беғалыұлы", taken) == \
        "nurgali@mastereducation.kz"
    # Two-word Cyrillic: Surname Given.
    assert teacher_onboarding.suggest_workspace_email("Кенжебаев Арсен", taken) == \
        "arsen@mastereducation.kz"


def test_suggestion_disambiguates_a_collision_with_the_surname():
    taken = {"nurai@mastereducation.kz"}
    assert teacher_onboarding.suggest_workspace_email("Нурай Бақытжанқызы", taken) == \
        "nurai.bakitjankizi@mastereducation.kz"


def test_surname_suffix_marks_the_other_word_as_given():
    """One -ов/-ин surname in the pair is decisive whichever order the name is written."""
    assert teacher_onboarding.given_name_word("Даниил Бутырин") == "daniil"
    assert teacher_onboarding.given_name_word("Жансерик Курбанов") == "janserik"
    assert teacher_onboarding.given_name_word("Махамаджанов Диербек") == "dierbek"
    assert teacher_onboarding.given_name_word("Нурай Кобейсин") == "nurai"
    # Patronymic still wins where present, surname suffix or not.
    assert teacher_onboarding.given_name_word("Исабеков Алпамыс Нургалиевич") == "alpamis"


def test_group_name_suffix_pins_the_given_name_only_when_unanchored():
    """"August 3 SAT - Киясбек" says Қиясбек is the given name — word order alone would
    have picked the surname. But a surname/patronymic in the name itself outranks a group
    tail, which may carry the surname ("- Исабеков") or two name words ("- Ақтай Мирас")."""
    assert teacher_onboarding.given_name_hint(
        "Қиясбек Мирас", ["August 3 SAT - Киясбек", "August 4 SAT - Киясбек"]) == "kiiasbek"
    assert teacher_onboarding.suggest_workspace_email(
        "Қиясбек Мирас", set(), group_names=["August 3 SAT - Киясбек"]) == \
        "kiiasbek@mastereducation.kz"
    assert teacher_onboarding.suggest_workspace_email(
        "Ерсултан Онталап", set(), group_names=["NUET September 1 2026 - Ерсултан"]) == \
        "ersultan@mastereducation.kz"
    # Anchored: the patronymic decides, the two-word tail is ignored either way.
    assert teacher_onboarding.suggest_workspace_email(
        "Ақтай Мирас Айқынұлы", set(), group_names=["August 9 SAT - Ақтай Мирас"]) == \
        "miras@mastereducation.kz"
    # Anchored: "- Исабеков" is the surname; the patronymic says given = Алпамыс.
    assert teacher_onboarding.suggest_workspace_email(
        "Исабеков Алпамыс Нургалиевич", set(),
        group_names=["August 6 SAT - Исабеков"]) == "alpamis@mastereducation.kz"
    # A tail matching two name words or none decides nothing.
    assert teacher_onboarding.given_name_hint("Ақтай Мирас", ["August 9 SAT - Ақтай Мирас"]) is None
    assert teacher_onboarding.given_name_hint("Қиясбек Мирас", ["Indi Asya SAT 2026"]) is None


def test_suggestion_is_none_when_the_name_has_no_letters():
    assert teacher_onboarding.suggest_workspace_email("—", set()) is None
    assert teacher_onboarding.suggest_workspace_email(None, set()) is None


# ── validation ────────────────────────────────────────────────────────────────────────────

def test_a_workspace_email_must_be_on_our_domain(db):
    teacher = _user(db, "teacher")
    with pytest.raises(ValueError):
        teacher_onboarding.validate_workspace_email(db, teacher, "teacher@gmail.com")


def test_only_teacher_roles_can_be_connected(db):
    student = _user(db, "student")
    with pytest.raises(ValueError):
        teacher_onboarding.validate_workspace_email(
            db, student, "student@mastereducation.kz")
    head = _user(db, "head_teacher")
    assert teacher_onboarding.validate_workspace_email(
        db, head, "head@mastereducation.kz") == "head@mastereducation.kz"


def test_an_address_cannot_be_claimed_twice(db):
    first = _user(db, "teacher")
    second = _user(db, "teacher")
    first.workspace_email = "taken@mastereducation.kz"
    db.flush()
    with pytest.raises(ValueError):
        teacher_onboarding.validate_workspace_email(db, second, "taken@mastereducation.kz")
    # Setting the same value on its owner is a no-op, not an error.
    assert teacher_onboarding.validate_workspace_email(
        db, first, "taken@mastereducation.kz") == "taken@mastereducation.kz"


def test_blank_and_case_fold_to_disconnect_or_lowercase(db):
    teacher = _user(db, "teacher")
    assert teacher_onboarding.validate_workspace_email(db, teacher, "  ") is None
    assert teacher_onboarding.validate_workspace_email(
        db, teacher, "  Said@MasterEducation.KZ ") == "said@mastereducation.kz"


# ── the list endpoint shape ────────────────────────────────────────────────────────────────

def _rows(db):
    from src.admin.routes.recordings import _teacher_rows
    return _teacher_rows(db)


def test_list_shows_connected_teachers_and_teachers_with_upcoming_lessons(world):
    db = world["db"]
    teacher = world["teacher"]
    group = world["group"](name="IELTS July 8 2026 - Gulzada")
    world["enrol"](group)
    world["lesson"](group, days_ahead=3)

    idle = _user(db, "teacher")           # no lessons, not connected → absent
    connected = _user(db, "teacher")      # no lessons but connected → present
    connected.workspace_email = "c@mastereducation.kz"
    db.flush()

    by_id = {row.id: row for row in _rows(db)}
    assert teacher.id in by_id and connected.id in by_id and idle.id not in by_id
    row = by_id[teacher.id]
    assert row.upcoming_lessons == 1 and row.rooms_ready == 0
    assert row.groups[0].name == "IELTS July 8 2026 - Gulzada"
    assert row.groups[0].telegram_linked is False
    assert row.suggested_workspace_email


def test_list_marks_telegram_links_and_meet_rooms(world):
    db = world["db"]
    teacher = world["teacher"]
    group = world["group"]()
    world["enrol"](group)
    db.add(TelegramGroupLink(lms_group_id=group.id, support_group_id=9001, chat_title="chat"))
    world["lesson"](group, days_ahead=2,
                    meeting_url="https://meet.google.com/abc-defg-hij")
    db.flush()

    row = next(r for r in _rows(db) if r.id == teacher.id)
    assert row.rooms_ready == 1
    assert row.groups[0].telegram_linked is True


def test_a_lesson_two_groups_share_counts_once_but_lists_both_groups(world):
    db = world["db"]
    teacher = world["teacher"]
    a, b = world["group"](name="a"), world["group"](name="b")
    world["enrol"](a); world["enrol"](b)
    world["lesson"](a, b, days_ahead=2)

    row = next(r for r in _rows(db) if r.id == teacher.id)
    assert row.upcoming_lessons == 1
    assert {g.id for g in row.groups} == {a.id, b.id}


# ── connect / disconnect ───────────────────────────────────────────────────────────────────

def test_set_workspace_email_connects_and_disconnects(world):
    db = world["db"]
    admin = _user(db, "admin")
    teacher = world["teacher"]

    teacher_onboarding.set_workspace_email(db, admin, teacher, "nurgali@mastereducation.kz")
    assert teacher.workspace_email == "nurgali@mastereducation.kz"

    teacher_onboarding.set_workspace_email(db, admin, teacher, None)
    assert teacher.workspace_email is None


def test_set_workspace_email_rejects_a_foreign_domain(world):
    db = world["db"]
    admin = _user(db, "admin")
    teacher = world["teacher"]
    with pytest.raises(ValueError):
        teacher_onboarding.set_workspace_email(db, admin, teacher, "t@gmail.com")
    assert teacher.workspace_email is None


# ── the CSV export ────────────────────────────────────────────────────────────────────────

def test_export_lists_only_pending_teachers_with_forced_password_change(world):
    import csv, io
    from src.admin.routes.recordings import export_workspace_import
    from fastapi import Response

    db = world["db"]
    admin = _user(db, "admin")
    teacher = world["teacher"]
    teacher.name = "Кенжебаев Арсен"
    connected = _user(db, "teacher")
    connected.workspace_email = "done@mastereducation.kz"
    world["group"](name="G")
    db.flush()

    resp = export_workspace_import(org_unit="/Teachers", db=db, current_user=admin)
    rows = list(csv.reader(io.StringIO(resp.body.decode() if hasattr(resp, "body") else resp)))
    header, data = rows[0], rows[1:]
    assert "Email Address [Required]" in header
    emails = [r[2] for r in data]
    assert "arsen@mastereducation.kz" in emails
    assert "done@mastereducation.kz" not in emails
    for r in data:
        assert r[4] == "/Teachers" and r[5] == "TRUE" and len(r[3]) >= 12
