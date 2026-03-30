# OpenNVR - User Manual

## 🚀 Quick Start (For Testing)

### Step 1: Pull Docker Images
```bash
docker compose pull
```
This downloads all required containers from Docker Hub.

### Step 2: Start Application
```bash
docker compose up -d
```

### Step 3: Access the Application
- **Web Interface**: http://localhost:8000
- **Default Login**: 
  - Username: `admin`
  - Password: `admin123`

---

## ⚠️ CRITICAL SECURITY NOTICE

**The default configuration uses INSECURE dummy credentials!**

### What's Insecure?
The `docker-compose.yml` file contains these **DUMMY** values:
- `POSTGRES_PASSWORD: CHANGE_THIS_PASSWORD_123`
- `SECRET_KEY: INSECURE_CHANGE_ME_SECRET_KEY_DUMMY_12345`
- `CREDENTIAL_ENCRYPTION_KEY: INSECURE_CHANGE_ME_CREDENTIAL_KEY_67890`
- `INTERNAL_API_KEY: INSECURE_CHANGE_ME_API_KEY_ABCDEF`
- `MEDIAMTX_SECRET: INSECURE_CHANGE_ME_MEDIAMTX_SECRET`

**Additionally, `mediamtx.yml` also contains the dummy secret:**
- `X-MTX-Secret: INSECURE_CHANGE_ME_MEDIAMTX_SECRET` (appears in 2 webhook configurations)

### Why This Matters?
- Anyone can access your database
- Anyone can decrypt stored credentials
- Anyone can forge authentication tokens
- Your system is **NOT PRODUCTION-READY** until you change these!

### 📝 Files You Must Edit for Production:
1. **`docker-compose.yml`** (already downloaded) - Change 5 dummy secrets
2. **`mediamtx.yml`** (already downloaded) - Change `MEDIAMTX_SECRET` in 2 webhook lines

---

## 🔐 Securing Your Installation (REQUIRED for Production)

### Method 1: Edit Files Directly (Recommended for Docker Hub Users)

This is the simplest approach - just edit two files and restart containers.

#### Step 1: Generate Strong Secrets
Use these commands to generate 5 secure random values:

**On Linux/Mac:**
```bash
# Run this 5 times to get 5 different secrets
openssl rand -hex 32
```

**On Windows (PowerShell):**
```powershell
# Run this 5 times to get 5 different secrets
-join ((65..90) + (97..122) + (48..57) | Get-Random -Count 32 | % {[char]$_})
```

**Example output (use your own, not these!):**
```
8f3a9c2e1d7b5a4f6c8e9a1b3d5f7a9c
a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0c1d2e3f4a5b6c7d8e9f0a1b2
9f8e7d6c5b4a3f2e1d0c9b8a7f6e5d4c3b2a1f0e9d8c7b6a5f4e3d2c1b0a9f8
b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7
c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8
```

#### Step 2: Edit `docker-compose.yml`
Open `docker-compose.yml` and find the `opennvr-core` service section.

**Find these lines (around line 64-67):**
```yaml
      - SECRET_KEY=${SECRET_KEY:-INSECURE_CHANGE_ME_SECRET_KEY_DUMMY_12345}
      - CREDENTIAL_ENCRYPTION_KEY=${CREDENTIAL_ENCRYPTION_KEY:-INSECURE_CHANGE_ME_CREDENTIAL_KEY_67890}
      - INTERNAL_API_KEY=${INTERNAL_API_KEY:-INSECURE_CHANGE_ME_API_KEY_ABCDEF}
      - MEDIAMTX_SECRET=${MEDIAMTX_SECRET:-INSECURE_CHANGE_ME_MEDIAMTX_SECRET}
```

**Replace with your actual secrets:**
```yaml
      - SECRET_KEY=a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0c1d2e3f4a5b6c7d8e9f0a1b2
      - CREDENTIAL_ENCRYPTION_KEY=9f8e7d6c5b4a3f2e1d0c9b8a7f6e5d4c3b2a1f0e9d8c7b6a5f4e3d2c1b0a9f8
      - INTERNAL_API_KEY=b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7
      - MEDIAMTX_SECRET=c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8
```

**Also find the database password (around line 13):**
```yaml
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:-CHANGE_THIS_PASSWORD_123}
```

**Replace with:**
```yaml
      POSTGRES_PASSWORD: 8f3a9c2e1d7b5a4f6c8e9a1b3d5f7a9c
```

**And update the DATABASE_URL (around line 54):**
```yaml
      - DATABASE_URL=postgresql://${POSTGRES_USER:-opennvr_user}:${POSTGRES_PASSWORD:-CHANGE_THIS_PASSWORD_123}@db:5432/${POSTGRES_DB:-opennvr_db}
```

**Replace with:**
```yaml
      - DATABASE_URL=postgresql://opennvr_user:8f3a9c2e1d7b5a4f6c8e9a1b3d5f7a9c@db:5432/opennvr_db
```

#### Step 3: Edit `mediamtx.yml`
Open `mediamtx.yml` and find the webhook configurations.

**Find these TWO lines (line 338 and 364):**
```yaml
runOnInit: 'curl -X GET -H "X-MTX-Secret: INSECURE_CHANGE_ME_MEDIAMTX_SECRET" ...'
runOnRecordSegmentComplete: 'curl -X GET -H "X-MTX-Secret: INSECURE_CHANGE_ME_MEDIAMTX_SECRET" ...'
```

