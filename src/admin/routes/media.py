from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form
from sqlalchemy.orm import Session
from typing import List, Optional
import re
import os
import json
from pathlib import Path
from datetime import datetime
import aiofiles

from src.config import get_db
from src.schemas.models import UserInDB, LessonMaterial, Lesson, Module, Course, Assignment, Group
from src.routes.auth import get_current_user_dependency
from src.utils.permissions import require_teacher_or_admin
from pydantic import BaseModel

router = APIRouter()

# =============================================================================
# MEDIA SCHEMAS
# =============================================================================

class YouTubeVideoSchema(BaseModel):
    title: str
    youtube_url: str
    description: Optional[str] = None
    duration_minutes: int = 0

class VideoUploadResponse(BaseModel):
    video_id: str
    title: str
    youtube_url: str
    embed_url: str
    thumbnail_url: str
    duration_minutes: int
    is_valid: bool

class MaterialUploadResponse(BaseModel):
    material_id: int
    title: str
    file_type: str
    file_url: str
    file_size_bytes: Optional[int] = None
# =============================================================================
# COURSE THUMBNAIL MANAGEMENT
# =============================================================================

class ThumbnailUrlSchema(BaseModel):
    url: str


@router.post("/courses/{course_id}/thumbnail")
async def upload_course_thumbnail(
    course_id: int,
    file: UploadFile = File(...),
    current_user: UserInDB = Depends(require_teacher_or_admin()),
    db: Session = Depends(get_db)
):
    """Upload a thumbnail image file for a course and update cover_image_url"""
    course = db.query(Course).filter(Course.id == course_id).first()
    if not course:
        raise HTTPException(status_code=404, detail="Course not found")

    # Only course teacher or admin can update
    if current_user.role != "admin" and course.teacher_id != current_user.id:
        raise HTTPException(status_code=403, detail="Access denied")

    allowed_types = {
        "image/jpeg": "jpg",
        "image/png": "png",
        "image/gif": "gif",
        "image/webp": "webp",
    }

    if file.content_type not in allowed_types:
        raise HTTPException(status_code=400, detail=f"Unsupported image type: {file.content_type}")

    ext = allowed_types[file.content_type]
    upload_dir = Path("uploads/courses/thumbnails")
    upload_dir.mkdir(parents=True, exist_ok=True)

    safe_filename = f"course_{course_id}_thumb.{ext}"
    file_path = upload_dir / safe_filename

    try:
        async with aiofiles.open(file_path, "wb") as buffer:
            content = await file.read()
            await buffer.write(content)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to save thumbnail: {str(e)}")

    public_url = f"/uploads/courses/thumbnails/{safe_filename}"

    # Update course
    course.cover_image_url = public_url
    db.commit()
    db.refresh(course)

    return {"cover_image_url": public_url}


@router.post("/upload")
async def upload_file(
    file: UploadFile = File(...),
    file_type: str = Form(...),
    current_user: UserInDB = Depends(get_current_user_dependency),
    db: Session = Depends(get_db)
):
    """Upload a general file and return the file URL"""
    
    # Validate file type
    allowed_types = {
        "teacher_assignment": ["pdf", "docx", "doc", "jpg", "jpeg", "png", "gif", "txt"],
        "assignment": ["pdf", "docx", "doc", "jpg", "jpeg", "png", "gif", "txt"],
        "submission": ["pdf", "docx", "doc", "jpg", "jpeg", "png", "gif", "txt"],
        "step_attachment": ["pdf", "docx", "doc", "jpg", "jpeg", "png", "gif", "txt", "zip", "xlsx", "pptx"],
        "question_media": ["pdf", "jpg", "jpeg", "png", "gif", "webp", "mp3", "wav", "ogg", "m4a"]  # For quiz question attachments and audio
    }
    
    if file_type not in allowed_types:
        raise HTTPException(status_code=400, detail=f"Unsupported file type: {file_type}")
    
    # Get file extension
    file_extension = file.filename.split('.')[-1].lower() if '.' in file.filename else ''
    if file_extension not in allowed_types[file_type]:
        raise HTTPException(status_code=400, detail=f"Unsupported file extension: {file_extension}")
    
    # Create upload directory
    upload_dir = Path(f"uploads/{file_type}")
    upload_dir.mkdir(parents=True, exist_ok=True)
    
    # Generate unique filename
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_filename = f"{file_type}_{timestamp}_{current_user.id}_{file.filename}"
    file_path = upload_dir / safe_filename
    
    try:
        content = await file.read()
        async with aiofiles.open(file_path, "wb") as buffer:
            await buffer.write(content)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to save file: {str(e)}")
    
    public_url = f"/uploads/{file_type}/{safe_filename}"
    
    return {
        "file_url": public_url,
        "filename": safe_filename,
        "original_filename": file.filename,
        "file_size": len(content)
    }

