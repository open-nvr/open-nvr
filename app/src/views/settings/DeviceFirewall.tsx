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

import { useCallback, useEffect, useState } from 'react'
import { useSnackbar } from '../../components/Snackbar'
import { Badge, Button, ErrorCard, Skeleton } from '../../components/ui'
import { apiService } from '../../lib/apiService'

type Device = {
  ip_address: string
  label: string | null
  status: 'approved' | 'pending' | 'blocked' | null
  user_agent: string | null
  attempt_count: number
  auto_enrolled: boolean
  last_seen: string | null
}

const STATUS_BADGE: Record<string, 'success' | 'warning' | 'destructive'> = {
  approved: 'success',
  pending: 'warning',
  blocked: 'destructive',
}

/**
 * OpenNVR device firewall — controls which devices (by IP) may use OpenNVR.
 *
 * The first device to log in on a fresh install is trusted automatically; any
 * new device is held pending until approved here. Enforcement is off until you
 * turn it on. Streams are covered too: MediaMTX only serves an OpenNVR-signed
 * token, which a blocked device cannot obtain.
 */
export function DeviceFirewall() {
  const { showSuccess, showError } = useSnackbar()
  const [devices, setDevices] = useState<Device[]>([])
  const [active, setActive] = useState(false)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState('')

  const load = useCallback(async () => {
    setLoading(true)
    setError('')
    try {
      const { data } = await apiService.listTrustedDevices()
      setDevices(data.devices || [])
      setActive(!!data.enforcement_active)
    } catch (e: any) {
      setError(e?.data?.detail || e?.message || 'Failed to load devices')
    } finally {
      setLoading(false)
    }
  }, [])
  useEffect(() => {
    load()
  }, [load])

  const toggle = async (v: boolean) => {
    setBusy('toggle')
    try {
      const { data } = await apiService.setFirewallEnforcement(v)
      setActive(!!data.enforcement_active)
      if (v && !data.enforcement_active) {
        showError('Enforcement is forced off by the DEVICE_FIREWALL_KILL override')
      } else {
        showSuccess(data.enforcement_active ? 'Firewall enabled' : 'Firewall disabled')
      }
    } catch (e: any) {
      showError(e?.data?.detail || e?.message || 'Failed to change enforcement')
    } finally {
      setBusy('')
    }
  }

  const act = async (fn: () => Promise<any>, ip: string) => {
    setBusy(ip)
    try {
      await fn()
      await load()
    } catch (e: any) {
      showError(e?.data?.detail || e?.message || 'Action failed')
    } finally {
      setBusy('')
    }
  }

  if (loading) return <Skeleton className="h-40 w-full" />
  if (error) return <ErrorCard message={error} onRetry={load} />

  const approvedCount = devices.filter((d) => d.status === 'approved').length
  const pending = devices.filter((d) => d.status === 'pending')

  return (
    <div className="space-y-4">
      {/* Enforcement toggle */}
      <div className="flex items-start justify-between gap-4 border border-[var(--border)] rounded p-4">
        <div>
          <div className="flex items-center gap-2">
            <span className="text-sm font-medium">Restrict access to approved devices</span>
            {active ? (
              <Badge variant="success">On</Badge>
            ) : (
              <Badge variant="neutral">Off</Badge>
            )}
          </div>
          <p className="text-xs text-[var(--text-dim)] mt-1 max-w-xl">
            When on, only approved devices can reach OpenNVR or its streams. The
            first device to log in was trusted automatically. Turning this on
            with unapproved devices present will lock them out immediately.
          </p>
        </div>
        <Button
          variant={active ? 'secondary' : 'primary'}
          disabled={busy === 'toggle'}
          onClick={() => toggle(!active)}
        >
          {busy === 'toggle' ? 'Working…' : active ? 'Disable' : 'Enable'}
        </Button>
      </div>

      {active && approvedCount === 0 && (
        <div className="rounded border border-amber-600/40 bg-amber-600/10 px-3 py-2 text-xs text-amber-500">
          Enforcement is on but no device is approved — you may lock yourself
          out. Approve at least this device before relying on it.
        </div>
      )}

      {pending.length > 0 && (
        <div className="rounded border border-amber-600/40 bg-amber-600/10 px-3 py-2 text-xs text-amber-500">
          {pending.length} device{pending.length > 1 ? 's are' : ' is'} waiting
          for approval.
        </div>
      )}

      {/* Device table */}
      <div className="overflow-auto border border-[var(--border)] rounded">
        <table className="w-full text-sm">
          <thead>
            <tr className="text-left text-[var(--text-dim)] border-b border-[var(--border)]">
              <th className="px-3 py-2 font-medium">Device (IP)</th>
              <th className="px-3 py-2 font-medium">Status</th>
              <th className="px-3 py-2 font-medium">Seen</th>
              <th className="px-3 py-2 font-medium">Last activity</th>
              <th className="px-3 py-2 font-medium text-right">Actions</th>
            </tr>
          </thead>
          <tbody>
            {devices.length === 0 && (
              <tr>
                <td colSpan={5} className="px-3 py-6 text-center text-[var(--text-dim)]">
                  No devices recorded yet.
                </td>
              </tr>
            )}
            {devices.map((d) => (
              <tr key={d.ip_address} className="border-b border-[var(--border)]/60">
                <td className="px-3 py-2">
                  <div className="font-mono">{d.ip_address}</div>
                  {(d.label || d.user_agent) && (
                    <div className="text-xs text-[var(--text-dim)] truncate max-w-[22rem]">
                      {d.label || d.user_agent}
                    </div>
                  )}
                </td>
                <td className="px-3 py-2">
                  <Badge variant={STATUS_BADGE[d.status || ''] ?? 'neutral'}>
                    {d.status ?? 'unknown'}
                  </Badge>
                  {d.auto_enrolled && (
                    <span className="ml-1 text-xs text-[var(--text-dim)]">auto</span>
                  )}
                </td>
                <td className="px-3 py-2 text-[var(--text-dim)]">{d.attempt_count}×</td>
                <td className="px-3 py-2 text-xs text-[var(--text-dim)]">
                  {d.last_seen ? new Date(d.last_seen).toLocaleString() : '—'}
                </td>
                <td className="px-3 py-2">
                  <div className="flex justify-end gap-2">
                    {d.status !== 'approved' && (
                      <Button
                        variant="primary"
                        disabled={busy === d.ip_address}
                        onClick={() => act(() => apiService.approveDevice(d.ip_address), d.ip_address)}
                      >
                        Approve
                      </Button>
                    )}
                    {d.status !== 'blocked' && (
                      <Button
                        disabled={busy === d.ip_address}
                        onClick={() => act(() => apiService.blockDevice(d.ip_address), d.ip_address)}
                      >
                        Block
                      </Button>
                    )}
                    <Button
                      disabled={busy === d.ip_address}
                      onClick={() => act(() => apiService.deleteDevice(d.ip_address), d.ip_address)}
                    >
                      Forget
                    </Button>
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <p className="text-xs text-[var(--text-dim)]">
        Locked out? Set <span className="font-mono">DEVICE_FIREWALL_KILL=true</span>{' '}
        and restart, or run{' '}
        <span className="font-mono">python -m manage_devices approve &lt;ip&gt;</span>{' '}
        inside the server container.
      </p>
    </div>
  )
}
