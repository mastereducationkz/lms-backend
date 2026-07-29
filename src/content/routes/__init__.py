from src.content.routes.flashcards import router as flashcards_router
from src.content.routes.questions import router as questions_router
from src.content.routes.ai_tools import router as ai_tools_router
from src.content.routes.favorite_steps import router as favorite_steps_router

__all__ = ["flashcards_router", "questions_router", "ai_tools_router", "favorite_steps_router"]
