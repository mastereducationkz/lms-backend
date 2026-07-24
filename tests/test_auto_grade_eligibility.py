"""_is_auto_gradable_multitask: bulk auto-grade covers completion homework, never
anything with audio or a student file upload (those still need manual review)."""
import json
from src.admin.routes.dashboard import _is_auto_gradable_multitask


class _A:
    def __init__(self, assignment_type, tasks):
        self.assignment_type = assignment_type
        self.content = json.dumps({"tasks": [{"task_type": t} for t in tasks]})


def test_completion_tasks_are_eligible():
    assert _is_auto_gradable_multitask(_A("multi_task", ["link_task", "course_unit"])) is True
    assert _is_auto_gradable_multitask(_A("multi_task", ["text_task"])) is True
    assert _is_auto_gradable_multitask(_A("multi_task", ["pdf_text_task", "link_task"])) is True


def test_audio_or_file_task_blocks():
    assert _is_auto_gradable_multitask(_A("multi_task", ["link_task", "audio"])) is False
    assert _is_auto_gradable_multitask(_A("multi_task", ["file_task"])) is False
    assert _is_auto_gradable_multitask(_A("multi_task", ["course_unit", "file_task"])) is False


def test_non_multitask_and_empty_are_ineligible():
    assert _is_auto_gradable_multitask(_A("multi_task", [])) is False
    assert _is_auto_gradable_multitask(_A("file_upload", ["link_task"])) is False
    assert _is_auto_gradable_multitask(_A("audio", ["link_task"])) is False


def test_malformed_content_is_safe():
    class Bad:
        assignment_type = "multi_task"
        content = "not json"
    assert _is_auto_gradable_multitask(Bad()) is False
