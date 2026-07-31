from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from sqlalchemy import desc, or_, and_
from typing import List, Optional
from datetime import datetime
import logging

from src.config import get_db
from src.schemas.models import (
    Message, UserInDB, Course, Enrollment,
    MessageSchema, SendMessageSchema
)
from src.messages.schemas import ReportMessageSchema
from src.routes.auth import get_current_user_dependency
from src.utils.permissions import check_student_access
from src.schemas.models import GroupStudent
from src.utils.push_notifications import send_message_notification, send_message_push_to_user

logger = logging.getLogger(__name__)
router = APIRouter()

# =============================================================================
# MESSAGE MANAGEMENT (Встроенный чат)
# =============================================================================

@router.get("/", response_model=List[MessageSchema])
def get_messages(
    with_user_id: Optional[int] = None,
    course_id: Optional[int] = None,
    skip: int = Query(0, ge=0),
    limit: int = Query(50, le=200),
    current_user: UserInDB = Depends(get_current_user_dependency),
    db: Session = Depends(get_db)
):
    """
    Получить сообщения для текущего пользователя
    - Студенты могут общаться только с учителями/кураторами
    - Учителя могут общаться с учениками из своих курсов
    - Кураторы могут общаться с учениками из своих групп
    - Админы могут общаться со всеми
    """
    
    query = db.query(Message).filter(
        or_(
            Message.from_user_id == current_user.id,
            Message.to_user_id == current_user.id
        )
    )
    
    # Фильтр по конкретному пользователю
    if with_user_id:
        # Проверим права доступа к этому пользователю
        if not can_communicate_with_user(current_user, with_user_id, db):
            raise HTTPException(status_code=403, detail="Cannot communicate with this user")
        
        query = query.filter(
            or_(
                and_(Message.from_user_id == current_user.id, Message.to_user_id == with_user_id),
                and_(Message.from_user_id == with_user_id, Message.to_user_id == current_user.id)
            )
        )
    
    # Фильтр по курсу (для учителей - показать сообщения с учениками этого курса)
    if course_id and current_user.role in ["teacher", "curator"]:
        # Получить учеников курса
        student_ids = db.query(Enrollment.user_id).filter(
            Enrollment.course_id == course_id,
            Enrollment.is_active == True
        )
        
        query = query.filter(
            or_(
                and_(Message.from_user_id == current_user.id, Message.to_user_id.in_(student_ids)),
                and_(Message.from_user_id.in_(student_ids), Message.to_user_id == current_user.id)
            )
        )
    
    messages = query.order_by(desc(Message.created_at)).offset(skip).limit(limit).all()
    
    # Добавляем имена отправителей и получателей
    enriched_messages = []
    for message in messages:
        sender = db.query(UserInDB).filter(UserInDB.id == message.from_user_id).first()
        recipient = db.query(UserInDB).filter(UserInDB.id == message.to_user_id).first()
        
        message_data = MessageSchema.from_orm(message)
        message_data.sender_name = sender.name if sender else "Unknown"
        message_data.recipient_name = recipient.name if recipient else "Unknown"
        
        enriched_messages.append(message_data)
    
    return enriched_messages