@router.post("/steps/{step_id}/attachments")
async def upload_step_attachment(
    step_id: int,
    file: UploadFile = File(...),
    current_user: UserInDB = Depends(require_teacher_or_admin()),
    db: Session = Depends(get_db)
):
    """Upload a file attachment for a lesson step"""
    from src.schemas.models import Step
    
    step = db.query(Step).filter(Step.id == step_id).first()
    if not step:
        raise HTTPException(status_code=404, detail="Step not found")
    
    # Check access permissions through lesson -> module -> course
    lesson = db.query(Lesson).filter(Lesson.id == step.lesson_id).first()
    module = db.query(Module).filter(Module.id == lesson.module_id).first()
    course = db.query(Course).filter(Course.id == module.course_id).first()
    
    if current_user.role != "admin" and course.teacher_id != current_user.id:
        raise HTTPException(status_code=403, detail="Access denied")
    
    # Validate file type
    allowed_types = {
        "application/pdf": "pdf",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
        "application/msword": "doc",
        "image/jpeg": "jpg",
        "image/png": "png",
        "image/gif": "gif",
        "image/webp": "webp",
        "text/plain": "txt",
        "application/zip": "zip",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "xlsx",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation": "pptx"
    }
    
    if file.content_type not in allowed_types:
        raise HTTPException(
            status_code=400, 
            detail=f"File type {file.content_type} not allowed. Allowed: {list(allowed_types.values())}"
        )
    
    file_type = allowed_types[file.content_type]
    
    # Create upload directory
    upload_dir = Path("uploads/step_attachments")
    upload_dir.mkdir(parents=True, exist_ok=True)
    
    # Generate safe filename
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_filename = f"step_{step_id}_{timestamp}_{file.filename}"
    file_path = upload_dir / safe_filename
    
    # Save file
    try:
        content = await file.read()
        async with aiofiles.open(file_path, "wb") as buffer:
            await buffer.write(content)
        
        file_size = len(content)
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to save file: {str(e)}")
    
    # Update step attachments
    import json
    current_attachments = json.loads(step.attachments) if step.attachments else []
    
    new_attachment = {
        "id": len(current_attachments) + 1,
        "filename": file.filename,
        "file_url": f"/uploads/step_attachments/{safe_filename}",
        "file_type": file_type,
        "file_size": file_size,
        "uploaded_at": datetime.now().isoformat()
    }
    
    current_attachments.append(new_attachment)
    step.attachments = json.dumps(current_attachments)
    
    db.commit()
    db.refresh(step)
    
    return {
        "attachment_id": new_attachment["id"],
        "filename": new_attachment["filename"],
        "file_url": new_attachment["file_url"],
        "file_type": new_attachment["file_type"],
        "file_size": new_attachment["file_size"]
    }

@router.delete("/steps/{step_id}/attachments/{attachment_id}")
async def delete_step_attachment(
    step_id: int,
    attachment_id: int,
    current_user: UserInDB = Depends(require_teacher_or_admin()),
    db: Session = Depends(get_db)
):
    """Delete a file attachment from a lesson step"""
    from src.schemas.models import Step
    
    step = db.query(Step).filter(Step.id == step_id).first()
    if not step:
        raise HTTPException(status_code=404, detail="Step not found")
    
    # Check access permissions
    lesson = db.query(Lesson).filter(Lesson.id == step.lesson_id).first()
    module = db.query(Module).filter(Module.id == lesson.module_id).first()
    course = db.query(Course).filter(Course.id == module.course_id).first()
    
    if current_user.role != "admin" and course.teacher_id != current_user.id:
        raise HTTPException(status_code=403, detail="Access denied")
    
    # Parse current attachments
    import json
    current_attachments = json.loads(step.attachments) if step.attachments else []
    
    # Find and remove the attachment
    attachment_to_remove = None
    for i, attachment in enumerate(current_attachments):
        if attachment["id"] == attachment_id:
            attachment_to_remove = current_attachments.pop(i)
            break
    
    if not attachment_to_remove:
        raise HTTPException(status_code=404, detail="Attachment not found")
    
    # Delete file from disk
    try:
        file_path = Path(f".{attachment_to_remove['file_url']}")
        if file_path.exists():
            file_path.unlink()
    except Exception as e:
        print(f"Warning: Could not delete file {attachment_to_remove['file_url']}: {e}")
    
    # Update step attachments
    step.attachments = json.dumps(current_attachments)
    db.commit()
    
    return {"detail": "Attachment deleted successfully"}
