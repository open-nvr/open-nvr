# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: Apache-2.0
"""`EventsClient` — the platform's memory: what was seen, and the proof.

Demonstrates: `EventsClient`, `.search`, `.evidence`,
`.recording_frame`, `StoredEvent`.

Every visit the platform remembers is queryable, and each one can hand
back the evidence photo that proves it. This is what an app uses to
answer a question about the PAST — "when did that van last come?" —
without keeping an index of its own.

Async, because it is I/O against core; use it from any archetype's run
loop or from `AsyncOpenNVR` code.
"""
import asyncio

from opennvr_app_sdk import EventsClient, StoredEvent


async def when_was_this_plate_last_here(plate: str) -> StoredEvent | None:
    client = EventsClient("http://opennvr-core:8000")
    events = await client.search(plate_text=plate, limit=1)
    return events[0] if events else None


async def evidence_for_the_last_person(camera_id: int) -> bytes | None:
    """Find the most recent person on a camera and fetch the frame that
    proves it — the pair of calls behind most 'show me' features."""
    client = EventsClient("http://opennvr-core:8000")
    events = await client.search(camera_id=camera_id, label="person", limit=1)
    if not events:
        return None
    event = events[0]
    if event.has_evidence:
        return await client.evidence(event.id)
    # No stored evidence photo? Pull the frame from the recording at the
    # moment the visit started.
    return await client.recording_frame(event.camera_id, event.started_at)


async def main() -> None:
    hit = await when_was_this_plate_last_here("MH12AB1234")
    if hit:
        print(f"{hit.plate_text} on camera {hit.camera_id} at {hit.started_at}")


if __name__ == "__main__":
    asyncio.run(main())
