"""Gap reconciliation: classify LMS memberships missing from a target as absent / email_mismatch."""

import io

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from src.services import sync_reconcile as R


def test_norm_name_order_and_case_insensitive():
    assert R._norm_name("Aizhan Bekova") == R._norm_name("bekova  AIZHAN")
    assert R._norm_name("") == ""


def test_load_target_users_csv(tmp_path):
    p = tmp_path / "t.csv"
    p.write_text("a@x.io,Ann Lee\nB@X.IO,Bob Roy\n", encoding="utf-8")
    emails, name_map = R.load_target_users(str(p))
    assert emails == {"a@x.io", "b@x.io"}
    assert name_map[R._norm_name("Lee Ann")] == "a@x.io"


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as c:
        c.execute(text("CREATE TABLE users (id integer primary key, email text, name text, is_active boolean)"))
        c.execute(text("CREATE TABLE groups (id integer primary key, name text, program_type text, is_over boolean, is_active boolean)"))
        c.execute(text("CREATE TABLE group_students (id integer primary key, group_id int, student_id int)"))
        # students: 1 synced, 2 absent, 3 email-mismatch, 4 in finished group, 5 inactive
        c.execute(text("INSERT INTO users VALUES "
                       "(1,'synced@x.io','Syn Ced',1),(2,'absent@x.io','Ab Sent',1),"
                       "(3,'lms-email@x.io','Mis Match',1),(4,'grad@x.io','Grad U',1),(5,'inact@x.io','In Active',0)"))
        c.execute(text("INSERT INTO groups VALUES (10,'IELTS-A','ielts',0,1),(11,'IELTS-OLD','ielts',1,1)"))
        c.execute(text("INSERT INTO group_students VALUES (1,10,1),(2,10,2),(3,10,3),(4,11,4),(5,10,5)"))
    return sessionmaker(bind=engine)()


def test_reconcile_classifies_and_excludes(db):
    target_emails = {"synced@x.io", "target-email@x.io"}          # student 3 exists here under a diff email
    name_map = {R._norm_name("Mis Match"): "target-email@x.io"}   # same name -> mismatch
    rows = R.reconcile(db, programs=["ielts"], target_emails=target_emails, target_name_to_email=name_map)
    by_email = {r["email"]: r for r in rows}

    assert "synced@x.io" not in by_email                 # on target -> not a gap
    assert "grad@x.io" not in by_email                   # finished (is_over) group -> excluded
    assert "inact@x.io" not in by_email                  # inactive student -> excluded
    assert by_email["absent@x.io"]["classification"] == "absent"
    assert by_email["lms-email@x.io"]["classification"] == "email_mismatch"
    assert by_email["lms-email@x.io"]["target_email_if_mismatch"] == "target-email@x.io"


def test_reconcile_include_finished_and_inactive(db):
    rows = R.reconcile(db, programs=["ielts"], target_emails={"synced@x.io"},
                       include_finished=True, include_inactive=True)
    emails = {r["email"] for r in rows}
    assert "grad@x.io" in emails and "inact@x.io" in emails   # now included


def test_reconcile_without_target_marks_unknown(db):
    rows = R.reconcile(db, programs=["ielts"])   # no target export
    assert rows and all(r["classification"].startswith("unknown") for r in rows)
