"""Where a lesson recording lives in the Shared Drive: Teacher → Group → lesson file.

The archive used to copy everything into the Shared Drive root. That is fine for one pilot
teacher and unusable at the real scale — this LMS has thousands of future lessons, and a
flat root is a folder nobody can browse and nobody can be given partial access to.

The tree exists for two reasons, and the second is the load-bearing one:

* **Browsing.** ``Gulzada Kassymbayeva / July 8 SAT / 2026-09-10 19-00 [14156].mp4`` is
  something a human can navigate. A root holding 2 500 files is not.
* **Access.** Each teacher's folder is shared with that teacher as a **reader**, so they
  can review their own lessons without anyone handing out links, and *cannot* see another
  teacher's. On a flat root the only choices are "everyone sees everything" or "nobody
  sees anything" — sharing is per-folder, so the folder tree is what makes per-teacher
  access possible at all. This mirrors the per-rep folders already used on the sales drive.

Teachers get **reader**, never writer. A recording is payroll evidence (§4.9: no
recording, no pay); a teacher who could delete their own recording could delete the
evidence. Reading is the whole need.

Nothing here is allowed to break an ingest. Every entry point falls back to the Shared
Drive root, because an archive in a slightly wrong place still protects the lesson, while
no archive at all means retention will eventually delete the only remaining copy
(``purge_drive_originals`` refuses to run without ``shared_drive_file_id``).
"""
import logging
import re
from typing import Optional
from zoneinfo import ZoneInfo

from src.services import google_workspace

logger = logging.getLogger(__name__)

FOLDER_MIME = "application/vnd.google-apps.folder"

# Lesson times are stored naive-UTC but teachers think in local time, and these folder
# names are read by teachers. Same convention as src/trials/routes/trials.py.
_ALMATY_TZ = ZoneInfo("Asia/Almaty")

UNKNOWN_TEACHER_FOLDER = "Без преподавателя"
UNGROUPED_FOLDER = "Без группы"

# Drive itself permits nearly anything, but these names are also read by humans and land
# on their filesystems when downloaded. Slashes read as path separators in several Drive
# clients, and control characters break listings.
_UNSAFE = re.compile(r"[/\\\x00-\x1f\x7f]")
_MAX_NAME = 120


def _sanitize(name: str, fallback: str) -> str:
    """Make a database string safe and pleasant as a Drive name."""
    cleaned = _UNSAFE.sub("-", (name or "").strip())
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    if len(cleaned) > _MAX_NAME:
        cleaned = cleaned[:_MAX_NAME].rstrip(" .")
    return cleaned or fallback


def _q_escape(value: str) -> str:
    """Escape a value for a Drive ``q`` string literal.

    Real names contain apostrophes. Without this, a teacher called O'Brien turns
    ``name = 'O'Brien'`` into a syntax error and the archive silently falls back to root.
    Backslash first, or it would escape the escapes.
    """
    return value.replace("\\", "\\\\").replace("'", "\\'")


def teacher_folder_name(event) -> str:
    """Official ФИО when the CRM has synced one, else the LMS display name."""
    teacher = getattr(event, "teacher", None)
    if teacher is None:
        return UNKNOWN_TEACHER_FOLDER
    name = getattr(teacher, "official_full_name", None) or getattr(teacher, "name", None)
    return _sanitize(name, UNKNOWN_TEACHER_FOLDER)


def group_folder_name(event) -> str:
    """The lesson's group.

    An event can carry several groups (``event_groups`` is many-to-many). Picking by
    lowest group id rather than list order makes the folder a lesson lands in stable
    across runs — list order is whatever the query returned, so without this the same
    lesson could archive into a different folder on a retry.
    """
    links = list(getattr(event, "event_groups", None) or [])
    groups = [lnk.group for lnk in links if getattr(lnk, "group", None) is not None]
    if not groups:
        return UNGROUPED_FOLDER
    first = min(groups, key=lambda g: getattr(g, "id", 0) or 0)
    return _sanitize(getattr(first, "name", None), UNGROUPED_FOLDER)


