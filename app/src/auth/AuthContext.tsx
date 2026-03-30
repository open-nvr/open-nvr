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

import React, { createContext, useCallback, useContext, useEffect, useMemo, useState } from 'react'
import { api, setAuthToken as apiSetToken } from '../lib/api'
import { apiService } from '../lib/apiService'

type User = {
  id: number
  username: string
  email: string
  role_id: number
  is_superuser: boolean
  mfa_enabled: boolean
}

type AuthState = {
  user: User | null
  token: string | null
  loading: boolean
  error: string | null
  setupRequired: boolean
}

type AuthContextType = AuthState & {
  login: (username: string, password: string, code?: string) => Promise<void>
  logout: () => void
  register: (username: string, email: string, password: string) => Promise<void>
  refreshUser: () => Promise<void>
  checkSetupStatus: () => Promise<boolean>
}

const AuthContext = createContext<AuthContextType | undefined>(undefined)

const TOKEN_KEY = 'opennvr.token'

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const [state, setState] = useState<AuthState>({ user: null, token: null, loading: true, error: null, setupRequired: false })

  // Check for first-time setup requirement on mount
  useEffect(() => {
    const checkSetup = async () => {
      try {
        const { data } = await apiService.checkSetup()
        if (data.setup_required) {
          setState((s) => ({ ...s, setupRequired: true, loading: false }))
          return
        }
      } catch (e) {
        // If check fails, continue with normal auth flow
      }

      // Load token from storage
      const saved = localStorage.getItem(TOKEN_KEY)
      if (saved) {
        apiSetToken(saved)
        setState((s) => ({ ...s, token: saved }))
        apiService.me().then(({ data }) => setState((s) => ({ ...s, user: data, loading: false }))).catch(() => setState((s) => ({ ...s, loading: false })))
      } else {
        setState((s) => ({ ...s, loading: false }))
      }
    }
    checkSetup()
  }, [])

  const setToken = useCallback((token: string | null) => {
    apiSetToken(token)
    if (token) localStorage.setItem(TOKEN_KEY, token)
    else localStorage.removeItem(TOKEN_KEY)
    setState((s) => ({ ...s, token }))
  }, [])

  const login = useCallback(async (username: string, password: string, code?: string) => {
    setState((s) => ({ ...s, loading: true, error: null }))
    try {
      // Prefer JSON login to support MFA
      const { data } = await apiService.loginJson(username, password, code)
      const token = data.access_token
      setToken(token)
      const me = await apiService.me()
      setState({ user: me.data, token, loading: false, error: null, setupRequired: false })
    } catch (e: any) {
      const detail = e?.data?.detail || ''
      
      // Check if first-time setup is required
      if (e?.status === 403 && e?.headers?.['x-setup-required'] === 'true') {
        setState((s) => ({ ...s, loading: false, error: null, setupRequired: true }))
        const setupErr: any = new Error('First-time setup required')
        setupErr.setupRequired = true
        throw setupErr
      }
      
      // If the backend indicates MFA is required/missing, surface a structured error
      if (e?.status === 401 && (String(detail).toLowerCase().includes('mfa') || String(detail).toLowerCase().includes('two'))) {
        setState((s) => ({ ...s, loading: false, error: null }))
        const mfaErr: any = new Error('MFA required')
        mfaErr.mfaRequired = true
        throw mfaErr
      }
      const message = detail || e?.message || 'Login failed'
      setState((s) => ({ ...s, loading: false, error: message }))
      throw e
    }
  }, [setToken])

  const register = useCallback(async (username: string, email: string, password: string) => {
    setState((s) => ({ ...s, loading: true, error: null }))
    try {
      await apiService.register(username, email, password)
      // Auto-login after registration
      await login(username, password)
    } catch (e: any) {
      const message = e?.data?.detail || e?.message || 'Registration failed'
      setState((s) => ({ ...s, loading: false, error: message }))
    }
  }, [login])

  const logout = useCallback(() => {
    setToken(null)
    setState({ user: null, token: null, loading: false, error: null, setupRequired: false })
  }, [setToken])

  const refreshUser = useCallback(async () => {
    if (!state.token) return
    try {
      const me = await apiService.me()
      setState((s) => ({ ...s, user: me.data }))
    } catch (e) {
      // token invalid
      logout()
    }
  }, [state.token, logout])

  const checkSetupStatus = useCallback(async () => {
    try {
      const { data } = await apiService.checkSetup()
      setState((s) => ({ ...s, setupRequired: data.setup_required }))
      return data.setup_required
    } catch (e) {
      return false
    }
  }, [])

  const value = useMemo<AuthContextType>(() => ({ ...state, login, logout, register, refreshUser, checkSetupStatus }), [state, login, logout, register, refreshUser, checkSetupStatus])

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
}

export function useAuth() {
  const ctx = useContext(AuthContext)
  if (!ctx) throw new Error('useAuth must be used within AuthProvider')
  return ctx
}
