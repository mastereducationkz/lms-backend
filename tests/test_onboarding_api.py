import pytest
from tests.test_exams_rbac import db, _user, _group, _enrol  # noqa: F401
from src.curator.onboarding_service import reconcile_onboarding
from src.schemas.models import CuratorOnboarding


def _seed(db):
    c = _user(db, "curator", "api-c@t.io", name="Cur A")
    c2 = _user(db, "curator", "api-c2@t.io", name="Cur B")
    s = _user(db, "student", "api-s@t.io", name="Stud A")
    s2 = _user(db, "student", "api-s2@t.io", name="Stud B")
    g = _group(db, "API G", curator_id=c.id)
    g2 = _group(db, "API G2", curator_id=c2.id)
    _enrol(db, g, s)
    _enrol(db, g2, s2)
    reconcile_onboarding(db)
    return dict(c=c, c2=c2, s=s, s2=s2)


def test_curator_sees_only_own(db):
    from src.curator.routes.onboarding import list_onboarding
    w = _seed(db)
    out = list_onboarding(db=db, current_user=w["c"], curator_id=None)
    ids = {card["student_id"] for card in out["cards"]}
    assert ids == {w["s"].id}                      # not s2


def test_head_curator_sees_all_and_can_filter(db):
    from src.curator.routes.onboarding import list_onboarding
    w = _seed(db)
    head = _user(db, "head_curator", "api-h@t.io")
    allcards = list_onboarding(db=db, current_user=head, curator_id=None)
    # NOTE: this DB may already contain real (backfilled) onboarding cards, so
    # assert head_curator's unrestricted view is a *superset* including both
    # newly seeded students, rather than an exact match on the whole table.
    all_ids = {c["student_id"] for c in allcards["cards"]}
    assert all_ids.issuperset({w["s"].id, w["s2"].id})
    filtered = list_onboarding(db=db, current_user=head, curator_id=w["c2"].id)
    assert {c["student_id"] for c in filtered["cards"]} == {w["s2"].id}


def test_patch_to_done_stamps_completion(db):
    from src.curator.routes.onboarding import update_onboarding, OnboardingStatusUpdate
    w = _seed(db)
    card = db.query(CuratorOnboarding).filter_by(curator_id=w["c"].id).first()
    out = update_onboarding(card_id=card.id, payload=OnboardingStatusUpdate(status="done"),
                            db=db, current_user=w["c"])
    assert out["status"] == "done"
    db.refresh(card)
    assert card.completed_at is not None and card.completed_by == w["c"].id


def test_curator_cannot_patch_foreign_card(db):
    from fastapi import HTTPException
    from src.curator.routes.onboarding import update_onboarding, OnboardingStatusUpdate
    w = _seed(db)
    foreign = db.query(CuratorOnboarding).filter_by(curator_id=w["c2"].id).first()
    with pytest.raises(HTTPException) as ei:
        update_onboarding(card_id=foreign.id,
                          payload=OnboardingStatusUpdate(status="done"),
                          db=db, current_user=w["c"])
    assert ei.value.status_code == 403
