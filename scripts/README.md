# Scripts Directory

Utility scripts for OpenNVR setup and operation.

---

## 📜 **Available Scripts**

### **Secret Generation**

#### `generate-secrets.ps1` (Windows/PowerShell)
Generates cryptographically secure random secrets for `.env` configuration.

**Usage:**
```powershell
.\scripts\generate-secrets.ps1
```

**Generates:**
- `SECRET_KEY` - FastAPI session secret (64 chars)
- `MEDIAMTX_SECRET` - MediaMTX webhook authentication (64 chars)
- `POSTGRES_PASSWORD` - Database password (32 chars)

**Output:** Prints generated secrets to console. Copy them to your `.env` file.

---

#### `generate-secrets.sh` (Linux/Mac/Bash)
Linux/Mac equivalent of `generate-secrets.ps1`.

**Usage:**
```bash
./scripts/generate-secrets.sh
```

**Requirements:** OpenSSL installed (`openssl` command)

---

### **Project Setup**

#### `setup.bat` (Windows Batch)
One-click setup script for Windows - initializes the entire development environment.

**Usage:**
```cmd
.\scripts\setup.bat
```

**What it does:**
- Checks prerequisites (Python, Node.js, PostgreSQL)
- Creates Python virtual environment
- Installs backend dependencies
- Installs frontend dependencies
- Creates `.env` files from examples
- Runs database migrations
- Prints next steps

---

#### `setup.sh` (Linux/Mac/Bash)
Linux/Mac equivalent of `setup.bat`.

**Usage:**
```bash
./scripts/setup.sh
```

---

### **MediaMTX Management**

#### `start-mediamtx.ps1` (Windows/PowerShell)
Starts MediaMTX streaming server for **local development** with correct environment variables.

**Usage:**
```powershell
.\scripts\start-mediamtx.ps1
```

**What it does:**
1. Loads `MEDIAMTX_SECRET` from `server/.env`
2. Sets environment variables:
   - `MEDIAMTX_SECRET` (from server/.env)
   - `BACKEND_HOST=127.0.0.1`
   - `BACKEND_PORT=8000`
3. Validates configuration
4. Starts MediaMTX with `mediamtx.yml`

**Requirements:**
- `server/.env` must exist with `MEDIAMTX_SECRET` defined
- MediaMTX binary in `mediamtx/` directory

**Why needed?** 
`mediamtx.yml` uses environment variables (`${MEDIAMTX_SECRET}`, `${BACKEND_HOST}`, etc.). This script ensures they're set correctly for local development.

---

#### `start-mediamtx.sh` (Linux/Mac/Bash)
Linux/Mac equivalent of `start-mediamtx.ps1`.

**Usage:**
```bash
./scripts/start-mediamtx.sh
```

**Note:** Make executable first: `chmod +x scripts/start-mediamtx.sh`

---

## 🚀 **Quick Start Workflows**

### **Docker Deployment** (Simplest)
```bash
# 1. Copy default environment
cp .env.docker .env

# 2. Start everything
docker compose pull
docker compose up -d
```

---

### **Local Development**
```bash
# 1. Run setup script (one-time)
.\scripts\setup.bat  # Windows
./scripts/setup.sh   # Linux/Mac

# 2. Start backend (Terminal 1)
cd server
.\venv\Scripts\activate       # Windows
source venv/bin/activate      # Linux/Mac
uvicorn main:app --reload

# 3. Start MediaMTX (Terminal 2)
cd mediamtx
.\mediamtx.exe ..\mediamtx.local.yml  # Windows
./mediamtx ../mediamtx.local.yml      # Linux/Mac

# 4. Start frontend (Terminal 3)
cd app
npm run dev
```

---

## 📝 **Notes**

- **PowerShell scripts** (`.ps1`): Windows PowerShell 5.1+ or PowerShell Core 7+
- **Bash scripts** (`.sh`): Linux, macOS, or Git Bash on Windows
- **Batch scripts** (`.bat`): Windows Command Prompt

All scripts are designed to be run from the project root or scripts directory.
