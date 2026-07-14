from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session, joinedload
from sqlalchemy import func, desc, and_, or_, select
from typing import List, Optional
from datetime import datetime, date, timedelta, timezone

from src.config import get_db
from src.schemas.models import (
    UserInDB, Event, EventGroup, EventParticipant, Group, GroupStudent,
    EventSchema, EventParticipantSchema, Enrollment, EventCourse, Course,
    CreateEventRequest, Assignment, Lesson, Module, LessonSchedule,
    AttendanceBulkUpdateSchema, EventStudentSchema, CourseGroupAccess, CourseHeadTeacher
)
from src.routes.auth import get_current_user_dependency
from src.utils.permissions import require_role, require_teacher_or_admin, require_teacher_curator_or_admin, check_event_access
from src.services.attendance_service import (
    AttendanceService,
    attendance_status_to_ui,
    ep_status_to_attendance_status,
)
from src.services.cache_service import cached

router = APIRouter()

@router.get("/my", response_model=List[EventSchema])
def get_my_events(
    skip: int = Query(0, ge=0),
    limit: int = Query(100, le=1000),
    event_type: Optional[str] = Query(None),
    group_id: Optional[int] = Query(None),
    start_date: Optional[date] = Query(None),
    end_date: Optional[date] = Query(None),
    upcoming_only: bool = Query(True),
    db: Session = Depends(get_db),
    current_user: UserInDB = Depends(get_current_user_dependency)
):
    """Get events for current user based on their groups"""
    
    # Get user's groups and courses
    user_group_ids = []
    user_course_ids = []
    
    if current_user.role == "student":
        # Get groups where user is a student
        user_groups = db.query(GroupStudent).filter(GroupStudent.student_id == current_user.id).all()
        user_group_ids = [ug.group_id for ug in user_groups]
        
        # Get courses where user is enrolled
        enrollments = db.query(Enrollment).filter(
            Enrollment.user_id == current_user.id,
            Enrollment.is_active == True
        ).all()
        user_course_ids = [e.course_id for e in enrollments]
        
        # Get courses accessible via user's groups
        if user_group_ids:
            from src.schemas.models import CourseGroupAccess
            group_access = db.query(CourseGroupAccess).filter(
                CourseGroupAccess.group_id.in_(user_group_ids),
                CourseGroupAccess.is_active == True
            ).all()
            group_course_ids = [ga.course_id for ga in group_access]
            user_course_ids.extend(group_course_ids)
            # Deduplicate
            user_course_ids = list(set(user_course_ids))
        
    elif current_user.role in ["teacher", "curator"]:
        print(f"DEBUG_EVENTS: Teacher/Curator view. user_id={current_user.id}")
        # Get groups where user is teacher or curator
        teacher_groups = db.query(Group).filter(Group.teacher_id == current_user.id).all()
        curator_groups = db.query(Group).filter(Group.curator_id == current_user.id).all()
        user_group_ids = list(set([g.id for g in teacher_groups + curator_groups]))
        
        # Teachers/Curators see events for courses they teach? 
        # For now, let's assume they see events for groups they manage.
        # If we want them to see course events, we'd need to know which courses they teach.
        # Assuming they see all events for now if they are admin, but here restricted.
        pass
        
    elif current_user.role == "admin":
        # Admins see all events
        user_group_ids = [g.id for g in db.query(Group).all()]
        user_course_ids = [c.id for c in db.query(Course).all()]
    
    if not user_group_ids and not user_course_ids:
        return []
    print(f"DEBUG: user_group_ids={user_group_ids} user_course_ids={user_course_ids}")
    
    # Build query
    # Events that are in user's groups OR in user's courses
    
    # Security check if group_id requested
    if group_id:
        if group_id not in user_group_ids:
            # If user is admin, allow
            if current_user.role != "admin":
                # Check if group is accessible via course? 
                # Simplest: if not in user's direct groups, deny or fallback?
                # User might have course access which covers group? 
                # Let's simple check:
                raise HTTPException(status_code=403, detail="Access denied to this group's events")
                
    query = db.query(Event).outerjoin(EventGroup).outerjoin(EventCourse).filter(
        Event.is_active == True
    )
    
    if group_id:
        query = query.filter(EventGroup.group_id == group_id)
    else:
        # Base condition: Events in user's groups or courses
        access_condition = or_(
            EventGroup.group_id.in_(user_group_ids),
            EventCourse.course_id.in_(user_course_ids)
        )
        
        # If user is a teacher/curator, ALSO include events where they are the assigned teacher
        # (e.g. substitutions where they are not the group's main teacher)
        if current_user.role in ["teacher", "curator"]:
            query = query.filter(
                or_(
                    access_condition,
                    Event.teacher_id == current_user.id
                )
            )
        else:
            query = query.filter(access_condition)
    
    query = query.distinct()
    
    # Apply filters
    if event_type:
        query = query.filter(Event.event_type == event_type)
    if start_date:
        query = query.filter(Event.start_datetime >= datetime.combine(start_date, datetime.min.time()))
    if end_date:
        query = query.filter(Event.start_datetime <= datetime.combine(end_date, datetime.max.time()))
    if upcoming_only:
        query = query.filter(Event.start_datetime >= datetime.now(timezone.utc))
    
    # Eager load relationships
    query = query.options(
        joinedload(Event.creator),
        joinedload(Event.event_groups).joinedload(EventGroup.group),
        joinedload(Event.event_courses).joinedload(EventCourse.course),
        joinedload(Event.teacher)
    )
    
    events = query.order_by(Event.start_datetime).offset(skip).limit(limit).all()
    print(f"DEBUG_EVENTS: Found {len(events)} events for user={current_user.id}")
    
    # 2. Expand Recurring Events if needed
    # If specific date range or upcoming_only, we must expand
    should_expand = start_date or end_date or upcoming_only
    
    if should_expand:
        from src.services.event_service import EventService
        
        # Determine range for expansion
        exp_start = datetime.combine(start_date, datetime.min.time()) if start_date else datetime.now(timezone.utc)
        if end_date:
            exp_end = datetime.combine(end_date, datetime.max.time())
        else:
            # Default lookup window for recurring expansion if no end date
            # E.g. next 90 days for "upcoming"
            exp_end = exp_start + timedelta(days=90)
            
        recurring_instances = EventService.expand_recurring_events(
            db=db,
            start_date=exp_start,
            end_date=exp_end,
            group_ids=[group_id] if group_id else user_group_ids,
            course_ids=user_course_ids
        )
        
        # Filter by event_type if requested
        if event_type:
            recurring_instances = [e for e in recurring_instances if e.event_type == event_type]
            
        # Combine
        # Deduplication strategy: Real events usually override recurring instances.
        # Filter out the parent recurring events from the initial query
        standard_events = [e for e in events if not e.is_recurring]
        
        # Start with standard non-recurring events
        events = list(standard_events)
        standard_times = {e.start_datetime for e in standard_events}
        
        # Add recurring instances only if there's no manual event at the same time
        for instance in recurring_instances:
            if instance.start_datetime not in standard_times:
                events.append(instance)
                
        events.sort(key=lambda x: x.start_datetime)
        
        # Re-apply limit after expansion
        if limit:
            events = events[:limit]

    # Final Deduplication by Signature (Group, Time)
    # For class events, deduplicate by group_id and start_datetime only to ensure
    # only one entry exists per time slot per group regardless of source
    seen_signatures = set()
    unique_events = []
    for e in events:
        # Round time to nearest minute for comparison
        time_sig = e.start_datetime.replace(second=0, microsecond=0)
        
        # For events with multiple groups, we check if WE (the user) are interested in this one
        # If group_id filter was used, check only that
        # Use _group_ids for virtual events
        virtual_group_ids = getattr(e, '_group_ids', None)
        if virtual_group_ids:
            relevant_groups = [group_id] if group_id else virtual_group_ids
        else:
            relevant_groups = [group_id] if group_id else [eg.group_id for eg in e.event_groups]
        
        for g_id in relevant_groups:
            # For class events, use simpler signature to catch all duplicates
            if e.event_type == "class":
                sig = (g_id, time_sig)
            else:
                sig = (g_id, time_sig, e.title)
            
            if sig not in seen_signatures:
                unique_events.append(e)
                seen_signatures.add(sig)
                break # Only add once even if multi-group (to keep list unique)
                
    events = unique_events
    # Batch fetch participant counts
    event_ids = [e.id for e in events]
    count_map = {}
    if event_ids:
        participant_counts = db.query(
            EventParticipant.event_id, 
            func.count(EventParticipant.id)
        ).filter(
            EventParticipant.event_id.in_(event_ids)
        ).group_by(EventParticipant.event_id).all()
        count_map = {event_id: count for event_id, count in participant_counts}
    
    # Enrich with additional data
    result = []
    for event in events:
        event_data = EventSchema.from_orm(event)
        event_data.creator_name = event.creator.name if event.creator else "Unknown"
        group_names = [eg.group.name for eg in event.event_groups if eg.group]
        event_data.groups = group_names
        event_data.participant_count = count_map.get(event.id, 0)
        
        # Ensure title includes group name for events
        if group_names:
            main_group = group_names[0]
            if not event.title.startswith(main_group):
                event_data.title = f"{main_group}: {event.title}"
                
        result.append(event_data)
        
    # Add Lesson Schedules (Planned) if no real event exists
    # Use group_id if specified, otherwise use all user's groups
    schedule_group_ids = [group_id] if group_id else user_group_ids
    
    if schedule_group_ids and (not event_type or event_type == "class"):
        ls_query = db.query(LessonSchedule).filter(
            LessonSchedule.group_id.in_(schedule_group_ids),
            LessonSchedule.is_active == True
        )
        if start_date:
            ls_query = ls_query.filter(LessonSchedule.scheduled_at >= datetime.combine(start_date, datetime.min.time()))
        if end_date:
            ls_query = ls_query.filter(LessonSchedule.scheduled_at <= datetime.combine(end_date, datetime.max.time()))
        if upcoming_only:
            ls_query = ls_query.filter(LessonSchedule.scheduled_at >= datetime.now(timezone.utc))
            
        lessons_schedules = ls_query.options(
            joinedload(LessonSchedule.lesson),
            joinedload(LessonSchedule.group)
        ).order_by(LessonSchedule.scheduled_at).all()

        
        # Pre-calculate lesson numbers for each group
        # Get all schedules for user's groups to calculate lesson numbers
        all_group_schedules = db.query(LessonSchedule).filter(
            LessonSchedule.group_id.in_(schedule_group_ids),
            LessonSchedule.is_active == True
        ).order_by(LessonSchedule.group_id, LessonSchedule.scheduled_at).all()
        
        # Build lesson_number map: {schedule_id: lesson_number}
        lesson_number_map = {}
        current_group = None
        counter = 0
        for sched in all_group_schedules:
            if sched.group_id != current_group:
                current_group = sched.group_id
                counter = 0
            counter += 1
            lesson_number_map[sched.id] = counter
        
        # Mapping course_id -> group_ids for current context
        course_to_groups = {}
        if schedule_group_ids:
            from src.schemas.models import CourseGroupAccess
            accesses = db.query(CourseGroupAccess).filter(
                CourseGroupAccess.group_id.in_(schedule_group_ids),
                CourseGroupAccess.is_active == True
            ).all()
            for acc in accesses:
                if acc.course_id not in course_to_groups:
                    course_to_groups[acc.course_id] = []
                course_to_groups[acc.course_id].append(acc.group_id)

        # Deduplication map
        existing_event_map = set()
        for e in events:
            # Group IDs directly linked OR from _group_ids for virtual events
            virtual_gids = getattr(e, '_group_ids', None)
            e_group_ids = virtual_gids or ([eg.group_id for eg in e.event_groups] if hasattr(e, 'event_groups') else [])
            for g_id in e_group_ids:
                sig = (g_id, e.start_datetime.replace(second=0, microsecond=0))
                existing_event_map.add(sig)
            
            # Group IDs linked via courses
            e_course_ids = [ec.course_id for ec in e.event_courses] if hasattr(e, 'event_courses') else []
            for c_id in e_course_ids:
                for g_id in course_to_groups.get(c_id, []):
                    sig = (g_id, e.start_datetime.replace(second=0, microsecond=0))
                    existing_event_map.add(sig)

        for sched in lessons_schedules:
            # Check for duplicate
            sched_time = sched.scheduled_at.replace(second=0, microsecond=0)
            if (sched.group_id, sched_time) in existing_event_map:
                continue # Skip, real event exists
                
            virtual_id = 2000000000 + sched.id 
            # Get lesson number from pre-calculated map
            group_name = sched.group.name if sched.group else "Group"
            lesson_number = lesson_number_map.get(sched.id, sched.week_number)
            
            title = f"{group_name}: Lesson {lesson_number}"
            end_dt = sched.scheduled_at + timedelta(minutes=60)
            
            result.append(EventSchema(
                id=virtual_id,
                title=title,
                description=f"Scheduled class for {group_name}",
                event_type="class",
                start_datetime=sched.scheduled_at,
                end_datetime=end_dt,
                location="Planned",
                is_online=True,
                created_by=0,
                creator_name="System",
                is_active=True,
                is_recurring=False,
                participant_count=0,
                created_at=datetime.now(timezone.utc),
                updated_at=datetime.now(timezone.utc),
                groups=[sched.group.name] if sched.group else [],
                group_ids=[sched.group_id],
                courses=[]
            ))
            
    # Sort and limit result if mixed
    result.sort(key=lambda x: x.start_datetime)
    return result[:limit]