@router.put("/courses/{course_id}/thumbnail-url")
async def set_course_thumbnail_url(
    course_id: int,
    data: ThumbnailUrlSchema,
    current_user: UserInDB = Depends(require_teacher_or_admin()),
    db: Session = Depends(get_db)
):
    """Set course thumbnail by external URL (no download)"""
    course = db.query(Course).filter(Course.id == course_id).first()
    if not course:
        raise HTTPException(status_code=404, detail="Course not found")

    if current_user.role != "admin" and course.teacher_id != current_user.id:
        raise HTTPException(status_code=403, detail="Access denied")

    url = data.url.strip()
    if not url.startswith("http://") and not url.startswith("https://"):
        raise HTTPException(status_code=400, detail="Invalid URL")

    course.cover_image_url = url
    db.commit()
    db.refresh(course)

    return {"cover_image_url": url}


# =============================================================================
# YOUTUBE VIDEO MANAGEMENT
# =============================================================================

@router.post("/videos/youtube", response_model=VideoUploadResponse)
async def add_youtube_video(
    video_data: YouTubeVideoSchema,
    current_user: UserInDB = Depends(require_teacher_or_admin()),
    db: Session = Depends(get_db)
):
    """
    Добавить YouTube видео для использования в уроках
    Валидирует ссылку и извлекает ID видео
    """
    
    # Валидируем и извлекаем YouTube ID
    video_info = validate_and_extract_youtube_info(video_data.youtube_url)
    
    if not video_info["is_valid"]:
        raise HTTPException(status_code=400, detail="Invalid YouTube URL")
    
    response = VideoUploadResponse(
        video_id=video_info["video_id"],
        title=video_data.title,
        youtube_url=video_info["clean_url"],
        embed_url=video_info["embed_url"],
        thumbnail_url=video_info["thumbnail_url"],
        duration_minutes=video_data.duration_minutes,
        is_valid=True
    )
    
    return response

@router.get("/videos/youtube/validate")
async def validate_youtube_url(
    url: str,
    current_user: UserInDB = Depends(get_current_user_dependency)
):
    """Валидировать YouTube ссылку и получить информацию о видео"""

    video_info = validate_and_extract_youtube_info(url)
    
    if not video_info["is_valid"]:
        raise HTTPException(status_code=400, detail="Invalid YouTube URL")
    
    return {
        "is_valid": True,
        "video_id": video_info["video_id"],
        "clean_url": video_info["clean_url"],
        "embed_url": video_info["embed_url"],
        "thumbnail_url": video_info["thumbnail_url"],
        "title_suggestion": f"Видео {video_info['video_id'][:8]}..."
    }

@router.put("/lessons/{lesson_id}/video")
async def update_lesson_video(
    lesson_id: int,
    video_data: YouTubeVideoSchema,
    current_user: UserInDB = Depends(require_teacher_or_admin()),
    db: Session = Depends(get_db)
):
    """Обновить видео урока с YouTube ссылкой"""
    
    lesson = db.query(Lesson).filter(Lesson.id == lesson_id).first()
    if not lesson:
        raise HTTPException(status_code=404, detail="Lesson not found")
    
    # Проверяем права доступа через курс
    module = db.query(Module).filter(Module.id == lesson.module_id).first()
    course = db.query(Course).filter(Course.id == module.course_id).first()
    
    if current_user.role != "admin" and course.teacher_id != current_user.id:
        raise HTTPException(status_code=403, detail="Access denied")
    
    # Валидируем YouTube ссылку
    video_info = validate_and_extract_youtube_info(video_data.youtube_url)
    if not video_info["is_valid"]:
        raise HTTPException(status_code=400, detail="Invalid YouTube URL")
    
    # Обновляем урок
    lesson.title = video_data.title
    lesson.description = video_data.description
    lesson.video_url = video_info["clean_url"]
    lesson.duration_minutes = video_data.duration_minutes
    lesson.content_type = "video"
    
    db.commit()
    db.refresh(lesson)
    
    return {
        "detail": "Lesson video updated successfully",
        "lesson_id": lesson_id,
        "video_url": video_info["clean_url"],
        "embed_url": video_info["embed_url"],
        "thumbnail_url": video_info["thumbnail_url"]
    }

