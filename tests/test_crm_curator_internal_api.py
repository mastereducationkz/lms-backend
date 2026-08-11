"""HTTP contract of the internal curator API the CRM workspace runs on.

The important assertions here are the ones about *who the LMS thinks the caller is*. The
CRM authenticates with a shared service key and then names an actor; if the LMS trusted the
role the CRM claimed, a stale CRM session or a CRM bug would be enough to read the whole
organisation. So these tests drive the endpoints through the real dependency chain and check
that the role is re-derived from the ``users`` table every time.
"""
from datetime import datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.config import get_db
from src.routes.crm_curator_internal import router as curator_router
from src.schemas.models import Group, GroupStudent, UserInDB
from tests.onboarding_fixtures import db  # noqa: F401

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


def _hdr(actor_id=None, staff_role=None):
    h = {"X-CRM-Service-Key": SERVICE_KEY}
    if actor_id is not None:
        h["X-Lms-Actor-Id"] = str(actor_id)
    if staff_role is not None:
        h["X-Crm-Staff-Role"] = staff_role
    return h


_n = [0]


def _mk_user(db, role, name="U"):  # noqa: F811
    from src.utils.auth_utils import hash_password

    _n[0] += 1
    u = UserInDB(
        email=f"api-{role}-{_n[0]}-{datetime.now().timestamp():.6f}@t.io",
        name=f"{name}{_n[0]}",
        role=role,
        hashed_password=hash_password("x"),
        is_active=True,
    )
    db.add(u)
    db.flush()
    return u


def _mk_group(db, curator, name="G", program="SAT"):  # noqa: F811
    _n[0] += 1
    g = Group(
        name=f"{name}-{_n[0]}",
        curator_id=curator.id if curator else None,
        is_active=True,
        is_over=False,
        program_type=program,
    )
    db.add(g)
    db.flush()
    return g


def _enrol(db, group, student):  # noqa: F811
    db.add(GroupStudent(group_id=group.id, student_id=student.id, created_at=datetime.utcnow()))
    db.flush()


@pytest.fixture
def world(db):  # noqa: F811
    """Two curators, a head, a shared student and a private one each."""
    c1, c2 = _mk_user(db, "curator", "C1"), _mk_user(db, "curator", "C2")
    head = _mk_user(db, "head_curator", "H")
    shared = _mk_user(db, "student", "Shared")
    only1, only2 = _mk_user(db, "student", "One"), _mk_user(db, "student", "Two")
    g1, g2 = _mk_group(db, c1, "G1", "SAT"), _mk_group(db, c2, "G2", "IELTS")
    for g, s in ((g1, shared), (g2, shared), (g1, only1), (g2, only2)):
        _enrol(db, g, s)
    db.commit()

    from src.curator.onboarding_core import reconcile_onboarding

    reconcile_onboarding(db)
    return dict(c1=c1, c2=c2, head=head, shared=shared, only1=only1, only2=only2, g1=g1, g2=g2)


# --- authentication -----------------------------------------------------------------------


def test_service_key_is_required(client, world):
    r = client.get("/internal/crm/curator/onboarding", headers={"X-Lms-Actor-Id": "1"})
    assert r.status_code == 401


def test_actor_header_is_required(client, world):
    r = client.get("/internal/crm/curator/onboarding", headers={"X-CRM-Service-Key": SERVICE_KEY})
    assert r.status_code == 400


def test_non_curator_actor_is_refused(client, db, world):  # noqa: F811
    student = world["shared"]
    r = client.get("/internal/crm/curator/onboarding", headers=_hdr(actor_id=student.id))
    assert r.status_code == 403


def test_inactive_curator_is_refused(client, db, world):  # noqa: F811
    world["c1"].is_active = False
    db.commit()
    r = client.get("/internal/crm/curator/onboarding", headers=_hdr(actor_id=world["c1"].id))
    assert r.status_code == 403


def test_role_comes_from_the_database_not_from_the_caller(client, db, world):  # noqa: F811
    """Claiming head-curator reach in a header must not grant it.

    A regular curator id plus a ``X-Crm-Staff-Role: admin`` header is the exact shape a
    confused (or compromised) CRM would send. The LMS resolves the actor id first and keeps
    the role it finds there.
    """
    r = client.get(
        "/internal/crm/curator/onboarding",
        headers=_hdr(actor_id=world["c1"].id, staff_role="admin"),
    )
    assert r.status_code == 200
    assert r.json()["is_head"] is False
    student_ids = {c["student_id"] for c in r.json()["cards"]}
    assert world["only2"].id not in student_ids


# --- scope --------------------------------------------------------------------------------


def test_curator_scope_is_their_active_groups_only(client, world):
    r = client.get("/internal/crm/curator/scope", headers=_hdr(actor_id=world["c1"].id))
    assert r.status_code == 200
    body = r.json()
    assert body["is_head"] is False
    assert set(body["curator_ids"]) == {world["c1"].id}
    assert world["shared"].id in body["student_ids"]
    assert world["only1"].id in body["student_ids"]
    assert world["only2"].id not in body["student_ids"]
    assert world["g1"].id in body["group_ids"]
    assert world["g2"].id not in body["group_ids"]


