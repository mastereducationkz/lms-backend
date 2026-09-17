"""The pinned timetable stays in the bar at the top of the chat (owner, 2026-09-16).

Telegram shows the newest pinned message by send date there, so a newer pin is answered with a fresh
copy of the timetable, pinned silently, and the old copy retired. Support is faked at
:mod:`group_bot_outbox`; everything else is the real database. Times are naive UTC.
"""
from datetime import timedelta

import pytest

from src.announcements.models import TelegramPinnedTimetable
from src.routes.support_api import GroupPinnedIn, group_message_pinned
from src.services import group_bot_outbox as outbox
from src.services import group_bot_pinned as pinned
from src.services import group_bot_pinned_top as top
from tests.test_group_bot import NOW, SUPPORT_CHAT, chat  # noqa: F401 - fixture
from tests.test_group_bot_v3_jobs import _greet, live  # noqa: F401 - fixture
from tests.test_operational_groups import db, world  # noqa: F401 - fixtures


@pytest.fixture
def bar(live, monkeypatch):
    """The live chat with its timetable posted (message 1001) and the rest of Support faked."""
    monkeypatch.setenv(pinned.FLAG, "1")
    monkeypatch.setenv(top.FLAG, "1")
    calls = live["calls"]
    calls.update(top=[], delete=[], unpin=[], on_top=1001, delete_result={"ok": True, "gone": False},
                 top_result=None)

    def top_pinned(support_group_id):
        calls["top"].append(support_group_id)
        return calls["top_result"] or {"ok": True, "pinned_message_id": calls["on_top"], "status_code": 200}

    def delete(support_group_id, message_id):
        calls["delete"].append(message_id)
        return calls["delete_result"]

    def unpin(support_group_id, message_id):
        calls["unpin"].append(message_id)
        return {"ok": True, "gone": False}

    monkeypatch.setattr(outbox, "top_pinned", top_pinned)
    monkeypatch.setattr(outbox, "delete", delete)
    monkeypatch.setattr(outbox, "unpin", unpin)

    live["on"](16)
    _greet(live["db"], live["linked"], NOW - timedelta(hours=1))
    live["tick"](pinned, NOW)
    assert live["calls"]["post"][0]["key"] == f"pinned-timetable:{live['linked'].id}"
    live["row"] = lambda: live["db"].query(TelegramPinnedTimetable).one()
    live["top_tick"] = lambda now, calls_left=outbox.CALLS_PER_TICK: top.run(
        live["db"], outbox.live_links(live["db"], now), outbox.Budget(calls_left), now)
    return live


def test_it_waits_for_both_flags(bar, monkeypatch):
    assert top.enabled(bar["db"]) is True
    monkeypatch.delenv(top.FLAG)
    assert top.enabled(bar["db"]) is False
    monkeypatch.setenv(top.FLAG, "1")
    monkeypatch.delenv(pinned.FLAG)
    assert top.enabled(bar["db"]) is False


def test_on_top_nothing_happens(bar):
    summary = bar["top_tick"](NOW)
    assert summary["checked"] == 1 and summary["raised"] == 0
    assert bar["calls"]["delete"] == [] and len(bar["calls"]["post"]) == 1
    bar["top_tick"](NOW + timedelta(minutes=10))
    assert len(bar["calls"]["top"]) == 1, "read again only every half hour"


def test_a_reported_pin_puts_a_fresh_copy_on_top_and_deletes_the_old_one(bar, monkeypatch):
    bar["top_tick"](NOW)                                            # on top at first
    monkeypatch.setattr(outbox, "utcnow", lambda: NOW)
    assert group_message_pinned(GroupPinnedIn(support_group_id=SUPPORT_CHAT, message_id=1500),
                                db=bar["db"]) == {"due": True}
    bar["calls"]["on_top"] = 1500

    bar["top_tick"](NOW + timedelta(seconds=30))
    assert len(bar["calls"]["top"]) == 1, "a burst of pins waits a minute"

    summary = bar["top_tick"](NOW + timedelta(seconds=61))
    assert summary["raised"] == 1
    probe, = bar["calls"]["edit"]
    assert probe["message_id"] == 1001
    new = bar["calls"]["post"][-1]
    assert (new["key"], new["pin"], new["silent"]) == (f"pinned-timetable:{bar['linked'].id}:1", True, True)
    assert new["text"] == bar["calls"]["post"][0]["text"] and new["reply_markup"] == bar["calls"]["post"][0]["reply_markup"]
    assert bar["calls"]["delete"] == [1001] and bar["calls"]["unpin"] == []
    row = bar["row"]()
    assert (row.telegram_message_id, row.raising_from_id, row.raises, row.check_due_at, row.status) == (
        1002, None, 1, None, "posted")


def test_an_older_pin_changes_nothing(bar):
    assert top.note_pin(bar["db"], support_group_id=SUPPORT_CHAT, message_id=900, now=NOW) is False
    assert bar["row"]().check_due_at is None


def test_the_half_hourly_read_catches_a_pin_nobody_reported(bar):
    bar["calls"]["on_top"] = 1500
    assert bar["top_tick"](NOW)["raised"] == 1
    assert bar["calls"]["delete"] == [1001]


