// Copyright (c) 2026 OpenNVR
// SPDX-License-Identifier: AGPL-3.0-or-later
//
// Who is signed in, and what OpenNVR lets them do here. The permissions
// only decide which buttons are shown: core checks every request anyway.

import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from 'react'
import { ApiError, SIGNED_OUT_EVENT, errorText, hasSession, signOut } from './api'
import { api } from './attendance'

type Me = { username: string; email?: string | null; first_name?: string | null }

type Auth = {
  status: 'loading' | 'signed-out' | 'signed-in' | 'error'
  me: Me | null
  error: string | null
  deviceBlocked: boolean
  can: { view: boolean; manage: boolean; enroll: boolean }
  refresh: () => void
  signOut: () => void
}

const Ctx = createContext<Auth | null>(null)

export function AuthProvider({ children }: { children: ReactNode }) {
  const [status, setStatus] = useState<Auth['status']>(hasSession() ? 'loading' : 'signed-out')
  const [me, setMe] = useState<Me | null>(null)
  const [perms, setPerms] = useState<Set<string>>(new Set())
  const [error, setError] = useState<string | null>(null)
  const [deviceBlocked, setDeviceBlocked] = useState(false)

  const refresh = useCallback(() => {
    if (!hasSession()) {
      setStatus('signed-out')
      return
    }
    setStatus('loading')
    Promise.all([api.me(), api.permissions()])
      .then(([who, p]) => {
        setMe(who)
        setPerms(new Set(p.permissions))
        setDeviceBlocked(false)
        setError(null)
        setStatus('signed-in')
      })
      .catch((e) => {
        if (e instanceof ApiError && e.status === 401) return setStatus('signed-out')
        const blocked = e instanceof ApiError && (e.body as { code?: string } | null)?.code === 'device_not_approved'
        setDeviceBlocked(blocked)
        setError(errorText(e))
        setStatus('error')
      })
  }, [])

  useEffect(() => {
    refresh()
    const out = () => {
      setMe(null)
      setStatus('signed-out')
    }
    window.addEventListener(SIGNED_OUT_EVENT, out)
    return () => window.removeEventListener(SIGNED_OUT_EVENT, out)
  }, [refresh])

  const value = useMemo<Auth>(
    () => ({
      status,
      me,
      error,
      deviceBlocked,
      can: {
        view: perms.has('attendance.view'),
        manage: perms.has('attendance.manage'),
        enroll: perms.has('attendance.enroll'),
      },
      refresh,
      signOut,
    }),
    [status, me, error, deviceBlocked, perms, refresh],
  )
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>
}

export function useAuth(): Auth {
  const v = useContext(Ctx)
  if (!v) throw new Error('useAuth outside AuthProvider')
  return v
}