@router.post("/", response_model=MessageSchema)
def send_message(
    message_data: SendMessageSchema,
    current_user: UserInDB = Depends(get_current_user_dependency),
    db: Session = Depends(get_db)
):
    """Отправить сообщение другому пользователю"""
    
    # Проверить, что получатель существует
    recipient = db.query(UserInDB).filter(UserInDB.id == message_data.to_user_id).first()
    if not recipient:
        raise HTTPException(status_code=404, detail="Recipient not found")
    
    # Проверить права на отправку сообщений этому пользователю
    if not can_communicate_with_user(current_user, message_data.to_user_id, db):
        raise HTTPException(status_code=403, detail="Cannot send message to this user")
    
    # Require text or an attachment
    content = message_data.content.strip()
    if not content and not message_data.file_url:
        raise HTTPException(status_code=400, detail="Message must have content or an attachment")

    # Создать сообщение
    new_message = Message(
        from_user_id=current_user.id,
        to_user_id=message_data.to_user_id,
        content=content,
        file_url=message_data.file_url,
    )
    
    db.add(new_message)
    db.commit()
    db.refresh(new_message)
    
    # Возвращаем с именами
    message_response = MessageSchema.from_orm(new_message)
    message_response.sender_name = current_user.name
    message_response.recipient_name = recipient.name
    
    # Push to all of the recipient's active devices (multi-device aware)
    send_message_push_to_user(
        db,
        recipient_id=recipient.id,
        sender_name=current_user.name,
        message_preview=content or "📎 Attachment",
        partner_id=current_user.id,
    )
    
    # Создать уведомление для получателя
    create_message_notification(new_message, db)
    
    return message_response

@router.put("/{message_id}/read")
def mark_message_as_read(
    message_id: int,
    current_user: UserInDB = Depends(get_current_user_dependency),
    db: Session = Depends(get_db)
):
    """Отметить сообщение как прочитанное"""
    
    message = db.query(Message).filter(Message.id == message_id).first()
    if not message:
        raise HTTPException(status_code=404, detail="Message not found")
    
    # Только получатель может отмечать сообщение как прочитанное
    if message.to_user_id != current_user.id:
        raise HTTPException(status_code=403, detail="Access denied")
    
    message.is_read = True
    db.commit()
    
    return {"detail": "Message marked as read"}

@router.put("/mark-all-read/{partner_id}")
def mark_all_messages_as_read(
    partner_id: int,
    current_user: UserInDB = Depends(get_current_user_dependency),
    db: Session = Depends(get_db)
):
    """Отметить все сообщения от конкретного пользователя как прочитанные"""
    
    # Проверить права доступа к этому пользователю
    if not can_communicate_with_user(current_user, partner_id, db):
        raise HTTPException(status_code=403, detail="Cannot access messages from this user")
    
    # Отметить все непрочитанные сообщения от этого пользователя как прочитанные
    unread_messages = db.query(Message).filter(
        Message.from_user_id == partner_id,
        Message.to_user_id == current_user.id,
        Message.is_read == False
    ).all()
    
    for message in unread_messages:
        message.is_read = True
    
    db.commit()
    
    return {"detail": f"Marked {len(unread_messages)} messages as read"}

@router.get("/conversations", response_model=List[dict])
def get_conversations(
    current_user: UserInDB = Depends(get_current_user_dependency),
    db: Session = Depends(get_db)
):
    """
    Получить список всех разговоров (чатов) пользователя
    Возвращает список пользователей, с которыми ведется переписка
    """
    
    # Получить все сообщения пользователя
    user_messages = db.query(Message).filter(
        or_(
            Message.from_user_id == current_user.id,
            Message.to_user_id == current_user.id
        )
    ).all()
    
    # Собрать уникальных собеседников
    conversation_partners = set()
    for message in user_messages:
        if message.from_user_id == current_user.id:
            conversation_partners.add(message.to_user_id)
        else:
            conversation_partners.add(message.from_user_id)
    
    # Подготовить данные о разговорах
    conversations = []
    for partner_id in conversation_partners:
        partner = db.query(UserInDB).filter(UserInDB.id == partner_id).first()
        if not partner:
            continue
        
        # Последнее сообщение в разговоре
        last_message = db.query(Message).filter(
            or_(
                and_(Message.from_user_id == current_user.id, Message.to_user_id == partner_id),
                and_(Message.from_user_id == partner_id, Message.to_user_id == current_user.id)
            )
        ).order_by(desc(Message.created_at)).first()
        
        # Количество непрочитанных сообщений от этого партнера
        unread_count = db.query(Message).filter(
            Message.from_user_id == partner_id,
            Message.to_user_id == current_user.id,
            Message.is_read == False
        ).count()
        
        conversations.append({
            "partner_id": partner_id,
            "partner_name": partner.name,
            "partner_role": partner.role,
            "partner_avatar": partner.avatar_url,
            "last_message": {
                "content": last_message.content if last_message else "",
                "created_at": last_message.created_at if last_message else None,
                "from_me": last_message.from_user_id == current_user.id if last_message else False
            },
            "unread_count": unread_count
        })
    
    # Сортируем по времени последнего сообщения
    conversations.sort(
        key=lambda x: x["last_message"]["created_at"] or datetime.min,
        reverse=True
    )
    
    return conversations

