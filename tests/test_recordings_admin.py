"""Connecting teachers to the recordings pipeline: the admin surface and the rules.

``users.workspace_email`` is the activation switch for rooms, recording, invitations and the
group bot. These tests pin what the admin routes and the shared rules may and may not do: the
English names and addresses the page proposes, that an address must exist in the uploaded
Workspace users list before it is connected, and that the Google import can never overwrite an
account that already exists.
"""
import csv
import io

import pytest
from fastapi import HTTPException

from src.announcements.models import TelegramGroupLink
from src.services import teacher_onboarding, workspace_directory, workspace_names
from tests.test_operational_groups import _user, db, world  # noqa: F401 - fixtures

DOMAIN = "@mastereducation.kz"


def _directory(db, *accounts):
    """Store a users list. Each account is an address or (address, first, last, extra...)."""
    rows = []
    for account in accounts:
        email, first, last, extra = (account, "", "", {}) if isinstance(account, str) else (
            account[0], account[1], account[2], account[3] if len(account) > 3 else {})
        rows.append({"email": email, "first_name": first, "last_name": last, "org_unit": "/Teachers",
                     "suspended": False, "signed_in": True, **extra})
    workspace_directory.store(db, None, rows)


# ── English names (owner decision 2026-09-14: passport style) ─────────────────────────────

@pytest.mark.parametrize("cyrillic, latin", [
    ("Жансерик", "zhanserik"), ("Нұрғалы", "nurgaly"), ("Ерполат", "yerpolat"), ("Нурай", "nuray"),
    ("Айша", "aisha"), ("Кенжебаев", "kenzhebayev"), ("Хамит", "khamit"), ("Цой", "tsoy"),
    ("Юлия", "yuliya"), ("Щукин", "shchukin"), ("Ильяс", "ilyas"), ("Қиясбек", "kiyasbek"),
    ("Арайлым", "arailym"), ("Gulzada", "gulzada"),
])
def test_romanization_follows_passport_spelling(cyrillic, latin):
    assert workspace_names.romanize(cyrillic) == latin


