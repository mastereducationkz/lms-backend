from fastapi import APIRouter, Depends, HTTPException, status, Request, Response, BackgroundTasks
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
from src.utils.password_policy import password_policy_error
from src.services.email_service import (
    send_password_reset_email,
    send_password_changed_email,
    _get_lms_base_url,
)
from src.services import email_log
from src.config import get_db
from src.schemas.models import UserInDB, Token, UserSchema
from src.auth.user_schema import build_user_schema_response
from src.auth.user_resolve import resolve_user_by_payload
import logging
from pydantic import BaseModel
from datetime import datetime, timedelta
import os
import time
import uuid

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
def login(user: UserLogin, response: Response, db: Session = Depends(get_db)):
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
        refresh_token = create_refresh_token(data={"sub": db_user.email, "jti": uuid.uuid4().hex})
        
        # Open a per-device refresh chain. The legacy users.refresh_token column is
        # still written so an emergency rollback to the previous release keeps working.
        db_user.refresh_token = refresh_token
        _start_refresh_session(db, db_user, refresh_token)
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


# Rotation grace: a refresh using the chain's PREVIOUS token within this window is
# treated as a raced duplicate (two tabs, a retry, a flaky network) and answered
# with the chain's CURRENT token instead of a 401 that logs the device out.
REFRESH_ROTATION_GRACE_SECONDS = 60
REFRESH_SESSION_LIFETIME_DAYS = 30


def _start_refresh_session(db, user, token: str) -> None:
    """Open a new per-device refresh chain (called at login)."""
    from src.auth.models import RefreshSession
    now = datetime.utcnow()
    db.add(RefreshSession(
        user_id=user.id, token=token,
        created_at=now, expires_at=now + timedelta(days=REFRESH_SESSION_LIFETIME_DAYS),
    ))
    # Opportunistic cleanup: this user's dead chains.
    db.query(RefreshSession).filter(
        RefreshSession.user_id == user.id,
        (RefreshSession.expires_at < now) | (RefreshSession.revoked_at.isnot(None)),
    ).delete(synchronize_session=False)


def _revoke_refresh_sessions(db, user) -> None:
    """Kill every refresh chain of a user (logout / password change)."""
    from src.auth.models import RefreshSession
    now = datetime.utcnow()
    db.query(RefreshSession).filter(
        RefreshSession.user_id == user.id,
        RefreshSession.revoked_at.is_(None),
    ).update({RefreshSession.revoked_at: now}, synchronize_session=False)
    user.refresh_token = None


@router.post("/refresh", response_model=Token)
def refresh_token(request: RefreshTokenRequest, response: Response, db: Session = Depends(get_db)):
    """Refresh access token using refresh token"""
    try:
        token = request.refresh_token
        payload = verify_token(token)
        if not payload:
            raise HTTPException(status_code=401, detail="Invalid or expired refresh token")
            
        user_email = payload.get("sub")
        # Find user by email (case-insensitive)
        user = db.query(UserInDB).filter(func.lower(UserInDB.email) == user_email.lower()).first()
        if not user or not user.is_active:
            raise HTTPException(status_code=401, detail="Invalid refresh token")

        from src.auth.models import RefreshSession
        now = datetime.utcnow()

        session = db.query(RefreshSession).filter(
            RefreshSession.token == token,
            RefreshSession.user_id == user.id,
        ).first()

        new_access_token = create_access_token(data={
            "sub": user.email,
            "user_id": user.id,
            "role": user.role
        })

        if session and session.revoked_at is None and session.expires_at > now:
            # Normal path: rotate this chain in place.
            new_refresh_token = create_refresh_token(data={"sub": user.email, "jti": uuid.uuid4().hex})
            session.previous_token = token
            session.token = new_refresh_token
            session.rotated_at = now
            session.expires_at = now + timedelta(days=REFRESH_SESSION_LIFETIME_DAYS)
        else:
            raced = db.query(RefreshSession).filter(
                RefreshSession.previous_token == token,
                RefreshSession.user_id == user.id,
                RefreshSession.revoked_at.is_(None),
            ).first()
            if raced and raced.rotated_at and                     (now - raced.rotated_at).total_seconds() <= REFRESH_ROTATION_GRACE_SECONDS:
                # A parallel refresh already rotated this chain moments ago (second
                # tab, retried request). Hand back the chain's CURRENT token instead
                # of logging the device out.
                new_refresh_token = raced.token
            elif user.refresh_token == token:
                # Legacy token from before per-device sessions existed: migrate it
                # into a chain of its own.
                new_refresh_token = create_refresh_token(data={"sub": user.email, "jti": uuid.uuid4().hex})
                db.add(RefreshSession(
                    user_id=user.id, token=new_refresh_token, previous_token=token,
                    rotated_at=now, created_at=now,
                    expires_at=now + timedelta(days=REFRESH_SESSION_LIFETIME_DAYS),
                ))
            else:
                raise HTTPException(status_code=401, detail="Invalid refresh token")

        # Legacy column mirrors the latest rotation for rollback compatibility.
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
def get_current_user(token: str = Depends(oauth2_scheme), db: Session = Depends(get_db)):
    """Get current user information"""
    payload = verify_bearer_token(token)
    if payload is None:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    
    # Resolve by stable central-auth id for OIDC (backfilled), else by email (SSO Phase 2).
    user = resolve_user_by_payload(db, payload)
    
    if user is None or not user.is_active:
        raise HTTPException(status_code=404, detail="User not found")

    return build_user_schema_response(user, db)

