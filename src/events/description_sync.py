"""Keep the narrow, generated lesson label in sync when a group is renamed."""

from sqlalchemy.orm import Session


def scheduled_class_description(group_name: str) -> str:
    return f"Scheduled class for {group_name}"


def replacement_for_group_rename(
    description: str | None, old_group_name: str, new_group_name: str
) -> str | None:
    """Replace only the exact system-generated description.

    Free-form staff notes must survive group renames, including notes which
    happen to start with the same words as the generated label.
    """
    if description == scheduled_class_description(old_group_name):
        return scheduled_class_description(new_group_name)
    return description


def sync_generated_descriptions_for_group_rename(
    db: Session, group_id: int, old_group_name: str, new_group_name: str
) -> int:
    """Update only class events linked to this group with its exact old label."""
    if old_group_name == new_group_name:
        return 0

    # Imported here to keep this small policy module independent of ORM model
    # import order during application startup.
    from src.schemas.models import Event, EventGroup

    event_ids = db.query(EventGroup.event_id).filter(
        EventGroup.group_id == group_id
    )
    return db.query(Event).filter(
        Event.id.in_(event_ids),
        Event.event_type == "class",
        Event.description == scheduled_class_description(old_group_name),
    ).update(
        {Event.description: scheduled_class_description(new_group_name)},
        synchronize_session=False,
    )
