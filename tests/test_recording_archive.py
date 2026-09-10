"""The Shared Drive folder tree: Teacher → Group → lesson file."""
from datetime import datetime

import pytest

from src.services import recording_archive


class _Teacher:
    def __init__(self, name="Gulzada Kassymbayeva", official=None, workspace="g@mastereducation.kz"):
        self.name = name
        self.official_full_name = official
        self.workspace_email = workspace


class _Group:
    def __init__(self, id, name):
        self.id = id
        self.name = name


class _Link:
    def __init__(self, group):
        self.group = group


class _Event:
    def __init__(self, id=14156, teacher=None, groups=(), topic=None,
                 start=datetime(2026, 9, 10, 14, 0)):
        self.id = id
        self.teacher = teacher if teacher is not None else _Teacher()
        self.event_groups = [_Link(g) for g in groups]
        self.topic = topic
        self.start_datetime = start


class _FakeFiles:
    def __init__(self, sink, existing=None):
        self.sink = sink
        self.existing = existing or {}
        self._next = 100

    def list(self, **kw):
        self.sink.append(("list", kw))
        q = kw["q"]
        hit = [fid for key, fid in self.existing.items() if key in q]
        return _Exec({"files": [{"id": hit[0]}] if hit else []})

    def create(self, **kw):
        self.sink.append(("create", kw))
        self._next += 1
        return _Exec({"id": f"folder{self._next}"})

    def copy(self, **kw):
        self.sink.append(("copy", kw))
        return _Exec({"id": "copied1"})


class _FakePerms:
    def __init__(self, sink, existing=()):
        self.sink = sink
        self.existing = list(existing)

    def list(self, **kw):
        self.sink.append(("perm.list", kw))
        return _Exec({"permissions": self.existing})

    def create(self, **kw):
        self.sink.append(("perm.create", kw))
        return _Exec({"id": "perm1"})


class _Exec:
    def __init__(self, value):
        self.value = value

    def execute(self):
        return self.value


class _FakeDrive:
    def __init__(self, sink, existing=None, perms=()):
        self._files = _FakeFiles(sink, existing)
        self._perms = _FakePerms(sink, perms)

    def files(self):
        return self._files

    def permissions(self):
        return self._perms


@pytest.fixture
def drive(monkeypatch):
    sink = []

    def make(existing=None, perms=()):
        d = _FakeDrive(sink, existing, perms)
        monkeypatch.setattr(recording_archive.google_workspace, "drive_client", lambda: d)
        monkeypatch.setattr(recording_archive.google_workspace,
                            "RECORDINGS_SHARED_DRIVE_ID", "ROOT")
        return sink

    return make


# --- naming ---------------------------------------------------------------


def test_lesson_file_is_named_in_the_teachers_local_time():
    """Stored naive-UTC; a teacher browsing Drive thinks in Almaty time.

    14:00 UTC is the 19:00 lesson. A file named 14-00 would look like a different lesson
    to the person who taught it.
    """
    name = recording_archive.lesson_file_name(_Event())
    assert name.startswith("2026-09-10 19-00")
    assert name.endswith("[14156].mp4")


def test_file_name_sorts_chronologically():
    early = recording_archive.lesson_file_name(_Event(start=datetime(2026, 9, 10, 4, 0)))
    late = recording_archive.lesson_file_name(_Event(start=datetime(2026, 9, 10, 14, 0)))
    assert early < late


def test_topic_is_included_when_set_and_omitted_when_not():
    assert " — Quadratics [" in recording_archive.lesson_file_name(_Event(topic="Quadratics"))
    assert " — " not in recording_archive.lesson_file_name(_Event(topic=None))


def test_official_full_name_wins_over_display_name():
    ev = _Event(teacher=_Teacher(name="Gulzada", official="Кассымбаева Гульзада"))
    assert recording_archive.teacher_folder_name(ev) == "Кассымбаева Гульзада"


def test_slashes_never_reach_a_folder_name():
    """A slash reads as a path separator in several Drive clients."""
    ev = _Event(groups=[_Group(1, "SAT 2026/2027")])
    assert "/" not in recording_archive.group_folder_name(ev)