@router.get("/calendar", response_model=List[EventSchema])
@cached(namespace="events:calendar", ttl=30, key_args=("year", "month"))
def get_calendar_events(
    year: int = Query(..., ge=2020, le=2030),
    month: int = Query(..., ge=1, le=12),
    db: Session = Depends(get_db),
    current_user: UserInDB = Depends(get_current_user_dependency)
):
    """Get events for calendar view by month"""
    
    # Calculate month boundaries
    start_date = datetime(year, month, 1)
    if month == 12:
        end_date = datetime(year + 1, 1, 1) - timedelta(seconds=1)
    else:
        end_date = datetime(year, month + 1, 1) - timedelta(seconds=1)
    
    # Get user's groups and courses
    user_group_ids = []
    user_course_ids = []
    
    if current_user.role == "student":
        user_groups = db.query(GroupStudent).filter(GroupStudent.student_id == current_user.id).all()
        user_group_ids = [ug.group_id for ug in user_groups]
        
        enrollments = db.query(Enrollment).filter(
            Enrollment.user_id == current_user.id,
            Enrollment.is_active == True
        ).all()
        user_course_ids = [e.course_id for e in enrollments]
        
        # Get courses accessible via user's groups
        if user_group_ids:
            from src.schemas.models import CourseGroupAccess
            group_access = db.query(CourseGroupAccess).filter(
                CourseGroupAccess.group_id.in_(user_group_ids),
                CourseGroupAccess.is_active == True
            ).all()
            group_course_ids = [ga.course_id for ga in group_access]
            user_course_ids.extend(group_course_ids)
            # Deduplicate
            user_course_ids = list(set(user_course_ids))
        
    elif current_user.role in ["teacher", "curator"]:
        teacher_groups = db.query(Group).filter(Group.teacher_id == current_user.id).all()
        curator_groups = db.query(Group).filter(Group.curator_id == current_user.id).all()
        user_group_ids = list(set([g.id for g in teacher_groups + curator_groups]))

    elif current_user.role == "head_curator":
        user_group_ids = [g.id for g in db.query(Group).filter(Group.is_active == True).all()]
        user_course_ids = [c.id for c in db.query(Course).filter(Course.is_active == True).all()]

    elif current_user.role == "head_teacher":
        managed_rows = db.query(CourseHeadTeacher.course_id).filter(
            CourseHeadTeacher.head_teacher_id == current_user.id
        ).all()
        user_course_ids = [row[0] for row in managed_rows]
        if user_course_ids:
            user_group_ids = [
                row[0] for row in db.query(CourseGroupAccess.group_id).filter(
                    CourseGroupAccess.course_id.in_(user_course_ids),
                    CourseGroupAccess.is_active == True,
                ).distinct().all()
            ]
        else:
            user_group_ids = []
        
    elif current_user.role == "admin":
        user_group_ids = [g.id for g in db.query(Group).all()]
        user_course_ids = [c.id for c in db.query(Course).all()]
    
    if not user_group_ids and not user_course_ids:
        print(f"DEBUG: No groups or courses for user {current_user.id}")
        return []
    
    import sys
    
    print(f"DEBUG: Calendar request - User: {current_user.id}, Role: {current_user.role}")
    print(f"DEBUG: Groups: {user_group_ids}")
    print(f"DEBUG: Courses: {user_course_ids}")
    print(f"DEBUG: Date Range: {start_date} to {end_date}")
    sys.stdout.flush()

    # Get events for the month with eager loading
    # 1. Get standard events in range
    
    # Logic to include events where I am the teacher (e.g. substitutions)
    base_access = or_(
        EventGroup.group_id.in_(user_group_ids),
        EventCourse.course_id.in_(user_course_ids)
    )
    
    final_filter = base_access
    if current_user.role in ["teacher", "curator", "head_teacher"]:
        print(f"DEBUG: Including events where teacher_id={current_user.id}")
        final_filter = or_(base_access, Event.teacher_id == current_user.id)

    standard_events = db.query(Event).outerjoin(EventGroup).outerjoin(EventCourse).filter(
        final_filter,
        Event.is_active == True,
        Event.start_datetime >= start_date,
        Event.start_datetime <= end_date,
        Event.is_recurring == False # Only non-recurring instances
    ).distinct().options(
        joinedload(Event.creator),
        joinedload(Event.event_groups).joinedload(EventGroup.group),
        joinedload(Event.event_courses).joinedload(EventCourse.course)
    ).all()

    # 2. Get expanded recurring events
    from src.services.event_service import EventService
    
    generated_events = EventService.expand_recurring_events(
        db=db,
        start_date=start_date,
        end_date=end_date,
        group_ids=user_group_ids,
        course_ids=user_course_ids
    )

    # Combine and sort
    all_events = standard_events + generated_events
    
    # Final Deduplication by ID
    seen_ids = set()
    unique_all = []
    for e in all_events:
        if e.id not in seen_ids:
            unique_all.append(e)
            seen_ids.add(e.id)
    all_events = unique_all
    
    all_events.sort(key=lambda x: x.start_datetime)
    

    # Batch fetch participant counts (only for real events)
    real_event_ids = [e.id for e in standard_events]
    count_map = {}
    
    if real_event_ids:
        participant_counts = db.query(
            EventParticipant.event_id, 
            func.count(EventParticipant.id)
        ).filter(
            EventParticipant.event_id.in_(real_event_ids)
        ).group_by(EventParticipant.event_id).all()
        
        count_map = {event_id: count for event_id, count in participant_counts}
    
    # Enrich with additional data
    result = []
    
    # 2.5 Get LessonSchedule items (Planned Schedule) that might not have Events yet
    # This ensures the calendar shows the "Plan" even if "Events" aren't created
    if user_group_ids:
        # Import here to avoid circular deps if any, though top-level is fine
        from src.schemas.models import LessonSchedule
        
        schedules = db.query(LessonSchedule).filter(
            LessonSchedule.group_id.in_(user_group_ids),
            LessonSchedule.is_active == True,
            LessonSchedule.scheduled_at >= start_date,
            LessonSchedule.scheduled_at <= end_date
        ).options(
            joinedload(LessonSchedule.group),
            joinedload(LessonSchedule.lesson)
        ).order_by(LessonSchedule.scheduled_at).all()
        
        # Pre-calculate lesson numbers for each group
        all_group_schedules = db.query(LessonSchedule).filter(
            LessonSchedule.group_id.in_(user_group_ids),
            LessonSchedule.is_active == True
        ).order_by(LessonSchedule.group_id, LessonSchedule.scheduled_at).all()
        
        # Build lesson_number map: {schedule_id: lesson_number}
        lesson_number_map = {}
        current_group = None
        counter = 0
        for sched in all_group_schedules:
            if sched.group_id != current_group:
                current_group = sched.group_id
                counter = 0
            counter += 1
            lesson_number_map[sched.id] = counter
        
        # Convert schedules to virtual events
        # Check against existing real events to avoid duplicates?
        # Robust strategy: If a real event exists at approx same time for same group, skip schedule?
        # Simpler: Just show both? Or show schedule. 
        # Usually admin creates events based on schedule. 
        # Let's map existing events by (group_id, start_time) to filter duplicates.
        
        # Mapping course_id -> group_ids for current context
        course_to_groups = {}
        if user_group_ids:
            from src.schemas.models import CourseGroupAccess
            accesses = db.query(CourseGroupAccess).filter(
                CourseGroupAccess.group_id.in_(user_group_ids),
                CourseGroupAccess.is_active == True
            ).all()
            for acc in accesses:
                if acc.course_id not in course_to_groups:
                    course_to_groups[acc.course_id] = []
                course_to_groups[acc.course_id].append(acc.group_id)

        existing_event_map = set()
        for e in all_events:
            # Group IDs directly linked OR from _group_ids for virtual events
            e_group_ids = getattr(e, '_group_ids', None) or [eg.group_id for eg in e.event_groups] if hasattr(e, 'event_groups') else []
            for g_id in e_group_ids:
                sig = (g_id, e.start_datetime.replace(second=0, microsecond=0))
                existing_event_map.add(sig)
            
            # Group IDs linked via courses
            e_course_ids = [ec.course_id for ec in e.event_courses] if hasattr(e, 'event_courses') else []
            for c_id in e_course_ids:
                for g_id in course_to_groups.get(c_id, []):
                    sig = (g_id, e.start_datetime.replace(second=0, microsecond=0))
                    existing_event_map.add(sig)
                
        for sched in schedules:
            # Check for duplicate
            sched_time = sched.scheduled_at.replace(second=0, microsecond=0)
            if (sched.group_id, sched_time) in existing_event_map:
                continue # Skip, real event exists
                
            # Create virtual event
            # Use negative ID or large offset to distinguish
            virtual_id = 2000000000 + sched.id 
            
            # Get lesson number from pre-calculated map
            group_name = sched.group.name if sched.group else "Group"
            lesson_number = lesson_number_map.get(sched.id, sched.week_number)
            title = f"{group_name}: Lesson {lesson_number}"
            
            # Duration default 1 hour
            end_dt = sched.scheduled_at + timedelta(minutes=60)
            
            sched_event = EventSchema(
                id=virtual_id,
                title=title,
                description=f"Scheduled class for {group_name}",
                event_type="class",
                start_datetime=sched.scheduled_at,
                end_datetime=end_dt,
                location="Planned", 
                is_online=True, 
                created_by=0,
                creator_name="System",
                is_active=True,
                is_recurring=False,
                participant_count=0,
                created_at=datetime.now(timezone.utc),
                updated_at=datetime.now(timezone.utc),
                groups=[sched.group.name] if sched.group else [],
                group_ids=[sched.group_id],
                courses=[]
            )
            result.append(sched_event)

    for event in all_events:
        # For virtual events, we might need to handle schema conversion manually 
        # because they are not attached to session
        
        event_data = EventSchema.from_orm(event)
        
        # Add creator name
        event_data.creator_name = event.creator.name if event.creator else "Unknown"
        
        # Add group names - use _group_ids for virtual events
        virtual_group_ids = getattr(event, '_group_ids', None)
        if virtual_group_ids:
            # For virtual events, fetch group names
            groups = db.query(Group).filter(Group.id.in_(virtual_group_ids)).all()
            group_names = [g.name for g in groups]
            event_data.group_ids = virtual_group_ids
        else:
            group_names = [eg.group.name for eg in event.event_groups if eg.group]
            event_data.group_ids = [eg.group_id for eg in event.event_groups]
        event_data.groups = group_names
        
        # Add participant count
        event_data.participant_count = count_map.get(event.id, 0)
        
        # Ensure title includes group name for events (non-class events from recurring)
        if group_names:
            main_group = group_names[0]
            if not event.title.startswith(main_group):
                event_data.title = f"{main_group}: {event.title}"
        
        result.append(event_data)
    
    # 3. Get Assignments as Events
    # Use split queries to ensure robustness and easy debugging
    
    found_assignments = []
    
    # 3a. Fetch by Group
    if user_group_ids:
        group_assignments = db.query(Assignment).filter(
            Assignment.is_active == True,
            Assignment.due_date >= start_date,
            Assignment.due_date <= end_date,
            or_(Assignment.is_hidden == False, Assignment.is_hidden == None),
            Assignment.group_id.in_(user_group_ids)
        ).all()
        found_assignments.extend(group_assignments)
                
    # 3b. Fetch by Course -> Lesson
    # First get lesson IDs for user's courses to avoid subquery issues
    course_lesson_ids = []
    if user_course_ids:
        lesson_rows = db.query(Lesson.id).join(Module).filter(
            Module.course_id.in_(user_course_ids)
        ).all()
        course_lesson_ids = [r[0] for r in lesson_rows]
        
    if course_lesson_ids:
        lesson_assignments = db.query(Assignment).filter(
            Assignment.is_active == True,
            Assignment.due_date >= start_date,
            Assignment.due_date <= end_date,
            or_(Assignment.is_hidden == False, Assignment.is_hidden == None),
            Assignment.lesson_id.in_(course_lesson_ids)
        ).all()
        found_assignments.extend(lesson_assignments)
        
    # Deduplicate assignments by ID
    assignments = list({a.id: a for a in found_assignments}.values())
    
    if not assignments and user_group_ids:
        # Extra debug: check raw count of assignments for this group globally
        raw_count = db.query(Assignment).filter(Assignment.group_id.in_(user_group_ids)).count()
        
    sys.stdout.flush()
    
    # Convert assignments to Event schema
    for assignment in assignments:
        # Create a virtual event for the assignment deadline
        # ID logic: maybe use negative ID to avoid collision or string ID?
        # But schema requires integer ID.
        # We can offset ID by a large number, e.g. 1000000000 + assignment.id
        
        assign_event_id = 1000000000 + assignment.id
        
        # Determine duration (e.g. 1 hour ending at due_date)
        end_dt = assignment.due_date
        start_dt = end_dt - timedelta(hours=1)
        
        assign_event = EventSchema(
            id=assign_event_id,
            title=f"Deadline: {assignment.title}",
            description=assignment.description,
            event_type="assignment", # Custom type for frontend
            start_datetime=start_dt,
            end_datetime=end_dt,
            location="LMS",
            is_online=True,
            created_by=0, # System
            creator_name="System",
            is_active=True,
            is_recurring=False,
            participant_count=0,
            created_at=assignment.created_at,
            updated_at=assignment.created_at,
            groups=[], # We could fetch, but might be slow
            courses=[]
        )
        
        result.append(assign_event)
        
    # Sort again by start date
    result.sort(key=lambda x: x.start_datetime)
    
    return result

