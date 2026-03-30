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

import requests
from .schemas import KAIRequest

class KaiConnector:
    def __init__(self, adapter_url="http://localhost:9100"):
        self.base_url = adapter_url

    def process_stream(self, request: KAIRequest):
        print(f"--- KAI-C: Starting Live Feed for {request.camera_id} using {request.model_name} ---")

        # 1. Prepare Payload
        # If stream_url is an integer (like 0), we keep it as an integer for the webcam
        source = request.stream_url
        
        payload = {
            "task": request.task,
            "input": {
                "frame": {
                    "uri": source  # This sends '0' (int) or 'rtsp://...' (str)
                },
                "params": request.options
            }
        }

        # 2. Call the endpoint
        full_url = f"{self.base_url}/infer"

        try:
            # Note: For a live stream, this request might "hang" open while the camera runs,
            # or it might return immediately saying "Stream Started" depending on your adapter logic.
            response = requests.post(full_url, json=payload)
            
            if response.status_code != 200:
                return {"status": "error", "message": f"Adapter failed: {response.text}"}

            return {
                "event_type": "STREAM_STARTED",
                "camera_id": request.camera_id,
                "model_used": request.model_name,
                "response": response.json()
            }

        except Exception as e:
            return {"status": "error", "message": str(e)}