# =============================================================================
# FILE UPLOAD FOR MATERIALS
# =============================================================================

@router.post("/materials/upload", response_model=MaterialUploadResponse)
async def upload_lesson_material(
    lesson_id: int = Form(...),
    title: str = Form(...),
    file: UploadFile = File(...),
    current_user: UserInDB = Depends(require_teacher_or_admin()),
    db: Session = Depends(get_db)
):
    """
    Загрузить файл материала для урока (PDF, DOCX, изображения)
    """
    
    lesson = db.query(Lesson).filter(Lesson.id == lesson_id).first()
    if not lesson:
        raise HTTPException(status_code=404, detail="Lesson not found")
    
    # Проверяем права доступа
    module = db.query(Module).filter(Module.id == lesson.module_id).first()
    course = db.query(Course).filter(Course.id == module.course_id).first()
    
    if current_user.role != "admin" and course.teacher_id != current_user.id:
        raise HTTPException(status_code=403, detail="Access denied")
    
    # Валидируем тип файла
    allowed_types = {
        "application/pdf": "pdf",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
        "application/msword": "doc",
        "image/jpeg": "jpg",
        "image/png": "png",
        "image/gif": "gif",
        "text/plain": "txt"
    }
    
    if file.content_type not in allowed_types:
        raise HTTPException(
            status_code=400, 
            detail=f"File type {file.content_type} not allowed. Allowed: {list(allowed_types.values())}"
        )
    
    file_type = allowed_types[file.content_type]
    
    # Создаем директорию для загрузок
    upload_dir = Path("uploads/materials")
    upload_dir.mkdir(parents=True, exist_ok=True)
    
    # Генерируем безопасное имя файла
    safe_filename = f"lesson_{lesson_id}_{len(lesson.materials) + 1}_{file.filename}"
    file_path = upload_dir / safe_filename
    
    # Сохраняем файл
    try:
        content = await file.read()
        async with aiofiles.open(file_path, "wb") as buffer:
            await buffer.write(content)
        
        file_size = len(content)
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to save file: {str(e)}")
    
    # Создаем запись в базе данных
    material = LessonMaterial(
        lesson_id=lesson_id,
        title=title,
        file_type=file_type,
        file_url=f"/uploads/materials/{safe_filename}",
        file_size_bytes=file_size
    )
    
    db.add(material)
    db.commit()
    db.refresh(material)
    
    return MaterialUploadResponse(
        material_id=material.id,
        title=material.title,
        file_type=material.file_type,
        file_url=material.file_url,
        file_size_bytes=material.file_size_bytes
    )

@router.get("/materials/{material_id}")
async def get_material_info(
    material_id: int,
    current_user: UserInDB = Depends(get_current_user_dependency),
    db: Session = Depends(get_db)
):
    """Получить информацию о материале"""
    
    material = db.query(LessonMaterial).filter(LessonMaterial.id == material_id).first()
    if not material:
        raise HTTPException(status_code=404, detail="Material not found")
    
    # Проверяем доступ через урок
    lesson = db.query(Lesson).filter(Lesson.id == material.lesson_id).first()
    module = db.query(Module).filter(Module.id == lesson.module_id).first()
    
    # Проверяем права доступа к курсу
    from src.utils.permissions import check_course_access
    if not check_course_access(module.course_id, current_user, db):
        raise HTTPException(status_code=403, detail="Access denied to this material")
    
    return {
        "material_id": material.id,
        "title": material.title,
        "file_type": material.file_type,
        "file_url": material.file_url,
        "file_size_bytes": material.file_size_bytes,
        "created_at": material.created_at,
        "lesson_id": material.lesson_id,
        "lesson_title": lesson.title
    }

