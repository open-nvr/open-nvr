/**
 * Copyright (c) 2026 OpenNVR
 * Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
 */
import { useEffect, useState } from 'react'

/**
 * Full-screen notice shown when the device firewall refuses this device.
 * Listens for the app-wide 'device-blocked' event dispatched by the API client
 * on a 403 device_not_approved, so a blocked device gets one clear message with
 * its own IP (to give an administrator) instead of a page full of errors.
 */
export function DeviceBlockedOverlay() {
  const [ip, setIp] = useState<string | null>(null)
  useEffect(() => {
    const onBlocked = (e: Event) => {
      const detail = (e as CustomEvent).detail
      setIp(detail?.ip ?? 'unknown')
    }
    window.addEventListener('device-blocked', onBlocked)
    return () => window.removeEventListener('device-blocked', onBlocked)
  }, [])

  if (!ip) return null
  return (
    <div className="fixed inset-0 z-[100] bg-[var(--panel)]/95 backdrop-blur flex items-center justify-center p-4">
      <div className="max-w-md text-center space-y-3">
        <h2 className="text-xl font-semibold">This device is not approved</h2>
        <p className="text-sm text-[var(--text-dim)]">
          An administrator has restricted OpenNVR to approved devices. Ask an
          administrator to approve this device, then reload.
        </p>
        <p className="text-sm">
          Your device address:{' '}
          <span className="font-mono text-[var(--text)]">{ip}</span>
        </p>
        <button
          className="px-4 py-2 bg-[var(--accent)] text-white rounded"
          onClick={() => window.location.reload()}
        >
          Reload
        </button>
      </div>
    </div>
  )
}
