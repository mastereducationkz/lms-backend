# LMS Backend Deployment Guide

## GitHub Actions CI/CD Setup

### Current Setup
- **Workflow**: `.github/workflows/deploy-backend.yml`
- **Trigger**: Automatic on push to `main` branch (after CI passes)
- **Target**: New server at `188.130.160.227`

## Update GitHub Secrets

Your repository needs these secrets configured for the new server:

### 1. Go to GitHub Repository Settings
```
https://github.com/YOUR_USERNAME/YOUR_REPO/settings/secrets/actions
```

### 2. Update/Add These Secrets

| Secret Name | Value | Description |
|------------|-------|-------------|
| `HOST` | `188.130.160.227` | New server IP |
| `USERNAME` | `root` | SSH username |
| `SSH_PRIVATE_KEY` | Your SSH private key | Key to access server |

### 3. Get Your SSH Private Key

If you don't have the private key saved:

**Option A: Generate new SSH key pair**
```bash
# On your local machine
ssh-keygen -t ed25519 -C "github-actions-deploy" -f ~/.ssh/github_deploy_key
```

Then add public key to server:
```bash
# Copy public key
cat ~/.ssh/github_deploy_key.pub

# On server
mkdir -p ~/.ssh
echo "YOUR_PUBLIC_KEY_HERE" >> ~/.ssh/authorized_keys
chmod 600 ~/.ssh/authorized_keys
```

**Option B: Use existing key**
```bash
# Show your private key (be careful - sensitive!)
cat ~/.ssh/id_rsa
# or
cat ~/.ssh/id_ed25519
```

Copy the entire output (including `-----BEGIN` and `-----END` lines) and paste into GitHub secret `SSH_PRIVATE_KEY`.

## Manual Deployment (If Needed)

If GitHub Actions fails or you need to deploy manually:

```bash
# Push your changes
git add .
git commit -m "feat: add automated backups"
git push origin main

# Or deploy directly on server
ssh root@188.130.160.227
cd ~/projects/lms
git pull origin main
chmod +x scripts/*.sh scripts/*.py
docker compose build
docker compose up -d
```

## First-Time Setup on New Server

Already done ✅ (you ran these commands earlier):
```bash
cd /root/projects/lms
mkdir -p backups logs
```

Still need to do:
```bash
# Install Python dependency for backup
pip3 install httpx

# Test backup system
./scripts/backup_and_notify.sh
```

## Deployment Process

When you push to `main` branch:

1. **CI runs** (Backend CI workflow)
   - Lints code
   - Runs tests
   
2. **CD runs** (Deploy Backend workflow) - triggers after CI passes
   - SSH to server
   - Git pull latest code
   - Set script permissions
   - Create directories
   - Build Docker images
   - Restart containers
   - Health check

3. **Health Check**
   - Waits up to 100 seconds for backend to respond
   - Checks `https://lmsapi.mastereducation.kz/health`
   - Shows logs if fails

## Monitoring Deployments

### GitHub Actions Tab
```
https://github.com/YOUR_USERNAME/YOUR_REPO/actions
```

### Check Deployment Logs
```bash
# On server
cd ~/projects/lms
docker compose logs -f backend --tail=100
```

### Check if Services Running
```bash
docker compose ps
```

## Rollback (If Needed)

```bash
ssh root@188.130.160.227
cd ~/projects/lms

# Rollback to previous commit
git log --oneline -10  # Find commit hash
git checkout COMMIT_HASH
docker compose build
docker compose up -d

# Or rollback to previous version
git reset --hard HEAD~1
docker compose up -d
```

## Troubleshooting

### Deployment Fails

**Check GitHub Actions logs:**
1. Go to Actions tab in GitHub
2. Click on failed workflow
3. Check error messages

**Common issues:**

| Error | Solution |
|-------|----------|
| Permission denied (publickey) | Update `SSH_PRIVATE_KEY` secret |
| Host key verification failed | Add server to known_hosts or use `StrictHostKeyChecking=no` |
| Docker build fails | Check Dockerfile syntax, check server disk space |
| Health check timeout | Check backend logs: `docker compose logs backend` |

### Container Won't Start

```bash
ssh root@188.130.160.227
cd ~/projects/lms

# Check logs
docker compose logs backend --tail=100

# Check if ports are in use
netstat -tlnp | grep 8000

# Restart
docker compose down
docker compose up -d
```

### Can't SSH to Server

```bash
# Test SSH connection
ssh -v root@188.130.160.227

# If key issues
ssh-add ~/.ssh/id_rsa  # or your key file
```

## Adding More Deployment Steps

Edit `.github/workflows/deploy-backend.yml` to add custom deployment steps:

```yaml
script: |
  set -e
  echo "🚀 Starting deployment..."
  cd ~/projects/lms
  
  # Your custom steps here
  # Example: Database migrations
  docker compose exec -T backend alembic upgrade head
  
  # Example: Clear cache
  docker compose exec -T redis redis-cli FLUSHDB
  
  docker compose up -d
```

## CI/CD Best Practices

✅ **Do:**
- Test locally before pushing
- Write meaningful commit messages
- Monitor deployment in Actions tab
- Check health endpoint after deploy

❌ **Don't:**
- Push directly to main without testing
- Commit sensitive data (use `.env` files)
- Skip health checks
- Deploy during peak hours without testing

## Security Notes

- SSH private key is **highly sensitive** - never commit to git
- Use GitHub Secrets for sensitive values
- Regularly rotate SSH keys
- Monitor GitHub Actions logs for suspicious activity
- Use branch protection rules for `main` branch

## Next Steps

1. ✅ Update GitHub Secrets (HOST, USERNAME, SSH_PRIVATE_KEY)
2. ✅ Push this code to trigger deployment
3. ✅ Monitor deployment in GitHub Actions
4. ✅ Verify health check passes
5. ✅ Test backup system on server
