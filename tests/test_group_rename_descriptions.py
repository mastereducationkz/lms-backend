from src.events.description_sync import (
    replacement_for_group_rename,
    scheduled_class_description,
)


OLD_NAME = "Шадеева - IELTS July 6 2026"
NEW_NAME = "IELTS July 6 2026 - Said"


def test_exact_generated_lesson_description_is_updated_for_a_group_rename():
    assert replacement_for_group_rename(
        scheduled_class_description(OLD_NAME), OLD_NAME, NEW_NAME
    ) == scheduled_class_description(NEW_NAME)


def test_free_form_description_with_the_same_prefix_is_not_updated():
    note = "Scheduled class for room 204; bring mock-test results."
    assert replacement_for_group_rename(note, OLD_NAME, NEW_NAME) == note


def test_unrelated_or_empty_description_is_not_updated():
    assert replacement_for_group_rename("Bring your mock-test results.", OLD_NAME, NEW_NAME) == "Bring your mock-test results."
    assert replacement_for_group_rename(None, OLD_NAME, NEW_NAME) is None
