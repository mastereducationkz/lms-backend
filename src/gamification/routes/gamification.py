"""Gamification routes for points, leaderboard, and teacher bonuses."""
from fastapi import APIRouter, Depends, HTTPException, status, Query
from sqlalchemy.orm import Session
from sqlalchemy import func, extract
from typing import Dict, List, Optional
from datetime import datetime, date, timedelta, timezone
from pydantic import BaseModel

from src.schemas.models import (
    UserInDB,
    PointHistory,
    PointHistorySchema,
    Group,
    GroupStudent,
)
from src.routes.auth import get_current_user_dependency
from src.config import get_db

router = APIRouter()


# =============================================================================
# SCHEMAS
# =============================================================================

class GamificationStatsResponse(BaseModel):
    activity_points: int
    daily_streak: int
    monthly_points: int
    rank_this_month: Optional[int] = None


class TeacherBonusRequest(BaseModel):
    student_id: int
    amount: int  # Max 10 points per week
    reason: Optional[str] = None
    group_id: Optional[int] = None


class LeaderboardEntry(BaseModel):
    user_id: int
    user_name: str
    avatar_url: Optional[str] = None
    points: int
    rank: int


class LeaderboardResponse(BaseModel):
    period: str  # 'monthly', 'weekly', 'all_time'
    start_date: Optional[date] = None
    end_date: Optional[date] = None
    total_participants: int
    entries: List[LeaderboardEntry]
    # Populated for students only: no classmates in `entries`, no PII in the payload.
    self_only: bool = False
    my_rank: Optional[int] = None
    my_points: Optional[int] = None
    points_to_next_rank: Optional[int] = None


# =============================================================================
# HELPER FUNCTIONS
# =============================================================================

from src.routes.progress import calculate_streak_multiplier

def award_points(db: Session, user_id: int, amount: int, reason: str, description: str = None):
    """Award points to a user and record in history."""
    # Update user's total points
    user = db.query(UserInDB).filter(UserInDB.id == user_id).first()
    if not user:
        return None
    
    # Calculate multiplier based on streak
    multiplier = calculate_streak_multiplier(user.daily_streak or 0)
    final_amount = int(amount * multiplier)
    
    user.activity_points = (user.activity_points or 0) + final_amount
    
    # Record in history
    # Add multiplier info to description if it's applied (>1.0)
    final_description = description
    if multiplier > 1.0:
        multiplier_info = f" (Streak Bonus: {multiplier}x)"
        if final_description:
            final_description += multiplier_info
        else:
            final_description = multiplier_info.strip()

    history_entry = PointHistory(
        user_id=user_id,
        amount=final_amount,
        reason=reason,
        description=final_description
    )
    db.add(history_entry)
    db.commit()
    
    return history_entry


def get_monthly_points(db: Session, user_id: int, year: int = None, month: int = None) -> int:
    """Get total points for a user in a specific month."""
    if year is None:
        year = datetime.now(timezone.utc).year
    if month is None:
        month = datetime.now(timezone.utc).month
    
    result = db.query(func.coalesce(func.sum(PointHistory.amount), 0)).filter(
        PointHistory.user_id == user_id,
        extract('year', PointHistory.created_at) == year,
        extract('month', PointHistory.created_at) == month
    ).scalar()
    
    return int(result) if result else 0


def get_teacher_weekly_bonus_given(db: Session, teacher_id: int, group_id: int = None) -> int:
    """Get how many bonus points a teacher has given this week, optionally filtered by group."""
    # Get start of current week (Monday)
    today = date.today()
    start_of_week = today - timedelta(days=today.weekday())
    
    query = db.query(func.coalesce(func.sum(PointHistory.amount), 0)).filter(
        PointHistory.reason == 'teacher_bonus',
        PointHistory.description.like(f'%teacher:{teacher_id}%'),
        PointHistory.created_at >= datetime.combine(start_of_week, datetime.min.time())
    )
    
    if group_id:
        # Find all students in this group
        student_ids = db.query(GroupStudent.student_id).filter(
            GroupStudent.group_id == group_id
        ).subquery()
        
        # Filter point history for these students
        query = query.filter(PointHistory.user_id.in_(student_ids))
    
    result = query.scalar()
    
    return int(result) if result else 0