@router.get("/unread-count")
def get_unread_message_count(
    current_user: UserInDB = Depends(get_current_user_dependency),
    db: Session = Depends(get_db)
):
    """Получить количество непрочитанных сообщений"""
    
    unread_count = db.query(Message).filter(
        Message.to_user_id == current_user.id,
        Message.is_read == False
    ).count()
    
    return {"unread_count": unread_count}


@router.get("/reports")
def list_message_reports(
    current_user: UserInDB = Depends(get_current_user_dependency),
    db: Session = Depends(get_db)
):
    """List reported messages for admin review."""
    from src.messages.models import MessageReport

    if current_user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")

    reports = (
        db.query(MessageReport)
        .order_by(desc(MessageReport.created_at))
        .limit(500)
        .all()
    )
    return [
        {
            "id": r.id,
            "message_id": r.message_id,
            "message_content": r.message.content if r.message else None,
            "message_sender_id": r.message.from_user_id if r.message else None,
            "reporter_id": r.reporter_id,
            "reporter_name": r.reporter.name if r.reporter else None,
            "reason": r.reason,
            "status": r.status,
            "created_at": r.created_at,
        }
        for r in reports
    ]


@router.post("/{message_id}/report")
def report_message(
    message_id: int,
    payload: ReportMessageSchema,
    current_user: UserInDB = Depends(get_current_user_dependency),
    db: Session = Depends(get_db)
):
    """Flag a message for admin review. Only conversation participants may report."""
    from src.messages.models import MessageReport

    message = db.query(Message).filter(Message.id == message_id).first()
    if not message:
        raise HTTPException(status_code=404, detail="Message not found")

    if current_user.id not in (message.from_user_id, message.to_user_id):
        raise HTTPException(status_code=403, detail="Only conversation participants can report a message")

    existing = db.query(MessageReport).filter(
        MessageReport.message_id == message_id,
        MessageReport.reporter_id == current_user.id,
    ).first()
    if existing:
        return {"status": "reported", "report_id": existing.id}

    report = MessageReport(
        message_id=message_id,
        reporter_id=current_user.id,
        reason=payload.reason,
    )
    db.add(report)
    db.commit()
    db.refresh(report)
    logger.info("message_report created id=%s message_id=%s reporter_id=%s", report.id, message_id, current_user.id)
    return {"status": "reported", "report_id": report.id}


def _parent_staff_ids(db: Session, parent_id: int) -> set:
    """Teacher + curator user ids across all of a parent's children (their groups'
    teacher/curator + enrolled-course teachers)."""
    from src.schemas.models import Group, GroupStudent, ParentStudent, Enrollment, Course
    child_ids = [ps.student_id for ps in db.query(ParentStudent).filter(
        ParentStudent.parent_id == parent_id).all()]
    if not child_ids:
        return set()
    staff: set = set()
    group_ids = [gs.group_id for gs in db.query(GroupStudent).filter(
        GroupStudent.student_id.in_(child_ids)).all()]
    if group_ids:
        for g in db.query(Group).filter(Group.id.in_(group_ids)).all():
            if g.teacher_id:
                staff.add(g.teacher_id)
            if g.curator_id:
                staff.add(g.curator_id)
    for (tid,) in db.query(Course.teacher_id).join(Enrollment).filter(
        Enrollment.user_id.in_(child_ids),
        Enrollment.is_active == True,
        Course.teacher_id.isnot(None),
    ).distinct().all():
        if tid:
            staff.add(tid)
    return staff


