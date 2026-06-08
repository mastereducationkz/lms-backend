#!/bin/bash

# Automated PostgreSQL Backup Script with Telegram Notification
# Runs nightly via cron to backup database and send to Telegram

set -e  # Exit on error

# Configuration
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND_DIR="$(dirname "$SCRIPT_DIR")"
BACKUP_DIR="${BACKEND_DIR}/backups"
LOG_DIR="${BACKEND_DIR}/logs"
LOG_FILE="${LOG_DIR}/backup.log"

# Database configuration
DB_NAME="lms_db"
DB_USER="myuser"

# Detect container name
if docker ps | grep -q "lms-postgres"; then
    CONTAINER_NAME="lms-postgres"
elif docker ps | grep -q "postgres-lms"; then
    CONTAINER_NAME="postgres-lms"
else
    echo "$(date '+%Y-%m-%d %H:%M:%S') ERROR: PostgreSQL container not found" >> "$LOG_FILE"
    exit 1
fi

# Retention policy (days)
RETENTION_DAYS=7

# Colors for output
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
NC='\033[0m'

# Logging function
log() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') $1" | tee -a "$LOG_FILE"
}

log_error() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') ERROR: $1" | tee -a "$LOG_FILE" >&2
}

# Create directories if they don't exist
mkdir -p "$BACKUP_DIR"
mkdir -p "$LOG_DIR"

log "================================================"
log "Starting automated database backup"
log "================================================"

# Generate backup filename with timestamp
DATE=$(date +%Y%m%d_%H%M%S)
BACKUP_FILE="${BACKUP_DIR}/${DB_NAME}_${DATE}.dump"

# Check if container is running
if ! docker ps | grep -q "$CONTAINER_NAME"; then
    log_error "Container $CONTAINER_NAME is not running"
    exit 1
fi

log "Database: $DB_NAME"
log "Container: $CONTAINER_NAME"
log "Backup file: $BACKUP_FILE"

# Create backup
log "Creating database backup..."
if docker exec "$CONTAINER_NAME" pg_dump -U "$DB_USER" -d "$DB_NAME" -F c -b > "$BACKUP_FILE"; then
    FILE_SIZE=$(du -h "$BACKUP_FILE" | cut -f1)
    log "✓ Backup created successfully (size: $FILE_SIZE)"
else
    log_error "Failed to create backup"
    rm -f "$BACKUP_FILE"
    exit 1
fi

# Send to Telegram
log "Sending backup to Telegram..."

# Check if backend container is running
if ! docker ps | grep -q "lms-backend\|backend"; then
    log_error "Backend container is not running, skipping Telegram upload"
else
    # Detect backend container name
    if docker ps | grep -q "lms-backend"; then
        BACKEND_CONTAINER="lms-backend"
    else
        BACKEND_CONTAINER="backend"
    fi
    
    # Send via Python script inside Docker container
    if docker exec "$BACKEND_CONTAINER" python scripts/send_backup_telegram.py "$BACKUP_FILE" >> "$LOG_FILE" 2>&1; then
        log "✓ Backup sent to Telegram successfully"
    else
        log_error "Failed to send backup to Telegram (backup file preserved)"
        # Don't exit - backup is still saved locally
    fi
fi

# Cleanup old backups
log "Cleaning up old backups (retention: ${RETENTION_DAYS} days)..."
DELETED_COUNT=0

if [ -d "$BACKUP_DIR" ]; then
    # Find and delete backups older than RETENTION_DAYS
    while IFS= read -r old_file; do
        if [ -f "$old_file" ]; then
            rm -f "$old_file"
            log "  Deleted: $(basename "$old_file")"
            ((DELETED_COUNT++))
        fi
    done < <(find "$BACKUP_DIR" -name "*.dump" -type f -mtime +${RETENTION_DAYS})
fi

if [ $DELETED_COUNT -gt 0 ]; then
    log "✓ Cleaned up $DELETED_COUNT old backup(s)"
else
    log "✓ No old backups to clean up"
fi

# Show current backups
BACKUP_COUNT=$(find "$BACKUP_DIR" -name "*.dump" -type f | wc -l | tr -d ' ')
log "Current backups on server: $BACKUP_COUNT"

if [ $BACKUP_COUNT -gt 0 ]; then
    log "Available backups:"
    find "$BACKUP_DIR" -name "*.dump" -type f -printf "  %f (%s bytes)\n" | sort -r | head -n 5 >> "$LOG_FILE"
fi

log "================================================"
log "Backup completed successfully"
log "================================================"

exit 0
