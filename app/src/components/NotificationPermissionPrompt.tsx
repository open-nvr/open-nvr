/**
 * Copyright (c) 2026 OpenNVR
 * This file is part of OpenNVR.
 * 
 * OpenNVR is free software: you can redistribute it and/or modify
 * it under the terms of the GNU Affero General Public License as published by
 * the Free Software Foundation, either version 3 of the License, or
 * (at your option) any later version.
 * 
 * OpenNVR is distributed in the hope that it will be useful,
 * but WITHOUT ANY WARRANTY; without even the implied warranty of
 * MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
 * GNU General Public License for more details.
 * 
 * You should have received a copy of the GNU Affero General Public License
 * along with OpenNVR.  If not, see <https://www.gnu.org/licenses/>.
 */

import React, { useEffect, useMemo, useState } from 'react'

type Props = {
  snoozeDays?: number
}

const SNOOZE_KEY = 'opennvr.notificationPrompt.snoozeUntil'
const DISABLE_KEY = 'opennvr.notificationPrompt.disabled'

export function NotificationPermissionPrompt({ snoozeDays = 7 }: Props) {
  const [permission, setPermission] = useState<NotificationPermission | 'unsupported'>(() => {
    if (typeof window === 'undefined' || typeof Notification === 'undefined') return 'unsupported'
    return Notification.permission
  })
  const [now, setNow] = useState<number>(() => Date.now())

  // update time occasionally in case the tab is left open while snoozed
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), 60_000)
    return () => clearInterval(id)
  }, [])

  const disabled = useMemo(() => {
    try {
      return localStorage.getItem(DISABLE_KEY) === '1'
    } catch {
      return false
    }
  }, [])

  const snoozedUntil = useMemo(() => {
    try {
      const v = localStorage.getItem(SNOOZE_KEY)
      return v ? parseInt(v, 10) : 0
    } catch {
      return 0
    }
  }, [])

  const shouldShow = useMemo(() => {
    if (permission === 'unsupported') return false
    if (disabled) return false
    if (permission === 'granted' || permission === 'denied') return false
    if (snoozedUntil && now < snoozedUntil) return false
    return true
  }, [permission, disabled, snoozedUntil, now])

  const handleAllow = async () => {
    if (typeof Notification === 'undefined') return
    try {
      const result = await Notification.requestPermission()
      setPermission(result)
    } catch (e) {
      // ignore
    }
  }

  const handleNotNow = () => {
    try {
      const until = Date.now() + snoozeDays * 24 * 60 * 60 * 1000
      localStorage.setItem(SNOOZE_KEY, String(until))
      setNow(Date.now())
    } catch {
      // ignore
    }
  }

  const handleNever = () => {
    try {
      localStorage.setItem(DISABLE_KEY, '1')
      setNow(Date.now())
    } catch {
      // ignore
    }
  }

  if (!shouldShow) return null

  return (
    <div className="mb-3 border border-[var(--border)] bg-[var(--panel-2)] rounded">
      <div className="p-3 flex items-start gap-3">
        <div className="flex-1 text-sm">
          <div className="font-medium">Permission Request</div>
          <div className="text-[var(--text-dim)]">Enable browser notifications to get alerts about incidents, login events, and system health.</div>
        </div>
        <div className="flex gap-2">
          <button onClick={handleAllow} className="px-3 py-1 rounded bg-[var(--panel)] hover:bg-[var(--bg-2)] text-sm">Allow</button>
          <button onClick={handleNotNow} className="px-3 py-1 rounded bg-transparent hover:bg-[var(--panel)] text-sm">Not Now</button>
          <button onClick={handleNever} className="px-3 py-1 rounded bg-transparent hover:bg-[var(--panel)] text-sm">Don't ask again</button>
        </div>
      </div>
    </div>
  )
}

export default NotificationPermissionPrompt
