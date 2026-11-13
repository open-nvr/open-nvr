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

// Settings > Two-factor sign-in: the signed-in user's own authenticator apps.
// Moving to a new app: add it, check it signs in, then remove the old one.
// The last working app can't be removed or turned off (the server enforces
// it too); losing every app is what an administrator's "Reset 2FA" is for.

import { useCallback, useEffect, useState } from 'react'
import { toDataURL } from 'qrcode'
import { useSnackbar } from '../../components/Snackbar'
import { Badge, Button, ErrorCard, Skeleton } from '../../components/ui'
import { Modal } from '../../components/Modal'
import { apiService } from '../../lib/apiService'
import { extractApiError } from '../../lib/apiError'
import { useAuth } from '../../auth/AuthContext'
import { useTranslation, useDateFormat } from '../../i18n'

type Factor = {
  id: number
  kind: string
  label: string
  enabled: boolean
  confirmed: boolean
  created_at: string | null
  last_used_at: string | null
}

// What the password + code dialog is confirming.
type Pending =
  | { kind: 'add' }
  | { kind: 'rename'; factor: Factor }
  | { kind: 'toggle'; factor: Factor }
  | { kind: 'remove'; factor: Factor }

type Enrolment = { factorId: number; secret: string; qr: string }

const inputCls = 'w-full bg-[var(--panel)] border border-[var(--border)] px-3 py-2 text-sm'