@router.delete("/materials/{material_id}")
async def delete_material(
    material_id: int,
    current_user: UserInDB = Depends(require_teacher_or_admin()),
    db: Session = Depends(get_db)
):
    """Удалить материал урока"""
    
    material = db.query(LessonMaterial).filter(LessonMaterial.id == material_id).first()
    if not material:
        raise HTTPException(status_code=404, detail="Material not found")
    
    # Проверяем права доступа
    lesson = db.query(Lesson).filter(Lesson.id == material.lesson_id).first()
    module = db.query(Module).filter(Module.id == lesson.module_id).first()
    course = db.query(Course).filter(Course.id == module.course_id).first()
    
    if current_user.role != "admin" and course.teacher_id != current_user.id:
        raise HTTPException(status_code=403, detail="Access denied")
    
    # Удаляем файл с диска
    try:
        file_path = Path(f".{material.file_url}")
        if file_path.exists():
            file_path.unlink()
    except Exception as e:
        print(f"Warning: Could not delete file {material.file_url}: {e}")
    
    # Удаляем запись из БД
    db.delete(material)
    db.commit()
    
    return {"detail": "Material deleted successfully"}

# =============================================================================
# MEDIA LIBRARY
# =============================================================================

@router.get("/library")
async def get_media_library(
    lesson_id: Optional[int] = None,
    file_type: Optional[str] = None,
    current_user: UserInDB = Depends(get_current_user_dependency),
    db: Session = Depends(get_db)
):
    """Получить библиотеку медиа файлов доступных пользователю"""
    
    # Базовый запрос материалов
    query = db.query(LessonMaterial)
    
    # Если указан конкретный урок
    if lesson_id:
        query = query.filter(LessonMaterial.lesson_id == lesson_id)
    
    # Фильтр по типу файла
    if file_type:
        query = query.filter(LessonMaterial.file_type == file_type)
    
    # Фильтрация по доступным курсам в зависимости от роли
    if current_user.role == "student":
        # Студенты видят только материалы своих курсов
        from src.schemas.models import Enrollment
        enrolled_course_ids = db.query(Course.id).join(Enrollment).filter(
            Enrollment.user_id == current_user.id,
            Enrollment.is_active == True
        ).subquery()
        
        lesson_ids = db.query(Lesson.id).join(Module).filter(
            Module.course_id.in_(enrolled_course_ids)
        ).subquery()
        
        query = query.filter(LessonMaterial.lesson_id.in_(lesson_ids))
        
    elif current_user.role == "teacher":
        # Учителя видят материалы своих курсов
        teacher_course_ids = db.query(Course.id).filter(Course.teacher_id == current_user.id).subquery()
        lesson_ids = db.query(Lesson.id).join(Module).filter(
            Module.course_id.in_(teacher_course_ids)
        ).subquery()
        
        query = query.filter(LessonMaterial.lesson_id.in_(lesson_ids))
    
    materials = query.all()
    
    # Группируем по урокам
    library = {}
    for material in materials:
        lesson = db.query(Lesson).filter(Lesson.id == material.lesson_id).first()
        module = db.query(Module).filter(Module.id == lesson.module_id).first()
        course = db.query(Course).filter(Course.id == module.course_id).first()
        
        course_key = f"course_{course.id}"
        if course_key not in library:
            library[course_key] = {
                "course_id": course.id,
                "course_title": course.title,
                "lessons": {}
            }
        
        lesson_key = f"lesson_{lesson.id}"
        if lesson_key not in library[course_key]["lessons"]:
            library[course_key]["lessons"][lesson_key] = {
                "lesson_id": lesson.id,
                "lesson_title": lesson.title,
                "video_url": lesson.video_url,
                "materials": []
            }
        
        library[course_key]["lessons"][lesson_key]["materials"].append({
            "material_id": material.id,
            "title": material.title,
            "file_type": material.file_type,
            "file_url": material.file_url,
            "file_size_bytes": material.file_size_bytes,
            "created_at": material.created_at
        })
    
    return {"library": library}

# =============================================================================
# ASSIGNMENT FILE UPLOAD
# =============================================================================

