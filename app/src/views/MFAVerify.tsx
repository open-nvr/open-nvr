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
import { Link, useLocation, useNavigate } from 'react-router-dom'
import { useAuth } from '../auth/AuthContext'
import { useTranslation } from '../i18n'
import { AlertCircle, ArrowLeft, Clock, Loader2, ShieldCheck } from 'lucide-react'
import { AuthAlert, AuthLayout, authButton, authLink } from '../components/AuthLayout'

type LocationState = {
  username?: string
  password?: string
}

export function MFAVerify() {
  const { t } = useTranslation()
  const { state } = useLocation()
  const navigate = useNavigate()
  const { login, loading, error } = useAuth()
  const { username, password } = (state || {}) as LocationState

  const [code, setCode] = useState('')
  const [retryAfterSeconds, setRetryAfterSeconds] = useState(0)

  const formatRetryTime = (seconds: number) => {
    if (seconds <= 0) return '0s'
    const mins = Math.floor(seconds / 60)
    const secs = seconds % 60
    if (mins > 0 && secs > 0) return `${mins}m ${secs}s`
    if (mins > 0) return `${mins}m`
    return `${secs}s`
  }

  useEffect(() => {
    if (retryAfterSeconds <= 0) return
    const timer = window.setInterval(() => {
      setRetryAfterSeconds((prev) => (prev > 0 ? prev - 1 : 0))
    }, 1000)
    return () => window.clearInterval(timer)
  }, [retryAfterSeconds])

  const onSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!username || !password || retryAfterSeconds > 0) return
    try {
      await login(username, password, code)
      navigate('/')
    } catch (e: any) {
      if (e?.accountLocked) {
        setRetryAfterSeconds(Math.max(0, Number(e?.retryAfterSeconds || 0)))
      }
    }
  }

  const locked = retryAfterSeconds > 0
  const shell = (children: React.ReactNode) => <AuthLayout>{children}</AuthLayout>
  const backLink = (
    <Link to="/login" className={`inline-flex items-center gap-1.5 text-sm ${authLink}`}>
      <ArrowLeft size={14} /> {t('mfa.differentAccount')}
    </Link>
  )

  if (!username || !password) {
    return shell(
      <>
        <AuthAlert tone="error" icon={<AlertCircle size={16} />}>{t('mfa.missingCredentials')}</AuthAlert>
        <div className="text-center">{backLink}</div>
      </>,
    )
  }

  return shell(
    <form onSubmit={onSubmit} className="space-y-5">
      {/* Centred and stacked: one heading, one line of instruction, then
          the field. The side-by-side icon made the header ragged, and a
          field label repeating the hint was noise. */}
      <div className="flex flex-col items-center gap-3 text-center">
        <span className="grid h-12 w-12 place-items-center rounded-full bg-[#5eb3f6]/15 text-[#5eb3f6]">
          <ShieldCheck size={22} />
        </span>
        <div className="space-y-1">
          <h1 className="text-lg font-semibold text-gray-100">{t('mfa.verifyTitle')}</h1>
          <p className="text-sm text-gray-400">{t('mfa.verifyHint')}</p>
        </div>
      </div>

      {locked && (
        <AuthAlert tone="warn" icon={<Clock size={16} />}>
          {t('login.tooManyAttempts')} {formatRetryTime(retryAfterSeconds)}.
        </AuthAlert>
      )}
      {error && <AuthAlert tone="error" icon={<AlertCircle size={16} />}>{error}</AuthAlert>}

      <div>
        <label htmlFor="mfa-code" className="sr-only">{t('mfa.code')}</label>
        <input
          id="mfa-code"
          className="h-12 w-full rounded-lg border border-[#2a3a4f] bg-[#0f1720] px-3 text-center font-mono text-xl tracking-[0.5em] text-gray-100 outline-none transition-colors placeholder:text-gray-600 focus:border-[#5eb3f6] focus:ring-2 focus:ring-[#5eb3f6]/20"
          value={code}
          onChange={(e) => setCode(e.target.value.replace(/\s+/g, ''))}
          inputMode="numeric"
          autoComplete="one-time-code"
          autoFocus
          minLength={6}
          maxLength={8}
          placeholder="000000"
          required
        />
      </div>

      <button
        type="submit"
        disabled={loading || locked}
        className={authButton}
      >
        {loading && <Loader2 size={16} className="animate-spin" />}
        {loading
          ? t('mfa.verifying')
          : locked
            ? `${t('login.tryAgainIn')} ${formatRetryTime(retryAfterSeconds)}`
            : t('mfa.verify')}
      </button>

      <div className="text-center">{backLink}</div>
    </form>,
  )
}
