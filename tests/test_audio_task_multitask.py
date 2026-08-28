"""`audio_task` — a voice recording as a sub-task inside multi_task homework.

Like the standalone `audio` assignment type it is graded by a human only, so it
must never be swept up by the bulk "auto-grade completion homework" action.

No DB / TestClient needed — validation and auto-grade eligibility are pure.
"""
import json

import pytest
from fastapi import HTTPException

from src.admin.routes.dashboard import _is_auto_gradable_multitask
from src.assignments.routes.assignments import validate_assignment_content


def _task(task_type, content, task_id="t1"):
    return {
        "id": task_id, "task_type": task_type, "title": "T",
        "order_index": 0, "points": 10, "content": content,
    }


def _multi(*tasks):
    return {"tasks": list(tasks)}


class _A:
    def __init__(self, assignment_type, tasks):
        self.assignment_type = assignment_type
        self.content = json.dumps({"tasks": [{"task_type": t} for t in tasks]})


# --- validate_assignment_content -------------------------------------------------

def test_audio_task_is_a_valid_task_type():
    validate_assignment_content(
        "multi_task", _multi(_task("audio_task", {"question": "Расскажите о себе"}))
    )


def test_audio_task_without_question_is_rejected():
    with pytest.raises(HTTPException) as exc:
        validate_assignment_content("multi_task", _multi(_task("audio_task", {})))
    assert exc.value.status_code == 400
    assert "audio_task" in exc.value.detail
    assert "question" in exc.value.detail


def test_audio_task_mixes_with_other_task_types():
    validate_assignment_content("multi_task", _multi(
        _task("link_task", {"url": "https://example.com"}, task_id="a"),
        _task("audio_task", {"question": "Speak"}, task_id="b"),
        _task("text_task", {"question": "Write"}, task_id="c"),
    ))


def test_a_bad_sibling_still_fails_alongside_audio_task():
    """audio_task passing must not short-circuit the other tasks' validation."""
    with pytest.raises(HTTPException) as exc:
        validate_assignment_content("multi_task", _multi(
            _task("audio_task", {"question": "Speak"}, task_id="a"),
            _task("link_task", {}, task_id="b"),
        ))
    assert exc.value.status_code == 400
    assert "link_task" in exc.value.detail


def test_unknown_task_type_is_still_rejected():
    with pytest.raises(HTTPException) as exc:
        validate_assignment_content("multi_task", _multi(_task("audio", {"question": "q"})))
    assert exc.value.status_code == 400
    assert "invalid task_type" in exc.value.detail


# --- bulk auto-grade eligibility -------------------------------------------------

def test_audio_task_blocks_bulk_auto_grade():
    """Mirrors standalone `audio`: the recording needs a teacher to listen to it."""
    assert _is_auto_gradable_multitask(_A("multi_task", ["audio_task"])) is False
    assert _is_auto_gradable_multitask(_A("multi_task", ["link_task", "audio_task"])) is False
    assert _is_auto_gradable_multitask(
        _A("multi_task", ["course_unit", "text_task", "audio_task"])) is False


def test_siblings_without_audio_task_stay_auto_gradable():
    assert _is_auto_gradable_multitask(_A("multi_task", ["link_task", "course_unit"])) is True
    # text_task/pdf_text_task moved to the needs-review set in c7545bf: a typed
    # answer must be read by a human before it earns full marks.
    assert _is_auto_gradable_multitask(_A("multi_task", ["text_task", "pdf_text_task"])) is False
