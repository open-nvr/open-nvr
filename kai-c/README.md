# KAI-C (Kavach AI Connector)

KAI-C is the middleware layer between the OpenNVR NVR backend and the AI Adapters engine. The backend never talks to AIAdapters directly -- KAI-C handles routing, URL management, authentication, and response standardization.

## Why KAI-C Exists

```
Without KAI-C:     Backend ──(must know adapter URL)──> AIAdapters
With KAI-C:        Backend ──> KAI-C ──(manages URLs internally)──> AIAdapters
```

- Backend developers don't need to know or configure AI adapter URLs
- KAI-C can route to multiple adapters (different models on different ports)
- Single place to add auth, logging, and response normalization
- Cloud inference (HuggingFace) is handled transparently

## Project Structure

```
kai-c/
├── main.py              # FastAPI server (entry point, runs on port 8100)
├── kai_c/               # Python package
│   ├── __init__.py
│   ├── connector.py     # KaiConnector -- sends requests to AI Adapters
│   └── schemas.py       # KAIRequest Pydantic model (data validation)
├── test/
│   └── test.py          # Demo script: webcam -> KAI-C -> AI Adapter
├── start.py             # Development server launcher (with reload)
├── start_no_reload.py   # Production server launcher
├── pyproject.toml       # Python dependencies (uv)
├── Dockerfile           # Container build
├── .env.example         # Example environment variables
└── START_INSTRUCTIONS.md
```

## Quick Start

```bash
# 1. Install dependencies
uv sync

# 2. Make sure AIAdapters is running on port 9100

# 3. Start KAI-C
python main.py
# Runs on http://localhost:8100
# API docs at http://localhost:8100/docs
```

## Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/` | Service info and configured adapters |
| `GET` | `/health` | KAI-C liveness check |
| `GET` | `/adapters/health` | Check health of all registered AI adapters |
| `GET` | `/capabilities` | Get capabilities from all adapters |
| `GET` | `/schema` | Get response schemas (proxied from adapter) |
| `POST` | `/infer` | Run inference via connector (structured request) |
| `POST` | `/infer/local` | Forward raw request to adapter (pass-through) |
| `POST` | `/infer/cloud` | Cloud inference (HuggingFace) with auth |

## How It Works

### Local Inference Flow

```
1. Backend sends POST /infer to KAI-C (port 8100)
   {
     "camera_id": "cam_1",
     "stream_url": "opennvr://frames/cam_1/latest.jpg",
     "model_name": "yolov8",
     "task": "person_detection",
     "options": {}
   }

2. KAI-C looks up adapter URL from ADAPTER_REGISTRY
   (backend never sees this URL)

3. KAI-C reformats and forwards to AIAdapters (port 9100)
   POST http://localhost:9100/infer

4. AIAdapters returns JSON result

5. KAI-C wraps in standard response and returns to backend
```

### Cloud Inference Flow

```
1. Backend sends POST /infer/cloud
   {
     "provider": "huggingface",
     "model_name": "google/vit-base-patch16-224",
     "task": "image-classification",
     "inputs": {"image": "opennvr://frames/cam_1/latest.jpg"},
     "credential_token": "hf_token_here"
   }

2. KAI-C validates internal API key

3. KAI-C formats and sends to AIAdapters' cloud handler

4. AIAdapters calls HuggingFace API

5. Result flows back: HF -> AIAdapters -> KAI-C -> Backend
```

## Key Files Explained

### main.py

The FastAPI server. Contains:
- **ADAPTER_REGISTRY** -- maps model names to adapter URLs (internal, never exposed to users)
- `/infer` -- structured inference via `KaiConnector`
- `/infer/local` -- raw pass-through to adapter
- `/infer/cloud` -- cloud inference with provider routing and auth

### kai_c/connector.py

`KaiConnector` class that takes a `KAIRequest`, formats the payload, and POSTs to the AI adapter's `/infer` endpoint. Handles errors and returns standardized responses.

### kai_c/schemas.py

Pydantic model `KAIRequest` with fields: `camera_id`, `stream_url`, `model_name`, `task`, `options`. Validates data before sending to the adapter.

## Configuration

### Environment Variables

| Variable | Purpose | Default |
|----------|---------|---------|
| `ADAPTER_URL` | Default AI Adapter URL | `http://localhost:9100` |
| `INTERNAL_API_KEY` | Auth key for cloud inference | (empty = no auth) |

### Adapter Registry

In `main.py`, the `ADAPTER_REGISTRY` dict maps model names to URLs:

```python
ADAPTER_REGISTRY = {
    "default": "http://localhost:9100",
    # "yolov8": "http://localhost:9100",
    # "blip": "http://localhost:9101",
}
```

To add a second adapter running on a different port, add an entry here. The model_name in the request selects which adapter to use.
