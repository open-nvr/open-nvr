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

import { useState, useEffect } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { useAuth } from '../auth/AuthContext'

export function Login() {
  const { login, loading, error, setupRequired } = useAuth()
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [msg, setMsg] = useState<string | null>(null)
  const navigate = useNavigate()

  // Redirect to setup if required
  useEffect(() => {
    if (setupRequired) {
      navigate('/first-time-setup', { replace: true })
    }
  }, [setupRequired, navigate])

  const onSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    setMsg(null)
    try {
      await login(username, password)
      navigate('/')
    } catch (e: any) {
      if (e?.setupRequired) {
        navigate('/first-time-setup', { replace: true })
        return
      }
      if (e?.mfaRequired) {
        navigate('/mfa-verify', { state: { username, password } })
        return
      }
      // other errors handled in context
    }
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
        {setupRequired && (
          <div className="text-sm text-orange-300 bg-orange-900/30 border border-orange-500/30 rounded p-3">
            First-time setup required. Please complete setup before logging in.
          </div>
        )}
        {error && <div className="text-sm text-red-300 bg-red-900/30 border border-red-500/30 rounded p-2">{error}</div>}
        {msg && <div className="text-sm text-emerald-300 bg-emerald-900/30 border border-emerald-500/30 rounded p-2">{msg}</div>}
        <label className="block text-sm text-gray-200">
          <span className="block mb-2 text-gray-400 font-medium">Username</span>
          <input className="w-full bg-[#0f1720] border border-[#2a3a4f] focus:border-[#5eb3f6] outline-none px-4 py-2.5 rounded text-gray-100 placeholder-gray-500" value={username} onChange={(e) => setUsername(e.target.value)} placeholder="admin" required />
        </label>
        <label className="block text-sm text-gray-200">
          <span className="block mb-2 text-gray-400 font-medium">Password</span>
          <input type="password" className="w-full bg-[#0f1720] border border-[#2a3a4f] focus:border-[#5eb3f6] outline-none px-4 py-2.5 rounded text-gray-100 placeholder-gray-500" value={password} onChange={(e) => setPassword(e.target.value)} placeholder="●●●●●●●●" required />
        </label>
        {/* MFA input moved to dedicated page */}
        <button disabled={loading} className="w-full px-3 py-2 rounded bg-[var(--accent)]/90 text-white disabled:opacity-60 shadow-md hover:bg-[var(--accent)]">
          {loading ? 'Signing in…' : 'Sign in'}
        </button>
        <div className="flex items-center justify-between text-xs text-gray-500">
          {/* <span>Tip: default admin is admin / admin123</span> */}
          {/* <Link to="/register" className="underline">Register</Link> */}
        </div>
      </form>
      </div>
    </div>
  )
}
