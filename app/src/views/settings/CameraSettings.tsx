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

import { RefreshCw } from 'lucide-react'
import { useCallback, useEffect, useState } from 'react'
import { useSnackbar } from '../../components/Snackbar'
import {
  Badge,
  Button,
  EmptyState,
  ErrorCard,
  Skeleton,
} from '../../components/ui'
import { ReadOnlyField } from '../../components/ui/ReadOnlyField'
import { Tabs } from '../../components/ui/Tabs'
import { useCameraCapabilities } from '../../hooks/useCameraCapabilities'
import { apiService } from '../../lib/apiService'

type CameraLite = { id: number; name: string }

/**
 * Camera settings, grouped. Sixteen flat tabs was too many to scan, so related
 * areas are collected into five groups. Each entry maps to a capability area
 * reported by the camera's driver — a group with no supported members is hidden
 * entirely rather than shown empty, so what you see is what the device can do.
 */
type TabDef = { key: string; label: string; area: string }
type GroupDef = { key: string; label: string; tabs: TabDef[] }

const GROUPS: GroupDef[] = [
  {
    key: 'general',
    label: 'General',
    tabs: [
      { key: 'info', label: 'Info', area: 'info' },
      { key: 'image', label: 'Image', area: 'imaging' },
      { key: 'video', label: 'Video', area: 'encoder' },
      { key: 'osd', label: 'OSD', area: 'osd' },
      { key: 'time', label: 'Time', area: 'time' },
    ],
  },
  {
    key: 'events',
    label: 'Events & Detection',
    tabs: [
      { key: 'motion', label: 'Motion', area: 'motion' },
      { key: 'smart', label: 'Smart', area: 'smart' },
      { key: 'events', label: 'Events', area: 'events' },
      { key: 'ai', label: 'AI', area: 'ai' },
      { key: 'ptz', label: 'PTZ', area: 'ptz' },
    ],
  },
  {
    key: 'network',
    label: 'Network',
    tabs: [
      { key: 'network', label: 'Network', area: 'network' },
      { key: 'services', label: 'Services', area: 'services' },
    ],
  },
  {
    key: 'security',
    label: 'Security',
    tabs: [
      { key: 'security', label: 'Access Control', area: 'security' },
      { key: 'users', label: 'Camera Users', area: 'users' },
    ],
  },
  {
    key: 'maintenance',
    label: 'Storage & Maintenance',
    tabs: [
      { key: 'storage', label: 'Storage', area: 'storage' },
      { key: 'maintenance', label: 'Maintenance', area: 'maintenance' },
    ],
  },
]

/** 'info' and 'ai' are always offered; everything else must be reported. */
const ALWAYS_ON = new Set(['info', 'ai'])

function Toggle({
  checked,
  onChange,
  label,
}: {
  checked: boolean
  onChange: (v: boolean) => void
  label: string
}) {
  return (
    <label className="flex items-center gap-2 text-sm cursor-pointer">
      <input
        type="checkbox"
        className="accent-[var(--accent)] w-4 h-4"
        checked={checked}
        onChange={(e) => onChange(e.target.checked)}
      />
      {label}
    </label>
  )
}

const INPUT_CLS =
  'bg-[var(--panel-2)] border border-[var(--border)] rounded px-2 py-1.5 text-sm'
type EncForm = {
  width?: number
  height?: number
  fps?: number | null
  bitrate?: number | null
  gov_length?: number | null
}
const encFormOf = (c: any): EncForm => ({
  width: c.width,
  height: c.height,
  fps: c.fps,
  bitrate: c.bitrate,
  gov_length: c.gov_length,
})

/**
 * Friendlier names for known image keys. Anything not listed is humanised from
 * its key, so a driver can expose new settings without a frontend change —
 * which vendors do a lot (Secureye alone reports 24).
 */
const IMG_LABELS: Record<string, string> = {
  ir_cut_filter: 'Day / Night (IR-cut)',
  wdr: 'Wide Dynamic Range',
  wdr_level: 'WDR level',
  backlight: 'Backlight compensation',
  noise_reduce_mode: 'Noise reduction mode',
  noise_reduction: 'Noise reduction',
  flip: 'Flip / rotate 180°',
  white_balance: 'White balance',
  wb_red: 'White balance — red',
  wb_blue: 'White balance — blue',
  ir_light_mode: 'IR light mode',
  ir_brightness: 'IR brightness (day)',
  ir_night_brightness: 'IR brightness (night)',
  light_suppression: 'Light suppression',
  light_suppression_strength: 'Light suppression strength',
  defog_strength: 'Defog strength',
  image_style: 'Image style',
  scene_mode: 'Scene mode',
}
function imgLabel(key: string): string {
  return (
    IMG_LABELS[key] ??
    key.replace(/_/g, ' ').replace(/^./, (c) => c.toUpperCase())
  )
}

function probeBadge(result: string | undefined) {
  if (result === 'ok') return <Badge variant="success">Reachable</Badge>
  if (result === 'unreachable') return <Badge variant="warning">Unreachable</Badge>
  if (result === 'not_probed') return <Badge variant="neutral">Not probed</Badge>
  return <Badge variant="destructive">Probe error</Badge>
}

/** Friendly label for the selected vendor driver shown in the header badge. */
const DRIVER_LABELS: Record<string, string> = {
  hikvision: 'Native: Hikvision ISAPI',
  dahua: 'Native: Dahua CGI',
  cpplus: 'Native: CP Plus (Dahua CGI)',
  secureye: 'Native: Secureye CGI',
  onvif: 'ONVIF baseline',
}
function driverLabel(name: string): string {
  return DRIVER_LABELS[name] ?? name
}

