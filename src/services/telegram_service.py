"""
Telegram Bot Service for sending notifications about question error reports.
"""
import os
import httpx
import asyncio
import logging
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)

# Telegram Bot Configuration
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
# Comma-separated list of chat IDs to receive notifications
TELEGRAM_ADMIN_CHAT_IDS = os.getenv("TELEGRAM_ADMIN_CHAT_IDS", "").split(",")
# Frontend URL for links in messages
FRONTEND_URL = os.getenv("FRONTEND_URL", "https://master-education.vercel.app")


def get_admin_chat_ids() -> List[str]:
    """Get list of admin chat IDs, filtering out empty strings."""
    return [chat_id.strip() for chat_id in TELEGRAM_ADMIN_CHAT_IDS if chat_id.strip()]


async def send_telegram_message(chat_id: str, message: str, parse_mode: str = "HTML") -> bool:
    """
    Send a message to a specific Telegram chat.
    
    Args:
        chat_id: The Telegram chat ID to send the message to
        message: The message text (supports HTML formatting)
        parse_mode: Message parse mode (HTML or Markdown)
    
    Returns:
        True if message was sent successfully, False otherwise
    """
    if not TELEGRAM_BOT_TOKEN:
        logger.warning("TELEGRAM_BOT_TOKEN not configured, skipping notification")
        return False
    
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": message,
        "parse_mode": parse_mode,
    }
    
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(url, json=payload, timeout=10.0)
            if response.status_code == 200:
                logger.info(f"Telegram message sent to {chat_id}")
                return True
            else:
                logger.error(f"Failed to send Telegram message: {response.text}")
                return False
    except Exception as e:
        logger.error(f"Error sending Telegram message: {e}")
        return False


async def send_telegram_file(
    chat_id: str,
    file_path: str,
    caption: Optional[str] = None,
    parse_mode: str = "HTML"
) -> bool:
    """
    Send a file to a specific Telegram chat.
    
    Args:
        chat_id: The Telegram chat ID to send the file to
        file_path: Path to the file to send
        caption: Optional caption for the file (supports HTML formatting)
        parse_mode: Caption parse mode (HTML or Markdown)
    
    Returns:
        True if file was sent successfully, False otherwise
    """
    if not TELEGRAM_BOT_TOKEN:
        logger.warning("TELEGRAM_BOT_TOKEN not configured, skipping file upload")
        return False
    
    file_path_obj = Path(file_path)
    if not file_path_obj.exists():
        logger.error(f"File not found: {file_path}")
        return False
    
    # Check file size (Telegram limit is 50MB)
    file_size = file_path_obj.stat().st_size
    max_size = 50 * 1024 * 1024  # 50 MB
    if file_size > max_size:
        logger.error(f"File too large: {file_size} bytes (max {max_size} bytes)")
        return False
    
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendDocument"
    
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            with open(file_path, 'rb') as f:
                files = {'document': (file_path_obj.name, f, 'application/octet-stream')}
                data = {'chat_id': chat_id}
                if caption:
                    data['caption'] = caption
                    data['parse_mode'] = parse_mode
                
                response = await client.post(url, files=files, data=data)
                
                if response.status_code == 200:
                    logger.info(f"File sent to {chat_id}: {file_path}")
                    return True
                else:
                    logger.error(f"Failed to send file: {response.text}")
                    return False
    except Exception as e:
        logger.error(f"Error sending file to Telegram: {e}")
        return False


async def send_backup_to_admins(
    file_path: str,
    db_name: str,
    file_size: int,
    checksum: str,
    timestamp: str
) -> int:
    """
    Send backup file to all admin chat IDs.
    
    Args:
        file_path: Path to the backup file
        db_name: Database name
        file_size: File size in bytes
        checksum: MD5 checksum of the file
        timestamp: Backup timestamp
    
    Returns:
        Number of successful sends
    """
    admin_chat_ids = get_admin_chat_ids()
    
    if not admin_chat_ids:
        logger.warning("No admin chat IDs configured, skipping backup upload")
        return 0
    
    # Format file size
    if file_size < 1024:
        size_str = f"{file_size} B"
    elif file_size < 1024 * 1024:
        size_str = f"{file_size / 1024:.2f} KB"
    else:
        size_str = f"{file_size / (1024 * 1024):.2f} MB"
    
    # Build caption
    caption = f"""💾 <b>Database Backup</b>

📊 <b>Database:</b> {db_name}
🕐 <b>Timestamp:</b> {timestamp}
📦 <b>Size:</b> {size_str}
🔒 <b>MD5:</b> <code>{checksum}</code>

✅ Backup completed successfully"""
    
    # Send to all admins
    success_count = 0
    for chat_id in admin_chat_ids:
        if await send_telegram_file(chat_id, file_path, caption):
            success_count += 1
        else:
            logger.error(f"Failed to send backup to chat_id: {chat_id}")
    
    return success_count


