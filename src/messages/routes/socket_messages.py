import socketio
from fastapi import FastAPI
from sqlalchemy.orm import Session
from sqlalchemy import desc, or_, and_
from typing import List, Optional
from datetime import datetime
import logging
import os

from src.config import get_db
from src.schemas.models import (
    Message, UserInDB, Course, Enrollment,
    MessageSchema, SendMessageSchema
)
from src.routes.auth import verify_token
from src.routes.messages import can_communicate_with_user, create_message_notification
from src.schemas.models import GroupStudent
from src.utils.push_notifications import send_message_push_to_user

logger = logging.getLogger(__name__)

# Socket.IO CORS origins. Includes Expo/React Native dev origins; extend via the
# EXTRA_CORS_ORIGINS env var (comma-separated) for LAN testing without code changes.
_SOCKET_CORS_ORIGINS = [
    "http://localhost:3000", "http://localhost:5174", "http://localhost:5173",
    "https://mastereducation.kz", "https://lms.mastereducation.kz", "https://lms-master.vercel.app",
    "http://localhost:8081", "http://localhost:19006", "exp://localhost:8081",
]
_SOCKET_CORS_ORIGINS += [o.strip() for o in os.getenv("EXTRA_CORS_ORIGINS", "").split(",") if o.strip()]

# Create Socket.IO server
sio = socketio.AsyncServer(
    async_mode='asgi',
    cors_allowed_origins=_SOCKET_CORS_ORIGINS,
    logger=False,
    engineio_logger=False,
    # Optimized connection settings for better stability
    ping_timeout=60,  # Increased from default 20s
    ping_interval=25,  # Keep default 25s
    max_http_buffer_size=1000000,  # 1MB buffer
    allow_upgrades=True,
    transports=['polling', 'websocket']
)

USER_ROOM_PREFIX = "user:"

def _get_user_id_from_environ(environ, auth=None) -> int | None:
    # Try Socket.IO auth payload first
    if auth and isinstance(auth, dict):
        token = auth.get('token')
        if token:
            payload = verify_token(token)
            if payload:
                uid = payload.get('user_id')
                if uid:
                    try:
                        return int(uid)
                    except (TypeError, ValueError):
                        pass
    
    # Try Authorization header
    headers = environ.get('asgi.scope', {}).get('headers') or []
    token = None
    for k, v in headers:
        if k == b'authorization':
            try:
                scheme, bearer = v.decode().split(' ', 1)
                if scheme.lower() == 'bearer':
                    token = bearer
            except Exception:
                pass
            break
    # Fallback to query string token
    if token is None:
        query_string = environ.get('QUERY_STRING') or environ.get('asgi.scope', {}).get('query_string')
        if isinstance(query_string, bytes):
            query_string = query_string.decode()
        if isinstance(query_string, str) and 'token=' in query_string:
            for part in query_string.split('&'):
                if part.startswith('token='):
                    token = part.split('=', 1)[1]
                    break
    if not token:
        return None
    payload = verify_token(token)
    if not payload:
        return None
    uid = payload.get('user_id')
    if not uid:
        return None
    try:
        return int(uid)
    except (TypeError, ValueError):
        return None

def _resolve_user_id(session_data, db: Session) -> int | None:
    raw = session_data.get('user_id') if session_data else None
    if isinstance(raw, int):
        return raw
    # If token saved email by mistake, resolve to numeric id
    if isinstance(raw, str):
        try:
            return int(raw)
        except ValueError:
            # Try treat as email
            user = db.query(UserInDB).filter(UserInDB.email == raw).first()
            return user.id if user else None
    return None

async def _emit_threads_update(user_id: int):
    """Emit threads update to user's room"""
    await sio.emit('threads:update', to=f"{USER_ROOM_PREFIX}{user_id}")

async def _emit_unread_update(user_id: int):
    """Emit unread count update to user's room"""
    await sio.emit('unread:update', to=f"{USER_ROOM_PREFIX}{user_id}")

async def emit_unseen_graded_update(user_id: int):
    """Emit unseen graded count update to user's room"""
    await sio.emit('unseen_graded:update', to=f"{USER_ROOM_PREFIX}{user_id}")

# Socket.IO Events
@sio.event
async def connect(sid, environ, auth):
    # Use the proper function to get user_id from token
    user_id = _get_user_id_from_environ(environ, auth)
    if not user_id:
        logger.debug(f"Connection rejected for sid {sid}: Invalid token")
        await sio.disconnect(sid)
        return
    
    await sio.save_session(sid, { 'user_id': user_id })
    await sio.enter_room(sid, f"{USER_ROOM_PREFIX}{user_id}")