class SsoCallbackErrorReport(BaseModel):
    """What the browser saw when /auth/callback could not finish (SSO Phase 2).

    Unauthenticated by necessity — a failed SSO login has no session yet — so every field is
    treated as untrusted input: bounded, truncated, and only ever written to the log.
    """

    reason: str
    detail: str | None = None
    idp_error: str | None = None
    hint_email: str | None = None
    user_agent: str | None = None


# Known classifications from the frontend. Anything else is logged as "other" so an
# unexpected value can never shape the log line.
_SSO_FAILURE_REASONS = {
    "idp_rejected",
    "link_expired",
    "storage_blocked",
    "not_configured",
    "network",
    "lms_unreachable",
    "no_lms_account",
    "token_rejected",
    "unknown",
}


def _clip(value, limit: int) -> str:
    """Bound an untrusted string and strip newlines so it can't forge extra log lines."""
    if not value:
        return ""
    return str(value).replace("\r", " ").replace("\n", " ")[:limit]


# The report endpoint has to be unauthenticated (a failed login has no session), so cap how
# much log it can produce. Real failures arrive in ones and twos; anything above this is
# noise or abuse and is dropped rather than written.
_SSO_REPORT_MAX_PER_MINUTE = 60
_sso_report_window: list = [0.0, 0]  # [window_start_epoch, count_in_window]


def _sso_report_allowed(now: float) -> bool:
    if now - _sso_report_window[0] >= 60:
        _sso_report_window[0] = now
        _sso_report_window[1] = 0
    if _sso_report_window[1] >= _SSO_REPORT_MAX_PER_MINUTE:
        return False
    _sso_report_window[1] += 1
    return True


@router.post("/sso-callback-error", status_code=204)
def report_sso_callback_error(report: SsoCallbackErrorReport, request: Request) -> Response:
    """Record why a "Continue with Master Education" login died at /auth/callback.

    Before this existed, every SSO failure — a spent one-time link, an IdP that refused the
    account, a browser blocking storage, a rejected token — reached the user as one identical
    sentence and reached us as nothing at all. There was no way to tell a self-inflicted
    double-submit from a genuinely broken account, so the same report ("SSO doesn't work for
    this student") was unanswerable. This is the missing half of that loop.
    """
    if not _sso_report_allowed(time.time()):
        return Response(status_code=204)
    reason = report.reason if report.reason in _SSO_FAILURE_REASONS else "other"
    logger.warning(
        "SSO callback failed: reason=%s idp_error=%s hint_email=%s ip=%s ua=%s detail=%s",
        reason,
        _clip(report.idp_error, 200) or "-",
        _clip(report.hint_email, 200) or "-",
        request.client.host if request.client else "-",
        _clip(report.user_agent, 300) or "-",
        _clip(report.detail, 500) or "-",
    )
    return Response(status_code=204)