def test_a_deleted_timetable_is_not_brought_back(bar):
    bar["calls"]["on_top"] = 1500
    bar["calls"]["edit_result"] = {"ok": False, "gone": True}
    summary = bar["top_tick"](NOW)
    assert summary["removed"] == 1 and len(bar["calls"]["post"]) == 1
    assert bar["row"]().status == "removed"


def test_an_unpinned_timetable_is_respected_after_a_second_look(bar):
    bar["calls"]["on_top"] = 900                                    # an older pin is on top: ours was unpinned
    bar["top_tick"](NOW)
    assert bar["row"]().unpinned_seen_at == NOW and bar["row"]().status == "posted"
    bar["top_tick"](NOW + timedelta(minutes=10))
    assert len(bar["calls"]["top"]) == 1
    summary = bar["top_tick"](NOW + timedelta(minutes=15))
    assert summary["unpinned"] == 1 and bar["row"]().status == "removed"
    assert len(bar["calls"]["post"]) == 1 and bar["calls"]["delete"] == []


def test_a_lagging_read_is_forgiven_when_the_second_look_finds_it_pinned(bar):
    bar["calls"]["on_top"] = None
    bar["top_tick"](NOW)
    bar["calls"]["on_top"] = 1001
    bar["top_tick"](NOW + timedelta(minutes=15))
    row = bar["row"]()
    assert (row.status, row.unpinned_seen_at, row.check_due_at) == ("posted", None, None)


def test_an_old_copy_telegram_keeps_is_unpinned_and_becomes_a_pointer(bar):
    bar["calls"]["on_top"] = 1500
    bar["calls"]["delete_result"] = {"ok": False, "gone": False, "error_code": 400, "status_code": 200,
                                     "description": "Bad Request: message can't be deleted"}
    bar["top_tick"](NOW)
    assert bar["calls"]["unpin"] == [1001]
    pointer = bar["calls"]["edit"][-1]
    assert pointer == {"message_id": 1001, "text": top.STALE_TEXT}
    assert "!" not in top.STALE_TEXT
    assert bar["row"]().telegram_message_id == 1002


def test_a_busy_telegram_retires_the_old_copy_next_tick_instead_of_leaving_a_pointer(bar):
    bar["calls"]["on_top"] = 1500
    bar["calls"]["delete_result"] = {"ok": False, "gone": False, "error_code": 429, "status_code": 200,
                                     "description": "Too Many Requests: retry after 5"}
    bar["top_tick"](NOW)
    assert bar["calls"]["unpin"] == [] and bar["row"]().raising_from_id == 1001
    bar["calls"]["delete_result"] = {"ok": True, "gone": False, "status_code": 200}
    assert bar["top_tick"](NOW + timedelta(minutes=1))["raised"] == 1
    assert bar["calls"]["delete"] == [1001, 1001] and len(bar["calls"]["post"]) == 2


def test_a_refused_pin_takes_the_copy_back_down_and_waits_a_day(bar, monkeypatch):
    real_post = outbox.post

    def post_without_pin(*args, **kwargs):
        return {**real_post(*args, **kwargs), "pin_error": "not enough rights to pin a message"}

    monkeypatch.setattr(outbox, "post", post_without_pin)
    bar["calls"]["on_top"] = 1500
    summary = bar["top_tick"](NOW)
    assert summary["failed"] == 1 and bar["calls"]["delete"] == [1002]
    row = bar["row"]()
    assert (row.telegram_message_id, row.raising_from_id, row.check_due_at) == (1001, None, NOW + top.GIVE_UP_FOR)
    bar["top_tick"](NOW + timedelta(hours=1))
    assert len(bar["calls"]["post"]) == 2, "not retried every half hour"


def test_a_tick_that_runs_out_of_calls_resumes_without_a_second_copy(bar):
    bar["calls"]["on_top"] = 1500
    bar["top_tick"](NOW, calls_left=3)                              # read, probe, post — no calls left to delete
    row = bar["row"]()
    assert (row.telegram_message_id, row.raising_from_id) == (1002, 1001)
    bar["top_tick"](NOW + timedelta(minutes=1))
    assert len(bar["calls"]["post"]) == 2 and bar["calls"]["delete"] == [1001]
    assert bar["row"]().raising_from_id is None


def test_a_failed_post_is_retried_under_the_same_key_then_given_up(bar):
    bar["calls"]["on_top"] = 1500
    bar["calls"]["outcome"] = "failed"
    for minute in range(4):
        bar["top_tick"](NOW + timedelta(minutes=minute))
    keys = {call["key"] for call in bar["calls"]["post"][1:]}
    assert keys == {f"pinned-timetable:{bar['linked'].id}:1"} and len(bar["calls"]["post"]) == 1 + outbox.MAX_ATTEMPTS
    row = bar["row"]()
    assert (row.raising_from_id, row.telegram_message_id, row.check_due_at) == (None, 1001, NOW + timedelta(minutes=3) + top.GIVE_UP_FOR)
    assert bar["calls"]["delete"] == []


def test_the_regular_edits_follow_the_new_copy(bar):
    bar["calls"]["on_top"] = 1500
    bar["top_tick"](NOW)
    bar["on"](15, hour=10, minute=0)                                # the timetable would change
    bar["tick"](pinned, NOW)
    assert bar["calls"]["edit"][-1]["message_id"] == 1002
