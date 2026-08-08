"""Student testimonials: consent, moderation and who may see what.

The rules that matter here are not CRUD rules. These are photos and words from students
who are frequently minors, used in advertising, so the tests pin the things that would
be indefensible if they broke: nothing reaches the sales team without a recorded
consent, consent can always be withdrawn, and a withdrawal takes effect immediately.
"""
from datetime import date, datetime, timezone

import pytest
from fastapi import HTTPException

from src.schemas.models import Group, GroupStudent, UserInDB  # noqa: F401  (import-order guard)
from src.exams.models import StudentTestimonial
from src.exams.testimonials import (
    ModerationAction,
    TestimonialUpsert,
    approve_testimonial,
    list_testimonials,
    reject_testimonial,
    revoke_testimonial,
    upsert_testimonial,
)


@pytest.fixture
def db():
    from sqlalchemy import event
    from sqlalchemy.exc import OperationalError
    from sqlalchemy.orm import Session as SASession
    from src.config import engine
    try:
        connection = engine.connect()
    except OperationalError:
        pytest.skip("No database available (requires Postgres); skipping")
    trans = connection.begin()
    session = SASession(bind=connection)
    session.begin_nested()

    @event.listens_for(session, "after_transaction_end")
    def _restart_savepoint(sess, transaction):
        if transaction.nested and not transaction._parent.nested:
            sess.begin_nested()

    try:
        yield session
    finally:
        event.remove(session, "after_transaction_end", _restart_savepoint)
        session.close()
        trans.rollback()
        connection.close()


def _user(db, role, email, name=None):
    u = UserInDB(email=email, name=name or f"{role} {email}", hashed_password="x",
                 role=role, is_active=True)
    db.add(u)
    db.flush()
    return u


@pytest.fixture
def world(db):
    curator = _user(db, "curator", "ts-cur@t.io")
    other_curator = _user(db, "curator", "ts-cur2@t.io")
    admin = _user(db, "admin", "ts-admin@t.io")
    teacher = _user(db, "teacher", "ts-teach@t.io")

    g = Group(name="ts Group", curator_id=curator.id, teacher_id=teacher.id,
              is_active=True, is_over=False, program_type="sat")
    other = Group(name="ts Other", curator_id=other_curator.id,
                  is_active=True, is_over=False, program_type="sat")
    db.add_all([g, other])
    db.flush()

    student = _user(db, "student", "ts-stu@t.io", name="TS Student")
    foreign = _user(db, "student", "ts-stu2@t.io", name="TS Foreign")
    db.add(GroupStudent(group_id=g.id, student_id=student.id))
    db.add(GroupStudent(group_id=other.id, student_id=foreign.id))
    db.flush()
    return dict(curator=curator, admin=admin, teacher=teacher,
                student=student, foreign=foreign)


def _payload(student_id, **kw):
    base = dict(student_id=student_id, quote="It changed everything.",
                consent_given=True, consent_channels=["website"],
                guardian_consent=False, consent_note="verbally at the centre")
    base.update(kw)
    return TestimonialUpsert(**base)


# --------------------------------------------------------------------------------------
# Consent gates approval - the whole point of the model
# --------------------------------------------------------------------------------------

def test_cannot_approve_without_recorded_consent(db, world):
    row = upsert_testimonial(_payload(world["student"].id, consent_given=False,
                                      consent_channels=[]),
                             current_user=world["curator"], db=db)
    with pytest.raises(HTTPException) as exc:
        approve_testimonial(row.id, current_user=world["admin"], db=db)
    assert exc.value.status_code == 400
    assert "consent" in str(exc.value.detail).lower()


def test_cannot_approve_without_at_least_one_channel(db, world):
    """Agreeing to a website quote is not agreeing to appear in paid advertising."""
    row = upsert_testimonial(_payload(world["student"].id, consent_channels=[]),
                             current_user=world["curator"], db=db)
    with pytest.raises(HTTPException) as exc:
        approve_testimonial(row.id, current_user=world["admin"], db=db)
    assert exc.value.status_code == 400
    assert "channel" in str(exc.value.detail).lower()


