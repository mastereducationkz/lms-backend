from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from sqlalchemy.orm import Session
from datetime import datetime, timezone, timedelta
import os
import uuid

from src.config import get_db
from src.schemas.models import (
    UserInDB, AssignmentZeroSubmission, 
    AssignmentZeroSubmissionSchema, AssignmentZeroSubmitSchema,
    AssignmentZeroSaveProgressSchema, Group, GroupStudent,
    AssignmentZeroPlannedDateUpdateSchema, AssignmentZeroExamResultUpdateSchema
)
from src.routes.auth import get_current_user_dependency
from src.services import storage_service
from src.utils.course_access import student_has_only_special_groups
from src.assignments.exam_dates import (
    SAT_OFFICIAL_TEST_DATES,
    parse_sat_target_date,
    format_sat_label,
)

router = APIRouter()

# Upload directory for Assignment Zero screenshots
UPLOAD_DIR = "uploads/assignment_zero"
os.makedirs(UPLOAD_DIR, exist_ok=True)

_ALLOWED_IMAGE_MIMES = frozenset(
    {"image/jpeg", "image/jpg", "image/png", "image/gif", "image/webp"}
)


def _sniff_image_mime(contents: bytes) -> str | None:
    """Detect image MIME from magic bytes when the client sends wrong or empty Content-Type."""
    if len(contents) >= 3 and contents[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if len(contents) >= 8 and contents[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if len(contents) >= 6 and contents[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if len(contents) >= 12 and contents[:4] == b"RIFF" and contents[8:12] == b"WEBP":
        return "image/webp"
    return None


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)

def _align_datetime_kind(reference: datetime, value: datetime) -> datetime:
    """
    Align datetime awareness (naive vs aware) to match reference datetime.
    Prevents TypeError when DB stores naive timestamps.
    """
    if reference.tzinfo is None and value.tzinfo is not None:
        return value.replace(tzinfo=None)
    if reference.tzinfo is not None and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _normalize_exam_type(exam_type: str) -> str:
    normalized = exam_type.strip().lower()
    if normalized not in {"sat", "ielts"}:
        raise HTTPException(status_code=400, detail="exam_type must be either 'sat' or 'ielts'")
    return normalized


def _set_planned_dates_from_targets(submission: AssignmentZeroSubmission) -> None:
    if submission.sat_planned_test_date is None:
        submission.sat_planned_test_date = parse_sat_target_date(submission.sat_target_date)

    if submission.ielts_target_date and submission.ielts_planned_test_date is None:
        try:
            submission.ielts_planned_test_date = datetime.fromisoformat(submission.ielts_target_date).date()
        except ValueError:
            submission.ielts_planned_test_date = None


def _curator_can_access_student(db: Session, *, curator_id: int, student_id: int) -> bool:
    """Return True if student belongs to an active group curated by curator_id."""
    count = (
        db.query(GroupStudent)
        .join(Group, Group.id == GroupStudent.group_id)
        .filter(
            GroupStudent.student_id == student_id,
            Group.curator_id == curator_id,
            Group.is_active == True,
        )
        .count()
    )
    return count > 0


def _is_ielts_group_name(group_name: str | None) -> bool:
    normalized = (group_name or "").lower()
    return ("ielts" in normalized) or ("iealts" in normalized)


def _forbid_special_group_student_assignment_zero(user: UserInDB, db: Session) -> None:
    if user.role != "student":
        return
    if student_has_only_special_groups(user.id, db):
        raise HTTPException(
            status_code=403,
            detail="Assignment Zero is not available for your group type",
        )


class CuratorUpcomingExamRow(AssignmentZeroSubmissionSchema):
    exam_type: str
    planned_test_date: str | None = None
    ask_result_on: str | None = None
    status: str  # pending | overdue | completed

# =============================================================================
# ASSIGNMENT ZERO ENDPOINTS
# =============================================================================

@router.get("/status")
async def get_assignment_zero_status(
    current_user: UserInDB = Depends(get_current_user_dependency),
    db: Session = Depends(get_db)
):
    """Check if student needs to complete Assignment Zero"""
    # Only students need to complete Assignment Zero
    if current_user.role != "student":
        return {
            "needs_completion": False,
            "completed": True,
            "message": "Assignment Zero is only for students",
            "user_groups": []
        }

    if student_has_only_special_groups(current_user.id, db):
        return {
            "needs_completion": False,
            "completed": bool(current_user.assignment_zero_completed),
            "special_group_exempt": True,
            "message": "Assignment Zero does not apply to your group",
            "user_groups": [],
        }

    # Check for draft submission
    draft = db.query(AssignmentZeroSubmission).filter(
        AssignmentZeroSubmission.user_id == current_user.id,
        AssignmentZeroSubmission.is_draft == True
    ).first()
    
    # Get user's groups with names
    user_group_students = db.query(GroupStudent).filter(
        GroupStudent.student_id == current_user.id
    ).all()
    
    user_groups = []
    for gs in user_group_students:
        group = db.query(Group).filter(Group.id == gs.group_id).first()
        if group and group.is_active:
            user_groups.append({
                "id": group.id,
                "name": group.name,
                "program_type": getattr(group, "program_type", None) or "general_english",
                "is_special": bool(getattr(group, "is_special", False)),
            })
    
    return {
        "needs_completion": not current_user.assignment_zero_completed,
        "completed": current_user.assignment_zero_completed,
        "completed_at": current_user.assignment_zero_completed_at,
        "has_draft": draft is not None,
        "last_saved_step": draft.last_saved_step if draft else None,
        "user_groups": user_groups
    }

@router.get("/my-submission", response_model=AssignmentZeroSubmissionSchema)
async def get_my_assignment_zero_submission(
    current_user: UserInDB = Depends(get_current_user_dependency),
    db: Session = Depends(get_db)
):
    """Get current user's Assignment Zero submission (including draft)"""
    _forbid_special_group_student_assignment_zero(current_user, db)
    submission = db.query(AssignmentZeroSubmission).filter(
        AssignmentZeroSubmission.user_id == current_user.id
    ).first()
    
    if not submission:
        raise HTTPException(status_code=404, detail="Assignment Zero submission not found")

    before_sat = submission.sat_planned_test_date
    before_ielts = submission.ielts_planned_test_date
    _set_planned_dates_from_targets(submission)
    if submission.sat_planned_test_date != before_sat or submission.ielts_planned_test_date != before_ielts:
        submission.updated_at = _utc_now()
        db.commit()
        db.refresh(submission)

    return AssignmentZeroSubmissionSchema.model_validate(submission)

@router.post("/save-progress", response_model=AssignmentZeroSubmissionSchema)
async def save_assignment_zero_progress(
    data: AssignmentZeroSaveProgressSchema,
    current_user: UserInDB = Depends(get_current_user_dependency),
    db: Session = Depends(get_db)
):
    """Save progress on Assignment Zero (auto-save)"""
    if current_user.role != "student":
        raise HTTPException(status_code=403, detail="Only students can save Assignment Zero progress")
    _forbid_special_group_student_assignment_zero(current_user, db)

    # Check if already fully submitted (not draft)
    existing = db.query(AssignmentZeroSubmission).filter(
        AssignmentZeroSubmission.user_id == current_user.id
    ).first()
    
    if existing and not existing.is_draft:
        raise HTTPException(status_code=400, detail="Assignment Zero already submitted")
    
    # Update or create draft
    if existing:
        # Update existing draft with non-None values
        for field, value in data.model_dump(exclude_unset=True).items():
            if value is not None:
                setattr(existing, field, value)
        existing.updated_at = datetime.utcnow()
        db.commit()
        db.refresh(existing)
        return AssignmentZeroSubmissionSchema.model_validate(existing)
    else:
        # Create new draft submission with default required values
        draft = AssignmentZeroSubmission(
            user_id=current_user.id,
            full_name=data.full_name or current_user.name or "",
            phone_number=data.phone_number or "",
            parent_phone_number=data.parent_phone_number or "",
            telegram_id=data.telegram_id or "",
            email=data.email or current_user.email or "",
            college_board_email=data.college_board_email or "",
            college_board_password=data.college_board_password or "",
            birthday_date=data.birthday_date or datetime.now().date(),
            city=data.city or "",
            school_type=data.school_type or "",
            group_name=data.group_name or "",
            sat_target_date=data.sat_target_date or "",
            sat_planned_test_date=data.sat_planned_test_date or parse_sat_target_date(data.sat_target_date),
            sat_result_score=data.sat_result_score,
            sat_result_test_date=data.sat_result_test_date,
            has_passed_sat_before=data.has_passed_sat_before or False,
            previous_sat_score=data.previous_sat_score,
            recent_practice_test_score=data.recent_practice_test_score or "",
            bluebook_practice_test_5_score=data.bluebook_practice_test_5_score or "",
            screenshot_url=data.screenshot_url,
            # SAT Assessment fields
            grammar_punctuation=data.grammar_punctuation,
            grammar_noun_clauses=data.grammar_noun_clauses,
            grammar_relative_clauses=data.grammar_relative_clauses,
            grammar_verb_forms=data.grammar_verb_forms,
            grammar_comparisons=data.grammar_comparisons,
            grammar_transitions=data.grammar_transitions,
            grammar_synthesis=data.grammar_synthesis,
            reading_word_in_context=data.reading_word_in_context,
            reading_text_structure=data.reading_text_structure,
            reading_cross_text=data.reading_cross_text,
            reading_central_ideas=data.reading_central_ideas,
            reading_inferences=data.reading_inferences,
            passages_literary=data.passages_literary,
            passages_social_science=data.passages_social_science,
            passages_humanities=data.passages_humanities,
            passages_science=data.passages_science,
            passages_poetry=data.passages_poetry,
            math_topics=data.math_topics,
            # IELTS fields
            ielts_target_date=data.ielts_target_date,
            ielts_planned_test_date=data.ielts_planned_test_date,
            ielts_result_score=data.ielts_result_score,
            ielts_result_test_date=data.ielts_result_test_date,
            ielts_last_date_prompted_at=data.ielts_last_date_prompted_at,
            has_passed_ielts_before=data.has_passed_ielts_before or False,
            previous_ielts_score=data.previous_ielts_score,
            ielts_target_score=data.ielts_target_score,
            ielts_listening_main_idea=data.ielts_listening_main_idea,
            ielts_listening_details=data.ielts_listening_details,
            ielts_listening_opinion=data.ielts_listening_opinion,
            ielts_listening_accents=data.ielts_listening_accents,
            ielts_reading_skimming=data.ielts_reading_skimming,
            ielts_reading_scanning=data.ielts_reading_scanning,
            ielts_reading_vocabulary=data.ielts_reading_vocabulary,
            ielts_reading_inference=data.ielts_reading_inference,
            ielts_reading_matching=data.ielts_reading_matching,
            ielts_writing_task1_graphs=data.ielts_writing_task1_graphs,
            ielts_writing_task1_process=data.ielts_writing_task1_process,
            ielts_writing_task2_structure=data.ielts_writing_task2_structure,
            ielts_writing_task2_arguments=data.ielts_writing_task2_arguments,
            ielts_writing_grammar=data.ielts_writing_grammar,
            ielts_writing_vocabulary=data.ielts_writing_vocabulary,
            ielts_speaking_fluency=data.ielts_speaking_fluency,
            ielts_speaking_vocabulary=data.ielts_speaking_vocabulary,
            ielts_speaking_grammar=data.ielts_speaking_grammar,
            ielts_speaking_pronunciation=data.ielts_speaking_pronunciation,
            ielts_speaking_part2=data.ielts_speaking_part2,
            ielts_speaking_part3=data.ielts_speaking_part3,
            ielts_weak_topics=data.ielts_weak_topics,
            additional_comments=data.additional_comments,
            # Draft status
            is_draft=True,
            last_saved_step=data.last_saved_step or 1
        )
        db.add(draft)
        db.commit()
        db.refresh(draft)
        return AssignmentZeroSubmissionSchema.model_validate(draft)

@router.post("/submit", response_model=AssignmentZeroSubmissionSchema)
async def submit_assignment_zero(
    data: AssignmentZeroSubmitSchema,
    current_user: UserInDB = Depends(get_current_user_dependency),
    db: Session = Depends(get_db)
):
    """Submit Assignment Zero questionnaire"""
    # Only students can submit
    if current_user.role != "student":
        raise HTTPException(status_code=403, detail="Only students can submit Assignment Zero")
    _forbid_special_group_student_assignment_zero(current_user, db)

    # Check if already submitted (not draft)
    existing = db.query(AssignmentZeroSubmission).filter(
        AssignmentZeroSubmission.user_id == current_user.id
    ).first()
    
    if existing and not existing.is_draft:
        raise HTTPException(status_code=400, detail="Assignment Zero already submitted")
    
    if existing:
        # Update existing draft to final submission
        existing.full_name = data.full_name
        existing.phone_number = data.phone_number
        existing.parent_phone_number = data.parent_phone_number
        existing.telegram_id = data.telegram_id
        existing.email = data.email
        existing.college_board_email = data.college_board_email
        existing.college_board_password = data.college_board_password
        existing.birthday_date = data.birthday_date
        existing.city = data.city
        existing.school_type = data.school_type
        existing.group_name = data.group_name
        existing.sat_target_date = data.sat_target_date
        existing.sat_planned_test_date = data.sat_planned_test_date or parse_sat_target_date(data.sat_target_date)
        existing.sat_result_score = data.sat_result_score
        existing.sat_result_test_date = data.sat_result_test_date
        existing.has_passed_sat_before = data.has_passed_sat_before
        existing.previous_sat_score = data.previous_sat_score
        existing.recent_practice_test_score = data.recent_practice_test_score
        existing.bluebook_practice_test_5_score = data.bluebook_practice_test_5_score
        existing.screenshot_url = data.screenshot_url
        # SAT Assessment fields
        existing.grammar_punctuation = data.grammar_punctuation
        existing.grammar_noun_clauses = data.grammar_noun_clauses
        existing.grammar_relative_clauses = data.grammar_relative_clauses
        existing.grammar_verb_forms = data.grammar_verb_forms
        existing.grammar_comparisons = data.grammar_comparisons
        existing.grammar_transitions = data.grammar_transitions
        existing.grammar_synthesis = data.grammar_synthesis
        existing.reading_word_in_context = data.reading_word_in_context
        existing.reading_text_structure = data.reading_text_structure
        existing.reading_cross_text = data.reading_cross_text
        existing.reading_central_ideas = data.reading_central_ideas
        existing.reading_inferences = data.reading_inferences
        existing.passages_literary = data.passages_literary
        existing.passages_social_science = data.passages_social_science
        existing.passages_humanities = data.passages_humanities
        existing.passages_science = data.passages_science
        existing.passages_poetry = data.passages_poetry
        existing.math_topics = data.math_topics
        # IELTS fields
        existing.ielts_target_date = data.ielts_target_date
        existing.ielts_planned_test_date = data.ielts_planned_test_date
        existing.ielts_result_score = data.ielts_result_score
        existing.ielts_result_test_date = data.ielts_result_test_date
        existing.ielts_last_date_prompted_at = data.ielts_last_date_prompted_at
        existing.has_passed_ielts_before = data.has_passed_ielts_before
        existing.previous_ielts_score = data.previous_ielts_score
        existing.ielts_target_score = data.ielts_target_score
        existing.ielts_listening_main_idea = data.ielts_listening_main_idea
        existing.ielts_listening_details = data.ielts_listening_details
        existing.ielts_listening_opinion = data.ielts_listening_opinion
        existing.ielts_listening_accents = data.ielts_listening_accents
        existing.ielts_reading_skimming = data.ielts_reading_skimming
        existing.ielts_reading_scanning = data.ielts_reading_scanning
        existing.ielts_reading_vocabulary = data.ielts_reading_vocabulary
        existing.ielts_reading_inference = data.ielts_reading_inference
        existing.ielts_reading_matching = data.ielts_reading_matching
        existing.ielts_writing_task1_graphs = data.ielts_writing_task1_graphs
        existing.ielts_writing_task1_process = data.ielts_writing_task1_process
        existing.ielts_writing_task2_structure = data.ielts_writing_task2_structure
        existing.ielts_writing_task2_arguments = data.ielts_writing_task2_arguments
        existing.ielts_writing_grammar = data.ielts_writing_grammar
        existing.ielts_writing_vocabulary = data.ielts_writing_vocabulary
        existing.ielts_speaking_fluency = data.ielts_speaking_fluency
        existing.ielts_speaking_vocabulary = data.ielts_speaking_vocabulary
        existing.ielts_speaking_grammar = data.ielts_speaking_grammar
        existing.ielts_speaking_pronunciation = data.ielts_speaking_pronunciation
        existing.ielts_speaking_part2 = data.ielts_speaking_part2
        existing.ielts_speaking_part3 = data.ielts_speaking_part3
        existing.ielts_weak_topics = data.ielts_weak_topics
        existing.additional_comments = data.additional_comments
        # Mark as submitted
        existing.is_draft = False
        existing.updated_at = datetime.utcnow()
        
        submission = existing
    else:
        # Create new submission
        submission = AssignmentZeroSubmission(
            user_id=current_user.id,
            full_name=data.full_name,
            phone_number=data.phone_number,
            parent_phone_number=data.parent_phone_number,
            telegram_id=data.telegram_id,
            email=data.email,
            college_board_email=data.college_board_email,
            college_board_password=data.college_board_password,
            birthday_date=data.birthday_date,
            city=data.city,
            school_type=data.school_type,
            group_name=data.group_name,
            sat_target_date=data.sat_target_date,
            sat_planned_test_date=data.sat_planned_test_date or parse_sat_target_date(data.sat_target_date),
            sat_result_score=data.sat_result_score,
            sat_result_test_date=data.sat_result_test_date,
            has_passed_sat_before=data.has_passed_sat_before,
            previous_sat_score=data.previous_sat_score,
            recent_practice_test_score=data.recent_practice_test_score,
            bluebook_practice_test_5_score=data.bluebook_practice_test_5_score,
            screenshot_url=data.screenshot_url,
            # SAT Assessment fields
            grammar_punctuation=data.grammar_punctuation,
            grammar_noun_clauses=data.grammar_noun_clauses,
            grammar_relative_clauses=data.grammar_relative_clauses,
            grammar_verb_forms=data.grammar_verb_forms,
            grammar_comparisons=data.grammar_comparisons,
            grammar_transitions=data.grammar_transitions,
            grammar_synthesis=data.grammar_synthesis,
            reading_word_in_context=data.reading_word_in_context,
            reading_text_structure=data.reading_text_structure,
            reading_cross_text=data.reading_cross_text,
            reading_central_ideas=data.reading_central_ideas,
            reading_inferences=data.reading_inferences,
            passages_literary=data.passages_literary,
            passages_social_science=data.passages_social_science,
            passages_humanities=data.passages_humanities,
            passages_science=data.passages_science,
            passages_poetry=data.passages_poetry,
            math_topics=data.math_topics,
            # IELTS fields
            ielts_target_date=data.ielts_target_date,
            ielts_planned_test_date=data.ielts_planned_test_date,
            ielts_result_score=data.ielts_result_score,
            ielts_result_test_date=data.ielts_result_test_date,
            ielts_last_date_prompted_at=data.ielts_last_date_prompted_at,
            has_passed_ielts_before=data.has_passed_ielts_before,
            previous_ielts_score=data.previous_ielts_score,
            ielts_target_score=data.ielts_target_score,
            ielts_listening_main_idea=data.ielts_listening_main_idea,
            ielts_listening_details=data.ielts_listening_details,
            ielts_listening_opinion=data.ielts_listening_opinion,
            ielts_listening_accents=data.ielts_listening_accents,
            ielts_reading_skimming=data.ielts_reading_skimming,
            ielts_reading_scanning=data.ielts_reading_scanning,
            ielts_reading_vocabulary=data.ielts_reading_vocabulary,
            ielts_reading_inference=data.ielts_reading_inference,
            ielts_reading_matching=data.ielts_reading_matching,
            ielts_writing_task1_graphs=data.ielts_writing_task1_graphs,
            ielts_writing_task1_process=data.ielts_writing_task1_process,
            ielts_writing_task2_structure=data.ielts_writing_task2_structure,
            ielts_writing_task2_arguments=data.ielts_writing_task2_arguments,
            ielts_writing_grammar=data.ielts_writing_grammar,
            ielts_writing_vocabulary=data.ielts_writing_vocabulary,
            ielts_speaking_fluency=data.ielts_speaking_fluency,
            ielts_speaking_vocabulary=data.ielts_speaking_vocabulary,
            ielts_speaking_grammar=data.ielts_speaking_grammar,
            ielts_speaking_pronunciation=data.ielts_speaking_pronunciation,
            ielts_speaking_part2=data.ielts_speaking_part2,
            ielts_speaking_part3=data.ielts_speaking_part3,
            ielts_weak_topics=data.ielts_weak_topics,
            additional_comments=data.additional_comments,
            # Mark as submitted
            is_draft=False
        )
        db.add(submission)
    
    # Update user's assignment_zero_completed status
    current_user.assignment_zero_completed = True
    current_user.assignment_zero_completed_at = datetime.utcnow()
    
    db.commit()
    db.refresh(submission)
    
    return AssignmentZeroSubmissionSchema.model_validate(submission)


@router.patch("/planned-date", response_model=AssignmentZeroSubmissionSchema)
async def update_planned_exam_date(
    data: AssignmentZeroPlannedDateUpdateSchema,
    current_user: UserInDB = Depends(get_current_user_dependency),
    db: Session = Depends(get_db)
):
    if current_user.role != "student":
        raise HTTPException(status_code=403, detail="Only students can update planned exam dates")
    _forbid_special_group_student_assignment_zero(current_user, db)

    submission = db.query(AssignmentZeroSubmission).filter(
        AssignmentZeroSubmission.user_id == current_user.id
    ).first()
    if not submission:
        raise HTTPException(status_code=404, detail="Assignment Zero submission not found")

    exam_type = _normalize_exam_type(data.exam_type)
    if exam_type == "sat":
        submission.sat_planned_test_date = data.planned_test_date
        submission.sat_target_date = format_sat_label(data.planned_test_date)
    else:
        submission.ielts_planned_test_date = data.planned_test_date
        submission.ielts_target_date = data.planned_test_date.isoformat()

    submission.updated_at = _utc_now()
    db.commit()
    db.refresh(submission)
    return AssignmentZeroSubmissionSchema.model_validate(submission)


@router.patch("/exam-result", response_model=AssignmentZeroSubmissionSchema)
async def update_exam_result(
    data: AssignmentZeroExamResultUpdateSchema,
    current_user: UserInDB = Depends(get_current_user_dependency),
    db: Session = Depends(get_db)
):
    if current_user.role != "student":
        raise HTTPException(status_code=403, detail="Only students can update exam results")
    _forbid_special_group_student_assignment_zero(current_user, db)

    submission = db.query(AssignmentZeroSubmission).filter(
        AssignmentZeroSubmission.user_id == current_user.id
    ).first()
    if not submission:
        raise HTTPException(status_code=404, detail="Assignment Zero submission not found")

    exam_type = _normalize_exam_type(data.exam_type)
    result_score = data.result_score.strip()
    if result_score == "":
        raise HTTPException(status_code=400, detail="result_score cannot be empty")

    if exam_type == "sat":
        submission.sat_result_score = result_score
        submission.sat_result_test_date = data.result_test_date
    else:
        submission.ielts_result_score = result_score
        submission.ielts_result_test_date = data.result_test_date

    submission.updated_at = _utc_now()
    db.commit()
    db.refresh(submission)
    return AssignmentZeroSubmissionSchema.model_validate(submission)


@router.get("/ielts-date-prompt-status")
async def get_ielts_date_prompt_status(
    current_user: UserInDB = Depends(get_current_user_dependency),
    db: Session = Depends(get_db)
):
    if current_user.role != "student":
        return {"is_ielts_student": False, "should_prompt": False}

    if student_has_only_special_groups(current_user.id, db):
        return {"is_ielts_student": False, "should_prompt": False}

    submission = db.query(AssignmentZeroSubmission).filter(
        AssignmentZeroSubmission.user_id == current_user.id
    ).first()

    group_students = db.query(GroupStudent).filter(GroupStudent.student_id == current_user.id).all()
    group_ids = [gs.group_id for gs in group_students]
    group_names: list[str] = []
    if group_ids:
        groups = db.query(Group).filter(Group.id.in_(group_ids)).all()
        group_names = [group.name.lower() for group in groups if group.name]

    is_ielts_group_student = any("ielts" in name for name in group_names)
    has_ielts_data = bool(
        submission and (
            submission.ielts_target_date
            or submission.ielts_planned_test_date
            or submission.ielts_target_score
            or submission.has_passed_ielts_before
        )
    )
    is_ielts_student = is_ielts_group_student or has_ielts_data
    if not is_ielts_student:
        return {"is_ielts_student": False, "should_prompt": False}

    if submission is None:
        return {
            "is_ielts_student": True,
            "should_prompt": False,
            "reason": "assignment_zero_submission_required",
        }

    last_prompted = submission.ielts_last_date_prompted_at
    if last_prompted is None:
        return {"is_ielts_student": True, "should_prompt": True, "days_until_next_prompt": 0}

    now = _align_datetime_kind(last_prompted, _utc_now())
    next_prompt_at = last_prompted + timedelta(days=14)
    should_prompt = now >= next_prompt_at
    remaining_days = max((next_prompt_at.date() - now.date()).days, 0)
    return {
        "is_ielts_student": True,
        "should_prompt": should_prompt,
        "days_until_next_prompt": 0 if should_prompt else remaining_days,
        "last_prompted_at": last_prompted,
        "next_prompt_at": next_prompt_at,
    }


@router.post("/ielts-date-prompt-touch")
async def touch_ielts_date_prompt(
    current_user: UserInDB = Depends(get_current_user_dependency),
    db: Session = Depends(get_db)
):
    if current_user.role != "student":
        raise HTTPException(status_code=403, detail="Only students can update IELTS prompt state")
    _forbid_special_group_student_assignment_zero(current_user, db)

    submission = db.query(AssignmentZeroSubmission).filter(
        AssignmentZeroSubmission.user_id == current_user.id
    ).first()
    if not submission:
        return {"success": True, "ielts_last_date_prompted_at": None}

    submission.ielts_last_date_prompted_at = _utc_now()
    submission.updated_at = _utc_now()
    db.commit()
    return {"success": True, "ielts_last_date_prompted_at": submission.ielts_last_date_prompted_at}

@router.post("/upload-screenshot")
async def upload_screenshot(
    file: UploadFile = File(...),
    current_user: UserInDB = Depends(get_current_user_dependency),
    db: Session = Depends(get_db)
):
    """Upload screenshot for Assignment Zero"""
    if current_user.role != "student":
        raise HTTPException(status_code=403, detail="Only students can upload screenshots")
    _forbid_special_group_student_assignment_zero(current_user, db)

    contents = await file.read()
    if len(contents) > 10 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="File size must be less than 10MB")

    ct = (file.content_type or "").strip().lower()
    if ct == "image/jpg":
        ct = "image/jpeg"
    sniffed = _sniff_image_mime(contents)
    if ct not in _ALLOWED_IMAGE_MIMES and sniffed not in _ALLOWED_IMAGE_MIMES:
        raise HTTPException(status_code=400, detail="Only image files are allowed (JPEG, PNG, GIF, WEBP)")
    
    # Generate unique filename
    file_ext = os.path.splitext(file.filename)[1] if file.filename else ".png"
    unique_filename = f"{current_user.id}_{uuid.uuid4()}{file_ext}"

    # Save file
    file_url = storage_service.save(f"assignment_zero/{unique_filename}", contents, file.content_type)

    return {"url": file_url, "filename": unique_filename}

# =============================================================================
# ADMIN/TEACHER ENDPOINTS
# =============================================================================

@router.get("/submissions", response_model=list[AssignmentZeroSubmissionSchema])
async def get_all_submissions(
    group_name: str | None = None,
    skip: int = 0,
    limit: int = 100,
    current_user: UserInDB = Depends(get_current_user_dependency),
    db: Session = Depends(get_db)
):
    """Get all Assignment Zero submissions (admin/teacher/head roles)"""
    if current_user.role not in ["admin", "teacher", "head_curator", "head_teacher"]:
        raise HTTPException(status_code=403, detail="Access denied")
    
    query = db.query(AssignmentZeroSubmission)
    
    if group_name:
        query = query.filter(AssignmentZeroSubmission.group_name == group_name)
    
    submissions = query.offset(skip).limit(limit).all()
    
    return [AssignmentZeroSubmissionSchema.model_validate(s) for s in submissions]

@router.get("/submissions/{user_id}", response_model=AssignmentZeroSubmissionSchema)
async def get_submission_by_user(
    user_id: int,
    current_user: UserInDB = Depends(get_current_user_dependency),
    db: Session = Depends(get_db)
):
    """Get Assignment Zero submission for a specific user (admin/teacher/head roles)"""
    if current_user.role not in ["admin", "teacher", "head_curator", "head_teacher"]:
        raise HTTPException(status_code=403, detail="Access denied")
    
    submission = db.query(AssignmentZeroSubmission).filter(
        AssignmentZeroSubmission.user_id == user_id
    ).first()
    
    if not submission:
        raise HTTPException(status_code=404, detail="Submission not found")
    
    return AssignmentZeroSubmissionSchema.model_validate(submission)


@router.get("/curator/upcoming", response_model=list[CuratorUpcomingExamRow])
async def get_curator_upcoming_exam_results(
    days: int = 7,
    current_user: UserInDB = Depends(get_current_user_dependency),
    db: Session = Depends(get_db),
):
    """Curator view: upcoming/overdue exam-result collection rows for their students."""
    if current_user.role not in ["curator", "head_curator", "admin"]:
        raise HTTPException(status_code=403, detail="Access denied")
    if days < 1 or days > 60:
        raise HTTPException(status_code=400, detail="days must be between 1 and 60")

    now = datetime.utcnow().date()
    horizon = now + timedelta(days=days)

    # Gather students for this curator + their active group names (for accurate SAT/IELTS detection)
    student_groups_q = (
        db.query(GroupStudent.student_id, Group.name)
        .join(Group, Group.id == GroupStudent.group_id)
        .filter(Group.is_active == True)
    )
    if current_user.role == "curator":
        student_groups_q = student_groups_q.filter(Group.curator_id == current_user.id)

    student_groups_raw = student_groups_q.all()
    student_ids = sorted({r[0] for r in student_groups_raw})
    if not student_ids:
        return []

    group_names_by_student: dict[int, list[str]] = {}
    for student_id, group_name in student_groups_raw:
        group_names_by_student.setdefault(student_id, []).append(group_name or "")

    submissions = (
        db.query(AssignmentZeroSubmission)
        .filter(AssignmentZeroSubmission.user_id.in_(student_ids))
        .all()
    )

    rows: list[CuratorUpcomingExamRow] = []
    for s in submissions:
        group_names = group_names_by_student.get(s.user_id, [])
        normalized_group_names = [name.lower() for name in group_names if name]
        is_sat_student = any("sat" in name for name in normalized_group_names)
        is_ielts_student = any(("ielts" in name) or ("iealts" in name) for name in normalized_group_names)
        is_ielts_only = is_ielts_student and not is_sat_student

        preferred_group_name = next((name for name in group_names if name), None) or s.group_name

        # SAT row
        sat_planned = s.sat_planned_test_date
        if sat_planned and not is_ielts_only:
            ask = sat_planned + timedelta(days=13)
            sat_completed = bool(s.sat_result_score and s.sat_result_test_date)
            sat_status = "completed" if sat_completed else ("overdue" if ask < now else "pending")
            if sat_status != "completed" and ask > horizon:
                pass
            else:
                rows.append(CuratorUpcomingExamRow(
                    **{
                        **AssignmentZeroSubmissionSchema.model_validate(s).model_dump(),
                        "group_name": preferred_group_name or "",
                    },
                    exam_type="sat",
                    planned_test_date=sat_planned.isoformat(),
                    ask_result_on=ask.isoformat(),
                    status=sat_status,
                ))

        # IELTS row
        if s.ielts_planned_test_date:
            ask = s.ielts_planned_test_date + timedelta(days=13)
            i_completed = bool(s.ielts_result_score and s.ielts_result_test_date)
            i_status = "completed" if i_completed else ("overdue" if ask < now else "pending")
            if i_status != "completed" and ask > horizon:
                continue
            rows.append(CuratorUpcomingExamRow(
                **{
                    **AssignmentZeroSubmissionSchema.model_validate(s).model_dump(),
                    "group_name": preferred_group_name or "",
                },
                exam_type="ielts",
                planned_test_date=s.ielts_planned_test_date.isoformat(),
                ask_result_on=ask.isoformat(),
                status=i_status,
            ))

    return sorted(rows, key=lambda r: (r.ask_result_on or "9999-12-31", r.full_name))


@router.patch("/curator/planned-date", response_model=AssignmentZeroSubmissionSchema)
async def curator_update_planned_exam_date(
    user_id: int,
    data: AssignmentZeroPlannedDateUpdateSchema,
    current_user: UserInDB = Depends(get_current_user_dependency),
    db: Session = Depends(get_db),
):
    if current_user.role not in ["curator", "head_curator", "admin"]:
        raise HTTPException(status_code=403, detail="Access denied")
    if current_user.role == "curator" and not _curator_can_access_student(db, curator_id=current_user.id, student_id=user_id):
        raise HTTPException(status_code=403, detail="Access denied")

    submission = db.query(AssignmentZeroSubmission).filter(AssignmentZeroSubmission.user_id == user_id).first()
    if not submission:
        raise HTTPException(status_code=404, detail="Assignment Zero submission not found")

    exam_type = _normalize_exam_type(data.exam_type)
    if exam_type == "sat":
        submission.sat_planned_test_date = data.planned_test_date
        submission.sat_target_date = format_sat_label(data.planned_test_date)
        # Clear result if rescheduled
        submission.sat_result_score = None
        submission.sat_result_test_date = None
    else:
        submission.ielts_planned_test_date = data.planned_test_date
        submission.ielts_target_date = data.planned_test_date.isoformat()
        submission.ielts_result_score = None
        submission.ielts_result_test_date = None

    submission.updated_at = _utc_now()
    db.commit()
    db.refresh(submission)
    return AssignmentZeroSubmissionSchema.model_validate(submission)


@router.patch("/curator/exam-result", response_model=AssignmentZeroSubmissionSchema)
async def curator_update_exam_result(
    user_id: int,
    data: AssignmentZeroExamResultUpdateSchema,
    current_user: UserInDB = Depends(get_current_user_dependency),
    db: Session = Depends(get_db),
):
    if current_user.role not in ["curator", "head_curator", "admin"]:
        raise HTTPException(status_code=403, detail="Access denied")
    if current_user.role == "curator" and not _curator_can_access_student(db, curator_id=current_user.id, student_id=user_id):
        raise HTTPException(status_code=403, detail="Access denied")

    submission = db.query(AssignmentZeroSubmission).filter(AssignmentZeroSubmission.user_id == user_id).first()
    if not submission:
        raise HTTPException(status_code=404, detail="Assignment Zero submission not found")

    exam_type = _normalize_exam_type(data.exam_type)
    result_score = data.result_score.strip()
    if result_score == "":
        raise HTTPException(status_code=400, detail="result_score cannot be empty")

    if exam_type == "sat":
        submission.sat_result_score = result_score
        submission.sat_result_test_date = data.result_test_date
    else:
        submission.ielts_result_score = result_score
        submission.ielts_result_test_date = data.result_test_date

    submission.updated_at = _utc_now()
    db.commit()
    db.refresh(submission)
    return AssignmentZeroSubmissionSchema.model_validate(submission)
