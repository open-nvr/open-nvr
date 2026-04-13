# 🚀 Docker Quick Start Guide

Get OpenNVR running in **under 2 minutes** with default configuration.

---

## Prerequisites

- **Docker Desktop** installed and running
- **4GB RAM** minimum (8GB recommended)
- **10GB free disk space**

---

## Quick Start (3 Commands)

```bash
# 1. Copy the default environment file
cp .env.docker .env

# 2. Start all services
docker compose up -d

# 3. Check status
docker compose ps
```

**That's it!** 🎉

---

## Access Your Application

| Service | URL | Credentials |
|---------|-----|-------------|
| **Web UI** | http://localhost:8000 | `admin` / `SecurePass123!` |
| **API Docs** | http://localhost:8000/docs | Same as above |
| **MediaMTX HLS** | http://localhost:8888 | JWT required |
| **MediaMTX WebRTC** | http://localhost:8889 | JWT required |

---

## Default Configuration

The `.env.docker` file contains **working defaults** for local development:

✅ **Auto-configured services:**
- PostgreSQL database
- MediaMTX streaming server
- FastAPI backend
- React frontend
- AI adapters (Kai-C)

✅ **Default credentials:**
- Database: `opennvr_user` / `dev_postgres_pass_2024`
- Admin user: `admin` / `SecurePass123!`

✅ **Storage location:**
- Recordings: `./Recordings` (local directory)
- Database: Docker volume `opennvr_db_data`

---

## Common Commands

### Start Services
```bash
docker compose up -d
```

### View Logs
```bash
# All services
docker compose logs -f

# Specific service
docker compose logs -f opennvr-core
docker compose logs -f mediamtx
docker compose logs -f db
```

### Stop Services
```bash
docker compose down
```

### Restart Services
```bash
docker compose restart
```

### Rebuild After Code Changes
```bash
docker compose up -d --build
```

### Remove Everything (including data)
```bash
docker compose down -v
```

---

## Verify Installation

### 1. Check Service Health
```bash
docker compose ps
```

All services should show `Up` or `Healthy`:
```
NAME              STATUS
opennvr_core      Up (healthy)
opennvr_db        Up (healthy)
opennvr_mediamtx  Up (healthy)
opennvr_ai        Up
```

### 2. Test Backend API
```bash
curl http://localhost:8000/health
```

Expected response:
```json
{"status": "healthy"}
```

### 3. Access Web UI
Open browser: http://localhost:8000
- Login with: `admin` / `SecurePass123!`
- You should see the dashboard

---

## Troubleshooting

### Services Won't Start
```bash
# Check logs
docker compose logs

# Common issue: Port already in use
# Solution: Stop conflicting services or change ports in docker-compose.yml
```

### Database Connection Errors
```bash
# Restart database service
docker compose restart db

# Wait for health check
docker compose ps
```

### Permission Errors (Linux/Mac)
```bash
# Fix recordings directory permissions
sudo chown -R $USER:$USER ./Recordings

# Or run with sudo (not recommended)
sudo docker compose up -d
```

### Out of Disk Space
```bash
# Remove old images and containers
docker system prune -a --volumes

# Warning: This removes ALL unused Docker data
```

---

## Customization

### Change Storage Location

Edit `.env` file:
```bash
# Windows
RECORDINGS_PATH=D:/opennvr-recordings

# Linux
RECORDINGS_PATH=/var/lib/opennvr/recordings

# macOS
RECORDINGS_PATH=/Users/Shared/opennvr-recordings
```

Then restart:
```bash
docker compose down
docker compose up -d
```

### Change Admin Password

Edit `.env` file:
```bash
DEFAULT_ADMIN_PASSWORD=YourStrongPassword123!
```

Then recreate the admin user:
```bash
docker compose down -v  # ⚠️ Deletes database
docker compose up -d
```

Or change it via the Web UI after login.

### Enable Debug Mode

Edit `.env` file:
```bash
DEBUG=True
LOG_LEVEL=DEBUG
```

Restart:
```bash
docker compose restart opennvr-core
```

---

## Production Deployment

⚠️ **DO NOT use `.env.docker` in production!**

For production deployment:

### 1. Generate Secure Secrets
```bash
# Windows
.\scripts\generate-secrets.ps1

# Linux/Mac
bash scripts/generate-secrets.sh
```

### 2. Create Production .env
```bash
cp .env.example .env
# Edit .env with generated secrets
```

### 3. Review Security Settings
- Change all default passwords
- Set `DEBUG=False`
- Configure HTTPS/TLS
- Restrict network access
- Enable backups
- Review `SECURITY.md`

### 4. Deploy
```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d
```

See [DOCKER_SETUP.md](docs/DOCKER_SETUP.md) for complete production guide.

---

## Next Steps

1. **Add Cameras**
   - Go to Settings → Cameras
   - Add ONVIF or RTSP cameras

2. **Configure AI Detection**
   - Go to AI → Models
   - Enable detection models

3. **Set Up Recording**
   - Go to Cameras → Recording Settings
   - Configure retention policies

4. **Explore API**
   - Visit http://localhost:8000/docs
   - Interactive API documentation

---

## Support

- **Documentation:** [USER_MANUAL.md](USER_MANUAL.md)
- **Security:** [SECURITY.md](SECURITY.md)
- **Contributing:** [CONTRIBUTING.md](CONTRIBUTING.md)
- **Issues:** GitHub Issues

---

**Enjoy your new NVR system!** 🎥📹
