from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Optional
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Optional, List
from datetime import datetime

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
        groups = db.query(Group).filter(Group.id.in_(group_ids), Group.is_active == True).all()
        
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
    db: Session = Depends(get_db),
    user: UserInDB = Depends(get_current_user),
):
    if user.id != user_id:
        raise HTTPException(status_code=403, detail="Not authorized to update this profile")

    if update.name is not None:
        user.name = update.name
    if update.password is not None:
        from src.utils.auth_utils import hash_password

        user.hashed_password = hash_password(update.password)
    db.commit()
    db.refresh(user)
    return user 


@router.post("/complete-onboarding", response_model=UserSchema)
async def complete_onboarding(
    db: Session = Depends(get_db),
    user: UserInDB = Depends(get_current_user),
):
    """Mark user's onboarding as completed."""
    if user.onboarding_completed:
        # Already completed, just return the user
        return user
    
    user.onboarding_completed = True
    from datetime import timezone
    user.onboarding_completed_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(user)
    return user


class PushTokenRequest(BaseModel):
    push_token: str
    device_type: str = "expo"  # expo, ios, android


@router.post("/push-token")
async def register_push_token(
    token_data: PushTokenRequest,
    db: Session = Depends(get_db),
    user: UserInDB = Depends(get_current_user),
):
    """Register or update user's push notification token."""
    user.push_token = token_data.push_token
    user.device_type = token_data.device_type
    db.commit()
    return {"detail": "Push token registered successfully"}


@router.delete("/push-token")
async def remove_push_token(
    db: Session = Depends(get_db),
    user: UserInDB = Depends(get_current_user),
):
    """Remove user's push notification token."""
    user.push_token = None
    user.device_type = None
    db.commit()
    return {"detail": "Push token removed successfully"}
