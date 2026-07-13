"""Flashcards routes for managing favorite flashcards."""
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from typing import List
import json

from src.schemas.models import (
    FavoriteFlashcard,
    FavoriteFlashcardSchema,
    FavoriteFlashcardCreateSchema,
    UserInDB,
)
from src.routes.auth import get_current_user_dependency
from src.config import get_db

router = APIRouter()


@router.post("/favorites", response_model=FavoriteFlashcardSchema, status_code=status.HTTP_201_CREATED)
def add_favorite_flashcard(
    favorite: FavoriteFlashcardCreateSchema,
    current_user: UserInDB = Depends(get_current_user_dependency),
    db: Session = Depends(get_db)
):
    """
    Add a flashcard to user's favorites.
    
    Args:
        favorite: Flashcard data to add to favorites
        current_user: Current authenticated user
        db: Database session
        
    Returns:
        Created favorite flashcard
        
    Raises:
        HTTPException: If flashcard already exists in favorites
    """
    # Check if already favorited
    existing = db.query(FavoriteFlashcard).filter(
        FavoriteFlashcard.user_id == current_user.id,
        FavoriteFlashcard.step_id == favorite.step_id,
        FavoriteFlashcard.flashcard_id == favorite.flashcard_id
    ).first()
    
    if existing:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Flashcard already in favorites"
        )
    
    # Validate flashcard_data is valid JSON
    try:
        json.loads(favorite.flashcard_data)
    except json.JSONDecodeError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid flashcard data format"
        )
    
    # Create favorite
    db_favorite = FavoriteFlashcard(
        user_id=current_user.id,
        step_id=favorite.step_id,
        flashcard_id=favorite.flashcard_id,
        lesson_id=favorite.lesson_id,
        course_id=favorite.course_id,
        flashcard_data=favorite.flashcard_data
    )
    
    db.add(db_favorite)
    db.commit()
    db.refresh(db_favorite)
    
    return db_favorite


@router.get("/favorites", response_model=List[FavoriteFlashcardSchema])
def get_favorite_flashcards(
    current_user: UserInDB = Depends(get_current_user_dependency),
    db: Session = Depends(get_db)
):
    """
    Get all favorite flashcards for current user.
    
    Args:
        current_user: Current authenticated user
        db: Database session
        
    Returns:
        List of favorite flashcards
    """
    favorites = db.query(FavoriteFlashcard).filter(
        FavoriteFlashcard.user_id == current_user.id
    ).order_by(FavoriteFlashcard.created_at.desc()).all()
    
    return favorites


@router.get("/favorites/{favorite_id}", response_model=FavoriteFlashcardSchema)
def get_favorite_flashcard(
    favorite_id: int,
    current_user: UserInDB = Depends(get_current_user_dependency),
    db: Session = Depends(get_db)
):
    """
    Get a specific favorite flashcard.
    
    Args:
        favorite_id: ID of the favorite flashcard
        current_user: Current authenticated user
        db: Database session
        
    Returns:
        Favorite flashcard
        
    Raises:
        HTTPException: If flashcard not found
    """
    favorite = db.query(FavoriteFlashcard).filter(
        FavoriteFlashcard.id == favorite_id,
        FavoriteFlashcard.user_id == current_user.id
    ).first()
    
    if not favorite:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Favorite flashcard not found"
        )
    
    return favorite


@router.delete("/favorites/{favorite_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_favorite_flashcard(
    favorite_id: int,
    current_user: UserInDB = Depends(get_current_user_dependency),
    db: Session = Depends(get_db)
):
    """
    Remove a flashcard from favorites.
    
    Args:
        favorite_id: ID of the favorite flashcard to remove
        current_user: Current authenticated user
        db: Database session
        
    Raises:
        HTTPException: If flashcard not found
    """
    favorite = db.query(FavoriteFlashcard).filter(
        FavoriteFlashcard.id == favorite_id,
        FavoriteFlashcard.user_id == current_user.id
    ).first()
    
    if not favorite:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Favorite flashcard not found"
        )
    
    db.delete(favorite)
    db.commit()
    
    return None


