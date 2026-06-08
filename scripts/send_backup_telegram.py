#!/usr/bin/env python3
"""
Send database backup file to Telegram admin chats.

Usage:
    python send_backup_telegram.py <backup_file_path>

Exit codes:
    0 - Success (file sent to at least one admin)
    1 - File not found
    2 - Environment not configured
    3 - File too large
    4 - Failed to send to all admins
"""

import sys
import os
import hashlib
from pathlib import Path
from datetime import datetime
import asyncio

# Add parent directory to path to import telegram_service
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.services.telegram_service import send_backup_to_admins, TELEGRAM_BOT_TOKEN, get_admin_chat_ids


def calculate_md5(file_path: str) -> str:
    """Calculate MD5 checksum of a file."""
    md5_hash = hashlib.md5()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(4096), b""):
            md5_hash.update(chunk)
    return md5_hash.hexdigest()


def main():
    if len(sys.argv) < 2:
        print("Error: Backup file path required", file=sys.stderr)
        print(f"Usage: {sys.argv[0]} <backup_file_path>", file=sys.stderr)
        sys.exit(1)
    
    backup_file = sys.argv[1]
    file_path = Path(backup_file)
    
    # Check if file exists
    if not file_path.exists():
        print(f"Error: File not found: {backup_file}", file=sys.stderr)
        sys.exit(1)
    
    # Check environment configuration
    if not TELEGRAM_BOT_TOKEN:
        print("Error: TELEGRAM_BOT_TOKEN not configured in environment", file=sys.stderr)
        sys.exit(2)
    
    admin_chat_ids = get_admin_chat_ids()
    if not admin_chat_ids:
        print("Error: TELEGRAM_ADMIN_CHAT_IDS not configured in environment", file=sys.stderr)
        sys.exit(2)
    
    # Check file size (50 MB limit)
    file_size = file_path.stat().st_size
    max_size = 50 * 1024 * 1024
    if file_size > max_size:
        print(f"Error: File too large: {file_size / (1024*1024):.2f} MB (max 50 MB)", file=sys.stderr)
        sys.exit(3)
    
    print(f"Sending backup to Telegram: {backup_file}")
    print(f"File size: {file_size / (1024*1024):.2f} MB")
    print(f"Admin chats: {len(admin_chat_ids)}")
    
    # Calculate MD5 checksum
    print("Calculating MD5 checksum...")
    checksum = calculate_md5(backup_file)
    print(f"MD5: {checksum}")
    
    # Extract database name from filename
    db_name = "lms_db"
    if "lms_db" in file_path.name:
        db_name = "lms_db"
    elif "crm" in file_path.name.lower():
        db_name = "crm_db"
    
    # Format timestamp
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
    # Send to admins
    print("Uploading to Telegram...")
    success_count = asyncio.run(send_backup_to_admins(
        file_path=backup_file,
        db_name=db_name,
        file_size=file_size,
        checksum=checksum,
        timestamp=timestamp
    ))
    
    if success_count == 0:
        print("Error: Failed to send backup to any admin", file=sys.stderr)
        sys.exit(4)
    elif success_count < len(admin_chat_ids):
        print(f"Warning: Sent to {success_count}/{len(admin_chat_ids)} admins", file=sys.stderr)
        sys.exit(4)
    else:
        print(f"✓ Successfully sent backup to {success_count} admin(s)")
        sys.exit(0)


if __name__ == "__main__":
    main()