@router.get("/available-contacts")
def get_available_contacts(
    role_filter: Optional[str] = None,
    current_user: UserInDB = Depends(get_current_user_dependency),
    db: Session = Depends(get_db)
):
    """
    Получить список пользователей, с которыми можно начать чат
    Основано на правах доступа и ролях
    """
    
    available_contacts = []
    
    if current_user.role == "student":
        from src.schemas.models import Group, GroupStudent

        is_special_group_student = db.query(GroupStudent).join(
            Group, GroupStudent.group_id == Group.id
        ).filter(
            GroupStudent.student_id == current_user.id,
            Group.is_special == True
        ).first() is not None

        if is_special_group_student:
            admins = db.query(UserInDB).filter(
                UserInDB.role == "admin",
                UserInDB.is_active == True
            ).all()
            for admin in admins:
                available_contacts.append({
                    "user_id": admin.id,
                    "name": admin.name,
                    "role": admin.role,
                    "avatar_url": admin.avatar_url
                })
            available_contacts.sort(key=lambda x: x["name"])
            return {"available_contacts": available_contacts}

        # Студенты могут писать учителям и кураторам своих курсов/групп
        
        # Учителя курсов, на которые записан студент
        teacher_ids = db.query(Course.teacher_id).join(Enrollment).filter(
            Enrollment.user_id == current_user.id,
            Enrollment.is_active == True
        ).distinct().all()
        
        teachers = db.query(UserInDB).filter(
            UserInDB.id.in_([t[0] for t in teacher_ids]),
            UserInDB.is_active == True
        ).all()
        
        for teacher in teachers:
            available_contacts.append({
                "user_id": teacher.id,
                "name": teacher.name,
                "role": teacher.role,
                "avatar_url": teacher.avatar_url
            })
        
        # Учителя из групп студента
        group_student = db.query(GroupStudent).filter(
            GroupStudent.student_id == current_user.id
        ).first()
        
        if group_student:
            group_teacher = db.query(UserInDB).filter(
                UserInDB.id == group_student.group.teacher_id,
                UserInDB.is_active == True
            ).first()
            
            if group_teacher and not any(contact["user_id"] == group_teacher.id for contact in available_contacts):
                available_contacts.append({
                    "user_id": group_teacher.id,
                    "name": group_teacher.name,
                    "role": group_teacher.role,
                    "avatar_url": group_teacher.avatar_url
                })
        
        # Кураторы из той же группы
        # First get the student's group(s)
        student_groups = db.query(GroupStudent.group_id).filter(
            GroupStudent.student_id == current_user.id
        ).all()
        
        if student_groups:
            group_ids = [group[0] for group in student_groups]
            
            # Get curators from the same groups
            # Get curators from the same groups
            curator_ids_query = db.query(Group.curator_id).filter(
                Group.id.in_(group_ids),
                Group.curator_id.isnot(None)
            )
            
            curators = db.query(UserInDB).filter(
                UserInDB.role == "curator",
                UserInDB.id.in_(curator_ids_query),
                UserInDB.is_active == True
            ).all()
            

            
            for curator in curators:
                if not any(contact["user_id"] == curator.id for contact in available_contacts):
                    available_contacts.append({
                        "user_id": curator.id,
                        "name": curator.name,
                        "role": curator.role,
                        "avatar_url": curator.avatar_url
                    })

        # Администраторы (всегда доступны студентам)
        admins = db.query(UserInDB).filter(
            UserInDB.role == "admin",
            UserInDB.is_active == True
        ).all()
        for admin in admins:
            if not any(contact["user_id"] == admin.id for contact in available_contacts):
                available_contacts.append({
                    "user_id": admin.id,
                    "name": admin.name,
                    "role": admin.role,
                    "avatar_url": admin.avatar_url
                })
    
    elif current_user.role == "teacher":
        # Учителя могут писать всем студентам и другим учителям
        all_students = db.query(UserInDB).filter(
            UserInDB.role == "student",
            UserInDB.is_active == True
        ).all()
        
        all_teachers = db.query(UserInDB).filter(
            UserInDB.role == "teacher",
            UserInDB.id != current_user.id,
            UserInDB.is_active == True
        ).all()
        
        all_curators = db.query(UserInDB).filter(
            UserInDB.role == "curator",
            UserInDB.is_active == True
        ).all()
        
        # Добавляем студентов
        for student in all_students:
            available_contacts.append({
                "user_id": student.id,
                "name": student.name,
                "role": student.role,
                "avatar_url": student.avatar_url,
                "student_id": student.student_id
            })
        
        # Добавляем учителей
        for teacher in all_teachers:
            available_contacts.append({
                "user_id": teacher.id,
                "name": teacher.name,
                "role": teacher.role,
                "avatar_url": teacher.avatar_url
            })
        
        # Добавляем кураторов
        for curator in all_curators:
            available_contacts.append({
                "user_id": curator.id,
                "name": curator.name,
                "role": curator.role,
                "avatar_url": curator.avatar_url
            })
    
    elif current_user.role == "curator":
        # Кураторы могут писать ученикам из своей группы
        # Get groups where current_user is curator
        from src.schemas.models import Group, GroupStudent
        curator_groups = db.query(Group).filter(
            Group.curator_id == current_user.id
        ).all()
        
        if curator_groups:
            group_ids = [group.id for group in curator_groups]
            
            # Get students in curator's groups
            group_student_ids = db.query(GroupStudent.student_id).filter(
                GroupStudent.group_id.in_(group_ids)
            )
            
            students = db.query(UserInDB).filter(
                UserInDB.role == "student",
                UserInDB.id.in_(group_student_ids),
                UserInDB.is_active == True
            ).all()
            
            for student in students:
                available_contacts.append({
                    "user_id": student.id,
                    "name": student.name,
                    "role": student.role,
                    "avatar_url": student.avatar_url,
                    "student_id": student.student_id
                })
    
    elif current_user.role == "admin":
        # Админы могут писать всем
        all_users = db.query(UserInDB).filter(
            UserInDB.id != current_user.id,
            UserInDB.is_active == True
        ).all()
        
        for user in all_users:
            available_contacts.append({
                "user_id": user.id,
                "name": user.name,
                "role": user.role,
                "avatar_url": user.avatar_url,
                "student_id": user.student_id if user.role == "student" else None
            })

    elif current_user.role == "parent":
        # Родители могут писать учителям/кураторам своих детей и администраторам.
        staff_ids = _parent_staff_ids(db, current_user.id)
        if staff_ids:
            for u in db.query(UserInDB).filter(
                UserInDB.id.in_(staff_ids), UserInDB.is_active == True
            ).all():
                available_contacts.append({
                    "user_id": u.id, "name": u.name, "role": u.role, "avatar_url": u.avatar_url
                })
        for admin in db.query(UserInDB).filter(
            UserInDB.role == "admin", UserInDB.is_active == True
        ).all():
            if not any(c["user_id"] == admin.id for c in available_contacts):
                available_contacts.append({
                    "user_id": admin.id, "name": admin.name, "role": admin.role, "avatar_url": admin.avatar_url
                })

    # Фильтрация по роли, если указана
    if role_filter:
        available_contacts = [
            contact for contact in available_contacts 
            if contact["role"] == role_filter
        ]
    
    # Сортировка по имени
    available_contacts.sort(key=lambda x: x["name"])
    
    return {"available_contacts": available_contacts}

