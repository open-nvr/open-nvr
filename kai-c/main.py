# Copyright (c) 2026 OpenNVR
# This file is part of OpenNVR.
# 
# OpenNVR is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
# 
# OpenNVR is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
# 
# You should have received a copy of the GNU Affero General Public License
# along with OpenNVR.  If not, see <https://www.gnu.org/licenses/>.

"""
KAI-C HTTP Service - Middleware between OpenNVR NVR and AI Adapters

This service runs as a standalone HTTP server that:
1. Receives requests from OpenNVR Backend
2. Forwards them to AI Adapters
3. Returns standardized responses
"""

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Dict, Any, Optional
import uvicorn
import requests
import os
from fastapi import Header

from kai_c.connector import KaiConnector
from kai_c.schemas import KAIRequest

app = FastAPI(
    title="KAI-C (Kavach AI Connector)",
    description="Middleware connector between OpenNVR Kavach and AI Adapters",
    version="1.0.0"
)

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ============================================================
# ADAPTER REGISTRY - KAI-C manages all AI Adapter URLs
# Users NEVER see or configure these URLs
# ============================================================
ADAPTER_REGISTRY = {
    "default": os.getenv("ADAPTER_URL", "http://localhost:9100"),  # Default AI Adapter
    # Add more adapters here as needed:
    # "yolov8": "http://localhost:9100",
    # "blip": "http://localhost:9101",
    # "insightface": "http://localhost:9102",
}

def get_adapter_url(model_name: str = "default") -> str:
    """Get AI Adapter URL from internal registry."""
    return ADAPTER_REGISTRY.get(model_name, ADAPTER_REGISTRY["default"])


class InferenceRequest(BaseModel):
    """Request model for inference endpoint"""
    camera_id: str
    stream_url: str
    model_name: str
    task: str
    options: Optional[Dict[str, Any]] = {}


class InferenceResponse(BaseModel):
    """Response model for inference endpoint"""
    status: str
    camera_id: Optional[str] = None
    model_used: Optional[str] = None
    event_type: Optional[str] = None
    response: Optional[Dict[str, Any]] = None
    message: Optional[str] = None


class CloudInferenceRequest(BaseModel):
    """Request model for cloud inference endpoint"""
    provider: str
    model_name: str
    task: str
    inputs: Dict[str, Any]
    parameters: Optional[Dict[str, Any]] = {}
    credential_token: str


class CloudInferenceResponse(BaseModel):
    """Response model for cloud inference endpoint"""
    status: str
    task: str
    model_name: str
    result: Optional[Any] = None
    latency_ms: int
    executed_at: str
    error: Optional[str] = None


# Internal API key for authentication between opennvr and kai-c
INTERNAL_API_KEY = os.getenv("INTERNAL_API_KEY", "")


@app.get("/")
async def root():
    """Root endpoint"""
    return {
        "service": "KAI-C (Kavach AI Connector)",
        "version": "1.0.0",
        "status": "running",
        "configured_adapters": list(ADAPTER_REGISTRY.keys())
    }


@app.post("/infer", response_model=InferenceResponse)
async def process_inference(request: InferenceRequest):
    """
    Process inference request through KAI-C connector.
    
    This endpoint:
    1. Receives request from OpenNVR Backend (NO adapter URL from user!)
    2. KAI-C looks up the correct AI Adapter from internal registry
    3. Forwards request to AI Adapter
    4. Returns standardized response
    
    Flow: OpenNVR Backend → KAI-C → AI Adapter (from registry) → KAI-C → OpenNVR Backend
    """
    try:
        # Get AI Adapter URL from internal registry (user never provides this!)
        adapter_url = get_adapter_url(request.model_name)
        
        # Create KAI-C connector for the adapter
        connector = KaiConnector(adapter_url=adapter_url)
        
        # Create KAI request
        kai_request = KAIRequest(
            camera_id=request.camera_id,
            stream_url=request.stream_url,
            model_name=request.model_name,
            task=request.task,
            options=request.options
        )
        
        # Process through connector (forwards to AI Adapter)
        result = connector.process_stream(kai_request)
        
        # Check if there's an error
        if result.get("status") == "error":
            return InferenceResponse(
                status="error",
                message=result.get("message", "Unknown error from AI Adapter")
            )
        
        # Return success response
        return InferenceResponse(
            status="success",
            camera_id=result.get("camera_id", request.camera_id),
            model_used=result.get("model_used", request.model_name),
            event_type=result.get("event_type", "INFERENCE_COMPLETE"),
            response=result.get("response", result)
        )
        
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"KAI-C processing error: {str(e)}"
        )


@app.post("/infer/local")
async def process_local_inference(request: dict):
    """
    Process local AI inference request through KAI-C.
    
    This endpoint accepts the task/input format from the backend
    and forwards it to the AI Adapter.
    
    Request format:
    {
        "task": "person_detection",
        "input": {
            "frame": {"uri": "kavach://frames/camera_1/latest.jpg"}
        }
    }
    
    Flow: OpenNVR Backend → KAI-C → AI Adapter → KAI-C → OpenNVR Backend
    """
    try:
        adapter_url = get_adapter_url()
        
        # Forward request directly to AI Adapter
        response = requests.post(
            f"{adapter_url}/infer",
            json=request,
            timeout=60
        )
        response.raise_for_status()
        result = response.json()
        
        # Return AI Adapter response wrapped in standard format
        return {
            "status": "success",
            "response": result
        }
        
    except requests.HTTPError as e:
        raise HTTPException(
            status_code=e.response.status_code if e.response else 500,
            detail=f"AI Adapter error: {str(e)}"
        )
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"KAI-C processing error: {str(e)}"
        )


