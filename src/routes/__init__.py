from fastapi import FastAPI


def register_routes(app: FastAPI):
    """Register all domain routers with the FastAPI application."""
    from src.auth.routes import auth_router, users_router
    from src.admin.routes import (
        admin_router, dashboard_router,
        head_teacher_router, analytics_router, media_router,
        sat_schedules_router, weekly_top_students_router,
    )
    from src.courses.routes import courses_router
    from src.assignments.routes import assignments_router, assignment_zero_router
    from src.progress.routes import progress_router, admin_progress_router
    from src.events.routes import events_router
    from src.messages.routes import messages_router, notifications_router, group_messages_router
    from src.parents.routes import router as parents_router
    from src.gamification.routes import (
        gamification_router, leaderboard_router, daily_questions_router,
    )
    from src.content.routes import flashcards_router, questions_router, ai_tools_router, favorite_steps_router
    from src.curator.routes import curator_tasks_router, student_journal_router, onboarding_router
    from src.lesson_requests.routes import router as lesson_requests_router
    from src.routes.crm_internal import router as crm_internal_router
    from src.routes.crm_curator_internal import router as crm_curator_internal_router
    from src.routes.email_internal import router as email_internal_router
    from src.exams.routes import router as exams_router
    from src.trials.routes import trials_router
    from src.routes.support_api import router as support_api_router
    from src.reports.routes import router as reports_router
    from src.integrations.routes import router as integrations_router
    from src.integrations.handoff_routes import handoff_router, wellknown_router
    from src.integrations.assignment_routes import platform_assignments_router
    from src.integrations.targets_routes import targets_router
    from src.checkpoints.routes import checkpoints_router, checkpoints_admin_router

    app.include_router(auth_router, prefix="/auth", tags=["Authentication"])
    app.include_router(admin_router, prefix="/admin", tags=["Admin"])
    app.include_router(admin_progress_router, prefix="/admin", tags=["Admin Progress"])
    app.include_router(weekly_top_students_router, prefix="/admin", tags=["Admin"])
    app.include_router(dashboard_router, prefix="/dashboard", tags=["Dashboard"])
    app.include_router(users_router, prefix="/users", tags=["Users"])
    app.include_router(courses_router, prefix="/courses", tags=["Courses"])
    app.include_router(assignments_router, prefix="/assignments", tags=["Assignments"])
    app.include_router(messages_router, prefix="/messages", tags=["Messages"])
    app.include_router(group_messages_router, prefix="/messages", tags=["Group Chat"])
    app.include_router(notifications_router, prefix="/notifications", tags=["Notifications"])
    app.include_router(parents_router, prefix="/parents", tags=["Parents"])
    app.include_router(progress_router, prefix="/progress", tags=["Progress"])
    app.include_router(media_router, prefix="/media", tags=["Media"])
    app.include_router(events_router, prefix="/events", tags=["Events"])
    app.include_router(analytics_router, prefix="/analytics", tags=["Analytics"])
    app.include_router(flashcards_router, prefix="/flashcards", tags=["Flashcards"])
    app.include_router(favorite_steps_router, prefix="/favorite-steps", tags=["Favorite Steps"])
    app.include_router(leaderboard_router, prefix="/leaderboard", tags=["Leaderboard"])
    app.include_router(assignment_zero_router, prefix="/assignment-zero", tags=["Assignment Zero"])
    app.include_router(questions_router, tags=["Questions"])
    app.include_router(gamification_router, prefix="/gamification", tags=["Gamification"])
    app.include_router(ai_tools_router, prefix="/ai-tools", tags=["AI Tools"])
    app.include_router(head_teacher_router, prefix="/head-teacher", tags=["Head Teacher"])
    app.include_router(daily_questions_router, prefix="/daily-questions", tags=["Daily Questions"])
    app.include_router(lesson_requests_router, prefix="/lesson-requests", tags=["Lesson Requests"])
    app.include_router(curator_tasks_router, prefix="/curator-tasks", tags=["Curator Tasks"])
    app.include_router(student_journal_router, prefix="/student-journal", tags=["Student Journal"])
    app.include_router(onboarding_router, prefix="/curator-onboarding", tags=["Curator Onboarding"])
    app.include_router(sat_schedules_router, prefix="/sat", tags=["SAT Schedules"])
    app.include_router(crm_internal_router, prefix="/internal/crm", tags=["CRM Internal"])
    app.include_router(
        crm_curator_internal_router, prefix="/internal/crm/curator", tags=["CRM Curator Internal"]
    )
    app.include_router(email_internal_router, prefix="/internal/email", tags=["Email Internal"])
    app.include_router(exams_router, prefix="/exams", tags=["Exams"])
    app.include_router(trials_router, prefix="/trials", tags=["Trials"])
    app.include_router(support_api_router, prefix="/support-api", tags=["Support API"])
    app.include_router(reports_router, prefix="/reports", tags=["Reports"])
    app.include_router(integrations_router, prefix="/integrations", tags=["Platform Integrations"])
    app.include_router(platform_assignments_router, prefix="/integrations", tags=["Platform Integrations"])
    app.include_router(targets_router, prefix="/targets", tags=["Student Targets"])
    app.include_router(checkpoints_admin_router, prefix="/checkpoints/admin", tags=["Checkpoints Admin"])
    app.include_router(checkpoints_router, prefix="/checkpoints", tags=["Checkpoints"])
    app.include_router(handoff_router, prefix="/handoff", tags=["Platform Handoff"])
    app.include_router(wellknown_router, tags=["Platform Handoff"])