def _leaderboard_scope_student_ids(db: Session, group_id: Optional[int]) -> List[int]:
    if group_id:
        rows = db.query(GroupStudent.student_id).filter(GroupStudent.group_id == group_id).all()
        return [r[0] for r in rows]
    rows = db.query(UserInDB.id).filter(UserInDB.role == "student", UserInDB.is_active == True).all()
    return [r[0] for r in rows]


def _student_leaderboard_self_only(
    db: Session,
    current_user: UserInDB,
    period: str,
    group_id: Optional[int],
    now: datetime,
) -> LeaderboardResponse:
    """
    Rank and points for the current student only (no classmates in the response body).
    """
    if group_id:
        total_participants = db.query(GroupStudent).filter(GroupStudent.group_id == group_id).count()
    else:
        total_participants = db.query(UserInDB).filter(UserInDB.role == "student", UserInDB.is_active == True).count()

    user_ids = _leaderboard_scope_student_ids(db, group_id)
    if not user_ids or current_user.id not in user_ids:
        return LeaderboardResponse(
            period=period,
            start_date=None,
            end_date=None,
            total_participants=total_participants,
            entries=[],
            self_only=True,
            my_rank=None,
            my_points=0,
            points_to_next_rank=None,
        )

    scores: Dict[int, int] = {uid: 0 for uid in user_ids}
    start_date: Optional[date] = None
    end_date: Optional[date] = None

    if period == "monthly":
        start_date = date(now.year, now.month, 1)
        if now.month == 12:
            end_date = date(now.year + 1, 1, 1) - timedelta(days=1)
        else:
            end_date = date(now.year, now.month + 1, 1) - timedelta(days=1)
        rows = db.query(
            PointHistory.user_id,
            func.coalesce(func.sum(PointHistory.amount), 0),
        ).filter(
            PointHistory.user_id.in_(user_ids),
            extract("year", PointHistory.created_at) == now.year,
            extract("month", PointHistory.created_at) == now.month,
        ).group_by(PointHistory.user_id).all()
        for uid, total in rows:
            scores[int(uid)] = int(total or 0)

    elif period == "weekly":
        today = date.today()
        start_date = today - timedelta(days=today.weekday())
        end_date = start_date + timedelta(days=6)
        week_start = datetime.combine(start_date, datetime.min.time())
        week_end = datetime.combine(end_date, datetime.max.time())
        rows = db.query(
            PointHistory.user_id,
            func.coalesce(func.sum(PointHistory.amount), 0),
        ).filter(
            PointHistory.user_id.in_(user_ids),
            PointHistory.created_at >= week_start,
            PointHistory.created_at <= week_end,
        ).group_by(PointHistory.user_id).all()
        for uid, total in rows:
            scores[int(uid)] = int(total or 0)

    else:
        rows = db.query(UserInDB.id, UserInDB.activity_points).filter(UserInDB.id.in_(user_ids)).all()
        for uid, pts in rows:
            scores[int(uid)] = int(pts or 0)

    sorted_ids = sorted(user_ids, key=lambda u: (-scores[u], u))
    my_points = scores.get(current_user.id, 0)
    my_rank: Optional[int] = None
    points_to_next: Optional[int] = None

    try:
        idx = sorted_ids.index(current_user.id)
    except ValueError:
        idx = -1

    if period in ("monthly", "weekly"):
        if my_points <= 0 or idx < 0:
            my_rank = None
            points_to_next = None
        else:
            my_rank = idx + 1
            points_to_next = scores[sorted_ids[idx - 1]] - my_points if idx > 0 else 0
    else:
        if idx < 0:
            my_rank = None
            points_to_next = None
        else:
            my_rank = idx + 1
            points_to_next = scores[sorted_ids[idx - 1]] - my_points if idx > 0 else 0

    return LeaderboardResponse(
        period=period,
        start_date=start_date,
        end_date=end_date,
        total_participants=total_participants,
        entries=[],
        self_only=True,
        my_rank=my_rank,
        my_points=my_points,
        points_to_next_rank=points_to_next,
    )


# =============================================================================
# ENDPOINTS
# =============================================================================

