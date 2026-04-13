# KAI-C Service Startup Instructions

## Architecture

```
Frontend (Port 5173)
    ↓
Backend (Port 8000)
    ↓
KAI-C Service (Port 8100) ← You need to start this!
    ↓
AI Adapter (Port 9100)
```

## How to Start KAI-C Service

### Step 1: Activate Virtual Environment

```powershell
cd d:\opennvr\kai-c
.\venv\Scripts\Activate.ps1
```

If you don't have a virtual environment yet, create one using `uv`:

```powershell
cd d:\opennvr\kai-c
uv venv venv
.\venv\Scripts\Activate.ps1
```

### Step 2: Install Dependencies

```powershell
uv sync
```

### Step 3: Start KAI-C Service

```powershell
python start.py
```

Or using uvicorn directly:

```powershell
uvicorn main:app --host 0.0.0.0 --port 8100 --reload
```

### Step 4: Verify KAI-C is Running

Open your browser and check:
- Health Check: http://localhost:8100/health
- API Docs: http://localhost:8100/docs

## Full System Startup Order

1. **AI Adapter** (Port 9100) - Start first
2. **KAI-C Service** (Port 8100) - Start second (THIS SERVICE)
3. **Backend** (Port 8000) - Already running
4. **Frontend** (Port 5173) - Already running

## Testing the Flow

Once KAI-C is running:

1. Go to your frontend: http://localhost:5173
2. Navigate to AI Models page
3. Enter AI Adapter URL: `http://localhost:9100`
4. Click "Test" to check connection
5. Click "Fetch Tasks" to get available tasks
6. Select a camera and task, then click "Start Inference"

The request will flow: Frontend → Backend → **KAI-C** → AI Adapter

## Troubleshooting

### KAI-C won't start
- Make sure port 8100 is not already in use
- Check that dependencies are installed: `uv tree`
- Ensure you're in the correct directory

### Backend can't connect to KAI-C
- Verify KAI-C is running: http://localhost:8100/health
- Check firewall settings
- Review backend logs for connection errors

### KAI-C can't connect to AI Adapter
- Verify AI Adapter is running: http://localhost:9100/health
- Check the adapter URL in your frontend configuration