@router.post("/logout")
def logout(response: Response, token: str = Depends(oauth2_scheme), db: Session = Depends(get_db)):
    """Logout user by invalidating refresh token"""
    payload = verify_bearer_token(token)
    if payload is None:
        raise HTTPException(status_code=401, detail="Invalid token")

    # Resolve by stable central-auth id for OIDC (backfilled), else by email (SSO Phase 2).
    user = resolve_user_by_payload(db, payload)

    if user:
        _revoke_refresh_sessions(db, user)
        db.commit()
    
    # Clear cookies
    response.delete_cookie(key="access_token", path="/")
    response.delete_cookie(key="refresh_token", path="/")

    return {"detail": "Logged out successfully"}

# Dependency for getting current user
def get_current_user_dependency(token: str = Depends(oauth2_scheme), db: Session = Depends(get_db)) -> UserInDB:
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
def require_admin(current_user: UserInDB = Depends(get_current_user_dependency)) -> UserInDB:
    """Dependency to require admin role"""
    if current_user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    return current_user

# Teacher or admin dependency
def require_teacher_or_admin(current_user: UserInDB = Depends(get_current_user_dependency)) -> UserInDB:
    """Dependency to require teacher or admin role"""
    if current_user.role not in ["teacher", "admin"]:
        raise HTTPException(status_code=403, detail="Teacher or admin access required")
    return current_user


# ── Self-service password flows ───────────────────────────────────────────────

#: One sentence for every outcome — found, not found, throttled. Any variation between
#: them turns this endpoint into an account-existence oracle.
_FORGOT_PASSWORD_RESPONSE = (
    "Если такой аккаунт существует, на почту отправлена ссылка для сброса пароля."
)


