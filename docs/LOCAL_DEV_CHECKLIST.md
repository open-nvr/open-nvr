# Local Development Configuration Checklist

Quick reference for configuring OpenNVR NVR for local development in VS Code/IDE.

---

## Critical Configuration Differences: Docker vs Local

| Setting | Docker Mode | Local Dev Mode |
|---------|-------------|----------------|
| Config File | `mediamtx.docker.yml` | `mediamtx.local.yml` |
| Backend URL | `http://opennvr_core:8000` | `http://127.0.0.1:8000` |
| MediaMTX Binding | `0.0.0.0` (all interfaces) | `127.0.0.1` (localhost only) |
| MediaMTX Secret | Environment variable `${MEDIAMTX_SECRET}` | Hardcoded in mediamtx.local.yml |
| KAI-C URL | `http://ai-adapters:9100` | `http://localhost:8100` |

---

## Setup Checklist

### ✅ 1. Backend Configuration (`server/.env`)

**File**: `server/.env`

**Required Settings:**
```env
# Database - local PostgreSQL
DATABASE_URL=postgresql://opennvr_admin:your_password@localhost:5432/opennvr

# MediaMTX URLs - localhost
MEDIAMTX_API_URL=http://127.0.0.1:9997
MEDIAMTX_PLAYBACK_URL=http://127.0.0.1:9996
MEDIAMTX_EXTERNAL_BASE_URL=http://127.0.0.1:8889

# MediaMTX Webhook Secret - For reference only (not used by standalone MediaMTX)
# You must use this same value in mediamtx.local.yml
# Generate with: openssl rand -hex 32
MEDIAMTX_SECRET=your-generated-secret-here

# KAI-C URL - localhost
KAI_C_URL=http://localhost:8100
KAI_C_IP=127.0.0.1

# Recording paths - local directories
RECORDINGS_HOST_BASE=D:/opennvr/Recordings  # Windows
# RECORDINGS_HOST_BASE=/home/user/opennvr/Recordings  # Linux

# Development mode
DEBUG=True
LOG_LEVEL=DEBUG
```

---

### ✅ 2. MediaMTX Configuration (`mediamtx.local.yml`)

**File**: `mediamtx.local.yml` (in project root)

**⚠️ IMPORTANT:** Use `mediamtx.local.yml` for local development, NOT `mediamtx.docker.yml`!

**Required Changes:**

**Change #1: Replace Hardcoded Secret Placeholder (2 locations)**

1. Generate a webhook secret:
```bash
# Windows PowerShell
$secret = -join ((48..57) + (97..102) | Get-Random -Count 64 | ForEach-Object {[char]$_})
Write-Host "Your MediaMTX Secret: $secret"

# Linux/Mac
openssl rand -hex 32
```

2. Open `mediamtx.local.yml` and find/replace `YOUR_SECRET_HERE` with your generated secret:

```yaml
# Line ~367: Startup webhook
runOnInit: 'curl -X GET -H "X-MTX-Secret: your_actual_secret_here" "http://127.0.0.1:8000/api/v1/mediamtx/startup/hook?delay=5"'

# Line ~388: Recording webhook - use SAME secret as above
runOnRecordSegmentComplete: 'curl -X GET -H "X-MTX-Secret: your_actual_secret_here" "http://127.0.0.1:8000/api/v1/mediamtx/hooks/segment-complete?path=$MTX_PATH&segment_path=$MTX_SEGMENT_PATH&segment_duration=$MTX_SEGMENT_DURATION"'
```

**Change #2: Update Recording Path (if needed)**

Line ~165:
```yaml
# Windows
recordPath: D:/opennvr/Recordings/%path/%Y/%m/%d

# Linux/Mac
recordPath: /home/user/opennvr/Recordings/%path/%Y/%m/%d
```

**✅ Already configured correctly in mediamtx.local.yml:**
- ✅ Localhost binding (`apiAddress: 127.0.0.1:9997`)
- ✅ Backend URLs use `127.0.0.1:8000`
- ✅ All services bound to localhost for security

---

### ✅ 3. Frontend Configuration (`app/.env`)

**File**: `app/.env`

```env
VITE_API_URL=http://localhost:8000
VITE_WS_URL=ws://localhost:8000
```

---

