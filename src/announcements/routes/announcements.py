"""Telegram announcements — the authorization boundary for staff broadcasts.

Every route here is a thin proxy to the Support platform's ``/service-api``
(see ``src/services/support_client.py``). What is NOT thin is the role gate:
Support authenticates this call by shared key, not by user token, so **this is
the only place a human is checked** before an announcement can reach every
student group on Telegram.

Why the gate must live here: ``head_teacher`` is one of the roles allowed to
broadcast, and Support's role vocabulary has no such role -- it maps
``head_teacher`` to ``teacher`` on login. This side is the only one that can
tell a head teacher from a teacher, so this side is where the decision belongs.
"""

import json
from typing import List, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile

from src.routes.auth import get_current_user_dependency  # noqa: F401 -- see permissions.py
from src.schemas.models import UserInDB
from src.services import support_client
from src.utils.permissions import require_role

router = APIRouter()

#: Who may compose and send. A broadcast to every student group has a large
#: blast radius, so it stops at the heads: curators and ordinary teachers can
#: not reach it.
ANNOUNCER_ROLES = ["admin", "head_curator", "head_teacher"]

#: Telegram's album cap. Enforced here as well as in Support so the user gets a
#: clear rejection before their upload crosses the network.
MAX_IMAGES = 10


def _announcer():
    """The role gate. NOTE the call: require_role is a factory."""
    return require_role(ANNOUNCER_ROLES)


# --- group registry ---------------------------------------------------------------------


@router.get("/groups")
def list_groups(
    status: Optional[str] = Query(default=None),
    current_user: UserInDB = Depends(_announcer()),
):
    """Groups the bot has been discovered in, with their approval state."""
    return support_client.call(
        "GET",
        "/telegram/groups",
        actor_email=current_user.email,
        actor_name=current_user.name,
        params={"status_filter": status} if status else None,
    )


@router.patch("/groups/{group_id}")
def set_group_status(
    group_id: int,
    body: dict,
    current_user: UserInDB = Depends(_announcer()),
):
    """Approve or reject a discovered group. Until a group is approved it
    cannot receive anything -- discovery alone is not consent."""
    status = (body or {}).get("status")
    if status not in ("pending", "approved", "rejected"):
        raise HTTPException(status_code=422, detail="status must be pending, approved or rejected")
    return support_client.call(
        "PATCH",
        f"/telegram/groups/{group_id}",
        actor_email=current_user.email,
        actor_name=current_user.name,
        json_body={"status": status},
    )


@router.get("/recipients")
def recipients(current_user: UserInDB = Depends(_announcer())):
    """Counts behind the composer's 'everyone': approved groups, and students
    who have bound a chat and not muted announcements."""
    return support_client.call(
        "GET",
        "/telegram/recipients",
        actor_email=current_user.email,
        actor_name=current_user.name,
    )


# --- announcements ----------------------------------------------------------------------


@router.get("")
def list_announcements(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    current_user: UserInDB = Depends(_announcer()),
):
    return support_client.call(
        "GET",
        "/announcements",
        actor_email=current_user.email,
        actor_name=current_user.name,
        params={"limit": limit, "offset": offset},
    )


@router.get("/{announcement_id}")
def get_announcement(
    announcement_id: int,
    current_user: UserInDB = Depends(_announcer()),
):
    """One announcement with its per-recipient delivery rows."""
    return support_client.call(
        "GET",
        f"/announcements/{announcement_id}",
        actor_email=current_user.email,
        actor_name=current_user.name,
    )


@router.post("", status_code=201)
def create_announcement(
    payload: str = Form(...),
    images: List[UploadFile] = File(default=[]),
    current_user: UserInDB = Depends(_announcer()),
):
    """Create and submit. ``payload`` is the JSON half of a multipart request:

        {"body": str, "target_group_ids": [int], "target_all_students": bool,
         "pin": bool, "silent": bool, "scheduled_for": iso8601 | null}

    Support freezes the recipient list at this moment and never recomputes it,
    so the count the sender confirmed is the count that ships.
    """
    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=422, detail="payload is not valid JSON") from exc
    if not isinstance(parsed, dict):
        raise HTTPException(status_code=422, detail="payload must be a JSON object")
    if len(images) > MAX_IMAGES:
        raise HTTPException(
            status_code=422, detail=f"Telegram allows at most {MAX_IMAGES} images per album"
        )

    files = [
        ("images", (image.filename or "photo.jpg", image.file.read(), image.content_type))
        for image in images
    ]
    return support_client.call(
        "POST",
        "/announcements",
        actor_email=current_user.email,
        actor_name=current_user.name,
        data={"payload": json.dumps(parsed)},
        files=files or None,
    )


@router.post("/test-send")
def test_send_preview(
    payload: str = Form(...),
    images: List[UploadFile] = File(default=[]),
    current_user: UserInDB = Depends(_announcer()),
):
    """Preview a composition that has not been created yet.

    Registered BEFORE ``/{announcement_id}/test-send`` because FastAPI matches
    in declaration order. This is the one the composer calls: a preview you can
    only run after committing to send is not a preview.

    ``payload`` is the same JSON object as create, plus ``test_chat_id`` — the
    single chat (the staff test group) that receives it. Nothing is persisted.
    """
    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=422, detail="payload is not valid JSON") from exc
    if not isinstance(parsed, dict):
        raise HTTPException(status_code=422, detail="payload must be a JSON object")
    if not isinstance(parsed.get("test_chat_id"), int):
        raise HTTPException(status_code=422, detail="test_chat_id must be an integer")
    if len(images) > MAX_IMAGES:
        raise HTTPException(
            status_code=422, detail=f"Telegram allows at most {MAX_IMAGES} images per album"
        )

    files = [
        ("images", (image.filename or "photo.jpg", image.file.read(), image.content_type))
        for image in images
    ]
    return support_client.call(
        "POST",
        "/announcements/test-send",
        actor_email=current_user.email,
        actor_name=current_user.name,
        data={"payload": json.dumps(parsed)},
        files=files or None,
    )


@router.post("/{announcement_id}/test-send")
def test_send(
    announcement_id: int,
    body: dict,
    current_user: UserInDB = Depends(_announcer()),
):
    """Render it for real in one chat -- the staff test group -- before it goes
    anywhere else. Nothing is recorded, so a test send is not recallable."""
    chat_id = (body or {}).get("chat_id")
    if not isinstance(chat_id, int):
        raise HTTPException(status_code=422, detail="chat_id must be an integer")
    return support_client.call(
        "POST",
        f"/announcements/{announcement_id}/test-send",
        actor_email=current_user.email,
        actor_name=current_user.name,
        json_body={"chat_id": chat_id},
    )


@router.post("/{announcement_id}/cancel")
def cancel_announcement(
    announcement_id: int,
    current_user: UserInDB = Depends(_announcer()),
):
    """Withdraw a scheduled announcement before delivery starts."""
    return support_client.call(
        "POST",
        f"/announcements/{announcement_id}/cancel",
        actor_email=current_user.email,
        actor_name=current_user.name,
    )


@router.post("/{announcement_id}/recall")
def recall_announcement(
    announcement_id: int,
    current_user: UserInDB = Depends(_announcer()),
):
    """Delete the delivered messages wherever Telegram still permits it
    (roughly 48 hours) and stop anything not yet sent."""
    return support_client.call(
        "POST",
        f"/announcements/{announcement_id}/recall",
        actor_email=current_user.email,
        actor_name=current_user.name,
    )
