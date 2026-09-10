from src.events.routes.events import router as events_router
from src.events.routes.recordings import router as lesson_recordings_router
from src.events.routes.recording_library import router as recording_library_router
from src.events.routes.meet_attendance import router as meet_attendance_router

__all__ = ["events_router", "lesson_recordings_router", "recording_library_router", "meet_attendance_router"]