@app.post("/infer/cloud", response_model=CloudInferenceResponse)
async def process_cloud_inference(
    request: CloudInferenceRequest,
    x_internal_api_key: Optional[str] = Header(None)
):
    """
    Process cloud AI inference request.
    
    This endpoint:
    1. Validates internal API key from opennvr
    2. Routes to cloud provider handler (e.g., HuggingFace)
    3. Returns unified response format
    
    Flow: OpenNVR Backend → KAI-C → Cloud Provider API → KAI-C → OpenNVR Backend
    """
    # Validate internal API key
    if INTERNAL_API_KEY and x_internal_api_key != INTERNAL_API_KEY:
        raise HTTPException(
            status_code=401,
            detail="Unauthorized: Invalid internal API key"
        )
    
    try:
        # Route to appropriate cloud provider
        if request.provider == "huggingface":
            result = await _process_huggingface_inference(request)
        else:
            raise HTTPException(
                status_code=400,
                detail=f"Unsupported provider: {request.provider}"
            )
        
        return CloudInferenceResponse(**result)
    
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Cloud inference error: {str(e)}"
        )


async def _process_huggingface_inference(request: CloudInferenceRequest) -> Dict[str, Any]:
    """
    Process Hugging Face inference via AI Adapter.
    
    Routes request to AI Adapter's HuggingFaceHandler.
    """
    adapter_url = get_adapter_url()
    
    # Prepare payload for AI Adapter's /infer endpoint
    payload = {
        "task": request.task,
        "input_data": {
            "model_name": request.model_name,
            "inputs": request.inputs,
            "parameters": request.parameters,
            "api_token": request.credential_token
        }
    }
    
    try:
        response = requests.post(
            f"{adapter_url}/infer",
            json=payload,
            timeout=60
        )
        response.raise_for_status()
        
        result = response.json()
        
        # Transform to unified format
        return {
            "status": result.get("status", "success"),
            "task": request.task,
            "model_name": request.model_name,
            "result": result.get("result") or result.get("response"),
            "latency_ms": result.get("latency_ms", 0),
            "executed_at": result.get("executed_at", ""),
            "error": result.get("error")
        }
    
    except requests.HTTPError as e:
        raise HTTPException(
            status_code=e.response.status_code if e.response else 500,
            detail=f"AI Adapter error: {str(e)}"
        )
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to call AI Adapter: {str(e)}"
        )


@app.get("/health")
async def health():
    """Health check endpoint"""
    return {
        "status": "ok",
        "service": "kai-c",
        "message": "KAI-C is running and ready to process requests"
    }


@app.get("/adapters/health")
async def check_adapters_health():
    """
    Check health of all configured AI Adapters.
    
    Flow: Backend → KAI-C → (checks internal adapters)
    
    Returns status of all adapters in the registry.
    """
    results = {}
    for name, url in ADAPTER_REGISTRY.items():
        try:
            response = requests.get(f"{url}/health", timeout=5)
            if response.status_code == 200:
                results[name] = {"status": "ok", "url": url}
            else:
                results[name] = {"status": "error", "url": url, "message": f"Returned {response.status_code}"}
        except Exception as e:
            results[name] = {"status": "error", "url": url, "message": str(e)}
    
    return {
        "kai_c_status": "ok",
        "adapters": results
    }


@app.get("/capabilities")
async def get_all_capabilities():
    """
    Get capabilities from all configured AI Adapters.
    
    Flow: Backend → KAI-C → (queries all internal adapters)
    
    Returns combined capabilities from all adapters.
    """
    all_capabilities = {
        "kai_c": {
            "version": "1.0.0",
            "service": "kai-c"
        },
        "adapters": {}
    }
    
    for name, url in ADAPTER_REGISTRY.items():
        try:
            response = requests.get(f"{url}/capabilities", timeout=10)
            response.raise_for_status()
            all_capabilities["adapters"][name] = {
                "url": url,
                "capabilities": response.json()
            }
        except Exception as e:
            all_capabilities["adapters"][name] = {
                "url": url,
                "error": str(e)
            }
    
    return all_capabilities


@app.get("/schema")
async def get_schemas(task: Optional[str] = None):
    """
    Get schemas from AI Adapters.
    
    Flow: Backend → KAI-C → (queries internal adapters)
    """
    adapter_url = get_adapter_url()
    try:
        params = {"task": task} if task else {}
        response = requests.get(
            f"{adapter_url}/schema",
            params=params,
            timeout=10
        )
        response.raise_for_status()
        return response.json()
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to get schema: {str(e)}"
        )


if __name__ == "__main__":
    print("=" * 60)
    print("Starting KAI-C (Kavach AI Connector) Service")
    print("=" * 60)
    print("Running on: http://localhost:8100")
    print("API Docs: http://localhost:8100/docs")
    print("=" * 60)
    
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=8100,
        log_level="info"
    )