async def notify_admins_about_error_report(
    report_id: int,
    question_text: str,
    reporter_name: str,
    reporter_email: str,
    error_message: str,
    suggested_answer: Optional[str],
    course_title: Optional[str] = None,
    module_title: Optional[str] = None,
    lesson_title: Optional[str] = None,
    step_title: Optional[str] = None,
) -> None:
    """
    Send notification to all admin chat IDs about a new error report.
    
    Args:
        report_id: The ID of the error report
        question_text: The question text (truncated if too long)
        reporter_name: Name of the user who reported the error
        reporter_email: Email of the reporter
        error_message: The error description from the user
        suggested_answer: The suggested correct answer (if provided)
        course_title: Course name
        module_title: Module name
        lesson_title: Lesson name
        step_title: Step name
    """
    admin_chat_ids = get_admin_chat_ids()
    
    if not admin_chat_ids:
        logger.warning("No admin chat IDs configured, skipping notification")
        return
    
    # Truncate question text if too long
    max_question_length = 200
    truncated_question = question_text[:max_question_length] + "..." if len(question_text) > max_question_length else question_text
    
    # Build location string
    location_parts = []
    if course_title:
        location_parts.append(course_title)
    if module_title:
        location_parts.append(module_title)
    if lesson_title:
        location_parts.append(lesson_title)
    location = " → ".join(location_parts) if location_parts else "Unknown location"
    
    # Build the message
    message = f"""🚨 <b>New Question Error Report</b>

<b>📝 Question:</b>
<i>{truncated_question}</i>

<b>❌ Issue:</b>
{error_message}
"""

    if suggested_answer:
        message += f"""
<b>💡 Suggested Answer:</b>
{suggested_answer}
"""

    message += f"""
<b>📍 Location:</b>
{location}

<b>👤 Reported by:</b>
{reporter_name} ({reporter_email})

<b>🔗 Report ID:</b> #{report_id}

<a href="{FRONTEND_URL}/admin/question-reports?report={report_id}">Open Report in Admin Panel</a>"""

    # Send to all admins
    for chat_id in admin_chat_ids:
        await send_telegram_message(chat_id, message)


def notify_admins_sync(
    report_id: int,
    question_text: str,
    reporter_name: str,
    reporter_email: str,
    error_message: str,
    suggested_answer: Optional[str],
    course_title: Optional[str] = None,
    module_title: Optional[str] = None,
    lesson_title: Optional[str] = None,
    step_title: Optional[str] = None,
) -> None:
    """
    Synchronous wrapper for notify_admins_about_error_report.
    Use this when calling from a sync context.
    """
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            # If there's already a running loop, schedule the coroutine
            asyncio.create_task(notify_admins_about_error_report(
                report_id=report_id,
                question_text=question_text,
                reporter_name=reporter_name,
                reporter_email=reporter_email,
                error_message=error_message,
                suggested_answer=suggested_answer,
                course_title=course_title,
                module_title=module_title,
                lesson_title=lesson_title,
                step_title=step_title,
            ))
        else:
            loop.run_until_complete(notify_admins_about_error_report(
                report_id=report_id,
                question_text=question_text,
                reporter_name=reporter_name,
                reporter_email=reporter_email,
                error_message=error_message,
                suggested_answer=suggested_answer,
                course_title=course_title,
                module_title=module_title,
                lesson_title=lesson_title,
                step_title=step_title,
            ))
    except RuntimeError:
        # No event loop, create a new one
        asyncio.run(notify_admins_about_error_report(
            report_id=report_id,
            question_text=question_text,
            reporter_name=reporter_name,
            reporter_email=reporter_email,
            error_message=error_message,
            suggested_answer=suggested_answer,
            course_title=course_title,
            module_title=module_title,
            lesson_title=lesson_title,
            step_title=step_title,
        ))
