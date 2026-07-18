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
import { Modal } from '../../components/Modal'
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

const ALL_TABS: { key: string; label: string; area: string }[] = [
  { key: 'info', label: 'Info', area: 'info' },
  { key: 'image', label: 'Image', area: 'imaging' },
  { key: 'video', label: 'Video', area: 'encoder' },
  { key: 'osd', label: 'OSD', area: 'osd' },
  { key: 'ptz', label: 'PTZ', area: 'ptz' },
  { key: 'motion', label: 'Motion', area: 'motion' },
  { key: 'events', label: 'Events', area: 'events' },
  { key: 'ai', label: 'AI', area: 'ai' },
  { key: 'time', label: 'Time', area: 'time' },
  { key: 'network', label: 'Network', area: 'network' },
  { key: 'storage', label: 'Storage', area: 'storage' },
  { key: 'users', label: 'Users', area: 'users' },
]

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

const IMG_NUM: [string, string][] = [
  ['brightness', 'Brightness'],
  ['contrast', 'Contrast'],
  ['saturation', 'Saturation'],
  ['sharpness', 'Sharpness'],
]
const IMG_SEL: [string, string][] = [
  ['ir_cut_filter', 'Day / Night (IR-cut)'],
  ['wdr', 'Wide Dynamic Range'],
  ['backlight', 'Backlight compensation'],
]

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
      <Button variant="primary" onClick={save} disabled={saving}>
        {saving ? 'Applying…' : 'Apply changes'}
      </Button>
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
        {IMG_NUM.filter(([k]) => k in data.settings).map(([k, label]) => {
          const r = data.ranges?.[k] || { min: 0, max: 100 }
          return (
            <div key={k} className="flex items-center gap-3">
              <label className="w-28 text-sm text-[var(--text-dim)]">{label}</label>
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
        {IMG_SEL.filter(([k]) => k in data.settings).map(([k, label]) => (
          <label key={k} className="flex flex-col gap-1">
            <span className="text-xs text-[var(--text-dim)]">{label}</span>
            <select
              value={form[k] ?? ''}
              onChange={(e) => setForm({ ...form, [k]: e.target.value })}
              className="bg-[var(--panel-2)] border border-[var(--border)] rounded px-3 py-2 text-sm"
            >
              {(data.ranges?.[k]?.options || [form[k]]).map((o: string) => (
                <option key={o} value={o}>
                  {o}
                </option>
              ))}
            </select>
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
  if (!data.present)
    return (
      <EmptyState
        title="No SD card / disk detected"
        description="This camera has no local storage inserted."
      />
    )
  return (
    <div className="space-y-3">
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
    </div>
  )
}

export function CameraSettings({
  open,
  camera,
  onClose,
}: {
  open: boolean
  camera: CameraLite | null
  onClose: () => void
}) {
  const { caps, loading, error, reload } = useCameraCapabilities(camera?.id ?? null, open)
  const [active, setActive] = useState('info')

  useEffect(() => {
    if (open) setActive('info')
  }, [open, camera?.id])

  const tabs = ALL_TABS.filter(
    (t) => t.area === 'info' || t.area === 'ai' || caps?.supported_areas?.[t.area]
  )
  const activeTab = tabs.some((t) => t.key === active) ? active : 'info'

  return (
    <Modal
      open={open}
      title={camera ? `Camera settings — ${camera.name}` : 'Camera settings'}
      onClose={onClose}
      widthClassName="w-[820px]"
    >
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
              <Tabs tabs={tabs} active={activeTab} onChange={setActive} />
              <div className="pt-2">
                {activeTab === 'info' && <InfoTab cameraId={camera.id} />}
                {activeTab === 'image' && <ImageTab cameraId={camera.id} />}
                {activeTab === 'video' && <VideoTab cameraId={camera.id} />}
                {activeTab === 'osd' && <OsdTab cameraId={camera.id} />}
                {activeTab === 'ptz' && <PtzTab cameraId={camera.id} />}
                {activeTab === 'motion' && <MotionTab cameraId={camera.id} />}
                {activeTab === 'events' && <EventsTab cameraId={camera.id} />}
                {activeTab === 'ai' && <AiTab cameraId={camera.id} />}
                {activeTab === 'users' && <UsersTab cameraId={camera.id} />}
                {activeTab === 'time' && <TimeTab cameraId={camera.id} />}
                {activeTab === 'network' && <NetworkTab cameraId={camera.id} />}
                {activeTab === 'storage' && <StorageTab cameraId={camera.id} />}
              </div>
              <p className="text-xs text-[var(--text-dim)] border-t border-[var(--border)] pt-3">
                OpenNVR never changes a camera's IP and never factory-resets it —
                those actions stay on the camera's own web page by design.
              </p>
            </>
          )}
        </div>
      )}
    </Modal>
  )
}
