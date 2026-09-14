"""Onboarding a teacher onto the recordings pipeline: their ``workspace_email``.

Setting ``users.workspace_email`` is the whole activation: the recordings worker gives the
teacher's upcoming lessons LMS Meet rooms, auto-records them, sends Telegram invitations to
linked group chats, and makes their groups eligible for the group bot. This module is the one
rule both admin surfaces share — the recordings page and the Manage Users edit dialog.

A Workspace account is only *referenced* here, never created: accounts are made in the Google
Admin Console from the import this module writes, because the pipeline's OAuth scopes cannot
touch ``admin.directory`` and service-account keys are org-policy-disabled on this tenant. The
uploaded users list (``workspace_directory``) is how the LMS knows which accounts exist.
"""
from __future__ import annotations

import csv
import io
import logging
import re
import secrets
import string
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, List, Optional

from src.schemas.models import AppSetting, UserInDB
from src.services import workspace_directory, workspace_names

logger = logging.getLogger(__name__)

WORKSPACE_DOMAIN = workspace_names.DOMAIN

# Roles whose lessons get LMS Meet rooms once onboarded.
TEACHER_ROLES = frozenset({"teacher", "head_teacher"})

# Who may see the onboarding list; only admins may change anything — the same split the
# group-bot switch uses (src/services/group_bot_settings.py).
READERS = frozenset({"admin", "head_curator", "head_teacher"})
WRITERS = frozenset({"admin"})

ROLLOUT_KEY = "recordings_rollout"
DEFAULT_ORG_UNIT = "/Teachers"
NAME_MAX = 60  # Google's limit for a first or last name

_MAILBOX = re.compile(r"[a-z0-9]+([._-][a-z0-9]+)*")
_ENGLISH_NAME = re.compile(r"[A-Za-z]+(?:[ '\-][A-Za-z]+)*")
_RECOVERY = re.compile(r"[^@\s]+@[^@\s]+\.[a-z]{2,}")


# ── addresses ──────────────────────────────────────────────────────────────────────────────

def normalise_address(email: Optional[str]) -> Optional[str]:
    """Lowercase an address and check its shape. None/blank → None (disconnect)."""
    cleaned = (email or "").strip().lower()
    if not cleaned:
        return None
    if not cleaned.endswith(f"@{WORKSPACE_DOMAIN}"):
        raise ValueError(f"Workspace email must end with @{WORKSPACE_DOMAIN}")
    if not _MAILBOX.fullmatch(cleaned[: -len(WORKSPACE_DOMAIN) - 1]):
        raise ValueError("Not a valid mailbox name")
    return cleaned


def connected_addresses(db) -> set:
    return {row[0] for row in db.query(UserInDB.workspace_email)
            .filter(UserInDB.workspace_email.isnot(None)).all()}


def validate_workspace_email(db, user: UserInDB, email: Optional[str]) -> Optional[str]:
    """Normalise and check an address before it becomes the activation switch.

    Returns the value to store (None = disconnect). Raises ValueError with a readable reason,
    which the route layer turns into a 400. A new address must be an existing, not suspended
    account in the uploaded Workspace users list — see ``workspace_directory``.
    """
    if not (email or "").strip():
        return None
    if user.role not in TEACHER_ROLES:
        raise ValueError("Only teachers and head teachers can be connected to recordings")
    cleaned = normalise_address(email)
    other = (
        db.query(UserInDB)
        .filter(UserInDB.workspace_email == cleaned, UserInDB.id != user.id)
        .first()
    )
    if other is not None:
        raise ValueError(f"{cleaned} is already connected to {other.name or other.email}")
    if cleaned == user.workspace_email:
        return cleaned
    directory = workspace_directory.accounts_by_email(db)
    if directory is None:
        raise ValueError(
            "Upload the Google Workspace users list on Admin → Recordings Rollout first, so the "
            f"LMS can confirm {cleaned} exists")
    account = directory.get(cleaned)
    if account is None:
        raise ValueError(
            f"{cleaned} is not in the Workspace users list uploaded "
            f"{workspace_directory.uploaded_at(db)}. Create the account in Google Admin, then "
            "upload the list again")
    if account.get("suspended"):
        raise ValueError(f"{cleaned} is suspended in Google Workspace")
    return cleaned