@sio.event
async def disconnect(sid):
    session = await sio.get_session(sid)
    user_id = session.get('user_id') if session else None
    # Rooms get auto-cleaned on disconnect
    return

@sio.on('message:send')
async def handle_message_send(sid, data):
    session = await sio.get_session(sid)
    db: Session = next(get_db())
    from_user_id = _resolve_user_id(session, db)
    to_user_id = int(data.get('to_user_id')) if data and data.get('to_user_id') is not None else None
    content = (data.get('content') or '').strip()
    file_url = (data.get('file_url') or None)
    if not from_user_id or not to_user_id or (not content and not file_url):
        await sio.emit('message:error', { 'detail': 'Invalid payload' }, to=sid)
        return
    try:
        # Authorization
        current_user = db.query(UserInDB).filter(UserInDB.id == from_user_id).first()
        if not current_user or not can_communicate_with_user(current_user, to_user_id, db):
            await sio.emit('message:error', { 'detail': 'Access denied' }, to=sid)
            return
        
        # Create message
        new_message = Message(
            from_user_id=from_user_id,
            to_user_id=to_user_id,
            content=content,
            file_url=file_url
        )
        db.add(new_message)
        db.commit()
        db.refresh(new_message)
        
        # Enrich with names
        sender = db.query(UserInDB).filter(UserInDB.id == from_user_id).first()
        recipient = db.query(UserInDB).filter(UserInDB.id == to_user_id).first()
        
        message_data = {
            'id': new_message.id,
            'from_user_id': new_message.from_user_id,
            'to_user_id': new_message.to_user_id,
            'content': new_message.content,
            'file_url': new_message.file_url,
            'is_read': new_message.is_read,
            'created_at': new_message.created_at.isoformat(),
            'sender_name': sender.name if sender else 'Unknown',
            'recipient_name': recipient.name if recipient else 'Unknown'
        }
        
        # Emit to both users
        await sio.emit('message:new', message_data, to=f"{USER_ROOM_PREFIX}{from_user_id}")
        await sio.emit('message:new', message_data, to=f"{USER_ROOM_PREFIX}{to_user_id}")
        
        # Update threads for both users
        await _emit_threads_update(from_user_id)
        await _emit_threads_update(to_user_id)
        
        # Update unread count for recipient
        await _emit_unread_update(to_user_id)
        
        # Create notification
        create_message_notification(new_message, db)

        # Push to all of the recipient's active devices
        send_message_push_to_user(
            db,
            recipient_id=to_user_id,
            sender_name=sender.name if sender else 'Someone',
            message_preview=content or '📎 Attachment',
            partner_id=from_user_id,
        )
        
    except Exception as e:
        logger.error(f"Error sending message: {e}")
        await sio.emit('message:error', { 'detail': 'Internal server error' }, to=sid)
    finally:
        db.close()

@sio.on('message:read')
async def handle_message_read(sid, data):
    session = await sio.get_session(sid)
    db: Session = next(get_db())
    user_id = _resolve_user_id(session, db)
    message_id = int(data.get('message_id')) if data and data.get('message_id') is not None else None
    if not user_id or not message_id:
        return
    try:
        msg = db.query(Message).filter(Message.id == message_id).first()
        if not msg or msg.to_user_id != user_id:
            return
        if not msg.is_read:
            msg.is_read = True
            db.commit()
            
            # Emit update to both users
            message_data = {
                'id': msg.id,
                'from_user_id': msg.from_user_id,
                'to_user_id': msg.to_user_id,
                'content': msg.content,
                'is_read': msg.is_read,
                'created_at': msg.created_at.isoformat()
            }
            await sio.emit('message:updated', message_data, to=f"{USER_ROOM_PREFIX}{msg.from_user_id}")
            await sio.emit('message:updated', message_data, to=f"{USER_ROOM_PREFIX}{msg.to_user_id}")
            
            # Update unread count for reader
            await _emit_unread_update(user_id)
    except Exception as e:
        logger.error(f"Error marking message as read: {e}")
    finally:
        db.close()

