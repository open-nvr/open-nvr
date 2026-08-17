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

import { useEffect, useState } from 'react'
import { Activity, Save } from 'lucide-react'
import { apiService } from '../../lib/apiService'
import { useSnackbar } from '../../components/Snackbar'

type MonitoringSettings = {
  enabled: boolean
  cpu_percent_threshold: number | null
  memory_percent_threshold: number | null
  sustained_seconds: number
  disk_min_free_gb: number | null
  disk_used_percent_threshold: number | null
  resolve_hysteresis_percent: number
  notify_integrations: boolean
  renotify_cooldown_minutes: number
}

/**
 * Host monitoring thresholds (CPU / RAM / recordings disk). Alerts raise
 * after a sustained breach and land in Alerts & Incidents (System source),
 * toasts, and — optionally — enabled integrations.
 */
export function SystemHealthSettings() {
  const { showError, showSuccess } = useSnackbar()
  const [loading, setLoading] = useState(false)
  const [settings, setSettings] = useState<MonitoringSettings | null>(null)

  useEffect(() => {
    let cancelled = false
    apiService.getMonitoringSettings()
      .then(({ data }) => { if (!cancelled) setSettings(data as MonitoringSettings) })
      .catch((e: any) => showError(e?.message || 'Failed to load monitoring settings'))
    return () => { cancelled = true }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const set = <K extends keyof MonitoringSettings>(key: K, value: MonitoringSettings[K]) =>
    setSettings(prev => (prev ? { ...prev, [key]: value } : prev))

  const numOrNull = (v: string) => (v === '' ? null : parseInt(v))

  const handleSave = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!settings) return
    try {
      setLoading(true)
      await apiService.updateMonitoringSettings(settings)
      showSuccess('Monitoring settings saved')
    } catch (err: any) {
      showError(err?.response?.data?.detail?.[0]?.msg || err?.message || 'Failed to save monitoring settings')
    } finally {
      setLoading(false)
    }
  }

  if (!settings) return <div className="p-6 text-sm text-[var(--text-dim)]">Loading…</div>

  const inputClass = 'w-full px-3 py-2 bg-[var(--bg-2)] border border-neutral-700 rounded focus:border-[var(--accent)] focus:outline-none'

  return (
    <div className="p-6 max-w-3xl">
      <h2 className="text-xl font-semibold mb-6 flex items-center gap-2">
        <Activity className="text-[var(--accent)]" />
        System Health Monitoring
      </h2>

      <form onSubmit={handleSave} className="space-y-6">
        <div className="bg-[var(--panel)] border border-neutral-800 rounded-lg p-6 space-y-6">
          <div className="flex items-center gap-3">
            <input
              type="checkbox"
              id="mon-enabled"
              checked={settings.enabled}
              onChange={(e) => set('enabled', e.target.checked)}
              className="w-4 h-4 text-[var(--accent)] bg-[var(--bg-2)] border-neutral-700 rounded"
            />
            <label htmlFor="mon-enabled" className="text-sm font-medium">
              Monitor host resources and raise alerts
            </label>
          </div>

          <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
            <div>
              <label className="block text-sm font-medium mb-1">CPU alert threshold (%)</label>
              <input
                type="number" min={1} max={100}
                value={settings.cpu_percent_threshold ?? ''}
                onChange={(e) => set('cpu_percent_threshold', numOrNull(e.target.value))}
                className={inputClass}
                placeholder="90 — empty disables"
              />
            </div>
            <div>
              <label className="block text-sm font-medium mb-1">Memory alert threshold (%)</label>
              <input
                type="number" min={1} max={100}
                value={settings.memory_percent_threshold ?? ''}
                onChange={(e) => set('memory_percent_threshold', numOrNull(e.target.value))}
                className={inputClass}
                placeholder="90 — empty disables"
              />
            </div>
            <div>
              <label className="block text-sm font-medium mb-1">Disk used alert threshold (%)</label>
              <input
                type="number" min={1} max={100}
                value={settings.disk_used_percent_threshold ?? ''}
                onChange={(e) => set('disk_used_percent_threshold', numOrNull(e.target.value))}
                className={inputClass}
                placeholder="90 — empty disables"
              />
              <p className="text-xs text-[var(--text-dim)] mt-1">Recordings volume. On by default so a filling disk is never silent.</p>
            </div>
            <div>
              <label className="block text-sm font-medium mb-1">Warn when free space below (GB)</label>
              <input
                type="number" min={1}
                value={settings.disk_min_free_gb ?? ''}
                onChange={(e) => set('disk_min_free_gb', numOrNull(e.target.value))}
                className={inputClass}
                placeholder="Empty disables"
              />
              <p className="text-xs text-[var(--text-dim)] mt-1">
                Alert only — set it above the retention purge threshold so the warning fires before footage is deleted.
              </p>
            </div>
            <div>
              <label className="block text-sm font-medium mb-1">Sustained breach window (seconds)</label>
              <input
                type="number" min={15} max={3600}
                value={settings.sustained_seconds}
                onChange={(e) => set('sustained_seconds', parseInt(e.target.value) || 120)}
                className={inputClass}
                placeholder="120"
              />
              <p className="text-xs text-[var(--text-dim)] mt-1">CPU/RAM must stay above threshold this long before alerting (ignores spikes).</p>
            </div>
            <div>
              <label className="block text-sm font-medium mb-1">Re-notify cooldown (minutes)</label>
              <input
                type="number" min={0}
                value={settings.renotify_cooldown_minutes}
                onChange={(e) => set('renotify_cooldown_minutes', parseInt(e.target.value) || 0)}
                className={inputClass}
                placeholder="60"
              />
            </div>
          </div>

          <div className="flex items-center gap-3">
            <input
              type="checkbox"
              id="mon-notify"
              checked={settings.notify_integrations}
              onChange={(e) => set('notify_integrations', e.target.checked)}
              className="w-4 h-4 text-[var(--accent)] bg-[var(--bg-2)] border-neutral-700 rounded"
            />
            <label htmlFor="mon-notify" className="text-sm font-medium">
              Send alerts to enabled integrations (email / webhook / Slack / Teams)
            </label>
          </div>
        </div>

        <div className="flex justify-end">
          <button
            type="submit"
            disabled={loading}
            className="px-4 py-2 bg-[var(--accent)] text-white rounded hover:bg-[var(--accent)]/90 disabled:opacity-50 flex items-center gap-2"
          >
            <Save size={16} />
            {loading ? 'Saving...' : 'Save Monitoring Settings'}
          </button>
        </div>
      </form>
    </div>
  )
}
