"""
Logic test for the dashboard homework-updates feed
(src/assignments/routes/assignments.py :: get_homework_updates).

The endpoint itself needs a live Postgres session, so — following the repo
convention for pure-logic tests (see test_recurrence_logic.py) — this replicates
the two tricky, DB-independent pieces and pins their behaviour:

  1. Classification of a non-submitted assignment into due_soon / assigned / None.
  2. Ordering of the mixed feed.

Keep these in sync with the endpoint.
"""
from datetime import datetime, timedelta


# --- mirrors the endpoint's non-submitted classification (step 3) ---
def classify(due, created, now, window_start, window_end):
    if due is not None and now <= due <= window_end:
        return "due_soon"
    if due is not None and due < now:
        return None  # overdue & not submitted — not an update
    if created is not None and created >= window_start:
        return "assigned"
    return None


# --- mirrors the endpoint's ordering (step 4) ---
KIND_RANK = {"due_soon": 0, "graded": 1, "assigned": 2}


def sort_updates(items):
    def key(it):
        ts = it["timestamp"]
        epoch = ts.timestamp() if ts else 0.0
        secondary = epoch if it["kind"] == "due_soon" else -epoch
        return (KIND_RANK[it["kind"]], secondary)
    return sorted(items, key=key)


def test_classification():
    now = datetime(2026, 7, 14, 12, 0, 0)
    ws = now - timedelta(days=7)
    we = now + timedelta(days=7)

    # due tomorrow, not submitted -> due_soon
    assert classify(now + timedelta(days=1), now - timedelta(days=10), now, ws, we) == "due_soon"
    # due far in the future, created recently -> assigned (not due_soon)
    assert classify(now + timedelta(days=30), now - timedelta(days=1), now, ws, we) == "assigned"
    # overdue, not submitted -> None
    assert classify(now - timedelta(days=1), now - timedelta(days=10), now, ws, we) is None
    # no due date, created recently -> assigned
    assert classify(None, now - timedelta(days=2), now, ws, we) == "assigned"
    # no due date, created long ago -> None
    assert classify(None, now - timedelta(days=30), now, ws, we) is None
    # due exactly at window edge -> due_soon (inclusive)
    assert classify(we, None, now, ws, we) == "due_soon"


def test_ordering():
    now = datetime(2026, 7, 14, 12, 0, 0)
    items = [
        {"kind": "assigned", "timestamp": now - timedelta(days=1)},
        {"kind": "graded", "timestamp": now - timedelta(days=3)},
        {"kind": "due_soon", "timestamp": now + timedelta(days=5)},
        {"kind": "graded", "timestamp": now - timedelta(hours=1)},   # newer graded
        {"kind": "due_soon", "timestamp": now + timedelta(days=1)},  # sooner deadline
        {"kind": "assigned", "timestamp": now - timedelta(days=4)},
    ]
    ordered = sort_updates(items)
    kinds = [i["kind"] for i in ordered]
    # groups: all due_soon, then all graded, then all assigned
    assert kinds == ["due_soon", "due_soon", "graded", "graded", "assigned", "assigned"]
    # due_soon: soonest deadline first
    assert ordered[0]["timestamp"] < ordered[1]["timestamp"]
    # graded: most recent first
    assert ordered[2]["timestamp"] > ordered[3]["timestamp"]
    # assigned: most recent first
    assert ordered[4]["timestamp"] > ordered[5]["timestamp"]


if __name__ == "__main__":
    test_classification()
    test_ordering()
    print("ALL PASSED")
