from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Optional, List
from datetime import datetime

from src.auth.user_schema import build_user_schema_response
from src.schemas.models import UserInDB, UserSchema, Group, GroupStudent, GroupSchema
from src.config import get_db
from src.utils.auth_utils import verify_token
from fastapi.security import OAuth2PasswordBearer

router = APIRouter()

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="login")


async def get_current_user(token: str = Depends(oauth2_scheme), db: Session = Depends(get_db)) -> UserInDB:
    payload = verify_token(token)
    if payload is None or "sub" not in payload:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    user_email = payload["sub"]
    user = db.query(UserInDB).filter(UserInDB.email == user_email).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return user


class UserUpdate(BaseModel):
    name: Optional[str] = None
    password: Optional[str] = None


@router.get("/users/{user_id}", response_model=UserSchema)
async def get_user_by_id(user_id: int, db: Session = Depends(get_db), token: str = Depends(oauth2_scheme)):
    payload = verify_token(token)
    if not payload:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    user_email = payload["sub"]
    user = db.query(UserInDB).filter(UserInDB.email == user_email).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if user.id != user_id:
        raise HTTPException(status_code=403, detail="Not authorized to view this profile")
    return user


@router.get("/groups/me", response_model=List[GroupSchema])
async def get_my_groups(
    db: Session = Depends(get_db),
    user: UserInDB = Depends(get_current_user),
):
    """Get groups the current user belongs to."""
    if user.role == 'student':
        # Get groups via enrollment/GroupStudent
        group_ids = db.query(GroupStudent.group_id).filter(GroupStudent.student_id == user.id).subquery()
        groups = db.query(Group).filter(
            Group.id.in_(group_ids),
            Group.is_active == True,
            Group.is_over == False,
        ).all()
        
        # Enrich with details
        result = []
        for group in groups:
            # Basic schema without recursive students list to keep it light
            result.append(GroupSchema(
                id=group.id,
                name=group.name,
                description=group.description,
                teacher_id=group.teacher_id,
                teacher_name="", # Not needed for this view
                curator_id=group.curator_id,
                is_active=group.is_active,
                is_special=group.is_special,
                is_over=group.is_over,
                group_type=getattr(group, "group_type", None) or "group",
                program_type=getattr(group, "program_type", None) or "general_english",
                student_count=0, # Not needed
                students=[],
                created_at=group.created_at
            ))
        return result
        
    elif user.role in ['teacher', 'curator']:
        # Teachers see groups they teach
        query = db.query(Group).filter(Group.is_active == True)
        if user.role == 'teacher':
            query = query.filter(Group.teacher_id == user.id)
        elif user.role == 'curator':
            query = query.filter(Group.curator_id == user.id)
            
        groups = query.all()
        # simplified return
        return [
            GroupSchema(
                id=g.id, 
                name=g.name, 
                description=g.description, 
                teacher_id=g.teacher_id,
                curator_id=g.curator_id,
                is_active=g.is_active,
                is_special=g.is_special,
                is_over=g.is_over,
                group_type=getattr(g, "group_type", None) or "group",
                program_type=getattr(g, "program_type", None) or "general_english",
                students=[],
                student_count=0,
                created_at=g.created_at
            ) for g in groups
        ]
        
    return []


@router.put("/{user_id}", response_model=UserSchema)
async def update_profile(
    user_id: int,
    update: UserUpdate,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    user: UserInDB = Depends(get_current_user),
):
    if user.id != user_id:
        raise HTTPException(status_code=403, detail="Not authorized to update this profile")

    password_changed = False
    if update.name is not None:
        user.name = update.name
    if update.password is not None:
        from src.utils.auth_utils import hash_password

        user.hashed_password = hash_password(update.password)
        user.refresh_token = None  # Invalidate other sessions
        password_changed = True
    db.commit()
    db.refresh(user)

    if password_changed:
        from src.services.email_service import send_password_changed_email
        background_tasks.add_task(send_password_changed_email, user.email, user.name or "", None)
        # Keep the Master Education (Zitadel) password in step — best-effort, off the response path.
        from src.services.zitadel_provisioning import mirror_password

        background_tasks.add_task(
            mirror_password, user.central_auth_user_id, update.password, lms_user_id=user.id
        )

    return build_user_schema_response(user, db)


