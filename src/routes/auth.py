from fastapi import APIRouter, Depends, HTTPException, status, Response
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.orm import Session
from sqlalchemy import func
from src.utils.auth_utils import (
    hash_password, 
    verify_password, 
    create_access_token,
    verify_token,
    verify_bearer_token,
    create_refresh_token
)
from src.config import get_db
from src.schemas.models import UserInDB, Token, UserSchema
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
    payload = verify_token(token)
    if payload is None:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    
    # Resolve by stable central-auth id for OIDC (backfilled), else by email (SSO Phase 2).
    user = resolve_user_by_payload(db, payload)
    
    if user is None or not user.is_active:
        raise HTTPException(status_code=404, detail="User not found")
    
    return user

@router.post("/logout")
async def logout(response: Response, token: str = Depends(oauth2_scheme), db: Session = Depends(get_db)):
    """Logout user by invalidating refresh token"""
    payload = verify_token(token)
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