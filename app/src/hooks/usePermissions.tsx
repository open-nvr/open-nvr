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

import { createContext, useCallback, useContext, useEffect, useMemo, useState } from 'react'
import { apiService } from '../lib/apiService'
import { useAuth } from '../auth/AuthContext'

type PermissionsState = {
  permissions: string[]
  isSuperuser: boolean
  loading: boolean
  error: string | null
}

type PermissionsContextType = PermissionsState & {
  hasPermission: (permission: string) => boolean
  hasAnyPermission: (...permissions: string[]) => boolean
  hasAllPermissions: (...permissions: string[]) => boolean
  refresh: () => Promise<void>
}

const PermissionsContext = createContext<PermissionsContextType | undefined>(undefined)

export function PermissionsProvider({ children }: { children: React.ReactNode }) {
  const { token, user } = useAuth()
  const [state, setState] = useState<PermissionsState>({
    permissions: [],
    isSuperuser: false,
    loading: true,
    error: null,
  })

  const fetchPermissions = useCallback(async () => {
    if (!token) {
      setState({ permissions: [], isSuperuser: false, loading: false, error: null })
      return
    }
    try {
      setState((s) => ({ ...s, loading: true, error: null }))
      const { data } = await apiService.myPermissions()
      setState({
        permissions: data.permissions || [],
        isSuperuser: data.is_superuser || false,
        loading: false,
        error: null,
      })
    } catch (e: any) {
      setState((s) => ({
        ...s,
        loading: false,
        error: e?.message || 'Failed to load permissions',
      }))
    }
  }, [token])

  useEffect(() => {
    fetchPermissions()
  }, [fetchPermissions, user?.id])

  const hasPermission = useCallback(
    (permission: string) => {
      if (state.isSuperuser) return true
      if (state.permissions.includes('full_access')) return true
      return state.permissions.includes(permission)
    },
    [state.permissions, state.isSuperuser]
  )

  const hasAnyPermission = useCallback(
    (...permissions: string[]) => {
      if (state.isSuperuser) return true
      if (state.permissions.includes('full_access')) return true
      return permissions.some((p) => state.permissions.includes(p))
    },
    [state.permissions, state.isSuperuser]
  )

  const hasAllPermissions = useCallback(
    (...permissions: string[]) => {
      if (state.isSuperuser) return true
      if (state.permissions.includes('full_access')) return true
      return permissions.every((p) => state.permissions.includes(p))
    },
    [state.permissions, state.isSuperuser]
  )

  const value = useMemo<PermissionsContextType>(
    () => ({
      ...state,
      hasPermission,
      hasAnyPermission,
      hasAllPermissions,
      refresh: fetchPermissions,
    }),
    [state, hasPermission, hasAnyPermission, hasAllPermissions, fetchPermissions]
  )

  return <PermissionsContext.Provider value={value}>{children}</PermissionsContext.Provider>
}

export function usePermissions() {
  const ctx = useContext(PermissionsContext)
  if (!ctx) throw new Error('usePermissions must be used within PermissionsProvider')
  return ctx
}

// Navigation items with their required permissions
export const NAV_PERMISSIONS = {
  '/': null, // Dashboard - always visible
  '/live': 'live.view',
  '/playback': 'recordings.view',
  '/cameras': 'cameras.view',
  '/rbac': 'users.view',
  '/byok': 'byok.manage',
  '/network': 'network.view',
  '/audit-logs': 'audit.view',
  '/updates': 'firmware.view',
  '/logs': 'audit.view',
  '/ai-engine': 'ai.view',
  '/byom': 'byom.manage',
  '/compliance': 'compliance.view',
  '/alerts-incidents': 'alerts.view',
  '/integrations': 'integrations.view',
  '/onvif-tools': 'onvif.discover',
  '/cloud': 'cloud.view',
  '/support': null, // Always visible
  '/settings': 'settings.view',
  '/firmware': 'firmware.view',
} as const