# (LMS name, official ФИО from CRM, first, last, address) — real teachers, production 2026-09-14.
PRODUCTION_TEACHERS = [
    # The eleven pilot addresses people chose by hand are reproduced exactly.
    ("Есен Нұрғалы Беғалыұлы", "Нургалы Есен", "Nurgaly", "Yesen", "nurgaly"),
    ("Aisha Temirkhan", "Айша Темирхан", "Aisha", "Temirkhan", "aisha"),
    ("Алина Сыздыкова", "Алина Сыздыкова", "Alina", "Syzdykova", "alina"),
    ("Gulzada Kassymbayeva", "Гулзада Касымбаева", "Gulzada", "Kassymbayeva", "gulzada"),
    ("Ерполат Бекдәулет Полатұл", "Бекдаулет Ерполат", "Bekdaulet", "Yerpolat", "bekdaulet"),
    ("Кенжебаев Арсен", "Арсен Кенжебаев", "Arsen", "Kenzhebayev", "arsen"),
    ("Zhansaya Makhambetaliyeva", "Жансая Махамбеталиева", "Zhansaya", "Makhambetaliyeva", "zhansaya"),
    ("Шадеева Арайлым", "Арайлым Шадеева", "Arailym", "Shadeyeva", "arailym"),
    # Pending teachers.
    ("Орынбасар Ақжол Ерғалиұлы", "Акжол Орынбасар", "Akzhol", "Orynbasar", "akzhol"),
    ("Жансерик Курбанов", "Жансерик Курбанов", "Zhanserik", "Kurbanov", "zhanserik"),
    ("Болат Еркебулан Бауыржанұлы", "Еркебулан Болат", "Yerkebulan", "Bolat", "yerkebulan"),
    ("Айтжан Әділет Дулатұлы", "Адилет Айтжан", "Adilet", "Aitzhan", "adilet"),
    ("Миниус Ернұр Темірболатұлы", "Ернур Миниус", "Yernur", "Minius", "yernur"),
    ("Даниил Бутырин", "Даниил Бутырин", "Daniil", "Butyrin", "daniil"),
    ("Лайла Жанатбеккызы", "Лайла Жанатбеккызы", "Laila", "Zhanatbekkyzy", "laila"),
    ("Ахметов Бексултан Замирович", "Бексултан Ахметов", "Beksultan", "Akhmetov", "beksultan"),
    ("Ақтай Мирас Айқынұлы", "Мирас Актай", "Miras", "Aktay", "miras"),
    ("Aliya Dosniyazova", "Алия Досниязова", "Aliya", "Dosniyazova", "aliya"),
    ("Madina", "Мадина Сибанова", "Madina", "Sibanova", "madina"),
    ("Жақсылық Даниял Едігеұлы", "Даниял Жаксылык", "Daniyal", "Zhaksylyk", "daniyal"),
    ("Қиясбек Мирас", "Мирас Киясбек", "Miras", "Kiyasbek", "miras"),
    ("Ержанқызы Елдана", "Елдана Ержанкызы", "Yeldana", "Yerzhankyzy", "yeldana"),
    ("Махамаджанов Диербек", "Диербек Махамаджанов", "Diyerbek", "Makhamadzhanov", "diyerbek"),
    ("Сырым Өркенұлы", "Сырым Оркенулы", "Syrym", "Orkenuly", "syrym"),
    ("Maulen", "Маулен Аязбай", "Maulen", "Ayazbay", "maulen"),
    ("Ерсултан Онталап", "Ерсултан Онталап", "Yersultan", "Ontalap", "yersultan"),
    ("Ayanat", "Аянат Ислам", "Ayanat", "Islam", "ayanat"),
    ("Дулатұлы Абай", "Абай Дулатулы", "Abay", "Dulatuly", "abay"),
    ("Нурай Бақытжанқызы", "Нурай Бакытжанкызы", "Nuray", "Bakytzhankyzy", "nuray"),
    ("Тлеуғали Әли Робертұлы", "Али Тлеугали", "Ali", "Tleugali", "ali"),
    ("Оралбекова Аида Саятқызы", "Аида Оралбекова", "Aida", "Oralbekova", "aida"),
    ("Исабеков Алпамыс Нургалиевич", "Алпамыс Исабеков", "Alpamys", "Isabekov", "alpamys"),
    ("Beksultan Balkybek", "Бексултан Балкыбек", "Beksultan", "Balkybek", "beksultan"),
    ("Нурай Кобейсин", "Нурай Кобейсин", "Nuray", "Kobeisin", "nuray"),
]


@pytest.mark.parametrize("name, official, first, last, local", PRODUCTION_TEACHERS)
def test_production_teachers_get_english_names_and_addresses(name, official, first, last, local):
    assert workspace_names.english_name(name, official) == (first, last)
    assert workspace_names.suggest_address(first, last, set()) == local + DOMAIN


def test_without_an_official_name_the_lms_name_order_is_read_from_its_endings():
    assert workspace_names.english_name("Қиясбек Мирас") == ("Miras", "Kiyasbek")
    assert workspace_names.english_name("Даниил Бутырин") == ("Daniil", "Butyrin")
    assert workspace_names.english_name("Ахметов Бексултан Замирович") == ("Beksultan", "Akhmetov")
    assert workspace_names.english_name("Ержанқызы Елдана") == ("Yeldana", "Yerzhankyzy")
    # A single word never invents a surname — the page asks for one.
    assert workspace_names.english_name("Madina") == ("Madina", "")
    assert workspace_names.english_name("—") == ("", "")


def test_a_latin_label_does_not_beat_the_official_name():
    assert workspace_names.english_name("Albar Head", "Head of NUET Альбар Керимхан") == \
        ("Albar", "Kerimkhan")


