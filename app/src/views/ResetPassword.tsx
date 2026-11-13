/**
 * Copyright (c) 2026 OpenNVR
 * SPDX-License-Identifier: AGPL-3.0-or-later
 *
 * The page a reset link opens: a new password, twice, then back to sign
 * in. The token in the URL is single-use and thirty minutes old at most;
 * the server says so when it is neither.
 */
import { useState } from 'react'
import { Link, useNavigate, useSearchParams } from 'react-router-dom'
import { AlertCircle, CheckCircle2, KeyRound, Loader2, Lock } from 'lucide-react'
import { useTranslation } from '../i18n'
import { apiService } from '../lib/apiService'
import { extractApiError } from '../lib/apiError'
import { AuthAlert, AuthLayout, authButton, authFieldIcon, authInput, authLabel, authLink } from '../components/AuthLayout'

export function ResetPassword() {
  const { t } = useTranslation()
  const [params] = useSearchParams()
  const navigate = useNavigate()
  const token = params.get('token') || ''
  const [password, setPassword] = useState('')
  const [confirm, setConfirm] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [done, setDone] = useState<string | null>(null)

  const onSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    setError(null)
    if (password !== confirm) {
      setError(t('reset.mismatch'))
      return
    }
    setBusy(true)
    try {
      const { data } = await apiService.resetPassword(token, password)
      setDone(data.username)
      window.setTimeout(() => navigate('/login', { replace: true, state: { message: t('reset.done') } }), 1500)
    } catch (err: any) {
      setError(extractApiError(err, t('reset.failed')))
    } finally {
      setBusy(false)
    }
  }

  return (
    <AuthLayout>
      <form onSubmit={onSubmit} className="space-y-5">
        <div className="flex items-center gap-2 text-sm font-semibold text-[var(--text)]">
          <KeyRound size={16} /> {t('reset.title')}
        </div>
        {!token && <AuthAlert tone="error" icon={<AlertCircle size={16} />}>{t('reset.noToken')}</AuthAlert>}
        {error && <AuthAlert tone="error" icon={<AlertCircle size={16} />}>{error}</AuthAlert>}
        {done && <AuthAlert tone="ok" icon={<CheckCircle2 size={16} />}>{t('reset.done')}</AuthAlert>}
        <div className="space-y-1.5">
          <label htmlFor="reset-password" className={authLabel}>{t('reset.newPassword')}</label>
          <div className="relative">
            <Lock size={16} className={authFieldIcon} />
            <input id="reset-password" type="password" className={authInput} value={password}
              onChange={(e) => setPassword(e.target.value)} autoComplete="new-password" minLength={8} required autoFocus />
          </div>
        </div>
        <div className="space-y-1.5">
          <label htmlFor="reset-confirm" className={authLabel}>{t('reset.confirm')}</label>
          <div className="relative">
            <Lock size={16} className={authFieldIcon} />
            <input id="reset-confirm" type="password" className={authInput} value={confirm}
              onChange={(e) => setConfirm(e.target.value)} autoComplete="new-password" minLength={8} required />
          </div>
        </div>
        <button type="submit" disabled={busy || !token || !!done} className={authButton}>
          {busy && <Loader2 size={16} className="animate-spin" />}
          {t('reset.submit')}
        </button>
        <div className="text-center text-xs">
          <Link to="/login" className={authLink}>{t('reset.backToLogin')}</Link>
        </div>
      </form>
    </AuthLayout>
  )
}
