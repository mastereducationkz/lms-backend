# Database Backup System Setup Guide

## Overview
Automated nightly database backups for LMS PostgreSQL database at 2:00 AM (UTC+5), with Telegram notifications and 7-day local retention.

## Prerequisites

### 1. Check Environment Variables
SSH to server and verify `.env` file:
```bash
ssh root@188.130.160.227
cd /root/projects/lms/backend
cat .env | grep TELEGRAM
```

### 2. Check Python Dependencies
```bash
cd /root/projects/lms/backend
python3 -c "import httpx; print('httpx installed')"
```

If not installed:
```bash
pip3 install httpx
```

### 3. Create Required Directories
```bash
cd /root/projects/lms/backend
mkdir -p backups logs
```

### 4. Set Script Permissions
```bash
chmod +x scripts/backup_and_notify.sh
chmod +x scripts/send_backup_telegram.py
```

## Testing the Backup System

### Manual Test Run
Before setting up cron, test the backup manually:

```bash
cd /root/projects/lms/backend
./scripts/backup_and_notify.sh
```

Expected output:
```
2026-06-08 14:30:00 ================================================
2026-06-08 14:30:00 Starting automated database backup
2026-06-08 14:30:00 ================================================
2026-06-08 14:30:00 Database: lms_db
2026-06-08 14:30:00 Container: lms-postgres
2026-06-08 14:30:00 Backup file: /root/projects/lms/backend/backups/lms_db_20260608_143000.dump
2026-06-08 14:30:01 Creating database backup...
2026-06-08 14:30:05 ✓ Backup created successfully (size: 3.6M)
2026-06-08 14:30:05 Sending backup to Telegram...
Sending backup to Telegram: /root/projects/lms/backend/backups/lms_db_20260608_143000.dump
File size: 3.60 MB
Admin chats: 3
Calculating MD5 checksum...
MD5: a1b2c3d4e5f6...
Uploading to Telegram...
✓ Successfully sent backup to 3 admin(s)
2026-06-08 14:30:15 ✓ Backup sent to Telegram successfully
2026-06-08 14:30:15 Cleaning up old backups (retention: 7 days)...
2026-06-08 14:30:15 ✓ No old backups to clean up
2026-06-08 14:30:15 Current backups on server: 1
2026-06-08 14:30:15 ================================================
2026-06-08 14:30:15 Backup completed successfully
2026-06-08 14:30:15 ================================================
```

### Check Telegram
You should receive the backup file in your Telegram chat with caption:
```
💾 Database Backup

📊 Database: lms_db
🕐 Timestamp: 2026-06-08 14:30:00
📦 Size: 3.60 MB
🔒 MD5: a1b2c3d4e5f6...

✅ Backup completed successfully
```

### Check Log File
```bash
tail -f /root/projects/lms/backend/logs/backup.log
```

## Setting Up Cron Job

### 1. Determine Server Timezone
```bash
timedatectl
```

If server is UTC (most common), 2:00 AM UTC+5 = 21:00 UTC previous day.
If server is already UTC+5, use 2:00 AM directly.

### 2. Edit Root Crontab
```bash
crontab -e
```

### 3. Add Cron Entry

**If server runs UTC:**
```cron
# LMS Database Backup - 2:00 AM Kazakhstan Time (21:00 UTC)
0 21 * * * cd /root/projects/lms/backend && ./scripts/backup_and_notify.sh >> ./logs/backup.log 2>&1
```

**If server runs UTC+5:**
```cron
# LMS Database Backup - 2:00 AM Kazakhstan Time
0 2 * * * cd /root/projects/lms/backend && ./scripts/backup_and_notify.sh >> ./logs/backup.log 2>&1
```

**Alternative with explicit timezone:**
```cron
# LMS Database Backup - 2:00 AM Kazakhstan Time
TZ=Asia/Almaty
0 2 * * * cd /root/projects/lms/backend && ./scripts/backup_and_notify.sh >> ./logs/backup.log 2>&1
```

### 4. Verify Cron Entry
```bash
crontab -l | grep backup
```

### 5. Check Cron Service
```bash
systemctl status cron
# or on some systems:
systemctl status crond
```

## Testing Cron Execution

### Option 1: Temporary Test Entry
Add a test entry that runs in 2 minutes:
```bash
crontab -e
```

Add (adjust time to 2 minutes from now):
```cron
# TEST - remove after verification
32 14 * * * cd /root/projects/lms/backend && ./scripts/backup_and_notify.sh >> ./logs/backup.log 2>&1
```

Wait 2 minutes, then check:
```bash
tail -20 /root/projects/lms/backend/logs/backup.log
```

Remove test entry after verification.

