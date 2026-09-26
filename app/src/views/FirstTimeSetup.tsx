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

import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { toDataURL } from 'qrcode'
import { apiService } from '../lib/apiService'
import { useAuth } from '../auth/AuthContext'
import {
  AlertCircle, ArrowRight, Check, Copy, Eye, EyeOff, KeyRound, Loader2, Lock, UserRound,
} from 'lucide-react'
import { AuthAlert, AuthLayout, authButton, authFieldIcon, authInput, authLabel } from '../components/AuthLayout'

export function FirstTimeSetup() {
  const [username, setUsername] = useState('admin')
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
  const [mfaQrUri, setMfaQrUri] = useState<string | null>(null)
  const [qrDataUrl, setQrDataUrl] = useState<string>('')
  const navigate = useNavigate()
  const { checkSetupStatus } = useAuth()

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
      setMfaQrUri(data.mfa_qr_uri)
      
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

  const onComplete = async () => {
    // Refresh setup status so AuthContext knows setup is complete
    await checkSetupStatus()
    navigate('/login', { replace: true })
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
      <div className="text-xs font-medium uppercase tracking-wider text-[#5eb3f6]">Step {n} of 2</div>
      <h1 className="text-lg font-semibold text-gray-100">{title}</h1>
      <p className="text-sm text-gray-400">{hint}</p>
    </div>
  )

  // Step 2: the account exists; bind an authenticator to it.
  if (mfaSecret && qrDataUrl) {
    return (
      <AuthLayout wide>
        {step(2, 'Turn on two-step verification', 'Your admin account is created. Link an authenticator app to finish.')}

        <ol className="space-y-2 text-sm text-gray-300">
          {[
            'Install an authenticator app (Google Authenticator, Microsoft Authenticator, Authy…).',
            'Scan the QR code below with it.',
            'Sign in with your new password and the 6-digit code it shows.',
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
        <div className="mx-auto w-fit rounded-lg bg-white p-3">
          <img src={qrDataUrl} alt="QR code for your authenticator app" className="h-48 w-48" />
        </div>

        <div className="space-y-1.5">
          <div className={authLabel}>Can’t scan? Enter this code instead</div>
          <div className="flex items-center gap-2 rounded-lg border border-[#2a3a4f] bg-[#0f1720] py-1.5 pl-3 pr-1.5">
            <code className="min-w-0 flex-1 break-all font-mono text-sm text-gray-100">{mfaSecret}</code>
            <button
              type="button"
              onClick={copySecret}
              className="inline-flex shrink-0 items-center gap-1 rounded-md px-2 py-1 text-xs text-gray-400 hover:bg-white/5 hover:text-gray-100"
            >
              {copied ? <Check size={14} className="text-emerald-400" /> : <Copy size={14} />}
              {copied ? 'Copied' : 'Copy'}
            </button>
          </div>
        </div>

        <button type="button" onClick={onComplete} className={authButton}>
          Continue to sign in <ArrowRight size={16} />
        </button>
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
            <input id="setup-username" className={authInput} value={username} disabled readOnly />
          </div>
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
          <p className="text-xs text-gray-500">
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
              className="absolute right-1.5 top-1/2 grid h-8 w-8 -translate-y-1/2 place-items-center rounded-md text-gray-500 hover:bg-white/5 hover:text-gray-200"
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
