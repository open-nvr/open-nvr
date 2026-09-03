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

import { Outlet, NavLink, Link, useLocation } from 'react-router-dom'
import { DeviceBlockedOverlay } from '../components/DeviceBlockedOverlay'
import { Menu, Monitor, Camera, Car, Users, Settings as SettingsIcon, Bell, Maximize, Minimize, LogOut, User as UserIcon, Sun, Moon, MonitorPlay, RefreshCcw, FileSearch, Brain, FileCheck, AlertTriangle, Plug, LifeBuoy, KeyRound, Shield, Network, Cpu, Boxes, Cloud, Database, ChevronDown, Layers } from 'lucide-react'
import { useQuery } from '@tanstack/react-query'
import { apiService } from '../lib/apiService'
import { Fragment, useEffect, useMemo, useRef, useState } from 'react'
import { useFullscreen } from '../hooks/useFullscreen'
import { useClickOutside } from '../hooks/useClickOutside'
import { useAuth } from '../auth/AuthContext'
import { useTheme } from '../hooks/useTheme'
import { usePermissions, NAV_PERMISSIONS } from '../hooks/usePermissions'
import { ErrorBoundary } from '../components/ErrorBoundary'
import { CameraStatusProvider } from '../hooks/useCameraStatus'
import { SystemAlertBanner } from '../components/SystemAlertBanner'
import { AlertBell } from '../components/AlertBell'

type NavItem = {
  to: string
  label: string
  icon: React.ReactNode
  /** key into NAV_PERMISSIONS; null-valued entries are always visible */
  perm: keyof typeof NAV_PERMISSIONS
  /** exact-match highlighting — set when a sibling route extends this path */
  end?: boolean
}

type NavGroup = {
  key: string
  label: string
  /** pinned groups render their items directly — no header, never collapsible */
  pinned?: boolean
  items: NavItem[]
}

// Navigation grouped by product surface (see docs/design/platform-blueprint.html):
// the NVR operator surface first, then AI, security, governance, administration.
const NAV_GROUPS: NavGroup[] = [
  {
    key: 'nvr',
    label: 'NVR',
    pinned: true,
    items: [
      { to: '/', label: 'Dashboard', icon: <Monitor size={16} />, perm: '/' },
      { to: '/live', label: 'Live View', icon: <Camera size={16} />, perm: '/live' },
      { to: '/playback/sync', label: 'Recordings', icon: <MonitorPlay size={16} />, perm: '/playback/sync' },
      { to: '/cameras', label: 'Cameras', icon: <Camera size={16} />, perm: '/cameras' },
    ],
  },
  {
    key: 'ai',
    label: 'AI & Detections',
    items: [
      { to: '/ai-engine', label: 'AI Engine', icon: <Brain size={16} />, perm: '/ai-engine' },
      { to: '/byom', label: 'AI Models (BYOM)', icon: <Boxes size={16} />, perm: '/byom' },
      { to: '/ai-detection-results', label: 'Detection Results', icon: <Database size={16} />, perm: '/byom' },
      { to: '/ai-adapters', label: 'AI Adapters', icon: <Layers size={16} />, perm: '/ai-engine' },
      { to: '/app-catalog', label: 'App Catalog', icon: <Boxes size={16} />, perm: '/ai-engine' },
    ],
  },
  {
    key: 'security',
    label: 'Security & Network',
    items: [
      { to: '/network', label: 'Network', icon: <Network size={16} />, perm: '/network' },
      { to: '/logs', label: 'Logs & Forensics', icon: <FileSearch size={16} />, perm: '/logs' },
    ],
  },
  {
    key: 'governance',
    label: 'Governance',
    items: [
      { to: '/audit-logs', label: 'Audit Logs', icon: <Bell size={16} />, perm: '/audit-logs' },
      { to: '/compliance', label: 'Compliance & Reports', icon: <FileCheck size={16} />, perm: '/compliance' },
      { to: '/alarms', label: 'Alarms', icon: <Bell size={16} />, perm: '/alarms' },
      { to: '/alerts-incidents', label: 'Alerts & Incidents', icon: <AlertTriangle size={16} />, perm: '/alerts-incidents' },
      { to: '/rbac', label: 'Access Control (RBAC)', icon: <Shield size={16} />, perm: '/rbac' },
      { to: '/byok', label: 'Customer Keys (BYOK)', icon: <KeyRound size={16} />, perm: '/byok' },
    ],
  },
  {
    key: 'admin',
    label: 'Administration',
    items: [
      { to: '/settings', label: 'Configuration', icon: <SettingsIcon size={16} />, perm: '/settings' },
      { to: '/updates', label: 'Media Server Config', icon: <RefreshCcw size={16} />, perm: '/updates' },
      { to: '/integrations', label: 'Integrations', icon: <Plug size={16} />, perm: '/integrations' },
      { to: '/cloud', label: 'Cloud', icon: <Cloud size={16} />, perm: '/cloud' },
      { to: '/firmware', label: 'Firmware', icon: <Cpu size={16} />, perm: '/firmware' },
      { to: '/support', label: 'Support', icon: <LifeBuoy size={16} />, perm: '/support' },
    ],
  },
]