@router.post("/assignments/upload")
async def upload_assignment_file(
    assignment_id: int = Form(...),
    file: UploadFile = File(...),
    current_user: UserInDB = Depends(require_teacher_or_admin()),
    db: Session = Depends(get_db)
):
    """
    Загрузить файл для задания (PDF, DOCX, изображения)
    """
    
    from src.schemas.models import Assignment
    
    assignment = db.query(Assignment).filter(Assignment.id == assignment_id).first()
    if not assignment:
        raise HTTPException(status_code=404, detail="Assignment not found")
    
    # Проверяем права доступа
    if assignment.lesson_id:
        lesson = db.query(Lesson).filter(Lesson.id == assignment.lesson_id).first()
        module = db.query(Module).filter(Module.id == lesson.module_id).first()
        course = db.query(Course).filter(Course.id == module.course_id).first()
        
        if current_user.role != "admin" and course.teacher_id != current_user.id:
            raise HTTPException(status_code=403, detail="Access denied")
    elif assignment.group_id:
        group = db.query(Group).filter(Group.id == assignment.group_id).first()
        if current_user.role != "admin" and group.teacher_id != current_user.id:
            raise HTTPException(status_code=403, detail="Access denied")
    
    # Валидируем тип файла
    allowed_types = {
        "application/pdf": "pdf",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
        "application/msword": "doc",
        "image/jpeg": "jpg",
        "image/png": "png",
        "image/gif": "gif",
        "text/plain": "txt"
    }
    
    if file.content_type not in allowed_types:
        raise HTTPException(
            status_code=400, 
            detail=f"File type {file.content_type} not allowed. Allowed: {list(allowed_types.values())}"
        )
    
    # Создаем директорию для загрузок
    upload_dir = Path("uploads/assignments")
    upload_dir.mkdir(parents=True, exist_ok=True)
    
    # Генерируем безопасное имя файла
    safe_filename = f"assignment_{assignment_id}_{file.filename}"
    file_path = upload_dir / safe_filename
    
    # Сохраняем файл
    try:
        content = await file.read()
        async with aiofiles.open(file_path, "wb") as buffer:
            await buffer.write(content)
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to save file: {str(e)}")
    
    # Обновляем задание
    assignment.file_url = f"/uploads/assignments/{safe_filename}"
    db.commit()
    db.refresh(assignment)
    
    return {
        "assignment_id": assignment.id,
        "file_url": assignment.file_url,
        "filename": file.filename
    }

@router.post("/submissions/upload")
async def upload_submission_file(
    assignment_id: int = Form(...),
    file: UploadFile = File(...),
    current_user: UserInDB = Depends(get_current_user_dependency),
    db: Session = Depends(get_db)
):
    """
    Загрузить файл ответа на задание (только для студентов)
    """
    
    if current_user.role != "student":
        raise HTTPException(status_code=403, detail="Only students can submit assignments")
    
    from src.schemas.models import Assignment, AssignmentSubmission
    
    assignment = db.query(Assignment).filter(Assignment.id == assignment_id).first()
    if not assignment:
        raise HTTPException(status_code=404, detail="Assignment not found")
    
    # Проверяем доступ к заданию
    if assignment.lesson_id:
        lesson = db.query(Lesson).filter(Lesson.id == assignment.lesson_id).first()
        module = db.query(Module).filter(Module.id == lesson.module_id).first()
        
        from src.utils.permissions import check_course_access
        if not check_course_access(module.course_id, current_user, db):
            raise HTTPException(status_code=403, detail="Access denied to this assignment")
    elif assignment.group_id:
        # Проверяем, состоит ли студент в группе
        from src.schemas.models import GroupStudent
        group_member = db.query(GroupStudent).filter(
            GroupStudent.group_id == assignment.group_id,
            GroupStudent.student_id == current_user.id
        ).first()
        if not group_member:
            raise HTTPException(status_code=403, detail="Access denied to this assignment")
    
    # Проверяем, не просрочено ли задание
    if assignment.due_date and assignment.due_date < datetime.utcnow():
        raise HTTPException(status_code=400, detail="Assignment deadline has passed")
    
    # Валидируем тип файла
    allowed_types = assignment.allowed_file_types or ["pdf", "docx", "doc", "jpg", "png", "gif", "txt"]
    
    file_extension = file.filename.split('.')[-1].lower() if '.' in file.filename else ''
    if file_extension not in allowed_types:
        raise HTTPException(
            status_code=400, 
            detail=f"File type {file_extension} not allowed. Allowed: {allowed_types}"
        )
    
    # Проверяем размер файла
    content = await file.read()
    file_size_mb = len(content) / (1024 * 1024)
    if file_size_mb > assignment.max_file_size_mb:
        raise HTTPException(
            status_code=400, 
            detail=f"File size {file_size_mb:.1f}MB exceeds maximum allowed size of {assignment.max_file_size_mb}MB"
        )
    
    # Создаем директорию для загрузок
    upload_dir = Path("uploads/submissions")
    upload_dir.mkdir(parents=True, exist_ok=True)
    
    # Генерируем безопасное имя файла
    safe_filename = f"submission_{assignment_id}_{current_user.id}_{file.filename}"
    file_path = upload_dir / safe_filename
    
    # Сохраняем файл
    try:
        async with aiofiles.open(file_path, "wb") as buffer:
            await buffer.write(content)
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to save file: {str(e)}")
    
    # Just return the file URL without creating a submission record
    # The submission will be created when the assignment is actually submitted
    
    return {
        "file_url": f"/uploads/submissions/{safe_filename}",
        "filename": file.filename
    }

