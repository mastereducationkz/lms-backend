from src.models.base import Base

# Registered here so `Base.metadata.create_all` and Alembic autogenerate both see it.
from src.crm_audit.models import CrmAuditOutbox

# Imported as a module rather than by name. Unlike the domain models below, this one also
# carries the journal's helper functions, so `from src.services.email_log import ...` is a
# plausible *first* import in a script — and binding names out of it here would turn that
# into a circular-import error. Importing the module is enough: defining the classes is
# what registers their tables on Base.metadata.
from src.services import email_log as _email_log  # noqa: F401

from src.auth.models import UserInDB, PointHistory, UserPushToken
from src.courses.models import (
    Group, GroupStudent, Step, Course, CourseHeadTeacher,
    CourseGroupAccess, CourseTeacherAccess, Module, Lesson,
    LessonMaterial, Enrollment, ManualLessonUnlock,
)
from src.assignments.models import (
    Assignment, AssignmentSubmission, AssignmentLinkedLesson,
    AssignmentExtension, GroupAssignment, AssignmentZeroSubmission,
    AssignmentDraft,
)
from src.progress.models import (
    StudentProgress, StepProgress, ProgressSnapshot,
    StudentCourseSummary, CourseAnalyticsCache, QuizAttempt,
)
from src.events.models import (
    Event, EventGroup, EventCourse, EventParticipant,
    MissedAttendanceLog, LessonSchedule, Attendance,
)
from src.messages.models import Message, Notification
from src.messages.group_models import GroupConversation, GroupConversationMember, GroupMessage
from src.gamification.models import (
    LeaderboardEntry, LeaderboardConfig, CuratorRating,
    DailyQuestionCompletion,
)
from src.content.models import FavoriteFlashcard, QuestionErrorReport, FavoriteStep
from src.curator.models import (
    CuratorTaskTemplate, CuratorTaskInstance, CuratorOnboarding,
    CuratorOnboardingEvent, CuratorOnboardingNote,
)
from src.lesson_requests.models import LessonRequest
from src.parents.models import ParentStudent
from src.exams.models import ExamResult, BluebookResult, StudentTestimonial
from src.trials.models import TrialAccess

__all__ = [
    "Base",
    "UserInDB", "PointHistory", "UserPushToken",
    "Group", "GroupStudent", "Step", "Course", "CourseHeadTeacher",
    "CourseGroupAccess", "CourseTeacherAccess", "Module", "Lesson",
    "LessonMaterial", "Enrollment", "ManualLessonUnlock",
    "Assignment", "AssignmentSubmission", "AssignmentLinkedLesson",
    "AssignmentExtension", "GroupAssignment", "AssignmentZeroSubmission",
    "AssignmentDraft",
    "StudentProgress", "StepProgress", "ProgressSnapshot",
    "StudentCourseSummary", "CourseAnalyticsCache", "QuizAttempt",
    "Event", "EventGroup", "EventCourse", "EventParticipant",
    "MissedAttendanceLog", "LessonSchedule", "Attendance",
    "Message", "Notification",
    "GroupConversation", "GroupConversationMember", "GroupMessage",
    "LeaderboardEntry", "LeaderboardConfig", "CuratorRating",
    "DailyQuestionCompletion",
    "FavoriteFlashcard", "QuestionErrorReport", "FavoriteStep",
    "CuratorTaskTemplate", "CuratorTaskInstance", "CuratorOnboarding",
    "CuratorOnboardingEvent", "CuratorOnboardingNote",
    "LessonRequest",
    "ParentStudent",
    "ExamResult", "BluebookResult", "StudentTestimonial",
    "TrialAccess",
    "CrmAuditOutbox",
]
