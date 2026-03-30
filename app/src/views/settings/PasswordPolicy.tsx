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
import { apiService } from '../../lib/apiService'
import { useAuth } from '../../auth/AuthContext'

type Policy = {
  min_length: number
  min_classes: number
  disallow_username_email: boolean
  passphrase_enabled: boolean
  passphrase_min_length: number
  history_count: number
  expiration_days?: number | null
  max_failed_attempts: number
  lockout_minutes: number
  reset_token_ttl_minutes: number
  require_mfa_for_privileged: boolean
}

const defaultPolicy: Policy = {
  min_length: 12,
  min_classes: 3,
  disallow_username_email: true,
  passphrase_enabled: true,
  passphrase_min_length: 16,
  history_count: 5,
  expiration_days: null,
  max_failed_attempts: 5,
  lockout_minutes: 15,
  reset_token_ttl_minutes: 15,
  require_mfa_for_privileged: true,
}

export function PasswordPolicy() {
  const { user: me } = useAuth()
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [policy, setPolicy] = useState<Policy>(defaultPolicy)

  const canAdmin = !!me?.is_superuser

  useEffect(() => {
    if (!canAdmin) return
    ;(async () => {
      try {
        setLoading(true)
        setError(null)
        const res = await apiService.getPasswordPolicy()
        const data = res.data as any
        setPolicy({
          min_length: data.min_length,
          min_classes: data.min_classes,
          disallow_username_email: data.disallow_username_email,
          passphrase_enabled: data.passphrase_enabled,
          passphrase_min_length: data.passphrase_min_length,
          history_count: data.history_count,
          expiration_days: data.expiration_days ?? null,
          max_failed_attempts: data.max_failed_attempts,
          lockout_minutes: data.lockout_minutes,
          reset_token_ttl_minutes: data.reset_token_ttl_minutes,
          require_mfa_for_privileged: data.require_mfa_for_privileged,
        })
      } catch (e: any) {
        const detail = e?.data?.detail || e?.response?.data?.detail
        if (Array.isArray(detail)) {
          setError(detail.map((d: any) => d.msg || JSON.stringify(d)).join(', '))
        } else if (typeof detail === 'object' && detail !== null) {
          setError(detail.msg || JSON.stringify(detail))
        } else {
          setError(detail || e?.message || 'Failed to load policy')
        }
      } finally {
        setLoading(false)
      }
    })()
  }, [canAdmin])

  const save = async () => {
    try {
      setLoading(true)
      setError(null)
      await apiService.updatePasswordPolicy({
        ...policy,
        expiration_days: policy.expiration_days ?? null,
      })
    } catch (e: any) {
      const detail = e?.data?.detail || e?.response?.data?.detail
      if (Array.isArray(detail)) {
        setError(detail.map((d: any) => d.msg || JSON.stringify(d)).join(', '))
      } else if (typeof detail === 'object' && detail !== null) {
        setError(detail.msg || JSON.stringify(detail))
      } else {
        setError(detail || e?.message || 'Failed to save policy')
      }
    } finally {
      setLoading(false)
    }
  }

  if (!canAdmin) {
    return <div className="text-sm text-amber-400">Admin only: you don’t have permission to modify password policy.</div>
  }

  return (
    <div className="space-y-3">
      <div className="flex items-center gap-2">
        <h2 className="text-base font-semibold">Password Policy</h2>
        <button className="ml-auto px-2 py-1 bg-[var(--accent)] text-white" onClick={save} disabled={loading}>Save</button>
      </div>
      {error && <div className="text-sm text-red-400">{error}</div>}
      <div className="grid grid-cols-2 gap-3 text-sm">
        <label className="flex items-center justify-between gap-2 border border-neutral-700 bg-[var(--panel-2)] p-2">
          <span>Minimum length</span>
          <input type="number" min={4} max={128} className="w-24 bg-[var(--panel)] border border-neutral-700 px-2 py-1" value={policy.min_length} onChange={(e) => setPolicy({ ...policy, min_length: Number(e.target.value) })} />
        </label>
        <label className="flex items-center justify-between gap-2 border border-neutral-700 bg-[var(--panel-2)] p-2">
          <span>Required character classes</span>
          <select className="w-24 bg-[var(--panel)] border border-neutral-700 px-2 py-1" value={policy.min_classes} onChange={(e) => setPolicy({ ...policy, min_classes: Number(e.target.value) })}>
            {[1,2,3,4].map(n => <option key={n} value={n}>{n}</option>)}
          </select>
        </label>
        <label className="flex items-center justify-between gap-2 border border-neutral-700 bg-[var(--panel-2)] p-2">
          <span>Disallow username/email in password</span>
          <input type="checkbox" className="accent-[var(--accent)]" checked={policy.disallow_username_email} onChange={(e) => setPolicy({ ...policy, disallow_username_email: e.target.checked })} />
        </label>
        <label className="flex items-center justify-between gap-2 border border-neutral-700 bg-[var(--panel-2)] p-2">
          <span>Enable passphrase mode</span>
          <input type="checkbox" className="accent-[var(--accent)]" checked={policy.passphrase_enabled} onChange={(e) => setPolicy({ ...policy, passphrase_enabled: e.target.checked })} />
        </label>
        <label className="flex items-center justify-between gap-2 border border-neutral-700 bg-[var(--panel-2)] p-2">
          <span>Passphrase min length</span>
          <input type="number" min={8} max={256} className="w-24 bg-[var(--panel)] border border-neutral-700 px-2 py-1" value={policy.passphrase_min_length} onChange={(e) => setPolicy({ ...policy, passphrase_min_length: Number(e.target.value) })} />
        </label>
        <label className="flex items-center justify-between gap-2 border border-neutral-700 bg-[var(--panel-2)] p-2">
          <span>Password history (disallow last N)</span>
          <input type="number" min={0} max={50} className="w-24 bg-[var(--panel)] border border-neutral-700 px-2 py-1" value={policy.history_count} onChange={(e) => setPolicy({ ...policy, history_count: Number(e.target.value) })} />
        </label>
        <label className="flex items-center justify-between gap-2 border border-neutral-700 bg-[var(--panel-2)] p-2">
          <span>Expiration (days, 0 = off)</span>
          <input type="number" min={0} max={3650} className="w-24 bg-[var(--panel)] border border-neutral-700 px-2 py-1" value={policy.expiration_days ?? 0} onChange={(e) => setPolicy({ ...policy, expiration_days: Number(e.target.value) })} />
        </label>
        <label className="flex items-center justify-between gap-2 border border-neutral-700 bg-[var(--panel-2)] p-2">
          <span>Max failed attempts</span>
          <input type="number" min={0} max={50} className="w-24 bg-[var(--panel)] border border-neutral-700 px-2 py-1" value={policy.max_failed_attempts} onChange={(e) => setPolicy({ ...policy, max_failed_attempts: Number(e.target.value) })} />
        </label>
        <label className="flex items-center justify-between gap-2 border border-neutral-700 bg-[var(--panel-2)] p-2">
          <span>Lockout (minutes)</span>
          <input type="number" min={0} max={1440} className="w-24 bg-[var(--panel)] border border-neutral-700 px-2 py-1" value={policy.lockout_minutes} onChange={(e) => setPolicy({ ...policy, lockout_minutes: Number(e.target.value) })} />
        </label>
        <label className="flex items-center justify-between gap-2 border border-neutral-700 bg-[var(--panel-2)] p-2">
          <span>Reset token TTL (minutes)</span>
          <input type="number" min={1} max={1440} className="w-24 bg-[var(--panel)] border border-neutral-700 px-2 py-1" value={policy.reset_token_ttl_minutes} onChange={(e) => setPolicy({ ...policy, reset_token_ttl_minutes: Number(e.target.value) })} />
        </label>
        <label className="flex items-center justify-between gap-2 border border-neutral-700 bg-[var(--panel-2)] p-2">
          <span>Require MFA for admin/operator</span>
          <input type="checkbox" className="accent-[var(--accent)]" checked={policy.require_mfa_for_privileged} onChange={(e) => setPolicy({ ...policy, require_mfa_for_privileged: e.target.checked })} />
        </label>
      </div>
    </div>
  )
}