def lesson_file_name(event, suffix: str = ".mp4") -> str:
    """``2026-09-10 19-00 — Topic [14156].mp4``.

    Date first so a name sort is a chronological sort. The event id is carried in the
    name because it is the one identifier that ties the file back to the LMS row, the S3
    prefix and the tombstone; a renamed group or retitled lesson must not break that.
    ``topic`` is the per-session field meant for describing the session — the title is
    skipped because it repeats the group and teacher already spelled out by the folders.
    """
    local = event.start_datetime.replace(tzinfo=ZoneInfo("UTC")).astimezone(_ALMATY_TZ)
    stamp = local.strftime("%Y-%m-%d %H-%M")
    topic = _sanitize(getattr(event, "topic", None) or "", "")
    middle = f" — {topic}" if topic else ""
    return f"{stamp}{middle} [{event.id}]{suffix}"


def _find_folder(drive, name: str, parent_id: str) -> Optional[str]:
    res = drive.files().list(
        q=(f"name = '{_q_escape(name)}' and '{parent_id}' in parents "
           f"and mimeType = '{FOLDER_MIME}' and trashed = false"),
        fields="files(id)",
        pageSize=1,
        supportsAllDrives=True,
        includeItemsFromAllDrives=True,
    ).execute()
    files = res.get("files", [])
    return files[0]["id"] if files else None


def _find_or_create_folder(drive, name: str, parent_id: str) -> str:
    """Look up first, create second — this runs on every recording, forever."""
    existing = _find_folder(drive, name, parent_id)
    if existing:
        return existing
    created = drive.files().create(
        body={"name": name, "mimeType": FOLDER_MIME, "parents": [parent_id]},
        fields="id",
        supportsAllDrives=True,
    ).execute()
    logger.info("archive: created Drive folder %r under %s", name, parent_id)
    return created["id"]


def _share_with_teacher(drive, folder_id: str, email: str) -> None:
    """Give the teacher read access to their own folder, once.

    Checked before granting rather than granted blindly: this runs on every recording,
    and a permission write per recording is both noise and a chance to clobber a
    permission an admin set by hand. ``sendNotificationEmail=False`` because teachers
    should not get a Google email every time a lesson is archived.
    """
    perms = drive.permissions().list(
        fileId=folder_id,
        fields="permissions(id,emailAddress,role)",
        supportsAllDrives=True,
    ).execute().get("permissions", [])
    if any((p.get("emailAddress") or "").lower() == email.lower() for p in perms):
        return
    drive.permissions().create(
        fileId=folder_id,
        body={"type": "user", "role": "reader", "emailAddress": email},
        sendNotificationEmail=False,
        supportsAllDrives=True,
    ).execute()
    logger.info("archive: shared folder %s with %s as reader", folder_id, email)


def ensure_lesson_folder(event) -> str:
    """Return the Drive folder id this lesson's files belong in.

    Falls back to the Shared Drive root on any failure. A misfiled archive is a tidiness
    problem; a missing archive is a data-loss problem, because retention treats "no
    ``shared_drive_file_id``" as "do not purge the original" and the lesson would sit
    undeleted forever — or, worse, a later code change could purge it.
    """
    root = google_workspace.RECORDINGS_SHARED_DRIVE_ID
    try:
        drive = google_workspace.drive_client()
        teacher_id = _find_or_create_folder(drive, teacher_folder_name(event), root)

        workspace_email = getattr(getattr(event, "teacher", None), "workspace_email", None)
        if workspace_email:
            try:
                _share_with_teacher(drive, teacher_id, workspace_email)
            except Exception as e:
                # Sharing is a convenience; the archive is the point.
                logger.error("archive: could not share folder %s with %s: %s",
                             teacher_id, workspace_email, e)

        return _find_or_create_folder(drive, group_folder_name(event), teacher_id)
    except Exception as e:
        logger.error("archive: falling back to Shared Drive root for lesson %s: %s",
                     getattr(event, "id", "?"), e)
        return root