### Option 2: Monitor Log in Real-Time
On the day cron should run:
```bash
tail -f /root/projects/lms/backend/logs/backup.log
```

## Monitoring

### Check Recent Backups
```bash
ls -lh /root/projects/lms/backend/backups/ | tail -10
```

### Check Log File
```bash
# Last 50 lines
tail -50 /root/projects/lms/backend/logs/backup.log

# Search for errors
grep ERROR /root/projects/lms/backend/logs/backup.log
```

### Check Disk Space
```bash
df -h /root/projects/lms/backend/backups/
```

### Backup Statistics
```bash
cd /root/projects/lms/backend/backups
echo "Total backups: $(ls -1 *.dump 2>/dev/null | wc -l)"
echo "Total size: $(du -sh . | cut -f1)"
echo "Oldest: $(ls -t *.dump 2>/dev/null | tail -1)"
echo "Newest: $(ls -t *.dump 2>/dev/null | head -1)"
```

## Troubleshooting

### Backup Not Created
1. Check if PostgreSQL container is running:
   ```bash
   docker ps | grep postgres
   ```

2. Check container logs:
   ```bash
   docker logs lms-postgres --tail 50
   ```

3. Test pg_dump manually:
   ```bash
   docker exec lms-postgres pg_dump -U myuser -d lms_db -F c -b > /tmp/test.dump
   ls -lh /tmp/test.dump
   ```

### Telegram Upload Fails
1. Check environment variables:
   ```bash
   cd /root/projects/lms/backend
   grep TELEGRAM .env
   ```

2. Test Python script directly:
   ```bash
   cd /root/projects/lms/backend
   python3 scripts/send_backup_telegram.py backups/lms_db_YYYYMMDD_HHMMSS.dump
   ```

3. Check httpx is installed:
   ```bash
   python3 -c "import httpx; print(httpx.__version__)"
   ```

4. Test Telegram Bot token:
   ```bash
   TOKEN="8009187685:AAHFQLKwwAqoevO_sGVlUXgw41ilD8afW-c"
   curl "https://api.telegram.org/bot${TOKEN}/getMe"
   ```

### Cron Not Running
1. Check cron service:
   ```bash
   systemctl status cron
   systemctl restart cron
   ```

2. Check cron logs:
   ```bash
   grep CRON /var/log/syslog | tail -20
   # or
   journalctl -u cron --since "1 hour ago"
   ```

3. Verify crontab:
   ```bash
   crontab -l
   ```

### File Too Large (>50MB)
Current backup is ~3.6MB, well within limit. If it grows beyond 50MB:

1. Add compression to script (edit `backup_and_notify.sh`):
   ```bash
   # After creating backup
   gzip "$BACKUP_FILE"
   BACKUP_FILE="${BACKUP_FILE}.gz"
   ```

2. Or implement incremental backups (future enhancement)

## Maintenance

### Weekly Check
```bash
cd /root/projects/lms/backend
echo "=== Backup System Health Check ==="
echo "Latest backup:"
ls -lh backups/*.dump 2>/dev/null | tail -1
echo ""
echo "Backup count (should be ≤ 7):"
ls -1 backups/*.dump 2>/dev/null | wc -l
echo ""
echo "Last 3 log entries:"
tail -3 logs/backup.log
```

### Manual Backup
```bash
cd /root/projects/lms/backend
./scripts/backup_and_notify.sh
```

### Restore from Backup
```bash
# List available backups
ls -lh /root/projects/lms/backend/backups/

# Restore specific backup
./scripts/restore_postgres.sh backups/lms_db_20260608_143000.dump
```

## Migration to S3 (Future)

When S3/Spaces is approved, add to `backup_and_notify.sh` after Telegram upload:

```bash
# Upload to S3 (add after Telegram section)
log "Uploading to S3..."
if command -v aws &> /dev/null; then
    aws s3 cp "$BACKUP_FILE" s3://your-bucket/backups/ \
        --storage-class STANDARD_IA \
        --metadata "database=${DB_NAME},timestamp=${DATE}"
    log "✓ Uploaded to S3"
else
    log "WARNING: AWS CLI not found, skipping S3 upload"
fi
```

Install AWS CLI:
```bash
pip3 install awscli
aws configure
```

## Security Notes

- Backup files contain **sensitive data**
- Files stored at `/root/projects/lms/backend/backups/` (root-only access)
- Telegram chat IDs are private (only specified admins receive files)
- Bot token in `.env` (not in git)
- Consider encrypting backups before upload (future enhancement)

## Support

If backup fails consistently:
1. Check `logs/backup.log` for errors
2. Verify all prerequisites above
3. Test manual backup: `./scripts/backup_and_notify.sh`
4. Check Telegram by sending test message
