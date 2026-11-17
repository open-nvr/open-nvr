// Copyright (c) 2026 OpenNVR
// SPDX-License-Identifier: AGPL-3.0-or-later

/** USB webcams MediaMTX serves on a Linux server (server/services/local_webcams.py). */

/** ``source_device`` of a webcam MediaMTX serves (services/local_webcams.py). */
export const LOCAL_DEVICE_PREFIX = '/dev/v4l/by-id/'

export const isLocalDevice = (device: string | null | undefined): boolean =>
  !!device && device.startsWith(LOCAL_DEVICE_PREFIX)

/** "usb-046d_HD_Pro_Webcam_C920_ABCDEF-video-index0" → "046d HD Pro Webcam C920 ABCDEF",
 *  as the server names one it cannot read a sysfs name for. */
export function localDeviceLabel(device: string): string {
  const entry = device.slice(LOCAL_DEVICE_PREFIX.length)
  return entry.replace(/^usb-/, '').replace(/-video-index\d+$/, '').replace(/_/g, ' ').trim() || entry
}