def test_the_same_given_name_falls_back_to_the_surname_then_a_number():
    taken = {"miras" + DOMAIN}
    assert workspace_names.suggest_address("Miras", "Kiyasbek", taken) == "miras.kiyasbek" + DOMAIN
    taken.add("miras.kiyasbek" + DOMAIN)
    assert workspace_names.suggest_address("Miras", "Kiyasbek", taken) == "miras.kiyasbek2" + DOMAIN
    assert workspace_names.suggest_address("Madina", "", {"madina" + DOMAIN}) == "madina2" + DOMAIN
    assert workspace_names.suggest_address("", "", set()) is None


# ── the Workspace users list ──────────────────────────────────────────────────────────────

GOOGLE_USERS_CSV = (
    "﻿First Name [Required],Last Name [Required],Email Address [Required],Status [READ ONLY],"
    "Last Sign In [READ ONLY],Org Unit Path [Required]\n"
    "Ernur,Akzhol,ernur@mastereducation.kz,Active,2026/09/10 10:00:00,/Leadership\n"
    "Nuray,Bakytzhankyzy,Nuray@MasterEducation.kz,Active,Never logged in,/Teachers\n"
    "Old,Staff,old@mastereducation.kz,Suspended,2025/01/01 10:00:00,/\n"
)


def test_the_users_list_is_read_from_googles_download():
    accounts = {a["email"]: a for a in workspace_directory.parse_users_csv(GOOGLE_USERS_CSV)}
    assert set(accounts) == {"ernur" + DOMAIN, "nuray" + DOMAIN, "old" + DOMAIN}
    assert accounts["ernur" + DOMAIN]["signed_in"] is True
    assert accounts["ernur" + DOMAIN]["org_unit"] == "/Leadership"
    assert accounts["nuray" + DOMAIN]["signed_in"] is False
    assert accounts["nuray" + DOMAIN]["first_name"] == "Nuray"
    assert accounts["old" + DOMAIN]["suspended"] is True


def test_a_file_that_is_not_a_users_list_is_refused():
    with pytest.raises(ValueError, match="Email Address"):
        workspace_directory.parse_users_csv("Name,Mail\nA,b@c.d\n")
    with pytest.raises(ValueError):
        workspace_directory.parse_users_csv("")
    with pytest.raises(ValueError, match="no accounts"):
        workspace_directory.parse_users_csv("Email Address [Required]\nnot-an-address\n")


# ── validation ────────────────────────────────────────────────────────────────────────────

def test_a_workspace_email_must_be_on_our_domain(db):
    teacher = _user(db, "teacher")
    with pytest.raises(ValueError, match="must end"):
        teacher_onboarding.validate_workspace_email(db, teacher, "teacher@gmail.com")


def test_only_teacher_roles_can_be_connected(db):
    _directory(db, "student" + DOMAIN, "head" + DOMAIN)
    with pytest.raises(ValueError):
        teacher_onboarding.validate_workspace_email(db, _user(db, "student"), "student" + DOMAIN)
    head = _user(db, "head_teacher")
    assert teacher_onboarding.validate_workspace_email(db, head, "head" + DOMAIN) == "head" + DOMAIN


def test_an_address_cannot_be_claimed_twice(db):
    first, second = _user(db, "teacher"), _user(db, "teacher")
    first.workspace_email = "taken" + DOMAIN
    db.flush()
    _directory(db, "taken" + DOMAIN)
    with pytest.raises(ValueError, match="already connected"):
        teacher_onboarding.validate_workspace_email(db, second, "taken" + DOMAIN)
    # The owner's own value is a no-op, not an error — even with no users list at all.
    assert teacher_onboarding.validate_workspace_email(db, first, "taken" + DOMAIN) == "taken" + DOMAIN


def test_blank_disconnects_and_case_folds(db):
    teacher = _user(db, "teacher")
    _directory(db, "said" + DOMAIN)
    assert teacher_onboarding.validate_workspace_email(db, teacher, "  ") is None
    assert teacher_onboarding.validate_workspace_email(
        db, teacher, "  Said@MasterEducation.KZ ") == "said" + DOMAIN


