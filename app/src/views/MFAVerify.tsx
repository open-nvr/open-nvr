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
import { Link, useLocation, useNavigate } from 'react-router-dom'
import { useAuth } from '../auth/AuthContext'

type LocationState = {
  username?: string
  password?: string
}

export function MFAVerify() {
  const { state } = useLocation()
  const navigate = useNavigate()
  const { login, loading, error } = useAuth()
  const { username, password } = (state || {}) as LocationState

  const [code, setCode] = useState('')

  const onSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!username || !password) return
    try {
      await login(username, password, code)
      navigate('/')
    } catch (_) {
      // errors handled in context
    }
  }

  if (!username || !password) {
    return (
      <div className="min-h-screen grid place-items-center bg-[var(--bg)] text-[var(--text)] p-4">
        <div className="w-full max-w-sm bg-[var(--panel)] border border-neutral-700 p-4 space-y-3 text-sm">
          <div className="text-red-400">Missing credentials. Please sign in again.</div>
          <Link to="/login" className="underline">Back to sign in</Link>
        </div>
      </div>
    )
  }

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
        
        <form onSubmit={onSubmit} className="w-full max-w-sm rounded-lg bg-[#1a2332] border border-[#2a3a4f] shadow-2xl p-6 space-y-4">
        <h1 className="text-lg font-semibold tracking-wide text-gray-100">Two‑factor verification</h1>
        <div className="text-xs text-gray-400">Enter the 6‑digit code from your authenticator app.</div>
        {error && <div className="text-sm text-red-300 bg-red-900/30 border border-red-500/30 rounded p-2">{error}</div>}
        <label className="block text-sm text-gray-200">
          <span className="block mb-2 text-gray-400 font-medium">MFA Code</span>
          <input className="w-full bg-[#0f1720] border border-[#2a3a4f] focus:border-[#5eb3f6] outline-none px-4 py-2.5 rounded text-gray-100 placeholder-gray-500 text-center tracking-widest text-lg" value={code} onChange={(e) => setCode(e.target.value)} minLength={6} maxLength={8} placeholder="000000" required />
        </label>
        <button disabled={loading} className="w-full px-4 py-3 rounded-lg bg-[#5eb3f6] text-white font-semibold disabled:opacity-60 shadow-lg hover:bg-[#4a9de5] transition-colors">
          {loading ? 'Verifying…' : 'Verify'}
        </button>
        <div className="text-xs text-gray-400 text-center">
          <Link to="/login" className="text-[#5eb3f6] hover:text-[#4a9de5] underline">Use a different account</Link>
        </div>
      </form>
      </div>
    </div>
  )
}


