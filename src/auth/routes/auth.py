from fastapi import APIRouter, Depends, HTTPException, status, Response, BackgroundTasks
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.orm import Session
from sqlalchemy import func
from src.utils.auth_utils import (
    hash_password,
    verify_password,
    create_access_token,
    verify_token,
    verify_bearer_token,
    create_refresh_token,
    create_password_reset_token,
    verify_password_reset_token,
    password_stamp_matches,
)
from src.services.email_service import (
    send_password_reset_email,
    send_password_changed_email,
    _get_lms_base_url,
)
from src.config import get_db
from src.schemas.models import UserInDB, Token, UserSchema
from src.auth.user_schema import build_user_schema_response
from src.auth.user_resolve import resolve_user_by_payload
import logging
from pydantic import BaseModel
from datetime import datetime
import os

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

router = APIRouter()

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="auth/login")

# Pydantic models for auth
class UserLogin(BaseModel):
    email: str
    password: str

class RefreshTokenRequest(BaseModel):
    refresh_token: str

@router.post("/login", response_model=Token)
async def login(user: UserLogin, response: Response, db: Session = Depends(get_db)):
    """Simple login with email and password"""
    try:
        logger.info(f"Attempting login for email: {user.email}")
        
        # Find user by email (case-insensitive)
        db_user = db.query(UserInDB).filter(func.lower(UserInDB.email) == user.email.lower()).first()
        if not db_user:
            logger.warning(f"User not found: {user.email}")
            raise HTTPException(status_code=400, detail="Invalid credentials")
        
        # Check if user is active
        if not db_user.is_active:
            logger.warning(f"Inactive user attempted login: {user.email}")
            raise HTTPException(status_code=400, detail="Account is inactive")
        
        # Verify password
        if not verify_password(user.password, db_user.hashed_password):
            logger.warning(f"Password verification failed for user: {user.email}")
            raise HTTPException(status_code=400, detail="Invalid credentials")
        
        logger.info(f"Login successful for user: {user.email}")
        
        # Create access and refresh tokens
        access_token = create_access_token(data={
            "sub": db_user.email, 
            "user_id": db_user.id,
            "role": db_user.role
        })
        refresh_token = create_refresh_token(data={"sub": db_user.email})
        
        # Store refresh token in database
        db_user.refresh_token = refresh_token
        db.commit()
        
        # Determine if we're in production (HTTPS) or development (HTTP)
        is_production = os.getenv("ENVIRONMENT", "development") == "production"
        
        # Set cookies with proper attributes for Safari iOS compatibility
        # Access token cookie
        response.set_cookie(
            key="access_token",
            value=access_token,
            httponly=True,  # Prevent JavaScript access
            secure=is_production,  # HTTPS only in production
            samesite="none" if is_production else "lax",  # "none" for cross-origin in production
            max_age=24 * 60 * 60,  # 24 hours
            path="/"
        )
        
        # Refresh token cookie
        response.set_cookie(
            key="refresh_token",
            value=refresh_token,
            httponly=True,
            secure=is_production,
            samesite="none" if is_production else "lax",
            max_age=30 * 24 * 60 * 60,  # 30 days
            path="/"
        )
        
        return {
            "access_token": access_token, 
            "refresh_token": refresh_token,
            "type": "bearer"
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Unexpected error in login: {str(e)}")
        raise HTTPException(status_code=500, detail="Login failed")

@router.post("/refresh", response_model=Token)
async def refresh_token(request: RefreshTokenRequest, response: Response, db: Session = Depends(get_db)):
    """Refresh access token using refresh token"""
    try:
        token = request.refresh_token
        payload = verify_token(token)
        if not payload:
            raise HTTPException(status_code=401, detail="Invalid or expired refresh token")
            
        user_email = payload.get("sub")
        # Find user by email (case-insensitive)
        user = db.query(UserInDB).filter(func.lower(UserInDB.email) == user_email.lower()).first()

        if not user or user.refresh_token != token or not user.is_active:
            raise HTTPException(status_code=401, detail="Invalid refresh token")
        
        # Generate new tokens
        new_access_token = create_access_token(data={
            "sub": user.email, 
            "user_id": user.id,
            "role": user.role
        })
        new_refresh_token = create_refresh_token(data={"sub": user.email})
        
        # Update user's refresh token
        user.refresh_token = new_refresh_token
        db.commit()
        
        # Determine environment
        is_production = os.getenv("ENVIRONMENT", "development") == "production"
        
        # Set new cookies
        response.set_cookie(
            key="access_token",
            value=new_access_token,
            httponly=True,
            secure=is_production,
            samesite="none" if is_production else "lax",
            max_age=24 * 60 * 60,  # 24 hours
            path="/"
        )
        
        response.set_cookie(
            key="refresh_token",
            value=new_refresh_token,
            httponly=True,
            secure=is_production,
            samesite="none" if is_production else "lax",
            max_age=30 * 24 * 60 * 60,  # 30 days
            path="/"
        )
        
        return {
            "access_token": new_access_token,
            "refresh_token": new_refresh_token,
            "type": "bearer"
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Refresh token error: {e}")
        raise HTTPException(status_code=500, detail="Could not refresh token")

@router.get("/me", response_model=UserSchema)
async def get_current_user(token: str = Depends(oauth2_scheme), db: Session = Depends(get_db)):
    """Get current user information"""
    payload = verify_bearer_token(token)
    if payload is None:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    
    # Resolve by stable central-auth id for OIDC (backfilled), else by email (SSO Phase 2).
    user = resolve_user_by_payload(db, payload)
    
    if user is None or not user.is_active:
        raise HTTPException(status_code=404, detail="User not found")

    return build_user_schema_response(user, db)

@router.post("/logout")
async def logout(response: Response, token: str = Depends(oauth2_scheme), db: Session = Depends(get_db)):
    """Logout user by invalidating refresh token"""
    payload = verify_bearer_token(token)
    if payload is None:
        raise HTTPException(status_code=401, detail="Invalid token")

    # Resolve by stable central-auth id for OIDC (backfilled), else by email (SSO Phase 2).
    user = resolve_user_by_payload(db, payload)

    if user:
        user.refresh_token = None
        db.commit()
    
    # Clear cookies
    response.delete_cookie(key="access_token", path="/")
    response.delete_cookie(key="refresh_token", path="/")

    return {"detail": "Logged out successfully"}

# Dependency for getting current user
async def get_current_user_dependency(token: str = Depends(oauth2_scheme), db: Session = Depends(get_db)) -> UserInDB:
    """Dependency to get current authenticated user"""
    payload = verify_bearer_token(token)
    if payload is None:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    
    # Resolve by stable central-auth id for OIDC (backfilled), else by email (SSO Phase 2).
    user = resolve_user_by_payload(db, payload)
    
    if user is None or not user.is_active:
        raise HTTPException(status_code=401, detail="User not found or inactive")
    
    return user

# Admin-only dependency  
async def require_admin(current_user: UserInDB = Depends(get_current_user_dependency)) -> UserInDB:
    """Dependency to require admin role"""
    if current_user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    return current_user

# Teacher or admin dependency
async def require_teacher_or_admin(current_user: UserInDB = Depends(get_current_user_dependency)) -> UserInDB:
    """Dependency to require teacher or admin role"""
    if current_user.role not in ["teacher", "admin"]:
        raise HTTPException(status_code=403, detail="Teacher or admin access required")
    return current_user


# ── Self-service password flows ───────────────────────────────────────────────

class ForgotPasswordRequest(BaseModel):
    email: str

class ResetPasswordRequest(BaseModel):
    token: str
    new_password: str

class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str


@router.post("/forgot-password")
async def forgot_password(
    payload: ForgotPasswordRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    """Send a password-reset link. Always returns a generic response (no user enumeration)."""
    user = db.query(UserInDB).filter(func.lower(UserInDB.email) == payload.email.lower()).first()
    if user and user.is_active:
        token = create_password_reset_token(user.email, user.id, user.hashed_password)
        reset_url = f"{_get_lms_base_url()}/reset-password?token={token}"
        background_tasks.add_task(send_password_reset_email, user.email, user.name or "", reset_url)
    return {"detail": "Если такой аккаунт существует, на почту отправлена ссылка для сброса пароля."}


@router.post("/reset-password")
async def reset_password(
    payload: ResetPasswordRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    """Set a new password using a reset token from the email link."""
    if not payload.new_password or len(payload.new_password) < 6:
        raise HTTPException(status_code=400, detail="Пароль должен быть не короче 6 символов")
    data = verify_password_reset_token(payload.token)
    if not data or not data.get("uid"):
        raise HTTPException(status_code=400, detail="Недействительная или истёкшая ссылка")
    user = db.query(UserInDB).filter(UserInDB.id == data["uid"]).first()
    if not user or not user.is_active or not password_stamp_matches(data.get("pv"), user.hashed_password):
        raise HTTPException(status_code=400, detail="Ссылка недействительна или уже использована")
    user.hashed_password = hash_password(payload.new_password)
    user.refresh_token = None
    user.updated_at = datetime.utcnow()
    db.commit()
    background_tasks.add_task(send_password_changed_email, user.email, user.name or "", None)
    # Keep the Master Education (Zitadel) password in step — best-effort, off the response path.
    from src.services.zitadel_provisioning import mirror_password

    background_tasks.add_task(
        mirror_password, user.central_auth_user_id, payload.new_password, lms_user_id=user.id
    )
    return {"detail": "Пароль успешно изменён"}


@router.post("/change-password")
async def change_password(
    payload: ChangePasswordRequest,
    background_tasks: BackgroundTasks,
    current_user: UserInDB = Depends(get_current_user_dependency),
    db: Session = Depends(get_db),
):
    """Change the password of the authenticated user (requires the current password)."""
    if not verify_password(payload.current_password, current_user.hashed_password):
        raise HTTPException(status_code=400, detail="Текущий пароль неверен")
    if not payload.new_password or len(payload.new_password) < 6:
        raise HTTPException(status_code=400, detail="Пароль должен быть не короче 6 символов")
    current_user.hashed_password = hash_password(payload.new_password)
    current_user.refresh_token = None
    current_user.updated_at = datetime.utcnow()
    db.commit()
    background_tasks.add_task(send_password_changed_email, current_user.email, current_user.name or "", None)
    # Keep the Master Education (Zitadel) password in step — best-effort, off the response path.
    from src.services.zitadel_provisioning import mirror_password

    background_tasks.add_task(
        mirror_password, current_user.central_auth_user_id, payload.new_password,
        lms_user_id=current_user.id,
    )
    return {"detail": "Пароль успешно изменён"}