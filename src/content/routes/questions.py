from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from sqlalchemy.orm import Session, joinedload
from pydantic import BaseModel
from typing import Optional, List, Any
from datetime import datetime
import json

from src.config import get_db
from src.schemas.models import UserInDB, QuestionErrorReport, Step, Lesson, Module, Course
from src.routes.auth import get_current_user
from src.services.telegram_service import notify_admins_about_error_report

router = APIRouter(prefix="/questions", tags=["Questions"])


class ReportErrorRequest(BaseModel):
    question_id: str
    message: str
    step_id: Optional[int] = None
    suggested_answer: Optional[str] = None


class ReportErrorResponse(BaseModel):
    success: bool
    message: str


class UpdateQuestionRequest(BaseModel):
    question_text: Optional[str] = None
    correct_answer: Optional[Any] = None
    options: Optional[List[Any]] = None
    explanation: Optional[str] = None


@router.post("/report-error", response_model=ReportErrorResponse)
def report_question_error(
    request: ReportErrorRequest,
    background_tasks: BackgroundTasks,
    current_user: UserInDB = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """
    Report an error in a quiz question.
    Students can report mistakes, typos, or incorrect answers in questions.
    """
    # Create the error report
    report = QuestionErrorReport(
        question_id=str(request.question_id),
        user_id=current_user.id,
        step_id=request.step_id,
        message=request.message,
        suggested_answer=request.suggested_answer,
        created_at=datetime.utcnow(),
        status="pending"
    )
    
    db.add(report)
    db.commit()
    db.refresh(report)
    
    # Get additional info for Telegram notification
    question_text = "Question"
    course_title = None
    module_title = None
    lesson_title = None
    step_title = None
    
    if request.step_id:
        step = db.query(Step).options(
            joinedload(Step.lesson).joinedload(Lesson.module).joinedload(Module.course)
        ).filter(Step.id == request.step_id).first()
        
        if step:
            step_title = step.title
            
            # Try to get question text from step content
            if step.content_text:
                try:
                    content = json.loads(step.content_text)
                    questions = content.get("questions", [])
                    for q in questions:
                        if str(q.get("id")) == str(request.question_id):
                            question_text = q.get("question_text", "Question")
                            # Strip HTML tags for cleaner Telegram message
                            import re
                            question_text = re.sub(r'<[^>]+>', '', question_text)
                            break
                except (json.JSONDecodeError, TypeError):
                    pass
            
            if step.lesson:
                lesson_title = step.lesson.title
                if step.lesson.module:
                    module_title = step.lesson.module.title
                    if step.lesson.module.course:
                        course_title = step.lesson.module.course.title
    
    # Send Telegram notification in background
    background_tasks.add_task(
        notify_admins_about_error_report,
        report_id=report.id,
        question_text=question_text,
        reporter_name=current_user.name,
        reporter_email=current_user.email,
        error_message=request.message,
        suggested_answer=request.suggested_answer,
        course_title=course_title,
        module_title=module_title,
        lesson_title=lesson_title,
        step_title=step_title,
    )
    
    return ReportErrorResponse(
        success=True,
        message="Error report submitted successfully. Thank you for helping us improve!"
    )


@router.get("/error-reports")
def get_error_reports(
    status: Optional[str] = None,
    current_user: UserInDB = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """
    Get all question error reports with full context (admin only).
    """
    if current_user.role not in ["admin", "teacher"]:
        raise HTTPException(status_code=403, detail="Not authorized")
    
    query = db.query(QuestionErrorReport).options(
        joinedload(QuestionErrorReport.user),
        joinedload(QuestionErrorReport.step).joinedload(Step.lesson).joinedload(Lesson.module).joinedload(Module.course),
        joinedload(QuestionErrorReport.resolver)
    )
    
    if status:
        query = query.filter(QuestionErrorReport.status == status)
    
    reports = query.order_by(QuestionErrorReport.created_at.desc()).all()
    
    result = []
    for report in reports:
        # Get question details from step content
        question_data = None
        step_info = None
        course_info = None
        
        if report.step:
            # Calculate step number (1-based index based on order in lesson)
            step_number = 1
            try:
                # Count steps in the same lesson with lower order_index
                prev_steps_count = db.query(Step).filter(
                    Step.lesson_id == report.step.lesson_id,
                    Step.order_index < report.step.order_index
                ).count()
                step_number = prev_steps_count + 1
            except Exception:
                pass

            step_info = {
                "id": report.step.id,
                "title": report.step.title,
                "content_type": report.step.content_type,
                "step_number": step_number
            }
            
            # Get course info through relationships
            if report.step.lesson and report.step.lesson.module and report.step.lesson.module.course:
                course = report.step.lesson.module.course
                course_info = {
                    "id": course.id,
                    "title": course.title,
                    "lesson_id": report.step.lesson.id,
                    "lesson_title": report.step.lesson.title,
                    "module_title": report.step.lesson.module.title,
                }
            
            # Parse quiz content to find the question
            if report.step.content_text:
                try:
                    content = json.loads(report.step.content_text)
                    questions = content.get("questions", [])
                    for q in questions:
                        # Compare as strings since question IDs in JSON are strings
                        if str(q.get("id")) == str(report.question_id):
                            question_data = q
                            break
                except (json.JSONDecodeError, TypeError):
                    pass
        
        result.append({
            "id": report.id,
            "question_id": report.question_id,
            "user_id": report.user_id,
            "user_name": report.user.name if report.user else "Unknown",
            "user_email": report.user.email if report.user else None,
            "step_id": report.step_id,
            "step_info": step_info,
            "course_info": course_info,
            "message": report.message,
            "suggested_answer": report.suggested_answer,
            "status": report.status,
            "created_at": report.created_at.isoformat() if report.created_at else None,
            "resolved_at": report.resolved_at.isoformat() if report.resolved_at else None,
            "resolved_by": report.resolved_by,
            "resolver_name": report.resolver.name if report.resolver else None,
            "question_data": question_data,
        })
    
    return result


@router.get("/error-reports/{report_id}")
def get_error_report_detail(
    report_id: int,
    current_user: UserInDB = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """
    Get detailed information about a specific error report including full question context.
    """
    if current_user.role not in ["admin", "teacher"]:
        raise HTTPException(status_code=403, detail="Not authorized")
    
    report = db.query(QuestionErrorReport).options(
        joinedload(QuestionErrorReport.user),
        joinedload(QuestionErrorReport.step),
        joinedload(QuestionErrorReport.resolver)
    ).filter(QuestionErrorReport.id == report_id).first()
    
    if not report:
        raise HTTPException(status_code=404, detail="Report not found")
    
    # Get full step and question data
    question_data = None
    all_questions = []
    quiz_settings = {}
    
    if report.step and report.step.content_text:
        try:
            content = json.loads(report.step.content_text)
            all_questions = content.get("questions", [])
            quiz_settings = {
                "title": content.get("title"),
                "description": content.get("description"),
                "time_limit": content.get("time_limit"),
                "passing_score": content.get("passing_score"),
            }
            for q in all_questions:
                # Compare as strings since question IDs in JSON are strings
                if str(q.get("id")) == str(report.question_id):
                    question_data = q
                    break
        except (json.JSONDecodeError, TypeError):
            pass
    
    # Get course hierarchy
    course_info = None
    step_number = 1

    if report.step:
        # Calculate step number
        try:
            prev_steps_count = db.query(Step).filter(
                Step.lesson_id == report.step.lesson_id,
                Step.order_index < report.step.order_index
            ).count()
            step_number = prev_steps_count + 1
        except Exception:
            pass

        step = db.query(Step).options(
            joinedload(Step.lesson).joinedload(Lesson.module).joinedload(Module.course)
        ).filter(Step.id == report.step_id).first()
        
        if step and step.lesson and step.lesson.module and step.lesson.module.course:
            course_info = {
                "course_id": step.lesson.module.course.id,
                "course_title": step.lesson.module.course.title,
                "module_id": step.lesson.module.id,
                "module_title": step.lesson.module.title,
                "lesson_id": step.lesson.id,
                "lesson_title": step.lesson.title,
            }
    
    sibling_reports = []
    if report.step_id is not None:
        others = (
            db.query(QuestionErrorReport)
            .options(joinedload(QuestionErrorReport.user))
            .filter(
                QuestionErrorReport.step_id == report.step_id,
                QuestionErrorReport.question_id == report.question_id,
                QuestionErrorReport.id != report.id,
            )
            .order_by(QuestionErrorReport.created_at.desc())
            .all()
        )
        sibling_reports = [
            {
                "id": o.id,
                "message": o.message,
                "suggested_answer": o.suggested_answer,
                "status": o.status,
                "created_at": o.created_at.isoformat() if o.created_at else None,
                "user_name": o.user.name if o.user else "Unknown",
                "user_email": o.user.email if o.user else None,
            }
            for o in others
        ]

    return {
        "report": {
            "id": report.id,
            "question_id": report.question_id,
            "message": report.message,
            "suggested_answer": report.suggested_answer,
            "status": report.status,
            "created_at": report.created_at.isoformat() if report.created_at else None,
            "resolved_at": report.resolved_at.isoformat() if report.resolved_at else None,
        },
        "sibling_reports": sibling_reports,
        "user": {
            "id": report.user.id,
            "name": report.user.name,
            "email": report.user.email,
        } if report.user else None,
        "resolver": {
            "id": report.resolver.id,
            "name": report.resolver.name,
        } if report.resolver else None,
        "course_info": course_info,
        "step": {
            "id": report.step.id,
            "title": report.step.title,
            "content_type": report.step.content_type,
            "original_image_url": report.step.original_image_url,
            "step_number": step_number,
        } if report.step else None,
        "quiz_settings": quiz_settings,
        "question_data": question_data,
        "question_index": next((i for i, q in enumerate(all_questions) if str(q.get("id")) == str(report.question_id)), -1),
        "total_questions": len(all_questions),
    }


@router.patch("/error-reports/{report_id}")
def update_error_report(
    report_id: int,
    status: str,
    sync_same_question: bool = True,
    current_user: UserInDB = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """
    Update the status of an error report (admin only).
    When sync_same_question is True (default), applies the same status to all reports
    for the same quiz step and question_id so duplicates are handled once.
    """
    if current_user.role not in ["admin", "teacher"]:
        raise HTTPException(status_code=403, detail="Not authorized")
    
    report = db.query(QuestionErrorReport).filter(QuestionErrorReport.id == report_id).first()
    
    if not report:
        raise HTTPException(status_code=404, detail="Report not found")

    if sync_same_question and report.step_id is not None:
        reports_to_update = (
            db.query(QuestionErrorReport)
            .filter(
                QuestionErrorReport.step_id == report.step_id,
                QuestionErrorReport.question_id == report.question_id,
            )
            .all()
        )
    else:
        reports_to_update = [report]

    updated_ids: list[int] = []
    for r in reports_to_update:
        r.status = status
        if status == "resolved":
            r.resolved_at = datetime.utcnow()
            r.resolved_by = current_user.id
        else:
            r.resolved_at = None
            r.resolved_by = None
        updated_ids.append(r.id)

    db.commit()

    return {
        "success": True,
        "message": "Report updated successfully",
        "updated_report_ids": updated_ids,
        "synced_count": len(updated_ids),
    }


@router.put("/update-question/{step_id}/{question_id}")
def update_question(
    step_id: int,
    question_id: str,
    request: UpdateQuestionRequest,
    current_user: UserInDB = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """
    Update a specific question in a quiz step (admin only).
    """
    if current_user.role not in ["admin", "teacher"]:
        raise HTTPException(status_code=403, detail="Not authorized")
    
    step = db.query(Step).filter(Step.id == step_id).first()
    
    if not step:
        raise HTTPException(status_code=404, detail="Step not found")
    
    if step.content_type != "quiz":
        raise HTTPException(status_code=400, detail="Step is not a quiz")
    
    try:
        content = json.loads(step.content_text) if step.content_text else {}
        questions = content.get("questions", [])
        
        question_found = False
        updated_question = None
        for i, q in enumerate(questions):
            if str(q.get("id")) == str(question_id):
                # Update the question with provided fields
                if request.question_text is not None:
                    questions[i]["question_text"] = request.question_text
                if request.correct_answer is not None:
                    questions[i]["correct_answer"] = request.correct_answer
                if request.options is not None:
                    questions[i]["options"] = request.options
                if request.explanation is not None:
                    questions[i]["explanation"] = request.explanation
                question_found = True
                updated_question = questions[i]
                break
        
        if not question_found:
            raise HTTPException(status_code=404, detail="Question not found in this step")
        
        content["questions"] = questions
        step.content_text = json.dumps(content)
        db.commit()
        
        return {
            "success": True,
            "message": "Question updated successfully",
            "question": updated_question
        }
        
    except json.JSONDecodeError:
        raise HTTPException(status_code=500, detail="Failed to parse quiz content")
