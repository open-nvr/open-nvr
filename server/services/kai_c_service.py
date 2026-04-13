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
KAI-C Service - Backend service to communicate with KAI-C connector

This service handles communication between the backend and KAI-C connector,
which then forwards requests to AI Adapter servers.
"""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import httpx

from core.logging_config import main_logger

try:
    import cv2

    CV2_AVAILABLE = True
except ImportError:
    CV2_AVAILABLE = False
    main_logger.warning(
        "OpenCV (cv2) not available. RTSP frame capture will be disabled."
    )


class KaiCService:
    """Service to communicate with KAI-C HTTP service."""

    def __init__(self, kai_c_url: str = "http://localhost:8100"):
        """
        Initialize KAI-C service.

        Args:
            kai_c_url: Base URL of KAI-C HTTP service (default: http://localhost:8100)
        """
        self.kai_c_url = kai_c_url.rstrip("/")
        # Use the same frames directory as AI Adapter expects
        # AI Adapter expects frames at: D:\testing repos\ai-adapter\frames
        workspace_root = Path(__file__).parent.parent.parent.parent
        self.frames_dir = workspace_root / "ai-adapter" / "frames"
        self.frames_dir.mkdir(exist_ok=True, parents=True)

        # Thread pool for blocking operations (RTSP capture)
        self.executor = ThreadPoolExecutor(max_workers=10)

        # Async HTTP client for non-blocking requests
        self.http_client = httpx.AsyncClient(timeout=30.0)

        main_logger.info(f"KaiCService initialized with KAI-C URL: {self.kai_c_url}")
        main_logger.info(f"Frames directory: {self.frames_dir}")

    async def check_kai_c_health(self) -> dict[str, Any]:
        """
        Check if KAI-C and its configured adapters are healthy asynchronously.

        Flow: Backend â†’ KAI-C â†’ (KAI-C checks its adapters)

        Returns:
            Dictionary with KAI-C and adapter health status
        """
        try:
            # Call KAI-C health check
            response = await self.http_client.get(
                f"{self.kai_c_url}/adapters/health",
                timeout=10.0,
                headers={"Accept": "application/json"},
            )
            if response.status_code == 200:
                return response.json()
            return {
                "kai_c_status": "error",
                "message": f"KAI-C returned {response.status_code}",
            }
        except Exception as e:
            main_logger.error(f"KAI-C health check failed: {e}")
            return {"kai_c_status": "error", "message": str(e)}

    async def get_capabilities(self) -> dict[str, Any]:
        """
        Fetch available capabilities from KAI-C asynchronously.

        KAI-C will query all its configured adapters and return combined capabilities.

        Flow: Backend â†’ KAI-C â†’ (KAI-C queries adapters) â†’ KAI-C â†’ Backend

        Returns:
            Dictionary with all available models, tasks, and capabilities
        """
        try:
            # Call KAI-C to get all capabilities
            response = await self.http_client.get(
                f"{self.kai_c_url}/capabilities",
                timeout=15.0,
                headers={"Accept": "application/json"},
            )
            response.raise_for_status()
            return response.json()
        except Exception as e:
            main_logger.error(f"Failed to fetch capabilities from KAI-C: {e}")
            raise

    def _capture_frame_sync(self, rtsp_url: str, camera_id: int) -> str | None:
        """
        Synchronous frame capture (runs in thread pool).

        Args:
            rtsp_url: RTSP stream URL
            camera_id: Camera ID for file naming

        Returns:
            Path to saved frame file, or None if capture failed
        """
        if not CV2_AVAILABLE:
            main_logger.error("OpenCV not available. Cannot capture frames from RTSP.")
            return None

        try:
            # Create camera-specific directory
            camera_dir = self.frames_dir / f"camera_{camera_id}"
            camera_dir.mkdir(exist_ok=True, parents=True)

            frame_path = camera_dir / "latest.jpg"

            # Capture frame from RTSP
            cap = cv2.VideoCapture(rtsp_url)
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)  # Reduce latency

            ret, frame = cap.read()
            cap.release()

            if not ret or frame is None:
                main_logger.warning(f"Failed to capture frame from {rtsp_url}")
                return None

            # Save frame
            cv2.imwrite(str(frame_path), frame)

            # Return opennvr:// URI format expected by AI Adapter
            return f"opennvr://frames/camera_{camera_id}/latest.jpg"

        except Exception as e:
            main_logger.error(f"Error capturing frame from RTSP: {e}", exc_info=True)
            return None

    async def capture_frame_from_rtsp(
        self, rtsp_url: str, camera_id: int
    ) -> str | None:
        """
        Capture a frame from RTSP stream asynchronously.

        Args:
            rtsp_url: RTSP stream URL
            camera_id: Camera ID for file naming

        Returns:
            Path to saved frame file, or None if capture failed
        """
        # Run blocking capture in thread pool
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            self.executor, self._capture_frame_sync, rtsp_url, camera_id
        )

    async def process_inference(
        self,
        camera_id: int,
        rtsp_url: str,
        model_name: str,
        task: str,
        options: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """
        Process inference request asynchronously: capture frame and send to KAI-C.

        KAI-C will route to the correct AI Adapter based on model_name.

        Flow: Server â†’ KAI-C â†’ (KAI-C routes to correct adapter) â†’ KAI-C â†’ Server

        Args:
            camera_id: Camera ID
            rtsp_url: RTSP stream URL
            model_name: Model name (e.g., yolov8, yolov11) - KAI-C routes based on this
            task: Task name (e.g., person_detection, person_counting)
            options: Additional options/parameters

        Returns:
            Inference result via KAI-C
        """
        try:
            # Capture frame from RTSP (async, runs in thread pool)
            frame_uri = await self.capture_frame_from_rtsp(rtsp_url, camera_id)
            if not frame_uri:
                return {
                    "status": "error",
                    "message": "Failed to capture frame from RTSP stream",
                }

            # Prepare payload for AI Adapters (correct format)
            payload = {
                "task": task,
                "input": {
                    "frame": {
                        "uri": frame_uri
                    },
                    "params": options or{}
                }
            }

            main_logger.info(
                f"Sending inference request to AI Adapters: camera={camera_id}, task={task}, frame={frame_uri}"
            )

            # Send async HTTP POST request to KAI-C service
            # KAI-C will forward to AI Adapter
            response = await self.http_client.post(
                f"{self.kai_c_url}/infer/local",
                json=payload,
                headers={
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
            )

            if response.status_code != 200:
                error_text = response.text
                main_logger.error(f"KAI-C request failed: {error_text}")
                return {
                    "status": "error",
                    "message": f"KAI-C service failed: {error_text}",
                }

            result = response.json()

            # Check if KAI-C returned an error
            if result.get("status") == "error":
                return {
                    "status": "error",
                    "message": result.get("message", "Unknown error from KAI-C"),
                }

            # Return standardized response
            return {
                "status": "success",
                "camera_id": camera_id,
                "model_used": result.get("model_used", model_name),
                "task": task,
                "response": result.get("response", result),
            }

        except httpx.RequestError as e:
            main_logger.error(f"Failed to connect to KAI-C service: {e}", exc_info=True)
            return {
                "status": "error",
                "message": f"Cannot connect to KAI-C service at {self.kai_c_url}. Please ensure KAI-C is running.",
            }
        except Exception as e:
            main_logger.error(f"Inference processing failed: {e}", exc_info=True)
            return {"status": "error", "message": str(e)}

    def _extract_frame_from_video_sync(
        self, video_path: str, frame_number: int, camera_id: int
    ) -> str | None:
        """
        Extract a specific frame from video file (synchronous).

        Args:
            video_path: Absolute path to video file
            frame_number: Frame number to extract (0-indexed)
            camera_id: Camera ID for file naming

        Returns:
            Path to saved frame file (opennvr:// URI), or None if extraction failed
        """
        if not CV2_AVAILABLE:
            main_logger.error("OpenCV not available. Cannot extract frames from video.")
            return None

        try:
            # Create camera-specific directory
            camera_dir = self.frames_dir / f"camera_{camera_id}"
            camera_dir.mkdir(exist_ok=True, parents=True)

            frame_path = camera_dir / f"frame_{frame_number}.jpg"

            # Open video file
            cap = cv2.VideoCapture(str(video_path))

            # Set frame position
            cap.set(cv2.CAP_PROP_POS_FRAMES, frame_number)

            ret, frame = cap.read()
            cap.release()

            if not ret or frame is None:
                main_logger.warning(
                    f"Failed to extract frame {frame_number} from {video_path}"
                )
                return None

            # Save frame
            cv2.imwrite(str(frame_path), frame)

            # Return opennvr:// URI format expected by AI Adapter
            return f"opennvr://frames/camera_{camera_id}/frame_{frame_number}.jpg"

        except Exception as e:
            main_logger.error(f"Error extracting frame from video: {e}", exc_info=True)
            return None

    async def extract_frame_from_video(
        self, video_path: str, frame_number: int, camera_id: int
    ) -> str | None:
        """
        Extract a frame from video file asynchronously.

        Args:
            video_path: Absolute path to video file
            frame_number: Frame number to extract (0-indexed)
            camera_id: Camera ID for file naming

        Returns:
            Path to saved frame file, or None if extraction failed
        """
        # Run blocking extraction in thread pool
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            self.executor,
            self._extract_frame_from_video_sync,
            video_path,
            frame_number,
            camera_id,
        )

    async def process_recording_inference(
        self,
        camera_id: int,
        recording_path: str,
        model_name: str,
        task: str,
        frame_interval: int = 30,  # Process every Nth frame (default: 1 fps at 30fps video)
        options: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """
        Process inference on a recorded video file.

        Extracts frames at specified intervals and runs inference on each frame.

        Args:
            camera_id: Camera ID
            recording_path: Relative path to recording file (e.g., "cam-95/2025/12/...")
            model_name: Model name (e.g., yolov8, yolov11)
            task: Task name (e.g., person_detection, person_counting)
            frame_interval: Extract every Nth frame (default: 30 = ~1fps for 30fps video)
            options: Additional options/parameters

        Returns:
            List of inference results for all processed frames
        """
        if not CV2_AVAILABLE:
            return [
                {
                    "status": "error",
                    "message": "OpenCV not available. Cannot process video files.",
                }
            ]

        try:
            # Build absolute path to recording
            from core.database import SessionLocal
            from services.storage_service import get_effective_recordings_base_path

            db = SessionLocal()
            try:
                recordings_base = get_effective_recordings_base_path(db)
            finally:
                db.close()

            video_path = Path(recordings_base) / recording_path

            if not video_path.exists():
                return [
                    {
                        "status": "error",
                        "message": f"Recording not found: {recording_path}",
                    }
                ]

            main_logger.info(f"Processing recording: {video_path}")

            # Get video properties
            cap = cv2.VideoCapture(str(video_path))
            total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
            fps = cap.get(cv2.CAP_PROP_FPS)
            cap.release()

            main_logger.info(f"Video info: {total_frames} frames, {fps:.2f} fps")

            results = []
            frames_to_process = range(0, total_frames, frame_interval)

            main_logger.info(
                f"Processing {len(frames_to_process)} frames (every {frame_interval} frames)"
            )

            # Process each frame
            for frame_num in frames_to_process:
                # Extract frame
                frame_uri = await self.extract_frame_from_video(
                    str(video_path), frame_num, camera_id
                )

                if not frame_uri:
                    main_logger.warning(f"Failed to extract frame {frame_num}")
                    continue

                # Prepare payload for KAI-C (correct format)
                payload = {
                    "task": task,
                    "input": {
                        "frame": {
                            "uri": frame_uri
                        },
                        "params": options or {}
                    }
                }

                # Send inference request
                try:
                    response = await self.http_client.post(
                        f"{self.kai_c_url}/infer",
                        json=payload,
                        headers={
                            "Content-Type": "application/json",
                            "Accept": "application/json",
                        },
                        timeout=30.0,
                    )
                    response.raise_for_status()
                    result = response.json()

                    # Add frame metadata
                    result["frame_number"] = frame_num
                    result["timestamp_seconds"] = frame_num / fps if fps > 0 else 0

                    results.append(result)

                except Exception as e:
                    main_logger.error(f"Inference failed for frame {frame_num}: {e}")
                    results.append(
                        {
                            "status": "error",
                            "frame_number": frame_num,
                            "message": str(e),
                        }
                    )

            main_logger.info(
                f"Completed processing {len(results)} frames from recording"
            )

            return results

        except Exception as e:
            main_logger.error(f"Error processing recording: {e}", exc_info=True)
            return [{"status": "error", "message": str(e)}]

    async def get_task_schema(self, task: str | None = None) -> dict[str, Any]:
        """
        Get schema documentation via KAI-C asynchronously.

        Flow: Backend â†’ KAI-C â†’ (KAI-C queries adapters)

        Args:
            task: Optional task name

        Returns:
            Schema dictionary
        """
        try:
            params = {"task": task} if task else {}
            response = await self.http_client.get(
                f"{self.kai_c_url}/schema",
                params=params,
                headers={"Accept": "application/json"},
            )
            response.raise_for_status()
            return response.json()
        except Exception as e:
            main_logger.error(f"Failed to fetch schema from KAI-C: {e}")
            raise

    async def close(self):
        """Cleanup resources."""
        await self.http_client.aclose()
        self.executor.shutdown(wait=False)


# Singleton instance
_kai_c_service: KaiCService | None = None


def get_kai_c_service() -> KaiCService:
    """Get singleton KAI-C service instance."""
    global _kai_c_service
    if _kai_c_service is None:
        from core.config import settings

        kai_c_url = getattr(settings, "kai_c_url", "http://localhost:8100")
        _kai_c_service = KaiCService(kai_c_url)
    return _kai_c_service