def set_workspace_email(db, actor: UserInDB, user: UserInDB, email: Optional[str]) -> UserInDB:
    """Connect or disconnect a teacher. ValueError propagates to the route as a 400."""
    cleaned = validate_workspace_email(db, user, email)
    if cleaned == user.workspace_email:
        return user
    logger.info(
        "recordings onboarding: admin %s set user %s workspace_email %s -> %s",
        getattr(actor, "id", None), user.id, user.workspace_email, cleaned,
    )
    user.workspace_email = cleaned
    if cleaned:
        _write_skipped(db, actor, skipped_ids(db) - {user.id})
    db.commit()
    db.refresh(user)
    return user


# ── skipped teachers ───────────────────────────────────────────────────────────────────────

def skipped_ids(db) -> set:
    """Teachers an admin decided to leave out of the rollout (e.g. a seed account)."""
    row = db.get(AppSetting, ROLLOUT_KEY)
    value = row.value if row is not None and isinstance(row.value, dict) else {}
    return {int(i) for i in value.get("skipped_user_ids", [])}


def _write_skipped(db, actor, ids: set) -> None:
    row = db.get(AppSetting, ROLLOUT_KEY)
    if row is None:
        row = AppSetting(key=ROLLOUT_KEY)
        db.add(row)
    value = dict(row.value) if isinstance(row.value, dict) else {}
    value["skipped_user_ids"] = sorted(ids)
    row.value = value
    row.updated_by = getattr(actor, "id", None)
    row.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)


def set_skipped(db, actor, user: UserInDB, skipped: bool) -> None:
    if user.role not in TEACHER_ROLES:
        raise ValueError("Only teachers and head teachers are part of the recordings rollout")
    ids = skipped_ids(db)
    _write_skipped(db, actor, (ids | {user.id}) if skipped else (ids - {user.id}))
    db.commit()


# ── what the page proposes for a pending teacher ───────────────────────────────────────────

@dataclass
class Proposal:
    first_name: str
    last_name: str
    address: Optional[str]
    #: accounts in the directory with the same first name — "is this the same person?"
    similar: List[str]


def propose(teacher: UserInDB, *, taken: set, directory: Optional[Dict[str, dict]]) -> Proposal:
    """English name and an address for a teacher who is not connected yet.

    An account that already exists for this person wins: their LMS login is a Workspace
    address, or exactly one directory account carries the same first and last name. Otherwise
    the address is new and avoids every address the LMS or Workspace already uses — Google's
    import would overwrite an existing account.
    """
    first, last = workspace_names.english_name(teacher.name, teacher.official_full_name)
    directory = directory or {}
    free = {email: a for email, a in directory.items() if email not in taken}
    login = (teacher.email or "").strip().lower()
    if login.endswith(f"@{WORKSPACE_DOMAIN}") and login not in taken:
        return Proposal(first, last, login, [])
    if first and last:
        same_name = [email for email, a in free.items()
                     if (a.get("first_name") or "").casefold() == first.casefold()
                     and (a.get("last_name") or "").casefold() == last.casefold()]
        if len(same_name) == 1:
            return Proposal(first, last, same_name[0], [])
    similar = sorted(email for email, a in free.items()
                     if first and (a.get("first_name") or "").casefold() == first.casefold())
    address = workspace_names.suggest_address(first, last, taken | set(directory))
    return Proposal(first, last, address, similar)


# ── the Google Admin import ────────────────────────────────────────────────────────────────