@router.get("/status", response_model=GamificationStatsResponse)
async def get_gamification_status(
    current_user: UserInDB = Depends(get_current_user_dependency),
    db: Session = Depends(get_db)
):
    """Get current user's gamification stats."""
    monthly_points = get_monthly_points(db, current_user.id)
    
    # Calculate rank this month
    now = datetime.now(timezone.utc)
    subquery = db.query(
        PointHistory.user_id,
        func.sum(PointHistory.amount).label('total')
    ).filter(
        extract('year', PointHistory.created_at) == now.year,
        extract('month', PointHistory.created_at) == now.month
    ).group_by(PointHistory.user_id).subquery()
    
    # Count users with more points
    users_ahead = db.query(func.count()).select_from(subquery).filter(
        subquery.c.total > monthly_points
    ).scalar()
    
    rank = (users_ahead or 0) + 1
    
    return GamificationStatsResponse(
        activity_points=current_user.activity_points or 0,
        daily_streak=current_user.daily_streak or 0,
        monthly_points=monthly_points,
        rank_this_month=rank
    )


@router.get("/bonus-allowance", response_model=dict)
async def get_bonus_allowance(
    group_id: Optional[int] = Query(None, description="Optional group ID to check limit for"),
    current_user: UserInDB = Depends(get_current_user_dependency),
    db: Session = Depends(get_db)
):
    """Get teacher's remaining weekly bonus allowance."""
    if current_user.role not in ['teacher', 'admin', 'curator', 'head_curator']:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only teachers, curators and admins can inspect bonus allowance"
        )
        
    weekly_given = get_teacher_weekly_bonus_given(db, current_user.id, group_id)
    limit = 50
    remaining = max(0, limit - weekly_given)
    
    return {
        "limit": limit,
        "given": weekly_given,
        "remaining": remaining
    }


@router.post("/bonus")
async def give_teacher_bonus(
    request: TeacherBonusRequest,
    current_user: UserInDB = Depends(get_current_user_dependency),
    db: Session = Depends(get_db)
):
    """Teacher gives bonus points to a student (max 10 per week)."""
    # Check if current user is a teacher
    if current_user.role not in ['teacher', 'admin', 'curator', 'head_curator']:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only teachers, curators and admins can give bonus points"
        )
    
    # Validate amount
    if request.amount < 1 or request.amount > 50:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Bonus amount must be between 1 and 50"
        )
    
    # Verify student exists
    student = db.query(UserInDB).filter(UserInDB.id == request.student_id, UserInDB.role == 'student').first()
    if not student:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Student not found"
        )

    # Determine group ID for limit checking
    check_group_id = request.group_id
    
    # If no group ID provided, try to find a common group between teacher and student
    if not check_group_id:
        # Find groups where this teacher is teaching this student
        common_group = db.query(GroupStudent).join(Group).filter(
            GroupStudent.student_id == request.student_id,
            Group.teacher_id == current_user.id
        ).first()
        
        if common_group:
            check_group_id = common_group.group_id
    
    # Check weekly limit for this teacher in this group
    weekly_given = get_teacher_weekly_bonus_given(db, current_user.id, check_group_id)
    if weekly_given + request.amount > 50:
        remaining = max(0, 50 - weekly_given)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Weekly bonus limit reached for this group. You can give {remaining} more points."
        )
    
    # Award the bonus
    description = f"teacher:{current_user.id}|{request.reason or 'Good activity'}"
    entry = award_points(db, request.student_id, request.amount, 'teacher_bonus', description)
    
    return {
        "success": True,
        "message": f"Awarded {request.amount} points to {student.name}",
        "new_total": student.activity_points
    }