@sio.on('message:read-all')
async def handle_message_read_all(sid, data):
    session = await sio.get_session(sid)
    db: Session = next(get_db())
    user_id = _resolve_user_id(session, db)
    partner_id = int(data.get('partner_id')) if data and data.get('partner_id') is not None else None
    if not user_id or not partner_id:
        return
    try:
        msgs = db.query(Message).filter(
            Message.from_user_id == partner_id,
            Message.to_user_id == user_id,
            Message.is_read == False
        ).all()
        
        if msgs:
            for m in msgs:
                m.is_read = True
            db.commit()
            
            # Emit bulk update to both users
            message_ids = [msg.id for msg in msgs]
            await sio.emit('message:bulk-updated', { 'message_ids': message_ids }, to=f"{USER_ROOM_PREFIX}{user_id}")
            await sio.emit('message:bulk-updated', { 'message_ids': message_ids }, to=f"{USER_ROOM_PREFIX}{partner_id}")
            
            # Update unread count and threads for both users
            await _emit_unread_update(user_id)
            await _emit_unread_update(partner_id)
            await _emit_threads_update(user_id)
            await _emit_threads_update(partner_id)
    except Exception as e:
        logger.error(f"Error marking all messages as read: {e}")
    finally:
        db.close()

@sio.on('threads:get')
async def handle_threads_get(sid):
    session = await sio.get_session(sid)
    db: Session = next(get_db())
    user_id = _resolve_user_id(session, db)
    if not user_id:
        return []
    try:
        # Fetch messages involving the user
        user_messages = db.query(Message).filter(
            (Message.from_user_id == user_id) | (Message.to_user_id == user_id)
        ).all()
        
        # Collect unique conversation partners
        conversation_partners = set()
        for message in user_messages:
            if message.from_user_id == user_id:
                conversation_partners.add(message.to_user_id)
            else:
                conversation_partners.add(message.from_user_id)
        
        # Prepare conversation data
        conversations = []
        for partner_id in conversation_partners:
            partner = db.query(UserInDB).filter(UserInDB.id == partner_id).first()
            if not partner:
                continue
            
            # Last message in conversation
            last_message = db.query(Message).filter(
                or_(
                    and_(Message.from_user_id == user_id, Message.to_user_id == partner_id),
                    and_(Message.from_user_id == partner_id, Message.to_user_id == user_id)
                )
            ).order_by(desc(Message.created_at)).first()
            
            # Unread count from this partner
            unread_count = db.query(Message).filter(
                Message.from_user_id == partner_id,
                Message.to_user_id == user_id,
                Message.is_read == False
            ).count()
            
            conversations.append({
                "partner_id": partner_id,
                "partner_name": partner.name,
                "partner_role": partner.role,
                "partner_avatar": partner.avatar_url,
                "last_message": {
                    "content": last_message.content if last_message else "",
                    "created_at": last_message.created_at.isoformat() if last_message else None,
                    "from_me": last_message.from_user_id == user_id if last_message else False
                },
                "unread_count": unread_count
            })
        
        # Sort by last message time
        conversations.sort(
            key=lambda x: x["last_message"]["created_at"] or datetime.min.isoformat(),
            reverse=True
        )
        
        return conversations
    except Exception as e:
        logger.error(f"Error getting threads: {e}")
        return []
    finally:
        db.close()

@sio.on('messages:get')
async def handle_messages_get(sid, data):
    session = await sio.get_session(sid)
    db: Session = next(get_db())
    current_user_id = _resolve_user_id(session, db)
    partner_id = int(data.get('with_user_id')) if data and data.get('with_user_id') is not None else None
    if not current_user_id:
        return []
    try:
        query = db.query(Message).filter(
            (Message.from_user_id == current_user_id) | (Message.to_user_id == current_user_id)
        )
        if partner_id:
            current_user = db.get(UserInDB, current_user_id)
            if not can_communicate_with_user(current_user, partner_id, db):
                return []
            query = query.filter(
                or_(
                    and_(Message.from_user_id == current_user_id, Message.to_user_id == partner_id),
                    and_(Message.from_user_id == partner_id, Message.to_user_id == current_user_id)
                )
            )
        
        messages = query.order_by(desc(Message.created_at)).limit(50).all()
        
        # Enrich with names
        enriched_messages = []
        for message in messages:
            sender = db.query(UserInDB).filter(UserInDB.id == message.from_user_id).first()
            recipient = db.query(UserInDB).filter(UserInDB.id == message.to_user_id).first()
            
            message_data = {
                'id': message.id,
                'from_user_id': message.from_user_id,
                'to_user_id': message.to_user_id,
                'content': message.content,
                'is_read': message.is_read,
                'created_at': message.created_at.isoformat(),
                'sender_name': sender.name if sender else 'Unknown',
                'recipient_name': recipient.name if recipient else 'Unknown'
            }
            enriched_messages.append(message_data)
        
        return enriched_messages
    except Exception as e:
        logger.error(f"Error getting messages: {e}")
        return []
    finally:
        db.close()

