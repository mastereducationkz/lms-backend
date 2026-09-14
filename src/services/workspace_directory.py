"""The Google Workspace users list, as an admin downloaded it from the Admin Console.

The LMS cannot read the Workspace directory: the pipeline's OAuth grant is calendar, drive and
meet only, and service-account keys are disabled on this tenant. The owner closed that gap on
2026-09-14 by uploading Google's own users export, because the blindness had two costs:

* **Google's bulk upload updates an account whose address already exists** — password, name
  and org unit are overwritten. An import row for an address somebody already holds would
  reset that person's password. The import export refuses every address in this list.
* **Connecting a teacher to an address that does not exist** gives their lessons Meet rooms,
  calendar invitations and Drive shares addressed to nobody, with nobody from the organisation
  in the call, so nothing records. Connecting is allowed only for an address in this list,
  and never for a suspended one.

The list is a snapshot and goes stale the moment accounts are created, so the page shows when
it was uploaded and asks for a fresh one after the import.
"""
from __future__ import annotations

import csv
import io
import re
from datetime import datetime, timezone
from typing import Dict, List, Optional

from src.schemas.models import AppSetting, UserInDB
from src.utils.utc_json import utc_z

KEY = "workspace_directory"
MAX_CSV_CHARS = 2_000_000
_NEVER_SIGNED_IN = {"", "never", "never logged in"}


def _column(header: str) -> str:
    """"Email Address [Required]" → "email address"."""
    return re.sub(r"\s*\[[^\]]*\]", "", header or "").strip().lower()


def parse_users_csv(text: Optional[str]) -> List[dict]:
    """Accounts from Google Admin's users CSV. Raises ValueError with a readable reason."""
    if not text or not text.strip():
        raise ValueError("The file is empty")
    if len(text) > MAX_CSV_CHARS:
        raise ValueError("The file is too large to be a users list")
    reader = csv.reader(io.StringIO(text.lstrip("﻿")))
    header = next(reader, [])
    columns = {_column(h): i for i, h in enumerate(header)}
    email_at = columns.get("email address")
    if email_at is None:
        raise ValueError(
            "No 'Email Address' column — download the list from Google Admin → Directory → "
            "Users → Download users (CSV)")

    def cell(row: list, name: str) -> str:
        at = columns.get(name)
        return row[at].strip() if at is not None and at < len(row) else ""

    has_sign_in = "last sign in" in columns
    accounts: Dict[str, dict] = {}
    for row in reader:
        email = row[email_at].strip().lower() if email_at < len(row) else ""
        if "@" not in email:
            continue
        accounts[email] = {
            "email": email,
            "first_name": cell(row, "first name"),
            "last_name": cell(row, "last name"),
            "org_unit": cell(row, "org unit path") or None,
            "suspended": cell(row, "status").lower().startswith("suspend"),
            "signed_in": (cell(row, "last sign in").lower() not in _NEVER_SIGNED_IN) if has_sign_in else None,
        }
    if not accounts:
        raise ValueError("The file lists no accounts")
    return sorted(accounts.values(), key=lambda a: a["email"])


def store(db, user, accounts: List[dict]) -> dict:
    value = {
        "accounts": accounts,
        "uploaded_at": utc_z(datetime.now(timezone.utc).replace(tzinfo=None)),
        "uploaded_by": getattr(user, "id", None),
    }
    row = db.get(AppSetting, KEY)
    if row is None:
        row = AppSetting(key=KEY)
        db.add(row)
    row.value = value
    row.updated_by = getattr(user, "id", None)
    row.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
    db.commit()
    return value


def current(db) -> Optional[dict]:
    row = db.get(AppSetting, KEY)
    value = row.value if row is not None and isinstance(row.value, dict) else None
    return value if value and isinstance(value.get("accounts"), list) else None


def accounts_by_email(db) -> Optional[Dict[str, dict]]:
    """None when no list was ever uploaded — callers must treat that as "unknown", not "empty"."""
    value = current(db)
    return None if value is None else {a["email"]: a for a in value["accounts"]}


def uploaded_at(db) -> Optional[str]:
    value = current(db)
    return value.get("uploaded_at") if value else None


def describe(db) -> dict:
    value = current(db)
    if value is None:
        return {"uploaded_at": None, "uploaded_by": None, "count": 0, "accounts": []}
    by = db.get(UserInDB, value["uploaded_by"]) if value.get("uploaded_by") else None
    return {
        "uploaded_at": value.get("uploaded_at"),
        "uploaded_by": by.name if by else None,
        "count": len(value["accounts"]),
        "accounts": value["accounts"],
    }
