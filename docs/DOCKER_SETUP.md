# OpenNVR - Docker Setup Guide

Complete guide for deploying OpenNVR using Docker containers.


---

## 🌐 Network Strategy: Windows/Mac vs Linux

OpenNVR requires specific network configurations depending on your operating system, specifically related to **ONVIF Camera Auto-discovery**. ONVIF relies on UDP Multicast packets which do not route through default Docker Bridges.

### Windows & macOS (BRIDGE MODE)
*This is the default configuration out-of-the-box.*
Docker on Windows/Mac runs inside a hidden VM. It **cannot** bind directly to your host's physical network card.
- **How it runs:** Services communicate via Docker's internal DNS (e.g., http://mediamtx:8889 or 	cp://db:5432). 
- **Limitation:** ONVIF Auto-discovery will NOT work. You must add cameras manually via their IP address.

### Linux (HOST MODE)
Because Linux runs Docker natively, containers can attach directly to your physical network interface, perfectly enabling UDP Multicast rules for fast ONVIF auto-discovery!
**To enable Host Mode on Linux:**
1. Open \docker-compose.yml\.
2. On **EVERY** service (\db\, \mediamtx\, \opennvr-core\, \i-adapters\):
   - Uncomment: etwork_mode: host   - Comment out: the entire \ports:\ array block.
   - Comment out: the entire etworks:\ array block.
3. Under the \opennvr-core\ environment section:
   - Comment out all the variables under \============ BRIDGE MODE ============   - Uncomment all the variables under \============ HOST MODE ============
This switches the internal routing from Docker's virtual DNS back to raw W.0.0.1\ binding.


---

## Prerequisites

- **Docker Desktop** installed and running
- **4GB RAM** minimum (8GB recommended)
- **10GB free disk space** (plus storage for recordings)
- **Windows 10/11**, **Ubuntu 22.04+**, or **macOS**

---

## Quick Start (2 Minutes!)

### 1. Clone Repository

```bash
git clone https://github.com/open-nvr/open-nvr.git
cd opennvr
```

### 2. Configure Environment (Optional)

**Use defaults** (recommended for first-time setup):
```bash
# Copy pre-configured environment file with working defaults
cp .env.docker .env
```

**Or customize** (if you want to change settings):
```bash
# Copy and edit the environment file
cp .env.docker .env

# Windows
notepad .env

# Linux/Mac
nano .env
```

The `.env.docker` file includes:
- ✅ **Pre-generated Fernet encryption key** (valid and tested)
- ✅ **Default database credentials** (change in production!)
- ✅ **Working MediaMTX webhook secret**
- ✅ **Cross-platform recording paths** (Windows/Linux/macOS)

**Important settings you may want to customize:**

```env
# Database password (recommended to change)
POSTGRES_PASSWORD=opennvr_secure_db_pass_2024

# Recording storage location
RECORDINGS_PATH=D:/Recordings                    # Windows
# RECORDINGS_PATH=/var/lib/opennvr/recordings      # Linux/Mac

# For production, also change:
# SECRET_KEY, CREDENTIAL_ENCRYPTION_KEY, MEDIAMTX_SECRET
```

### 3. Create Recordings Directory

```bash
# Windows PowerShell
New-Item -ItemType Directory -Force -Path "D:\Recordings"

# Linux/Mac
mkdir -p /var/lib/opennvr/recordings
```

### 4. Pull and Start Containers

```bash
# Pull latest images from Docker Hub
docker compose pull

# Start all services in background
docker compose up -d

# Check status
docker compose ps
```

**That's it!** 🎉 All services are now running.

### 5. Access Application

Open browser to: **http://localhost:8000**

**Default Credentials:**
- Username: `admin`
- Password: `admin123`

⚠️ **Change password immediately after first login!**

---

## Container Architecture

The Docker Compose stack builds or pulls the following images:

| Service | Image | Purpose |
|---------|-------|---------|
| **opennvr_core** | Built locally from `Dockerfile` | FastAPI backend + React frontend + Kai-C AI |
| **opennvr_db** | `postgres:15-alpine` | PostgreSQL database |
| **opennvr_mediamtx** | Built from `bluenviron/mediamtx:1.15.4` + curl | Streaming server (RTSP/HLS/WebRTC) |
| **opennvr_ai** | Built locally from `../ai-adapter/Dockerfile` | AI inference engine with model adapters |