@router.get("/upcoming", response_model=List[EventSchema])
def get_upcoming_events(
    limit: int = Query(10, le=50),
    days_ahead: int = Query(7, ge=1, le=30),
    db: Session = Depends(get_db),
    current_user: UserInDB = Depends(get_current_user_dependency)
):
    """Get upcoming events for current user"""
    
    # Calculate date range
    start_date = datetime.now(timezone.utc)
    end_date = start_date + timedelta(days=days_ahead)
    
    # Get user's groups and courses
    user_group_ids = []
    user_course_ids = []
    
    if current_user.role == "student":
        user_groups = db.query(GroupStudent).filter(GroupStudent.student_id == current_user.id).all()
        user_group_ids = [ug.group_id for ug in user_groups]
        
        enrollments = db.query(Enrollment).filter(
            Enrollment.user_id == current_user.id,
            Enrollment.is_active == True
        ).all()
        user_course_ids = [e.course_id for e in enrollments]
        
        # Get courses accessible via user's groups
        if user_group_ids:
            from src.schemas.models import CourseGroupAccess
            group_access = db.query(CourseGroupAccess).filter(
                CourseGroupAccess.group_id.in_(user_group_ids),
                CourseGroupAccess.is_active == True
            ).all()
            group_course_ids = [ga.course_id for ga in group_access]
            user_course_ids.extend(group_course_ids)
            # Deduplicate
            user_course_ids = list(set(user_course_ids))
        
    elif current_user.role in ["teacher", "curator"]:
        teacher_groups = db.query(Group).filter(Group.teacher_id == current_user.id).all()
        curator_groups = db.query(Group).filter(Group.curator_id == current_user.id).all()
        user_group_ids = list(set([g.id for g in teacher_groups + curator_groups]))
        
    elif current_user.role == "admin":
        user_group_ids = [g.id for g in db.query(Group).all()]
        user_course_ids = [c.id for c in db.query(Course).all()]
    
    if not user_group_ids and not user_course_ids:
        return []
    
    # Get upcoming events with eager loading
    
    events = db.query(Event).outerjoin(EventGroup).outerjoin(EventCourse).filter(
        or_(
            EventGroup.group_id.in_(user_group_ids),
            EventCourse.course_id.in_(user_course_ids)
        ),
        Event.is_active == True,
        Event.start_datetime >= start_date,
        Event.start_datetime <= end_date
    ).distinct().options(
        joinedload(Event.creator),
        joinedload(Event.event_groups).joinedload(EventGroup.group),
        joinedload(Event.event_courses).joinedload(EventCourse.course)
    ).order_by(Event.start_datetime).limit(limit).all()
    
    # Batch fetch participant counts
    event_ids = [e.id for e in events]
    count_map = {}
    if event_ids:
        participant_counts = db.query(
            EventParticipant.event_id, 
            func.count(EventParticipant.id)
        ).filter(
            EventParticipant.event_id.in_(event_ids)
        ).group_by(EventParticipant.event_id).all()
        count_map = {event_id: count for event_id, count in participant_counts}
    
    # Enrich with additional data
    result = []
    for event in events:
        event_data = EventSchema.from_orm(event)
        event_data.creator_name = event.creator.name if event.creator else "Unknown"
        group_names = [eg.group.name for eg in event.event_groups if eg.group]
        event_data.groups = group_names
        event_data.participant_count = count_map.get(event.id, 0)
        
        # Ensure title includes group name for events
        if group_names:
            main_group = group_names[0]
            if not event.title.startswith(main_group):
                event_data.title = f"{main_group}: {event.title}"
        
        result.append(event_data)
        
    # Mapping course_id -> group_ids for current context
    course_to_groups = {}
    if user_group_ids:
        from src.schemas.models import CourseGroupAccess
        accesses = db.query(CourseGroupAccess).filter(
            CourseGroupAccess.group_id.in_(user_group_ids),
            CourseGroupAccess.is_active == True
        ).all()
        for acc in accesses:
            if acc.course_id not in course_to_groups:
                course_to_groups[acc.course_id] = []
            course_to_groups[acc.course_id].append(acc.group_id)

    # Create expansion map for deduplication
    existing_event_map = set()
    for e in events:
        # Group IDs directly linked
        e_group_ids = [eg.group_id for eg in e.event_groups] if hasattr(e, 'event_groups') else []
        for g_id in e_group_ids:
            sig = (g_id, e.start_datetime.replace(second=0, microsecond=0))
            existing_event_map.add(sig)
            
        # Group IDs linked via courses
        e_course_ids = [ec.course_id for ec in e.event_courses] if hasattr(e, 'event_courses') else []
        for c_id in e_course_ids:
            for g_id in course_to_groups.get(c_id, []):
                sig = (g_id, e.start_datetime.replace(second=0, microsecond=0))
                existing_event_map.add(sig)

    # Add upcoming Lesson Schedules (Planned)
    if user_group_ids:
        ls_query = db.query(LessonSchedule).filter(
            LessonSchedule.group_id.in_(user_group_ids),
            LessonSchedule.is_active == True,
            LessonSchedule.scheduled_at >= start_date,
            LessonSchedule.scheduled_at <= end_date
        ).options(
            joinedload(LessonSchedule.lesson),
            joinedload(LessonSchedule.group)
        ).order_by(LessonSchedule.scheduled_at).limit(limit).all()
        
        # Pre-calculate lesson numbers for each group
        all_group_schedules = db.query(LessonSchedule).filter(
            LessonSchedule.group_id.in_(user_group_ids),
            LessonSchedule.is_active == True
        ).order_by(LessonSchedule.group_id, LessonSchedule.scheduled_at).all()
        
        # Build lesson_number map
        lesson_number_map = {}
        current_group = None
        counter = 0
        for sched in all_group_schedules:
            if sched.group_id != current_group:
                current_group = sched.group_id
                counter = 0
            counter += 1
            lesson_number_map[sched.id] = counter
        
        for sched in ls_query:
            # Deduplicate against real events
            sched_time = sched.scheduled_at.replace(second=0, microsecond=0)
            if (sched.group_id, sched_time) in existing_event_map:
                continue

            lesson_event_id = 2000000000 + sched.id
            # Get lesson number from pre-calculated map
            group_name = sched.group.name if sched.group else "Group"
            lesson_number = lesson_number_map.get(sched.id, sched.week_number)
            title = f"{group_name}: Lesson {lesson_number}"
            end_dt = sched.scheduled_at + timedelta(minutes=60)
            
            lesson_event = EventSchema(
                id=lesson_event_id,
                title=title,
                description=f"Scheduled class for {group_name}",
                event_type="class",
                start_datetime=sched.scheduled_at,
                end_datetime=end_dt,
                location="Planned",
                is_online=True,
                created_by=0,
                creator_name="System",
                is_active=True,
                is_recurring=False,
                participant_count=0,
                created_at=sched.scheduled_at,
                updated_at=sched.scheduled_at,
                groups=[sched.group.name] if sched.group else [],
                lesson_id=sched.lesson_id,
                group_ids=[sched.group_id]
            )
            result.append(lesson_event)
            
    # Sort and limit
    result.sort(key=lambda x: x.start_datetime)
    return result[:limit]