@router.get("/files/{file_type}/{filename:path}")
async def download_file(
    file_type: str,
    filename: str,
    current_user: UserInDB = Depends(get_current_user_dependency),
    db: Session = Depends(get_db)
):
    """
    Скачать файл с проверкой прав доступа
    file_type: assignments, submissions, materials
    """
    
    from fastapi.responses import FileResponse
    from pathlib import Path
    
    file_path = Path(f"uploads/{file_type}/{filename}")
    
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="File not found")
    
    # Проверяем права доступа в зависимости от типа файла
    if file_type == "assignments":
        # Извлекаем assignment_id из имени файла
        assignment_id = int(filename.split('_')[1])
        from src.schemas.models import Assignment
        
        assignment = db.query(Assignment).filter(Assignment.id == assignment_id).first()
        if not assignment:
            raise HTTPException(status_code=404, detail="Assignment not found")
        
        # Проверяем доступ к заданию
        if assignment.lesson_id:
            lesson = db.query(Lesson).filter(Lesson.id == assignment.lesson_id).first()
            module = db.query(Module).filter(Module.id == lesson.module_id).first()
            
            from src.utils.permissions import check_course_access
            if not check_course_access(module.course_id, current_user, db):
                raise HTTPException(status_code=403, detail="Access denied")
        elif assignment.group_id:
            if current_user.role == "student":
                from src.schemas.models import GroupStudent
                group_member = db.query(GroupStudent).filter(
                    GroupStudent.group_id == assignment.group_id,
                    GroupStudent.student_id == current_user.id
                ).first()
                if not group_member:
                    raise HTTPException(status_code=403, detail="Access denied")
            elif current_user.role == "teacher":
                group = db.query(Group).filter(Group.id == assignment.group_id).first()
                if group.teacher_id != current_user.id:
                    raise HTTPException(status_code=403, detail="Access denied")
    
    elif file_type == "submissions":
        # Извлекаем assignment_id и user_id из имени файла
        parts = filename.split('_')
        assignment_id = int(parts[1])
        user_id = int(parts[2])
        
        from src.schemas.models import Assignment, AssignmentSubmission
        
        # Проверяем, что это submission пользователя или учитель имеет доступ
        submission = db.query(AssignmentSubmission).filter(
            AssignmentSubmission.assignment_id == assignment_id,
            AssignmentSubmission.user_id == user_id
        ).first()
        
        if not submission:
            raise HTTPException(status_code=404, detail="Submission not found")
        
        # Студенты могут скачивать только свои файлы
        if current_user.role == "student" and submission.user_id != current_user.id:
            raise HTTPException(status_code=403, detail="Access denied")
        
        # Учителя могут скачивать файлы своих студентов
        if current_user.role == "teacher":
            assignment = db.query(Assignment).filter(Assignment.id == assignment_id).first()
            if assignment.lesson_id:
                lesson = db.query(Lesson).filter(Lesson.id == assignment.lesson_id).first()
                module = db.query(Module).filter(Module.id == lesson.module_id).first()
                course = db.query(Course).filter(Course.id == module.course_id).first()
                if course.teacher_id != current_user.id:
                    raise HTTPException(status_code=403, detail="Access denied")
            elif assignment.group_id:
                group = db.query(Group).filter(Group.id == assignment.group_id).first()
                if group.teacher_id != current_user.id:
                    raise HTTPException(status_code=403, detail="Access denied")
    
    elif file_type == "materials":
        # Используем существующую логику для материалов
        material_id = int(filename.split('_')[1])
        from src.schemas.models import LessonMaterial
        
        material = db.query(LessonMaterial).filter(LessonMaterial.id == material_id).first()
        if not material:
            raise HTTPException(status_code=404, detail="Material not found")
        
        lesson = db.query(Lesson).filter(Lesson.id == material.lesson_id).first()
        module = db.query(Module).filter(Module.id == lesson.module_id).first()
        
        from src.utils.permissions import check_course_access
        if not check_course_access(module.course_id, current_user, db):
            raise HTTPException(status_code=403, detail="Access denied")
    
    return FileResponse(file_path, filename=filename)