**Networks:**
- `sentinel_internal` - Internal communication between services
- `public_uplink` - AI adapters internet access for cloud inference

**Volumes:**
- `${RECORDINGS_PATH}:/app/recordings` - Recording storage (path from `.env`)
- `./mediamtx.docker.yml:/mediamtx.yml` - MediaMTX configuration for Docker
- `opennvr_db_data` - Database persistence
- `shared_frames` - AI frame processing between services

**Port Mappings:**
- `8000` - Web UI and API
- `8554` - RTSP streaming
- `8888` - HLS streaming
- `8889` - WebRTC streaming
- `9997` - MediaMTX Admin API
- `9996` - MediaMTX Playback API

**Security Architecture:**
- ✅ **No hardcoded secrets** - All secrets loaded from `.env` file
- ✅ **Official MediaMTX base** - Pinned to `bluenviron/mediamtx:1.15.4`, curl added via Alpine package manager
- ✅ **Configuration via environment variables** - `mediamtx.docker.yml` uses `${MEDIAMTX_SECRET}`, `${BACKEND_HOST}`, etc.
- ✅ **Single source of truth** - All configuration in `.env` file

---

## Verification

### Check Services Status

```bash
docker compose ps
```

Expected output:
```
NAME              IMAGE                          STATUS
opennvr_core      opennvr-opennvr-core           Up
opennvr_db        postgres:15-alpine             Up (healthy)
opennvr_mediamtx  opennvr-mediamtx               Up (healthy)
opennvr_ai        opennvr-ai-adapters            Up
```

### View Logs

```bash
# All services
docker compose logs -f

# Specific service
docker compose logs -f opennvr-core
docker compose logs -f mediamtx
docker compose logs -f ai-adapters
```

### Test Streaming

1. Login to web UI
2. Add a camera (RTSP URL)
3. Enable streaming
4. Click "View Stream" - should see live video

### Test Recording

1. Enable recording for a camera
2. Wait 5 minutes
3. Check recording directory: `D:\Recordings\cam-1\`
4. Should see `.mp4` segment files

### Test AI Detection

1. Add AI model via UI
2. Enable inference for a camera
3. Check AI results: `http://localhost:8000/api/v1/ai-model-management/inference/running`

---

## Configuration

### Customizing .env File

All configuration is done in the `.env` file (copied from `.env.docker`). No need to edit `docker-compose.yml`!

**Common Customizations:**

**Database Configuration:**
```env
POSTGRES_USER=opennvr_user
POSTGRES_PASSWORD=your_secure_password_here
POSTGRES_DB=opennvr_db
```

**Backend Security Secrets:**
```env
SECRET_KEY=your_64_character_hex_secret_here
CREDENTIAL_ENCRYPTION_KEY=your_fernet_key_here==
INTERNAL_API_KEY=your_base64_api_key_here
MEDIAMTX_SECRET=your_mediamtx_webhook_secret_here
```

**MediaMTX External URL** (for remote access):
```env
MEDIAMTX_EXTERNAL_BASE_URL=http://192.168.1.100:8889
```
Change to your server's IP if accessing from other devices on the network.

**Recording Storage Path:**
```env
# Windows
RECORDINGS_PATH=D:/Recordings

# Linux/Mac
RECORDINGS_PATH=/var/lib/opennvr/recordings
```

**Optional Settings:**
```env
DEBUG=False
LOG_LEVEL=INFO
```

### Custom Recording Path

1. Create directory on host:
```bash
# Windows
New-Item -ItemType Directory -Force -Path "E:\MyRecordings"

# Linux/Mac
mkdir -p /mnt/storage/recordings
```

2. Update `.env`:
```env
RECORDINGS_PATH=E:/MyRecordings           # Windows
# RECORDINGS_PATH=/mnt/storage/recordings  # Linux
```

3. Restart services:
```bash
docker compose restart
```

### Network Configuration

To expose on local network (access from other devices), you can create a `.env.local` override or modify docker-compose.yml ports directly:

In `docker-compose.yml`, change:
```yaml
ports:
  - "0.0.0.0:8000:8000"  # Allow external access
```

⚠️ **Security Warning**: Only expose if behind a firewall!

---

## Maintenance

### Update to Latest Version