def test_connecting_needs_an_uploaded_users_list(db):
    teacher = _user(db, "teacher")
    with pytest.raises(ValueError, match="Upload the Google Workspace users list"):
        teacher_onboarding.validate_workspace_email(db, teacher, "nurgaly" + DOMAIN)


def test_an_address_missing_from_the_users_list_or_suspended_cannot_be_connected(db):
    teacher = _user(db, "teacher")
    _directory(db, "other" + DOMAIN, ("old" + DOMAIN, "Old", "Staff", {"suspended": True}))
    with pytest.raises(ValueError, match="not in the Workspace users list"):
        teacher_onboarding.validate_workspace_email(db, teacher, "nurgaly" + DOMAIN)
    with pytest.raises(ValueError, match="suspended"):
        teacher_onboarding.validate_workspace_email(db, teacher, "old" + DOMAIN)


# ── the list endpoint ─────────────────────────────────────────────────────────────────────

def _rows(db):
    from src.admin.routes.recordings import _teacher_rows
    return _teacher_rows(db)


def _pending_teacher(world, name, lessons=1, official=None, email=None):
    db = world["db"]
    teacher = _user(db, "teacher")
    teacher.name, teacher.official_full_name = name, official
    if email:
        teacher.email = email
    group = world["group"]()
    world["enrol"](group)
    for day in range(lessons):
        world["lesson"](group, days_ahead=day + 1, teacher_id=teacher.id, created_by=teacher.id)
    db.flush()
    return teacher


def test_list_shows_connected_teachers_and_teachers_with_upcoming_lessons(world):
    db = world["db"]
    teacher = world["teacher"]
    group = world["group"](name="IELTS July 8 2026 - Gulzada")
    world["enrol"](group)
    world["lesson"](group, days_ahead=3)

    idle = _user(db, "teacher")           # no lessons, not connected → absent
    connected = _user(db, "teacher")      # no lessons but connected → present
    connected.workspace_email = "c" + DOMAIN
    db.flush()

    by_id = {row.id: row for row in _rows(db)}
    assert teacher.id in by_id and connected.id in by_id and idle.id not in by_id
    row = by_id[teacher.id]
    assert row.upcoming_lessons == 1 and row.rooms_ready == 0
    assert row.groups[0].name == "IELTS July 8 2026 - Gulzada"
    assert row.groups[0].telegram_linked is False
    assert row.suggested_workspace_email and row.account.status == "unknown"


def test_list_marks_telegram_links_and_meet_rooms(world):
    db = world["db"]
    teacher = world["teacher"]
    group = world["group"]()
    world["enrol"](group)
    db.add(TelegramGroupLink(lms_group_id=group.id, support_group_id=9001, chat_title="chat"))
    world["lesson"](group, days_ahead=2, meeting_url="https://meet.google.com/abc-defg-hij")
    db.flush()

    row = next(r for r in _rows(db) if r.id == teacher.id)
    assert row.rooms_ready == 1
    assert row.groups[0].telegram_linked is True


def test_a_lesson_two_groups_share_counts_once_but_lists_both_groups(world):
    db = world["db"]
    teacher = world["teacher"]
    a, b = world["group"](name="a"), world["group"](name="b")
    world["enrol"](a)
    world["enrol"](b)
    world["lesson"](a, b, days_ahead=2)

    row = next(r for r in _rows(db) if r.id == teacher.id)
    assert row.upcoming_lessons == 1
    assert {g.id for g in row.groups} == {a.id, b.id}


