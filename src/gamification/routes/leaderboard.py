from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from sqlalchemy import func, desc, or_
from typing import List, Optional
from datetime import datetime, date, timedelta

from src.config import get_db
from src.schemas.models import (
    UserInDB, Group, GroupStudent, Assignment, AssignmentSubmission, Lesson, Module, Course,
    LeaderboardEntry, LeaderboardEntrySchema, LeaderboardEntryCreateSchema,
    GroupSchema, LessonSchedule, Attendance, AttendanceSchema, GroupAssignment,
    LeaderboardConfig, LeaderboardConfigSchema, LeaderboardConfigUpdateSchema,
    CourseGroupAccess, CourseHeadTeacher, Event, EventGroup, EventParticipant
)
from pydantic import BaseModel
from src.routes.auth import get_current_user_dependency
from src.services.attendance_service import (
    AttendanceService,
    attendance_status_to_ui,
    ep_status_to_attendance_status,
)

router = APIRouter()

@router.get("/curator/groups", response_model=List[GroupSchema])
async def get_curator_groups(
    current_user: UserInDB = Depends(get_current_user_dependency),
    db: Session = Depends(get_db)
):
    """Get groups managed by average curator"""
    if current_user.role == "admin" or current_user.role == "head_curator":
        groups = db.query(Group).filter(Group.is_special == False).all()
    elif current_user.role == "curator":
        groups = db.query(Group).filter(
            Group.curator_id == current_user.id,
            Group.is_special == False
        ).all()
    else:
        raise HTTPException(status_code=403, detail="Only curators and admins can access this endpoint")
    # We need to return GroupSchema. Since GroupSchema has many fields, we might need to populate them or use a simplified schema.
    # The frontend only uses id and name for the dropdown.
    # But for compatibility, let's use GroupSchema and fill basics.
    
    
    # 3. Calculate current_week for each group
    from src.schemas.models import Event, EventGroup, LessonSchedule
    
    result = []
    for group in groups:
        # Determine Week 1 Start (same logic as get_weekly_lessons_with_hw_status)
        first_event = db.query(Event).join(EventGroup).filter(
            EventGroup.group_id == group.id,
            Event.event_type == 'class',
            Event.is_active == True
        ).order_by(Event.start_datetime.asc()).first()
        
        start_of_week1 = None
        if first_event:
            start_of_week1 = first_event.start_datetime.date()
        else:
            first_sched = db.query(LessonSchedule).filter(
                LessonSchedule.group_id == group.id,
                LessonSchedule.is_active == True
            ).order_by(LessonSchedule.scheduled_at.asc()).first()
            if first_sched:
                start_of_week1 = first_sched.scheduled_at.date()
        
        current_week = 1
        max_week = 52
        if start_of_week1:
             # Align to Monday
            start_of_week1 = start_of_week1 - timedelta(days=start_of_week1.weekday())
            now_date = datetime.utcnow().date()
            
            # Calculate calendar week difference
            days_diff = (now_date - start_of_week1).days
            calendar_week = (days_diff // 7) + 1
            if calendar_week < 1: 
                calendar_week = 1
                
            # Calculate Max Content Week (Last scheduled event)
            last_event = db.query(Event).join(EventGroup).filter(
                EventGroup.group_id == group.id,
                Event.event_type == 'class',
                Event.is_active == True
            ).order_by(Event.start_datetime.desc()).first()
            
            last_date = None
            if last_event:
                last_date = last_event.start_datetime.date()
            else:
                last_sched = db.query(LessonSchedule).filter(
                    LessonSchedule.group_id == group.id,
                    LessonSchedule.is_active == True
                ).order_by(LessonSchedule.scheduled_at.desc()).first()
                if last_sched:
                    last_date = last_sched.scheduled_at.date()
            
            max_content_week = 1
            if last_date:
                 last_diff = (last_date - start_of_week1).days
                 max_content_week = (last_diff // 7) + 1
                 if max_content_week < 1: max_content_week = 1
            
            # 3.5 Also consider Course Length (Total lessons / 5)
            from src.schemas.models import CourseGroupAccess, Lesson, Module
            course_max_week = 1
            course_access = db.query(CourseGroupAccess).filter(
                CourseGroupAccess.group_id == group.id,
                CourseGroupAccess.is_active == True
            ).first()
            if course_access:
                lesson_count = db.query(func.count(Lesson.id)).join(Module).filter(
                    Module.course_id == course_access.course_id
                ).scalar()
                if lesson_count:
                    course_max_week = (lesson_count + 4) // 5 # Round up
            
            # Final max_week logic:
            # - Always show up to last scheduled event
            # - Always show up to course length
            # - If active, show up to current week
            potential_max = max(max_content_week, course_max_week)
            if group.is_active:
                potential_max = max(potential_max, calendar_week)
            
            current_week = min(calendar_week, potential_max)
            max_week = min(potential_max, 52) # Hard cap 52

        # Simplified population since we just need the list
        result.append(GroupSchema(
            id=group.id,
            name=group.name,
            description=group.description,
            teacher_id=group.teacher_id,
            teacher_name="", # Not critical for dropdown
            curator_id=group.curator_id,
            curator_name=current_user.name,
            student_count=0, # Not critical
            students=[],
            created_at=group.created_at,
            is_active=group.is_active,
            is_special=group.is_special,
            current_week=current_week,
            max_week=max_week
        ))
    return result

@router.get("/curator/leaderboard/{group_id}", response_model=List[dict])
async def get_group_leaderboard(
    group_id: int,
    week_number: int = Query(..., ge=1, le=52),
    current_user: UserInDB = Depends(get_current_user_dependency),
    db: Session = Depends(get_db)
):
    """
    Get leaderboard data for a specific group and week.
    Uses Events for schedule data (not LessonSchedule).
    """
    if current_user.role == "curator":
        group = db.query(Group).filter(Group.id == group_id, Group.curator_id == current_user.id).first()
        if not group:
            raise HTTPException(status_code=403, detail="Access denied to this group")
    elif current_user.role in ["admin", "head_curator"]:
        pass
    else:
        raise HTTPException(status_code=403, detail="Only curators and admins can access leaderboard")

    # 1. Get all students in the group
    group_students = db.query(GroupStudent).filter(GroupStudent.group_id == group_id).all()
    student_ids = [gs.student_id for gs in group_students]
    
    if not student_ids:
        return []
        
    students = db.query(UserInDB).filter(UserInDB.id.in_(student_ids)).all()
    students_map = {s.id: s for s in students}

    # 2. Get Events for this group (class type, active)
    from src.schemas.models import Event, EventGroup
    
    all_events = db.query(Event).join(EventGroup).filter(
        EventGroup.group_id == group_id,
        Event.event_type == 'class',
        Event.is_active == True
    ).order_by(Event.start_datetime.asc()).all()
    
    if not all_events:
        # No events - return empty leaderboard with students
        result = []
        for student_id in student_ids:
            student = students_map.get(student_id)
            if not student:
                continue
            row = {
                "student_id": student.id,
                "student_name": student.name,
                "avatar_url": student.avatar_url,
                "hw_lesson_1": None, "hw_lesson_2": None, "hw_lesson_3": None,
                "hw_lesson_4": None, "hw_lesson_5": None,
                "lesson_1": 0, "lesson_2": 0, "lesson_3": 0, "lesson_4": 0, "lesson_5": 0,
                "curator_hour": 0, "mock_exam": 0, "study_buddy": 0,
                "self_reflection_journal": 0, "weekly_evaluation": 0, "extra_points": 0,
            }
            result.append(row)
        result.sort(key=lambda x: x["student_name"])
        return result

    # 3. Calculate week boundaries based on first event
    first_event_date = all_events[0].start_datetime.date()
    start_of_week1 = first_event_date - timedelta(days=first_event_date.weekday())
    
    week_start_date = start_of_week1 + timedelta(weeks=week_number - 1)
    week_end_date = week_start_date + timedelta(days=7)
    week_start_dt = datetime.combine(week_start_date, datetime.min.time())
    week_end_dt = datetime.combine(week_end_date, datetime.min.time())
    
    # 4. Get events for this specific week
    week_events = [e for e in all_events 
                   if week_start_dt <= e.start_datetime < week_end_dt]
    week_events.sort(key=lambda e: e.start_datetime)
    
    # Map event_id -> lesson index (1-5) within the week
    event_to_index = {}
    for idx, event in enumerate(week_events[:5]):  # Max 5 lessons per week
        event_to_index[event.id] = idx + 1
    
    # 5. Get Homework data
    # Strategy: 
    #   a) First try to find assignments linked to events via event_id
    #   b) Fallback: find assignments for this group with due_date in this week
    homework_data = {}  # {student_id: {lesson_index: score}}
    
    event_ids = [e.id for e in week_events[:5]]
    
    # Method A: Assignments linked to events via event_id
    event_linked_assignments = []
    if event_ids:
        event_linked_assignments = db.query(Assignment).filter(
            Assignment.event_id.in_(event_ids),
            Assignment.is_active == True
        ).all()
    
    # Method B: Assignments for this group with due_date in this week (fallback)
    # This covers assignments created before event linking was implemented
    group_assignments = db.query(Assignment).filter(
        Assignment.group_id == group_id,
        Assignment.is_active == True,
        Assignment.due_date >= week_start_dt,
        Assignment.due_date < week_end_dt
    ).order_by(Assignment.due_date).all()
    
    # Combine and dedupe by assignment ID
    all_week_assignments = {a.id: a for a in event_linked_assignments}
    for a in group_assignments:
        if a.id not in all_week_assignments:
            all_week_assignments[a.id] = a
    
    assignments = list(all_week_assignments.values())
    assignments.sort(key=lambda a: a.due_date if a.due_date else datetime.max)
    
    # Create assignment -> lesson index mapping
    # Priority: event_id mapping, then by due_date order
    assignment_to_index = {}
    used_indices = set()
    
    # First, map assignments with event_id
    for a in assignments:
        if a.event_id and a.event_id in event_to_index:
            idx = event_to_index[a.event_id]
            assignment_to_index[a.id] = idx
            used_indices.add(idx)
    
    # Then, map remaining assignments by due_date order to unused indices
    next_idx = 1
    for a in assignments:
        if a.id not in assignment_to_index:
            while next_idx in used_indices and next_idx <= 5:
                next_idx += 1
            if next_idx <= 5:
                assignment_to_index[a.id] = next_idx
                used_indices.add(next_idx)
                next_idx += 1
    
    assignment_ids = list(assignment_to_index.keys())
    
    if assignment_ids:
        submissions = db.query(AssignmentSubmission).filter(
            AssignmentSubmission.assignment_id.in_(assignment_ids),
            AssignmentSubmission.user_id.in_(student_ids),
            AssignmentSubmission.is_graded == True
        ).all()
        
        for sub in submissions:
            lesson_idx = assignment_to_index.get(sub.assignment_id, 0)
            if lesson_idx > 0:
                if sub.user_id not in homework_data:
                    homework_data[sub.user_id] = {}
                homework_data[sub.user_id][lesson_idx] = sub.score
    
    # 6. Get Attendance data - from Attendance (single source of truth)
    attendance_data = {}  # {student_id: {lesson_index: score}}

    if event_ids:
        att_map = AttendanceService.get_attendance_map_for_events(db, event_ids, student_ids)
        for (uid, eid), att in att_map.items():
            lesson_idx = event_to_index.get(eid, 0)
            if lesson_idx > 0:
                if uid not in attendance_data:
                    attendance_data[uid] = {}
                score = 1 if att["status"] in ("present", "late") else 0
                attendance_data[uid][lesson_idx] = score

    # 7. Get Manual Leaderboard Entries
    entries = db.query(LeaderboardEntry).filter(
        LeaderboardEntry.group_id == group_id,
        LeaderboardEntry.week_number == week_number
    ).all()
    entries_map = {e.user_id: e for e in entries}

    # 8. FETCH SAT DATA
    from src.services.sat_service import SATService
    sat_results_map = {}
    
    emails = [s.email.lower() for s in students if s.email]
    if emails:
        batch_data = await SATService.fetch_batch_test_results(emails)
        results = batch_data.get("results", [])
        email_to_id = {s.email.lower(): s.id for s in students if s.email}
        for res in results:
            email = res.get("email", "").lower()
            sid = email_to_id.get(email)
            if sid and res.get("data"):
                pct = SATService.get_percentage_for_week(res["data"], week_start_dt, week_end_dt)
                if pct is not None:
                    sat_results_map[sid] = pct

    # 9. Construct Response
    result = []
    
    for student_id in student_ids:
        student = students_map.get(student_id)
        if not student: 
            continue
            
        entry = entries_map.get(student_id)
        hw_scores = homework_data.get(student_id, {})
        att_scores = attendance_data.get(student_id, {})
        
        # Priority for mock_exam: SAT Platform data > Manual Entry
        sat_score = sat_results_map.get(student_id)
        mock_exam_score = sat_score if sat_score is not None else (entry.mock_exam if entry else 0)

        # Manual scores defaults
        manual = {
            "curator_hour": entry.curator_hour if entry else 0,
            "mock_exam": mock_exam_score,
            "study_buddy": entry.study_buddy if entry else 0,
            "self_reflection_journal": entry.self_reflection_journal if entry else 0,
            "weekly_evaluation": entry.weekly_evaluation if entry else 0,
            "extra_points": entry.extra_points if entry else 0,
        }
        
        # Lesson scores from attendance
        lesson_scores = {}
        for i in range(1, 6):
            lesson_scores[f"lesson_{i}"] = att_scores.get(i, 0)
        
        row = {
            "student_id": student.id,
            "student_name": student.name,
            "avatar_url": student.avatar_url,
            "hw_lesson_1": hw_scores.get(1, None),
            "hw_lesson_2": hw_scores.get(2, None),
            "hw_lesson_3": hw_scores.get(3, None),
            "hw_lesson_4": hw_scores.get(4, None),
            "hw_lesson_5": hw_scores.get(5, None),
            **lesson_scores,
            **manual
        }
        result.append(row)

    # Sort by name
    result.sort(key=lambda x: x["student_name"])
    
    return result

@router.post("/config", response_model=LeaderboardConfigSchema)
async def update_leaderboard_config(
    payload: LeaderboardConfigUpdateSchema,
    current_user: UserInDB = Depends(get_current_user_dependency),
    db: Session = Depends(get_db)
):
    """Create or update leaderboard column visibility settings for a group/week"""
    import logging
    logger = logging.getLogger(__name__)
    
    # 1. Authorization
    if current_user.role == "curator":
        group = db.query(Group).filter(Group.id == payload.group_id, Group.curator_id == current_user.id).first()
        if not group:
            raise HTTPException(status_code=403, detail="Access denied to this group")
    elif current_user.role in ["admin", "head_curator"]:
        pass
    else:
        raise HTTPException(status_code=403, detail="Access denied")
    
    logger.warning(f"Received config update: {payload.model_dump()}")
    
    # 2. Get or create config
    config = db.query(LeaderboardConfig).filter(
        LeaderboardConfig.group_id == payload.group_id,
        LeaderboardConfig.week_number == payload.week_number
    ).first()
    
    if not config:
        logger.warning(f"Creating new config for group {payload.group_id}, week {payload.week_number}")
        config = LeaderboardConfig(
            group_id=payload.group_id,
            week_number=payload.week_number
        )
        db.add(config)
    else:
        logger.warning(f"Updating existing config ID {config.id}")
    
    # 3. Update fields
    update_data = payload.model_dump(exclude_unset=True)
    logger.warning(f"Update data (exclude_unset): {update_data}")
    
    for field, value in update_data.items():
        if field not in ["group_id", "week_number"] and hasattr(config, field):
            old_value = getattr(config, field)
            setattr(config, field, value)
            logger.warning(f"Updated {field}: {old_value} -> {value}")
            
    db.commit()
    db.refresh(config)
    
    logger.warning(f"Final config state: curator_hour_enabled={config.curator_hour_enabled}, study_buddy_enabled={config.study_buddy_enabled}")
    
    return config

@router.get("/curator/weekly-lessons/{group_id}")
async def get_weekly_lessons_with_hw_status(
    group_id: int,
    week_number: int = Query(..., ge=1, le=52),
    current_user: UserInDB = Depends(get_current_user_dependency),
    db: Session = Depends(get_db)
):
    """
    Get weekly lessons (Events) with homework status for leaderboards.
    Dynamically assumes the group's first event is Week 1.
    """
    # 1. Authorization
    # 1. Authorization
    if current_user.role == "curator":
        group = db.query(Group).filter(Group.id == group_id, Group.curator_id == current_user.id).first()
        if not group:
            raise HTTPException(status_code=403, detail="Access denied to this group")
    elif current_user.role in ["admin", "head_curator"]:
        pass
    else:
        raise HTTPException(status_code=403, detail="Access denied")

    # 1.5 Get Leaderboard Config
    config = db.query(LeaderboardConfig).filter(
        LeaderboardConfig.group_id == group_id,
        LeaderboardConfig.week_number == week_number
    ).first()
    
    # Default config if not exists
    if not config:
        config_data = {
            "curator_hour_enabled": True,
            "curator_hour_date": None,
            "study_buddy_enabled": True,
            "self_reflection_journal_enabled": True,
            "weekly_evaluation_enabled": True,
            "extra_points_enabled": True
        }
    else:
        config_data = {
            "curator_hour_enabled": config.curator_hour_enabled,
            "curator_hour_date": config.curator_hour_date,
            "study_buddy_enabled": config.study_buddy_enabled,
            "self_reflection_journal_enabled": config.self_reflection_journal_enabled,
            "weekly_evaluation_enabled": config.weekly_evaluation_enabled,
            "extra_points_enabled": config.extra_points_enabled
        }

    # 2. Determine Week Start Date based on first event OR first schedule
    # Logic: Find earliest event for this group -> that is start of Week 1
    # Week N start = Earliest + (N-1)*7 days
    from src.schemas.models import Event, EventGroup, LessonSchedule
    
    first_event = db.query(Event).join(EventGroup).filter(
        EventGroup.group_id == group_id,
        Event.event_type == 'class',
        Event.is_active == True
    ).order_by(Event.start_datetime.asc()).first()
    
    week_start_date = None
    week_end_date = None
    events = []
    mode = "event"
    seen_times = set()

    # Determine Week 1 Start
    start_of_week1 = None
    if first_event:
        start_of_week1 = first_event.start_datetime.date()
    else:
        first_sched_any = db.query(LessonSchedule).filter(
            LessonSchedule.group_id == group_id,
            LessonSchedule.is_active == True
        ).order_by(LessonSchedule.scheduled_at.asc()).first()
        if first_sched_any:
            start_of_week1 = first_sched_any.scheduled_at.date()
            
    if start_of_week1:
        # Align to Monday
        start_of_week1 = start_of_week1 - timedelta(days=start_of_week1.weekday())
        week_start_date = start_of_week1 + timedelta(weeks=week_number - 1)
        week_end_date = week_start_date + timedelta(days=7)
    
        # 3. Get Events for this week
        from src.services.event_service import EventService
        from src.schemas.models import CourseGroupAccess, EventCourse
        
        course_accesses = db.query(CourseGroupAccess).filter(
            CourseGroupAccess.group_id == group_id,
            CourseGroupAccess.is_active == True
        ).all()
        course_ids = [ca.course_id for ca in course_accesses]
        
        week_end_dt = datetime.combine(week_end_date, datetime.min.time())
        week_start_dt = datetime.combine(week_start_date, datetime.min.time())
        
        # Standard events
        standard_events = db.query(Event).outerjoin(EventGroup).outerjoin(EventCourse).filter(
            Event.event_type == 'class',
            Event.is_active == True,
            Event.start_datetime >= week_start_dt,
            Event.start_datetime < week_end_dt,
            Event.is_recurring == False,
            or_(
                EventGroup.group_id == group_id,
                EventCourse.course_id.in_(course_ids)
            )
        ).distinct().order_by(Event.start_datetime).all()
        
        # Recurring events
        recurring_instances = EventService.expand_recurring_events(
            db=db,
            start_date=week_start_dt,
            end_date=week_end_dt - timedelta(seconds=1),
            group_ids=[group_id],
            course_ids=course_ids
        )
        recurring_instances = [e for e in recurring_instances if e.event_type == 'class']
        
        for e in standard_events:
            time_sig = e.start_datetime.replace(second=0, microsecond=0)
            if time_sig not in seen_times:
                e.is_pseudo = False
                events.append(e)
                seen_times.add(time_sig)
                
        for instance in recurring_instances:
            time_sig = instance.start_datetime.replace(second=0, microsecond=0)
            if time_sig not in seen_times:
                instance.is_pseudo = False
                events.append(instance)
                seen_times.add(time_sig)

    events.sort(key=lambda x: x.start_datetime)
                
    if not events:
         return {"week_number": week_number, "week_start": week_start_date or datetime.utcnow(), "lessons": [], "students": []}
         
    if not week_start_date:
         week_start_date = datetime.utcnow() # Warning: Should not happen if events exist
    
    # 4. Get Assignments linked to lessons by lesson_number
    lesson_homework_map = {}  # lesson_number -> assignment
    
    # Query assignments by group_id and lesson_number
    assignments = db.query(Assignment).filter(
        Assignment.group_id == group_id,
        Assignment.lesson_number.isnot(None),
        Assignment.is_active == True
    ).all()
    
    # Build lesson_number -> assignment map
    for a in assignments:
        if a.lesson_number:
            lesson_homework_map[a.lesson_number] = a

    # Collect final assignment IDs for submission lookup
    assignment_ids = list(set([a.id for a in lesson_homework_map.values()])) if lesson_homework_map else []
    
    # 4.5. Calculate GLOBAL lesson_number for each event in this week
    # Get ALL events for this group to calculate correct lesson_number
    all_group_events = db.query(Event).outerjoin(EventGroup).filter(
        Event.event_type == 'class',
        Event.is_active == True,
        EventGroup.group_id == group_id
    ).order_by(Event.start_datetime.asc()).all()
    
    # Build event_id -> global lesson_number map
    event_to_lesson_number = {}
    for idx, e in enumerate(all_group_events):
        event_to_lesson_number[e.id] = idx + 1
    
    # 5. Build Lesson Columns metadata
    lessons_meta = []
    for idx, event in enumerate(events):
        # Use GLOBAL lesson_number, not local week index
        lesson_num = event_to_lesson_number.get(event.id, idx + 1)
        hw = lesson_homework_map.get(lesson_num)
        lessons_meta.append({
            "lesson_number": lesson_num,
            "event_id": event.id,
            "title": event.title,
            "start_datetime": event.start_datetime,
            "homework": {
                "id": hw.id,
                "title": hw.title
            } if hw else None
        })
        
    # 6. Get Students
    group_students = db.query(GroupStudent).filter(GroupStudent.group_id == group_id).all()
    student_ids = [gs.student_id for gs in group_students]
    
    if not student_ids:
        return {
            "week_number": week_number, 
            "week_start": week_start_date, 
            "lessons": lessons_meta, 
            "students": [],
            "config": config_data
        }
        
    students = db.query(UserInDB).filter(UserInDB.id.in_(student_ids)).all()
    students_list = sorted(students, key=lambda s: s.name or "")
    
    # 7. Get Attendance — from Attendance (single source of truth)
    event_ids = [e.id for e in events if hasattr(e, 'id') and e.id]
    attendance_map = {}  # (user_id, event_id) -> status str
    if event_ids:
        raw_map = AttendanceService.get_attendance_map_for_events(db, event_ids, student_ids)
        for (uid, eid), att in raw_map.items():
            attendance_map[(uid, eid)] = attendance_status_to_ui(att["status"])

    
    # 8. Get HW Submissions
    submission_map = {}
    if assignment_ids:
        submissions = db.query(AssignmentSubmission).filter(
            AssignmentSubmission.assignment_id.in_(assignment_ids),
            AssignmentSubmission.user_id.in_(student_ids)
        ).all()
        # Map (user_id, assignment_id) -> submission
        submission_map = {(s.user_id, s.assignment_id): s for s in submissions}
    
    # 9. Get Manual Leaderboard Entries
    manual_entries = db.query(LeaderboardEntry).filter(
        LeaderboardEntry.group_id == group_id,
        LeaderboardEntry.week_number == week_number
    ).all()
    manual_map = {e.user_id: e for e in manual_entries}

    # 9.5 FETCH SAT DATA
    from src.services.sat_service import SATService
    sat_results_map = {} # user_id -> combinedScore
    
    if student_ids and week_start_date and week_end_date:
        emails = [s.email.lower() for s in students_list if s.email]
        if emails:
            # Fetch all results for these emails
            batch_data = await SATService.fetch_batch_test_results(emails)
            results = batch_data.get("results", [])
            
            # Map email -> data
            email_to_id = {s.email.lower(): s.id for s in students_list if s.email}
            
            # Convert dates to datetime for get_score_for_week
            w_start = datetime.combine(week_start_date, datetime.min.time())
            w_end = datetime.combine(week_end_date, datetime.min.time())
            
            for res in results:
                email = res.get("email", "").lower()
                student_id = email_to_id.get(email)
                if student_id and res.get("data"):
                    pct = SATService.get_percentage_for_week(res["data"], w_start, w_end)
                    if pct is not None:
                        sat_results_map[student_id] = pct

    # 10. Build Student Rows
    student_rows = []
    for student in students_list:
        # Get manual entry
        manual = manual_map.get(student.id)
        
        # Priority for mock_exam: SAT Platform data for this week > Manual Entry
        sat_score = sat_results_map.get(student.id)
        mock_exam_score = sat_score if sat_score is not None else (manual.mock_exam if manual else 0)

        manual_data = {
            "curator_hour": manual.curator_hour if manual else 0,
            "mock_exam": mock_exam_score,
            "study_buddy": manual.study_buddy if manual else 0,
            "self_reflection_journal": manual.self_reflection_journal if manual else 0,
            "weekly_evaluation": manual.weekly_evaluation if manual else 0,
            "extra_points": manual.extra_points if manual else 0,
        }

        lesson_data = {}
        for idx, event in enumerate(events):
            # Attendance
            # Default to "registered" if event exists? No, default absent if not in participant table?
            # Actually, EventParticipant is usually created when they register/attend.
            # If nothing, assumption: missed.
            status = attendance_map.get((student.id, event.id), "missed") 
            
            # Homework - now by GLOBAL lesson_number
            lesson_num = event_to_lesson_number.get(event.id, idx + 1)
            hw = lesson_homework_map.get(lesson_num)
            hw_status = None
            if hw:
                sub = submission_map.get((student.id, hw.id))
                if sub:
                    hw_status = {
                        "submitted": True,
                        "score": sub.score,
                        "max_score": sub.max_score,
                        "is_graded": sub.is_graded,
                        "submission_id": sub.id
                    }
                else:
                    hw_status = {"submitted": False, "score": None}
            
            # Use GLOBAL lesson_number as key to match lessons_meta
            lesson_data[str(lesson_num)] = {
                "event_id": event.id,
                "attendance_status": status,
                "homework_status": hw_status
            }
            
        student_rows.append({
            "student_id": student.id,
            "student_name": student.name,
            "avatar_url": student.avatar_url,
            "lessons": lesson_data,
            **manual_data
        })
        
    return {
        "week_number": week_number,
        "week_start": week_start_date,
        "lessons": lessons_meta,
        "students": student_rows,
        "config": config_data
    }

@router.get("/curator/full-attendance/{group_id}")
async def get_group_full_attendance_matrix(
    group_id: int,
    current_user: UserInDB = Depends(get_current_user_dependency),
    db: Session = Depends(get_db)
):
    """
    Get full attendance matrix for a group (all lessons).
    Handles standard events and expanded recurring schedules.
    """
    from src.schemas.models import Event, EventGroup, EventParticipant, LessonSchedule, EventCourse

    # 1. Authorization & Group Info
    group_obj = db.query(Group).filter(Group.id == group_id).first()
    if not group_obj:
        raise HTTPException(status_code=404, detail="Group not found")

    if current_user.role == "curator":
        if group_obj.curator_id != current_user.id:
            raise HTTPException(status_code=403, detail="Access denied to this group (Curator mismatch)")
    elif current_user.role == "teacher":
        if group_obj.teacher_id != current_user.id:
            raise HTTPException(status_code=403, detail="Access denied to this group (Teacher mismatch)")
    elif current_user.role == "head_teacher":
        course_accesses = db.query(CourseGroupAccess).filter(
            CourseGroupAccess.group_id == group_id,
            CourseGroupAccess.is_active == True
        ).all()
        course_ids = [ca.course_id for ca in course_accesses]
        if not course_ids:
            raise HTTPException(status_code=403, detail="Group not linked to any course")
        head_teacher_access = db.query(CourseHeadTeacher).filter(
            CourseHeadTeacher.course_id.in_(course_ids),
            CourseHeadTeacher.head_teacher_id == current_user.id
        ).first()
        if not head_teacher_access:
            raise HTTPException(status_code=403, detail="Access denied to this group")
    elif current_user.role == "admin":
        pass
    else:
        raise HTTPException(status_code=403, detail="Access denied")

    # 2. Get Group Creation Date and Linked Courses
    creation_date = group_obj.created_at if group_obj else datetime.utcnow() - timedelta(days=90)
    
    # Find courses linked to this group
    course_accesses = db.query(CourseGroupAccess).filter(CourseGroupAccess.group_id == group_id, CourseGroupAccess.is_active == True).all()
    course_ids = [ca.course_id for ca in course_accesses]
    
    # 3. Fetch Standard Events (Group-linked OR Course-linked)
    standard_events_query = db.query(Event).outerjoin(EventGroup).outerjoin(EventCourse).filter(
        Event.event_type == 'class',
        Event.is_active == True,
        Event.is_recurring == False,
        or_(
            EventGroup.group_id == group_id,
            EventCourse.course_id.in_(course_ids)
        )
    ).distinct()
    standard_events = standard_events_query.order_by(Event.start_datetime.asc()).all()

    # 4. Expand Recurring Events (Group-linked OR Course-linked)
    from src.services.event_service import EventService
    
    start_search = datetime.utcnow() - timedelta(days=365)
    end_search = datetime.utcnow() + timedelta(days=365)
    
    recurring_instances = EventService.expand_recurring_events(
        db=db,
        start_date=start_search,
        end_date=end_search,
        group_ids=[group_id],
        course_ids=course_ids
    )
    
    recurring_instances = [e for e in recurring_instances if e.event_type == 'class']

    # 5. Combine and Deduplicate
    combined_events = []
    seen_times = set()
    
    # Process standard events
    for e in standard_events:
        time_sig = e.start_datetime.replace(second=0, microsecond=0)
        if time_sig not in seen_times:
            combined_events.append(e)
            seen_times.add(time_sig)
            
    # Process recurring instances
    for instance in recurring_instances:
        time_sig = instance.start_datetime.replace(second=0, microsecond=0)
        if time_sig not in seen_times:
            combined_events.append(instance)
            seen_times.add(time_sig)
            
    all_events = combined_events
    all_events.sort(key=lambda x: x.start_datetime)

    # 6. Build Lessons Meta
    lessons_meta = []
    for idx, event in enumerate(all_events):
        lessons_meta.append({
            "lesson_number": idx + 1,
            "event_id": event.id,
            "title": event.title,
            "start_datetime": event.start_datetime
        })

    if not all_events:
         return {"lessons": [], "students": []}

    event_ids = [e.id for e in all_events]
    
    # 7. Get Students (only role=student - teachers must not appear in attendance)
    group_students = db.query(GroupStudent).filter(GroupStudent.group_id == group_id).all()
    student_ids = [gs.student_id for gs in group_students]
    
    if not student_ids:
        return {"lessons": lessons_meta, "students": []}
        
    students = db.query(UserInDB).filter(
        UserInDB.id.in_(student_ids),
        UserInDB.role == "student"
    ).all()
    students_list = sorted(students, key=lambda s: s.name or "")

    # 8. Get Attendance — from Attendance (single source of truth)
    attendance_map = AttendanceService.get_attendance_map_for_events(db, event_ids, student_ids)

    # 10. Build Student Rows
    student_rows = []
    for student in students_list:
        lesson_data = {}
        for idx, event in enumerate(all_events):
            att_data = attendance_map.get((student.id, event.id))
            status = attendance_status_to_ui(att_data["status"] if att_data else None)
            activity_score = att_data["activity_score"] if att_data else None
            lesson_data[str(idx + 1)] = {
                "event_id": event.id,
                "attendance_status": status,
                "activity_score": activity_score
            }
            
        student_rows.append({
            "student_id": student.id,
            "student_name": student.name,
            "avatar_url": student.avatar_url,
            "lessons": lesson_data
        })
        
    return {
        "lessons": lessons_meta,
        "students": student_rows
    }

@router.post("/curator/leaderboard")
async def update_leaderboard_entry(
    data: LeaderboardEntryCreateSchema,
    current_user: UserInDB = Depends(get_current_user_dependency),
    db: Session = Depends(get_db)
):
    """
    Update or create a manual leaderboard entry.
    """
    import logging
    logger = logging.getLogger(__name__)
    logger.warning(f"Received leaderboard entry update: {data.dict()}")
    
    if current_user.role == "curator":
         group = db.query(Group).filter(
             Group.id == data.group_id, 
             Group.curator_id == current_user.id
         ).first()
         if not group:
             logger.warning(f"Access denied: curator {current_user.id} not owner of group {data.group_id}")
             raise HTTPException(status_code=403, detail="Access denied to this group")
    elif current_user.role in ["admin", "head_curator"]:
        pass
    else:
        raise HTTPException(status_code=403, detail="Access denied")

    # Check existence
    entry = db.query(LeaderboardEntry).filter(
        LeaderboardEntry.user_id == data.user_id,
        LeaderboardEntry.group_id == data.group_id,
        LeaderboardEntry.week_number == data.week_number
    ).first()
    
    if entry:
        # Update existing fields if provided
        for field, value in data.dict(exclude_unset=True).items():
            if field not in ['user_id', 'group_id', 'week_number']:
                setattr(entry, field, value)
    else:
        # Create new
        entry = LeaderboardEntry(**data.dict())
        db.add(entry)
    
    db.commit()
    db.refresh(entry)
    return entry


@router.get("/curator/leaderboard-full/{group_id}")
async def get_weekly_lessons_with_hw_status(
    group_id: int,
    week_number: int = Query(..., ge=1, le=52),
    current_user: UserInDB = Depends(get_current_user_dependency),
    db: Session = Depends(get_db)
):
    """
    Enhanced leaderboard endpoint returning structured lessons, homework status, 
    student rows and configuration. Uses Events (not LessonSchedule).
    """
    if current_user.role not in ["curator", "admin", "head_curator"]:
        raise HTTPException(status_code=403, detail="Not authorized")

    # 1. Get Group
    group = db.query(Group).filter(Group.id == group_id).first()
    if not group:
        raise HTTPException(status_code=404, detail="Group not found")

    # 2. Get all Events for this group
    all_events = db.query(Event).join(EventGroup).filter(
        EventGroup.group_id == group_id,
        Event.event_type == 'class',
        Event.is_active == True
    ).order_by(Event.start_datetime.asc()).all()
    
    lessons_meta = []
    week_start = None
    week_events = []
    
    if all_events:
        # Calculate week boundaries based on first event
        first_event_date = all_events[0].start_datetime.date()
        start_of_week1 = first_event_date - timedelta(days=first_event_date.weekday())
        
        week_start_date = start_of_week1 + timedelta(weeks=week_number - 1)
        week_end_date = week_start_date + timedelta(days=7)
        week_start_dt = datetime.combine(week_start_date, datetime.min.time())
        week_end_dt = datetime.combine(week_end_date, datetime.min.time())
        week_start = week_start_dt
        
        # Get events for this specific week
        week_events = [e for e in all_events 
                       if week_start_dt <= e.start_datetime < week_end_dt]
        week_events.sort(key=lambda e: e.start_datetime)
        
        # Get homework for this week (by group_id and due_date)
        week_assignments = db.query(Assignment).filter(
            Assignment.group_id == group_id,
            Assignment.is_active == True,
            Assignment.due_date >= week_start_dt,
            Assignment.due_date < week_end_dt
        ).order_by(Assignment.due_date).all()
        
        # Also check assignments linked to events via event_id
        event_ids = [e.id for e in week_events]
        if event_ids:
            event_linked_assignments = db.query(Assignment).filter(
                Assignment.event_id.in_(event_ids),
                Assignment.is_active == True
            ).all()
            # Merge
            existing_ids = {a.id for a in week_assignments}
            for a in event_linked_assignments:
                if a.id not in existing_ids:
                    week_assignments.append(a)
            week_assignments.sort(key=lambda a: a.due_date if a.due_date else datetime.max)
        
        # Create assignment lookup by event_id or by index
        assignment_by_event = {a.event_id: a for a in week_assignments if a.event_id}
        assignments_without_event = [a for a in week_assignments if not a.event_id]
        
        for idx, event in enumerate(week_events[:5]):
            # Find homework for this lesson
            # Priority: assignment linked to this event, then by order
            assignment = assignment_by_event.get(event.id)
            if not assignment and idx < len(assignments_without_event):
                assignment = assignments_without_event[idx]
            
            lessons_meta.append({
                "lesson_number": idx + 1,
                "event_id": event.id,
                "title": event.title,
                "start_datetime": event.start_datetime.isoformat(),
                "homework": {
                    "id": assignment.id,
                    "title": assignment.title
                } if assignment else None
            })
    else:
        # No events - use group creation date as fallback
        group_base_date = group.created_at or datetime.utcnow()
        week1_start = group_base_date - timedelta(days=group_base_date.weekday())
        week_start = week1_start + timedelta(weeks=week_number - 1)

    # 2. Get Student Rows (re-use existing function)
    students_data = await get_group_leaderboard(group_id, week_number, current_user, db)
    
    # 3. Format students for the frontend expected structure
    formatted_students = []
    for row in students_data:
        student_lessons = {}
        
        for i in range(1, 6):
            hw_score = row.get(f"hw_lesson_{i}")
            att_score = row.get(f"lesson_{i}", 0)
            
            # Determine attendance status
            if att_score >= 1:
                attendance_status = "attended"
            elif att_score > 0:
                attendance_status = "late"
            else:
                attendance_status = "missed"
            
            # Get event_id for this lesson
            event_id = 0
            if i <= len(week_events):
                event_id = week_events[i-1].id

            student_lessons[str(i)] = {
                "event_id": event_id,
                "attendance_status": attendance_status,
                "homework_status": {
                    "submitted": hw_score is not None,
                    "score": hw_score
                } if hw_score is not None else None
            }

        formatted_students.append({
            "student_id": row["student_id"],
            "student_name": row["student_name"],
            "avatar_url": row["avatar_url"],
            "lessons": student_lessons,
            "curator_hour": row["curator_hour"],
            "mock_exam": row["mock_exam"],
            "study_buddy": row["study_buddy"],
            "self_reflection_journal": row["self_reflection_journal"],
            "weekly_evaluation": row["weekly_evaluation"],
            "extra_points": row["extra_points"]
        })

    # 4. Get/Create Config
    config = db.query(LeaderboardConfig).filter(
        LeaderboardConfig.group_id == group_id,
        LeaderboardConfig.week_number == week_number
    ).first()
    
    if not config:
        config = LeaderboardConfig(
            group_id=group_id,
            week_number=week_number
        )
        db.add(config)
        db.commit()
        db.refresh(config)

    return {
        "week_number": week_number,
        "week_start": week_start.isoformat(),
        "lessons": lessons_meta,
        "students": formatted_students,
        "config": {
            "curator_hour_enabled": config.curator_hour_enabled,
            "study_buddy_enabled": config.study_buddy_enabled,
            "self_reflection_journal_enabled": config.self_reflection_journal_enabled,
            "weekly_evaluation_enabled": config.weekly_evaluation_enabled,
            "extra_points_enabled": config.extra_points_enabled,
            "curator_hour_date": config.curator_hour_date
        }
    }


@router.post("/curator/leaderboard-config")
async def update_leaderboard_config(
    data: LeaderboardConfigUpdateSchema,
    current_user: UserInDB = Depends(get_current_user_dependency),
    db: Session = Depends(get_db)
):
    """Update or create leaderboard configuration."""
    if current_user.role not in ["curator", "admin", "head_curator"]:
        raise HTTPException(status_code=403, detail="Not authorized")

    config = db.query(LeaderboardConfig).filter(
        LeaderboardConfig.group_id == data.group_id,
        LeaderboardConfig.week_number == data.week_number
    ).first()

    if config:
        for field, value in data.dict(exclude_unset=True).items():
            if field not in ['group_id', 'week_number']:
                setattr(config, field, value)
    else:
        config = LeaderboardConfig(**data.dict())
        db.add(config)
    
    db.commit()
    db.refresh(config)
    return config

class AttendanceInputSchema(BaseModel):
    group_id: int
    week_number: int
    lesson_index: int # 1-5
    student_id: int
    score: int
    status: str = "present"
    event_id: Optional[int] = None
    activity_score: Optional[float] = None  # Activity score out of 10

class BulkAttendanceInputSchema(BaseModel):
    updates: List[AttendanceInputSchema]

@router.post("/curator/attendance/bulk")
async def update_attendance_bulk(
    data: BulkAttendanceInputSchema,
    current_user: UserInDB = Depends(get_current_user_dependency),
    db: Session = Depends(get_db)
):
    """
    Update multiple attendance records in a single transaction.
    Supports event-based updates (preferred) and legacy schedule-based updates.
    """
    if current_user.role not in ["curator", "admin", "teacher"]:
        raise HTTPException(status_code=403, detail="Access denied")

    from src.services.event_service import EventService

    updated_count = 0
    cached_groups = {} # id -> group_obj

    for item in data.updates:
        # Auth check
        if item.group_id not in cached_groups:
            group = db.query(Group).filter(Group.id == item.group_id).first()
            if not group: continue
            
            # Role-based restriction
            if current_user.role == "curator" and group.curator_id != current_user.id: continue
            if current_user.role == "teacher" and group.teacher_id != current_user.id: continue
            cached_groups[item.group_id] = group

        # Priority: event_id — write to Attendance (single source of truth)
        if item.event_id:
            real_event_id = EventService.resolve_event_id(db, item.event_id)
            if not real_event_id:
                continue
            score = 1 if item.status in ("attended", "late") else 0
            AttendanceService.upsert_for_event(
                db=db,
                event_id=real_event_id,
                user_id=item.student_id,
                status=ep_status_to_attendance_status(item.status),
                score=score,
                activity_score=item.activity_score,
            )
            updated_count += 1
            continue

        # Fallback: Schedule-based (Legacy)
        schedules = db.query(LessonSchedule).filter(
            LessonSchedule.group_id == item.group_id,
            LessonSchedule.week_number == item.week_number,
            LessonSchedule.is_active == True
        ).order_by(LessonSchedule.scheduled_at).all()

        if schedules and 0 < item.lesson_index <= len(schedules):
            target_schedule = schedules[item.lesson_index - 1]
            attendance = db.query(Attendance).filter(
                Attendance.lesson_schedule_id == target_schedule.id,
                Attendance.user_id == item.student_id
            ).first()

            if attendance:
                attendance.score = item.score
                attendance.status = item.status
                if item.activity_score is not None:
                    attendance.activity_score = item.activity_score
            else:
                attendance = Attendance(
                    lesson_schedule_id=target_schedule.id,
                    user_id=item.student_id,
                    score=item.score,
                    status=item.status,
                    activity_score=item.activity_score
                )
                db.add(attendance)
            updated_count += 1

    db.commit()
    return {"status": "success", "updated_count": updated_count}

@router.post("/curator/attendance")
async def update_attendance(
    data: AttendanceInputSchema,
    current_user: UserInDB = Depends(get_current_user_dependency),
    db: Session = Depends(get_db)
):
    """
    Update attendance score for a specific scheduled lesson.
    If no schedule exists, it fails (or should we fallback to manual columns in LeaderboardEntry?).
    Decision: Support both?
    If schedule exists -> update Attendance model.
    If schedule does not exist -> update LeaderboardEntry model (legacy).
    """
    
    # Auth
    if current_user.role not in ["curator", "admin", "head_curator", "teacher"]:
        raise HTTPException(status_code=403, detail="Access denied")
        
    group = db.query(Group).filter(Group.id == data.group_id).first()
    if not group:
         raise HTTPException(status_code=404, detail="Group not found")
         
    if current_user.role == "curator" and group.curator_id != current_user.id:
         raise HTTPException(status_code=403, detail="Access denied to this group (Curator mismatch)")
         
    if current_user.role == "teacher" and group.teacher_id != current_user.id:
         raise HTTPException(status_code=403, detail="Access denied to this group (Teacher mismatch)")

    # Mode 1: Event-based — write to Attendance (single source of truth)
    if data.event_id:
        from src.services.event_service import EventService

        real_event_id = EventService.resolve_event_id(db, data.event_id)
        if not real_event_id:
            raise HTTPException(status_code=404, detail="Event could not be resolved/materialized")

        att_status = "present" if data.score > 0 else "absent"
        AttendanceService.upsert_for_event(
            db=db,
            event_id=real_event_id,
            user_id=data.student_id,
            status=att_status,
            score=data.score,
            activity_score=data.activity_score,
        )
        db.commit()
        return {"status": "success", "mode": "event", "event_id": real_event_id}

    # Mode 2: Schedule-based (Legacy/Generated)
    schedules = db.query(LessonSchedule).filter(
        LessonSchedule.group_id == data.group_id,
        LessonSchedule.week_number == data.week_number,
        LessonSchedule.is_active == True
    ).order_by(LessonSchedule.scheduled_at).all()
    
    if schedules and 0 < data.lesson_index <= len(schedules):
        # Update Attendance Model
        target_schedule = schedules[data.lesson_index - 1]
        
        attendance = db.query(Attendance).filter(
            Attendance.lesson_schedule_id == target_schedule.id,
            Attendance.user_id == data.student_id
        ).first()
        
        if attendance:
            attendance.score = data.score
            attendance.status = data.status
            if data.activity_score is not None:
                attendance.activity_score = data.activity_score
        else:
            attendance = Attendance(
                lesson_schedule_id=target_schedule.id,
                user_id=data.student_id,
                score=data.score,
                status=data.status,
                activity_score=data.activity_score
            )
            db.add(attendance)
        db.commit()
        return {"status": "success", "mode": "schedule"}
        
    else:
        # Fallback to LeaderboardEntry (Legacy)
        # Check if 1 <= lesson_index <= 5
        if not (1 <= data.lesson_index <= 5):
             raise HTTPException(status_code=400, detail="Invalid lesson index")
             
        entry = db.query(LeaderboardEntry).filter(
            LeaderboardEntry.user_id == data.student_id,
            LeaderboardEntry.group_id == data.group_id,
            LeaderboardEntry.week_number == data.week_number
        ).first()
        
        if not entry:
            entry = LeaderboardEntry(
                user_id=data.student_id,
                group_id=data.group_id,
                week_number=data.week_number
            )
            db.add(entry)
            
        # Update specific column
        # lesson_1, lesson_2 ...
        col_name = f"lesson_{data.lesson_index}"
        setattr(entry, col_name, float(data.score)) # Ensure float for legacy compatibility
        
        db.commit()
        return {"status": "success", "mode": "legacy"}

class ScheduleItem(BaseModel):
    day_of_week: int # 0=Mon, ... 6=Sun
    time_of_day: str # "18:00"

class ScheduleGenerationSchema(BaseModel):
    group_id: int
    start_date: date
    schedule_items: List[ScheduleItem]
    weeks_count: int = 12
    lessons_count: Optional[int] = None

class GroupScheduleResponse(BaseModel):
    start_date: date
    weeks_count: int
    lessons_count: Optional[int] = None
    schedule_items: List[ScheduleItem]


@router.post("/curator/schedule/generate")
async def generate_schedule(
    data: ScheduleGenerationSchema,
    current_user: UserInDB = Depends(get_current_user_dependency),
    db: Session = Depends(get_db)
):
    """
    Generate lesson schedules for a group.
    Creates individual Event entries for each lesson (no more recurring events or LessonSchedule).
    Events are completely independent of course content.
    """
    if current_user.role not in ["curator", "admin", "head_curator", "teacher"]:
       raise HTTPException(status_code=403, detail="Access denied")
    
    group = db.query(Group).filter(Group.id == data.group_id).first()
    if not group:
        raise HTTPException(status_code=404, detail="Group not found")

    # Legacy LessonSchedules: deactivate (no attendance on these in current flow)
    existing_schedules = db.query(LessonSchedule).filter(
        LessonSchedule.group_id == data.group_id,
        LessonSchedule.is_active == True
    ).all()

    for old_sched in existing_schedules:
        old_sched.is_active = False
        old_assignments = db.query(GroupAssignment).filter(
            GroupAssignment.lesson_schedule_id == old_sched.id,
            GroupAssignment.is_active == True
        ).all()
        for oa in old_assignments:
            oa.is_active = False

    # Calculate number of weeks needed
    import math
    lessons_count = data.lessons_count if data.lessons_count else (len(data.schedule_items) * data.weeks_count)
    frequency = len(data.schedule_items)
    week_limit = math.ceil(lessons_count / frequency) + 2

    start_date = data.start_date
    KZ_OFFSET = timedelta(hours=5)

    all_lesson_dates = []
    for week in range(week_limit):
        for item in data.schedule_items:
            try:
                time_obj = datetime.strptime(item.time_of_day, "%H:%M").time()
            except ValueError:
                time_obj = datetime.strptime("19:00", "%H:%M").time()

            days_ahead = item.day_of_week - start_date.weekday()
            if days_ahead < 0:
                days_ahead += 7

            target_date = start_date + timedelta(days=days_ahead) + timedelta(weeks=week)
            target_dt_kz = datetime.combine(target_date, time_obj)
            target_dt_utc = target_dt_kz - KZ_OFFSET

            if target_date >= start_date:
                all_lesson_dates.append(target_dt_utc)

    all_lesson_dates.sort()
    all_lesson_dates = all_lesson_dates[:lessons_count]

    from datetime import timezone as _tz
    from src.services.schedule_reconciliation import reconcile_group_schedule

    dt_utc = lambda d: d.replace(tzinfo=_tz.utc) if d.tzinfo is None else d
    desired_slots = [(dt_utc(dt), ln) for ln, dt in enumerate(all_lesson_dates, start=1)]

    result = reconcile_group_schedule(
        db=db,
        group_id=data.group_id,
        desired_slots=desired_slots,
        group_name=group.name,
        teacher_id=group.teacher_id,
        created_by=current_user.id,
    )
    lessons_created = result["updated"] + result["created"]

    # Save config for future use
    group.schedule_config = {
        "start_date": data.start_date.isoformat(),
        "weeks_count": week_limit,
        "lessons_count": lessons_count,
        "schedule_items": [item.dict() for item in data.schedule_items]
    }
    db.commit()
    
    return {"message": f"Schedule generated successfully. Created {lessons_created} individual lessons."}

@router.get("/curator/schedule/{group_id}", response_model=GroupScheduleResponse)
async def get_group_schedule(
    group_id: int,
    current_user: UserInDB = Depends(get_current_user_dependency),
    db: Session = Depends(get_db)
):
    """
    Fetch existing recurring schedule for a group.
    """
    if current_user.role not in ["curator", "admin", "teacher"]:
        raise HTTPException(status_code=403, detail="Access denied")
        
    group = db.query(Group).filter(Group.id == group_id).first()
    if not group:
         raise HTTPException(status_code=404, detail="Group not found")
         
    if current_user.role == "teacher" and group.teacher_id != current_user.id:
         raise HTTPException(status_code=403, detail="Access denied to this group schedule")
        
    # Find active recurring events for this group
    # Try to return saved config first (more accurate)
    if group.schedule_config:
        config = group.schedule_config
        return {
            "start_date": config.get("start_date"),
            "weeks_count": config.get("weeks_count", 12),
            "lessons_count": config.get("lessons_count"),
            "schedule_items": config.get("schedule_items", [])
        }

    events = db.query(Event).join(EventGroup).filter(
        EventGroup.group_id == group_id,
        Event.event_type == 'class',
        Event.is_recurring == True,
        Event.is_active == True
    ).all()
    
    if not events:
        # Fallback to defaults or return empty
        return {
            "start_date": date.today(),
            "weeks_count": 12,
            "lessons_count": 48,
            "schedule_items": []
        }
        
    # Reconstruct schedule items
    schedule_items = []
    min_start_date = events[0].start_datetime.date()
    max_end_date = events[0].recurrence_end_date or date.today()
    
    for event in events:
        start_date_only = event.start_datetime.date()
        if start_date_only < min_start_date:
            min_start_date = start_date_only
        
        if event.recurrence_end_date and event.recurrence_end_date > max_end_date:
            max_end_date = event.recurrence_end_date
            
        schedule_items.append({
            "day_of_week": event.start_datetime.weekday(),
            "time_of_day": event.start_datetime.strftime("%H:%M")
        })
        
    # Calculate weeks count
    weeks_count = 12
    if min_start_date and max_end_date:
        days_diff = (max_end_date - min_start_date).days
        weeks_count = max(1, (days_diff // 7))
    
    return {
        "start_date": min_start_date,
        "weeks_count": weeks_count,
        "lessons_count": weeks_count * len(schedule_items),
        "schedule_items": schedule_items
    }


# ==================== STUDENT LEADERBOARD ====================

from src.schemas.models import StepProgress
from datetime import timedelta

@router.get("/student/my-ranking")
async def get_student_ranking(
    period: str = Query("all_time", regex="^(all_time|this_week|this_month)$"),
    current_user: UserInDB = Depends(get_current_user_dependency),
    db: Session = Depends(get_db)
):
    """
    Get leaderboard for current student's group.
    Shows ranking by completed steps and time spent.
    Perfect for competitive students who want to flex 💪
    """
    
    if current_user.role not in ['student', 'admin', 'head_curator']:
        raise HTTPException(status_code=403, detail="This endpoint is for students only")
    
    # Find user's group
    group_membership = db.query(GroupStudent).filter(
        GroupStudent.student_id == current_user.id
    ).first()
    
    group_id = None
    group_name = None
    student_ids = []
    
    if group_membership:
        group = db.query(Group).filter(Group.id == group_membership.group_id).first()
        if group:
            group_id = group.id
            group_name = group.name
            
            # Get all students in this group
            group_students = db.query(GroupStudent).filter(
                GroupStudent.group_id == group.id
            ).all()
            student_ids = [gs.student_id for gs in group_students]
    
    if not student_ids:
        # User is not in any group, show global leaderboard for students
        students = db.query(UserInDB).filter(
            UserInDB.role == 'student',
            UserInDB.is_active == True
        ).limit(100).all()
        student_ids = [s.id for s in students]
        group_name = "Global Rankings"
    
    # Calculate time filter
    time_filter = None
    now = datetime.utcnow()
    
    if period == "this_week":
        # Start of current week (Monday)
        start_of_week = now - timedelta(days=now.weekday())
        start_of_week = start_of_week.replace(hour=0, minute=0, second=0, microsecond=0)
        time_filter = start_of_week
    elif period == "this_month":
        start_of_month = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        time_filter = start_of_month
    
    # Build query for progress stats
    progress_query = db.query(
        StepProgress.user_id,
        func.count(StepProgress.id).label('steps_completed'),
        func.coalesce(func.sum(StepProgress.time_spent_minutes), 0).label('time_spent')
    ).filter(
        StepProgress.user_id.in_(student_ids),
        StepProgress.status == 'completed'
    )
    
    if time_filter:
        progress_query = progress_query.filter(StepProgress.completed_at >= time_filter)
    
    progress_stats = progress_query.group_by(StepProgress.user_id).all()
    
    # Create stats dictionary
    stats_dict = {
        stat.user_id: {
            'steps_completed': stat.steps_completed,
            'time_spent_minutes': int(stat.time_spent)
        }
        for stat in progress_stats
    }
    
    # Get all student info
    students = db.query(UserInDB).filter(UserInDB.id.in_(student_ids)).all()
    
    # Build leaderboard entries
    entries = []
    for student in students:
        stats = stats_dict.get(student.id, {'steps_completed': 0, 'time_spent_minutes': 0})
        entries.append({
            'user_id': student.id,
            'user_name': student.name or student.email.split('@')[0],
            'avatar_url': student.avatar_url,
            'steps_completed': stats['steps_completed'],
            'time_spent_minutes': stats['time_spent_minutes'],
            'is_current_user': student.id == current_user.id
        })
    
    # Sort by steps completed (primary), then by time spent (secondary)
    entries.sort(key=lambda x: (-x['steps_completed'], -x['time_spent_minutes']))
    
    # Add ranks and find current user
    leaderboard = []
    current_user_rank = 0
    current_user_entry = None
    
    for i, entry in enumerate(entries):
        rank = i + 1
        leaderboard_entry = {
            'rank': rank,
            'user_id': entry['user_id'],
            'user_name': entry['user_name'],
            'avatar_url': entry['avatar_url'],
            'steps_completed': entry['steps_completed'],
            'time_spent_minutes': entry['time_spent_minutes'],
            'is_current_user': entry['is_current_user']
        }
        leaderboard.append(leaderboard_entry)
        
        if entry['is_current_user']:
            current_user_rank = rank
            current_user_entry = leaderboard_entry
    
    # Calculate steps to next rank
    steps_to_next_rank = 0
    if current_user_rank > 1 and current_user_entry:
        # Find the person ahead
        person_ahead = leaderboard[current_user_rank - 2]  # -2 because rank is 1-indexed and we need previous
        steps_to_next_rank = person_ahead['steps_completed'] - current_user_entry['steps_completed'] + 1
        if steps_to_next_rank < 0:
            steps_to_next_rank = 0
    
    # Fun titles based on rank
    def get_rank_title(rank: int, total: int) -> str:
        if rank == 1:
            return "👑 The GOAT"
        elif rank == 2:
            return "🥈 Almost There"
        elif rank == 3:
            return "🥉 Bronze Legend"
        elif rank <= 5:
            return "🔥 On Fire"
        elif rank <= total * 0.25:
            return "💪 Top 25%"
        elif rank <= total * 0.5:
            return "📈 Rising Star"
        else:
            return "🚀 Just Getting Started"
    
    return {
        "group_id": group_id,
        "group_name": group_name,
        "leaderboard": leaderboard[:20],  # Top 20
        "current_user_rank": current_user_rank,
        "current_user_entry": current_user_entry,
        "current_user_title": get_rank_title(current_user_rank, len(entries)) if current_user_rank > 0 else "🎯 No Progress Yet",
        "total_participants": len(entries),
        "period": period,
        "steps_to_next_rank": steps_to_next_rank
    }


@router.get("/group-schedules/{group_id}")
async def get_group_schedules(
    group_id: int,
    weeks_back: int = Query(default=4, ge=0, le=12),
    weeks_ahead: int = Query(default=8, ge=0, le=24),
    current_user: UserInDB = Depends(get_current_user_dependency),
    db: Session = Depends(get_db)
):
    """
    Get class events for a group (from Events table).
    Used for linking assignments to specific classes.
    Returns events with calculated lesson_number based on chronological order.
    """
    from src.schemas.models import Event, EventGroup, CourseGroupAccess, EventCourse
    from src.services.event_service import EventService
    
    # Check permissions
    if current_user.role not in ["admin", "teacher", "head_curator"]:
        group = db.query(Group).filter(
            Group.id == group_id,
            or_(Group.teacher_id == current_user.id, Group.curator_id == current_user.id)
        ).first()
        if not group:
            raise HTTPException(status_code=403, detail="Access denied to this group")
    
    # Get group info
    group = db.query(Group).filter(Group.id == group_id).first()
    group_name = group.name if group else "Group"
    
    # Calculate date range
    now = datetime.utcnow()
    start_date = now - timedelta(weeks=weeks_back)
    end_date = now + timedelta(weeks=weeks_ahead)
    
    # Get courses linked to this group
    course_accesses = db.query(CourseGroupAccess).filter(
        CourseGroupAccess.group_id == group_id,
        CourseGroupAccess.is_active == True
    ).all()
    course_ids = [ca.course_id for ca in course_accesses]
    
    # Get standard events (non-recurring) for this group
    standard_events = db.query(Event).outerjoin(EventGroup).outerjoin(EventCourse).filter(
        Event.event_type == 'class',
        Event.is_active == True,
        Event.is_recurring == False,
        or_(
            EventGroup.group_id == group_id,
            EventCourse.course_id.in_(course_ids) if course_ids else False
        )
    ).distinct().all()
    
    # Expand recurring events
    recurring_instances = EventService.expand_recurring_events(
        db=db,
        start_date=start_date,
        end_date=end_date,
        group_ids=[group_id],
        course_ids=course_ids
    )
    recurring_instances = [e for e in recurring_instances if e.event_type == 'class']
    
    # Combine and deduplicate
    all_events = []
    seen_times = set()
    
    for e in standard_events:
        time_sig = e.start_datetime.replace(second=0, microsecond=0)
        if time_sig not in seen_times:
            all_events.append(e)
            seen_times.add(time_sig)
            
    for instance in recurring_instances:
        time_sig = instance.start_datetime.replace(second=0, microsecond=0)
        if time_sig not in seen_times:
            all_events.append(instance)
            seen_times.add(time_sig)
    
    # Sort by date
    all_events.sort(key=lambda x: x.start_datetime)
    
    # Calculate lesson numbers based on chronological order (all events, not just filtered)
    lesson_number_map = {}
    for idx, event in enumerate(all_events, start=1):
        lesson_number_map[event.id] = idx
    
    # Filter to date range for response
    filtered_events = [e for e in all_events if start_date <= e.start_datetime <= end_date]
    
    result = []
    for event in filtered_events:
        lesson_number = lesson_number_map.get(event.id, 0)
        
        # Mark if lesson is in the past
        is_past = event.start_datetime < now
        
        # Return UTC with explicit Z for timezone clarity
        dt = event.start_datetime
        iso_str = dt.isoformat() + ('Z' if dt.tzinfo is None else '')
        
        result.append({
            "id": event.id,
            "event_id": event.id,
            "title": event.title or f"{group_name}: Lesson {lesson_number}",
            "scheduled_at": iso_str,
            "group_id": group_id,
            "lesson_number": lesson_number,
            "is_past": is_past
        })
    
    return result