@router.get("/group/{group_id}/classes", response_model=List[EventSchema])
def get_group_class_events(
    group_id: int,
    weeks_back: int = Query(1, ge=0, le=52),
    weeks_ahead: int = Query(8, ge=0, le=52),
    db: Session = Depends(get_db),
    current_user: UserInDB = Depends(get_current_user_dependency)
):
    """
    Get class events for a specific group within a time window.
    Used for linking assignments to class sessions.
    """
    # Verify access to group
    group = db.query(Group).filter(Group.id == group_id).first()
    if not group:
        raise HTTPException(status_code=404, detail="Group not found")
    
    # Check permissions
    if current_user.role == "student":
        is_member = db.query(GroupStudent).filter(
            GroupStudent.group_id == group_id,
            GroupStudent.student_id == current_user.id
        ).first()
        if not is_member:
            raise HTTPException(status_code=403, detail="Access denied to this group")
    elif current_user.role == "teacher":
        if group.teacher_id != current_user.id:
            raise HTTPException(status_code=403, detail="Access denied to this group")
    elif current_user.role == "curator":
        if group.curator_id != current_user.id and group.teacher_id != current_user.id:
            raise HTTPException(status_code=403, detail="Access denied to this group")
    # admin has access to all groups
    
    # Calculate time window
    # For homework assignment linking, we need FUTURE events
    # But for attendance tracking, we need PAST events
    # This endpoint is used for homework linking, so include future events too
    now = datetime.now()
    start_date = now - timedelta(weeks=weeks_back)
    end_date = now + timedelta(weeks=weeks_ahead)
    
    # Get class events for this group (both past and future)
    events = db.query(Event).join(EventGroup).filter(
        EventGroup.group_id == group_id,
        Event.event_type == "class",
        Event.is_active == True,
        Event.start_datetime >= start_date,  # Not too old
        Event.start_datetime <= end_date  # Within time window
    ).order_by(Event.start_datetime).all()
    
    return events