# =============================================================================
# HELPER FUNCTIONS
# =============================================================================

def can_communicate_with_user(current_user: UserInDB, target_user_id: int, db: Session) -> bool:
    """
    Проверить, может ли текущий пользователь общаться с целевым пользователем
    Основано на ролях и связях (курсы, группы)
    """
    
    if current_user.role == "admin":
        return True  # Админы могут общаться со всеми
    
    target_user = db.query(UserInDB).filter(UserInDB.id == target_user_id).first()
    if not target_user or not target_user.is_active:
        return False
    
    # Все пользователи могут общаться с администраторами
    if target_user.role == "admin":
        return True

    # Родитель ↔ учителя/кураторы его детей (в обе стороны).
    if current_user.role == "parent":
        return target_user_id in _parent_staff_ids(db, current_user.id)
    if target_user.role == "parent":
        return current_user.id in _parent_staff_ids(db, target_user_id)

    if current_user.role == "student":
        from src.schemas.models import Group, GroupStudent

        is_special_group_student = db.query(GroupStudent).join(
            Group, GroupStudent.group_id == Group.id
        ).filter(
            GroupStudent.student_id == current_user.id,
            Group.is_special == True
        ).first() is not None

        if is_special_group_student:
            return target_user.role == "admin"

        # Студенты могут общаться с учителями/кураторами своих курсов/групп
        if target_user.role in ["teacher", "curator"]:
            # Проверяем, есть ли общие курсы с учителем
            if target_user.role == "teacher":
                # Сначала проверяем курсы
                common_courses = db.query(Course).join(Enrollment).filter(
                    Course.teacher_id == target_user_id,
                    Enrollment.user_id == current_user.id,
                    Enrollment.is_active == True
                ).first()
                if common_courses is not None:
                    return True
                
                # Если нет общих курсов, проверяем группы
                group_student = db.query(GroupStudent).filter(
                    GroupStudent.student_id == current_user.id
                ).first()
                
                if group_student:
                    teacher_group = db.query(Group).filter(
                        Group.id == group_student.group_id,
                        Group.teacher_id == target_user_id
                    ).first()
                    return teacher_group is not None
            
            # Проверяем, в одной ли группе с куратором
            if target_user.role == "curator":
                # Get student's group
                student_group = db.query(GroupStudent).filter(
                    GroupStudent.student_id == current_user.id
                ).first()
                
                if not student_group:
                    return False
                
                # Get curator's group(s)
                curator_group = db.query(Group).filter(
                    Group.id == student_group.group_id,
                    Group.curator_id == target_user_id
                ).first()
                
                return curator_group is not None
        
        return False
    
    elif current_user.role == "teacher":
        # Учителя могут общаться со всеми студентами, учителями, кураторами и администраторами
        if target_user.role in ["student", "teacher", "curator", "admin"]:
            return True
        
        return False
    
    elif current_user.role == "curator":
        # Кураторы могут общаться с учениками из своей группы и с администраторами
        if target_user.role == "student":
            from src.schemas.models import Group, GroupStudent
            # Get groups where current_user is curator
            curator_groups = db.query(Group).filter(
                Group.curator_id == current_user.id
            ).all()
            
            if not curator_groups:
                return False
            
            curator_group_ids = [g.id for g in curator_groups]
            
            # Check if target student is in any of curator's groups
            student_in_group = db.query(GroupStudent).filter(
                GroupStudent.student_id == target_user_id,
                GroupStudent.group_id.in_(curator_group_ids)
            ).first()
            
            return student_in_group is not None
        
        return False
    
    return False

def create_message_notification(message: Message, db: Session):
    """Создать уведомление о новом сообщении"""
    from src.schemas.models import Notification
    
    sender = db.query(UserInDB).filter(UserInDB.id == message.from_user_id).first()
    
    notification = Notification(
        user_id=message.to_user_id,
        title="Новое сообщение",
        content=f"Новое сообщение от {sender.name if sender else 'Неизвестный пользователь'}",
        notification_type="message",
        related_id=message.id
    )
    
    db.add(notification)
    db.commit()