def test_group_choice_is_stable_when_a_lesson_has_several():
    """List order is whatever the query returned; a retry must not refile the lesson."""
    a = _Event(groups=[_Group(9, "Later"), _Group(2, "Earlier")])
    b = _Event(groups=[_Group(2, "Earlier"), _Group(9, "Later")])
    assert recording_archive.group_folder_name(a) == recording_archive.group_folder_name(b)


def test_a_lesson_with_no_group_still_gets_a_folder():
    assert recording_archive.group_folder_name(_Event()) == recording_archive.UNGROUPED_FOLDER


def test_apostrophes_are_escaped_for_the_drive_query():
    """Unescaped, a teacher called O'Brien makes the query a syntax error."""
    assert recording_archive._q_escape("O'Brien") == "O\\'Brien"


# --- tree + sharing -------------------------------------------------------


def test_builds_teacher_then_group_under_the_shared_drive_root(drive):
    sink = drive()
    folder = recording_archive.ensure_lesson_folder(
        _Event(groups=[_Group(1, "July 8 SAT")]))

    creates = [kw for verb, kw in sink if verb == "create"]
    assert [c["body"]["name"] for c in creates] == ["Gulzada Kassymbayeva", "July 8 SAT"]
    assert creates[0]["body"]["parents"] == ["ROOT"], "teacher folder hangs off the root"

    # The group folder must nest inside the teacher folder that was just created —
    # otherwise both land at the root and the tree is flat in disguise.
    teacher_folder_id = "folder101"
    assert creates[1]["body"]["parents"] == [teacher_folder_id]
    assert folder == "folder102", "returns the group folder, where the file goes"


def test_existing_folders_are_reused_not_duplicated(drive):
    sink = drive(existing={"'Gulzada Kassymbayeva'": "T1", "'July 8 SAT'": "G1"})
    folder = recording_archive.ensure_lesson_folder(
        _Event(groups=[_Group(1, "July 8 SAT")]))

    assert folder == "G1"
    assert not [kw for verb, kw in sink if verb == "create"], "must not re-create folders"


def test_teacher_is_given_read_access_to_their_own_folder(drive):
    sink = drive()
    recording_archive.ensure_lesson_folder(_Event(groups=[_Group(1, "July 8 SAT")]))

    grants = [kw for verb, kw in sink if verb == "perm.create"]
    assert len(grants) == 1
    assert grants[0]["body"]["emailAddress"] == "g@mastereducation.kz"
    assert grants[0]["body"]["role"] == "reader", "a teacher must not delete the evidence"
    assert grants[0]["sendNotificationEmail"] is False


def test_access_is_not_regranted_on_every_recording(drive):
    sink = drive(perms=[{"id": "p1", "emailAddress": "g@mastereducation.kz",
                         "role": "reader"}])
    recording_archive.ensure_lesson_folder(_Event(groups=[_Group(1, "July 8 SAT")]))
    assert not [kw for verb, kw in sink if verb == "perm.create"]


def test_a_teacher_without_a_workspace_email_is_not_shared_with(drive):
    sink = drive()
    recording_archive.ensure_lesson_folder(
        _Event(teacher=_Teacher(workspace=None), groups=[_Group(1, "G")]))
    assert not [kw for verb, kw in sink if verb == "perm.create"]


def test_sharing_failure_does_not_lose_the_folder(drive, monkeypatch):
    """Sharing is a convenience; the archive is the point."""
    sink = drive()

    def boom(**kw):
        raise RuntimeError("permission denied")

    monkeypatch.setattr(recording_archive.google_workspace.drive_client().permissions(),
                        "create", boom)
    folder = recording_archive.ensure_lesson_folder(_Event(groups=[_Group(1, "G")]))
    assert folder.startswith("folder")


def test_drive_failure_falls_back_to_the_root(monkeypatch):
    """A misfiled archive is untidy; a missing one lets retention delete the last copy."""
    monkeypatch.setattr(recording_archive.google_workspace,
                        "RECORDINGS_SHARED_DRIVE_ID", "ROOT")

    def boom():
        raise RuntimeError("Drive is down")

    monkeypatch.setattr(recording_archive.google_workspace, "drive_client", boom)
    assert recording_archive.ensure_lesson_folder(_Event()) == "ROOT"