def _client_ip(request: Request) -> str | None:
    """Best-effort caller IP. The app sits behind a reverse proxy, so prefer the
    left-most X-Forwarded-For entry and fall back to the socket peer."""
    forwarded = (request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
    if forwarded:
        return forwarded[:320]
    client = request.client
    return client.host if client else None


class ForgotPasswordRequest(BaseModel):
    email: str

class ResetPasswordRequest(BaseModel):
    token: str
    new_password: str

class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str


@router.post("/forgot-password")
def forgot_password(
    payload: ForgotPasswordRequest,
    background_tasks: BackgroundTasks,
    request: Request,
    db: Session = Depends(get_db),
):
    """Send a password-reset link. Always returns a generic response (no user enumeration).

    Throttled at 3 requests per address and 10 per client IP per hour. Unlimited, this
    endpoint was a mail bomb anyone could point at a known address, and it sends a real
    email for every request. The counters live in the database because production runs
    four uvicorn workers — a per-process counter would hand out four times the budget.

    A throttled request still returns the same sentence as an accepted one. The generic
    response is what stops the endpoint confirming which addresses exist, and a distinct
    429 would give that away just as effectively as a distinct 404.
    """
    if not email_log.password_reset_allowed(payload.email, _client_ip(request)):
        logger.warning("Password reset throttled for %s", payload.email)
        return {"detail": _FORGOT_PASSWORD_RESPONSE}
    user = db.query(UserInDB).filter(func.lower(UserInDB.email) == payload.email.lower()).first()
    if user and user.is_active:
        token = create_password_reset_token(user.email, user.id, user.hashed_password)
        reset_url = f"{_get_lms_base_url()}/reset-password?token={token}"
        background_tasks.add_task(
            send_password_reset_email, user.email, user.name or "", reset_url, user.id
        )
    return {"detail": _FORGOT_PASSWORD_RESPONSE}


@router.post("/reset-password")
def reset_password(
    payload: ResetPasswordRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    """Set a new password using a reset token from the email link."""
    _pw_err = password_policy_error(payload.new_password)
    if _pw_err:
        raise HTTPException(status_code=400, detail=_pw_err)
    data = verify_password_reset_token(payload.token)
    if not data or not data.get("uid"):
        raise HTTPException(status_code=400, detail="Недействительная или истёкшая ссылка")
    user = db.query(UserInDB).filter(UserInDB.id == data["uid"]).first()
    if not user or not user.is_active or not password_stamp_matches(data.get("pv"), user.hashed_password):
        raise HTTPException(status_code=400, detail="Ссылка недействительна или уже использована")
    user.hashed_password = hash_password(payload.new_password)
    _revoke_refresh_sessions(db, user)
    user.updated_at = datetime.utcnow()
    db.commit()
    background_tasks.add_task(send_password_changed_email, user.email, user.name or "", None, user.id)
    # Keep the Master Education (Zitadel) password in step — best-effort, off the response path.
    from src.services.zitadel_provisioning import mirror_password

    background_tasks.add_task(
        mirror_password, user.central_auth_user_id, payload.new_password, lms_user_id=user.id
    )
    return {"detail": "Пароль успешно изменён"}


@router.post("/change-password")
def change_password(
    payload: ChangePasswordRequest,
    background_tasks: BackgroundTasks,
    current_user: UserInDB = Depends(get_current_user_dependency),
    db: Session = Depends(get_db),
):
    """Change the password of the authenticated user (requires the current password)."""
    if not verify_password(payload.current_password, current_user.hashed_password):
        raise HTTPException(status_code=400, detail="Текущий пароль неверен")
    _pw_err = password_policy_error(payload.new_password)
    if _pw_err:
        raise HTTPException(status_code=400, detail=_pw_err)
    current_user.hashed_password = hash_password(payload.new_password)
    _revoke_refresh_sessions(db, current_user)
    current_user.updated_at = datetime.utcnow()
    db.commit()
    background_tasks.add_task(
        send_password_changed_email, current_user.email, current_user.name or "", None,
        current_user.id,
    )
    # Keep the Master Education (Zitadel) password in step — best-effort, off the response path.
    from src.services.zitadel_provisioning import mirror_password

    background_tasks.add_task(
        mirror_password, current_user.central_auth_user_id, payload.new_password,
        lms_user_id=current_user.id,
    )
    return {"detail": "Пароль успешно изменён"}


class DeleteAccountRequest(BaseModel):
    current_password: str


@router.delete("/account", status_code=204)
def delete_account(
    payload: DeleteAccountRequest,
    current_user: UserInDB = Depends(get_current_user_dependency),
    db: Session = Depends(get_db),
):
    """Permanently delete the authenticated user's own account (student/parent only).

    Apple App Store Guideline 5.1.1(v): apps that support account creation must let
    users initiate account deletion in-app. Staff accounts are excluded because their
    non-cascade foreign keys (courses.teacher_id, events.created_by, submissions.graded_by)
    would orphan institutional data — staff must be removed by an admin.
    """
    if current_user.role not in ("student", "parent"):
        raise HTTPException(
            status_code=403,
            detail="Staff accounts cannot be self-deleted. Please contact your administrator.",
        )
    if not verify_password(payload.current_password, current_user.hashed_password):
        raise HTTPException(status_code=400, detail="Текущий пароль неверен")

    # Best-effort external deprovision (Zitadel / Master Education mirror); never blocks deletion.
    if current_user.central_auth_user_id:
        try:
            from src.services.zitadel_provisioning import set_user_active
            set_user_active(current_user.central_auth_user_id, False)
        except Exception:
            logger.warning("Zitadel deprovision failed for user %s", current_user.id, exc_info=True)

    # ORM relationships on UserInDB use cascade="all, delete-orphan", so db.delete cascades
    # messages, submissions, push tokens, progress, parent links, notifications, points, etc.
    # A few child tables have a non-nullable, non-ondelete-CASCADE FK to users.id that is NOT
    # modeled as a UserInDB relationship, so db.delete would orphan them and raise IntegrityError.
    # Delete those rows explicitly first (staff-only FKs like events.created_by/teacher_id are
    # unreachable here since the role guard above already blocks non-student/parent accounts).
    from src.events.models import EventParticipant
    from src.progress.models import ProgressSnapshot, QuizAttempt

    db.query(EventParticipant).filter(EventParticipant.user_id == current_user.id).delete(synchronize_session=False)
    db.query(ProgressSnapshot).filter(ProgressSnapshot.user_id == current_user.id).delete(synchronize_session=False)
    db.query(QuizAttempt).filter(QuizAttempt.user_id == current_user.id).delete(synchronize_session=False)

    db.delete(current_user)
    db.commit()
    return Response(status_code=204)