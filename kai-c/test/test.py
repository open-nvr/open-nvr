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

import sys
import os
import cv2
import time

# --- CRITICAL FIX START ---
# Get the absolute path of the current script's directory (D:\Ageis AI\kai_c\test)
current_script_dir = os.path.dirname(os.path.abspath(__file__))

# Go up one level to the project root (D:\Ageis AI\kai_c)
project_root = os.path.abspath(os.path.join(current_script_dir, ".."))

# Add this root path to Python's search list
if project_root not in sys.path:
    sys.path.insert(0, project_root)
# --- CRITICAL FIX END ---

# Now these imports will work because Python knows to look in the project root
from kai_c.schemas import KAIRequest
from kai_c.connector import KaiConnector

# --- CONFIGURATION ---
# Point this to where your AI Adapter saves frames
# Updated to match the path expected by the adapter in the current workspace
FRAMES_DIR = r"D:\myWorksace\AI-adapters\AIAdapters\frames"
CURRENT_FRAME_PATH = os.path.join(FRAMES_DIR, "live_feed.jpg")
RTSP_URL = "rtsp://admin:India%40123@192.168.1.102:554/1/1"

# Ensure the frames directory exists
if not os.path.exists(FRAMES_DIR):
    os.makedirs(FRAMES_DIR)

# --- MAIN LOGIC ---
if __name__ == "__main__":
    connector = KaiConnector()
    print(f"--- STARTING LIVE CAMERA FEED (Press 'Ctrl+C' to stop) ---")
    print(f"Project Root Detected: {project_root}")
    print(f"Connecting to RTSP Source: {RTSP_URL}")
    
    cap = cv2.VideoCapture(RTSP_URL)

    if not cap.isOpened():
        print(f"Error: Could not open streaming source: {RTSP_URL}")
        exit()

    try:
        while True:
            ret, frame = cap.read()
            if not ret:
                print("Failed to grab frame (stream might be disconnected)")
                # For RTSP, we might want to try reconnecting instead of breaking, 
                # but for this test script, breaking is fine.
                break

            # --- DEBUGGING STREAM QUALITY ---
            if frame is not None:
                height, width, _ = frame.shape
                # Check if image is completely black
                if frame.sum() == 0:
                    print(f"[WARNING] Frame captured but it is COMPLETELY BLACK. Resolution: {width}x{height}")
                else:
                    print(f"[DEBUG] Frame valid. Res: {width}x{height}. Saving...")
            # --------------------------------

            # Save frame for AI Adapter
            cv2.imwrite(CURRENT_FRAME_PATH, frame)

            # Request
            req = KAIRequest(
                camera_id="RTSP_CAM_01",
                stream_url="live_feed.jpg",
                model_name="yolov8",
                task="person_detection"
            )

            # Process
            result = connector.process_stream(req)

            # --- UPDATED OUTPUT SECTION ---
            # This prints the FULL RAW JSON containing all details
            print(f"\nRESULT: {result}")
            # ------------------------------

            time.sleep(0.1)

    except KeyboardInterrupt:
        print("\n--- Stopping ---")
        cap.release()