export function TwoFactorSettings() {
  const { t } = useTranslation()
  const fmt = useDateFormat()
  const { user } = useAuth()
  const { showSuccess, showError } = useSnackbar()
  const [factors, setFactors] = useState<Factor[] | null>(null)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [pending, setPending] = useState<Pending | null>(null)
  const [password, setPassword] = useState('')
  const [code, setCode] = useState('')
  const [label, setLabel] = useState('')
  const [busy, setBusy] = useState(false)
  const [dialogError, setDialogError] = useState<string | null>(null)
  const [enrolment, setEnrolment] = useState<Enrolment | null>(null)
  const [newCode, setNewCode] = useState('')

  const load = useCallback(async () => {
    try {
      setLoadError(null)
      const { data } = await apiService.mfaFactors()
      setFactors((data?.factors ?? []) as Factor[])
    } catch (e) {
      setLoadError(extractApiError(e, t('twoFactor.failedLoad')))
    }
  }, [t])

  useEffect(() => { load() }, [load])

  const sso = user?.auth_provider === 'oidc' && !user?.mfa_enabled
  const confirmed = (factors ?? []).filter((f) => f.confirmed)
  const working = confirmed.filter((f) => f.enabled)
  const isLastWorking = (f: Factor) => f.enabled && f.confirmed && working.length <= 1

  const open = (p: Pending) => {
    setPending(p)
    setPassword('')
    setCode('')
    setLabel(p.kind === 'rename' ? p.factor.label : p.kind === 'add' ? '' : '')
    setDialogError(null)
  }
  const close = () => { setPending(null); setPassword(''); setCode(''); setDialogError(null) }

  const submit = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!pending) return
    setBusy(true)
    setDialogError(null)
    try {
      if (pending.kind === 'add') {
        const { data } = await apiService.mfaBeginTotp({ label: label.trim() || undefined, current_password: password }, code.trim())
        const qr = await toDataURL(data.otpauth_url, { width: 200, margin: 2, color: { dark: '#000000', light: '#ffffff' } })
        setEnrolment({ factorId: data.factor_id, secret: data.secret, qr })
        setNewCode('')
      } else if (pending.kind === 'rename') {
        await apiService.mfaUpdateFactor(pending.factor.id, { label: label.trim(), current_password: password }, code.trim())
        showSuccess(t('twoFactor.renamed'))
      } else if (pending.kind === 'toggle') {
        await apiService.mfaUpdateFactor(pending.factor.id, { enabled: !pending.factor.enabled, current_password: password }, code.trim())
        showSuccess(pending.factor.enabled ? t('twoFactor.turnedOff', { label: pending.factor.label }) : t('twoFactor.turnedOn', { label: pending.factor.label }))
      } else {
        await apiService.mfaRemoveFactor(pending.factor.id, password, code.trim())
        showSuccess(t('twoFactor.removed', { label: pending.factor.label }))
      }
      close()
      await load()
    } catch (err) {
      setDialogError(extractApiError(err, t('twoFactor.failedChange')))
    } finally {
      setBusy(false)
    }
  }

  const confirmEnrolment = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!enrolment) return
    setBusy(true)
    try {
      await apiService.mfaConfirmTotp(enrolment.factorId, { code: newCode.trim() })
      setEnrolment(null)
      showSuccess(t('twoFactor.addedToast'))
      await load()
    } catch (err) {
      showError(extractApiError(err, t('twoFactor.failedConfirm')))
    } finally {
      setBusy(false)
    }
  }

  if (sso) {
    return <p className="text-sm text-[var(--text-dim)] max-w-2xl">{t('twoFactor.sso')}</p>
  }

  return (
    <div className="space-y-4" data-testid="two-factor-settings">
      <div className="flex items-start justify-between gap-4">
        <p className="text-xs text-[var(--text-dim)] max-w-2xl">{t('twoFactor.description')}</p>
        {!enrolment && (
          <Button variant="primary" data-testid="two-factor-add" onClick={() => open({ kind: 'add' })}>
            {t('twoFactor.add')}
          </Button>
        )}
      </div>

      {enrolment && (
        <form onSubmit={confirmEnrolment} className="border border-[var(--border)] p-4 space-y-3" data-testid="two-factor-enrol">
          <div className="text-sm font-medium">{t('twoFactor.scanTitle')}</div>
          <p className="text-xs text-[var(--text-dim)]">{t('twoFactor.scanHelp')}</p>
          <div className="flex flex-col sm:flex-row gap-4 items-start">
            <img src={enrolment.qr} alt={t('twoFactor.qrAlt')} className="h-[200px] w-[200px] bg-white" />
            <div className="space-y-3 min-w-0 flex-1">
              <div>
                <div className="text-xs text-[var(--text-dim)] mb-1">{t('twoFactor.manualKey')}</div>
                <code className="block font-mono text-xs break-all bg-[var(--panel)] border border-[var(--border)] px-3 py-2 select-all">{enrolment.secret}</code>
              </div>
              <label className="block">
                <span className="text-xs text-[var(--text-dim)]">{t('twoFactor.codeFromNewApp')}</span>
                <input className={`${inputCls} tracking-widest`} inputMode="numeric" autoComplete="one-time-code" maxLength={6}
                  placeholder="123456" value={newCode} onChange={(e) => setNewCode(e.target.value.replace(/\D/g, ''))} autoFocus required />
              </label>
              <div className="flex gap-2">
                <Button variant="outline" onClick={() => setEnrolment(null)}>{t('twoFactor.cancel')}</Button>
                <Button variant="primary" type="submit" disabled={busy || newCode.length !== 6}>{t('twoFactor.finish')}</Button>
              </div>
              <p className="text-[11px] text-[var(--text-dim)]">{t('twoFactor.notActiveYet')}</p>
            </div>
          </div>
        </form>
      )}

      {loadError ? (
        <ErrorCard message={loadError} onRetry={load} />
      ) : factors === null ? (
        <Skeleton className="h-24" />
      ) : confirmed.length === 0 ? (
        <p className="text-sm text-[var(--text-dim)]">{t('twoFactor.none')}</p>
      ) : (
        <div className="overflow-auto border border-[var(--border)]">
          <table className="w-full text-sm">
            <thead className="bg-[var(--panel-2)] text-left">
              <tr>
                <th className="p-2">{t('twoFactor.name')}</th>
                <th className="p-2">{t('twoFactor.status')}</th>
                <th className="p-2">{t('twoFactor.added')}</th>
                <th className="p-2">{t('twoFactor.lastUsed')}</th>
                <th className="p-2" />
              </tr>
            </thead>
            <tbody>
              {confirmed.map((f) => (
                <tr key={f.id} className="odd:bg-[var(--bg-2)] even:bg-[var(--panel)]" data-testid="two-factor-row">
                  <td className="p-2">{f.label}</td>
                  <td className="p-2">
                    <Badge variant={f.enabled ? 'success' : 'neutral'}>{f.enabled ? t('twoFactor.on') : t('twoFactor.off')}</Badge>
                  </td>
                  <td className="p-2 text-xs">{f.created_at ? fmt.dateTime(f.created_at) : '—'}</td>
                  <td className="p-2 text-xs">{f.last_used_at ? fmt.dateTime(f.last_used_at) : t('twoFactor.neverUsed')}</td>
                  <td className="p-2 space-x-2 whitespace-nowrap text-right">
                    <Button size="sm" onClick={() => open({ kind: 'rename', factor: f })}>{t('twoFactor.rename')}</Button>
                    <Button size="sm" disabled={isLastWorking(f)} title={isLastWorking(f) ? t('twoFactor.lastOne') : undefined}
                      onClick={() => open({ kind: 'toggle', factor: f })}>{f.enabled ? t('twoFactor.turnOff') : t('twoFactor.turnOn')}</Button>
                    <Button size="sm" variant="danger" disabled={isLastWorking(f)} title={isLastWorking(f) ? t('twoFactor.lastOne') : undefined}
                      onClick={() => open({ kind: 'remove', factor: f })}>{t('twoFactor.remove')}</Button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {pending && (
        <Modal
          open
          title={<span className="font-semibold">{
            pending.kind === 'add' ? t('twoFactor.add')
              : pending.kind === 'rename' ? t('twoFactor.renameTitle', { label: pending.factor.label })
                : pending.kind === 'toggle' ? (pending.factor.enabled ? t('twoFactor.turnOffTitle', { label: pending.factor.label }) : t('twoFactor.turnOnTitle', { label: pending.factor.label }))
                  : t('twoFactor.removeTitle', { label: pending.factor.label })
          }</span>}
          onClose={close}
          widthClassName="w-[calc(100vw-2rem)] max-w-md"
          closeOnBackdrop={false}
        >
          <form onSubmit={submit} className="space-y-3 p-1" data-testid="two-factor-confirm">
            {dialogError && (
              <div className="p-2 bg-[color-mix(in_oklab,var(--danger)_14%,transparent)] border border-[color-mix(in_oklab,var(--danger)_45%,var(--border))] text-[var(--danger)] text-sm">{dialogError}</div>
            )}
            {(pending.kind === 'add' || pending.kind === 'rename') && (
              <label className="block">
                <span className="text-xs text-[var(--text-dim)]">{t('twoFactor.name')}</span>
                <input className={inputCls} value={label} maxLength={64} placeholder={t('twoFactor.namePlaceholder')}
                  onChange={(e) => setLabel(e.target.value)} required={pending.kind === 'rename'} />
              </label>
            )}
            <label className="block">
              <span className="text-xs text-[var(--text-dim)]">{t('twoFactor.password')}</span>
              <input className={inputCls} type="password" autoComplete="current-password" value={password}
                onChange={(e) => setPassword(e.target.value)} required autoFocus />
            </label>
            <label className="block">
              <span className="text-xs text-[var(--text-dim)]">{t('twoFactor.currentCode')}</span>
              <input className={`${inputCls} tracking-widest`} inputMode="numeric" autoComplete="one-time-code" maxLength={6}
                placeholder="123456" value={code} onChange={(e) => setCode(e.target.value.replace(/\D/g, ''))} required />
            </label>
            <div className="flex justify-end gap-2 pt-1">
              <Button variant="outline" onClick={close}>{t('twoFactor.cancel')}</Button>
              <Button variant={pending.kind === 'remove' ? 'danger' : 'primary'} type="submit"
                disabled={busy || !password || code.length !== 6 || (pending.kind === 'rename' && !label.trim())}>
                {pending.kind === 'add' ? t('twoFactor.continue') : t('twoFactor.confirm')}
              </Button>
            </div>
          </form>
        </Modal>
      )}
    </div>
  )
}