def test_approving_with_consent_makes_it_marketing_ready(db, world):
    row = upsert_testimonial(_payload(world["student"].id),
                             current_user=world["curator"], db=db)
    out = approve_testimonial(row.id, current_user=world["admin"], db=db)
    assert out.status == "approved"
    assert out.is_marketing_ready is True


def test_recording_consent_captures_who_and_when(db, world):
    row = upsert_testimonial(_payload(world["student"].id),
                             current_user=world["curator"], db=db)
    stored = db.query(StudentTestimonial).filter(StudentTestimonial.id == row.id).one()
    assert stored.consent_recorded_by == world["curator"].id
    assert stored.consent_recorded_at is not None


def test_unknown_consent_channel_is_rejected(db, world):
    with pytest.raises(HTTPException) as exc:
        upsert_testimonial(_payload(world["student"].id, consent_channels=["billboard"]),
                           current_user=world["curator"], db=db)
    assert exc.value.status_code == 400


# --------------------------------------------------------------------------------------
# Revocation - consent that cannot be withdrawn is not consent
# --------------------------------------------------------------------------------------

def test_revoking_immediately_removes_it_from_marketing(db, world):
    row = upsert_testimonial(_payload(world["student"].id),
                             current_user=world["curator"], db=db)
    approve_testimonial(row.id, current_user=world["admin"], db=db)

    out = revoke_testimonial(row.id, ModerationAction(reason="student asked"),
                             current_user=world["curator"], db=db)
    assert out.status == "revoked"
    assert out.is_marketing_ready is False

    ready = list_testimonials(marketing_ready=True, status=None,
                              current_user=world["admin"], db=db)
    assert all(x.student_id != world["student"].id for x in ready)


def test_a_revoked_testimonial_cannot_be_edited_back(db, world):
    row = upsert_testimonial(_payload(world["student"].id),
                             current_user=world["curator"], db=db)
    revoke_testimonial(row.id, ModerationAction(), current_user=world["curator"], db=db)
    with pytest.raises(HTTPException) as exc:
        upsert_testimonial(_payload(world["student"].id), current_user=world["curator"], db=db)
    assert exc.value.status_code == 409


def test_a_revoked_testimonial_cannot_be_approved(db, world):
    row = upsert_testimonial(_payload(world["student"].id),
                             current_user=world["curator"], db=db)
    revoke_testimonial(row.id, ModerationAction(), current_user=world["curator"], db=db)
    with pytest.raises(HTTPException) as exc:
        approve_testimonial(row.id, current_user=world["admin"], db=db)
    assert exc.value.status_code == 409


def test_the_collecting_curator_can_revoke_without_an_approver(db, world):
    """Whoever hears "take my photo down" must be able to act immediately."""
    row = upsert_testimonial(_payload(world["student"].id),
                             current_user=world["curator"], db=db)
    approve_testimonial(row.id, current_user=world["admin"], db=db)
    out = revoke_testimonial(row.id, ModerationAction(), current_user=world["curator"], db=db)
    assert out.status == "revoked"


# --------------------------------------------------------------------------------------
# Re-approval after a change
# --------------------------------------------------------------------------------------

def test_editing_the_quote_after_approval_sends_it_back_for_review(db, world):
    """An approval attests to the material as reviewed; changing it must be re-checked."""
    row = upsert_testimonial(_payload(world["student"].id),
                             current_user=world["curator"], db=db)
    approve_testimonial(row.id, current_user=world["admin"], db=db)

    out = upsert_testimonial(_payload(world["student"].id, quote="Different words entirely"),
                             current_user=world["curator"], db=db)
    assert out.status == "pending"
    assert out.is_marketing_ready is False


def test_narrowing_the_channels_after_approval_also_re_opens_review(db, world):
    row = upsert_testimonial(_payload(world["student"].id, consent_channels=["website", "ads"]),
                             current_user=world["curator"], db=db)
    approve_testimonial(row.id, current_user=world["admin"], db=db)

    out = upsert_testimonial(_payload(world["student"].id, consent_channels=["website"]),
                             current_user=world["curator"], db=db)
    assert out.status == "pending"


