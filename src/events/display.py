"""Viewer-facing shape of calendar events.

Group-scoped events carry their group's name in the title ("August 14 SAT - A: Lesson 3").
Auto-managed weekly-test events (``event_type == "weekly_test"``) are attached to every group of
a track at once, so the first group's name would be a stranger's group on most students'
calendars: they keep their own title and list only the viewer's groups."""

from __future__ import annotations

from typing import Iterable, Optional

MULTI_GROUP_TYPES = frozenset({"weekly_test"})


def display_title(event, group_names: Iterable[str]) -> str:
    title = event.title or ""
    if (event.event_type or "") in MULTI_GROUP_TYPES:
        return title
    names = [n for n in group_names if n]
    if names and not title.startswith(names[0]):
        return f"{names[0]}: {title}"
    return title


def display_description(event, group_names: Iterable[str]) -> str | None:
    """Return a current group label for auto-generated class descriptions.

    Lesson events keep their generated description in the database. Group names
    are mutable, so preserving that stored text makes a renamed group appear
    under its former teacher in the calendar. Only the exact generated form is
    derived again; staff-authored descriptions remain historical content.
    """
    description = event.description
    names = [name for name in group_names if name]
    if (
        (event.event_type or "") == "class"
        and len(names) == 1
        and description
        and description.startswith("Scheduled class for ")
    ):
        return f"Scheduled class for {names[0]}"
    return description


def display_groups(event, viewer_group_ids: Optional[Iterable[int]]) -> list[str]:
    """Group names to show: every attached group, except that a multi-group weekly test shows a
    student (``viewer_group_ids`` given) only the groups they belong to."""
    links = [eg for eg in getattr(event, "event_groups", []) or [] if getattr(eg, "group", None)]
    if (event.event_type or "") in MULTI_GROUP_TYPES and viewer_group_ids is not None:
        allowed = set(viewer_group_ids)
        links = [eg for eg in links if eg.group_id in allowed]
    return [eg.group.name for eg in links]
