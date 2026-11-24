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

// Where a sensitive action asks for "your MFA code" (sent as X-MFA-Code).
// Someone who signs in through single sign-on has no authenticator app, so
// instead of the six-digit field they get a button that opens the identity
// provider in a pop-up to sign in again. The server answers with a
// short-lived step-up token, which goes where the code would ("sso:<token>").

import { useState } from 'react'
import { ShieldCheck } from 'lucide-react'
import { useAuth } from '../auth/AuthContext'

export const SSO_STEP_UP_PREFIX = 'sso:'
const CHANNEL = 'opennvr-sso-step-up'
const WAIT_MS = 5 * 60 * 1000

type StepUpMessage = { type?: string; rid?: string; token?: string | null; error?: string | null }

/** True when the signed-in account confirms by signing in at its provider. */
export function useSsoStepUp(): boolean {
  const { user } = useAuth()
  return user?.auth_provider === 'oidc' && !user?.mfa_enabled
}

/** Whether ``value`` can be sent: six digits, or a single sign-on confirmation. */
export function stepUpReady(value: string): boolean {
  return value.startsWith(SSO_STEP_UP_PREFIX) ? value.length > SSO_STEP_UP_PREFIX.length : /^\d{6}$/.test(value.trim())
}

function requestId(): string {
  const bytes = new Uint8Array(16)
  crypto.getRandomValues(bytes)
  return Array.from(bytes, (b) => b.toString(16).padStart(2, '0')).join('')
}

/** Sign in at the identity provider again, in a pop-up; resolves to the
 * value to send as X-MFA-Code. The answer comes back over a same-origin
 * BroadcastChannel (a provider's Cross-Origin-Opener-Policy can cut the
 * pop-up off from window.opener), with postMessage as a second route. */
export function confirmWithSso(): Promise<string> {
  const rid = requestId()
  const popup = window.open(`/api/v1/auth/oidc/start?reauth=${rid}`, `opennvr-sso-${rid}`, 'popup,width=520,height=720')
  if (!popup) return Promise.reject(new Error('The browser blocked the sign-in window. Allow pop-ups for this site and try again.'))
  return new Promise((resolve, reject) => {
    let channel: BroadcastChannel | null = null
    const finish = (data: StepUpMessage | null | undefined) => {
      if (!data || data.type !== 'sso-step-up' || data.rid !== rid) return
      cleanup()
      if (data.token) resolve(SSO_STEP_UP_PREFIX + data.token)
      else reject(new Error(data.error || 'Single sign-on did not confirm it was you.'))
    }
    const onMessage = (e: MessageEvent) => { if (e.origin === window.location.origin) finish(e.data) }
    const timer = window.setTimeout(() => { cleanup(); reject(new Error('Single sign-on took too long. Try again.')) }, WAIT_MS)
    function cleanup() {
      window.clearTimeout(timer)
      window.removeEventListener('message', onMessage)
      try { channel?.close() } catch { /* already closed */ }
    }
    try {
      channel = new BroadcastChannel(CHANNEL)
      channel.onmessage = (e) => finish(e.data)
    } catch { /* older browser: postMessage below still works without COOP */ }
    window.addEventListener('message', onMessage)
  })
}

type Props = {
  value: string
  onChange: (value: string) => void
  /** Classes for the six-digit input (the button keeps its own look). */
  className?: string
  placeholder?: string
  autoFocus?: boolean
  required?: boolean
  'data-testid'?: string
}

/** The six-digit MFA code field, or for a single sign-on account the
 * "Confirm with single sign-on" button. Either way ``value`` is what to send. */
export function StepUpCode({ value, onChange, className, placeholder = '123456', autoFocus, required, ...rest }: Props) {
  const sso = useSsoStepUp()
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  if (!sso) {
    return (
      <input
        type="text"
        inputMode="numeric"
        autoComplete="one-time-code"
        maxLength={6}
        className={className}
        placeholder={placeholder}
        value={value}
        onChange={(e) => onChange(e.target.value.replace(/\D/g, ''))}
        autoFocus={autoFocus}
        required={required}
        data-testid={rest['data-testid']}
      />
    )
  }

  const confirmed = value.startsWith(SSO_STEP_UP_PREFIX)
  const start = async () => {
    setBusy(true)
    setError(null)
    try {
      onChange(await confirmWithSso())
    } catch (e) {
      onChange('')
      setError(e instanceof Error ? e.message : 'Single sign-on failed')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="flex flex-col gap-1" data-testid={rest['data-testid']}>
      <div className="flex flex-wrap items-center gap-2">
        <button
          type="button"
          onClick={start}
          disabled={busy}
          className="inline-flex items-center gap-2 px-3 py-2 text-sm border border-[var(--border)] bg-[var(--bg-2)] hover:bg-[var(--panel-2)] disabled:opacity-50"
          data-testid="sso-step-up-button"
        >
          <ShieldCheck size={16} />
          {busy ? 'Waiting for single sign-on…' : confirmed ? 'Confirm again' : 'Confirm with single sign-on'}
        </button>
        {confirmed && !busy && <span className="text-xs text-[var(--ok)]">Confirmed for the next few minutes</span>}
      </div>
      <span className="text-[10px] text-[var(--text-dim)]">
        Your account signs in through single sign-on, so you confirm by signing in there again.
      </span>
      {error && <span role="alert" className="text-xs text-[var(--danger)]">{error}</span>}
    </div>
  )
}
