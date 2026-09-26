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

import { useEffect, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import {
  AlertCircle, AlertTriangle, CheckCircle2, Clock, Eye, EyeOff, KeyRound, Loader2, Lock, UserRound,
} from 'lucide-react'
import { useAuth } from '../auth/AuthContext'
import { useTranslation } from '../i18n'
import { AuthAlert, AuthLayout, authButton, authFieldIcon, authInput, authLabel, authLink } from '../components/AuthLayout'

export function Login() {
  const { t } = useTranslation()
  const { login, loading, error, setupRequired } = useAuth()
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [showPassword, setShowPassword] = useState(false)
  const [msg, setMsg] = useState<string | null>(null)
  const [retryAfterSeconds, setRetryAfterSeconds] = useState(0)
  const navigate = useNavigate()

  const formatRetryTime = (seconds: number) => {
    if (seconds <= 0) return '0s'
    const mins = Math.floor(seconds / 60)
    const secs = seconds % 60
    if (mins > 0 && secs > 0) return `${mins}m ${secs}s`
    if (mins > 0) return `${mins}m`
    return `${secs}s`
  }

  // Redirect to setup if required
  useEffect(() => {
    if (setupRequired) {
      navigate('/first-time-setup', { replace: true })
    }
  }, [setupRequired, navigate])

  useEffect(() => {
    if (retryAfterSeconds <= 0) return
    const timer = window.setInterval(() => {
      setRetryAfterSeconds((prev) => (prev > 0 ? prev - 1 : 0))
    }, 1000)
    return () => window.clearInterval(timer)
  }, [retryAfterSeconds])

  const onSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    if (retryAfterSeconds > 0) return
    setMsg(null)
    try {
      await login(username, password)
      setRetryAfterSeconds(0)
      navigate('/')
    } catch (e: any) {
      if (e?.setupRequired) {
        navigate('/first-time-setup', { replace: true })
        return
      }
      if (e?.accountLocked) {
        setRetryAfterSeconds(Math.max(0, Number(e?.retryAfterSeconds || 0)))
        return
      }
      if (e?.mfaRequired) {
        navigate('/mfa-verify', { state: { username, password } })
        return
      }
      // other errors handled in context
    }
  }

  const locked = retryAfterSeconds > 0

  return (
    <AuthLayout>
      <form onSubmit={onSubmit} className="space-y-5">
          {setupRequired && (
            <AuthAlert tone="warn" icon={<AlertTriangle size={16} />}>{t('login.setupRequired')}</AuthAlert>
          )}
          {locked && (
            <AuthAlert tone="warn" icon={<Clock size={16} />}>
              {t('login.tooManyAttempts')} {formatRetryTime(retryAfterSeconds)}.
            </AuthAlert>
          )}
          {error && <AuthAlert tone="error" icon={<AlertCircle size={16} />}>{error}</AuthAlert>}
          {msg && <AuthAlert tone="ok" icon={<CheckCircle2 size={16} />}>{msg}</AuthAlert>}

          <div className="space-y-1.5">
            <label htmlFor="login-username" className={authLabel}>
              {t('login.username')}
            </label>
            <div className="relative">
              <UserRound size={16} className={authFieldIcon} />
              <input
                id="login-username"
                className={authInput}
                value={username}
                onChange={(e) => setUsername(e.target.value)}
                placeholder="admin"
                autoComplete="username"
                autoFocus
                required
              />
            </div>
          </div>

          <div className="space-y-1.5">
            <div className="flex items-center justify-between">
              <label htmlFor="login-password" className={authLabel}>
                {t('login.password')}
              </label>
              <ForgotPassword />
            </div>
            <div className="relative">
              <Lock size={16} className={authFieldIcon} />
              <input
                id="login-password"
                type={showPassword ? 'text' : 'password'}
                className={`${authInput} pr-11`}
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                autoComplete="current-password"
                required
              />
              <button
                type="button"
                onClick={() => setShowPassword((v) => !v)}
                className="absolute right-1.5 top-1/2 grid h-8 w-8 -translate-y-1/2 place-items-center rounded-md text-gray-500 hover:bg-white/5 hover:text-gray-200"
                aria-label={showPassword ? t('login.hidePassword') : t('login.showPassword')}
                title={showPassword ? t('login.hidePassword') : t('login.showPassword')}
              >
                {showPassword ? <EyeOff size={16} /> : <Eye size={16} />}
              </button>
            </div>
          </div>

          <button
            type="submit"
            disabled={loading || locked}
            className={authButton}
          >
            {loading && <Loader2 size={16} className="animate-spin" />}
            {loading
              ? t('login.signingIn')
              : locked
                ? `${t('login.tryAgainIn')} ${formatRetryTime(retryAfterSeconds)}`
                : t('login.signIn')}
          </button>
      </form>
    </AuthLayout>
  )
}

/** "Forgot password?" as a popover anchored to the link.
 *
 *  There is no self-service reset — no mail server to send a link
 *  through — so the honest answer is who can help. It floats rather than
 *  pushing the form down, opens on click only (a hover card that appears
 *  as the pointer crosses the form is a distraction), and closes on
 *  "Got it", Escape or a click outside. */
function ForgotPassword() {
  const { t } = useTranslation()
  const [open, setOpen] = useState(false)
  const ref = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!open) return
    const onDown = (e: MouseEvent) => {
      if (!ref.current?.contains(e.target as Node)) setOpen(false)
    }
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') setOpen(false) }
    document.addEventListener('mousedown', onDown)
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('mousedown', onDown)
      document.removeEventListener('keydown', onKey)
    }
  }, [open])

  return (
    <div ref={ref} className="relative">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        aria-controls="forgot-password-help"
        className={`text-xs ${authLink}`}
      >
        {t('login.forgot')}
      </button>
      {open && (
        <div
          id="forgot-password-help"
          role="dialog"
          aria-label={t('login.forgotTitle')}
          className="absolute right-0 top-full z-20 mt-2 w-72 rounded-xl border border-[#2a3a4f] bg-[#111a27] p-4 shadow-2xl"
        >
          {/* The arrow: a rotated square sharing the card's border. */}
          <span
            aria-hidden
            className="absolute -top-1.5 right-6 h-3 w-3 rotate-45 border-l border-t border-[#2a3a4f] bg-[#111a27]"
          />
          <div className="flex gap-3">
            <span className="grid h-8 w-8 shrink-0 place-items-center rounded-lg bg-[#5eb3f6]/15 text-[#5eb3f6]">
              <KeyRound size={16} />
            </span>
            <div className="space-y-1">
              <div className="text-sm font-semibold text-gray-100">{t('login.forgotTitle')}</div>
              <p className="text-xs leading-relaxed text-gray-400">{t('login.forgotHelp')}</p>
            </div>
          </div>
          <div className="mt-3 flex justify-end">
            <button
              type="button"
              onClick={() => setOpen(false)}
              autoFocus
              className="rounded-md px-2.5 py-1 text-xs font-medium text-[#5eb3f6] hover:bg-white/5"
            >
              {t('login.forgotOk')}
            </button>
          </div>
        </div>
      )}
    </div>
  )
}