### ✅ 4. Create Required Directories

```bash
# Windows
New-Item -ItemType Directory -Force -Path "D:\opennvr\Recordings"

# Linux/Mac
mkdir -p /home/user/opennvr/Recordings
```

---

## Quick Verification

### Before Starting Services

- [ ] `server/.env` exists with all required settings
- [ ] `mediamtx.local.yml` exists (NOT using mediamtx.docker.yml)
- [ ] Replaced `YOUR_SECRET_HERE` in `mediamtx.local.yml` (2 places) with actual secret
- [ ] `mediamtx.local.yml` webhooks use `127.0.0.1:8000` (already configured)
- [ ] `KAI_C_URL` in `server/.env` is `http://localhost:8100`
- [ ] Recording directory exists and has write permissions
 - [ ] PostgreSQL is running and database is created
- [ ] MediaMTX binary is extracted to `mediamtx/` directory

### After Starting Services

Test each service individually:

```bash
# 1. Test Backend
curl http://localhost:8000/api/v1/health

# 2. Test MediaMTX API
curl http://127.0.0.1:9997/v3/config/get

# 3. Test MediaMTX Playback
curl http://127.0.0.1:9996/v3/segments/list

# 4. Test KAI-C (if running)
curl http://localhost:8100/health
```

---

## Common Mistakes

### ❌ Using Docker config file in local mode
```bash
# WRONG - This is for Docker:
mediamtx mediamtx.docker.yml
```

```bash
# CORRECT - For local development:
mediamtx mediamtx.local.yml
```

### ❌ Using Docker service names in local mode
```env
# WRONG - This is for Docker:
KAI_C_URL=http://ai-adapters:9100
MEDIAMTX_API_URL=http://opennvr-mediamtx:9997
```

```env
# CORRECT - For local development:
KAI_C_URL=http://localhost:8100
MEDIAMTX_API_URL=http://127.0.0.1:9997
```

### ❌ Forgot to replace placeholder secret
```yaml
# mediamtx.local.yml - WRONG
X-MTX-Secret: YOUR_SECRET_HERE  # Still has placeholder!
```
```yaml
# mediamtx.local.yml - CORRECT
X-MTX-Secret: c50e72422be77e0c...  # Actual generated secret
```

### ❌ Using wrong config file
```yaml
# WRONG - mediamtx.docker.yml uses environment variable substitution:
X-MTX-Secret: ${MEDIAMTX_SECRET}  # Won't work in standalone MediaMTX

# CORRECT - mediamtx.local.yml uses hardcoded values:
X-MTX-Secret: c50e72422be77e0c...  # Direct value
```

---

## Troubleshooting

### Backend can't connect to MediaMTX
**Error**: `Connection refused` to MediaMTX API

**Fix**: 
1. Check MediaMTX is running with `mediamtx.local.yml`
2. Check `MEDIAMTX_API_URL` in `server/.env` is `http://127.0.0.1:9997`

### Webhook authentication failed
**Error**: `Invalid MediaMTX secret`

**Fix**: Ensure you replaced `YOUR_SECRET_HERE` in `mediamtx.local.yml` (2 places, lines ~367 and ~388)

### Wrong config file loaded
**Error**: MediaMTX logs show "cannot expand environment variable" or "invalid character '$'"

**Fix**: You're using `mediamtx.docker.yml` by mistake. Use `mediamtx.local.yml` instead:
```bash
cd mediamtx
mediamtx.exe ..\mediamtx.local.yml  # Windows
./mediamtx ../mediamtx.local.yml     # Linux/Mac
```

### AI inference failing
**Error**: `Cannot connect to KAI-C service`

**Fix**: 
1. Check `KAI_C_URL=http://localhost:8100` in `server/.env`
2. Ensure KAI-C service is running: `cd kai-c && python main.py`

### Recording segments not appearing
**Fix**:
1. Check recording directory exists and has write permissions
2. Check `recordPath` in `mediamtx.local.yml` points to correct local directory
3. Check webhook `runOnRecordSegmentComplete` uses `127.0.0.1:8000` (already configured)

---

## Need Help?

See full setup guide: [LOCAL_SETUP.md](LOCAL_SETUP.md)

For Docker deployment: [DOCKER_SETUP.md](DOCKER_SETUP.md)