def test_withdrawing_consent_clears_the_recorded_consent(db, world):
    upsert_testimonial(_payload(world["student"].id), current_user=world["curator"], db=db)
    out = upsert_testimonial(_payload(world["student"].id, consent_given=False,
                                      consent_channels=[]),
                             current_user=world["curator"], db=db)
    assert out.consent_given is False
    assert out.consent_recorded_at is None


# --------------------------------------------------------------------------------------
# Authorization
# --------------------------------------------------------------------------------------

def test_teachers_cannot_collect_testimonials(db, world):
    with pytest.raises(HTTPException) as exc:
        upsert_testimonial(_payload(world["student"].id), current_user=world["teacher"], db=db)
    assert exc.value.status_code == 403


def test_curator_cannot_collect_for_a_foreign_students(db, world):
    with pytest.raises(HTTPException) as exc:
        upsert_testimonial(_payload(world["foreign"].id), current_user=world["curator"], db=db)
    assert exc.value.status_code == 403


def test_a_curator_cannot_approve_their_own_collection(db, world):
    """Approval is deliberately narrower than collection, so the collector is not the
    only check on the material."""
    row = upsert_testimonial(_payload(world["student"].id),
                             current_user=world["curator"], db=db)
    with pytest.raises(HTTPException) as exc:
        approve_testimonial(row.id, current_user=world["curator"], db=db)
    assert exc.value.status_code == 403


def test_students_cannot_touch_testimonials_at_all(db, world):
    with pytest.raises(HTTPException) as exc:
        list_testimonials(marketing_ready=False, status=None,
                          current_user=world["student"], db=db)
    assert exc.value.status_code == 403


def test_listing_is_row_scoped(db, world):
    upsert_testimonial(_payload(world["student"].id), current_user=world["curator"], db=db)
    seen = list_testimonials(marketing_ready=False, status=None,
                             current_user=world["curator"], db=db)
    assert {x.student_id for x in seen} == {world["student"].id}


def test_payload_never_exposes_the_photo_storage_key(db, world):
    """A student photo is PII; the key is only handed out by the scope-checked endpoint."""
    row = upsert_testimonial(_payload(world["student"].id),
                             current_user=world["curator"], db=db)
    stored = db.query(StudentTestimonial).filter(StudentTestimonial.id == row.id).one()
    stored.photo_url = "/uploads/testimonial_media/secret-key-123.jpg"
    db.flush()

    listed = list_testimonials(marketing_ready=False, status=None,
                               current_user=world["curator"], db=db)
    blob = "".join(x.model_dump_json() for x in listed)
    assert "secret-key-123" not in blob
    assert any(x.has_photo for x in listed)


def test_rejecting_records_the_reason(db, world):
    row = upsert_testimonial(_payload(world["student"].id),
                             current_user=world["curator"], db=db)
    out = reject_testimonial(row.id, ModerationAction(reason="photo too blurry"),
                             current_user=world["admin"], db=db)
    assert out.status == "rejected"
    assert out.rejected_reason == "photo too blurry"


def test_photo_is_streamed_not_redirected(db, world, monkeypatch):
    """Same CORS regression as the exam proof: a 307 to a presigned S3 URL is followed
    by the browser during an XHR, and S3 sends no Access-Control-Allow-Origin."""
    from starlette.responses import RedirectResponse
    from src.exams.testimonials import get_testimonial_photo
    from src.services import storage_service

    row = upsert_testimonial(_payload(world["student"].id),
                             current_user=world["curator"], db=db)
    stored = db.query(StudentTestimonial).filter(StudentTestimonial.id == row.id).one()
    stored.photo_url = "/uploads/testimonial_media/x.png"
    db.flush()
    monkeypatch.setattr(storage_service, "read", lambda key: b"\x89PNG\r\n\x1a\n data")

    resp = get_testimonial_photo(row.id, current_user=world["curator"], db=db)
    assert not isinstance(resp, RedirectResponse), "must not redirect to storage"
    assert resp.status_code == 200
    assert resp.body.startswith(b"\x89PNG")
    assert resp.headers["content-type"].startswith("image/png")
    assert "amazonaws" not in str(resp.headers)
