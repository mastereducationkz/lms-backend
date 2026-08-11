import pytest
from datetime import datetime, timezone, timedelta

from src.schemas.models import CuratorOnboarding, GroupStudent
# reuse the transactional db fixture + factories from the exams test module
from tests.test_exams_rbac import db, _user, _group, _enrol  # noqa: F401


def _cards(db, curator_id):
    return (db.query(CuratorOnboarding)
              .filter(CuratorOnboarding.curator_id == curator_id).all())


def test_new_pair_creates_card(db):
    from src.curator.onboarding_service import reconcile_onboarding
    c = _user(db, "curator", "ob-c1@t.io")
    s = _user(db, "student", "ob-s1@t.io")
    g = _group(db, "OB G1", curator_id=c.id)
    _enrol(db, g, s)
    reconcile_onboarding(db)
    rows = _cards(db, c.id)
    assert len(rows) == 1 and rows[0].student_id == s.id and rows[0].status == "new"


def test_late_curator_assignment_still_creates_card(db):
    # student enrolled while group had NO curator, curator assigned later
    from src.curator.onboarding_service import reconcile_onboarding
    c = _user(db, "curator", "ob-c2@t.io")
    s = _user(db, "student", "ob-s2@t.io")
    g = _group(db, "OB G2", curator_id=None)
    _enrol(db, g, s)
    reconcile_onboarding(db)
    assert _cards(db, c.id) == []          # no curator yet -> no card
    g.curator_id = c.id
    db.flush()
    reconcile_onboarding(db)
    assert len(_cards(db, c.id)) == 1      # appears when pairing becomes active


def test_transfer_to_new_curator_moves_card(db):
    from src.curator.onboarding_service import reconcile_onboarding
    c1 = _user(db, "curator", "ob-c3a@t.io")
    c2 = _user(db, "curator", "ob-c3b@t.io")
    s = _user(db, "student", "ob-s3@t.io")
    g1 = _group(db, "OB G3a", curator_id=c1.id)
    g2 = _group(db, "OB G3b", curator_id=c2.id)
    _enrol(db, g1, s)
    reconcile_onboarding(db)
    assert len(_cards(db, c1.id)) == 1
    # transfer: remove from g1, add to g2
    db.query(GroupStudent).filter(GroupStudent.group_id == g1.id,
                                  GroupStudent.student_id == s.id).delete()
    _enrol(db, g2, s)
    db.flush()
    reconcile_onboarding(db)
    assert _cards(db, c1.id)[0].status == "cancelled"   # old curator no longer nagged
    assert len(_cards(db, c2.id)) == 1 and _cards(db, c2.id)[0].status == "new"


def test_same_curator_second_group_no_duplicate(db):
    from src.curator.onboarding_service import reconcile_onboarding
    c = _user(db, "curator", "ob-c4@t.io")
    s = _user(db, "student", "ob-s4@t.io")
    g1 = _group(db, "OB G4a", curator_id=c.id)
    g2 = _group(db, "OB G4b", curator_id=c.id)
    _enrol(db, g1, s)
    _enrol(db, g2, s)
    reconcile_onboarding(db)
    assert len(_cards(db, c.id)) == 1      # one card despite two groups


def test_done_card_survives_and_reactivation(db):
    from src.curator.onboarding_service import reconcile_onboarding
    c = _user(db, "curator", "ob-c5@t.io")
    s = _user(db, "student", "ob-s5@t.io")
    g = _group(db, "OB G5", curator_id=c.id)
    _enrol(db, g, s)
    reconcile_onboarding(db)
    card = _cards(db, c.id)[0]
    card.status = "done"
    db.flush()
    reconcile_onboarding(db)
    assert _cards(db, c.id)[0].status == "done"   # done is terminal, not re-created
    # cancel path -> reactivate
    card.status = "cancelled"
    db.flush()
    reconcile_onboarding(db)
    assert _cards(db, c.id)[0].status == "new"    # active pair revives a cancelled card


def test_null_group_membership_created_at_does_not_crash(db):
    from src.curator.onboarding_service import reconcile_onboarding
    c = _user(db, "curator", "ob-null@t.io")
    s = _user(db, "student", "ob-null-s@t.io")
    g1 = _group(db, "OB N1", curator_id=c.id)
    g2 = _group(db, "OB N2", curator_id=c.id)
    _enrol(db, g1, s)
    _enrol(db, g2, s)
    # force one membership timestamp to NULL (external CRM inserts can do this)
    rows = db.query(GroupStudent).filter(GroupStudent.student_id == s.id).all()
    rows[0].created_at = None
    db.flush()
    reconcile_onboarding(db)  # must not raise
    cards = db.query(CuratorOnboarding).filter(CuratorOnboarding.curator_id == c.id).all()
    assert len(cards) == 1


def test_telegram_link():
    from src.curator.onboarding_service import telegram_link
    assert telegram_link("@durov") == "https://t.me/durov"
    assert telegram_link("durov") == "https://t.me/durov"
    assert telegram_link("123456789") is None   # bare numeric id not resolvable
    assert telegram_link(None) is None
    assert telegram_link("  ") is None
