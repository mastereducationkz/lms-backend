"""Per-target delivery state for the student-sync drainer (Platform Integration Pack §4).

Before this, a pending row was re-posted to EVERY target on each retry, so a SAT outage
re-hit IELTS ~63k times/day. Now ``student_sync_outbox.target_state`` remembers each target's
outcome; a retry posts only to targets not yet ``ok``/``skipped``; the row is ``done`` when every
configured target is ``ok``/``skipped`` and ``failed`` when any single target exhausts its budget.
SQLite + mocked HTTP, same style as tests/test_student_sync.py.
"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import src.schemas.models  # noqa: F401 - register models
from src.courses.models import StudentSyncOutbox
from src.services import student_sync

SAT = "https://api.mastereducation.kz/api/lms"
IELTS = "https://ielts.example/api"


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    StudentSyncOutbox.__table__.create(bind=engine)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture(autouse=True)
def both_targets(monkeypatch):
    monkeypatch.setenv("MASTEREDU_API_KEY", "ksat")
    monkeypatch.setenv("IELTS_SYNC_URL", IELTS)
    monkeypatch.setenv("IELTS_API_KEY", "kielts")


class _Resp:
    def __init__(self, code, text="", body=None):
        self.status_code = code
        self.text = text
        self._body = body

    def json(self):
        if self._body is None:
            raise ValueError("no json body")
        return self._body


def _seed_member(db, target_state=None, eid="m1"):
    payload = {
        "event_id": eid, "event_type": "member.upserted", "source": "lms_db_trigger",
        "lms_group_id": 9, "program_type": "nuet",
        "student": {"email": "s@x.io", "central_auth_user_id": "c", "name": "S"},
    }
    row = StudentSyncOutbox(event_id=eid, event_type="member.upserted", payload=payload,
                            target_state=target_state)
    db.add(row)
    db.commit()
    return row


def _responder(monkeypatch, by_host):
    """Route each POST by target host; record which targets were hit, in order."""
    hits = []

    def post(url, json, headers, timeout):
        name = "sat" if url.startswith(SAT) else "ielts"
        hits.append(name)
        return by_host[name]() if callable(by_host[name]) else by_host[name]

    monkeypatch.setattr(student_sync.httpx, "post", post)
    return hits


def _redrain(db):
    db.query(StudentSyncOutbox).update({StudentSyncOutbox.next_attempt_at: None})
    db.commit()
    return student_sync.drain_outbox(db)


# --- retry only hits the unfinished targets -----------------------------------

def test_retry_posts_only_to_targets_not_yet_ok(db, monkeypatch):
    # The production defect: sat=not_ready, ielts=ok must NOT re-hit IELTS.
    _seed_member(db, target_state={
        "sat": {"status": "not_ready", "attempts": 0},
        "ielts": {"status": "ok", "attempts": 0},
    })
    hits = _responder(monkeypatch, {"sat": _Resp(200), "ielts": _Resp(200)})

    result = student_sync.drain_outbox(db)

    assert hits == ["sat"]
    assert result == {"published": 1, "failed": 0, "retried": 0}
    row = db.query(StudentSyncOutbox).one()
    assert row.status == "done"
    assert row.target_state["sat"]["status"] == "ok"
    assert row.target_state["ielts"]["status"] == "ok"


def test_first_pass_records_each_target_then_reposts_only_the_not_ready_one(db, monkeypatch):
    _seed_member(db)  # existing rows: target_state NULL -> every target attempted once
    hits = _responder(monkeypatch, {"sat": _Resp(503, "off"), "ielts": _Resp(200)})

    result = student_sync.drain_outbox(db)

    assert hits == ["sat", "ielts"]
    assert result == {"published": 0, "failed": 0, "retried": 1}
    row = db.query(StudentSyncOutbox).one()
    assert row.status == "pending"
    assert row.attempts == 0                                   # 503 spends no budget
    assert row.target_state["sat"]["status"] == "not_ready"
    assert row.target_state["sat"]["attempts"] == 0
    assert "503" in row.target_state["sat"]["last_error"]
    assert row.target_state["sat"]["updated_at"]
    assert row.target_state["ielts"]["status"] == "ok"

    _redrain(db)                                               # SAT still off
    assert hits == ["sat", "ielts", "sat"]                     # IELTS not re-hit


# --- a 2xx "skipped" body is a terminal outcome for that target ---------------

@pytest.mark.parametrize("body", [{"skipped": True, "reason": "program"}, {"status": "skipped"}])
def test_skipped_body_marks_target_skipped_and_row_done(db, monkeypatch, body):
    _seed_member(db)
    hits = _responder(monkeypatch, {"sat": _Resp(200, body={"ok": True}),
                                    "ielts": _Resp(200, body=body)})

    result = student_sync.drain_outbox(db)

    assert result["published"] == 1
    row = db.query(StudentSyncOutbox).one()
    assert row.status == "done"
    assert row.target_state["ielts"]["status"] == "skipped"
    assert row.target_state["sat"]["status"] == "ok"
    assert hits == ["sat", "ielts"]


def test_skipped_target_is_not_reposted_while_sibling_retries(db, monkeypatch):
    _seed_member(db)
    hits = _responder(monkeypatch, {"sat": _Resp(500, "boom"),
                                    "ielts": _Resp(200, body={"skipped": True})})

    student_sync.drain_outbox(db)
    _redrain(db)

    assert hits == ["sat", "ielts", "sat"]
    row = db.query(StudentSyncOutbox).one()
    assert row.status == "pending"
    assert row.target_state["ielts"]["status"] == "skipped"
    assert row.target_state["sat"]["status"] == "retry"
    assert row.target_state["sat"]["attempts"] == 2


# --- dead-letter after one target exhausts its own budget ---------------------

def test_dead_letter_after_max_attempts_on_one_target(db, monkeypatch):
    _seed_member(db)
    hits = _responder(monkeypatch, {"sat": _Resp(200), "ielts": _Resp(500, "boom")})

    last = student_sync.drain_outbox(db)
    for _ in range(student_sync._MAX_ATTEMPTS - 1):
        assert db.query(StudentSyncOutbox).one().status == "pending"
        last = _redrain(db)

    assert last == {"published": 0, "failed": 1, "retried": 0}
    row = db.query(StudentSyncOutbox).one()
    assert row.status == "failed"
    assert row.target_state["ielts"]["attempts"] == student_sync._MAX_ATTEMPTS
    assert row.target_state["ielts"]["status"] == "retry"
    assert row.target_state["sat"]["status"] == "ok"
    assert hits.count("sat") == 1                              # never re-hit the ok target
    assert hits.count("ielts") == student_sync._MAX_ATTEMPTS


def test_not_ready_target_never_spends_its_own_budget(db, monkeypatch):
    _seed_member(db)
    _responder(monkeypatch, {"sat": _Resp(503, "off"), "ielts": _Resp(200)})

    student_sync.drain_outbox(db)
    for _ in range(12):
        _redrain(db)

    row = db.query(StudentSyncOutbox).one()
    assert row.status == "pending"
    assert row.target_state["sat"]["attempts"] == 0


# --- unconfigured targets are outside the contract ---------------------------

def test_unconfigured_target_is_absent_from_state(db, monkeypatch):
    monkeypatch.delenv("IELTS_SYNC_URL", raising=False)
    _seed_member(db)
    hits = _responder(monkeypatch, {"sat": _Resp(200), "ielts": _Resp(200)})

    student_sync.drain_outbox(db)

    assert hits == ["sat"]
    row = db.query(StudentSyncOutbox).one()
    assert row.status == "done"
    assert set(row.target_state) == {"sat"}


# --- replay keeps the targets that already succeeded --------------------------

def test_replay_resets_only_unfinished_targets(db, monkeypatch):
    from src.services import sync_replay

    row = _seed_member(db, target_state={
        "sat": {"status": "ok", "attempts": 1},
        "ielts": {"status": "retry", "attempts": 8, "last_error": "ielts: HTTP 404: no user"},
    })
    row.status = "failed"
    row.attempts = 8
    db.commit()

    res = sync_replay.replay(db)

    assert res["replayed"] == 1
    row = db.query(StudentSyncOutbox).one()
    assert row.status == "pending" and row.attempts == 0
    assert row.target_state["sat"] == {"status": "ok", "attempts": 1}
    assert row.target_state["ielts"]["attempts"] == 0
    assert row.target_state["ielts"]["status"] == "retry"

    hits = _responder(monkeypatch, {"sat": _Resp(200), "ielts": _Resp(200)})
    student_sync.drain_outbox(db)
    assert hits == ["ielts"]
    assert db.query(StudentSyncOutbox).one().status == "done"
