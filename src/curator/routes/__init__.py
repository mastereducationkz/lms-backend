from src.curator.routes.curator_tasks import router as curator_tasks_router
from src.curator.routes.student_journal import router as student_journal_router
from src.curator.routes.onboarding import router as onboarding_router

__all__ = ["curator_tasks_router", "student_journal_router", "onboarding_router"]
