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

import { useState } from 'react'
import { Link } from 'react-router-dom'
import { AlertTriangle, X } from 'lucide-react'
import { useSystemAlerts } from '../hooks/useCameraStatus'
import { useSystemResources } from '../lib/queries'

const DISMISS_KEY = 'system-alert-banner-dismissed'

function formatGb(bytes: number): string {
  return `${(bytes / 1024 ** 3).toFixed(1)} GB`
}

/**
 * Persistent every-page banner for a critically full recordings disk. A 5s
 * toast is not enough for silent footage loss: this stays until the
 * condition clears. Belt-and-braces trigger — a live `disk_low` /
 * `disk_purge_exhausted` alert on the socket OR the polled snapshot already
 * below threshold, so a page load during an ongoing incident shows it
 * without waiting for the next event.
 */
export function SystemAlertBanner() {
  const { diskCritical } = useSystemAlerts()
  const { data } = useSystemResources()
  const [dismissed, setDismissed] = useState(() => sessionStorage.getItem(DISMISS_KEY) === '1')

  const disk = data?.disk
  const thresholds = data?.thresholds
  const snapshotCritical = !!disk && (
    disk.percent >= 98 ||
    disk.free < 2 * 1024 ** 3 ||
    (thresholds?.disk_min_free_gb != null && disk.free / 1024 ** 3 < thresholds.disk_min_free_gb)
  )

  const show = (diskCritical || snapshotCritical) && !dismissed
  if (!show) return null

  const detail = disk
    ? `${formatGb(disk.free)} free of ${formatGb(disk.total)} (${Math.round(disk.percent)}% used).`
    : ''

  return (
    <div className="flex items-center gap-3 mb-4 px-4 py-2.5 rounded border border-red-500/40 bg-red-500/10 text-sm" role="alert">
      <AlertTriangle size={18} className="text-red-400 shrink-0" />
      <div className="flex-1 min-w-0">
        <span className="font-semibold text-red-400">Recordings disk critically low.</span>{' '}
        <span className="text-[var(--text)]">
          {detail} Oldest footage may be purged, and recording stops if the disk fills.
        </span>{' '}
        <Link to="/settings/recording" className="underline text-[var(--accent)]">Review retention settings</Link>
      </div>
      <button
        type="button"
        aria-label="Dismiss for this session"
        className="shrink-0 text-[var(--text-dim)] hover:text-[var(--text)]"
        onClick={() => { sessionStorage.setItem(DISMISS_KEY, '1'); setDismissed(true) }}
      >
        <X size={16} />
      </button>
    </div>
  )
}