# =============================================================================
# HELPER FUNCTIONS
# =============================================================================

def validate_and_extract_youtube_info(url: str) -> dict:
    """
    Валидировать YouTube ссылку и извлечь информацию о видео
    Поддерживает различные форматы YouTube URL
    """
    
    # Паттерны для различных форматов YouTube URLs
    patterns = [
        r'(?:https?://)?(?:www\.)?youtube\.com/watch\?v=([a-zA-Z0-9_-]{11})',
        r'(?:https?://)?(?:www\.)?youtu\.be/([a-zA-Z0-9_-]{11})',
        r'(?:https?://)?(?:www\.)?youtube\.com/embed/([a-zA-Z0-9_-]{11})',
        r'(?:https?://)?(?:m\.)?youtube\.com/watch\?v=([a-zA-Z0-9_-]{11})',
    ]
    
    video_id = None
    
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            video_id = match.group(1)
            break
    
    if not video_id:
        return {"is_valid": False}
    
    # Генерируем различные форматы ссылок
    clean_url = f"https://www.youtube.com/watch?v={video_id}"
    embed_url = f"https://www.youtube.com/embed/{video_id}"
    thumbnail_url = f"https://img.youtube.com/vi/{video_id}/maxresdefault.jpg"
    
    return {
        "is_valid": True,
        "video_id": video_id,
        "clean_url": clean_url,
        "embed_url": embed_url,
        "thumbnail_url": thumbnail_url
    }

@router.get("/youtube/formats")
def get_supported_youtube_formats():
    """Получить список поддерживаемых форматов YouTube ссылок"""
    
    return {
        "supported_formats": [
            "https://www.youtube.com/watch?v=VIDEO_ID",
            "https://youtu.be/VIDEO_ID",
            "https://www.youtube.com/embed/VIDEO_ID",
            "https://m.youtube.com/watch?v=VIDEO_ID",
            "www.youtube.com/watch?v=VIDEO_ID",
            "youtu.be/VIDEO_ID"
        ],
        "features": [
            "Автоматическое извлечение ID видео",
            "Генерация embed ссылки для плеера",
            "Получение thumbnail изображения",
            "Валидация корректности ссылки"
        ],
        "examples": {
            "input": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            "output": {
                "video_id": "dQw4w9WgXcQ",
                "clean_url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
                "embed_url": "https://www.youtube.com/embed/dQw4w9WgXcQ",
                "thumbnail_url": "https://img.youtube.com/vi/dQw4w9WgXcQ/maxresdefault.jpg"
            }
        }
    }

@router.get("/upload-guidelines")
def get_upload_guidelines():
    """Получить рекомендации по загрузке медиа контента"""
    
    return {
        "video_guidelines": {
            "platform": "YouTube",
            "recommended_formats": ["MP4", "AVI", "MOV", "WMV"],
            "recommended_resolution": ["1920x1080 (Full HD)", "1280x720 (HD)"],
            "recommended_duration": "5-60 минут для урока",
            "tips": [
                "Используйте четкий звук и изображение",
                "Добавьте субтитры для лучшей доступности",
                "Структурируйте видео с четкими разделами",
                "Добавьте описание и теги для поиска"
            ]
        },
        "material_guidelines": {
            "supported_formats": ["PDF", "DOCX", "DOC", "JPG", "PNG", "GIF", "TXT"],
            "max_file_size": "50 MB",
            "recommendations": [
                "PDF - для документов и презентаций",
                "DOCX - для редактируемых документов",
                "JPG/PNG - для изображений и схем",
                "TXT - для простых текстовых инструкций"
            ]
        },
        "naming_conventions": {
            "videos": "Урок X: Название темы",
            "materials": "Материал_УрокX_Тип",
            "examples": [
                "Урок 1: Введение в Python",
                "Материал_Урок1_Презентация.pdf",
                "Материал_Урок2_Упражнения.docx"
            ]
        }
    }
