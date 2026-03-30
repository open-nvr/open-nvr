# OpenNVR - Local Development Setup

Complete guide for setting up OpenNVR for local development without Docker.

---

## ⚠️ CRITICAL: Local Development vs Docker Configuration

**When running in local development mode (IDE/VS Code), you are using:**

1. **mediamtx.local.yml** - Local development config file (uses `127.0.0.1`, hardcoded placeholders)
2. **server/.env** - Backend environment variables
3. **Hardcoded MediaMTX secret** - Must manually replace `YOUR_SECRET_HERE` in mediamtx.local.yml

**Docker deployment uses:**
1. **mediamtx.docker.yml** - Docker config file (uses `0.0.0.0`, environment variable substitution)
2. **.env** - Docker Compose environment variables
3. **Environment-injected secrets** - Docker Compose automatically injects from .env

**These settings are different!** See detailed instructions below.

---

## Prerequisites

### Required Software

- **Python 3.11+** ([python.org](https://www.python.org/downloads/))
- **Node.js 18+** and npm ([nodejs.org](https://nodejs.org/))
- **PostgreSQL 13+** ([postgresql.org](https://www.postgresql.org/download/))
- **MediaMTX v1.15.4+** ([github.com/bluenviron/mediamtx](https://github.com/bluenviron/mediamtx/releases))
- **Git** ([git-scm.com](https://git-scm.com/))

### System Requirements

- **OS**: Windows 10/11, Ubuntu 22.04+, or macOS
- **RAM**: 4GB minimum (8GB recommended)
- **Disk**: 10GB free space (plus storage for recordings)

---

## Installation Steps

### 1. Clone Repository

```bash
git clone https://github.com/open-nvr/open-nvr.git
cd opennvr
```

### 2. Database Setup

**Create PostgreSQL Database:**

```bash
# Linux/Mac
sudo -u postgres psql
CREATE DATABASE opennvr;
CREATE USER opennvr_admin WITH PASSWORD 'your_secure_password';
GRANT ALL PRIVILEGES ON DATABASE opennvr TO opennvr_admin;
\q

# Windows (in psql)
psql -U postgres
CREATE DATABASE opennvr;
CREATE USER opennvr_admin WITH PASSWORD 'your_secure_password';
GRANT ALL PRIVILEGES ON DATABASE opennvr TO opennvr_admin;
\q
```

### 3. Backend Setup

**Navigate to server directory:**
```bash
cd server
```

**Create virtual environment using `uv`:**
```bash
# Windows
uv venv .venv
.\.venv\Scripts\Activate.ps1

# Linux/Mac
uv venv .venv
source .venv/bin/activate
```

**Install dependencies:**
```bash

uv pip install -r requirements.txt
```

**Create environment file:**
```bash
# Copy template
cp env.example .env

# Edit .env file
```

**Configure `.env` file:**
```env
# Database
DATABASE_URL=postgresql://opennvr_admin:your_secure_password@localhost:5432/opennvr

# Security - Generate these!
SECRET_KEY=<use: openssl rand -hex 32>
CREDENTIAL_ENCRYPTION_KEY=<use: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())">
INTERNAL_API_KEY=<use: openssl rand -base64 32>

# MediaMTX
MEDIAMTX_API_URL=http://127.0.0.1:9997
MEDIAMTX_PLAYBACK_API_URL=http://127.0.0.1:9996
MEDIAMTX_EXTERNAL_BASE_URL=http://127.0.0.1:8889

# MediaMTX Webhook Secret - For reference/documentation only
# Generate with: openssl rand -hex 32
# You must MANUALLY replace "YOUR_SECRET_HERE" in mediamtx.local.yml (2 places)
# See step 5 for detailed instructions
MEDIAMTX_SECRET=your-secret-here-generate-with-openssl-rand-hex-32

# Recording Paths (use your local paths)
RECORDINGS_HOST_BASE=D:/opennvr/Recordings  # Windows
# RECORDINGS_HOST_BASE=/home/user/opennvr/Recordings  # Linux
RECORDINGS_CONTAINER_BASE=/app/recordings

# Development
DEBUG=True
LOG_LEVEL=DEBUG
```

**Generate secrets:**
```bash
# Windows PowerShell (from project root)
..\scripts\generate-secrets.ps1

# Linux/Mac
bash ../scripts/generate-secrets.sh
```

**Initialize database:**
```bash
# Run migrations
alembic upgrade head

# Create default admin user (optional, auto-created on first start)
python -c "from core.database import init_db; init_db()"
```

### 4. Frontend Setup

**Navigate to app directory:**
```bash
cd ../app
```

**Install dependencies:**
```bash
npm install
```

**✅ No `.env` configuration needed!**

The frontend auto-detects the backend URL:
- **Development mode** (Vite dev server): Uses Vite proxy → `http://localhost:8000`
- **Production mode** (built): Uses same origin as frontend

**Optional:** Only create `app/.env` if backend runs on non-standard port:
```bash
# Only needed if backend is NOT on localhost:8000
cp env.example .env
# Then edit .env to set VITE_API_BASE_URL=http://localhost:9000
```

### 5. MediaMTX Setup

**Download MediaMTX:**

- Windows: Download `mediamtx_v1.15.4_windows_amd64.zip`
- Linux: Download `mediamtx_v1.15.4_linux_amd64.tar.gz`
- macOS: Download `mediamtx_v1.15.4_darwin_amd64.tar.gz`

From: https://github.com/bluenviron/mediamtx/releases

**Extract to mediamtx directory:**

```bash
# Extract to opennvr/mediamtx directory
unzip mediamtx_v1.15.4_windows_amd64.zip -d mediamtx/  # Windows
# or
tar -xzf mediamtx_v1.15.4_linux_amd64.tar.gz -C mediamtx/  # Linux/Mac
```

**⚠️ IMPORTANT: Use mediamtx.local.yml for local development**

The project includes TWO MediaMTX config files:
- `mediamtx.docker.yml` - For Docker (uses environment variables, 0.0.0.0 binding)
- `mediamtx.local.yml` - For local development (uses hardcoded values, 127.0.0.1 binding)

**Configure mediamtx.local.yml with your secret:**

1. **Generate a MediaMTX webhook secret:**
```bash
# Windows PowerShell
$secret = -join ((48..57) + (97..102) | Get-Random -Count 64 | ForEach-Object {[char]$_})
Write-Host "Your MediaMTX Secret: $secret"

# Linux/Mac
openssl rand -hex 32
```

2. **Copy the generated secret**

3. **Open mediamtx.local.yml and find/replace `YOUR_SECRET_HERE` (appears in 2 places):**

   - **Line ~367** (runOnInit hook): `X-MTX-Secret: YOUR_SECRET_HERE`
   - **Line ~388** (runOnRecordSegmentComplete hook): `X-MTX-Secret: YOUR_SECRET_HERE`

   Replace both instances with your generated secret.

4. **Save the file**

**✅ mediamtx.local.yml is already configured with:**
- Localhost binding (127.0.0.1) for security
- Correct backend URLs (http://127.0.0.1:8000)
- Recording paths (update if needed)

**Update recording path in `mediamtx.local.yml` (if needed):**

Find line ~165 with `recordPath:` and update:
```yaml
# Recording path (create this directory first!)
recordPath: D:/opennvr/Recordings/%path/%Y/%m/%d  # Windows
# recordPath: /home/user/opennvr/Recordings/%path/%Y/%m/%d  # Linux/Mac
```

**Create recordings directory:**
```bash
# Windows
New-Item -ItemType Directory -Force -Path "D:\opennvr\Recordings"

# Linux/Mac
mkdir -p /home/user/opennvr/Recordings
```

### 6. AI Adapters Setup (Optional)

**Navigate to AI adapters directory:**
```bash
cd ../AI-adapters/AIAdapters
```

**Create virtual environment using `uv`:**
```bash
uv venv .venv
source .venv/bin/activate  # Linux/Mac
# or
.\.venv\Scripts\Activate.ps1  # Windows
```

**Install dependencies:**
```bash
uv pip install -r requirements.txt
```

**Create frames directory:**
```bash
mkdir -p frames
```

---

## Running the Application

### Start All Services

Open **4 separate terminals**:

**Terminal 1 - Database:**
```bash
# Already running as a service
# Or start manually if needed
postgres -D /var/lib/postgresql/data  # Linux
```

**Terminal 2 - MediaMTX:**
```bash
# Navigate to mediamtx directory
cd mediamtx

# Windows
.\mediamtx.exe ..\mediamtx.local.yml

# Linux/Mac
./mediamtx ../mediamtx.local.yml
```

**Note:** Make sure you've replaced `YOUR_SECRET_HERE` in mediamtx.local.yml first (step 5)!

**Terminal 3 - Backend:**
```bash
cd server
source .venv/bin/activate  # Linux/Mac
# or
.\.venv\Scripts\Activate.ps1  # Windows

python start.py
# Or for development with auto-reload:
uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

**Terminal 4 - Frontend:**
```bash
cd app
npm run dev
```

**Terminal 5 - AI Adapters (Optional):**
```bash
cd AI-adapters/AIAdapters
source .venv/bin/activate  # Linux/Mac
# or
.\.venv\Scripts\Activate.ps1  # Windows

python -m adapter.main
```

### Access Application

- **Frontend (Development)**: http://localhost:5173 (Vite dev server)
- **Backend API**: http://localhost:8000
- **API Docs**: http://localhost:8000/docs
- **MediaMTX API**: http://localhost:9997

**Default Credentials:**
- Username: `admin`
- Password: `admin123`

---

## Development Workflow

### Database Migrations

**Create new migration:**
```bash
cd server
alembic revision --autogenerate -m "description"
```

**Apply migrations:**
```bash
alembic upgrade head
```

**Rollback migration:**
```bash
alembic downgrade -1
```

### Code Quality

**Backend linting:**
```bash
cd server
ruff check .
black .
mypy .
```

**Frontend linting:**
```bash
cd app
npm run lint
npm run type-check
```

### Testing

**Backend tests:**
```bash
cd server
pytest
pytest --cov=. --cov-report=html
```

**Frontend tests:**
```bash
cd app
npm test
npm run test:coverage
```

### Hot Reload

- **Backend**: Use `uvicorn main:app --reload`
- **Frontend**: `npm run dev` has hot reload by default
- **MediaMTX**: Supports config reload via API

---

## Project Structure

```
opennvr/
├── server/                 # Backend (FastAPI)
│   ├── core/              # Core configuration
│   ├── routers/           # API endpoints
│   ├── services/          # Business logic
│   ├── models.py          # Database models
│   ├── schemas.py         # Pydantic schemas
│   └── main.py            # Application entry
├── app/                   # Frontend (React)
│   ├── src/
│   │   ├── components/    # React components
│   │   ├── views/         # Page views
│   │   ├── services/      # API clients
│   │   └── main.tsx       # App entry
│   └── package.json
├── AI-adapters/           # AI inference engines
│   ├── AIAdapters/        # Main AI adapter
├── kai-c/                 # KAI-C controller
├── mediamtx/              # Streaming server
│   └── mediamtx.yml       # Configuration
├── Recordings/            # Video recordings
├── docker-compose.yml     # Docker deployment
└── scripts/               # Utility scripts
```

---

## 🌐 Understanding the Network Routing (Local vs Docker)

When running OpenNVR inside Docker, the components discover each other using virtual Docker hostname DNS records (like `http://mediamtx:8889` or `http://opennvr_core:8000`). But when running as a Local Developer natively on your host machine, **you must use 127.0.0.1** to connect the services.

**Key Local Architectural differences:**
1. **MediaMTX:** You MUST use the `mediamtx.local.yml` file instead of the docker version. We pre-configured the local version to bind exclusively to `127.0.0.1:9997` instead of `0.0.0.0` to prevent exposing your unauthenticated dev environment to your home/office Wi-Fi.
2. **KAI-C (AI Orchestration):** KAI-C needs to know where the AI adapters are running. By default, when started locally it assumes the AI Adapter fastAPI server is running right next to it at `http://localhost:9100`. If you adjust the AI Adapter port, you must set the `ADAPTER_URL` environment variable for KAI-C.
3. **Database URL:** Your `server/.env` must point to `postgresql://user:pass@127.0.0.1:5432/opennvr` instead of `@db`.

---

## Troubleshooting

### Database Connection Error

**Error**: `could not connect to server: Connection refused`

**Fix**:
- Ensure PostgreSQL is running: `sudo systemctl status postgresql`
- Check DATABASE_URL in `.env`
- Verify PostgreSQL is listening: `sudo netstat -tlnp | grep 5432`

### MediaMTX Not Starting

**Error**: `bind: address already in use`

**Fix**:
- Check if port is in use: `netstat -ano | findstr :8554` (Windows) or `lsof -i :8554` (Linux)
- Kill process or change port in `mediamtx.yml`

### Frontend Build Errors

**Error**: `Cannot find module '@/components/...'`

**Fix**:
```bash
cd app
rm -rf node_modules package-lock.json
npm install
```

### Python Module Not Found

**Error**: `ModuleNotFoundError: No module named 'fastapi'`

**Fix**:
```bash
# Ensure virtual environment is activated
source .venv/bin/activate  # Linux/Mac
.\.venv\Scripts\Activate.ps1  # Windows

# Reinstall dependencies
uv pip install -r requirements.txt
```

### Migration Errors

**Error**: `alembic.util.exc.CommandError: Can't locate revision`

**Fix**:
```bash
# Reset migrations (⚠️ development only!)
cd server
rm -rf migrations/versions/*
alembic revision --autogenerate -m "initial"
alembic upgrade head
```

### Stream Not Loading

**Check MediaMTX is running:**
```bash
curl http://127.0.0.1:9997/v3/config/get
```

**Check camera path provisioned:**
```bash
curl http://127.0.0.1:9997/v3/config/paths/get/cam-1
```

### AI Inference Failing

**Check Python environment:**
```bash
cd AI-adapters/AIAdapters
which python  # Should point to .venv
pip list | grep opencv
```

**Check frames directory:**
```bash
ls -la frames/
# Should be writable by current user
```

---

## Environment Variables Reference

### Backend (`server/.env`)

| Variable | Description | Example |
|----------|-------------|---------|
| `DATABASE_URL` | PostgreSQL connection string | `postgresql://user:pass@localhost/db` |
| `SECRET_KEY` | JWT signing key (64 hex chars) | `openssl rand -hex 32` |
| `CREDENTIAL_ENCRYPTION_KEY` | Fernet encryption key | `Fernet.generate_key()` |
| `INTERNAL_API_KEY` | Inter-service auth token | `openssl rand -base64 32` |
| `MEDIAMTX_API_URL` | MediaMTX API endpoint | `http://127.0.0.1:9997` |
| `MEDIAMTX_SECRET` | MediaMTX auth token | `openssl rand -hex 16` |
| `MEDIAMTX_EXTERNAL_BASE_URL` | Browser-accessible URL | `http://127.0.0.1:8889` |
| `RECORDINGS_HOST_BASE` | Host recording path | `D:/opennvr/Recordings` |
| `DEBUG` | Enable debug mode | `True` or `False` |
| `LOG_LEVEL` | Logging verbosity | `DEBUG`, `INFO`, `WARNING` |

### Frontend (`app/.env`) - OPTIONAL

**The `app/.env` file is NOT required for standard setups!**

Frontend auto-detects backend URL:
- Development: Uses Vite proxy to `http://localhost:8000`
- Production: Uses `window.location.origin`

**Only needed if backend runs on non-standard port:**

| Variable | Description | Example |
|----------|-------------|---------|
| `VITE_API_BASE_URL` | Backend API endpoint (optional) | `http://localhost:9000` |

---

## Useful Commands

### Database Management

```bash
# Backup database
pg_dump -U opennvr_admin opennvr > backup.sql

# Restore database
psql -U opennvr_admin opennvr < backup.sql

# Connect to database
psql -U opennvr_admin -d opennvr
```

### Process Management

```bash
# Find process by port
lsof -i :8000  # Linux/Mac
netstat -ano | findstr :8000  # Windows

# Kill process
kill -9 <PID>  # Linux/Mac
taskkill /F /PID <PID>  # Windows
```

### Log Viewing

```bash
# Backend logs
tail -f server/logs/server.log

# Frontend dev server
# Logs in terminal where npm run dev is running

# MediaMTX logs
# Logs in terminal where mediamtx is running
```

---

## Next Steps

1. **Add a Camera**: Navigate to Cameras → Add Camera
2. **Enable Streaming**: Toggle "Stream" on camera card
3. **Test Playback**: Click "View Stream" button
4. **Enable Recording**: Toggle "Record" on camera card
5. **Setup AI Detection**: Navigate to AI Models → Add Model

---

## Getting Help

- **Documentation**: See other files in `docs/` directory
- **Issues**: https://github.com/open-nvr/open-nvr/issues
- **API Reference**: http://localhost:8000/docs (when running)

---

**Last Updated**: February 2026

