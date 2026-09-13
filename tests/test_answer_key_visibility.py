from datetime import datetime, timedelta

from src.assignments.answer_keys import student_visible_answer_keys, strip_answer_keys


def test_after_submission_key_is_hidden_until_the_student_submits():
    task = {
        "id": "paired-passages",
        "answer_keys": [{
            "id": "worked-example",
            "title": "Worked example",
            "release_policy": "after_submission",
            "resources": [{"kind": "text", "body": "Explanation"}],
        }],
    }

    assert student_visible_answer_keys(task, submitted=False, due_date=None) == []

    keys = student_visible_answer_keys(task, submitted=True, due_date=None)
    assert [key["id"] for key in keys] == ["worked-example"]
    assert keys[0]["resources"][0]["body"] == "Explanation"


def test_due_date_and_manual_release_policies_are_enforced():
    task = {"answer_keys": [
        {"id": "deadline", "title": "Deadline", "release_policy": "after_due_date", "resources": []},
        {"id": "manual", "title": "Manual", "release_policy": "manual", "resources": []},
    ]}
    future = datetime.utcnow() + timedelta(days=1)

    assert student_visible_answer_keys(task, submitted=False, due_date=future) == []
    keys = student_visible_answer_keys(task, submitted=False, due_date=future,
                                       manually_released_key_ids={"manual"})
    assert [key["id"] for key in keys] == ["manual"]


def test_regular_student_assignment_payload_never_contains_answer_keys():
    content = {"tasks": [{"id": "task-1", "answer_keys": [{"id": "hidden"}]}]}
    assert "answer_keys" not in strip_answer_keys(content)["tasks"][0]