@sio.on('contacts:get')
async def handle_contacts_get(sid, data=None):
    session = await sio.get_session(sid)
    db: Session = next(get_db())
    current_user_id = _resolve_user_id(session, db)
    if not current_user_id:
        return []
    try:
        current_user = db.query(UserInDB).filter(UserInDB.id == current_user_id).first()
        if not current_user:
            return []
        
        available_contacts = []
        
        if current_user.role == "student":
            # Students can write to teachers and curators of their courses/groups and admins
            
            # Teachers of courses the student is enrolled in
            teacher_ids = db.query(Course.teacher_id).join(Enrollment).filter(
                Enrollment.user_id == current_user_id,
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
            
            # Teachers from student's groups
            group_student = db.query(GroupStudent).filter(
                GroupStudent.student_id == current_user_id
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
            
            # Curators from the same group
            # Get groups where student is a member
            student_groups = db.query(GroupStudent.group_id).filter(
                GroupStudent.student_id == current_user_id
            ).all()
            
            if student_groups:
                student_group_ids = [sg.group_id for sg in student_groups]
                # Get students in these groups (peers) - logic seems to want curators though
                # Original logic: curators of the group
                curators = db.query(UserInDB).join(Group, Group.curator_id == UserInDB.id).filter(
                    Group.id.in_(student_group_ids),
                UserInDB.role.in_(["curator", "head_curator"]),
                UserInDB.is_active == True
            ).distinct().all()
                
                for curator in curators:
                    available_contacts.append({
                        "user_id": curator.id,
                        "name": curator.name,
                        "role": curator.role,
                        "avatar_url": curator.avatar_url
                    })
            
            # Admins (always available to students)
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
            # Teachers can write to all students, teachers and curators
            all_students = db.query(UserInDB).filter(
                UserInDB.role == "student",
                UserInDB.is_active == True
            ).all()
            
            all_teachers = db.query(UserInDB).filter(
                UserInDB.role == "teacher",
                UserInDB.id != current_user_id,
                UserInDB.is_active == True
            ).all()
            
            all_curators = db.query(UserInDB).filter(
                UserInDB.role.in_(["curator", "head_curator"]),
                UserInDB.is_active == True
            ).all()
            
            # Add students
            for student in all_students:
                available_contacts.append({
                    "user_id": student.id,
                    "name": student.name,
                    "role": student.role,
                    "avatar_url": student.avatar_url,
                    "student_id": student.student_id
                })
            
            # Add teachers
            for teacher in all_teachers:
                available_contacts.append({
                    "user_id": teacher.id,
                    "name": teacher.name,
                    "role": teacher.role,
                    "avatar_url": teacher.avatar_url
                })
            
            # Add curators
            for curator in all_curators:
                available_contacts.append({
                    "user_id": curator.id,
                    "name": curator.name,
                    "role": curator.role,
                    "avatar_url": curator.avatar_url
                })
        
        elif current_user.role in ["curator", "head_curator"]:
            # Curators can write to students from their groups
            if current_user.role == "head_curator":
                # Head curators can write to all students? Or maybe just like admin?
                # Let's give them access to everyone to be safe, like admin
                all_users = db.query(UserInDB).filter(
                    UserInDB.id != current_user_id,
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
                return {"available_contacts": available_contacts}
            
            curator_groups = db.query(Group).filter(Group.curator_id == current_user_id).all()
            
            if curator_groups:
                curator_group_ids = [g.id for g in curator_groups]
                group_student_ids = db.query(GroupStudent.student_id).filter(
                    GroupStudent.group_id.in_(curator_group_ids)
                ).subquery()
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
            # Admins can write to everyone
            all_users = db.query(UserInDB).filter(
                UserInDB.id != current_user_id,
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
        
        # Sort by name
        available_contacts.sort(key=lambda x: x["name"])
        
        return {"available_contacts": available_contacts}
    except Exception as e:
        logger.error(f"Error getting contacts: {e}")
        return {"available_contacts": []}
    finally:
        db.close()

@sio.on('unread:count')
async def handle_unread_count(sid):
    session = await sio.get_session(sid)
    db: Session = next(get_db())
    user_id = _resolve_user_id(session, db)
    if not user_id:
        return {"unread_count": 0}
    try:
        unread_count = db.query(Message).filter(
            Message.to_user_id == user_id,
            Message.is_read == False
        ).count()
        
        return {"unread_count": unread_count}
    except Exception as e:
        logger.error(f"Error getting unread count: {e}")
        return {"unread_count": 0}
    finally:
        db.close()

def create_socket_app(app: FastAPI):
    """Create Socket.IO app wrapper"""
    return socketio.ASGIApp(sio, other_asgi_app=app, socketio_path='/ws/socket.io')