# ---------------------------------------------------------------------------
# Virtual event reconstruction
#
# GET /events/calendar returns *virtual* instances that have no row in `events`:
#   recurring event/webinar : int(f"{parent.id}{int(start.timestamp())}") % 2**31-1
#   planned class           : VIRTUAL_CLASS_OFFSET      + lesson_schedule.id
#   assignment deadline     : VIRTUAL_ASSIGNMENT_OFFSET + assignment.id
# A plain GET /events/{id} would 404 on those synthetic ids. We rebuild the
# instance's data on the fly (no DB writes) so the detail screen and deep links
# resolve. Registration materializes lazily instead — see resolve_event_id.
# ---------------------------------------------------------------------------

VIRTUAL_CLASS_OFFSET = 2_000_000_000
VIRTUAL_ASSIGNMENT_OFFSET = 1_000_000_000


def _user_event_scope(db: Session, current_user: UserInDB):
    """Group and course ids the user may see events through. Admin sees all."""
    if current_user.role == "admin":
        group_ids = [gid for (gid,) in db.query(Group.id).all()]
        course_ids = [cid for (cid,) in db.query(Course.id).all()]
        return group_ids, course_ids

    group_ids: List[int] = []
    course_ids: List[int] = []
    if current_user.role == "student":
        group_ids = [ug.group_id for ug in db.query(GroupStudent).filter(
            GroupStudent.student_id == current_user.id).all()]
        course_ids = [e.course_id for e in db.query(Enrollment).filter(
            Enrollment.user_id == current_user.id, Enrollment.is_active == True).all()]
        if group_ids:
            accesses = db.query(CourseGroupAccess).filter(
                CourseGroupAccess.group_id.in_(group_ids),
                CourseGroupAccess.is_active == True).all()
            course_ids = list(set(course_ids + [a.course_id for a in accesses]))
    elif current_user.role in ("teacher", "curator"):
        groups = db.query(Group).filter(
            or_(Group.teacher_id == current_user.id, Group.curator_id == current_user.id)).all()
        group_ids = list({g.id for g in groups})
        course_ids = [c.id for c in db.query(Course).filter(
            Course.teacher_id == current_user.id).all()]
    return group_ids, course_ids


