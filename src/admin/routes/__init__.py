from src.admin.routes.admin import router as admin_router
from src.admin.routes.dashboard import router as dashboard_router
from src.admin.routes.head_teacher import router as head_teacher_router
from src.admin.routes.analytics import router as analytics_router
from src.admin.routes.media import router as media_router
from src.admin.routes.sat_schedules import router as sat_schedules_router

__all__ = [
    "admin_router", "dashboard_router", "head_teacher_router",
    "analytics_router", "media_router", "sat_schedules_router",
]
