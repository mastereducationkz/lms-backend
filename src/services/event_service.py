from datetime import datetime, timedelta, date, timezone
import calendar as cal_module
from typing import List, Optional
from sqlalchemy.orm import Session, joinedload
from sqlalchemy import or_

from src.schemas.models import Event, EventGroup, EventCourse, LessonSchedule

class EventService:
    @staticmethod
    def expand_recurring_events(
        db: Session, 
        start_date: datetime, 
        end_date: datetime, 
        group_ids: List[int] = [], 
        course_ids: List[int] = [],
        parent_events: Optional[List[Event]] = None,
        skip_class_events: bool = True  # Skip class events - use LessonSchedule instead
    ) -> List[Event]:
        """
        Generates virtual event instances for recurring events within a date range.
        For event_type='class', we skip expansion because LessonSchedule provides better data.
        """
        if not group_ids and not course_ids and not parent_events:
            return []

        # If parents not provided, fetch them
        if parent_events is None:
            query = db.query(Event).outerjoin(EventGroup).outerjoin(EventCourse).filter(
                Event.is_active == True,
                Event.is_recurring == True,
                Event.start_datetime <= end_date,
                or_(
                    Event.recurrence_end_date == None,
                    Event.recurrence_end_date >= start_date.date()
                )
            )
            
            # Skip class events if requested - they come from LessonSchedule
            if skip_class_events:
                query = query.filter(Event.event_type != "class")
            
            if group_ids or course_ids:
                query = query.filter(
                    or_(
                        EventGroup.group_id.in_(group_ids),
                        EventCourse.course_id.in_(course_ids)
                    )
                )
            
            parent_events = query.distinct().options(
                joinedload(Event.creator),
                joinedload(Event.event_groups).joinedload(EventGroup.group),
                joinedload(Event.event_courses).joinedload(EventCourse.course)
            ).all()

        generated_events = []

        def make_naive(dt: datetime) -> datetime:
            if dt.tzinfo is not None:
                return dt.replace(tzinfo=None)
            return dt

        start_date = make_naive(start_date)
        end_date = make_naive(end_date)

        for parent in parent_events:
            current_start = make_naive(parent.start_datetime)
            current_end = make_naive(parent.end_datetime)
            duration = current_end - current_start
            
            original_start_day = parent.start_datetime.day
            
            # Pre-extract group IDs from parent (relationships don't work on transient objects)
            parent_group_ids = [eg.group_id for eg in parent.event_groups] if parent.event_groups else []
            
            # Instance counter for this recurring event
            instance_counter = 1
            
            # Simple iteration (TODO: Optimize fast-forward if needed)
            while current_start <= end_date:
                # Check intersection
                if current_start >= start_date and current_start <= end_date:
                    # Create virtual event
                    # Check for collisions or override logic if needed
                    
                    # Generate pseudo-ID
                    pseudo_id = int(f"{parent.id}{int(current_start.timestamp())}") % 2147483647
                    
                    virtual_event = Event(
                        id=pseudo_id,
                        title=parent.title,
                        description=parent.description,
                        topic=parent.topic,
                        event_type=parent.event_type,
                        start_datetime=current_start,
                        end_datetime=current_start + duration,
                        location=parent.location,
                        is_online=parent.is_online,
                        meeting_url=parent.meeting_url,
                        created_by=parent.created_by,
                        is_recurring=True,
                        recurrence_pattern=parent.recurrence_pattern,
                        max_participants=parent.max_participants,
                        creator=parent.creator,
                        event_groups=parent.event_groups,  # Keep for first instance
                        created_at=parent.created_at,
                        updated_at=parent.updated_at,
                        is_active=True
                    )
                    # Store group_ids directly for deduplication (relationships don't copy to transient objects)
                    virtual_event._group_ids = parent_group_ids
                    # Store instance number
                    virtual_event._instance_number = instance_counter
                    
                    generated_events.append(virtual_event)
                    instance_counter += 1  # Increment after each generated event
                
                # Increment
                if parent.recurrence_pattern == "daily":
                    current_start += timedelta(days=1)
                elif parent.recurrence_pattern == "weekly":
                    current_start += timedelta(weeks=1)
                elif parent.recurrence_pattern == "biweekly":
                    current_start += timedelta(weeks=2)
                elif parent.recurrence_pattern == "monthly":
                    year = current_start.year + (current_start.month // 12)
                    month = (current_start.month % 12) + 1
                    day = min(original_start_day, cal_module.monthrange(year, month)[1])
                    current_start = current_start.replace(year=year, month=month, day=day)
                else:
                    break
                
                # Check end date
                if parent.recurrence_end_date and current_start.date() > parent.recurrence_end_date:
                    break
                    
        return generated_events
    @staticmethod
    def materialize_virtual_event(db: Session, pseudo_id: int) -> Optional[int]:
        """
        Scans all recurring events to find which one generated the given pseudo_id.
        If found, creates a real Event record in the database and returns its real ID.
        """
        # 1. Fetch all recurring events
        recurring_parents = db.query(Event).filter(
            Event.is_recurring == True,
            Event.is_active == True
        ).all()
        
        # 2. Search for the instance in a reasonable window (e.g. 3 months back, 6 months forward)
        now = datetime.now(timezone.utc)
        start_date = now - timedelta(days=90)
        end_date = now + timedelta(days=180)
        
        target_instance = None
        target_parent = None
        
        for parent in recurring_parents:
            # We don't need to load relationships for searching
            current_start = parent.start_datetime
            duration = parent.end_datetime - parent.start_datetime
            original_start_day = parent.start_datetime.day
            
            while current_start <= end_date:
                if current_start >= start_date:
                    # Check pseudo_id
                    p_id = int(f"{parent.id}{int(current_start.timestamp())}") % 2147483647
                    if p_id == pseudo_id:
                        target_instance = current_start
                        target_parent = parent
                        break
                
                # Increment
                if parent.recurrence_pattern == "daily":
                    current_start += timedelta(days=1)
                elif parent.recurrence_pattern == "weekly":
                    current_start += timedelta(weeks=1)
                elif parent.recurrence_pattern == "biweekly":
                    current_start += timedelta(weeks=2)
                elif parent.recurrence_pattern == "monthly":
                    year = current_start.year + (current_start.month // 12)
                    month = (current_start.month % 12) + 1
                    day = min(original_start_day, cal_module.monthrange(year, month)[1])
                    current_start = current_start.replace(year=year, month=month, day=day)
                else:
                    break
                
                if parent.recurrence_end_date and current_start.date() > parent.recurrence_end_date:
                    break
            
            if target_instance:
                break
                
        if not target_instance:
            return None
            
        # 3. Create real one-off event
        new_event = Event(
            title=target_parent.title,
            description=target_parent.description,
            event_type=target_parent.event_type,
            start_datetime=target_instance,
            end_datetime=target_instance + (target_parent.end_datetime - target_parent.start_datetime),
            location=target_parent.location,
            is_online=target_parent.is_online,
            meeting_url=target_parent.meeting_url,
            created_by=target_parent.created_by,
            is_recurring=False, # It's a specific materialized instance
            teacher_id=target_parent.teacher_id,
            is_active=True
        )
        db.add(new_event)
        db.flush()
        
        # Link to groups
        for eg in target_parent.event_groups:
            new_eg = EventGroup(event_id=new_event.id, group_id=eg.group_id)
            db.add(new_eg)
            
        # Link to courses
        for ec in target_parent.event_courses:
            new_ec = EventCourse(event_id=new_event.id, course_id=ec.course_id)
            db.add(new_ec)
            
        return new_event.id

    @staticmethod
    def materialize_lesson_schedule(db: Session, schedule_id: int, user_id: Optional[int] = None) -> Optional[int]:
        """
        Convert a virtual LessonSchedule into a real Event record.
        Returns the ID of the new (or existing) Event.
        """
        # Check if already linked to an event?
        # Actually LessonSchedule doesn't have a link to Event, Event has lesson_id.
        # But here we are creating a specific class instance.
        # Ideally we should prevent duplicates if one already exists for this schedule?
        # The uniqueness is (group_id, start_time).
        
        sched = db.query(LessonSchedule).filter(LessonSchedule.id == schedule_id).first()
        
        if not sched:
            return None
            
        # Check if event already exists for this group + time
        existing = db.query(Event).join(EventGroup).filter(
            EventGroup.group_id == sched.group_id,
            Event.start_datetime == sched.scheduled_at,
            Event.event_type == "class"
        ).first()
        
        if existing:
            return existing.id
            
        # Create real event
        lesson_title = sched.lesson.title if sched.lesson else f"Lesson {sched.id}"
        group_name = sched.group.name if sched.group else "Group"
        
        # Determine creator
        creator_id = user_id
        if not creator_id and sched.group:
            creator_id = sched.group.teacher_id
            
        # Fallback if still None (e.g. group has no teacher and no user passed)
        if not creator_id:
             from src.schemas.models import UserInDB
             # Try to find an admin if no teacher associated
             admin = db.query(UserInDB).filter(UserInDB.role == "admin").first()
             if admin:
                 creator_id = admin.id
        
        new_event = Event(
            title=f"{group_name}: {lesson_title}",
            description=f"Planned lesson: {lesson_title}",
            event_type="class",
            start_datetime=sched.scheduled_at,
            end_datetime=sched.scheduled_at + timedelta(minutes=60), # Default 1h
            location="Online (Scheduled)",
            is_online=True,
            meeting_url="",
            is_recurring=False,
            teacher_id=sched.group.teacher_id if sched.group else None,
            created_by=creator_id,
            is_active=True
        )
        db.add(new_event)
        db.flush()
        
        # Link to group
        eg = EventGroup(event_id=new_event.id, group_id=sched.group_id)
        db.add(eg)
        
        return new_event.id

    @staticmethod
    def resolve_event_id(db: Session, event_id: Optional[int], user_id: Optional[int] = None) -> Optional[int]:
        """
        Check if event_id exists, or try to materialize it if it's virtual.
        """
        if not event_id:
            return None
            
        # 1. Check if exists
        exists = db.query(Event.id).filter(Event.id == event_id).first()
        if exists:
            return event_id
            
        # 2. Check if it's a virtual LessonSchedule ID
        if event_id >= 2000000000:
            schedule_id = event_id - 2000000000
            return EventService.materialize_lesson_schedule(db, schedule_id, user_id=user_id)

        # 3. Try to materialize from recurring
        return EventService.materialize_virtual_event(db, event_id)