def _lesson_number_for_schedule(db: Session, sched: LessonSchedule) -> int:
    """1-based index of this schedule within its group's active schedule order —
    matches the "Lesson N" numbering the calendar endpoint builds."""
    rows = db.query(LessonSchedule.id).filter(
        LessonSchedule.group_id == sched.group_id,
        LessonSchedule.is_active == True,
    ).order_by(LessonSchedule.scheduled_at).all()
    for idx, (sid,) in enumerate(rows, start=1):
        if sid == sched.id:
            return idx
    return sched.week_number


def _reconstruct_virtual_event(db: Session, event_id: int, current_user: UserInDB):
    """Rebuild a virtual calendar instance read-only, or return None if the id
    matches no known virtual event. Raises 403 if the user lacks access.

    Note on id ranges: recurring pseudo ids are `... % 2147483647`, so they can
    land inside the assignment (>=1e9) and class (>=2e9) offset ranges. We can't
    disambiguate by range alone — so the offset branches only claim the id when
    the referenced row actually exists, otherwise we fall through to the
    recurring scan (which reuses the exact expansion that minted the id)."""
    is_admin = current_user.role == "admin"
    group_ids, course_ids = _user_event_scope(db, current_user)

    def deny():
        raise HTTPException(status_code=403, detail="Access denied to this event")

    def try_recurring():
        # Reuse the exact expansion that produced the pseudo id so the ids line
        # up; expansion is already scoped to the user's groups/courses, so a
        # match implies access.
        if not group_ids and not course_ids:
            return None
        from src.services.event_service import EventService
        now_naive = datetime.now(timezone.utc).replace(tzinfo=None)
        generated = EventService.expand_recurring_events(
            db=db,
            start_date=now_naive - timedelta(days=400),
            end_date=now_naive + timedelta(days=400),
            group_ids=group_ids,
            course_ids=course_ids,
        )
        match = next((e for e in generated if e.id == event_id), None)
        if match is None:
            return None
        event_data = EventSchema.from_orm(match)
        event_data.creator_name = match.creator.name if match.creator else "Unknown"
        v_group_ids = getattr(match, "_group_ids", None) or [eg.group_id for eg in match.event_groups]
        group_names = []
        if v_group_ids:
            group_names = [g.name for g in db.query(Group).filter(Group.id.in_(v_group_ids)).all()]
        event_data.group_ids = v_group_ids
        event_data.groups = group_names
        if match.teacher_id:
            t = db.query(UserInDB).filter(UserInDB.id == match.teacher_id).first()
            event_data.teacher_name = t.name if t else None
        if group_names and not match.title.startswith(group_names[0]):
            event_data.title = f"{group_names[0]}: {match.title}"
        event_data.participant_count = 0
        return event_data

    # Planned class (from LessonSchedule). May really be a recurring instance
    # whose pseudo id landed in this range — only claim it if the schedule exists.
    if event_id >= VIRTUAL_CLASS_OFFSET:
        sched = db.query(LessonSchedule).filter(
            LessonSchedule.id == event_id - VIRTUAL_CLASS_OFFSET,
            LessonSchedule.is_active == True,
        ).options(joinedload(LessonSchedule.group)).first()
        if sched:
            if not is_admin and sched.group_id not in group_ids:
                deny()
            group = sched.group
            group_name = group.name if group else "Group"
            teacher_name = None
            if group and group.teacher_id:
                t = db.query(UserInDB).filter(UserInDB.id == group.teacher_id).first()
                teacher_name = t.name if t else None
            now = datetime.now(timezone.utc)
            return EventSchema(
                id=event_id,
                title=f"{group_name}: Lesson {_lesson_number_for_schedule(db, sched)}",
                description=f"Scheduled class for {group_name}",
                event_type="class",
                start_datetime=sched.scheduled_at,
                end_datetime=sched.scheduled_at + timedelta(minutes=60),
                location="Planned", is_online=True, created_by=0, creator_name="System",
                is_active=True, is_recurring=False, participant_count=0,
                created_at=now, updated_at=now,
                groups=[group_name] if group else [], group_ids=[sched.group_id], courses=[],
                teacher_id=group.teacher_id if group else None, teacher_name=teacher_name,
            )
        return try_recurring()

    # Assignment deadline. Same ambiguity — only claim it if the assignment exists.
    if event_id >= VIRTUAL_ASSIGNMENT_OFFSET:
        assignment = db.query(Assignment).filter(
            Assignment.id == event_id - VIRTUAL_ASSIGNMENT_OFFSET,
            Assignment.is_active == True,
        ).first()
        if assignment and assignment.due_date:
            a_group_ids = [assignment.group_id] if assignment.group_id else []
            a_course_ids: List[int] = []
            if assignment.lesson_id:
                row = db.query(Module.course_id).join(
                    Lesson, Lesson.module_id == Module.id
                ).filter(Lesson.id == assignment.lesson_id).first()
                if row:
                    a_course_ids = [row[0]]
            if not is_admin and not (set(a_group_ids) & set(group_ids)
                                     or set(a_course_ids) & set(course_ids)):
                deny()
            end_dt = assignment.due_date
            return EventSchema(
                id=event_id, title=f"Deadline: {assignment.title}",
                description=assignment.description, event_type="assignment",
                start_datetime=end_dt - timedelta(hours=1), end_datetime=end_dt,
                location="LMS", is_online=True, created_by=0, creator_name="System",
                is_active=True, is_recurring=False, participant_count=0,
                created_at=assignment.created_at, updated_at=assignment.created_at,
                groups=[], group_ids=a_group_ids, courses=[],
            )
        return try_recurring()

    # Below both offsets — can only be a recurring instance.
    return try_recurring()