def test_two_teachers_with_one_given_name_get_different_addresses_busiest_first(world):
    busy = _pending_teacher(world, "Нурай Кобейсин", lessons=1)
    busier = _pending_teacher(world, "Нурай Бақытжанқызы", lessons=3)
    rows = {r.id: r for r in _rows(world["db"])}
    assert rows[busier.id].suggested_workspace_email == "nuray" + DOMAIN
    assert rows[busy.id].suggested_workspace_email == "nuray.kobeisin" + DOMAIN
    assert (rows[busier.id].first_name, rows[busier.id].last_name) == ("Nuray", "Bakytzhankyzy")


def test_an_existing_account_for_the_same_person_is_proposed_instead_of_a_new_one(world):
    teacher = _pending_teacher(world, "Нурай Бақытжанқызы")
    _directory(world["db"], ("nuray" + DOMAIN, "Nuray", "Bakytzhankyzy", {"signed_in": False}))
    row = next(r for r in _rows(world["db"]) if r.id == teacher.id)
    assert row.suggested_workspace_email == "nuray" + DOMAIN
    assert row.account.status == "exists" and row.account.signed_in is False


def test_a_new_address_never_reuses_someone_elses_existing_account(world):
    """The CEO's ernur@ exists; a teacher called Nuray Kobeisin must not be offered nuray@ when
    another Nuray already holds it — and the page says who that other account is."""
    teacher = _pending_teacher(world, "Нурай Кобейсин")
    _directory(world["db"], ("nuray" + DOMAIN, "Nuray", "Bakytzhankyzy"))
    row = next(r for r in _rows(world["db"]) if r.id == teacher.id)
    assert row.suggested_workspace_email == "nuray.kobeisin" + DOMAIN
    assert row.account.status == "missing"
    assert row.similar_accounts == ["nuray" + DOMAIN]


def test_a_workspace_login_is_proposed_as_the_teachers_account(world):
    teacher = _pending_teacher(world, "Nurkerim Oskenbay", email="nurkerim" + DOMAIN)
    row = next(r for r in _rows(world["db"]) if r.id == teacher.id)
    assert row.suggested_workspace_email == "nurkerim" + DOMAIN


def test_one_teacher_row_exists_without_lessons(world):
    from src.admin.routes.recordings import get_teacher
    db = world["db"]
    idle = _user(db, "teacher")
    row = get_teacher(idle.id, db=db, current_user=_user(db, "admin"))
    assert row.id == idle.id and row.upcoming_lessons == 0


def test_a_skipped_teacher_is_marked_sorted_last_and_unskipped_by_connecting(world):
    db = world["db"]
    admin = _user(db, "admin")
    john = _pending_teacher(world, "Teacher John", lessons=5)
    other = _pending_teacher(world, "Aisha Temirkhan", lessons=1)
    teacher_onboarding.set_skipped(db, admin, john, True)

    rows = _rows(db)
    order = [r.id for r in rows if r.id in (john.id, other.id)]
    assert order == [other.id, john.id]
    assert next(r for r in rows if r.id == john.id).skipped is True

    _directory(db, "john" + DOMAIN)
    teacher_onboarding.set_workspace_email(db, admin, john, "john" + DOMAIN)
    assert john.id not in teacher_onboarding.skipped_ids(db)


# ── connect / disconnect ──────────────────────────────────────────────────────────────────

def test_set_workspace_email_connects_and_disconnects(world):
    db = world["db"]
    admin = _user(db, "admin")
    teacher = world["teacher"]
    _directory(db, "nurgaly" + DOMAIN)

    teacher_onboarding.set_workspace_email(db, admin, teacher, "nurgaly" + DOMAIN)
    assert teacher.workspace_email == "nurgaly" + DOMAIN

    teacher_onboarding.set_workspace_email(db, admin, teacher, None)
    assert teacher.workspace_email is None


def test_the_connect_route_turns_a_refusal_into_a_400(world):
    from src.admin.routes.recordings import ConnectBody, connect_teacher
    db = world["db"]
    with pytest.raises(HTTPException) as refused:
        connect_teacher(world["teacher"].id, ConnectBody(workspace_email="t@gmail.com"),
                        db=db, current_user=_user(db, "admin"))
    assert refused.value.status_code == 400
    assert world["teacher"].workspace_email is None