// Accordion: at most one group is open at a time; its key is persisted.
const OPEN_GROUP_KEY = 'opennvr.sidebar.openGroup'

function loadOpenGroup(): string | null {
  try {
    return localStorage.getItem(OPEN_GROUP_KEY)
  } catch {
    return null
  }
}

export function AppShell() {
  const rootRef = useRef<HTMLDivElement>(null)
  const { isFullscreen, toggle } = useFullscreen(rootRef as React.RefObject<HTMLDivElement>)
  const [sidebarOpen, setSidebarOpen] = useState(true)
  const { user, logout } = useAuth()
  const { hasPermission } = usePermissions()
  const [menuOpen, setMenuOpen] = useState(false)
  const accountMenuRef = useRef<HTMLDivElement>(null)
  useClickOutside(accountMenuRef, menuOpen, () => setMenuOpen(false))
  const { theme, toggleTheme } = useTheme()
  const sidebarRef = useRef<HTMLDivElement>(null)
  const [sidebarScrolling, setSidebarScrolling] = useState(false)
  const location = useLocation()
  const [openGroup, setOpenGroup] = useState<string | null>(loadOpenGroup)

  const canView = (path: keyof typeof NAV_PERMISSIONS) => {
    const requiredPerm = NAV_PERMISSIONS[path]
    if (requiredPerm === null) return true
    return hasPermission(requiredPerm)
  }

  // Application surfaces LIGHT UP per install: a first-class page appears
  // only when an enabled catalog app provides its capability — so the nav
  // scales with what THIS deployment enabled, never with catalog size.
  // Capability-keyed (requires_tasks), so a community-built replacement
  // app lights the same page. Best-effort: registry down = no app pages.
  const appsNav = useQuery({
    queryKey: ['apps'],
    queryFn: async () => {
      const { data } = await apiService.getApps()
      return (Array.isArray(data) ? data : []) as {
        enabled?: boolean
        manifest?: { requires_tasks?: string[]; provides?: string[] } | null
      }[]
    },
    retry: 0,
    staleTime: 60_000,
    refetchInterval: 120_000,
  })
  // The curated first-class pages, capability-keyed on manifest
  // `provides` (with a legacy predicate for manifests that predate the
  // field). Adding a vertical = one row here + its page — the nav
  // scales with what THIS install enabled, never with catalog size.
  const apps = appsNav.data ?? []
  const providesEnabled = (capability: string, legacy?: (m: any) => boolean) =>
    apps.some((a) => a.enabled && (
      (a.manifest?.provides ?? []).includes(capability) ||
      (legacy ? legacy(a.manifest ?? {}) : false)
    ))
  const lprEnabled = providesEnabled('vehicles',
    (m) => (m.requires_tasks ?? []).includes('license_plate_recognition'))
  const occupancyEnabled = providesEnabled('occupancy')

  const visibleGroups = useMemo(
    () => {
      const groups = NAV_GROUPS.map((g) => ({ ...g, items: g.items.filter((i) => canView(i.perm)) })).filter((g) => g.items.length > 0)
      const appItems = [
        ...(lprEnabled && canView('/vehicles')
          ? [{ to: '/vehicles', label: 'Vehicles', icon: <Car size={16} />, perm: '/vehicles' as const }]
          : []),
        ...(occupancyEnabled && canView('/occupancy')
          ? [{ to: '/occupancy', label: 'Occupancy', icon: <Users size={16} />, perm: '/occupancy' as const }]
          : []),
      ]
      if (appItems.length > 0) {
        // Right after the pinned NVR group: these are operational pages.
        groups.splice(1, 0, { key: 'applications', label: 'Applications', items: appItems })
      }
      return groups
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [hasPermission, lprEnabled, occupancyEnabled]
  )
  const pinnedGroups = visibleGroups.filter((g) => g.pinned)
  const menuGroups = visibleGroups.filter((g) => !g.pinned)

  const activeGroupKey = useMemo(() => {
    for (const g of NAV_GROUPS) {
      if (g.items.some((i) => (i.to === '/' ? location.pathname === '/' : location.pathname.startsWith(i.to)))) return g.key
    }
    return null
  }, [location.pathname])

  function persistOpenGroup(key: string | null) {
    try {
      if (key === null) localStorage.removeItem(OPEN_GROUP_KEY)
      else localStorage.setItem(OPEN_GROUP_KEY, key)
    } catch { /* storage unavailable: keep in-memory state */ }
  }

  // Navigating into a group opens it (closing any other); the user can still
  // close it manually afterwards.
  useEffect(() => {
    if (!activeGroupKey) return
    setOpenGroup((prev) => {
      if (prev === activeGroupKey) return prev
      persistOpenGroup(activeGroupKey)
      return activeGroupKey
    })
  }, [activeGroupKey])

  function toggleGroup(key: string) {
    setOpenGroup((prev) => {
      const next = prev === key ? null : key
      persistOpenGroup(next)
      return next
    })
  }

  function onSidebarScroll() {
    if (!sidebarScrolling) setSidebarScrolling(true)
    window.clearTimeout((onSidebarScroll as any)._t)
    ;(onSidebarScroll as any)._t = window.setTimeout(() => setSidebarScrolling(false), 700)
  }

  return (
    <CameraStatusProvider>
    <div ref={rootRef} className="min-h-screen bg-[var(--bg)] text-[var(--text)]">
  {/* Top white header (sticky) */}
  <header className="bg-[var(--bg-2)] border-b border-[var(--border)] text-[var(--text)] h-12 flex items-center px-4 text-sm uppercase tracking-wide sticky top-0 z-40">
        <Link to="/" className="font-semibold inline-flex items-center gap-2">
          <img src="/opennvr-logo.svg" alt="OpenNVR" className="h-10" />
        </Link>
        <div className="ml-auto flex items-center gap-3">
          <AlertBell />
          <button
            aria-label="Toggle Theme"
            className="inline-flex items-center gap-1 px-2 py-1 bg-[var(--panel)] hover:bg-[var(--panel-2)] rounded"
            onClick={toggleTheme}
            title={theme === 'light' ? 'Switch to dark' : 'Switch to light'}
          >
            {theme === 'light' ? <Moon size={14} /> : <Sun size={14} />}
            <span className="hidden md:inline">{theme === 'light' ? 'Dark' : 'Light'}</span>
          </button>
          {canView('/live') && (
            <Link
              to="/live"
              className="inline-flex items-center gap-1 px-2 py-1 bg-[var(--panel)] hover:bg-[var(--panel-2)] rounded"
              title="Open Live View"
            >
              <Camera size={14} />
              <span className="hidden md:inline">Live</span>
            </Link>
          )}
          <div className="relative" ref={accountMenuRef}>
            <button
              className="inline-flex items-center gap-1 px-2 py-1 bg-[var(--panel)] hover:bg-[var(--panel-2)] rounded"
              onClick={() => setMenuOpen((s) => !s)}
              title={user ? user.username : 'Account'}
            >
              <UserIcon size={14} />
              <span className="hidden md:inline">{user?.username ?? 'Account'}</span>
            </button>
            {menuOpen && (
              <div className="absolute right-0 mt-1 bg-[var(--panel)] border border-[var(--border)] text-sm min-w-40 z-50">
                <div className="px-3 py-2 text-[var(--text-dim)]">Signed in as <span className="text-[var(--text)]">{user?.username}</span></div>
                <button className="w-full text-left px-3 py-2 hover:bg-[var(--panel-2)] inline-flex items-center gap-2" onClick={logout}>
                  <LogOut size={14} /> Logout
                </button>
              </div>
            )}
          </div>
          <button
            aria-label="Toggle Fullscreen"
            className="inline-flex items-center gap-1 px-2 py-1 bg-[var(--panel)] hover:bg-[var(--panel-2)] rounded"
            onClick={toggle}
            title={isFullscreen ? 'Exit Fullscreen' : 'Enter Fullscreen'}
          >
            {isFullscreen ? <Minimize size={14} /> : <Maximize size={14} />}
            <span className="hidden md:inline">Fullscreen</span>
          </button>
          <LiveClock />
        </div>
      </header>

      <div className="flex">
        {/* Sidebar */}
  <aside className={`${sidebarOpen ? 'w-56' : 'w-14'} flex-shrink-0 sticky top-12 self-start h-[calc(100vh-3rem)] transition-all duration-200 bg-[var(--bg-2)] flex flex-col`}>
          {/* Fixed header: toggle + pinned items never scroll away; its bottom
              border is the clip line for the scrollable nav below */}
          <div className="flex-shrink-0 p-2 border-b border-[var(--border)]">
            <button
              className="inline-flex items-center justify-center p-2 text-[var(--text-dim)] hover:text-[var(--text)] hover:bg-[var(--panel-2)] rounded"
              onClick={() => setSidebarOpen((s) => !s)}
              aria-label="Toggle Sidebar"
              title={sidebarOpen ? 'Collapse sidebar' : 'Expand sidebar'}
            >
              <Menu size={16} />
            </button>
            {pinnedGroups.map((group) => (
              <div key={group.key} className="mt-2 space-y-0.5">
                {group.items.map((item) => (
                  <SideLink key={item.to} to={item.to} end={item.end} label={item.label} icon={item.icon} collapsed={!sidebarOpen} />
                ))}
              </div>
            ))}
          </div>
          {/* No bottom padding: the scroll clip edge must coincide with where
              the last sticky header docks, or items peek out beneath it */}
          <nav ref={sidebarRef} onScroll={onSidebarScroll} className={`flex-1 overflow-y-auto overflow-x-hidden px-2 sidebar-scroll ${sidebarScrolling ? 'is-scrolling' : ''}`}>
            {menuGroups.map((group, gi) => {
              const collapsed = openGroup !== group.key
              if (!sidebarOpen) {
                return (
                  <div key={group.key} className="mb-2 pb-2 border-b border-[var(--border)] last:border-b-0 space-y-0.5">
                    {group.items.map((item) => (
                      <SideLink key={item.to} to={item.to} end={item.end} label={item.label} icon={item.icon} collapsed />
                    ))}
                  </div>
                )
              }
              // Sticky headers: headers and item lists are direct children of
              // the scroll container (sticky is bounded by its parent, so
              // nesting would defeat it). Headers passed while scrolling stack
              // in order at the top; groups further down scroll naturally.
              return (
                <Fragment key={group.key}>
                  <button
                    style={{ top: gi * 32 }}
                    className="sticky z-10 w-full h-8 flex items-center justify-between px-2.5 text-sm font-medium bg-[var(--bg-2)] text-[var(--text-dim)] hover:text-[var(--text)] hover:bg-[var(--panel-2)] rounded"
                    onClick={() => toggleGroup(group.key)}
                    aria-expanded={!collapsed}
                  >
                    <span className="truncate whitespace-nowrap">{group.label}</span>
                    <ChevronDown size={16} className={`flex-shrink-0 transition-transform ${collapsed ? '-rotate-90' : ''}`} />
                  </button>
                  {!collapsed && (
                    <div className="pl-3 py-1 space-y-0.5">
                      {group.items.map((item) => (
                        <SideLink key={item.to} to={item.to} end={item.end} label={item.label} icon={item.icon} />
                      ))}
                    </div>
                  )}
                </Fragment>
              )
            })}
          </nav>
          {/* Dead strip: keeps bottom-docked headers clear of the browser's
              status bubble that previews link URLs in the corner */}
          <div className="h-8 flex-shrink-0" aria-hidden="true" />
        </aside>

        {/* Main content — boundary keyed by route so navigating away resets a crash */}
        <main className="flex-1 min-w-0 p-4 bg-[var(--panel)] min-h-[calc(100vh-3rem)]">
          <SystemAlertBanner />
          <ErrorBoundary key={location.pathname}>
            <Outlet />
          </ErrorBoundary>
        </main>
      </div>
      <DeviceBlockedOverlay />
    </div>
    </CameraStatusProvider>
  )
}

function LiveClock() {
  const [now, setNow] = useState(() => new Date())
  useEffect(() => {
    const id = window.setInterval(() => setNow(new Date()), 1000)
    return () => window.clearInterval(id)
  }, [])
  return <span className="opacity-90 tabular-nums">{now.toLocaleString()}</span>
}

function SideLink({ to, label, icon, collapsed, end }: { to: string; label: string; icon: React.ReactNode; collapsed?: boolean; end?: boolean }) {
  return (
    <NavLink
      to={to}
      end={end || to === '/'}
      title={collapsed ? label : undefined}
      className={({ isActive }) => `flex items-center ${collapsed ? 'justify-center' : ''} gap-2 px-2.5 py-1 rounded text-sm ${isActive ? 'bg-[color-mix(in_oklab,var(--accent)_15%,transparent)] text-[var(--accent)] font-medium' : 'text-[var(--text-dim)] hover:text-[var(--text)] hover:bg-[var(--panel-2)]'}`}
    >
      <span className="flex-shrink-0 w-4 h-4 flex items-center justify-center">{icon}</span>
      <span className={`${collapsed ? 'hidden' : 'inline'} truncate whitespace-nowrap`}>{label}</span>
    </NavLink>
  )
}