@router.post("/complete-onboarding", response_model=UserSchema)
async def complete_onboarding(
    db: Session = Depends(get_db),
    user: UserInDB = Depends(get_current_user),
):
    """Mark user's onboarding as completed."""
    if user.onboarding_completed:
        return build_user_schema_response(user, db)

    user.onboarding_completed = True
    from datetime import timezone
    user.onboarding_completed_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(user)
    return build_user_schema_response(user, db)


class PushTokenRequest(BaseModel):
    push_token: str
    device_type: str = "expo"  # expo, ios, android


def _upsert_push_token(db: Session, user_id: int, token: str, platform: str, device_name: str | None = None):
    """Insert or reactivate a (globally unique) push token for a user.

    Tokens are globally unique; re-registering a device's token reassigns it to
    the current user (e.g. after a logout/login on the same device).
    """
    from src.auth.models import UserPushToken
    existing = db.query(UserPushToken).filter(UserPushToken.token == token).first()
    if existing:
        existing.user_id = user_id
        existing.platform = platform
        if device_name:
            existing.device_name = device_name
        existing.is_active = True
    else:
        db.add(UserPushToken(
            user_id=user_id, token=token, platform=platform,
            device_name=device_name, is_active=True,
        ))


@router.post("/push-token")
async def register_push_token(
    token_data: PushTokenRequest,
    db: Session = Depends(get_db),
    user: UserInDB = Depends(get_current_user),
):
    """Register/update the user's push token (legacy single-token endpoint).

    Writes the legacy column AND the multi-device user_push_tokens table so both
    the old web app and the new mobile app stay consistent.
    """
    user.push_token = token_data.push_token
    user.device_type = token_data.device_type
    _upsert_push_token(db, user.id, token_data.push_token, token_data.device_type)
    db.commit()
    return {"detail": "Push token registered successfully"}


@router.delete("/push-token")
async def remove_push_token(
    db: Session = Depends(get_db),
    user: UserInDB = Depends(get_current_user),
):
    """Remove the user's legacy push token and deactivate it in the multi-device table."""
    from src.auth.models import UserPushToken
    if user.push_token:
        db.query(UserPushToken).filter(
            UserPushToken.token == user.push_token
        ).update({"is_active": False})
    user.push_token = None
    user.device_type = None
    db.commit()
    return {"detail": "Push token removed successfully"}


class PushTokenRegisterRequest(BaseModel):
    token: str
    platform: str = "expo"   # ios | android | web | expo
    device_name: str | None = None


@router.post("/push-tokens")
async def add_push_token(
    body: PushTokenRegisterRequest,
    db: Session = Depends(get_db),
    user: UserInDB = Depends(get_current_user),
):
    """Register a device push token (multi-device aware)."""
    _upsert_push_token(db, user.id, body.token, body.platform, body.device_name)
    # Mirror into the legacy column so older code paths still have a token.
    if not user.push_token:
        user.push_token = body.token
        user.device_type = body.platform
    db.commit()
    return {"detail": "Push token registered"}


@router.delete("/push-tokens/{token}")
async def delete_push_token(
    token: str,
    db: Session = Depends(get_db),
    user: UserInDB = Depends(get_current_user),
):
    """Remove a specific device token (e.g. on logout from one device)."""
    from src.auth.models import UserPushToken
    db.query(UserPushToken).filter(
        UserPushToken.token == token,
        UserPushToken.user_id == user.id,
    ).delete()
    if user.push_token == token:
        user.push_token = None
        user.device_type = None
    db.commit()
    return {"detail": "Push token removed"}
