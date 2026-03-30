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
import { Link, useNavigate } from 'react-router-dom'
import { useAuth } from '../auth/AuthContext'

export function Register() {
  const { register, loading, error } = useAuth()
  const [username, setUsername] = useState('')
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [confirm, setConfirm] = useState('')
  const [msg, setMsg] = useState<string | null>(null)
  const navigate = useNavigate()

  const onSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    setMsg(null)
    if (password !== confirm) {
      setMsg('Passwords do not match')
      return
    }
    try {
      await register(username, email, password)
      navigate('/')
    } catch (_) {
      // handled in context
    }
  }

  return (
    <div className="min-h-screen grid place-items-center bg-[var(--bg)] text-[var(--text)] p-4">
      <form onSubmit={onSubmit} className="w-full max-w-sm bg-[var(--panel)] border border-neutral-700 p-4 space-y-3">
        <h1 className="text-lg font-semibold">Create account</h1>
        {error && <div className="text-sm text-red-400">{error}</div>}
        {msg && <div className="text-sm text-amber-400">{msg}</div>}
        <label className="block text-sm">
          <span className="block mb-1">Username</span>
          <input className="w-full bg-[var(--panel-2)] border border-neutral-700 px-2 py-1" value={username} onChange={(e) => setUsername(e.target.value)} required minLength={3} />
        </label>
        <label className="block text-sm">
          <span className="block mb-1">Email</span>
          <input type="email" className="w-full bg-[var(--panel-2)] border border-neutral-700 px-2 py-1" value={email} onChange={(e) => setEmail(e.target.value)} required />
        </label>
        <label className="block text-sm">
          <span className="block mb-1">Password</span>
          <input type="password" className="w-full bg-[var(--panel-2)] border border-neutral-700 px-2 py-1" value={password} onChange={(e) => setPassword(e.target.value)} required minLength={8} />
        </label>
        <label className="block text-sm">
          <span className="block mb-1">Confirm password</span>
          <input type="password" className="w-full bg-[var(--panel-2)] border border-neutral-700 px-2 py-1" value={confirm} onChange={(e) => setConfirm(e.target.value)} required minLength={8} />
        </label>
        <button disabled={loading} className="w-full px-3 py-2 bg-[var(--accent)]/90 text-white disabled:opacity-60">
          {loading ? 'Creating…' : 'Create account'}
        </button>
        <div className="text-xs text-[var(--text-dim)] text-center">
          Already have an account? <Link to="/login" className="underline">Sign in</Link>
        </div>
      </form>
    </div>
  )
}