**Replace `INSECURE_CHANGE_ME_MEDIAMTX_SECRET` with the SAME secret you used for `MEDIAMTX_SECRET` in docker-compose.yml:**
```yaml
runOnInit: 'curl -X GET -H "X-MTX-Secret: c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8" ...'
runOnRecordSegmentComplete: 'curl -X GET -H "X-MTX-Secret: c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8" ...'
```

⚠️ **CRITICAL**: The `X-MTX-Secret` in `mediamtx.yml` MUST match `MEDIAMTX_SECRET` in `docker-compose.yml`!

#### Step 4: Restart Containers
```bash
docker compose down
docker compose up -d
```

✅ Done! Your installation is now secured.

⚠️ **IMPORTANT**: After changing `CREDENTIAL_ENCRYPTION_KEY`, you'll need to re-enter all camera passwords as the old encrypted passwords won't decrypt correctly.

---

### Method 2: Using `.env` File (Optional - For Git Developers)

If you're developing and using Git, you may prefer keeping secrets in a separate `.env` file to avoid committing them.

**Why use this method?**
- Keeps secrets out of `docker-compose.yml` (which might be in Git)
- The `.env` file is already in `.gitignore` and won't be committed
- Docker Compose automatically reads `.env` file

**Steps:**
1. Copy `.env.example` to `.env`: `cp .env.example .env`
2. Edit `.env` and replace all 5 dummy secrets with strong random values
3. Edit `mediamtx.yml` to match the `MEDIAMTX_SECRET` you set in `.env`
4. Restart: `docker compose down && docker compose up -d`

See `.env.example` for the template with all variables documented.

---

## 📖 What Each Secret Does

| Secret Name | Purpose | Used By | Where to Change |
|------------|---------|---------|------------------|
| `POSTGRES_PASSWORD` | Database password | PostgreSQL & Backend | `docker-compose.yml` (2 places: db service + DATABASE_URL) |
| `SECRET_KEY` | JWT token signing, session encryption | Backend API | `docker-compose.yml` |
| `CREDENTIAL_ENCRYPTION_KEY` | Encrypts camera credentials in DB | Backend (Camera Service) | `docker-compose.yml` |
| `INTERNAL_API_KEY` | Service-to-service authentication | Backend ↔ AI Adapters | `docker-compose.yml` |
| `MEDIAMTX_SECRET` | Stream authentication token | MediaMTX ↔ Backend | **BOTH `docker-compose.yml` AND `mediamtx.yml` (2 webhooks)** |

---

## 🔄 Updating Your Installation

### Pulling Latest Images
```bash
# Pull updates from Docker Hub
docker compose pull

# Restart with new images
docker compose down
docker compose up -d
```

### Viewing Logs
```bash
# All services
docker compose logs -f

# Specific service
docker compose logs -f opennvr-core
docker compose logs -f ai-adapters
docker compose logs -f db
```

---

## 🛠️ Common Operations

### Change Admin Password (First Login)
1. Login with default credentials: `admin` / `admin123`
2. Go to **Settings** → **User Management**
3. Change admin password immediately!

### Adding Cameras
1. Navigate to **Cameras** → **Add Camera**
2. Enter camera details (IP, credentials, stream path)
3. Camera passwords are encrypted using `CREDENTIAL_ENCRYPTION_KEY`

### Viewing Recordings
- Recordings are stored in: `./Recordings/` directory
- Organized by camera and date: `Recordings/cam-1/2026/01/17/`

---

## 🐛 Troubleshooting

### Container Won't Start
```bash
# Check logs
docker compose logs

# Check if ports are already in use
netstat -ano | findstr "8000"  # Windows
lsof -i :8000                  # Linux/Mac
```

### Database Connection Failed
```bash
# Check if database is healthy
docker compose ps

# Ensure POSTGRES_PASSWORD matches in both db and opennvr-core services
```

### AI Detection Not Working
```bash
# Check AI adapter logs
docker compose logs ai-adapters

# Ensure model weights are downloaded
ls -l AI-adapters/AIAdapters/model_weights/
```

---

## 🔒 Security Best Practices

1. ✅ **Change ALL default secrets** before production use
2. ✅ **Use strong random passwords** (32+ characters)
3. ✅ **Never commit secrets** to Git (use `.env` file if using Git - it's in `.gitignore`)
4. ✅ **Change admin password** on first login
5. ✅ **Use HTTPS** in production (reverse proxy like Nginx)
6. ✅ **Limit port exposure** to trusted networks only
7. ✅ **Regular backups** of `opennvr_db_data` volume
8. ✅ **Keep images updated** with security patches

---

## 📊 System Requirements

- **RAM**: 8GB minimum, 16GB recommended
- **Storage**: 
  - 10GB for Docker images
  - Additional space for recordings (1GB per camera per day @ 4Mbps)
- **CPU**: 4 cores minimum (6+ for AI processing)
- **GPU**: Optional (NVIDIA GPU accelerates AI detection)

---

## 🆘 Support

- **Documentation**: See `docs/` folder
- **Issues**: Report on GitHub repository
- **Architecture**: See `docs/ARCHITECTURE.md`
- **Security**: See `docs/SECURITY.md`

---

## ⚡ Quick Reference Commands

```bash
# Start system
docker compose up -d

# Stop system
docker compose down

# Stop and delete all data (DANGEROUS!)
docker compose down -v

# Update to latest version
docker compose pull && docker compose up -d

# View logs
docker compose logs -f

# Restart specific service
docker compose restart opennvr-core

# Check running containers
docker compose ps

# Access database directly
docker compose exec db psql -U opennvr_user -d opennvr_db
```

---

**Last Updated**: February 17, 2026  
**Version**: 1.0.0