def test_head_scope_is_organisation_wide(client, world):
    r = client.get("/internal/crm/curator/scope", headers=_hdr(actor_id=world["head"].id))
    body = r.json()
    assert body["is_head"] is True
    assert {world["only1"].id, world["only2"].id} <= set(body["student_ids"])


def test_crm_staff_without_lms_identity_gets_oversight(client, world):
    r = client.get("/internal/crm/curator/scope", headers=_hdr(staff_role="manager"))
    assert r.status_code == 200
    assert r.json()["is_head"] is True


# --- board --------------------------------------------------------------------------------


def test_shared_student_appears_for_both_curators_independently(client, db, world):  # noqa: F811
    r1 = client.get("/internal/crm/curator/onboarding", headers=_hdr(actor_id=world["c1"].id))
    r2 = client.get("/internal/crm/curator/onboarding", headers=_hdr(actor_id=world["c2"].id))
    card1 = next(c for c in r1.json()["cards"] if c["student_id"] == world["shared"].id)
    card2 = next(c for c in r2.json()["cards"] if c["student_id"] == world["shared"].id)
    assert card1["id"] != card2["id"]

    moved = client.patch(
        f"/internal/crm/curator/onboarding/{card1['id']}",
        json={"status": "done"},
        headers=_hdr(actor_id=world["c1"].id),
    )
    assert moved.status_code == 200

    again = client.get("/internal/crm/curator/onboarding", headers=_hdr(actor_id=world["c2"].id))
    still = next(c for c in again.json()["cards"] if c["student_id"] == world["shared"].id)
    assert still["status"] == "new", "the other curator's cycle is untouched"


def test_curator_cannot_read_another_curators_board(client, world):
    r = client.get(
        f"/internal/crm/curator/onboarding?curator_id={world['c2'].id}",
        headers=_hdr(actor_id=world["c1"].id),
    )
    assert r.status_code == 403


def test_curator_cannot_patch_a_foreign_card_and_gets_no_existence_hint(client, world):
    r2 = client.get("/internal/crm/curator/onboarding", headers=_hdr(actor_id=world["c2"].id))
    foreign_id = r2.json()["cards"][0]["id"]
    r = client.patch(
        f"/internal/crm/curator/onboarding/{foreign_id}",
        json={"status": "done"},
        headers=_hdr(actor_id=world["c1"].id),
    )
    assert r.status_code == 404


def test_head_can_intervene_on_any_card(client, world):
    r1 = client.get("/internal/crm/curator/onboarding", headers=_hdr(actor_id=world["c1"].id))
    card_id = r1.json()["cards"][0]["id"]
    r = client.patch(
        f"/internal/crm/curator/onboarding/{card_id}",
        json={"status": "in_progress"},
        headers=_hdr(actor_id=world["head"].id),
    )
    assert r.status_code == 200

    detail = client.get(
        f"/internal/crm/curator/onboarding/{card_id}", headers=_hdr(actor_id=world["head"].id)
    ).json()
    assert detail["curator_id"] == world["c1"].id, "ownership unchanged"
    assert any(e["action"] == "intervention" for e in detail["history"])


def test_notes_and_next_action_roundtrip(client, world):
    r1 = client.get("/internal/crm/curator/onboarding", headers=_hdr(actor_id=world["c1"].id))
    card_id = r1.json()["cards"][0]["id"]

    assert client.post(
        f"/internal/crm/curator/onboarding/{card_id}/notes",
        json={"body": "Позвонил родителям"},
        headers=_hdr(actor_id=world["c1"].id),
    ).status_code == 200
    assert client.put(
        f"/internal/crm/curator/onboarding/{card_id}/next-action",
        json={"next_action_at": "2026-09-01", "note": "перезвонить"},
        headers=_hdr(actor_id=world["c1"].id),
    ).status_code == 200

    detail = client.get(
        f"/internal/crm/curator/onboarding/{card_id}", headers=_hdr(actor_id=world["c1"].id)
    ).json()
    assert detail["next_action_at"] == "2026-09-01"
    assert [n["body"] for n in detail["notes"]] == ["Позвонил родителям"]
    assert detail["notes"][0]["author_id"] == world["c1"].id


def test_invalid_status_is_rejected(client, world):
    r1 = client.get("/internal/crm/curator/onboarding", headers=_hdr(actor_id=world["c1"].id))
    card_id = r1.json()["cards"][0]["id"]
    r = client.patch(
        f"/internal/crm/curator/onboarding/{card_id}",
        json={"status": "deleted"},
        headers=_hdr(actor_id=world["c1"].id),
    )
    assert r.status_code == 400


# --- groups -------------------------------------------------------------------------------


def test_group_catalogue_hides_other_curators_rosters(client, world):
    r = client.get("/internal/crm/curator/groups", headers=_hdr(actor_id=world["c1"].id))
    by_id = {g["group_id"]: g for g in r.json()}

    mine = by_id[world["g1"].id]
    assert mine["is_mine"] is True and mine["student_count"] > 0

    # Selectable as a transfer destination, but its roster and teacher stay hidden.
    theirs = by_id[world["g2"].id]
    assert theirs["is_mine"] is False
    assert theirs["student_count"] == 0
    assert theirs["teacher_id"] is None
    assert theirs["name"], "the label is needed to pick it"