@router.delete("/favorites/by-card/{step_id}/{flashcard_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_favorite_by_card_id(
    step_id: int,
    flashcard_id: str,
    current_user: UserInDB = Depends(get_current_user_dependency),
    db: Session = Depends(get_db)
):
    """
    Remove a flashcard from favorites by step_id and flashcard_id.
    This is useful when you want to unfavorite from the flashcard viewer.
    
    Args:
        step_id: ID of the step containing the flashcard
        flashcard_id: ID of the flashcard within the set
        current_user: Current authenticated user
        db: Database session
        
    Raises:
        HTTPException: If flashcard not found
    """
    favorite = db.query(FavoriteFlashcard).filter(
        FavoriteFlashcard.user_id == current_user.id,
        FavoriteFlashcard.step_id == step_id,
        FavoriteFlashcard.flashcard_id == flashcard_id
    ).first()
    
    if not favorite:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Favorite flashcard not found"
        )
    
    db.delete(favorite)
    db.commit()
    
    return None


@router.get("/favorites/check/{step_id}/{flashcard_id}")
def check_is_favorite(
    step_id: int,
    flashcard_id: str,
    current_user: UserInDB = Depends(get_current_user_dependency),
    db: Session = Depends(get_db)
):
    """
    Check if a flashcard is in user's favorites.
    
    Args:
        step_id: ID of the step containing the flashcard
        flashcard_id: ID of the flashcard within the set
        current_user: Current authenticated user
        db: Database session
        
    Returns:
        Dictionary with is_favorite boolean
    """
    favorite = db.query(FavoriteFlashcard).filter(
        FavoriteFlashcard.user_id == current_user.id,
        FavoriteFlashcard.step_id == step_id,
        FavoriteFlashcard.flashcard_id == flashcard_id
    ).first()
    
    return {"is_favorite": favorite is not None, "favorite_id": favorite.id if favorite else None}


# =============================================================================
# QUICK CREATE - For vocabulary from Lookup
# =============================================================================

from pydantic import BaseModel
from typing import Optional
import uuid

class QuickCreateFlashcardRequest(BaseModel):
    word: str
    translation: str
    definition: Optional[str] = None
    context: Optional[str] = None
    phonetic: Optional[str] = None


@router.post("/quick_create", status_code=status.HTTP_201_CREATED)
def quick_create_flashcard(
    request: QuickCreateFlashcardRequest,
    current_user: UserInDB = Depends(get_current_user_dependency),
    db: Session = Depends(get_db)
):
    """
    Quickly create a flashcard from the lookup feature.
    Stores as a special "My Vocabulary" favorite flashcard with step_id=0.
    """
    # Generate unique ID for this flashcard
    flashcard_id = f"vocab_{uuid.uuid4().hex[:8]}"
    
    # Build flashcard data JSON
    flashcard_data = {
        "id": flashcard_id,
        "front_text": request.word,
        "back_text": request.translation,
        "front_image_url": None,
        "back_image_url": None,
        "difficulty": "normal",
        "tags": ["vocabulary", "lookup"],
        "order_index": 0,
        # Extra fields for vocabulary cards
        "definition": request.definition,
        "context": request.context,
        "phonetic": request.phonetic,
        "source": "lookup"
    }
    
    # Use step_id=0 as a special marker for "My Vocabulary" cards
    # that don't belong to any specific lesson step
    db_favorite = FavoriteFlashcard(
        user_id=current_user.id,
        step_id=None,  # NULL for vocabulary from lookup
        flashcard_id=flashcard_id,
        lesson_id=None,
        course_id=None,
        flashcard_data=json.dumps(flashcard_data)
    )
    
    db.add(db_favorite)
    db.commit()
    db.refresh(db_favorite)
    
    return {
        "success": True,
        "message": f"Added '{request.word}' to your vocabulary",
        "flashcard_id": flashcard_id,
        "favorite_id": db_favorite.id
    }


@router.get("/vocabulary")
def get_vocabulary_cards(
    current_user: UserInDB = Depends(get_current_user_dependency),
    db: Session = Depends(get_db)
):
    """
    Get all vocabulary cards created from lookup (step_id=NULL).
    """
    favorites = db.query(FavoriteFlashcard).filter(
        FavoriteFlashcard.user_id == current_user.id,
        FavoriteFlashcard.step_id.is_(None)  # Vocabulary cards
    ).order_by(FavoriteFlashcard.created_at.desc()).all()
    
    cards = []
    for fav in favorites:
        try:
            card_data = json.loads(fav.flashcard_data)
            cards.append({
                "id": fav.id,
                "flashcard_id": fav.flashcard_id,
                "word": card_data.get("front_text"),
                "translation": card_data.get("back_text"),
                "definition": card_data.get("definition"),
                "context": card_data.get("context"),
                "phonetic": card_data.get("phonetic"),
                "created_at": fav.created_at.isoformat() if fav.created_at else None
            })
        except:
            continue
    
    return {"vocabulary": cards, "count": len(cards)}

