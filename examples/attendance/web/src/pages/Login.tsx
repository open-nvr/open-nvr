// Copyright (c) 2026 OpenNVR
// SPDX-License-Identifier: AGPL-3.0-or-later
//
// attendly's sign-in card, signing in to OpenNVR: the same account, the
// same MFA code, the same lockout. Nothing here keeps a password.

import { useState, type FormEvent } from 'react'
import { login } from '../api'
import { useAuth } from '../auth'
import { Logo } from '../components/Layout'
import { Spinner } from '../components/ui'

export default function Login() {
  const { refresh } = useAuth()
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [code, setCode] = useState('')
  const [needsCode, setNeedsCode] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  const submit = async (e: FormEvent) => {
    e.preventDefault()
    setBusy(true)
    setError(null)
    const r = await login(username.trim(), password, needsCode ? code.trim() : undefined)
    setBusy(false)
    if (r.ok) return refresh()
    if (r.needsCode && !needsCode) {
      setNeedsCode(true)
      setError('Enter the 6-digit code from your authenticator app.')
      return
    }
    setError(r.setup ? 'This account has not been set up yet. Finish its setup in OpenNVR first.' : r.message)
  }

  const input =
    'relative block w-full appearance-none border border-slate-300 bg-white px-3 py-2 text-slate-900 placeholder-slate-400 focus:z-10 focus:border-indigo-300 focus:outline-none focus:ring-2 focus:ring-indigo-200 sm:text-sm'

  return (
    <div className="relative min-h-dvh overflow-hidden bg-gradient-to-br from-indigo-950 via-slate-900 to-indigo-800">
      <div
        aria-hidden
        className="absolute inset-0 opacity-30"
        style={{
          backgroundImage:
            'radial-gradient(circle at 20% 30%, rgba(129,140,248,.55), transparent 40%), radial-gradient(circle at 75% 70%, rgba(56,189,248,.35), transparent 45%)',
        }}
      />
      <div className="relative z-10 flex min-h-dvh items-center justify-center px-4 py-12 md:justify-end md:pr-16 lg:pr-28">
        <div className="hidden max-w-md flex-1 pr-12 text-white md:block">
          <h2 className="text-4xl font-extrabold leading-tight">Attendance from the cameras you already have.</h2>
          <p className="mt-4 text-indigo-100">
            Enrol people once. Each scheduled class or shift, OpenNVR recognises who is in the room and marks them
            present, late or absent.
          </p>
        </div>
        <div className="w-full max-w-md">
          <div className="rounded-2xl border border-slate-200/70 bg-white/90 p-8 shadow-2xl backdrop-blur-xl">
            <div className="text-center">
              <div className="mb-2 flex items-center justify-center gap-3">
                <Logo size="h-10 w-10" />
                <h1 className="text-3xl font-extrabold text-slate-900">Attendance</h1>
              </div>
              <p className="text-slate-600">Sign in with your OpenNVR account</p>
            </div>
            <form className="mt-6 space-y-5" onSubmit={submit}>
              <div className="-space-y-px rounded-md shadow-sm">
                <label htmlFor="username" className="sr-only">
                  Username
                </label>
                <input
                  id="username"
                  autoComplete="username"
                  required
                  className={`${input} rounded-t-md`}
                  placeholder="Username"
                  value={username}
                  onChange={(e) => setUsername(e.target.value)}
                  disabled={busy}
                />
                <label htmlFor="password" className="sr-only">
                  Password
                </label>
                <input
                  id="password"
                  type="password"
                  autoComplete="current-password"
                  required
                  className={`${input} ${needsCode ? '' : 'rounded-b-md'}`}
                  placeholder="Password"
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  disabled={busy}
                />
                {needsCode && (
                  <>
                    <label htmlFor="code" className="sr-only">
                      Authenticator code
                    </label>
                    <input
                      id="code"
                      inputMode="numeric"
                      autoComplete="one-time-code"
                      maxLength={6}
                      required
                      autoFocus
                      className={`${input} rounded-b-md tracking-widest`}
                      placeholder="6-digit code"
                      value={code}
                      onChange={(e) => setCode(e.target.value.replace(/\D/g, ''))}
                      disabled={busy}
                    />
                  </>
                )}
              </div>
              {error && (
                <p role="alert" className="text-center text-sm text-red-600">
                  {error}
                </p>
              )}
              <button
                type="submit"
                disabled={busy}
                className="flex w-full justify-center gap-2 rounded-md border border-transparent bg-indigo-600 px-4 py-2 text-sm font-medium text-white hover:bg-indigo-700 focus:outline-none focus:ring-2 focus:ring-indigo-300 disabled:opacity-50"
              >
                {busy && <Spinner className="h-4 w-4" />}
                {busy ? 'Signing in…' : 'Sign in'}
              </button>
            </form>
          </div>
          <p className="mt-4 text-center text-xs text-indigo-100/80">Part of OpenNVR · Your faces stay on your own server</p>
        </div>
      </div>
    </div>
  )
}
