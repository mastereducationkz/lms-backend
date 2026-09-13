"""How multi-group weekly-test calendar events are shown to a viewer: no foreign group name in
the title, and only the viewer's own groups listed (lead decision 2026-09-05)."""

from types import SimpleNamespace

from src.events.display import display_title, display_groups


def _event(event_type, title, groups):
    return SimpleNamespace(
        event_type=event_type, title=title,
        event_groups=[SimpleNamespace(group_id=gid, group=SimpleNamespace(name=name)) for gid, name in groups],
    )


GROUPS = [(1, "August 14 SAT - A"), (2, "Indi Ranya SAT - B"), (3, "June 20 SAT - C")]


def test_class_events_keep_the_first_group_prefix():
    ev = _event("class", "Lesson 3", GROUPS[:1])
    assert display_title(ev, ["August 14 SAT - A"]) == "August 14 SAT - A: Lesson 3"
    assert display_title(_event("class", "August 14 SAT - A: Lesson 3", GROUPS[:1]), ["August 14 SAT - A"]) \
        == "August 14 SAT - A: Lesson 3"
    assert display_title(_event("class", "Lesson 3", []), []) == "Lesson 3"


def test_weekly_test_events_are_never_prefixed_with_a_group():
    ev = _event("weekly_test", "SAT Weekly Test · Weekly Set (05.09-06.09)", GROUPS)
    assert display_title(ev, [g[1] for g in GROUPS]) == "SAT Weekly Test · Weekly Set (05.09-06.09)"


def test_weekly_test_events_list_only_the_viewers_groups():
    ev = _event("weekly_test", "SAT Weekly Test", GROUPS)
    assert display_groups(ev, viewer_group_ids={2, 99}) == ["Indi Ranya SAT - B"]
    assert display_groups(ev, viewer_group_ids=None) == [g[1] for g in GROUPS]   # staff: everything
    assert display_groups(ev, viewer_group_ids=set()) == []


def test_other_events_list_all_their_groups_for_everyone():
    ev = _event("class", "Lesson 3", GROUPS[:2])
    assert display_groups(ev, viewer_group_ids={1}) == ["August 14 SAT - A", "Indi Ranya SAT - B"]
