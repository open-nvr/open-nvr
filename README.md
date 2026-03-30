# 🛡️ OpenNVR
**AI-Powered, Security-First Video Surveillance Platform**

Bring AI to the Edge. Own Your Security. Deploy Anywhere.

---

## 🐳 1. Docker Build & Deployment (Recommended)
This is the recommended approach for both **Linux** and **Windows** users to get the entire ecosystem talking to each other automatically. 

### Prerequisites
- Git
- Docker & Docker Compose

### Step-by-Step Build
1. **Clone the main OpenNVR repository**
   ```bash
   git clone https://github.com/cryptovoip/open-nvr.git
   cd open-nvr
   ```

2. **Clone the AI Adapters directly inside**
   Because the OpenNVR Docker environment is preconfigured to tie into the local AI Adapter, you must clone it into the root directory:
   ```bash
   git clone https://github.com/cryptovoip/AIAdapters.git
   ```

3. **Generate Security Secrets**
   ```bash
   # Copy the example format
   cp .env.docker .env 

   # Linux:
   ./scripts/generate-secrets.sh
   # Windows (PowerShell):
   .\scripts\generate-secrets.ps1
   ```
   *Take the outputted values from the script and paste them into your `.env` file.*

4. **Build and Run**
   ```bash
   # Build the container images across both repositories
   docker compose build
   
   # Start the environment in the background
   docker compose up -d
   ```

🎉 **Access the Platform:** 
- OpenNVR API Docs: `http://localhost:8000/docs`
- MediaMTX: `http://localhost:8889`
- AI Adapter API: `http://localhost:9100`

---

## 💻 2. Local Developer Setup (Without Docker)
For developers looking to run OpenNVR purely locally in an IDE utilizing local virtual environments.

### Prerequisites
- **Python 3.11+**
- **Node.js 18+**
- **PostgreSQL 13+** (Running locally on your OS)
- **MediaMTX** (Download the binary for your OS from their GitHub releases)

### Preparation
1. **Clone Both Repositories side-by-side**
   ```bash
   git clone https://github.com/cryptovoip/open-nvr.git
   git clone https://github.com/cryptovoip/AIAdapters.git
   ```
2. **Setup your environment variables**
   ```bash
   cd open-nvr
   cp server/env.example server/.env
   # Edit server/.env to point to your local PostgreSQL username and password
   ```

### Running the Services (Requires 5 Terminals)
You must start the microservices independently.

**Terminal 1: PostgreSQL & OpenNVR Backend**
```bash
cd open-nvr/server
uv venv venv

# Activate venv (Linux: source venv/bin/activate | Windows: .\venv\Scripts\activate)
uv pip install -r requirements.txt

# Migrate DB and Start
alembic upgrade head
python start.py
```

**Terminal 2: KAI-C (AI Orchestrator)**
```bash
cd open-nvr/kai-c
uv venv venv

# Activate venv (Linux: source venv/bin/activate | Windows: .\venv\Scripts\activate)
uv pip install -r requirements.txt

# Start Connector
python start.py
```

**Terminal 3: React Frontend**
```bash
cd open-nvr/app
npm install
npm run dev
# Access frontend at http://localhost:5173
```

**Terminal 4: MediaMTX**
```bash
# Extract the binary you downloaded and run it using the local config file provided in our repo:
./mediamtx open-nvr/mediamtx.local.yml
```

**Terminal 5: AI Adapters**
```bash
cd AIAdapters
uv venv venv

# Activate venv (Linux: source venv/bin/activate | Windows: .\venv\Scripts\activate)
uv pip install -r requirements.txt

# Start Adapter
uvicorn adapter.main:app --reload --port 9100
```

---

## 📖 Additional Documentation
- [User Manual](USER_MANUAL.md) - Using the Web Interface
- [Security Policy](SECURITY.md) - Core system limits and hardening
- [Contributing](CONTRIBUTING.md) - PR flow and coding standards

---

## ⚖️ License
This project is 100% open-source and licensed under the **GNU Affero General Public License v3.0 (AGPL v3)**. 
By strictly enforcing the AGPLv3, OpenNVR guarantees that any ecosystem modifications—even when utilized over an external network or distributed cloud service—must uniformly remain open-source. For full terms, please see the `LICENSE` file in the root directory.

> For enterprise commercial licensing exemptions, custom deployment support, or corporate sponsorships, please reach out directly: **[contact@cryptovoip.in](mailto:contact@cryptovoip.in)**
