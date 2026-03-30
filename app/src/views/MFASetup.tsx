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
import { toDataURL } from 'qrcode'
import { apiService } from '../lib/apiService'
import { useAuth } from '../auth/AuthContext'

export function MFASetup() {
  const { user, refreshUser } = useAuth()
  const [otpauthUrl, setOtpauthUrl] = useState<string>('')
  const [secret, setSecret] = useState<string>('')
  const [code, setCode] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const canvasRef = useRef<HTMLCanvasElement>(null)
  const [qrDataUrl, setQrDataUrl] = useState<string>('')
  const navigate = useNavigate()

  useEffect(() => {
    let mounted = true
    // If user already has MFA enabled, go home
    if (user?.mfa_enabled) {
      navigate('/')
      return
    }
    ;(async () => {
      try {
        setLoading(true)
        const { data } = await apiService.mfaSetup('json')
        if (!mounted) return
        setOtpauthUrl(data.otpauth_url)
        setSecret(data.secret)
        if (data.otpauth_url) {
          const url = await toDataURL(data.otpauth_url, { width: 220, margin: 2, color: { dark: '#000000', light: '#ffffff' } })
          if (!mounted) return
          setQrDataUrl(url)
        } else {
          setError('Missing otpauth URL from server')
        }
      } catch (e: any) {
        setError(e?.data?.detail || e?.message || 'Failed to start MFA setup')
      } finally {
        setLoading(false)
      }
    })()
    return () => { mounted = false }
  }, [user, navigate])

  const onVerify = async (e: React.FormEvent) => {
    e.preventDefault()
    setError(null)
    try {
      await apiService.mfaVerify(code)
      await refreshUser()
      navigate('/')
    } catch (e: any) {
      setError(e?.data?.detail || e?.message || 'Invalid code')
    }
  }

  return (
    <div className="min-h-screen grid place-items-center bg-[var(--bg)] text-[var(--text)] p-4">
      <div className="w-full max-w-lg bg-[var(--panel)] border border-neutral-700 p-4">
        <h1 className="text-lg font-semibold mb-1">Set up Multi‑Factor Authentication</h1>
        <p className="text-sm text-[var(--text-dim)] mb-3">Scan the QR code with your authenticator app (Google Authenticator, Authy, etc.), then enter the 6‑digit code to verify.</p>
        {loading ? (
          <div className="text-sm">Loading…</div>
        ) : (
          <div className="grid grid-cols-1 md:grid-cols-2 gap-4 items-start">
            <div className="flex flex-col items-center gap-2">
              {qrDataUrl ? (
                <img src={qrDataUrl} alt="MFA QR code" className="bg-white p-2" width={220} height={220} />
              ) : (
                <div className="w-[220px] h-[220px] grid place-items-center bg-white text-black text-xs">Generating QR…</div>
              )}
              <div className="text-xs text-[var(--text-dim)] break-all">
                Secret: <span className="text-[var(--text)]">{secret}</span>
              </div>
            </div>
            <form onSubmit={onVerify} className="space-y-2">
              {error && <div className="text-sm text-red-400">{error}</div>}
              <label className="block text-sm">
                <span className="block mb-1">Authenticator Code</span>
                <input className="w-full bg-[var(--panel-2)] border border-neutral-700 px-2 py-1" value={code} onChange={(e) => setCode(e.target.value)} minLength={6} maxLength={8} required />
              </label>
              <button className="px-3 py-2 bg-[var(--accent)]/90 text-white">Verify and continue</button>
              <p className="text-[10px] text-[var(--text-dim)]">If you lose access to your MFA device, contact an administrator.</p>
            </form>
          </div>
        )}
      </div>
    </div>
  )
}