@router.get("/leaderboard", response_model=LeaderboardResponse)
async def get_leaderboard(
    period: str = Query("monthly", description="'monthly' or 'all_time'"),
    group_id: Optional[int] = Query(None, description="Filter by group ID"),
    limit: int = Query(50, le=100),
    current_user: UserInDB = Depends(get_current_user_dependency),
    db: Session = Depends(get_db)
):
    """Get leaderboard rankings."""
    now = datetime.now(timezone.utc)

    if current_user.role == "student":
        if group_id:
            member = db.query(GroupStudent).filter(
                GroupStudent.group_id == group_id,
                GroupStudent.student_id == current_user.id,
            ).first()
            if not member:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Not a member of this group",
                )
        return _student_leaderboard_self_only(db, current_user, period, group_id, now)

    if period == "monthly":
        # Monthly leaderboard from PointHistory
        start_date = date(now.year, now.month, 1)
        if now.month == 12:
            end_date = date(now.year + 1, 1, 1) - timedelta(days=1)
        else:
            end_date = date(now.year, now.month + 1, 1) - timedelta(days=1)
        
        # Base query for monthly points
        query = db.query(
            PointHistory.user_id,
            func.sum(PointHistory.amount).label('total_points')
        ).filter(
            extract('year', PointHistory.created_at) == now.year,
            extract('month', PointHistory.created_at) == now.month
        )
        
        # Filter by group if specified
        if group_id:
            student_ids = db.query(GroupStudent.student_id).filter(
                GroupStudent.group_id == group_id
            ).subquery()
            query = query.filter(PointHistory.user_id.in_(student_ids))
        
        results = query.group_by(PointHistory.user_id).order_by(
            func.sum(PointHistory.amount).desc()
        ).limit(limit).all()
        
        
    elif period == "weekly":
        # Weekly leaderboard (starts Monday)
        today = date.today()
        start_date = today - timedelta(days=today.weekday())
        end_date = start_date + timedelta(days=6)
        
        # Base query for weekly points
        query = db.query(
            PointHistory.user_id,
            func.sum(PointHistory.amount).label('total_points')
        ).filter(
            PointHistory.created_at >= datetime.combine(start_date, datetime.min.time()),
            PointHistory.created_at <= datetime.combine(end_date, datetime.max.time())
        )
        
        # Filter by group if specified
        if group_id:
            student_ids = db.query(GroupStudent.student_id).filter(
                GroupStudent.group_id == group_id
            ).subquery()
            query = query.filter(PointHistory.user_id.in_(student_ids))
        
        results = query.group_by(PointHistory.user_id).order_by(
            func.sum(PointHistory.amount).desc()
        ).limit(limit).all()
        
    else:
        # All-time leaderboard from User.activity_points
        start_date = None
        end_date = None
        
        query = db.query(UserInDB).filter(UserInDB.role == 'student')
        
        if group_id:
            student_ids = db.query(GroupStudent.student_id).filter(
                GroupStudent.group_id == group_id
            ).subquery()
            query = query.filter(UserInDB.id.in_(student_ids))
        
        users = query.order_by(UserInDB.activity_points.desc()).limit(limit).all()
        results = [(u.id, u.activity_points or 0) for u in users]
    
    # Build response with user details
    entries = []
    user_ids = [r[0] for r in results]
    users_map = {u.id: u for u in db.query(UserInDB).filter(UserInDB.id.in_(user_ids)).all()}
    
    for rank, (user_id, points) in enumerate(results, start=1):
        user = users_map.get(user_id)
        if user:
            entries.append(LeaderboardEntry(
                user_id=user_id,
                user_name=user.name,
                avatar_url=user.avatar_url,
                points=int(points),
                rank=rank
            ))
            
    # Calculate total participants for the context
    # If filtered by group, count students in group
    # If all students, count all students
    total_participants = 0
    if group_id:
        total_participants = db.query(GroupStudent).filter(GroupStudent.group_id == group_id).count()
    else:
        total_participants = db.query(UserInDB).filter(UserInDB.role == 'student', UserInDB.is_active == True).count()
    
    return LeaderboardResponse(
        period=period,
        start_date=start_date,
        end_date=end_date,
        total_participants=total_participants,
        entries=entries
    )


@router.get("/history", response_model=List[PointHistorySchema])
async def get_point_history(
    limit: int = Query(50, le=100),
    current_user: UserInDB = Depends(get_current_user_dependency),
    db: Session = Depends(get_db)
):
    """Get current user's point history."""
    history = db.query(PointHistory).filter(
        PointHistory.user_id == current_user.id
    ).order_by(PointHistory.created_at.desc()).limit(limit).all()
    
    return history
