/*
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

// An administrator's view of another user's authenticator apps: turn one
// off (a phone that went missing), back on, or remove it. Doing that to
// their last working app is a reset on the server (they are signed out and
// enrol again), so the dialog says so before it happens.

import { useCallback, useEffect, useState } from 'react'
import { KeyRound } from 'lucide-react'
import { Modal } from '../../components/Modal'
import { Badge, Button } from '../../components/ui'
import { apiService } from '../../lib/apiService'
import { extractApiError } from '../../lib/apiError'
import { useDateFormat } from '../../i18n'
import { useConfirm } from '../../components/ui/ConfirmDialog'

type Factor = {
  id: number
  label: string
  enabled: boolean
  confirmed: boolean
  created_at: string | null
  last_used_at: string | null
}

type Props = {
  user: { id: number; username: string; email: string }
  onClose: () => void
  onReset: () => void
  onChanged: () => void
}

export function UserMfaDialog({ user, onClose, onReset, onChanged }: Props) {
  const fmt = useDateFormat()
  const confirm = useConfirm()
  const [factors, setFactors] = useState<Factor[] | null>(null)
  const [code, setCode] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const load = useCallback(async () => {
    try {
      const { data } = await apiService.userMfaFactors(user.id)
      setFactors((data?.factors ?? []) as Factor[])
    } catch (e) {
      setError(extractApiError(e, 'Could not load their authenticator apps'))
    }
  }, [user.id])

  useEffect(() => { load() }, [load])

  const working = (factors ?? []).filter((f) => f.enabled)
  const isLast = (f: Factor) => f.enabled && working.length <= 1

  const act = async (f: Factor, what: 'toggle' | 'remove') => {
    if (isLast(f) && !(await confirm({
      title: `“${f.label}” is ${user.username}'s only working authenticator app. ` +
        `${what === 'remove' ? 'Removing' : 'Turning off'} it resets their two-factor sign-in: ` +
        'they are signed out and set up a new app at their next sign-in.',
      confirmLabel: what === 'remove' ? 'Remove and reset' : 'Turn off and reset',
      danger: true,
    }))) return
    setBusy(true)
    setError(null)
    try {
      const { data } = what === 'remove'
        ? await apiService.removeUserMfaFactor(user.id, f.id, code.trim())
        : await apiService.setUserMfaFactor(user.id, f.id, !f.enabled, code.trim())
      setFactors((data?.factors ?? []) as Factor[])
      onChanged()
    } catch (e) {
      setError(extractApiError(e, 'Could not make that change'))
    } finally {
      setBusy(false)
    }
  }

  return (
    <Modal
      open
      title={<span className="font-semibold flex items-center gap-2"><KeyRound size={18} /> Two-factor sign-in: {user.username}</span>}
      onClose={onClose}
      widthClassName="w-[calc(100vw-2rem)] max-w-xl"
      closeOnBackdrop={false}
      data-testid="user-mfa-dialog"
    >
      <div className="space-y-4 p-1">
        {error && (
          <div className="p-2 bg-[color-mix(in_oklab,var(--danger)_14%,transparent)] border border-[color-mix(in_oklab,var(--danger)_45%,var(--border))] text-[var(--danger)] text-sm">{error}</div>
        )}
        {factors === null ? (
          <div className="text-sm text-[var(--text-dim)]">Loading…</div>
        ) : factors.length === 0 ? (
          <div className="text-sm text-[var(--text-dim)]">{user.username} has no authenticator app set up. They will be asked to set one up at their next sign-in.</div>
        ) : (
          <table className="w-full text-sm border border-[var(--border)]">
            <thead className="bg-[var(--panel-2)] text-left">
              <tr><th className="p-2">App</th><th className="p-2">Status</th><th className="p-2">Last used</th><th className="p-2" /></tr>
            </thead>
            <tbody>
              {factors.map((f) => (
                <tr key={f.id} className="odd:bg-[var(--bg-2)] even:bg-[var(--panel)]">
                  <td className="p-2">{f.label}</td>
                  <td className="p-2"><Badge variant={f.enabled ? 'success' : 'neutral'}>{f.enabled ? 'On' : 'Off'}</Badge></td>
                  <td className="p-2 text-xs">{f.last_used_at ? fmt.dateTime(f.last_used_at) : 'Not used yet'}</td>
                  <td className="p-2 space-x-2 whitespace-nowrap text-right">
                    <Button size="sm" disabled={busy || code.length !== 6} onClick={() => act(f, 'toggle')}>{f.enabled ? 'Turn off' : 'Turn on'}</Button>
                    <Button size="sm" variant="danger" disabled={busy || code.length !== 6} onClick={() => act(f, 'remove')}>Remove</Button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
        <label className="flex flex-col gap-1">
          <span className="text-xs text-[var(--text-dim)]">Your MFA code (needed for each change)</span>
          <input type="text" inputMode="numeric" autoComplete="one-time-code" maxLength={6} placeholder="123456"
            className="bg-[var(--bg-2)] border border-[var(--border)] px-3 py-2 text-sm tracking-widest"
            value={code} onChange={(e) => setCode(e.target.value.replace(/\D/g, ''))} />
        </label>
        <div className="flex items-center justify-between gap-2 border-t border-[var(--border)] pt-3">
          <span className="text-xs text-[var(--text-dim)]">Lost every app? Reset removes them all and signs {user.username} out.</span>
          <div className="flex gap-2">
            <Button variant="outline" onClick={onClose}>Close</Button>
            {factors !== null && factors.length > 0 && <Button variant="danger" onClick={onReset}>Reset 2FA…</Button>}
          </div>
        </div>
      </div>
    </Modal>
  )
}
