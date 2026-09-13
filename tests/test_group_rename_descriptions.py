import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from src.events.description_sync import (
    replacement_for_group_rename,
    scheduled_class_description,
    sync_generated_descriptions_for_group_rename,
)


OLD_NAME = "Шадеева - IELTS July 6 2026"
NEW_NAME = "IELTS July 6 2026 - Said"


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        connection.execute(text("""
            CREATE TABLE events (
                id INTEGER PRIMARY KEY,
                description TEXT,
                event_type TEXT,
                updated_at TEXT
            )
        """))
        connection.execute(text("""
            CREATE TABLE event_groups (
                id INTEGER PRIMARY KEY,
                event_id INTEGER,
                group_id INTEGER
            )
        """))
    return sessionmaker(bind=engine)()


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


def test_sync_updates_only_exact_class_descriptions_linked_to_the_renamed_group(db):
    old_description = scheduled_class_description(OLD_NAME)
    db.execute(text("""
        INSERT INTO events (id, description, event_type) VALUES
        (1, :old_description, 'class'),
        (2, :old_description, 'class'),
        (3, :old_description, 'webinar'),
        (4, 'Scheduled class for room 204; bring mock-test results.', 'class')
    """), {"old_description": old_description})
    db.execute(text("""
        INSERT INTO event_groups (id, event_id, group_id) VALUES
        (1, 1, 253), (2, 2, 999), (3, 3, 253), (4, 4, 253)
    """))
    db.commit()

    changed = sync_generated_descriptions_for_group_rename(
        db, 253, OLD_NAME, NEW_NAME
    )
    descriptions = dict(db.execute(text(
        "SELECT id, description FROM events ORDER BY id"
    )).all())

    assert changed == 1
    assert descriptions == {
        1: scheduled_class_description(NEW_NAME),
        2: old_description,
        3: old_description,
        4: "Scheduled class for room 204; bring mock-test results.",
    }