/** Small per-tab async loader (each tab reads live from the device). */
function useDeviceRead<T>(fn: () => Promise<{ data: T }>, deps: unknown[]) {
  const [data, setData] = useState<T | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [nonce, setNonce] = useState(0)
  useEffect(() => {
    let alive = true
    setLoading(true)
    setError('')
    fn()
      .then((res) => alive && setData(res.data))
      .catch((e: any) => alive && setError(e?.data?.detail || e?.message || 'Read failed'))
      .finally(() => alive && setLoading(false))
    return () => {
      alive = false
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, nonce])
  return { data, loading, error, reload: () => setNonce((n) => n + 1) }
}

function InfoTab({ cameraId }: { cameraId: number }) {
  const { data, loading, error, reload } = useDeviceRead<any>(
    () => apiService.getCameraDeviceInfo(cameraId),
    [cameraId]
  )
  if (loading) return <Skeleton className="h-24 w-full" />
  if (error) return <ErrorCard message={error} onRetry={reload} />
  return (
    <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
      <ReadOnlyField label="Manufacturer" value={data?.manufacturer} />
      <ReadOnlyField label="Model" value={data?.model} />
      <ReadOnlyField label="Firmware" value={data?.firmware_version} />
      <ReadOnlyField label="Serial number" value={data?.serial_number} />
      <ReadOnlyField label="Hardware ID" value={data?.hardware_id} />
      <ReadOnlyField label="Driver" value={data?.driver_name} />
    </div>
  )
}

function NetworkTab({ cameraId }: { cameraId: number }) {
  const { data, loading, error, reload } = useDeviceRead<any>(
    () => apiService.getCameraNetwork(cameraId),
    [cameraId]
  )
  if (loading) return <Skeleton className="h-24 w-full" />
  if (error) return <ErrorCard message={error} onRetry={reload} />
  if (!data?.supported)
    return <EmptyState title="Network info not available for this device" />
  return (
    <div className="space-y-4">
      <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
        <ReadOnlyField label="IP address" value={data.ip_address} />
        <ReadOnlyField label="Subnet mask" value={data.subnet_mask} />
        <ReadOnlyField label="Gateway" value={data.gateway} />
        <ReadOnlyField label="Primary DNS" value={data.dns_primary} />
        <ReadOnlyField label="Secondary DNS" value={data.dns_secondary} />
        <ReadOnlyField label="MAC address" value={data.mac_address} />
        <ReadOnlyField
          label="Addressing"
          value={data.dhcp === null ? null : data.dhcp ? 'DHCP' : 'Static'}
        />
        <ReadOnlyField label="MTU" value={data.mtu} />
      </div>
      <p className="text-xs text-[var(--text-dim)]">
        Network settings are read-only. OpenNVR never changes a camera's IP — a
        wrong change could put the camera out of reach.
      </p>
    </div>
  )
}

function UsersTab({ cameraId }: { cameraId: number }) {
  const { showSuccess, showError } = useSnackbar()
  const [data, setData] = useState<any>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState('')
  const [form, setForm] = useState({ username: '', password: '', level: 'Operator' })

  const load = useCallback(async () => {
    setLoading(true)
    setError('')
    try {
      const { data } = await apiService.getCameraUsers(cameraId)
      setData(data)
    } catch (e: any) {
      setError(e?.data?.detail || e?.message || 'Failed to read users')
    } finally {
      setLoading(false)
    }
  }, [cameraId])
  useEffect(() => {
    load()
  }, [load])

  const add = async () => {
    if (!form.username || !form.password) return
    setBusy('add')
    try {
      await apiService.createCameraUser(cameraId, form)
      setForm({ username: '', password: '', level: 'Operator' })
      showSuccess(`User "${form.username}" created`)
      await load()
    } catch (e: any) {
      showError(e?.data?.detail || e?.message || 'Create failed')
    } finally {
      setBusy('')
    }
  }
  const remove = async (name: string) => {
    if (!window.confirm(`Delete camera user "${name}"? This cannot be undone.`)) return
    setBusy(name)
    try {
      await apiService.deleteCameraUser(cameraId, name)
      showSuccess(`User "${name}" deleted`)
      await load()
    } catch (e: any) {
      showError(e?.data?.detail || e?.message || 'Delete failed')
    } finally {
      setBusy('')
    }
  }
  const reboot = async () => {
    if (
      !window.confirm(
        'Reboot this camera? Live view and recording will drop for ~30–60 seconds. ' +
          'No settings are changed.'
      )
    )
      return
    setBusy('reboot')
    try {
      await apiService.rebootCamera(cameraId)
      showSuccess('Reboot command sent')
    } catch (e: any) {
      showError(e?.data?.detail || e?.message || 'Reboot failed')
    } finally {
      setBusy('')
    }
  }

  if (loading) return <Skeleton className="h-40 w-full" />
  if (error) return <ErrorCard message={error} onRetry={load} />
  if (!data?.supported)
    return <EmptyState title="User management not available for this device" />

  return (
    <div className="space-y-5">
      <div>
        {(data.users || []).map((u: any) => (
          <div
            key={u.id}
            className="flex items-center gap-3 border-b border-[var(--border)] py-2 text-sm"
          >
            <span className="font-medium flex-1">{u.name}</span>
            <Badge variant="neutral">{u.level}</Badge>
            {u.is_current ? (
              <Badge variant="info">OpenNVR account</Badge>
            ) : (
              <Button
                variant="danger"
                onClick={() => remove(u.name)}
                disabled={busy === u.name}
              >
                {busy === u.name ? '…' : 'Delete'}
              </Button>
            )}
          </div>
        ))}
      </div>

      <div className="border-t border-[var(--border)] pt-4 space-y-2">
        <div className="text-xs text-[var(--text-dim)]">Add a new account</div>
        <div className="grid grid-cols-1 sm:grid-cols-4 gap-2">
          <input
            className={INPUT_CLS}
            placeholder="Username"
            value={form.username}
            onChange={(e) => setForm({ ...form, username: e.target.value })}
          />
          <input
            className={INPUT_CLS}
            type="password"
            placeholder="Password"
            value={form.password}
            onChange={(e) => setForm({ ...form, password: e.target.value })}
          />
          <select
            className={INPUT_CLS}
            value={form.level}
            onChange={(e) => setForm({ ...form, level: e.target.value })}
          >
            <option value="Administrator">Administrator</option>
            <option value="Operator">Operator</option>
            <option value="Viewer">Viewer</option>
          </select>
          <Button
            variant="primary"
            onClick={add}
            disabled={busy === 'add' || !form.username || !form.password}
          >
            {busy === 'add' ? 'Adding…' : 'Add user'}
          </Button>
        </div>
      </div>

      <div className="border-t border-[var(--border)] pt-4 flex items-center gap-3">
        <Button variant="danger" onClick={reboot} disabled={busy === 'reboot'}>
          {busy === 'reboot' ? 'Sending…' : 'Reboot camera'}
        </Button>
        <span className="text-xs text-[var(--text-dim)]">
          OpenNVR never changes the camera's IP or factory-resets it.
        </span>
      </div>
    </div>
  )
}

function PtzTab({ cameraId }: { cameraId: number }) {
  const { showError } = useSnackbar()
  const move = async (x: number, y: number, z = 0) => {
    try {
      await apiService.ptzMove(cameraId, x, y, z)
    } catch (e: any) {
      showError(e?.data?.detail || e?.message || 'PTZ move failed')
    }
  }
  const stop = async () => {
    try {
      await apiService.ptzStop(cameraId)
    } catch {
      /* stop is best-effort */
    }
  }
  const Pad = ({
    x,
    y,
    z,
    label,
    className = '',
  }: {
    x: number
    y: number
    z?: number
    label: string
    className?: string
  }) => (
    <button
      className={`px-3 py-3 bg-[var(--panel-2)] border border-[var(--border)] rounded hover:bg-[var(--panel)] ${className}`}
      onMouseDown={() => move(x, y, z || 0)}
      onMouseUp={stop}
      onMouseLeave={stop}
    >
      {label}
    </button>
  )
  return (
    <div className="space-y-4">
      <p className="text-xs text-[var(--text-dim)]">
        Hold a control to move the camera; release to stop.
      </p>
      <div className="grid grid-cols-3 gap-2 max-w-[220px]">
        <div />
        <Pad x={0} y={0.5} label="▲" />
        <div />
        <Pad x={-0.5} y={0} label="◀" />
        <button
          className="px-3 py-3 bg-[var(--panel-2)] border border-[var(--border)] rounded hover:bg-[var(--panel)]"
          onClick={stop}
        >
          ■
        </button>
        <Pad x={0.5} y={0} label="▶" />
        <div />
        <Pad x={0} y={-0.5} label="▼" />
        <div />
      </div>
      <div className="flex gap-2">
        <Pad x={0} y={0} z={0.5} label="Zoom +" className="!py-2" />
        <Pad x={0} y={0} z={-0.5} label="Zoom −" className="!py-2" />
      </div>
    </div>
  )
}

function EventsTab({ cameraId }: { cameraId: number }) {
  const { showError, showSuccess } = useSnackbar()
  const [subscribed, setSubscribed] = useState(false)
  const [events, setEvents] = useState<any[]>([])
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState(false)

  const refresh = useCallback(async () => {
    try {
      const [st, ev] = await Promise.all([
        apiService.getCameraEventsStatus(cameraId),
        apiService.getCameraEvents(cameraId, 50),
      ])
      setSubscribed(!!st.data.subscribed)
      setEvents(ev.data || [])
    } catch {
      /* transient — keep last state */
    } finally {
      setLoading(false)
    }
  }, [cameraId])
  useEffect(() => {
    refresh()
    const t = setInterval(refresh, 3000)
    return () => clearInterval(t)
  }, [refresh])

  const toggle = async () => {
    setBusy(true)
    try {
      if (subscribed) {
        await apiService.unsubscribeCameraEvents(cameraId)
        showSuccess('Stopped listening for camera alarms')
      } else {
        await apiService.subscribeCameraEvents(cameraId)
        showSuccess('Now listening for camera alarms')
      }
      await refresh()
    } catch (e: any) {
      showError(e?.data?.detail || e?.message || 'Failed to change subscription')
    } finally {
      setBusy(false)
    }
  }

  if (loading) return <Skeleton className="h-32 w-full" />
  return (
    <div className="space-y-4">
      <div className="flex items-center gap-3">
        <Button
          variant={subscribed ? 'danger' : 'primary'}
          onClick={toggle}
          disabled={busy}
        >
          {busy ? '…' : subscribed ? 'Stop listening' : 'Start listening for alarms'}
        </Button>
        <Badge variant={subscribed ? 'success' : 'neutral'}>
          {subscribed ? 'Live' : 'Off'}
        </Badge>
      </div>
      <p className="text-xs text-[var(--text-dim)]">
        Streams motion / tamper alarms from the camera into OpenNVR. Enable
        motion detection (Motion tab) for the camera to report them.
        Subscriptions reset when the server restarts.
      </p>
      {events.length === 0 ? (
        <EmptyState
          title="No alarms yet"
          description={
            subscribed
              ? 'Waiting for the camera to report an alarm…'
              : 'Start listening to capture alarms.'
          }
        />
      ) : (
        <div className="max-h-72 overflow-auto">
          {events.map((e) => (
            <div
              key={e.id}
              className="flex items-center gap-3 text-sm border-b border-[var(--border)] py-1.5"
            >
              <Badge variant={e.event_state === 'active' ? 'warning' : 'neutral'}>
                {e.event_type}
              </Badge>
              <span className="text-[var(--text-dim)] flex-1">{e.description}</span>
              <span className="text-xs font-mono text-[var(--text-dim)]">
                {e.occurred_at ? new Date(e.occurred_at).toLocaleTimeString() : ''}
              </span>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

function AiTab({ cameraId }: { cameraId: number }) {
  const { showError, showSuccess } = useSnackbar()
  const [models, setModels] = useState<any[]>([])
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState(0)

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const { data } = await apiService.getAiModels()
      setModels((data || []).filter((m: any) => m.assigned_camera_id === cameraId))
    } catch {
      setModels([])
    } finally {
      setLoading(false)
    }
  }, [cameraId])
  useEffect(() => {
    load()
  }, [load])

  const running = (m: any) => m.is_running ?? m.enabled ?? false
  const toggle = async (m: any) => {
    setBusy(m.id)
    try {
      if (running(m)) {
        await apiService.stopInference(m.id)
        showSuccess('Detector stopped')
      } else {
        await apiService.startInference(m.id)
        showSuccess('Detector started')
      }
      await load()
    } catch (e: any) {
      showError(e?.data?.detail || e?.message || 'Failed to change detector')
    } finally {
      setBusy(0)
    }
  }

  if (loading) return <Skeleton className="h-32 w-full" />
  if (!models.length)
    return (
      <EmptyState
        title="No detectors assigned to this camera"
        description="Assign an AI model to this camera in the AI section to run detection on its live stream."
      />
    )
  return (
    <div className="space-y-3">
      {models.map((m) => (
        <div
          key={m.id}
          className="flex items-center gap-3 rounded border border-[var(--border)] p-3"
        >
          <div className="flex-1">
            <div className="font-medium text-sm">{m.name}</div>
            <div className="text-xs text-[var(--text-dim)]">
              {m.model_name} · {m.task}
            </div>
          </div>
          <Badge variant={running(m) ? 'success' : 'neutral'}>
            {running(m) ? 'Running' : 'Stopped'}
          </Badge>
          <Button
            variant={running(m) ? 'danger' : 'primary'}
            onClick={() => toggle(m)}
            disabled={busy === m.id}
          >
            {busy === m.id ? '…' : running(m) ? 'Stop' : 'Start'}
          </Button>
        </div>
      ))}
    </div>
  )
}

function OsdTab({ cameraId }: { cameraId: number }) {
  const { showSuccess, showError } = useSnackbar()
  const [data, setData] = useState<any>(null)
  const [form, setForm] = useState<any>({})
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [saving, setSaving] = useState(false)

  const load = useCallback(async () => {
    setLoading(true)
    setError('')
    try {
      const { data } = await apiService.getCameraOsd(cameraId)
      setData(data)
      setForm({
        datetime_enabled: !!data.datetime_enabled,
        channel_name_enabled: !!data.channel_name_enabled,
        text_enabled: !!data.text_enabled,
        text: data.text || '',
      })
    } catch (e: any) {
      setError(e?.data?.detail || e?.message || 'Failed to read OSD settings')
    } finally {
      setLoading(false)
    }
  }, [cameraId])
  useEffect(() => {
    load()
  }, [load])

  const save = async () => {
    setSaving(true)
    try {
      const { data } = await apiService.setCameraOsd(cameraId, form)
      setData(data)
      showSuccess('Overlay settings applied')
    } catch (e: any) {
      showError(e?.data?.detail || e?.message || 'Failed to apply OSD settings')
    } finally {
      setSaving(false)
    }
  }

  if (loading) return <Skeleton className="h-32 w-full" />
  if (error) return <ErrorCard message={error} onRetry={load} />
  if (!data?.supported)
    return <EmptyState title="On-screen overlays not available for this device" />

  return (
    <div className="space-y-4">
      <Toggle
        label="Show date &amp; time overlay"
        checked={form.datetime_enabled}
        onChange={(v) => setForm({ ...form, datetime_enabled: v })}
      />
      <Toggle
        label="Show camera name overlay"
        checked={form.channel_name_enabled}
        onChange={(v) => setForm({ ...form, channel_name_enabled: v })}
      />
      <div className="border-t border-[var(--border)] pt-4 space-y-2">
        <Toggle
          label="Show custom text overlay"
          checked={form.text_enabled}
          onChange={(v) => setForm({ ...form, text_enabled: v })}
        />
        <input
          className={`w-full ${INPUT_CLS}`}
          placeholder="Custom text"
          value={form.text}
          disabled={!form.text_enabled}
          onChange={(e) => setForm({ ...form, text: e.target.value })}
        />
      </div>
      {(data.slots || []).filter((sl: any) => sl.id !== 1).length > 0 && (
        <div className="border-t border-[var(--border)] pt-4 space-y-2">
          <p className="text-xs text-[var(--text-dim)]">
            Additional text overlays
          </p>
          {(data.slots || [])
            .filter((sl: any) => sl.id !== 1)
            .map((sl: any) => {
              const patched = (form.slots || []).find((x: any) => x.id === sl.id)
              const enabled = patched?.enabled ?? sl.enabled ?? false
              const text = patched?.text ?? sl.text ?? ''
              const upd = (next: any) => {
                const rest = (form.slots || []).filter((x: any) => x.id !== sl.id)
                setForm({
                  ...form,
                  slots: [...rest, { id: sl.id, enabled, text, ...next }],
                })
              }
              return (
                <div key={sl.id} className="flex items-center gap-2">
                  <Toggle
                    label={`#${sl.id}`}
                    checked={!!enabled}
                    onChange={(v) => upd({ enabled: v })}
                  />
                  <input
                    className={`flex-1 ${INPUT_CLS}`}
                    placeholder={`Overlay ${sl.id} text`}
                    value={text}
                    disabled={!enabled}
                    onChange={(e) => upd({ text: e.target.value })}
                  />
                </div>
              )
            })}
        </div>
      )}
      <Button variant="primary" onClick={save} disabled={saving}>
        {saving ? 'Applying…' : 'Apply changes'}
      </Button>
    </div>
  )
}

function ServicesTab({ cameraId }: { cameraId: number }) {
  const { showSuccess, showError } = useSnackbar()
  const [data, setData] = useState<any>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState('')

  const load = useCallback(async () => {
    setLoading(true)
    setError('')
    try {
      const { data } = await apiService.getCameraServices(cameraId)
      setData(data)
    } catch (e: any) {
      setError(e?.data?.detail || e?.message || 'Failed to read services')
    } finally {
      setLoading(false)
    }
  }, [cameraId])
  useEffect(() => {
    load()
  }, [load])

  const toggle = async (key: string, enabled: boolean) => {
    setBusy(key)
    try {
      const { data: res } = await apiService.setCameraService(cameraId, key, enabled)
      setData(res)
      showSuccess('Service updated')
    } catch (e: any) {
      showError(e?.data?.detail || e?.message || 'Failed to update service')
    } finally {
      setBusy('')
    }
  }

  if (loading) return <Skeleton className="h-40 w-full" />
  if (error) return <ErrorCard message={error} onRetry={load} />
  if (!data?.supported)
    return <EmptyState title="Service settings not available for this device" />

  return (
    <div className="space-y-3">
      <p className="text-xs text-[var(--text-dim)]">
        Network services running on the camera. Turning off the ones you don't
        use reduces the camera's attack surface.
      </p>
      {(data.services || []).map((s: any) => (
        <div
          key={s.key}
          className="flex items-center justify-between gap-3 border border-[var(--border)] rounded px-3 py-2"
        >
          <div className="min-w-0">
            <div className="text-sm">{s.label}</div>
            {s.detail && (
              <div className="text-xs text-[var(--text-dim)] truncate font-mono">
                {s.detail}
              </div>
            )}
          </div>
          {!s.supported ? (
            <span className="text-xs text-[var(--text-dim)] shrink-0">
              not supported
            </span>
          ) : s.writable ? (
            <Toggle
              label=""
              checked={!!s.enabled}
              onChange={(v) => !busy && toggle(s.key, v)}
            />
          ) : (
            <Badge variant={s.enabled ? 'info' : 'neutral'}>
              {s.enabled == null ? 'unknown' : s.enabled ? 'on' : 'off'}
            </Badge>
          )}
        </div>
      ))}
    </div>
  )
}

function MaintenanceTab({ cameraId }: { cameraId: number }) {
  const { showSuccess, showError } = useSnackbar()
  const [busy, setBusy] = useState('')
  const [confirmReboot, setConfirmReboot] = useState(false)
  const [secretKey, setSecretKey] = useState('')

  const reboot = async () => {
    setBusy('reboot')
    try {
      await apiService.rebootCamera(cameraId)
      setConfirmReboot(false)
      showSuccess('Reboot command sent — the camera will be offline briefly')
    } catch (e: any) {
      showError(e?.data?.detail || e?.message || 'Reboot failed')
    } finally {
      setBusy('')
    }
  }

  const backup = async () => {
    setBusy('backup')
    try {
      const res = await apiService.exportCameraConfig(cameraId, secretKey)
      const url = URL.createObjectURL(new Blob([res.data]))
      const a = document.createElement('a')
      a.href = url
      a.download = `camera-${cameraId}-config.bin`
      a.click()
      URL.revokeObjectURL(url)
      showSuccess('Configuration downloaded')
    } catch (e: any) {
      showError(e?.data?.detail || e?.message || 'Config export failed')
    } finally {
      setBusy('')
    }
  }

  return (
    <div className="space-y-4">
      <div className="border border-[var(--border)] rounded p-3 space-y-2">
        <div className="text-sm">Configuration backup</div>
        <p className="text-xs text-[var(--text-dim)]">
          Download the camera's full settings as a vendor backup file. Useful
          before making changes, and for cloning setup to an identical camera.
        </p>
        <input
          className={`w-full ${INPUT_CLS}`}
          placeholder="Encryption key for the backup"
          value={secretKey}
          onChange={(e) => setSecretKey(e.target.value)}
        />
        <p className="text-xs text-amber-500">
          The camera encrypts the backup with this key and the same key is
          required to restore it. Store it somewhere safe — without it the
          backup is useless.
        </p>
        <Button onClick={backup} disabled={busy === 'backup' || !secretKey.trim()}>
          {busy === 'backup' ? 'Exporting…' : 'Download backup'}
        </Button>
      </div>

      <div className="border border-[var(--border)] rounded p-3 space-y-2">
        <div className="text-sm">Reboot camera</div>
        <p className="text-xs text-[var(--text-dim)]">
          The camera will drop its stream and recording for roughly a minute.
        </p>
        {!confirmReboot ? (
          <Button onClick={() => setConfirmReboot(true)}>Reboot…</Button>
        ) : (
          <div className="flex gap-2">
            <Button variant="primary" onClick={reboot} disabled={busy === 'reboot'}>
              {busy === 'reboot' ? 'Rebooting…' : 'Confirm reboot'}
            </Button>
            <Button onClick={() => setConfirmReboot(false)}>Cancel</Button>
          </div>
        )}
      </div>

      <p className="text-xs text-[var(--text-dim)]">
        OpenNVR never factory-resets a camera and never changes its IP — those
        stay on the camera's own web page by design.
      </p>
    </div>
  )
}

function SecurityTab({ cameraId }: { cameraId: number }) {
  const { showSuccess, showError } = useSnackbar()
  const [data, setData] = useState<any>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState('')
  const [extra, setExtra] = useState('')
  const [confirmLock, setConfirmLock] = useState(false)

  const load = useCallback(async () => {
    setLoading(true)
    setError('')
    try {
      const { data } = await apiService.getCameraSecurity(cameraId)
      setData(data)
    } catch (e: any) {
      setError(e?.data?.detail || e?.message || 'Failed to read security settings')
    } finally {
      setLoading(false)
    }
  }, [cameraId])
  useEffect(() => {
    load()
  }, [load])

  const applyFilter = async (enabled: boolean) => {
    setBusy('filter')
    try {
      const entries = enabled
        ? [data.server_ip, ...extra.split(',').map((s: string) => s.trim())].filter(Boolean)
        : []
      const { data: res } = await apiService.setCameraIpFilter(cameraId, {
        enabled,
        mode: 'allow',
        entries,
      })
      setData(res)
      setConfirmLock(false)
      showSuccess(enabled ? 'Camera locked to OpenNVR' : 'IP filter disabled')
    } catch (e: any) {
      showError(e?.data?.detail || e?.message || 'Failed to update IP filter')
    } finally {
      setBusy('')
    }
  }

  const applyCloud = async (enabled: boolean) => {
    setBusy('cloud')
    try {
      const { data: res } = await apiService.setCameraCloud(cameraId, enabled)
      setData(res)
      showSuccess(enabled ? 'Cloud enabled' : 'Vendor cloud disabled')
    } catch (e: any) {
      showError(e?.data?.detail || e?.message || 'Failed to update cloud setting')
    } finally {
      setBusy('')
    }
  }

  if (loading) return <Skeleton className="h-40 w-full" />
  if (error) return <ErrorCard message={error} onRetry={load} />
  if (!data?.supported)
    return <EmptyState title="Security settings not available for this device" />

  const locked = data.ip_filter_enabled && data.ip_filter_mode === 'allow'

  return (
    <div className="space-y-5">
      {/* --- IP allowlist (lockout-capable) --- */}
      <div className="border border-[var(--border)] rounded p-3 space-y-3">
        <div className="flex items-center justify-between">
          <span className="text-sm">Restrict access to OpenNVR only</span>
          {locked ? (
            <Badge variant="success">Locked</Badge>
          ) : (
            <Badge variant="neutral">Open</Badge>
          )}
        </div>

        {!data.ip_filter_supported ? (
          <p className="text-xs text-[var(--text-dim)]">
            This camera does not support IP filtering.
          </p>
        ) : locked ? (
          <>
            <p className="text-xs text-[var(--text-dim)]">
              Only these addresses can reach this camera:{' '}
              <span className="font-mono">
                {(data.ip_filter_entries || []).join(', ')}
              </span>
            </p>
            <Button onClick={() => applyFilter(false)} disabled={busy === 'filter'}>
              {busy === 'filter' ? 'Working…' : 'Remove restriction'}
            </Button>
          </>
        ) : (
          <>
            <p className="text-xs text-[var(--text-dim)]">
              The camera will accept connections only from OpenNVR (
              <span className="font-mono">{data.server_ip || 'unknown'}</span>).
              Everyone else is refused — even with the correct password.
            </p>
            <label className="flex flex-col gap-1">
              <span className="text-xs text-[var(--text-dim)]">
                Additional allowed addresses (optional, comma-separated)
              </span>
              <input
                value={extra}
                onChange={(e) => setExtra(e.target.value)}
                placeholder="e.g. 192.168.1.20"
                className="bg-[var(--panel-2)] border border-[var(--border)] rounded px-3 py-2 text-sm font-mono"
              />
            </label>

            {!confirmLock ? (
              <Button
                variant="primary"
                onClick={() => setConfirmLock(true)}
                disabled={!data.server_ip}
              >
                Lock camera to OpenNVR
              </Button>
            ) : (
              <div className="space-y-2 border border-amber-600/50 bg-amber-600/10 rounded p-3">
                <p className="text-xs">
                  <strong>Read this first.</strong> If OpenNVR's address changes
                  later (for example on DHCP), it will no longer be able to reach
                  this camera and the only way back is the camera's physical reset
                  button. Give this server a static IP or DHCP reservation before
                  continuing.
                </p>
                <div className="flex gap-2">
                  <Button
                    variant="primary"
                    onClick={() => applyFilter(true)}
                    disabled={busy === 'filter'}
                  >
                    {busy === 'filter' ? 'Applying…' : 'I understand — lock it'}
                  </Button>
                  <Button onClick={() => setConfirmLock(false)}>Cancel</Button>
                </div>
              </div>
            )}
          </>
        )}
      </div>

      {/* --- vendor cloud --- */}
      {data.cloud_supported && (
        <div className="border border-[var(--border)] rounded p-3 space-y-2">
          <Toggle
            label="Vendor cloud (P2P)"
            checked={!!data.cloud_enabled}
            onChange={(v) => applyCloud(v)}
          />
          <p className="text-xs text-[var(--text-dim)]">
            {data.cloud_enabled
              ? `This camera can reach the vendor cloud${
                  data.cloud_host ? ` (${data.cloud_host})` : ''
                }. Disable it to keep the camera fully offline.`
              : 'Disabled — the camera is not phoning home to the vendor cloud.'}
          </p>
        </div>
      )}

      {/* --- read-only posture --- */}
      <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
        <ReadOnlyField
          label="Brute-force login lockout"
          value={
            data.illegal_login_lock == null
              ? undefined
              : data.illegal_login_lock
                ? 'Enabled'
                : 'Disabled'
          }
        />
        <ReadOnlyField label="OpenNVR is seen as" value={data.server_ip} />
      </div>

      {!!(data.protocols || []).length && (
        <div className="space-y-1">
          <p className="text-xs text-[var(--text-dim)]">
            Open services (read-only — disabling the HTTP port here would cut
            OpenNVR off from the camera)
          </p>
          <div className="flex flex-wrap gap-2">
            {data.protocols.map((p: any) => (
              <Badge
                key={p.protocol}
                variant={p.enabled === false ? 'neutral' : 'info'}
              >
                {p.protocol}:{p.port}{' '}
                {/* Some services (e.g. the SDK port) expose no enable flag at
                    all — reporting that as "off" would understate what is
                    actually listening, so say the state is unknown. */}
                {p.enabled == null ? '(no toggle)' : p.enabled ? 'on' : 'off'}
              </Badge>
            ))}
          </div>
        </div>
      )}
    </div>
  )
}

function SmartTab({ cameraId }: { cameraId: number }) {
  const { showSuccess, showError } = useSnackbar()
  const [data, setData] = useState<any>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState('')

  const load = useCallback(async () => {
    setLoading(true)
    setError('')
    try {
      const { data } = await apiService.getCameraSmart(cameraId)
      setData(data)
    } catch (e: any) {
      setError(e?.data?.detail || e?.message || 'Failed to read smart detection')
    } finally {
      setLoading(false)
    }
  }, [cameraId])
  useEffect(() => {
    load()
  }, [load])

  const apply = async (key: string, patch: Record<string, any>) => {
    setBusy(key)
    try {
      const { data } = await apiService.setCameraSmart(cameraId, key, patch)
      setData(data)
      showSuccess('Detector updated')
    } catch (e: any) {
      showError(e?.data?.detail || e?.message || 'Failed to update detector')
    } finally {
      setBusy('')
    }
  }

  if (loading) return <Skeleton className="h-40 w-full" />
  if (error) return <ErrorCard message={error} onRetry={load} />
  if (!data?.supported)
    return <EmptyState title="Smart detection not available for this device" />

  return (
    <div className="space-y-3">
      {(data.detectors || []).map((d: any) => (
        <div
          key={d.key}
          className="border border-[var(--border)] rounded p-3 space-y-3"
        >
          <div className="flex items-center justify-between gap-3">
            <div className="flex items-center gap-2">
              <span className="text-sm">{d.label}</span>
              {d.configured === false && d.enabled && (
                <Badge variant="warning">no region defined</Badge>
              )}
            </div>
            {d.supported ? (
              <Toggle
                label=""
                checked={!!d.enabled}
                onChange={(v) => apply(d.key, { enabled: v })}
              />
            ) : (
              <span className="text-xs text-[var(--text-dim)]">
                not supported by this model
              </span>
            )}
          </div>

          {d.supported && d.sensitivity != null && (
            <div className="flex items-center gap-3">
              <label className="w-24 text-xs text-[var(--text-dim)]">
                Sensitivity
              </label>
              <input
                type="range"
                min={1}
                max={d.sensitivity_max ?? 100}
                defaultValue={d.sensitivity}
                disabled={busy === d.key}
                onMouseUp={(e) =>
                  apply(d.key, {
                    sensitivity: parseInt((e.target as HTMLInputElement).value),
                  })
                }
                className="flex-1 accent-[var(--accent)]"
              />
              <span className="w-14 text-right text-xs font-mono">
                {d.sensitivity} / {d.sensitivity_max}
              </span>
            </div>
          )}

          {d.supported && d.configured === false && (
            <p className="text-xs text-[var(--text-dim)]">
              This detector has no line or region drawn, so it will not trigger
              even when enabled. Draw one in the camera's own web interface.
            </p>
          )}
        </div>
      ))}
    </div>
  )
}

function MotionTab({ cameraId }: { cameraId: number }) {
  const { showSuccess, showError } = useSnackbar()
  const [data, setData] = useState<any>(null)
  const [form, setForm] = useState<any>({})
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [saving, setSaving] = useState(false)

  const load = useCallback(async () => {
    setLoading(true)
    setError('')
    try {
      const { data } = await apiService.getCameraMotion(cameraId)
      setData(data)
      setForm({ enabled: !!data.enabled, sensitivity: data.sensitivity ?? 50 })
    } catch (e: any) {
      setError(e?.data?.detail || e?.message || 'Failed to read motion settings')
    } finally {
      setLoading(false)
    }
  }, [cameraId])
  useEffect(() => {
    load()
  }, [load])

  const save = async () => {
    setSaving(true)
    try {
      const { data } = await apiService.setCameraMotion(cameraId, form)
      setData(data)
      setForm({ enabled: !!data.enabled, sensitivity: data.sensitivity ?? 50 })
      showSuccess('Motion detection updated')
    } catch (e: any) {
      showError(e?.data?.detail || e?.message || 'Failed to apply motion settings')
    } finally {
      setSaving(false)
    }
  }

  if (loading) return <Skeleton className="h-32 w-full" />
  if (error) return <ErrorCard message={error} onRetry={load} />
  if (!data?.supported)
    return <EmptyState title="Motion detection not available for this device" />

  return (
    <div className="space-y-5">
      <Toggle
        label="Enable motion detection"
        checked={form.enabled}
        onChange={(v) => setForm({ ...form, enabled: v })}
      />
      <div className="flex items-center gap-3">
        <label className="w-28 text-sm text-[var(--text-dim)]">Sensitivity</label>
        <input
          type="range"
          min={0}
          max={data.sensitivity_max ?? 100}
          value={form.sensitivity}
          disabled={!form.enabled}
          onChange={(e) => setForm({ ...form, sensitivity: parseInt(e.target.value) })}
          className="flex-1 accent-[var(--accent)]"
        />
        <span className="w-10 text-right text-sm font-mono">{form.sensitivity}</span>
      </div>
      <Button variant="primary" onClick={save} disabled={saving}>
        {saving ? 'Applying…' : 'Apply changes'}
      </Button>
    </div>
  )
}

function VideoTab({ cameraId }: { cameraId: number }) {
  const { showSuccess, showError } = useSnackbar()
  const [data, setData] = useState<any>(null)
  const [forms, setForms] = useState<Record<string, EncForm>>({})
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [saving, setSaving] = useState('')

  const hydrate = (d: any) => {
    setData(d)
    const f: Record<string, EncForm> = {}
    for (const c of d.configs || []) f[c.token] = encFormOf(c)
    setForms(f)
  }
  const load = useCallback(async () => {
    setLoading(true)
    setError('')
    try {
      const { data } = await apiService.getCameraEncoder(cameraId)
      hydrate(data)
    } catch (e: any) {
      setError(e?.data?.detail || e?.message || 'Failed to read encoder settings')
    } finally {
      setLoading(false)
    }
  }, [cameraId])
  useEffect(() => {
    load()
  }, [load])

  const apply = async (token: string) => {
    setSaving(token)
    try {
      const { data } = await apiService.setCameraEncoder(cameraId, token, forms[token])
      hydrate(data)
      showSuccess(
        data.reconcile?.reconciled
          ? 'Encoder updated — live stream reconnected'
          : 'Encoder updated'
      )
    } catch (e: any) {
      showError(e?.data?.detail || e?.message || 'Failed to apply encoder settings')
    } finally {
      setSaving('')
    }
  }

  if (loading) return <Skeleton className="h-40 w-full" />
  if (error) return <ErrorCard message={error} onRetry={load} />
  if (!data?.supported)
    return <EmptyState title="Video encoder settings not available for this device" />

  return (
    <div className="space-y-4">
      <p className="text-xs text-[var(--text-dim)]">
        Applying encoder changes briefly reconnects the live stream and recording.
      </p>
      {(data.configs || []).map((c: any) => {
        const f = forms[c.token] || {}
        const opt = c.options || {}
        const resVal = `${f.width}x${f.height}`
        const dirty = JSON.stringify(f) !== JSON.stringify(encFormOf(c))
        return (
          <div key={c.token} className="rounded border border-[var(--border)] p-3 space-y-3">
            <div className="flex items-center gap-2">
              <span className="font-medium text-sm">{c.name || c.token}</span>
              <Badge variant="info">{c.encoding}</Badge>
            </div>
            <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
              <label className="flex flex-col gap-1 text-xs text-[var(--text-dim)]">
                Resolution
                {opt.resolutions?.length ? (
                  <select
                    className={INPUT_CLS}
                    value={resVal}
                    onChange={(e) => {
                      const [w, h] = e.target.value.split('x').map(Number)
                      setForms({ ...forms, [c.token]: { ...f, width: w, height: h } })
                    }}
                  >
                    {opt.resolutions.map((r: any) => (
                      <option key={`${r.width}x${r.height}`} value={`${r.width}x${r.height}`}>
                        {r.width}×{r.height}
                      </option>
                    ))}
                  </select>
                ) : (
                  <span className="text-sm text-[var(--text)] font-mono">
                    {c.width}×{c.height}
                  </span>
                )}
              </label>
              <label className="flex flex-col gap-1 text-xs text-[var(--text-dim)]">
                Frame rate (fps)
                <input
                  type="number"
                  className={INPUT_CLS}
                  min={opt.fps_range?.min}
                  max={opt.fps_range?.max}
                  value={f.fps ?? ''}
                  onChange={(e) =>
                    setForms({
                      ...forms,
                      [c.token]: { ...f, fps: parseInt(e.target.value) || null },
                    })
                  }
                />
              </label>
              <label className="flex flex-col gap-1 text-xs text-[var(--text-dim)]">
                Bitrate (kbps)
                <input
                  type="number"
                  className={INPUT_CLS}
                  value={f.bitrate ?? ''}
                  onChange={(e) =>
                    setForms({
                      ...forms,
                      [c.token]: { ...f, bitrate: parseInt(e.target.value) || null },
                    })
                  }
                />
              </label>
              {c.gov_length != null && (
                <label className="flex flex-col gap-1 text-xs text-[var(--text-dim)]">
                  GOP length
                  <input
                    type="number"
                    className={INPUT_CLS}
                    min={opt.gov_range?.min}
                    max={opt.gov_range?.max}
                    value={f.gov_length ?? ''}
                    onChange={(e) =>
                      setForms({
                        ...forms,
                        [c.token]: { ...f, gov_length: parseInt(e.target.value) || null },
                      })
                    }
                  />
                </label>
              )}
            </div>
            <Button
              variant="primary"
              onClick={() => apply(c.token)}
              disabled={saving === c.token || !dirty}
            >
              {saving === c.token ? 'Applying…' : 'Apply'}
            </Button>
          </div>
        )
      })}
    </div>
  )
}

function ImageTab({ cameraId }: { cameraId: number }) {
  const { showSuccess, showError } = useSnackbar()
  const [data, setData] = useState<any>(null)
  const [form, setForm] = useState<Record<string, any>>({})
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [saving, setSaving] = useState(false)

  const load = useCallback(async () => {
    setLoading(true)
    setError('')
    try {
      const { data } = await apiService.getCameraImaging(cameraId)
      setData(data)
      setForm({ ...data.settings })
    } catch (e: any) {
      setError(e?.data?.detail || e?.message || 'Failed to read image settings')
    } finally {
      setLoading(false)
    }
  }, [cameraId])
  useEffect(() => {
    load()
  }, [load])

  const save = async () => {
    setSaving(true)
    try {
      const { data } = await apiService.setCameraImaging(cameraId, form)
      setData(data)
      setForm({ ...data.settings })
      showSuccess('Image settings applied')
    } catch (e: any) {
      showError(e?.data?.detail || e?.message || 'Failed to apply image settings')
    } finally {
      setSaving(false)
    }
  }

  if (loading) return <Skeleton className="h-40 w-full" />
  if (error) return <ErrorCard message={error} onRetry={load} />
  if (!data?.supported)
    return <EmptyState title="Image settings not available for this device" />

  const dirty = JSON.stringify(form) !== JSON.stringify(data.settings)
  return (
    <div className="space-y-5">
      <div className="space-y-3">
        {Object.keys(data.settings)
          .filter((k) => data.ranges?.[k]?.min !== undefined)
          .sort()
          .map((k) => {
          const r = data.ranges?.[k] || { min: 0, max: 100 }
          const label = imgLabel(k)
          return (
            <div key={k} className="flex items-center gap-3">
              <label className="w-44 text-sm text-[var(--text-dim)]">{label}</label>
              <input
                type="range"
                min={r.min}
                max={r.max}
                value={form[k] ?? r.min}
                onChange={(e) => setForm({ ...form, [k]: parseInt(e.target.value) })}
                className="flex-1 accent-[var(--accent)]"
              />
              <span className="w-10 text-right text-sm font-mono">{form[k]}</span>
            </div>
          )
        })}
      </div>

      <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
        {Object.keys(data.settings)
          .filter((k) => data.ranges?.[k]?.min === undefined)
          .sort()
          .map((k) => (
          <label key={k} className="flex flex-col gap-1">
            <span className="text-xs text-[var(--text-dim)]">{imgLabel(k)}</span>
            {data.ranges?.[k]?.options ? (
              <select
                value={form[k] ?? ''}
                onChange={(e) => setForm({ ...form, [k]: e.target.value })}
                className="bg-[var(--panel-2)] border border-[var(--border)] rounded px-3 py-2 text-sm"
              >
                {data.ranges[k].options.map((o: string) => (
                  <option key={o} value={o}>
                    {o}
                  </option>
                ))}
              </select>
            ) : (
              /* Device reported a value but no option list (e.g. shutter
                 "1/12") — free text rather than a select with one entry. */
              <input
                value={form[k] ?? ''}
                onChange={(e) => setForm({ ...form, [k]: e.target.value })}
                className="bg-[var(--panel-2)] border border-[var(--border)] rounded px-3 py-2 text-sm font-mono"
              />
            )}
          </label>
        ))}
      </div>

      <div className="flex items-center gap-3 border-t border-[var(--border)] pt-4">
        <Button variant="primary" onClick={save} disabled={saving || !dirty}>
          {saving ? 'Applying…' : 'Apply changes'}
        </Button>
        <Button onClick={() => setForm({ ...data.settings })} disabled={saving || !dirty}>
          Reset
        </Button>
      </div>
    </div>
  )
}

function TimeTab({ cameraId }: { cameraId: number }) {
  const { showSuccess, showError } = useSnackbar()
  const [data, setData] = useState<any>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [tz, setTz] = useState('')
  const [ntp, setNtp] = useState('')
  const [busy, setBusy] = useState('')

  const load = useCallback(async () => {
    setLoading(true)
    setError('')
    try {
      const { data } = await apiService.getCameraTime(cameraId)
      setData(data)
      setTz(data.timezone || '')
      setNtp(data.ntp_server || 'pool.ntp.org')
    } catch (e: any) {
      setError(e?.data?.detail || e?.message || 'Failed to read camera time')
    } finally {
      setLoading(false)
    }
  }, [cameraId])
  useEffect(() => {
    load()
  }, [load])

  const syncNow = async () => {
    setBusy('sync')
    try {
      const { data } = await apiService.syncCameraTimeManual(cameraId, {
        timezone: tz.trim() || null,
      })
      setData(data)
      showSuccess('Camera clock synced to server time')
    } catch (e: any) {
      showError(e?.data?.detail || e?.message || 'Clock sync failed')
    } finally {
      setBusy('')
    }
  }
  const useNtp = async () => {
    setBusy('ntp')
    try {
      const { data } = await apiService.setCameraNtp(cameraId, {
        server: ntp.trim(),
        timezone: tz.trim() || null,
      })
      setData(data)
      showSuccess(`Camera now syncing from ${ntp.trim()}`)
    } catch (e: any) {
      showError(e?.data?.detail || e?.message || 'NTP config failed')
    } finally {
      setBusy('')
    }
  }

  if (loading) return <Skeleton className="h-24 w-full" />
  if (error) return <ErrorCard message={error} onRetry={load} />

  const inputCls =
    'bg-[var(--panel-2)] border border-[var(--border)] rounded px-3 py-2 text-sm font-mono'
  return (
    <div className="space-y-5">
      <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
        <ReadOnlyField label="Camera UTC time" value={data?.utc_datetime} />
        <ReadOnlyField label="Camera local time" value={data?.local_datetime} />
        <ReadOnlyField
          label="Mode"
          value={data?.mode === 'ntp' ? 'NTP (automatic)' : 'Manual'}
          mono={false}
        />
        <ReadOnlyField label="Timezone" value={data?.timezone} />
      </div>

      <div className="flex flex-col gap-1 border-t border-[var(--border)] pt-4">
        <label className="text-xs text-[var(--text-dim)]">
          Timezone (POSIX format, e.g. IST-5:30:00 for India, CST-8:00:00 for China)
        </label>
        <input
          className={`w-full ${inputCls}`}
          value={tz}
          onChange={(e) => setTz(e.target.value)}
          placeholder="e.g. IST-5:30:00"
        />
      </div>

      <div className="flex flex-wrap items-center gap-3">
        <Button variant="primary" onClick={syncNow} disabled={!!busy}>
          {busy === 'sync' ? 'Syncing…' : 'Sync clock to this server'}
        </Button>
        <span className="text-xs text-[var(--text-dim)]">
          Pushes this server's current UTC to the camera right now.
        </span>
      </div>

      <div className="flex flex-col gap-2 border-t border-[var(--border)] pt-4">
        <label className="text-xs text-[var(--text-dim)]">
          NTP server — keep the camera in sync automatically
        </label>
        <div className="flex flex-wrap gap-2">
          <input
            className={`flex-1 min-w-[200px] ${inputCls}`}
            value={ntp}
            onChange={(e) => setNtp(e.target.value)}
            placeholder="pool.ntp.org"
          />
          <Button onClick={useNtp} disabled={!!busy || !ntp.trim()}>
            {busy === 'ntp' ? 'Applying…' : 'Use NTP'}
          </Button>
        </div>
        <p className="text-xs text-[var(--text-dim)]">
          The camera must be able to reach this server over the network.
        </p>
      </div>
    </div>
  )
}

function StorageTab({ cameraId }: { cameraId: number }) {
  const { data, loading, error, reload } = useDeviceRead<any>(
    () => apiService.getCameraStorage(cameraId),
    [cameraId]
  )
  if (loading) return <Skeleton className="h-24 w-full" />
  if (error) return <ErrorCard message={error} onRetry={reload} />
  if (!data?.supported)
    return <EmptyState title="Storage info not available for this device" />
  return (
    <div className="space-y-3">
      {!data.present && (
        <EmptyState
          title="No SD card / disk detected"
          description="This camera has no local storage inserted."
        />
      )}
      {(data.slots || []).map((s: any, i: number) => (
        <div
          key={i}
          className="grid grid-cols-2 sm:grid-cols-4 gap-4 rounded border border-[var(--border)] p-3"
        >
          <ReadOnlyField label="Name" value={s.name} />
          <ReadOnlyField label="Status" value={s.status} />
          <ReadOnlyField
            label="Capacity"
            value={s.capacity_mb ? `${(s.capacity_mb / 1024).toFixed(1)} GB` : null}
          />
          <ReadOnlyField
            label="Free"
            value={s.free_mb ? `${(s.free_mb / 1024).toFixed(1)} GB` : null}
          />
        </div>
      ))}

      {data.nas_supported && (
        <div className="border-t border-[var(--border)] pt-3 space-y-2">
          <p className="text-xs text-[var(--text-dim)]">
            Network storage — supported mount types:{' '}
            <span className="font-mono">
              {(data.nas_mount_types || []).join(', ') || 'unknown'}
            </span>
          </p>
          {(data.nas_mounts || []).length === 0 ? (
            <p className="text-xs text-[var(--text-dim)]">
              No network share is mounted on this camera.
            </p>
          ) : (
            data.nas_mounts.map((n: any, i: number) => (
              <div
                key={i}
                className="grid grid-cols-2 sm:grid-cols-4 gap-4 rounded border border-[var(--border)] p-3"
              >
                <ReadOnlyField label="Address" value={n.address} />
                <ReadOnlyField label="Path" value={n.path} />
                <ReadOnlyField label="Type" value={n.type} />
                <ReadOnlyField label="Status" value={n.status} />
              </div>
            ))
          )}
        </div>
      )}
    </div>
  )
}

/**
 * The tabbed device-settings panel for one camera. Shared by the modal (opened
 * from a camera row) and the Settings > Camera Configuration page, so both
 * surfaces render exactly the same capability-gated tabs.
 */
export function CameraSettingsPanel({
  camera,
  active: activeProp,
}: {
  camera: CameraLite | null
  active?: boolean
}) {
  const enabled = activeProp ?? true
  const { caps, loading, error, reload } = useCameraCapabilities(
    camera?.id ?? null,
    enabled
  )
  const [active, setActive] = useState('info')
  const [group, setGroup] = useState('general')

  useEffect(() => {
    if (enabled) {
      setActive('info')
      setGroup('general')
    }
  }, [enabled, camera?.id])

  // Keep only what this camera actually supports, then drop groups left empty.
  const groups = GROUPS.map((g) => ({
    ...g,
    tabs: g.tabs.filter(
      (t) => ALWAYS_ON.has(t.area) || caps?.supported_areas?.[t.area]
    ),
  })).filter((g) => g.tabs.length > 0)

  const activeGroup =
    groups.find((g) => g.key === group) ?? groups[0] ?? { key: '', label: '', tabs: [] }
  const tabs = activeGroup.tabs
  const activeTab = tabs.some((t) => t.key === active) ? active : (tabs[0]?.key ?? 'info')

  // Areas the driver reports it cannot drive — surfaced once, honestly, rather
  // than as tabs that open onto nothing.
  const unavailable = GROUPS.flatMap((g) => g.tabs)
    .filter((t) => !ALWAYS_ON.has(t.area) && !caps?.supported_areas?.[t.area])
    .map((t) => t.label)

  return (
    <>
      {!camera ? null : (
        <div className="space-y-4">
          <div className="flex flex-wrap items-center gap-2">
            {probeBadge(caps?.probe_result)}
            {caps?.driver_name && (
              <Badge variant="info">{driverLabel(caps.driver_name)}</Badge>
            )}
            {caps?.capabilities?.hardware_verified === false && (
              <span
                className="text-xs text-amber-500"
                title="This vendor driver's write paths have not yet been verified against real hardware."
              >
                not hardware-verified
              </span>
            )}
            {caps?.probed_at && (
              <span className="text-xs text-[var(--text-dim)]">
                Probed {new Date(caps.probed_at).toLocaleString()}
              </span>
            )}
            <div className="ml-auto">
              <Button onClick={() => reload(true)} disabled={loading}>
                <RefreshCw size={14} className={loading ? 'animate-spin' : ''} /> Refresh
              </Button>
            </div>
          </div>

          {error ? (
            <ErrorCard message={error} onRetry={() => reload(true)} />
          ) : caps?.probe_result === 'unreachable' ? (
            <ErrorCard
              title="Camera unreachable"
              message={caps.probe_error || 'Could not reach the camera over ONVIF.'}
              onRetry={() => reload(true)}
            />
          ) : (
            <>
              <div className="flex gap-4">
                <nav className="w-48 shrink-0 space-y-1">
                  {groups.map((g) => (
                    <button
                      key={g.key}
                      onClick={() => {
                        setGroup(g.key)
                        setActive(g.tabs[0]?.key ?? 'info')
                      }}
                      className={`w-full text-left px-3 py-2 rounded text-sm transition-colors ${
                        g.key === activeGroup.key
                          ? 'bg-[var(--panel-2)] text-[var(--text)]'
                          : 'text-[var(--text-dim)] hover:bg-[var(--panel-2)]/60'
                      }`}
                    >
                      {g.label}
                      <span className="ml-1 text-xs opacity-60">
                        {g.tabs.length}
                      </span>
                    </button>
                  ))}
                  {unavailable.length > 0 && (
                    <p className="pt-3 text-xs text-[var(--text-dim)] leading-relaxed">
                      Not supported by this camera:{' '}
                      {unavailable.join(', ')}. Configure these on the camera's
                      own web page (link above).
                    </p>
                  )}
                </nav>

                <div className="flex-1 min-w-0">
                  {tabs.length > 1 && (
                    <Tabs tabs={tabs} active={activeTab} onChange={setActive} />
                  )}
                  <div className="pt-2">
                {activeTab === 'info' && <InfoTab cameraId={camera.id} />}
                {activeTab === 'image' && <ImageTab cameraId={camera.id} />}
                {activeTab === 'video' && <VideoTab cameraId={camera.id} />}
                {activeTab === 'osd' && <OsdTab cameraId={camera.id} />}
                {activeTab === 'ptz' && <PtzTab cameraId={camera.id} />}
                {activeTab === 'motion' && <MotionTab cameraId={camera.id} />}
                {activeTab === 'smart' && <SmartTab cameraId={camera.id} />}
                {activeTab === 'security' && <SecurityTab cameraId={camera.id} />}
                {activeTab === 'services' && <ServicesTab cameraId={camera.id} />}
                {activeTab === 'maintenance' && (
                  <MaintenanceTab cameraId={camera.id} />
                )}
                {activeTab === 'events' && <EventsTab cameraId={camera.id} />}
                {activeTab === 'ai' && <AiTab cameraId={camera.id} />}
                {activeTab === 'users' && <UsersTab cameraId={camera.id} />}
                {activeTab === 'time' && <TimeTab cameraId={camera.id} />}
                {activeTab === 'network' && <NetworkTab cameraId={camera.id} />}
                {activeTab === 'storage' && <StorageTab cameraId={camera.id} />}
                  </div>
                </div>
              </div>
              <p className="text-xs text-[var(--text-dim)] border-t border-[var(--border)] pt-3">
                OpenNVR never changes a camera's IP and never factory-resets it —
                those actions stay on the camera's own web page by design.
              </p>
            </>
          )}
        </div>
      )}
    </>
  )
}
