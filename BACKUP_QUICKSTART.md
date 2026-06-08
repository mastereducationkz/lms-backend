# Database Backup - Quick Start Guide

## 🚀 Quick Setup (5 minutes)

### On Server (SSH to 188.130.160.227)

```bash
# 1. Navigate to backend directory
cd /root/projects/lms/backend

# 2. Create required directories
mkdir -p backups logs

# 3. Install Python dependency (if needed)
pip3 install httpx

# 4. Test backup manually
./scripts/backup_and_notify.sh
```

**Check Telegram** - you should receive the backup file!

### Setup Automated Backups

```bash
# 1. Edit crontab
crontab -e

# 2. Add this line (for 2:00 AM UTC+5 if server runs UTC):
0 21 * * * cd /root/projects/lms/backend && ./scripts/backup_and_notify.sh >> ./logs/backup.log 2>&1

# 3. Save and exit (Ctrl+X, Y, Enter in nano)

# 4. Verify
crontab -l
```

## ✅ What You Get

- **Daily backups** at 2:00 AM (Kazakhstan time)
- **Telegram notification** with backup file sent to 3 admin chats
- **7-day retention** - old backups automatically deleted
- **Full logs** at `/root/projects/lms/backend/logs/backup.log`

## 📱 Telegram Message Format

You'll receive:
```
💾 Database Backup

📊 Database: lms_db
🕐 Timestamp: 2026-06-08 02:00:00
📦 Size: 3.60 MB
🔒 MD5: a1b2c3d4e5f6...

✅ Backup completed successfully
```

## 🔍 Daily Monitoring

```bash
# Check latest backup
ls -lh /root/projects/lms/backend/backups/ | tail -1

# Check log
tail -20 /root/projects/lms/backend/logs/backup.log
```

## 🆘 Troubleshooting

**Backup not received?**
```bash
# Check log for errors
grep ERROR /root/projects/lms/backend/logs/backup.log

# Test manually
cd /root/projects/lms/backend
./scripts/backup_and_notify.sh
```

**Need to restore?**
```bash
cd /root/projects/lms/backend
./scripts/restore_postgres.sh backups/lms_db_YYYYMMDD_HHMMSS.dump
```

## 📋 Full Documentation

See [BACKUP_SETUP.md](./BACKUP_SETUP.md) for complete setup, testing, and troubleshooting guide.

## 🔄 When You Get S3

Easy to add! Just install AWS CLI and add to the backup script:
```bash
aws s3 cp "$BACKUP_FILE" s3://your-bucket/backups/
```

Detailed instructions in [BACKUP_SETUP.md](./BACKUP_SETUP.md#migration-to-s3-future).