```bash
# Pull latest images
docker compose pull

# Restart with new images
docker compose up -d

# Verify
docker compose images
```

### Backup Database

```bash
# Create backup
docker exec opennvr_db pg_dump -U opennvr_user opennvr_db > backup.sql

# Restore backup
cat backup.sql | docker exec -i opennvr_db psql -U opennvr_user opennvr_db
```

### Backup Recordings

Simply copy the recordings directory:
```bash
cp -r D:/Recordings D:/Recordings-backup
```

### View Resource Usage

```bash
docker stats
```

### Restart Services

```bash
# All services
docker compose restart

# Single service
docker compose restart opennvr-core
```

---

## Troubleshooting

### "Cannot find .env file" or "Missing required environment variables"

**Cause**: `.env` file not created

**Fix**:
```bash
# Copy the default environment file
cp .env.docker .env

# Restart services
docker compose up -d
```

### "Cannot connect to database"

**Check database health:**
```bash
docker compose ps db
docker compose logs db | grep "ready to accept connections"
```

**Fix**: Wait 30 seconds and retry, or restart database:
```bash
docker compose restart db
```

### "Login Failed" or "500 Internal Server Error"

**Check backend logs:**
```bash
docker compose logs opennvr-core | grep -i error
```

**Common causes:**
- Database migration needed (automatic on first start)
- Invalid SECRET_KEY (regenerate and restart)

### Stream Not Loading

**Check MediaMTX:**
```bash
docker compose logs mediamtx | grep -i error
```

**Verify camera connectivity:**
```bash
docker exec opennvr_core curl http://mediamtx:9997/v3/config/paths/get/cam-1
```

### AI Inference Not Working

**Check frames directory permissions:**
```bash
docker exec opennvr_core ls -la /app/AI-adapters/AIAdapters/frames/
```

**Check AI adapter logs:**
```bash
docker compose logs ai-adapters
```

### Port Already in Use

**Change port in docker-compose.yml:**
```yaml
ports:
  - "8001:8000"  # Use 8001 instead of 8000
```

Then access at `http://localhost:8001`

### Permission Denied (Linux)

```bash
sudo chown -R $USER:$USER Recordings
chmod -R 755 Recordings
```

---

## Stopping and Cleanup

### Stop Services

```bash
# Stop all containers (data preserved)
docker compose down

# Stop and remove volumes (⚠️ DELETES ALL DATA!)
docker compose down -v
```

### Complete Uninstallation

```bash
# Stop and remove containers
docker compose down -v

# Remove images
docker rmi opennvr-opennvr-core
docker rmi opennvr-mediamtx
docker rmi opennvr-ai-adapters
docker rmi bluenviron/mediamtx:1.15.4
docker rmi postgres:15-alpine

# Remove project directory
cd ..
rm -rf opennvr
```

---

## Security Checklist

Before exposing to any network:

- [ ] Copied `.env.docker` to `.env`
- [ ] Changed database password in `.env` (POSTGRES_PASSWORD)
- [ ] Generated unique secrets in `.env` (SECRET_KEY, CREDENTIAL_ENCRYPTION_KEY)
- [ ] Changed admin password via web UI
- [ ] Set `DEBUG=False` in `.env`
- [ ] Verified all ports bound to `127.0.0.1` (localhost only) in docker-compose.yml
- [ ] Enabled firewall on host system
- [ ] Regular backups configured

---

## Advanced Configuration

### Custom Docker Network

```bash
# Create external network
docker network create opennvr_network

# Update docker-compose.yml
networks:
  external_network:
    external: true
    name: opennvr_network
```

### Resource Limits

Add to service in `docker-compose.yml`:
```yaml
deploy:
  resources:
    limits:
      cpus: '2'
      memory: 4G
    reservations:
      memory: 2G
```

### Health Checks

Already configured for database. Add for other services:
```yaml
healthcheck:
  test: ["CMD", "curl", "-f", "http://localhost:8000/api/v1/health"]
  interval: 30s
  timeout: 10s
  retries: 3
```

---

## Getting Help

- **Issues**: https://github.com/open-nvr/open-nvr/issues
- **Logs**: Always include output of `docker compose logs` when reporting issues
- **System Info**: Include OS, Docker version (`docker --version`), and compose version

---

**Last Updated**: February 2026