@router.get("/{event_id}", response_model=EventSchema)
def get_event_details(
    event_id: int,
    db: Session = Depends(get_db),
    current_user: UserInDB = Depends(get_current_user_dependency)
):
    """Get detailed information about a specific event"""

    event = db.query(Event).filter(Event.id == event_id, Event.is_active == True).first()
    if not event:
        # No real row — this may be a virtual instance the calendar handed out
        # (recurring webinar, planned class or assignment deadline). Rebuild it
        # read-only; 404 only if it matches no known virtual id.
        virtual = _reconstruct_virtual_event(db, event_id, current_user)
        if virtual is not None:
            return virtual
        raise HTTPException(status_code=404, detail="Event not found")
    
    # Check if user has access to this event
    if current_user.role == "admin":
        # Admins have full access
        pass
    else:
        user_group_ids = []
        user_course_ids = []
        
        if current_user.role == "student":
            user_groups = db.query(GroupStudent).filter(GroupStudent.student_id == current_user.id).all()
            user_group_ids = [ug.group_id for ug in user_groups]
            
            enrollments = db.query(Enrollment).filter(
                Enrollment.user_id == current_user.id,
                Enrollment.is_active == True
            ).all()
            user_course_ids = [e.course_id for e in enrollments]
            
        elif current_user.role in ["teacher", "curator"]:
            teacher_groups = db.query(Group).filter(Group.teacher_id == current_user.id).all()
            curator_groups = db.query(Group).filter(Group.curator_id == current_user.id).all()
            user_group_ids = [g.id for g in teacher_groups + curator_groups]
            
            # For teachers/curators, we might want to check courses they teach
            # For now, assuming if they are not admin, they are restricted to their groups
            # If we want to support course teachers seeing events:
            courses_taught = db.query(Course).filter(Course.teacher_id == current_user.id).all()
            user_course_ids = [c.id for c in courses_taught]
        
        # Check if event is associated with user's groups or courses
        
        event_groups = db.query(EventGroup).filter(EventGroup.event_id == event_id).all()
        event_group_ids = [eg.group_id for eg in event_groups]
        
        event_courses = db.query(EventCourse).filter(EventCourse.event_id == event_id).all()
        event_course_ids = [ec.course_id for ec in event_courses]
        
        has_group_access = any(group_id in user_group_ids for group_id in event_group_ids)
        has_course_access = any(course_id in user_course_ids for course_id in event_course_ids)
        
        if not (has_group_access or has_course_access):
            raise HTTPException(status_code=403, detail="Access denied to this event")
    
    # Enrich event data
    event_data = EventSchema.from_orm(event)
    
    # Add creator name
    creator = db.query(UserInDB).filter(UserInDB.id == event.created_by).first()
    event_data.creator_name = creator.name if creator else "Unknown"

    # Add teacher name
    if event.teacher_id:
        teacher = db.query(UserInDB).filter(UserInDB.id == event.teacher_id).first()
        event_data.teacher_name = teacher.name if teacher else None
    
    # Add group names
    event_data.groups = [eg.group.name for eg in event.event_groups if eg.group]
    
    # Add course names
    event_data.courses = [ec.course.title for ec in event.event_courses if ec.course]
    
    # Add group IDs
    event_data.group_ids = [eg.group_id for eg in event.event_groups]
    
    # Add course IDs
    event_data.course_ids = [ec.course_id for ec in event.event_courses]
    
    # Participant count from Attendance (single source of truth for class events)
    participant_count = AttendanceService.count_for_event(
        db, event.id, statuses=["present", "late", "absent", "registered"]
    )
    event_data.participant_count = participant_count
    
    return event_data

@router.post("/{event_id}/register")
def register_for_event(
    event_id: int,
    db: Session = Depends(get_db),
    current_user: UserInDB = Depends(get_current_user_dependency)
):
    """Register current user for an event (for webinars mainly)"""

    event = db.query(Event).filter(Event.id == event_id, Event.is_active == True).first()
    if not event:
        # Virtual (recurring/planned) instance — materialize a real row to hang
        # the registration off, then continue with its real id. Idempotent: a
        # second registration attempt reuses the already-materialized event.
        from src.services.event_service import EventService
        real_id = EventService.resolve_event_id(db, event_id, user_id=current_user.id)
        if real_id and real_id != event_id:
            event = db.query(Event).filter(Event.id == real_id, Event.is_active == True).first()
            event_id = real_id
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")

    # Check if user has access to this event
    user_group_ids = []
    if current_user.role == "student":
        user_groups = db.query(GroupStudent).filter(GroupStudent.student_id == current_user.id).all()
        user_group_ids = [ug.group_id for ug in user_groups]
    elif current_user.role in ["teacher", "curator"]:
        teacher_groups = db.query(Group).filter(Group.teacher_id == current_user.id).all()
        curator_groups = db.query(Group).filter(Group.curator_id == current_user.id).all()
        user_group_ids = [g.id for g in teacher_groups + curator_groups]
    elif current_user.role == "admin":
        user_group_ids = [g.id for g in db.query(Group).all()]
    
    event_groups = db.query(EventGroup).filter(EventGroup.event_id == event_id).all()
    event_group_ids = [eg.group_id for eg in event_groups]
    
    if not any(group_id in user_group_ids for group_id in event_group_ids):
        raise HTTPException(status_code=403, detail="Access denied to this event")
    
    # Check if already registered
    existing_registration = db.query(EventParticipant).filter(
        EventParticipant.event_id == event_id,
        EventParticipant.user_id == current_user.id
    ).first()
    
    if existing_registration:
        raise HTTPException(status_code=400, detail="Already registered for this event")
    
    # Check max participants limit
    if event.max_participants:
        current_participants = db.query(EventParticipant).filter(EventParticipant.event_id == event_id).count()
        if current_participants >= event.max_participants:
            raise HTTPException(status_code=400, detail="Event is full")
    
    # Create registration
    registration = EventParticipant(
        event_id=event_id,
        user_id=current_user.id,
        registration_status="registered"
    )
    
    db.add(registration)
    db.commit()

    # Return the resolved id so the client can swap a virtual/pseudo id for the
    # real materialized one (needed for a later unregister to target the row).
    return {"detail": "Successfully registered for event", "event_id": event_id}

