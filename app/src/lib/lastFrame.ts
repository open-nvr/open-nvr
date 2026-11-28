// Copyright (c) 2026 OpenNVR
// SPDX-License-Identifier: AGPL-3.0-or-later

/**
 * Each camera's last frame in this tab: a small JPEG data URL, shown in a
 * live tile until its video plays, so a tile coming back (a page change, a
 * reconnect, a tour page) is never a black box.
 *
 * Module-level, so every surface (Live View, the dashboard wall, tours,
 * peek) shares one picture per camera.
 */

const FRAMES = new Map<number, string>()
/** A 1000-camera tour keeps the most recent few hundred. */
const MAX_FRAMES = 400
const FRAME_WIDTH = 320
/** How often a playing tile refreshes its camera's frame. */
export const LAST_FRAME_EVERY_MS = 10_000

/** Keep `video`'s current picture as `cameraId`'s last frame. Does nothing
 *  for an element with no decoded picture. */
export function keepFrame(cameraId: number, video: HTMLVideoElement | null | undefined) {
  if (!video || !video.videoWidth || !video.videoHeight || video.readyState < 2) return
  try {
    const canvas = document.createElement('canvas')
    canvas.width = FRAME_WIDTH
    canvas.height = Math.round((FRAME_WIDTH * video.videoHeight) / video.videoWidth)
    canvas.getContext('2d')?.drawImage(video, 0, 0, canvas.width, canvas.height)
    FRAMES.delete(cameraId)
    FRAMES.set(cameraId, canvas.toDataURL('image/jpeg', 0.6))
    while (FRAMES.size > MAX_FRAMES) FRAMES.delete(FRAMES.keys().next().value!)
  } catch {
    // A frame the browser will not hand over (a tainted canvas): no still, no harm.
  }
}

/** `cameraId`'s last frame, if this tab has seen one. */
export function lastFrameOf(cameraId: number | null | undefined): string | undefined {
  return cameraId == null ? undefined : FRAMES.get(cameraId)
}

/** Forget every frame (sign-out: the next user may not see these cameras). */
export function clearFrames() {
  FRAMES.clear()
}