# ── the Google Admin import ───────────────────────────────────────────────────────────────

def _export(db, rows, org_unit="/Teachers"):
    from src.admin.routes.recordings import ImportBody, ImportRowIn, export_workspace_import
    body = ImportBody(org_unit=org_unit, rows=[ImportRowIn(**r) for r in rows])
    response = export_workspace_import(body, db=db, current_user=_user(db, "admin"))
    return list(csv.reader(io.StringIO(response.body.decode())))


def _row(teacher, email, first="Yernur", last="Minius"):
    return {"user_id": teacher.id, "workspace_email": email, "first_name": first, "last_name": last}


def test_the_import_uses_googles_template_and_only_the_reviewed_rows(world):
    db = world["db"]
    teacher = world["teacher"]
    teacher.email = "yernur.minius@nu.edu.kz"
    bystander = _pending_teacher(world, "Aisha Temirkhan")
    _directory(db, "ernur" + DOMAIN)

    header, *data = _export(db, [_row(teacher, " Yernur@MasterEducation.kz ")])
    assert header == teacher_onboarding.IMPORT_HEADER
    assert "Change Password at Next Sign-In" in header
    assert len(data) == 1
    first, last, email, password, org_unit, recovery, change = data[0]
    assert (first, last, email, org_unit, recovery, change) == \
        ("Yernur", "Minius", "yernur" + DOMAIN, "/Teachers", "yernur.minius@nu.edu.kz", "TRUE")
    assert len(password) >= 12
    assert bystander.email not in "".join(sum(data, []))


def test_the_import_refuses_an_address_that_already_exists_in_workspace(world):
    db = world["db"]
    _directory(db, ("ernur" + DOMAIN, "Ernur", "Akzhol"))
    with pytest.raises(HTTPException) as refused:
        _export(db, [_row(world["teacher"], "ernur" + DOMAIN)])
    assert refused.value.status_code == 400
    assert "overwrite" in refused.value.detail


def test_the_import_refuses_without_a_users_list(world):
    with pytest.raises(HTTPException) as refused:
        _export(world["db"], [_row(world["teacher"], "yernur" + DOMAIN)])
    assert "users list" in refused.value.detail


@pytest.mark.parametrize("first, last, problem", [
    ("Ернур", "Minius", "English letters"),
    ("Yernur", "", "Last name is empty"),
    ("Yernur", "M" * 61, "longer than 60"),
])
def test_the_import_refuses_names_that_are_not_english(world, first, last, problem):
    _directory(world["db"], "someone" + DOMAIN)
    with pytest.raises(HTTPException) as refused:
        _export(world["db"], [_row(world["teacher"], "yernur" + DOMAIN, first, last)])
    assert problem in refused.value.detail


def test_the_import_refuses_one_address_twice_and_a_connected_teacher(world):
    db = world["db"]
    other = _pending_teacher(world, "Нурай Кобейсин")
    connected = _user(db, "teacher")
    connected.workspace_email = "done" + DOMAIN
    db.flush()
    _directory(db, "done" + DOMAIN)
    with pytest.raises(HTTPException) as refused:
        _export(db, [_row(world["teacher"], "nuray" + DOMAIN, "Nuray", "A"),
                     _row(other, "nuray" + DOMAIN, "Nuray", "Kobeisin"),
                     _row(connected, "new" + DOMAIN, "New", "Person")])
    assert "also chosen for" in refused.value.detail
    assert "already connected to done" in refused.value.detail


def test_the_import_refuses_an_org_unit_outside_the_tree(world):
    _directory(world["db"], "someone" + DOMAIN)
    with pytest.raises(HTTPException) as refused:
        _export(world["db"], [_row(world["teacher"], "yernur" + DOMAIN)], org_unit="Teachers")
    assert "Org unit" in refused.value.detail