@router.delete("/{event_id}/register")
def unregister_from_event(
    event_id: int,
    db: Session = Depends(get_db),
    current_user: UserInDB = Depends(get_current_user_dependency)
):
    """Unregister current user from an event"""
    
    registration = db.query(EventParticipant).filter(
        EventParticipant.event_id == event_id,
        EventParticipant.user_id == current_user.id
    ).first()
    
    if not registration:
        raise HTTPException(status_code=404, detail="Registration not found")
    
    db.delete(registration)
    db.commit()
    
    return {"detail": "Successfully unregistered from event"}

@router.post("/curator/create", response_model=EventSchema)
def create_curator_event(
    event_data: CreateEventRequest,
    db: Session = Depends(get_db),
    current_user: UserInDB = Depends(get_current_user_dependency)
):
    """Create a new event as curator for managed groups"""
    
    # Only curators and admins can create events
    if current_user.role not in ["curator", "admin", "head_curator"]:
        raise HTTPException(status_code=403, detail="Only curators and admins can create events")
    
    # Validate event type
    valid_types = ["class", "weekly_test", "webinar"]
    if event_data.event_type not in valid_types:
        raise HTTPException(status_code=400, detail=f"Invalid event type. Must be one of: {valid_types}")
    
    # Validate datetime
    if event_data.start_datetime >= event_data.end_datetime:
        raise HTTPException(status_code=400, detail="Start datetime must be before end datetime")
    
    # Remove special groups from selected groups
    eligible_groups = []
    if event_data.group_ids:
        existing_groups = db.query(Group).filter(Group.id.in_(event_data.group_ids)).all()
        if len(existing_groups) != len(event_data.group_ids):
            raise HTTPException(status_code=400, detail="One or more groups not found")
        eligible_groups = [group for group in existing_groups if not group.is_special]

    if event_data.group_ids and not eligible_groups and not event_data.course_ids:
        raise HTTPException(
            status_code=400,
            detail="Selected groups are special and cannot be used for events"
        )

    eligible_group_ids = [group.id for group in eligible_groups]

    # For curators: verify they manage all specified non-special groups
    if current_user.role == "curator" and eligible_group_ids:
        curator_groups = db.query(Group).filter(
            Group.curator_id == current_user.id,
            Group.id.in_(eligible_group_ids)
        ).all()
        
        if len(curator_groups) != len(eligible_group_ids):
            raise HTTPException(
                status_code=403, 
                detail="You can only create events for groups you manage"
            )

    groups = eligible_groups

    # Validate courses exist (optional)
    courses = []
    if event_data.course_ids:
        courses = db.query(Course).filter(Course.id.in_(event_data.course_ids)).all()
        if len(courses) != len(event_data.course_ids):
            raise HTTPException(status_code=400, detail="One or more courses not found")
    
    # Create event
    event = Event(
        title=event_data.title,
        description=event_data.description,
        event_type=event_data.event_type,
        start_datetime=event_data.start_datetime,
        end_datetime=event_data.end_datetime,
        location=event_data.location,
        is_online=event_data.is_online,
        meeting_url=event_data.meeting_url,
        created_by=current_user.id,
        is_recurring=event_data.is_recurring,
        recurrence_pattern=event_data.recurrence_pattern,
        recurrence_end_date=event_data.recurrence_end_date,
        max_participants=event_data.max_participants,
        teacher_id=event_data.teacher_id
    )
    
    db.add(event)
    db.flush()  # To get the event ID
    
    # Create event-group associations
    for group_id in eligible_group_ids:
        event_group = EventGroup(event_id=event.id, group_id=group_id)
        db.add(event_group)

    # Create event-course associations
    if event_data.course_ids:
        for course_id in event_data.course_ids:
            event_course = EventCourse(event_id=event.id, course_id=course_id)
            db.add(event_course)
    
    db.commit()
    db.refresh(event)
    
    # Return enriched event data
    result = EventSchema.from_orm(event)
    result.creator_name = current_user.name
    result.groups = [g.name for g in groups] if eligible_group_ids else []
    result.group_ids = eligible_group_ids
    result.course_ids = event_data.course_ids or []
    
    if event_data.course_ids:
        course_names = [c.title for c in courses]
        result.courses = course_names
    else:
        result.courses = []
    
    return result
@router.get("/{event_id}/participants", response_model=List[EventStudentSchema])
def get_event_participants(
    event_id: int,
    group_id: Optional[int] = Query(None),
    db: Session = Depends(get_db),
    current_user: UserInDB = Depends(require_teacher_curator_or_admin())
):
    """Get students for an event, optionally filtered by group"""
    # 1. Verify event exists
    event = db.query(Event).filter(Event.id == event_id).first()
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")

    if not check_event_access(event_id, current_user, db):
        raise HTTPException(status_code=403, detail="Access denied to this event")
        
    # 2. Determine target groups
    if group_id:
        target_group_ids = [group_id]
    else:
        target_group_ids = [eg.group_id for eg in event.event_groups]
        
    if not target_group_ids:
        return []
        
    # 3. Fetch students in these groups
    students = db.query(UserInDB).join(GroupStudent, GroupStudent.student_id == UserInDB.id).filter(
        GroupStudent.group_id.in_(target_group_ids)
    ).all()
    
    # 4. Fetch existing attendance records from Attendance (single source of truth)
    student_ids = [s.id for s in students]
    att_map = AttendanceService.get_attendance_map_for_events(db, [event_id], student_ids)
    # (user_id, event_id) -> {status, score, activity_score}

    # 5. Build results
    results = []
    for s in students:
        att = att_map.get((s.id, event_id))
        results.append(EventStudentSchema(
            student_id=s.id,
            name=s.name,
            attendance_status=attendance_status_to_ui(att["status"] if att else None),
            last_updated=None,
        ))
        
    return results

@router.post("/{event_id}/attendance")
def update_event_attendance(
    event_id: int,
    data: AttendanceBulkUpdateSchema,
    db: Session = Depends(get_db),
    current_user: UserInDB = Depends(require_teacher_curator_or_admin())
):
    """Bulk update attendance for an event"""
    # 1. Verify event exists
    event = db.query(Event).filter(Event.id == event_id).first()
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")

    if not check_event_access(event_id, current_user, db):
        raise HTTPException(status_code=403, detail="Access denied to this event")
        
    # 2. Bulk upsert via AttendanceService (single source of truth)
    updates = [
        {
            "user_id": record.student_id,
            "status": ep_status_to_attendance_status(record.status),
            "score": 1 if record.status in ("attended", "late") else 0,
        }
        for record in data.attendance
    ]
    AttendanceService.bulk_upsert_for_event(db, event_id, updates)
    db.commit()
    return {"message": "Attendance updated successfully"}
