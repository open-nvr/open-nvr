# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: Apache-2.0
"""`InferStream` — many frames down one connection.

Demonstrates: `InferStream`, `.open`, `.infer`, `.close`, context-manager
use, `KaiCError`, `nvr.ai.stream`.

`KaiCClient.infer` is one HTTP round-trip per frame: fine at one frame
every five seconds, wasteful at ten a second. `InferStream` holds a
WebSocket session open, so the model stays warm and every frame in the
session shares one audit correlation id — which is what makes a
sequence of frames traceable as one episode rather than N unrelated
inferences.

Reach for it when you are polling fast (tracking, counting, an
interactive view). Stay with `KaiCClient` when you are not.
"""
from opennvr_app_sdk import InferStream, KaiCError, OpenNVR


def track_for_a_while(kaic_url: str, api_key: str, camera_id: str,
                      frames: list[bytes]) -> list[dict]:
    """One session, N frames. The session closes itself on the way out,
    including on an exception."""
    results = []
    with InferStream(kaic_url, api_key, adapter="yolov8",
                     camera_id=camera_id, client_id="dock-watch") as stream:
        for jpeg in frames:
            try:
                result = stream.infer(jpeg)      # -> §5.1-shaped dict
            except KaiCError:
                # The stream closes itself on failure; the next infer()
                # reopens it. Dropping one frame is the right response.
                continue
            results.append(result["result"])
    return results


def through_the_platform_client(camera_id: str, frames: list[bytes]) -> list[dict]:
    """The same thing without knowing where KAI-C lives or what the key
    is — `nvr.ai.stream` builds the InferStream from the app's own
    credential."""
    with OpenNVR() as nvr, nvr.ai.stream("yolov8", camera_id=camera_id) as stream:
        return [stream.infer(jpeg)["result"] for jpeg in frames]


def one_correlation_id_per_episode(stream: InferStream) -> str | None:
    """Every frame in a session shares KAI-C's correlation id. Thread it
    onto the alert you fire and the whole episode — frames, inferences,
    audit lines, alert — joins up in the timeline."""
    return stream.correlation_id
