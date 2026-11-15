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
import { useNavigate } from 'react-router-dom'
import { toDataURL } from 'qrcode'
import { apiService } from '../lib/apiService'
import { useAuth } from '../auth/AuthContext'
import {
  AlertCircle, ArrowRight, Check, Copy, Eye, EyeOff, KeyRound, Loader2, Lock, ShieldCheck, UserRound,
} from 'lucide-react'
import { AuthAlert, AuthLayout, authButton, authFieldIcon, authInput, authLabel } from '../components/AuthLayout'
import { RecoveryCodesPanel } from '../components/RecoveryCodesPanel'

type SignIn = { access_token: string; refresh_token: string; device_token?: string | null }

export function FirstTimeSetup() {
  const [password, setPassword] = useState('')
  const [confirmPassword, setConfirmPassword] = useState('')
  // V-001 / M0 C-1: the setup token is printed once on server startup
  // (stdout banner). Operator must paste it here to activate the admin
  // account — closes the bootstrap-race window where any LAN attacker
  // could claim the admin role between server-up and operator-setup.
  const [setupToken, setSetupToken] = useState('')
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [mfaSecret, setMfaSecret] = useState<string | null>(null)
  const [qrDataUrl, setQrDataUrl] = useState<string>('')
  // Step 2: the server hands the browser that claimed the account a session
  // for confirming the authenticator; a code from the scanned QR finishes it.
  const [setupSession, setSetupSession] = useState<string | null>(null)
  const [code, setCode] = useState('')
  const [verifying, setVerifying] = useState(false)
  const [verifyError, setVerifyError] = useState<string | null>(null)
  // Too many wrong codes, or the server restarted: the enrolment wall after
  // a password sign-in finishes the same pending authenticator.
  const [sessionEnded, setSessionEnded] = useState(false)
  // Step 3: recovery codes, shown once, and the session the verify step opened.
  const [recoveryCodes, setRecoveryCodes] = useState<string[] | null>(null)
  const [signIn, setSignIn] = useState<SignIn | null>(null)
  const navigate = useNavigate()
  const { checkSetupStatus, loginWithTokens, setupUsername } = useAuth()
  // The account to activate is the one the server created from
  // DEFAULT_ADMIN_USERNAME. Never assume 'admin': submitting a name with no
  // pending account is exactly how issue #632 locked operators out.
  const username = setupUsername ?? ''

  // Reached via a login redirect rather than app bootstrap: ask the server.
  useEffect(() => {
    if (!setupUsername) void checkSetupStatus()
  }, [setupUsername, checkSetupStatus])

  const onSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    setError(null)

    // Validation
    if (!setupToken.trim()) {
      setError(
        'Setup token is required. Find it in the server terminal output.'
      )
      return
    }
    if (!username) {
      setError('Could not load the admin username from the server. Reload the page.')
      return
    }
    if (password.length < 8) {
      setError('Password must be at least 8 characters long')
      return
    }
    if (password !== confirmPassword) {
      setError('Passwords do not match')
      return
    }

    setLoading(true)
    try {
      const { data } = await apiService.firstTimeSetup(
        username,
        password,
        setupToken.trim(),
      )

      // Show MFA setup
      setMfaSecret(data.mfa_secret)
      setSetupSession(data.setup_session ?? null)

      // Generate QR code image from otpauth URI
      if (data.mfa_qr_uri) {
        const qrUrl = await toDataURL(data.mfa_qr_uri, { width: 220, margin: 2 })
        setQrDataUrl(qrUrl)
      }
    } catch (e: any) {
      const message = e?.data?.detail || e?.message || 'Setup failed'
      setError(message)
    } finally {
      setLoading(false)
    }
  }

  const onVerify = async (e: React.FormEvent) => {
    e.preventDefault()
    setVerifyError(null)
    if (!setupSession) {
      setVerifyError('This setup session has ended. Sign in with your password to finish.')
      setSessionEnded(true)
      return
    }
    setVerifying(true)
    try {
      const { data } = await apiService.firstTimeSetupVerify(setupSession, code.replace(/\s+/g, ''))
      setSignIn({
        access_token: data.access_token,
        refresh_token: data.refresh_token,
        device_token: data.device_token,
      })
      setRecoveryCodes(data.recovery_codes ?? [])
    } catch (e: any) {
      setVerifyError(e?.data?.detail || e?.message || 'Could not verify the code')
      if (e?.status === 403 || e?.status === 409) setSessionEnded(true)
    } finally {
      setVerifying(false)
    }
  }

  const goToSignIn = async () => {
    // Refresh setup status so AuthContext knows setup is complete
    await checkSetupStatus()
    navigate('/login', { replace: true })
  }

  const onComplete = async () => {
    // The verify step proved the password and the authenticator, and signed
    // the operator in; the same code typed again at the login page would be
    // refused as a replay.
    if (signIn) {
      try {
        await loginWithTokens(signIn)
        navigate('/', { replace: true })
        return
      } catch { /* the session did not take; the login page works as before */ }
    }
    await goToSignIn()
  }

  const [showPassword, setShowPassword] = useState(false)
  const [copied, setCopied] = useState(false)

  const copySecret = async () => {
    if (!mfaSecret) return
    try {
      await navigator.clipboard.writeText(mfaSecret)
      setCopied(true)
      window.setTimeout(() => setCopied(false), 1500)
    } catch { /* clipboard blocked on plain http — the code is visible to copy by hand */ }
  }

  const step = (n: number, title: string, hint: string) => (
    <div className="space-y-1">
      <div className="text-xs font-medium uppercase tracking-wider text-[#5eb3f6]">Step {n} of 3</div>
      <h1 className="text-lg font-semibold text-[var(--text)]">{title}</h1>
      <p className="text-sm text-[var(--text-dim)]">{hint}</p>
    </div>
  )

  // Step 3: the authenticator is confirmed; the codes that replace it if the
  // phone is lost are shown this once.
  if (recoveryCodes) {
    return (
      <AuthLayout wide>
        {step(3, 'Save your recovery codes', 'Two-step verification is on. If you lose your phone, one of these codes signs you in instead.')}
        <RecoveryCodesPanel
          codes={recoveryCodes}
          username={username || undefined}
          doneLabel="Continue to OpenNVR"
          onDone={onComplete}
        />
      </AuthLayout>
    )
  }

  // Step 2: the account exists; bind an authenticator to it and prove it works.
  if (mfaSecret && qrDataUrl) {
    return (
      <AuthLayout wide>
        {step(2, 'Turn on two-step verification', 'Your admin account is created. Link an authenticator app and confirm it with a code to finish.')}

        <ol className="space-y-2 text-sm text-[var(--text)]">
          {[
            'Install an authenticator app (Google Authenticator, Microsoft Authenticator, Authy…).',
            'Scan the QR code below with it.',
            'Enter the 6-digit code it shows to confirm.',
          ].map((text, i) => (
            <li key={i} className="flex gap-3">
              <span className="grid h-5 w-5 shrink-0 place-items-center rounded-full bg-[#5eb3f6]/15 text-[11px] font-semibold text-[#5eb3f6]">
                {i + 1}
              </span>
              <span>{text}</span>
            </li>
          ))}
        </ol>

        {/* White behind the code is for the camera, not decoration:
            scanners need the contrast. */}
        <div className="mx-auto w-fit bg-white p-3">
          <img src={qrDataUrl} alt="QR code for your authenticator app" className="h-48 w-48" />
        </div>

        <div className="space-y-1.5">
          <div className={authLabel}>Can’t scan? Enter this code instead</div>
          <div className="flex items-center gap-2 border border-[#2a3a4f] bg-[#0f1720] py-1.5 pl-3 pr-1.5">
            <code className="min-w-0 flex-1 break-all font-mono text-sm text-[var(--text)]">{mfaSecret}</code>
            <button
              type="button"
              onClick={copySecret}
              className="inline-flex shrink-0 items-center gap-1 px-2 py-1 text-xs text-[var(--text-dim)] hover:bg-white/5 hover:text-[var(--text)]"
            >
              {copied ? <Check size={14} className="text-[var(--ok)]" /> : <Copy size={14} />}
              {copied ? 'Copied' : 'Copy'}
            </button>
          </div>
        </div>

        {sessionEnded ? (
          <div className="space-y-3">
            <AuthAlert tone="error" icon={<AlertCircle size={16} />}>{verifyError}</AuthAlert>
            <button type="button" onClick={goToSignIn} className={authButton}>
              Continue to sign in <ArrowRight size={16} />
            </button>
          </div>
        ) : (
          <form onSubmit={onVerify} className="space-y-3">
            {verifyError && <AuthAlert tone="error" icon={<AlertCircle size={16} />}>{verifyError}</AuthAlert>}

            <div className="space-y-1.5">
              <label htmlFor="setup-code" className={authLabel}>Code from the app</label>
              <div className="relative">
                <ShieldCheck size={16} className={authFieldIcon} />
                <input
                  id="setup-code"
                  type="text"
                  inputMode="numeric"
                  autoComplete="one-time-code"
                  className={`${authInput} font-mono tracking-widest`}
                  value={code}
                  onChange={(e) => setCode(e.target.value)}
                  placeholder="123456"
                  minLength={6}
                  maxLength={8}
                  autoFocus
                  required
                />
              </div>
              <p className="text-xs text-[var(--text-dim)]">
                If you close this page first, sign in with your password; you will be asked to finish there.
              </p>
            </div>

            <button type="submit" disabled={verifying || code.replace(/\s+/g, '').length < 6} className={authButton}>
              {verifying && <Loader2 size={16} className="animate-spin" />}
              {verifying ? 'Checking…' : 'Verify and finish'}
            </button>
          </form>
        )}
      </AuthLayout>
    )
  }

  // Step 1: claim the admin account with the token the server printed.
  return (
    <AuthLayout wide>
      <form onSubmit={onSubmit} className="space-y-5">
        {step(1, 'Create the admin account', 'Welcome to OpenNVR. Secure this system before anyone else can claim it.')}

        {error && <AuthAlert tone="error" icon={<AlertCircle size={16} />}>{error}</AuthAlert>}

        <div className="space-y-1.5">
          <label htmlFor="setup-username" className={authLabel}>Username</label>
          <div className="relative">
            <UserRound size={16} className={authFieldIcon} />
            <input id="setup-username" className={authInput} value={username} placeholder="Loading…" disabled readOnly />
          </div>
          <p className="text-xs text-[var(--text-dim)]">
            Set by DEFAULT_ADMIN_USERNAME in your .env file.
          </p>
        </div>

        <div className="space-y-1.5">
          <label htmlFor="setup-token" className={authLabel}>Setup token</label>
          <div className="relative">
            <KeyRound size={16} className={authFieldIcon} />
            <input
              id="setup-token"
              type="text"
              className={`${authInput} font-mono`}
              value={setupToken}
              onChange={(e) => setSetupToken(e.target.value)}
              placeholder="Paste the one-time token"
              autoComplete="off"
              autoCorrect="off"
              spellCheck={false}
              autoFocus
              required
            />
          </div>
          <p className="text-xs text-[var(--text-dim)]">
            Shown once in the terminal where you ran the start script.
          </p>
        </div>

        <div className="space-y-1.5">
          <label htmlFor="setup-password" className={authLabel}>Password</label>
          <div className="relative">
            <Lock size={16} className={authFieldIcon} />
            <input
              id="setup-password"
              type={showPassword ? 'text' : 'password'}
              className={`${authInput} pr-11`}
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              placeholder="At least 8 characters"
              autoComplete="new-password"
              required
              minLength={8}
            />
            <button
              type="button"
              onClick={() => setShowPassword((v) => !v)}
              className="absolute right-1.5 top-1/2 grid h-8 w-8 -translate-y-1/2 place-items-center text-[var(--text-dim)] hover:bg-white/5 hover:text-[var(--text)]"
              aria-label={showPassword ? 'Hide password' : 'Show password'}
              title={showPassword ? 'Hide password' : 'Show password'}
            >
              {showPassword ? <EyeOff size={16} /> : <Eye size={16} />}
            </button>
          </div>
        </div>

        <div className="space-y-1.5">
          <label htmlFor="setup-confirm" className={authLabel}>Confirm password</label>
          <div className="relative">
            <Lock size={16} className={authFieldIcon} />
            <input
              id="setup-confirm"
              type={showPassword ? 'text' : 'password'}
              className={authInput}
              value={confirmPassword}
              onChange={(e) => setConfirmPassword(e.target.value)}
              placeholder="Re-enter the password"
              autoComplete="new-password"
              required
              minLength={8}
            />
          </div>
        </div>

        <button type="submit" disabled={loading} className={authButton}>
          {loading && <Loader2 size={16} className="animate-spin" />}
          {loading ? 'Setting up…' : 'Create account'}
        </button>
      </form>
    </AuthLayout>
  )
}
