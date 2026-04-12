from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Optional, List, Any
from datetime import datetime, date, timezone
import httpx
import os
import logging

from src.config import get_db
from src.schemas.models import UserInDB, DailyQuestionCompletion
from src.routes.auth import get_current_user_dependency
from src.gamification.routes.gamification import award_points

router = APIRouter()
logger = logging.getLogger(__name__)

# Gamification: once per successful daily completion (same day guarded by DB row above)
DAILY_QUESTIONS_POINTS_MIN = 10
DAILY_QUESTIONS_POINTS_MAX = 26

# External API config
MASTER_ED_API_URL = "https://api.mastereducation.kz/api/lms/students/question-recommendations"
MASTER_ED_API_KEY = os.getenv("MASTEREDU_API_KEY")
MASTER_ED_BASE_URL = "https://api.mastereducation.kz/api"

# =============================================================================
# SCHEMAS
# =============================================================================

class DailyQuestionItem(BaseModel):
    questionId: int
    text: str
    imageUrl: Optional[str] = None
    primaryTag: str
    secondaryTags: Optional[Any] = None
    difficulty: str


class RecommendationSection(BaseModel):
    questions: List[DailyQuestionItem]
    reasoning: str


class DailyQuestionsResponse(BaseModel):
    email: str
    studentName: str
    mathTestId: Optional[int] = None
    verbalTestId: Optional[int] = None
    mathRecommendations: Optional[RecommendationSection] = None
    verbalRecommendations: Optional[RecommendationSection] = None


class DailyQuestionsStatusResponse(BaseModel):
    completed_today: bool
    completed_at: Optional[str] = None


class CompleteDailyQuestionsRequest(BaseModel):
    questions_data: Optional[dict] = None  # Optional: store which questions were answered
    score: Optional[int] = None  # Number of correct answers
    total_questions: Optional[int] = None  # Total number of questions


# =============================================================================
# ENDPOINTS
# =============================================================================

@router.get("/status")
async def get_daily_questions_status(
    current_user: UserInDB = Depends(get_current_user_dependency),
    db: Session = Depends(get_db)
):
    """Check if the student has completed daily questions today."""
    if current_user.role != "student":
        return {"completed_today": True, "message": "Daily questions are only for students"}

    today = date.today()
    completion = db.query(DailyQuestionCompletion).filter(
        DailyQuestionCompletion.user_id == current_user.id,
        DailyQuestionCompletion.completed_date == today
    ).first()

    return {
        "completed_today": completion is not None,
        "completed_at": completion.created_at.isoformat() if completion else None,
        "score": completion.score if completion else None,
        "total_questions": completion.total_questions if completion else None
    }


