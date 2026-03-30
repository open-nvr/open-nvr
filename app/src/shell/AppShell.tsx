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

import { Outlet, NavLink, Link } from 'react-router-dom'
import { Menu, Monitor, Camera, Settings as SettingsIcon, Bell, Maximize, Minimize, LogOut, User as UserIcon, Sun, Moon, Play, RefreshCcw, FileSearch, Brain, FileCheck, AlertTriangle, Plug, LifeBuoy, Satellite, KeyRound, Shield, Network, Cpu, Boxes, Cloud, Database } from 'lucide-react'
import { useRef, useState } from 'react'
import { useFullscreen } from '../hooks/useFullscreen'
import { useAuth } from '../auth/AuthContext'
import { useTheme } from '../hooks/useTheme'
import { usePermissions, NAV_PERMISSIONS } from '../hooks/usePermissions'

export function AppShell() {
  const rootRef = useRef<HTMLDivElement>(null)
  const { isFullscreen, toggle } = useFullscreen(rootRef as React.RefObject<HTMLDivElement>)
  const [sidebarOpen, setSidebarOpen] = useState(true)
  const { user, logout } = useAuth()
  const { hasPermission, loading: permissionsLoading } = usePermissions()
  const [menuOpen, setMenuOpen] = useState(false)
  const { theme, toggleTheme } = useTheme()
  const sidebarRef = useRef<HTMLDivElement>(null)
  const [sidebarScrolling, setSidebarScrolling] = useState(false)

  // Check if a nav item should be visible based on permissions
  const canView = (path: keyof typeof NAV_PERMISSIONS) => {
    const requiredPerm = NAV_PERMISSIONS[path]
    if (requiredPerm === null) return true // Always visible
    return hasPermission(requiredPerm)
  }

  function onSidebarScroll() {
    if (!sidebarScrolling) setSidebarScrolling(true)
    window.clearTimeout((onSidebarScroll as any)._t)
    ;(onSidebarScroll as any)._t = window.setTimeout(() => setSidebarScrolling(false), 700)
  }
  return (
    <div ref={rootRef} className="min-h-screen bg-[var(--bg)] text-[var(--text)]">
  {/* Top white header (sticky) */}
  <header className="bg-[var(--bg-2)] border-b border-[var(--border)] text-[var(--text)] h-16 flex items-center px-4 text-sm uppercase tracking-wide sticky top-0 z-40">
        <Link to="/" className="font-semibold inline-flex items-center gap-2">
          <img src="/opennvr-logo.svg" alt="OpenNVR" className="h-10" />
        </Link>
        <div className="ml-auto flex items-center gap-3">
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
          <div className="relative">
            <button
              className="inline-flex items-center gap-1 px-2 py-1 bg-[var(--panel)] hover:bg-[var(--panel-2)] rounded"
              onClick={() => setMenuOpen((s) => !s)}
              title={user ? user.username : 'Account'}
            >
              <UserIcon size={14} />
              <span className="hidden md:inline">{user?.username ?? 'Account'}</span>
            </button>
            {menuOpen && (
              <div className="absolute right-0 mt-1 bg-[var(--panel)] border border-neutral-700 text-sm min-w-40 z-50">
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
          <span className="opacity-90">{new Date().toLocaleString()}</span>
        </div>
      </header>

      <div className="flex">
        {/* Sidebar */}
  <aside ref={sidebarRef} onScroll={onSidebarScroll} className={`${sidebarOpen ? 'w-64' : 'w-14'} sticky top-16 self-start h-[calc(100vh-4rem)] transition-all duration-200 overflow-y-auto overflow-x-hidden bg-[var(--bg-2)] p-2 sidebar-scroll ${sidebarScrolling ? 'is-scrolling' : ''}`}>
          <button
            className="flex items-center gap-2 w-full px-2 py-2 text-sm text-[var(--text-dim)] hover:text-[var(--text)] hover:bg-[var(--panel-2)] rounded"
            onClick={() => setSidebarOpen((s) => !s)}
            aria-label="Toggle Sidebar"
            title={sidebarOpen ? 'Collapse' : 'Expand'}
          >
            <Menu size={16} />
            <span className={`${sidebarOpen ? 'inline' : 'hidden'}`}>Menu</span>
          </button>
          <nav className="mt-2 space-y-1">
            <SideLink to="/" label="Dashboard" icon={<Monitor size={16} />} collapsed={!sidebarOpen} />
            {canView('/live') && <SideLink to="/live" label="Live View" icon={<Camera size={16} />} collapsed={!sidebarOpen} />}
            {canView('/playback') && <SideLink to="/playback" label="Recordings" icon={<Play size={16} />} collapsed={!sidebarOpen} />}
            {canView('/cameras') && <SideLink to="/cameras" label="Cameras" icon={<Camera size={16} />} collapsed={!sidebarOpen} />}
            {canView('/rbac') && <SideLink to="/rbac" label="Access Control (RBAC)" icon={<Shield size={16} />} collapsed={!sidebarOpen} />}
            {canView('/byok') && <SideLink to="/byok" label="Customer Keys (BYOK)" icon={<KeyRound size={16} />} collapsed={!sidebarOpen} />}
            {canView('/network') && <SideLink to="/network" label="Network" icon={<Network size={16} />} collapsed={!sidebarOpen} />}
            {canView('/audit-logs') && <SideLink to="/audit-logs" label="Audit Logs" icon={<Bell size={16} />} collapsed={!sidebarOpen} />}
            {canView('/updates') && <SideLink to="/updates" label="Media Server Config" icon={<RefreshCcw size={16} />} collapsed={!sidebarOpen} />}
            {canView('/logs') && <SideLink to="/logs" label="Logs & Forensics" icon={<FileSearch size={16} />} collapsed={!sidebarOpen} />}
            {canView('/ai-engine') && <SideLink to="/ai-engine" label="Anomaly AI Engine" icon={<Brain size={16} />} collapsed={!sidebarOpen} />}
            {canView('/byom') && <SideLink to="/byom" label="AI Models (BYOM)" icon={<Boxes size={16} />} collapsed={!sidebarOpen} />}
            {canView('/byom') && <SideLink to="/ai-detection-results" label="AI Detection Results" icon={<Database size={16} />} collapsed={!sidebarOpen} />}
            {canView('/compliance') && <SideLink to="/compliance" label="Compliance & Reports" icon={<FileCheck size={16} />} collapsed={!sidebarOpen} />}
            {canView('/alerts-incidents') && <SideLink to="/alerts-incidents" label="Alerts & Incidents" icon={<AlertTriangle size={16} />} collapsed={!sidebarOpen} />}
            {canView('/integrations') && <SideLink to="/integrations" label="Integrations" icon={<Plug size={16} />} collapsed={!sidebarOpen} />}
            {/* {canView('/onvif-tools') && <SideLink to="/onvif-tools" label="ONVIF Tools" icon={<Satellite size={16} />} collapsed={!sidebarOpen} />} */}
            {canView('/cloud') && <SideLink to="/cloud" label="Cloud" icon={<Cloud size={16} />} collapsed={!sidebarOpen} />}
            <SideLink to="/support" label="Support" icon={<LifeBuoy size={16} />} collapsed={!sidebarOpen} />
            {canView('/settings') && <SideLink to="/settings" label="Configuration" icon={<SettingsIcon size={16} />} collapsed={!sidebarOpen} />}
            {canView('/firmware') && <SideLink to="/firmware" label="Firmware" icon={<Cpu size={16} />} collapsed={!sidebarOpen} />}
          </nav>
        </aside>

        {/* Main content */}
        <main className="flex-1 p-4 bg-[var(--panel)] min-h-[calc(100vh-4rem)]">
          <Outlet />
        </main>
      </div>
    </div>
  )
}

function SideLink({ to, label, icon, collapsed }: { to: string; label: string; icon: React.ReactNode; collapsed?: boolean }) {
  return (
    <NavLink
      to={to}
      end
      className={({ isActive }) => `flex items-center ${collapsed ? 'justify-center' : ''} gap-2 px-3 py-2 rounded text-sm ${isActive ? 'bg-[var(--panel-2)] text-[var(--text)]' : 'text-[var(--text-dim)] hover:text-[var(--text)] hover:bg-[var(--panel-2)]'}`}
    >
      <span className="flex-shrink-0 w-4 h-4 flex items-center justify-center">{icon}</span>
      <span className={`${collapsed ? 'hidden' : 'inline'}`}>{label}</span>
    </NavLink>
  )
}
