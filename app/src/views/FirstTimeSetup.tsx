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

export function FirstTimeSetup() {
  const [username, setUsername] = useState('admin')
  const [password, setPassword] = useState('')
  const [confirmPassword, setConfirmPassword] = useState('')
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
      const { data } = await apiService.firstTimeSetup(username, password)
      
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

  // If MFA setup is shown, display QR code
  if (mfaSecret && qrDataUrl) {
    return (
      <div
        className="min-h-screen grid place-items-center bg-[var(--bg)] text-[var(--text)] p-4"
        style={{
          backgroundImage: 'linear-gradient(rgba(0,0,0,0.45), rgba(0,0,0,0.45)), url(/opennvr_bg.svg)',
          backgroundSize: 'cover',
          backgroundPosition: 'center',
        }}
      >
        <div className="flex flex-col items-center gap-8">
          {/* Logo outside the form - 35% of viewport */}
          <img src="/opennvr-logo.svg" alt="OpenNVR" className="w-[35vw] h-auto" style={{ minWidth: '280px', maxWidth: '500px' }} />
          
          <div className="w-full max-w-md rounded-lg bg-[#1a2332] border border-[#2a3a4f] shadow-2xl p-6 space-y-4">
          
          <h2 className="text-xl font-semibold text-gray-100 text-center">MFA Setup Required</h2>
          
          <div className="bg-blue-900/30 border border-blue-500/30 rounded p-3 text-sm text-blue-200">
            <p className="font-semibold mb-2">Setup Complete! Now configure MFA:</p>
            <ol className="list-decimal list-inside space-y-1">
              <li>Install an authenticator app (Google Authenticator, Authy, etc.)</li>
              <li>Scan the QR code below</li>
              <li>Save your recovery codes (if prompted)</li>
              <li>Click "Continue" to login</li>
            </ol>
          </div>

          <div className="flex justify-center bg-white p-4 rounded border border-gray-300">
            <img src={qrDataUrl} alt="MFA QR Code" className="w-48 h-48" />
          </div>

          <div className="bg-gray-50 border border-gray-300 rounded p-3">
            <p className="text-xs text-gray-600 mb-1">Manual Entry Code:</p>
            <code className="text-sm font-mono text-gray-900 break-all">{mfaSecret}</code>
          </div>

          <button
            onClick={onComplete}
            className="w-full px-4 py-3 rounded-lg bg-[#5eb3f6] text-white font-semibold shadow-lg hover:bg-[#4a9de5] transition-colors"
          >
            Continue to Login
          </button>
        </div>
        </div>
      </div>
    )
  }

  // Show password setup form
  return (
    <div
      className="min-h-screen grid place-items-center bg-[var(--bg)] text-[var(--text)] p-4"
      style={{
        backgroundImage: 'linear-gradient(rgba(0,0,0,0.45), rgba(0,0,0,0.45)), url(/opennvr_bg.svg)',
        backgroundSize: 'cover',
        backgroundPosition: 'center',
      }}
    >
      <div className="flex flex-col items-center gap-8">
        {/* Logo outside the form - 35% of viewport */}
        <img src="/opennvr-logo.svg" alt="OpenNVR" className="w-[35vw] h-auto" style={{ minWidth: '280px', maxWidth: '500px' }} />
        
        <form onSubmit={onSubmit} className="w-full max-w-md rounded-lg bg-[#1a2332] border border-[#2a3a4f] shadow-2xl p-6 space-y-4">
        
        <div className="bg-orange-900/30 border border-orange-500/30 rounded p-3">
          <p className="text-sm text-orange-200 font-semibold">First-time setup required</p>
          <p className="text-xs text-orange-300 mt-1">
            Please set a secure password for the admin account.
          </p>
        </div>

        {error && <div className="text-sm text-red-300 bg-red-900/30 border border-red-500/30 rounded p-3">{error}</div>}

        <label className="block text-sm text-gray-200">
          <span className="block mb-2 text-gray-400 font-medium">Username</span>
          <input
            className="w-full bg-[#0f1720]/50 border border-[#2a3a4f] px-4 py-2.5 rounded text-gray-400 cursor-not-allowed"
            value={username}
            disabled
            readOnly
          />
        </label>

        <label className="block text-sm text-gray-200">
          <span className="block mb-2 text-gray-400 font-medium">Password</span>
          <input
            type="password"
            className="w-full bg-[#0f1720] border border-[#2a3a4f] focus:border-[#5eb3f6] outline-none px-4 py-2.5 rounded text-gray-100 placeholder-gray-500"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            placeholder="Minimum 8 characters"
            required
            minLength={8}
          />
        </label>

        <label className="block text-sm text-gray-200">
          <span className="block mb-2 text-gray-400 font-medium">Confirm Password</span>
          <input
            type="password"
            className="w-full bg-[#0f1720] border border-[#2a3a4f] focus:border-[#5eb3f6] outline-none px-4 py-2.5 rounded text-gray-100 placeholder-gray-500"
            value={confirmPassword}
            onChange={(e) => setConfirmPassword(e.target.value)}
            placeholder="Re-enter password"
            required
            minLength={8}
          />
        </label>

        <button
          disabled={loading}
          className="w-full px-4 py-3 rounded-lg bg-[#5eb3f6] text-white font-semibold disabled:opacity-60 shadow-lg hover:bg-[#4a9de5] transition-colors"
        >
          {loading ? 'Setting up…' : 'Complete Setup'}
        </button>
      </form>
      </div>
    </div>
  )
}