@router.get("/recommendations")
async def get_daily_question_recommendations(
    current_user: UserInDB = Depends(get_current_user_dependency),
    db: Session = Depends(get_db)
):
    """Fetch personalized question recommendations for the student from external API."""
    if current_user.role != "student":
        raise HTTPException(status_code=403, detail="Only students can access daily questions")

    logger.info(f"Fetching recommendations for {current_user.email}")
    
    # Retry logic with increased timeout
    max_retries = 2
    timeout = 30.0  # Increased from 15 to 30 seconds
    
    for attempt in range(max_retries):
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                logger.info(f"Making request to {MASTER_ED_API_URL} (attempt {attempt + 1}/{max_retries})")
                response = await client.post(
                    MASTER_ED_API_URL,
                    json={"email": current_user.email},
                    headers={
                        "Content-Type": "application/json",
                        "X-API-Key": MASTER_ED_API_KEY
                    }
                )
                logger.info(f"Response status: {response.status_code}")

            if response.status_code != 200:
                logger.error(f"External API error: {response.status_code} - {response.text}")
                
                # Handle specific error cases
                if response.status_code == 404:
                    error_detail = response.json().get("error", "Student not found")
                    raise HTTPException(
                        status_code=404,
                        detail=f"No recommendations available: {error_detail}. Please complete your assessment tests first."
                    )
                
                raise HTTPException(
                    status_code=502,
                    detail=f"Failed to fetch recommendations from external service: {response.text}"
                )

            data = response.json()

            # Debug logging - show raw response structure
            logger.info(f"DEBUG - Raw API response for {current_user.email}:")
            logger.info(f"Response keys: {list(data.keys())}")
            logger.info(f"Email: {data.get('email')}")
            logger.info(f"Student name: {data.get('studentName')}")
            logger.info(f"Math test ID: {data.get('mathTestId')}")
            logger.info(f"Verbal test ID: {data.get('verbalTestId')}")
            
            # Log math recommendations
            if data.get('mathRecommendations'):
                math_questions = data['mathRecommendations'].get('questions', [])
                logger.info(f"Math recommendations: {len(math_questions)} questions")
                logger.info(f"Math reasoning: {data['mathRecommendations'].get('reasoning')}")
                for idx, q in enumerate(math_questions[:2]):  # Show first 2 questions
                    logger.info(f"  Math Q{idx+1}: ID={q.get('questionId')}, text='{q.get('text')[:50]}...', imageUrl={q.get('imageUrl')}")
            
            # Log verbal recommendations
            if data.get('verbalRecommendations'):
                verbal_questions = data['verbalRecommendations'].get('questions', [])
                logger.info(f"Verbal recommendations: {len(verbal_questions)} questions")
                logger.info(f"Verbal reasoning: {data['verbalRecommendations'].get('reasoning')}")
                for idx, q in enumerate(verbal_questions[:2]):  # Show first 2 questions
                    logger.info(f"  Verbal Q{idx+1}: ID={q.get('questionId')}, text='{q.get('text')[:50]}...', imageUrl={q.get('imageUrl')}")

            # Prepend base URL to image URLs and clean up None values
            if data.get("mathRecommendations") and data["mathRecommendations"].get("questions"):
                for q in data["mathRecommendations"]["questions"]:
                    if q.get("imageUrl") and q["imageUrl"] != "None":
                        q["imageUrl"] = f"{MASTER_ED_BASE_URL}{q['imageUrl']}"
                    else:
                        q["imageUrl"] = None  # Convert string "None" to actual None

            if data.get("verbalRecommendations") and data["verbalRecommendations"].get("questions"):
                for q in data["verbalRecommendations"]["questions"]:
                    if q.get("imageUrl") and q["imageUrl"] != "None":
                        q["imageUrl"] = f"{MASTER_ED_BASE_URL}{q['imageUrl']}"
                    else:
                        q["imageUrl"] = None  # Convert string "None" to actual None

            # Log final processed data
            logger.info(f"DEBUG - After URL processing for {current_user.email}:")
            if data.get("mathRecommendations") and data["mathRecommendations"].get("questions"):
                logger.info(f"Math questions count: {len(data['mathRecommendations']['questions'])}")
                for idx, q in enumerate(data["mathRecommendations"]["questions"][:2]):
                    logger.info(f"  Processed Math Q{idx+1}: imageUrl='{q.get('imageUrl')}'")
            
            if data.get("verbalRecommendations") and data["verbalRecommendations"].get("questions"):
                logger.info(f"Verbal questions count: {len(data['verbalRecommendations']['questions'])}")
                for idx, q in enumerate(data["verbalRecommendations"]["questions"][:2]):
                    logger.info(f"  Processed Verbal Q{idx+1}: imageUrl='{q.get('imageUrl')}'")

            # Success - return data
            logger.info(f"Successfully fetched recommendations for {current_user.email}")
            logger.info(f"Returning data with keys: {list(data.keys())}")
            return data

        except httpx.ReadTimeout:
            logger.warning(f"Timeout on attempt {attempt + 1}/{max_retries} for {current_user.email}")
            if attempt < max_retries - 1:
                logger.info(f"Retrying...")
                continue
            else:
                logger.error(f"All {max_retries} attempts failed due to timeout")
                raise HTTPException(
                    status_code=504,
                    detail="The recommendations service is taking too long to respond. Please try again later."
                )

        except httpx.RequestError as e:
            logger.error(f"Request error fetching recommendations: {type(e).__name__}: {str(e)}")
            logger.error(f"Full error details: {repr(e)}")
            raise HTTPException(
                status_code=502,
                detail=f"Could not connect to recommendations service: {str(e)}"
            )

    # This should never be reached due to the loop logic, but for safety:
    raise HTTPException(
        status_code=500,
        detail="Internal error: retry loop exited unexpectedly"
    )


@router.post("/complete")
async def complete_daily_questions(
    request: CompleteDailyQuestionsRequest,
    current_user: UserInDB = Depends(get_current_user_dependency),
    db: Session = Depends(get_db)
):
    """Mark daily questions as completed for today."""
    if current_user.role != "student":
        raise HTTPException(status_code=403, detail="Only students can complete daily questions")

    today = date.today()

    # Check if already completed today
    existing = db.query(DailyQuestionCompletion).filter(
        DailyQuestionCompletion.user_id == current_user.id,
        DailyQuestionCompletion.completed_date == today
    ).first()

    if existing:
        return {
            "message": "Daily questions already completed today",
            "completed_today": True,
            "score": existing.score,
            "total_questions": existing.total_questions
        }

    completion = DailyQuestionCompletion(
        user_id=current_user.id,
        completed_date=today,
        questions_data=request.questions_data,
        score=request.score,
        total_questions=request.total_questions,
        created_at=datetime.now(timezone.utc)
    )
    db.add(completion)
    db.commit()

    score_n = int(request.score or 0)
    total_n = int(request.total_questions or 0)
    if total_n <= 0:
        points_amount = DAILY_QUESTIONS_POINTS_MIN
    else:
        ratio = min(1.0, max(0.0, score_n / total_n))
        points_amount = int(
            DAILY_QUESTIONS_POINTS_MIN
            + ratio * (DAILY_QUESTIONS_POINTS_MAX - DAILY_QUESTIONS_POINTS_MIN)
        )
        points_amount = max(
            DAILY_QUESTIONS_POINTS_MIN,
            min(DAILY_QUESTIONS_POINTS_MAX, points_amount),
        )

    try:
        award_points(
            db,
            current_user.id,
            points_amount,
            "daily_questions",
            f"date:{today.isoformat()}|score:{score_n}/{total_n}",
        )
    except Exception as e:
        logger.warning("Failed to award daily questions points: %s", e, exc_info=True)

    return {
        "message": "Daily questions completed!",
        "completed_today": True,
        "score": request.score,
        "total_questions": request.total_questions,
        "points_awarded": points_amount,
    }
