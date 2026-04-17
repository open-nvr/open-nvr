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
Inference Manager - Manages background inference tasks for AI models
"""

import asyncio
from datetime import datetime

from core.logging_config import main_logger

# Check if cv2 is available
try:
    import cv2

    CV2_AVAILABLE = True
except ImportError:
    CV2_AVAILABLE = False
    main_logger.warning(
        "OpenCV (cv2) not available - recording inference will not work"
    )


class InferenceManager:
    """Manages background inference tasks that run continuously."""

    def __init__(self):
        self.running_tasks: dict[int, asyncio.Task] = {}  # model_id -> task
        self._shutdown = False

    def is_running(self, model_id: int) -> bool:
        """Check if inference is running for a model."""
        return (
            model_id in self.running_tasks and not self.running_tasks[model_id].done()
        )

    def get_running_models(self) -> list:
        """Get list of model IDs with running inference."""
        return [
            model_id for model_id, task in self.running_tasks.items() if not task.done()
        ]

    async def start_inference(
        self,
        model_id: int,
        camera_id: int,
        rtsp_url: str,
        model_name: str,
        task: str,
        interval: int,
        config: str | None = None,
    ):
        """Start background inference for a model."""
        if self.is_running(model_id):
            main_logger.warning(f"Inference already running for model {model_id}")
            return

        main_logger.info(
            f"Starting inference for model {model_id} (interval: {interval}s)"
        )

        # Create background task
        task = asyncio.create_task(
            self._inference_loop(
                model_id, camera_id, rtsp_url, model_name, task, interval, config
            )
        )
        self.running_tasks[model_id] = task

    async def stop_inference(self, model_id: int):
        """Stop background inference for a model."""
        if model_id in self.running_tasks:
            main_logger.info(f"Stopping inference for model {model_id}")
            task = self.running_tasks[model_id]
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            # Task may remove itself in finally block, check before deleting
            if model_id in self.running_tasks:
                del self.running_tasks[model_id]

    async def stop_all(self):
        """Stop all running inference tasks."""
        self._shutdown = True
        main_logger.info("Stopping all inference tasks...")

        tasks_to_cancel = list(self.running_tasks.values())
        for task in tasks_to_cancel:
            task.cancel()

        # Wait for all tasks to complete
        if tasks_to_cancel:
            await asyncio.gather(*tasks_to_cancel, return_exceptions=True)

        self.running_tasks.clear()

    async def start_recording_inference(
        self,
        model_id: int,
        camera_id: int,
        recording_paths: list[str],
        model_name: str,
        task: str,
        frame_interval: int,
        config: str | None = None,
    ):
        """Start background inference for recorded video(s)."""
        if self.is_running(model_id):
            main_logger.warning(f"Inference already running for model {model_id}")
            return

        main_logger.info(
            f"Starting recording inference for model {model_id} (frame_interval: {frame_interval}, segments: {len(recording_paths)})"
        )

        # Create background task
        task_obj = asyncio.create_task(
            self._recording_inference_loop(
                model_id,
                camera_id,
                recording_paths,
                model_name,
                task,
                frame_interval,
                config,
            )
        )
        self.running_tasks[model_id] = task_obj

    async def start_cloud_inference(
        self,
        model_id: int,
        camera_id: int,
        rtsp_url: str,
        cloud_model_id: int,
        interval: int,
        user_id: int,
    ):
        """Start background inference for a cloud model."""
        if self.is_running(model_id):
            main_logger.warning(f"Cloud inference already running for model {model_id}")
            return

        main_logger.info(
            f"Starting cloud inference for model {model_id} (interval: {interval}s)"
        )

        # Create background task
        task = asyncio.create_task(
            self._cloud_inference_loop(
                model_id, camera_id, rtsp_url, cloud_model_id, interval, user_id
            )
        )
        self.running_tasks[model_id] = task

    async def _inference_loop(
        self,
        model_id: int,
        camera_id: int,
        rtsp_url: str,
        model_name: str,
        task: str,
        interval: int,
        config: str | None = None,
    ):
        """Background loop that runs inference periodically."""
        import json

        from core.database import SessionLocal
        from models import AIDetectionResult
        from services.kai_c_service import get_kai_c_service

        main_logger.info(f"Inference loop started for model {model_id}")

        # Parse config options
        options = {}
        if config:
            try:
                options = json.loads(config)
            except json.JSONDecodeError:
                main_logger.error(f"Invalid JSON config for model {model_id}")

        kai_c_service = get_kai_c_service()

        try:
            while not self._shutdown:
                try:
                    # Run inference
                    result = await kai_c_service.process_inference(
                        camera_id=camera_id,
                        rtsp_url=rtsp_url,
                        model_name=model_name,
                        task=task,
                        options=options,
                    )

                    # Save result to database
                    if result.get("status") == "success":
                        try:
                            db = SessionLocal()
                            try:
                                response_data = result.get("response", {})

                                # Skip saving detection results when nothing was detected
                                # (adapter returns confidence=0.0 as a placeholder)
                                if (
                                    response_data.get("confidence") is not None
                                    and response_data.get("confidence") == 0.0
                                    and response_data.get("count") is None
                                ):
                                    main_logger.debug(
                                        f"Skipping zero-confidence result for model {model_id} (no detection)"
                                    )
                                else:
                                    bbox = response_data.get("bbox") or []

                                    detection_result = AIDetectionResult(
                                        model_id=model_id,
                                        camera_id=camera_id,
                                        task=task,
                                        label=response_data.get("label"),
                                        confidence=response_data.get("confidence"),
                                        bbox_x=bbox[0] if len(bbox) > 0 else None,
                                        bbox_y=bbox[1] if len(bbox) > 1 else None,
                                        bbox_width=bbox[2] if len(bbox) > 2 else None,
                                        bbox_height=bbox[3] if len(bbox) > 3 else None,
                                        count=response_data.get("count"),
                                        caption=response_data.get("caption")
                                        or response_data.get("description"),
                                        latency_ms=response_data.get("latency_ms"),
                                        annotated_image_uri=response_data.get(
                                            "annotated_image_uri"
                                        ),
                                        executed_at=datetime.fromtimestamp(
                                            response_data.get("executed_at") / 1000.0
                                        )
                                        if response_data.get("executed_at")
                                        else None,
                                    )

                                    db.add(detection_result)
                                    db.commit()
                            finally:
                                db.close()
                        except Exception as e:
                            main_logger.error(
                                f"Failed to save detection result for model {model_id}: {e}"
                            )
                    else:
                        main_logger.error(
                            f"Inference failed for model {model_id}: {result.get('message')}"
                        )

                except Exception as e:
                    main_logger.error(
                        f"Error in inference loop for model {model_id}: {e}",
                        exc_info=True,
                    )

                # Wait for next interval
                await asyncio.sleep(interval)

        except asyncio.CancelledError:
            main_logger.info(f"Inference loop cancelled for model {model_id}")
            raise
        except Exception as e:
            main_logger.error(
                f"Fatal error in inference loop for model {model_id}: {e}",
                exc_info=True,
            )

    async def _recording_inference_loop(
        self,
        model_id: int,
        camera_id: int,
        recording_paths: list[str],  # Changed from single path to list
        model_name: str,
        task: str,
        frame_interval: int,
        config: str | None = None,
    ):
        """Process recording frame by frame across multiple segments, saving results in real-time."""
        import json
        from pathlib import Path

        from core.database import SessionLocal
        from models import AIDetectionResult
        from services.kai_c_service import get_kai_c_service
        from services.storage_service import get_effective_recordings_base_path

        if not CV2_AVAILABLE:
            main_logger.error(
                f"Cannot process recording for model {model_id}: OpenCV not available"
            )
            if model_id in self.running_tasks:
                del self.running_tasks[model_id]
            return

        main_logger.info(
            f"Recording inference started for model {model_id}: {len(recording_paths)} segment(s)"
        )

        # Parse config options
        options = {}
        if config:
            try:
                options = json.loads(config)
            except json.JSONDecodeError:
                main_logger.error(f"Invalid JSON config for model {model_id}")

        kai_c_service = get_kai_c_service()

        try:
            # Build absolute pathsto recordings
            db_temp = SessionLocal()
            try:
                recordings_base = get_effective_recordings_base_path(db_temp)
            finally:
                db_temp.close()

            saved_count = 0
            total_frames_processed = 0

            # Process each segment
            for seg_idx, recording_path in enumerate(recording_paths):
                video_path = Path(recordings_base) / recording_path

                if not video_path.exists():
                    main_logger.error(f"Recording not found: {recording_path}")
                    continue

                main_logger.info(
                    f"Processing segment {seg_idx + 1}/{len(recording_paths)}: {video_path}"
                )

                # Get video properties
                cap = cv2.VideoCapture(str(video_path))
                total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
                fps = cap.get(cv2.CAP_PROP_FPS)
                cap.release()

                main_logger.info(
                    f"Segment {seg_idx + 1} info: {total_frames} frames, {fps:.2f} fps, processing every {frame_interval} frames"
                )

                frames_to_process = list(range(0, total_frames, frame_interval))
                main_logger.info(
                    f"Will process {len(frames_to_process)} frames from this segment"
                )

                # Process each frame and save immediately
                for idx, frame_num in enumerate(frames_to_process):
                    if self._shutdown:
                        main_logger.info(
                            f"Recording inference interrupted for model {model_id}"
                        )
                        break

                    try:
                        total_frames_processed += 1
                        main_logger.info(
                            f"Processing frame {idx + 1}/{len(frames_to_process)} (frame #{frame_num}) from segment {seg_idx + 1}"
                        )

                        # Extract frame
                        frame_uri = await kai_c_service.extract_frame_from_video(
                            str(video_path), frame_num, camera_id
                        )

                        if not frame_uri:
                            main_logger.warning(f"Failed to extract frame {frame_num}")
                            continue

                        main_logger.info(f"Extracted frame to: {frame_uri}")

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
                        main_logger.info(
                            f"Sending inference request to kai-c for frame {frame_num}"
                        )
                        response = await kai_c_service.http_client.post(
                            f"{kai_c_service.kai_c_url}/infer/local",
                            json=payload,
                            headers={
                                "Content-Type": "application/json",
                                "Accept": "application/json",
                            },
                            timeout=30.0,
                        )
                        response.raise_for_status()
                        raw_result = response.json()

                        # Wrap raw adapter response to match expected format
                        # AI adapter returns response directly without status wrapper
                        if raw_result.get("status") == "error":
                            result = raw_result
                        else:
                            result = {
                                "status": "success",
                                "response": raw_result.get("response", raw_result),
                            }

                        main_logger.info(
                            f"Received inference result: {result.get('status')}, raw keys: {list(raw_result.keys())}"
                        )

                        # Save result immediately to database
                        if result.get("status") == "success":
                            db = SessionLocal()
                            try:
                                response_data = result.get("response", {})

                                # Skip saving detection results when nothing was detected
                                # (adapter returns confidence=0.0 as a placeholder)
                                if (
                                    response_data.get("confidence") is not None
                                    and response_data.get("confidence") == 0.0
                                    and response_data.get("count") is None
                                ):
                                    main_logger.debug(
                                        f"Skipping zero-confidence result for frame {frame_num} (no detection)"
                                    )
                                else:
                                    bbox = response_data.get("bbox") or []

                                    detection_result = AIDetectionResult(
                                        model_id=model_id,
                                        camera_id=camera_id,
                                        task=task,
                                        label=response_data.get("label"),
                                        confidence=response_data.get("confidence"),
                                        bbox_x=bbox[0] if len(bbox) > 0 else None,
                                        bbox_y=bbox[1] if len(bbox) > 1 else None,
                                        bbox_width=bbox[2] if len(bbox) > 2 else None,
                                        bbox_height=bbox[3] if len(bbox) > 3 else None,
                                        count=response_data.get("count"),
                                        caption=response_data.get("caption")
                                        or response_data.get("description"),
                                        latency_ms=response_data.get("latency_ms"),
                                        executed_at=datetime.now(),
                                    )

                                    db.add(detection_result)
                                    db.commit()
                                    saved_count += 1

                                    main_logger.info(
                                        f"✓ Saved detection result for frame {frame_num} ({saved_count} total results saved)"
                                    )
                            except Exception as e:
                                main_logger.error(
                                    f"Failed to save detection result for frame {frame_num}: {e}",
                                    exc_info=True,
                                )
                            finally:
                                db.close()
                        else:
                            main_logger.error(
                                f"Inference failed for frame {frame_num}: {result.get('message')}"
                            )

                    except Exception as e:
                        main_logger.error(
                            f"Error processing frame {frame_num}: {e}", exc_info=True
                        )

                if self._shutdown:
                    break

            main_logger.info(
                f"Recording inference completed for model {model_id}: {saved_count} results saved from {total_frames_processed} frames"
            )

        except asyncio.CancelledError:
            main_logger.info(f"Recording inference cancelled for model {model_id}")
            raise
        except Exception as e:
            main_logger.error(
                f"Fatal error in recording inference for model {model_id}: {e}",
                exc_info=True,
            )
        finally:
            # Remove from running tasks when done
            if model_id in self.running_tasks:
                del self.running_tasks[model_id]
                main_logger.info(f"Removed model {model_id} from running tasks")

    async def _cloud_inference_loop(
        self,
        model_id: int,
        camera_id: int,
        rtsp_url: str,
        cloud_model_id: int,
        interval: int,
        user_id: int,
    ):
        """Background loop for cloud model inference."""
        import json
        from datetime import datetime

        import httpx

        from core.config import get_settings
        from core.database import SessionLocal
        from models import AIDetectionResult, CloudProviderModel
        from services.credential_vault_service import CredentialVaultService

        main_logger.info(f"Cloud inference loop started for model {model_id}")
        settings = get_settings()
        credential_service = CredentialVaultService(settings)
        kai_c_url = settings.kai_c_url

        try:
            while not self._shutdown:
                db = None
                try:
                    # Get cloud model config
                    db = SessionLocal()
                    cloud_model = (
                        db.query(CloudProviderModel)
                        .filter(CloudProviderModel.id == cloud_model_id)
                        .first()
                    )

                    if not cloud_model:
                        main_logger.error(f"Cloud model {cloud_model_id} not found")
                        break

                    # Get decrypted credential
                    credential_token = credential_service.get_decrypted_credential(
                        db, cloud_model.credential_id, user_id
                    )

                    if not credential_token:
                        main_logger.error(
                            f"Credential {cloud_model.credential_id} not found"
                        )
                        break

                    # Capture frame from RTSP
                    from services.kai_c_service import get_kai_c_service

                    kai_c_service = get_kai_c_service()
                    frame_uri = await kai_c_service.capture_frame_from_rtsp(
                        rtsp_url, camera_id
                    )

                    if not frame_uri:
                        main_logger.warning(
                            f"Failed to capture frame for cloud model {model_id}"
                        )
                        await asyncio.sleep(interval)
                        continue

                    # Parse config for parameters
                    parameters = {}
                    if cloud_model.config:
                        try:
                            parameters = json.loads(cloud_model.config)
                        except json.JSONDecodeError:
                            pass

                    # Call kai-c cloud inference endpoint
                    async with httpx.AsyncClient(timeout=60.0) as client:
                        payload = {
                            "provider": cloud_model.provider,
                            "model_name": cloud_model.model_id,
                            "task": cloud_model.task,
                            "credential_token": credential_token,
                            "inputs": {"image": frame_uri},
                            "parameters": parameters,
                        }

                        response = await client.post(
                            f"{kai_c_url}/infer/cloud",
                            json=payload,
                            headers={"Content-Type": "application/json"},
                        )

                        if response.status_code == 200:
                            result = response.json()

                            # Parse HF API response based on task
                            hf_result = result.get("result", [])

                            # Store result(s) in database
                            if cloud_model.task == "object-detection":
                                # HF returns list of detections: [{"score": 0.9, "label": "person", "box": {...}}, ...]
                                if isinstance(hf_result, list) and len(hf_result) > 0:
                                    for detection_data in hf_result:
                                        box = detection_data.get("box", {})
                                        detection = AIDetectionResult(
                                            model_id=model_id,
                                            camera_id=camera_id,
                                            task=cloud_model.task,
                                            label=detection_data.get("label"),
                                            confidence=detection_data.get("score"),
                                            bbox_x=box.get("xmin"),
                                            bbox_y=box.get("ymin"),
                                            bbox_width=box.get("xmax", 0)
                                            - box.get("xmin", 0)
                                            if box.get("xmax") and box.get("xmin")
                                            else None,
                                            bbox_height=box.get("ymax", 0)
                                            - box.get("ymin", 0)
                                            if box.get("ymax") and box.get("ymin")
                                            else None,
                                            latency_ms=result.get("latency_ms"),
                                            annotated_image_uri=frame_uri,
                                            executed_at=datetime.fromisoformat(
                                                result.get("executed_at")
                                            )
                                            if result.get("executed_at")
                                            else None,
                                        )
                                        db.add(detection)
                                else:
                                    # No detections - still store attempt
                                    detection = AIDetectionResult(
                                        model_id=model_id,
                                        camera_id=camera_id,
                                        task=cloud_model.task,
                                        label="no_detection",
                                        confidence=0.0,
                                        latency_ms=result.get("latency_ms"),
                                        annotated_image_uri=frame_uri,
                                        executed_at=datetime.fromisoformat(
                                            result.get("executed_at")
                                        )
                                        if result.get("executed_at")
                                        else None,
                                    )
                                    db.add(detection)

                            elif cloud_model.task in [
                                "image-classification",
                                "zero-shot-image-classification",
                            ]:
                                # HF returns list: [{"score": 0.9, "label": "cat"}, ...]
                                if isinstance(hf_result, list) and len(hf_result) > 0:
                                    # Store top result
                                    top_result = hf_result[0]
                                    detection = AIDetectionResult(
                                        model_id=model_id,
                                        camera_id=camera_id,
                                        task=cloud_model.task,
                                        label=top_result.get("label"),
                                        confidence=top_result.get("score"),
                                        latency_ms=result.get("latency_ms"),
                                        annotated_image_uri=frame_uri,
                                        executed_at=datetime.fromisoformat(
                                            result.get("executed_at")
                                        )
                                        if result.get("executed_at")
                                        else None,
                                    )
                                    db.add(detection)

                            elif cloud_model.task in [
                                "image-to-text",
                                "image-captioning",
                            ]:
                                # HF returns: [{"generated_text": "A photo of..."}] or direct text
                                caption_text = None
                                if isinstance(hf_result, list) and len(hf_result) > 0:
                                    caption_text = hf_result[0].get("generated_text")
                                elif isinstance(hf_result, str):
                                    caption_text = hf_result

                                if caption_text:
                                    detection = AIDetectionResult(
                                        model_id=model_id,
                                        camera_id=camera_id,
                                        task=cloud_model.task,
                                        caption=caption_text,
                                        latency_ms=result.get("latency_ms"),
                                        annotated_image_uri=frame_uri,
                                        executed_at=datetime.fromisoformat(
                                            result.get("executed_at")
                                        )
                                        if result.get("executed_at")
                                        else None,
                                    )
                                    db.add(detection)

                            else:
                                # Generic storage for other tasks - store as caption
                                detection = AIDetectionResult(
                                    model_id=model_id,
                                    camera_id=camera_id,
                                    task=cloud_model.task,
                                    caption=json.dumps(hf_result),
                                    latency_ms=result.get("latency_ms"),
                                    annotated_image_uri=frame_uri,
                                    executed_at=datetime.fromisoformat(
                                        result.get("executed_at")
                                    )
                                    if result.get("executed_at")
                                    else None,
                                )
                                db.add(detection)

                            db.commit()

                            main_logger.info(
                                f"Cloud inference completed for model {model_id}, "
                                f"task: {cloud_model.task}, latency: {result.get('latency_ms', 0)}ms"
                            )
                        else:
                            main_logger.error(
                                f"Cloud inference failed: {response.status_code} - {response.text}"
                            )

                except Exception as e:
                    main_logger.error(
                        f"Error in cloud inference loop: {e}", exc_info=True
                    )
                finally:
                    if db:
                        db.close()

                # Wait for next interval
                await asyncio.sleep(interval)

        except asyncio.CancelledError:
            main_logger.info(f"Cloud inference cancelled for model {model_id}")
            raise
        except Exception as e:
            main_logger.error(
                f"Fatal error in cloud inference for model {model_id}: {e}",
                exc_info=True,
            )
        finally:
            # Remove from running tasks when done
            if model_id in self.running_tasks:
                del self.running_tasks[model_id]
                main_logger.info(f"Removed cloud model {model_id} from running tasks")


# Global inference manager instance
_inference_manager: InferenceManager | None = None


def get_inference_manager() -> InferenceManager:
    """Get singleton inference manager instance."""
    global _inference_manager
    if _inference_manager is None:
        _inference_manager = InferenceManager()
    return _inference_manager
