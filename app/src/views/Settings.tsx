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

import { NavLink, useLocation, useNavigate, Navigate } from 'react-router-dom'
import { useEffect, useMemo } from 'react'
import { UsersManager } from './settings/UsersManager'
import { RolesManager } from './settings/RolesManager'
import { PermissionsManager } from './settings/PermissionsManager'
import { CameraConfigManager } from './settings/CameraConfigManager'
import { PasswordPolicy } from './settings/PasswordPolicy'
import { SecurityFirewall } from './settings/SecurityFirewall'
import { SecurityPorts } from './settings/SecurityPorts'
import { SecurityPlatformAccess } from './settings/SecurityPlatformAccess'
import { SecurityNAT } from './settings/SecurityNAT'
import { WebRTCSettings } from './settings/WebRTCSettings'
import { MediaSourceSettings } from './settings/MediaSourceSettings'
import { MediaServerManager } from './settings/MediaServerManager'
import { RecordingSettings } from './settings/RecordingSettings'
import { SystemSettings } from './settings/general/SystemSettings'
import { NetworkSettings } from './settings/general/NetworkSettings'
import { AlarmSettings } from './settings/general/AlarmSettings'
import { Rs232Settings } from './settings/general/Rs232Settings'
import { LiveViewSettings } from './settings/general/LiveViewSettings'
import { ExceptionsSettings } from './settings/general/ExceptionsSettings'
import { UserSettings } from './settings/general/UserSettings'
import { PosSettings } from './settings/general/PosSettings'
import { MoreUplink } from './settings/more/Uplink'
import { WindowSettings } from './settings/more/WindowSettings'

const toSlug = (s: string) => s.toLowerCase().replace(/\s+|\//g, '-');

const TABS: { key: string; label: string; submenu: string[] }[] = [
  { key: 'general', label: 'General', submenu: ['General', 'Alarm', 'RS-232', 'Live View', 'Exceptions', 'User', 'POS'] },
  // Manage-Users moved to sidebar (Access Control)
  { key: 'security', label: 'Security', submenu: ['Firewall', 'Port Settings', 'Platform Access', 'NAT'] },
  { key: 'webrtc', label: 'Webrtc', submenu: [] },
  { key: 'camera-config', label: 'Camera-Config', submenu: [] },
  { key: 'recording', label: 'Recording', submenu: [] },
  { key: 'media-source', label: 'Media-Source', submenu: [] },
  { key: 'more-settings', label: 'More Settings', submenu: ['Window Settings', 'Uplink'] },
]

export function Settings() {
  const location = useLocation()
  const navigate = useNavigate()

  // parse /settings/:tab?/:submenu?
  const { activeTabKey, activeSubKey } = useMemo(() => {
    const match = location.pathname.split('/settings')[1] || ''
    const parts = match.replace(/^\//, '').split('/').filter(Boolean)
    const tab = parts[0] || 'webrtc'
    const sub = parts[1] || ''
    return { activeTabKey: tab, activeSubKey: sub }
  }, [location.pathname])

  useEffect(() => {
    // if no tab in URL, push default
    if (!location.pathname.match(/\/settings\//)) {
      navigate('/settings/webrtc', { replace: true })
    }
  }, [location.pathname, navigate])

  const tabDef = TABS.find(t => t.key === activeTabKey) ?? TABS[0]
  const submenu = tabDef.submenu

  return (
    <section className="space-y-4">
      {/* Top Tabs */}
      <div className="bg-[var(--accent)] text-white px-3 py-2 text-sm flex items-center gap-4 overflow-x-auto">
        {TABS.map((t) => (
          <NavLink
            key={t.key}
            to={t.submenu.length === 0 ? `/settings/${t.key}` : `/settings/${t.key}/${toSlug(t.submenu[0])}`}
            className={({ isActive }) => `px-2 py-1 rounded whitespace-nowrap ${location.pathname.startsWith(`/settings/${t.key}`) ? 'bg-white/15' : 'opacity-90 hover:opacity-100'}`}
          >
            {t.label}
          </NavLink>
        ))}
      </div>

      <div className="flex">
        {/* Dynamic Submenu (hidden for WebRTC tab) */}
        {submenu.length > 0 && (
          <aside className="w-64 bg-[var(--bg-2)] p-3 text-sm">
            {submenu.map((s) => {
              const slug = toSlug(s)
              const active = location.pathname === `/settings/${tabDef.key}/${slug}`
              return (
                <NavLink
                  key={s}
                  to={`/settings/${tabDef.key}/${slug}`}
                  className={`block px-2 py-2 rounded ${active ? 'bg-[var(--panel-2)] text-[var(--text)]' : 'text-[var(--text-dim)] hover:text-[var(--text)] hover:bg-[var(--panel-2)]'}`}
                >
                  {s}
                </NavLink>
              )
            })}
          </aside>
        )}

        {/* Content Area */}
        <div className={`p-4 bg-[var(--panel)] flex-1`}>
          {tabDef.key === 'general' && activeSubKey === 'general' ? (
            <SystemSettings />
          ) : tabDef.key === 'general' && activeSubKey === 'alarm' ? (
            <AlarmSettings />
          ) : tabDef.key === 'general' && activeSubKey === 'rs-232' ? (
            <Rs232Settings />
          ) : tabDef.key === 'general' && activeSubKey === 'live-view' ? (
            <LiveViewSettings />
          ) : tabDef.key === 'general' && activeSubKey === 'exceptions' ? (
            <ExceptionsSettings />
          ) : tabDef.key === 'general' && activeSubKey === 'user' ? (
            <UserSettings />
          ) : tabDef.key === 'general' && activeSubKey === 'pos' ? (
            <PosSettings />
          ) : tabDef.key === 'manage-cameras' ? (
            <Navigate to="/cameras" replace />
          ) : tabDef.key === 'recording' ? (
            <RecordingSettings />
          ) : tabDef.key === 'security' && activeSubKey === 'firewall' ? (
            <SecurityFirewall />
          ) : tabDef.key === 'security' && activeSubKey === 'port-settings' ? (
            <SecurityPorts />
          ) : tabDef.key === 'security' && activeSubKey === 'platform-access' ? (
            <SecurityPlatformAccess />
          ) : tabDef.key === 'security' && activeSubKey === 'nat' ? (
            <SecurityNAT />
          ) : tabDef.key === 'webrtc' ? (
            <WebRTCSettings />
          ) : tabDef.key === 'media-source' && (activeSubKey === 'settings' || !activeSubKey) ? (
            <MediaSourceSettings />
          ) : tabDef.key === 'media-source' && activeSubKey === 'media-server-manager' ? (
            <MediaServerManager />
          ) : tabDef.key === 'camera-config' ? (
            <CameraConfigManager />
          ) : tabDef.key === 'more-settings' && activeSubKey === 'window-settings' ? (
            <WindowSettings />
          ) : tabDef.key === 'more-settings' && activeSubKey === 'uplink' ? (
            <MoreUplink />
          ) : tabDef.key === 'more-settings' && activeSubKey === 'certificates' ? (
            <Navigate to="/byok" replace />
          ) : (
            <Placeholder title={`${tabDef.label}${activeSubKey ? ` · ${activeSubKey}` : ''}`} />
          )}
        </div>
      </div>
    </section>
  )
}

function Placeholder({ title }: { title: string }) {
  return (
    <div className="text-sm text-[var(--text-dim)]">
      <div className="mb-2 font-medium text-[var(--text)]">{title}</div>
      <div>Configuration options will appear here.</div>
    </div>
  )
}