IMPORT_HEADER = [
    "First Name [Required]", "Last Name [Required]", "Email Address [Required]",
    "Password [Required]", "Org Unit Path [Required]", "Recovery Email",
    "Change Password at Next Sign-In",
]


class ImportRefused(ValueError):
    def __init__(self, problems: List[str]):
        super().__init__("; ".join(problems))
        self.problems = problems


@dataclass
class ImportRow:
    user_id: int
    workspace_email: str
    first_name: str
    last_name: str


def generate_import_password(length: int = 14) -> str:
    """A temporary password for the import; the file forces a change at first sign-in."""
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def _name_problem(label: str, value: str) -> Optional[str]:
    if not value:
        return f"{label} is empty"
    if len(value) > NAME_MAX:
        return f"{label} is longer than {NAME_MAX} characters"
    if not _ENGLISH_NAME.fullmatch(value):
        return f"{label} must be written in English letters"
    return None


def build_import_csv(db, actor: UserInDB, rows: List[ImportRow], org_unit: Optional[str]) -> str:
    """The Admin-Console bulk-upload file for exactly the rows the admin reviewed on the page.

    Every row must create a NEW account: Google's upload silently updates an account whose
    address exists, resetting its password. So an address in the uploaded users list, an
    address another LMS user is connected to, and a repeated address are all refused, and the
    whole file is refused rather than written without them. Passwords exist only in the file.
    """
    org = (org_unit or DEFAULT_ORG_UNIT).strip()
    problems: List[str] = []
    if not org.startswith("/") or any(ch in org for ch in "\r\n,\""):
        problems.append("Org unit path must start with / (for example /Teachers)")
    directory = workspace_directory.accounts_by_email(db)
    if directory is None:
        problems.append(
            "Upload the Google Workspace users list first: without it the LMS cannot tell which "
            "addresses already exist, and Google's upload overwrites an existing account")
    if not rows:
        problems.append("Choose at least one teacher")
    if problems:
        raise ImportRefused(problems)

    users = {u.id: u for u in db.query(UserInDB).filter(UserInDB.id.in_({r.user_id for r in rows})).all()}
    taken = connected_addresses(db)
    seen: Dict[str, str] = {}
    lines = []
    for row in rows:
        user = users.get(row.user_id)
        label = (user.name if user else None) or f"user #{row.user_id}"
        issues: List[str] = []
        if user is None or user.role not in TEACHER_ROLES:
            issues.append("not a teacher")
        elif not user.is_active:
            issues.append("the LMS account is deactivated")
        elif user.workspace_email:
            issues.append(f"already connected to {user.workspace_email}")
        email = None
        try:
            email = normalise_address(row.workspace_email)
            if email is None:
                issues.append("no Workspace address")
        except ValueError as e:
            issues.append(str(e))
        if email:
            if email in directory:
                issues.append(f"{email} already exists in Google Workspace — connect it instead; "
                              "uploading it would overwrite that account")
            elif email in taken:
                issues.append(f"{email} is connected to another LMS user")
            if email in seen:
                issues.append(f"{email} is also chosen for {seen[email]}")
            seen.setdefault(email, label)
        first, last = (row.first_name or "").strip(), (row.last_name or "").strip()
        issues += [p for p in (_name_problem("First name", first), _name_problem("Last name", last)) if p]
        if issues:
            problems.append(f"{label}: {'; '.join(issues)}")
            continue
        login = (user.email or "").strip().lower()
        recovery = login if _RECOVERY.fullmatch(login) and not login.endswith(f"@{WORKSPACE_DOMAIN}") else ""
        lines.append([first, last, email, generate_import_password(), org, recovery, "TRUE"])
    if problems:
        raise ImportRefused(problems)

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(IMPORT_HEADER)
    writer.writerows(lines)
    logger.info("recordings onboarding: admin %s exported a Workspace import for users %s",
                getattr(actor, "id", None), [r.user_id for r in rows])
    return buf.getvalue()