def test_mine_only_returns_just_the_curators_groups(client, world):
    r = client.get(
        "/internal/crm/curator/groups?mine_only=true", headers=_hdr(actor_id=world["c1"].id)
    )
    assert {g["group_id"] for g in r.json()} == {world["g1"].id}


# --- academic projection ------------------------------------------------------------------


def test_academic_projection_differs_per_curator_for_a_shared_student(client, world):
    sid = world["shared"].id
    a = client.post(
        "/internal/crm/curator/students/academic",
        json={"lms_student_ids": [sid]},
        headers=_hdr(actor_id=world["c1"].id),
    ).json()["students"][str(sid)]
    b = client.post(
        "/internal/crm/curator/students/academic",
        json={"lms_student_ids": [sid]},
        headers=_hdr(actor_id=world["c2"].id),
    ).json()["students"][str(sid)]

    assert {g["group_id"] for g in a["groups"]} == {world["g1"].id}
    assert {g["group_id"] for g in b["groups"]} == {world["g2"].id}

    # Products stay visible to both — a curator may learn the student also studies IELTS,
    # without learning which group or whose.
    assert set(a["products"]) == {"SAT", "IELTS"} == set(b["products"])
    assert a["product_count"] == 2
    assert a["learning_start_date"] is not None


def test_head_sees_the_complete_projection(client, world):
    sid = world["shared"].id
    body = client.post(
        "/internal/crm/curator/students/academic",
        json={"lms_student_ids": [sid]},
        headers=_hdr(actor_id=world["head"].id),
    ).json()["students"][str(sid)]
    assert {g["group_id"] for g in body["groups"]} == {world["g1"].id, world["g2"].id}


def test_academic_projection_of_an_unauthorised_student_is_empty_not_an_error(client, world):
    """No 404-vs-200 oracle: an id outside scope simply yields nothing."""
    body = client.post(
        "/internal/crm/curator/students/academic",
        json={"lms_student_ids": [world["only2"].id]},
        headers=_hdr(actor_id=world["c1"].id),
    ).json()["students"][str(world["only2"].id)]
    assert body["groups"] == []


# --- head-only endpoints ------------------------------------------------------------------


def test_reconcile_and_per_curator_are_head_only(client, world):
    assert client.post(
        "/internal/crm/curator/reconcile", headers=_hdr(actor_id=world["c1"].id)
    ).status_code == 403
    assert client.get(
        "/internal/crm/curator/onboarding/per-curator", headers=_hdr(actor_id=world["c1"].id)
    ).status_code == 403
    assert client.post(
        "/internal/crm/curator/reconcile", headers=_hdr(actor_id=world["head"].id)
    ).status_code == 200


def test_per_curator_rollup_shape(client, world):
    rows = client.get(
        "/internal/crm/curator/onboarding/per-curator", headers=_hdr(actor_id=world["head"].id)
    ).json()
    mine = next(r for r in rows if r["curator_id"] == world["c1"].id)
    assert mine["student_count"] == 2
    assert mine["group_count"] == 1
    assert set(mine["onboarding"]) == {"new", "in_progress", "done", "overdue"}


def test_group_curator_reassignment_moves_the_cycles(client, db, world):  # noqa: F811
    r = client.put(
        f"/internal/crm/curator/groups/{world['g1'].id}/curator",
        json={"curator_id": world["c2"].id},
        headers=_hdr(actor_id=world["head"].id),
    )
    assert r.status_code == 200

    from src.curator.onboarding_core import active_cycle

    # only1 was in g1 alone, so responsibility moves wholesale.
    assert active_cycle(db, world["c1"].id, world["only1"].id) is None
    assert active_cycle(db, world["c2"].id, world["only1"].id) is not None


def test_reassignment_is_head_only(client, world):
    r = client.put(
        f"/internal/crm/curator/groups/{world['g1'].id}/curator",
        json={"curator_id": world["c2"].id},
        headers=_hdr(actor_id=world["c1"].id),
    )
    assert r.status_code == 403


def test_thresholds_are_served_not_duplicated(client, world):
    body = client.get("/internal/crm/curator/thresholds", headers=_hdr(actor_id=world["c1"].id)).json()
    assert body["new_overdue_days"] == 2
    assert body["in_progress_stale_days"] == 5
    assert body["lms_inactivity_days"] == 7
    assert body["product_ending_soon_days"] == 15


def test_directory_lists_curators_with_workload(client, world):
    rows = client.get("/internal/crm/curator/directory", headers=_hdr(actor_id=world["head"].id)).json()
    by_id = {r["lms_user_id"]: r for r in rows}
    assert world["c1"].id in by_id and world["head"].id in by_id
    assert by_id[world["c1"].id]["active_group_count"] == 1
    assert by_id[world["c1"].id]["student_count"] == 2
    assert by_id[world["c1"].id]["role"] == "curator"
