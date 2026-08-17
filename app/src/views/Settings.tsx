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
import { CameraDeviceConfig } from './settings/CameraDeviceConfig'
import { DeviceFirewall } from './settings/DeviceFirewall'
import { PasswordPolicy } from './settings/PasswordPolicy'
import { WebRTCSettings } from './settings/WebRTCSettings'
import { MediaSourceSettings } from './settings/MediaSourceSettings'
import { MediaServerManager } from './settings/MediaServerManager'
import { RecordingSettings } from './settings/RecordingSettings'
import { DeletedCameras } from './settings/DeletedCameras'
import { MoreUplink } from './settings/more/Uplink'
import { WindowSettings } from './settings/more/WindowSettings'
import { SystemHealthSettings } from './settings/SystemHealthSettings'

// Single settings registry: each tab owns its label and its subviews, and
// each subview owns its slug, label, and panel. Tabs without subviews render
// their own panel. The URL scheme is /settings/:tab[/:sub].
type SubEntry = { slug: string; label: string; panel: () => React.ReactElement }
type TabEntry = { key: string; label: string; panel?: () => React.ReactElement; submenu: SubEntry[] }

const SETTINGS_REGISTRY: TabEntry[] = [
  // Camera-scoped configuration. Everything below this entry is NVR-scoped —
  // keeping that split explicit is why the camera's own security settings live
  // inside Camera-Config rather than next to the NVR firewall.
  {
    key: 'camera-config',
    label: 'Camera-Config',
    panel: () => <CameraDeviceConfig />,
    submenu: [
      { slug: 'device', label: 'Device Settings', panel: () => <CameraDeviceConfig /> },
      { slug: 'streaming', label: 'Streaming & Recording', panel: () => <CameraConfigManager /> },
    ],
  },
  { key: 'recording', label: 'Recording', panel: () => <RecordingSettings />, submenu: [] },
  // The bin: irreversibly soft-deleted cameras, their recordings, and the
  // superuser-only permanent purge.
  { key: 'deleted-cameras', label: 'Deleted Cameras', panel: () => <DeletedCameras />, submenu: [] },
  {
    key: 'media-source',
    label: 'Media-Source',
    // No-sub URL keeps rendering the first subview (legacy behavior).
    panel: () => <MediaSourceSettings />,
    submenu: [
      { slug: 'settings', label: 'Settings', panel: () => <MediaSourceSettings /> },
      { slug: 'media-server-manager', label: 'Media Server Manager', panel: () => <MediaServerManager /> },
    ],
  },
  {
    // OpenNVR's own device firewall — which machines may use OpenNVR. Not a
    // camera's access control (that is under Camera-Config > (camera) >
    // Security).
    key: 'firewall',
    label: 'Firewall',
    panel: () => <DeviceFirewall />,
    submenu: [],
  },
  {
    key: 'more-settings',
    label: 'More Settings',
    submenu: [
      { slug: 'webrtc', label: 'WebRTC', panel: () => <WebRTCSettings /> },
      { slug: 'window-settings', label: 'Window Settings', panel: () => <WindowSettings /> },
      { slug: 'uplink', label: 'Uplink', panel: () => <MoreUplink /> },
      { slug: 'system-health', label: 'System Health', panel: () => <SystemHealthSettings /> },
    ],
  },
]

// Legacy paths that moved to their own top-level views.
const SETTINGS_REDIRECTS: Record<string, string> = {
  'manage-cameras': '/cameras',
  'more-settings/certificates': '/byok',
}

export function Settings() {
  const location = useLocation()
  const navigate = useNavigate()

  // parse /settings/:tab?/:submenu?
  const { activeTabKey, activeSubKey } = useMemo(() => {
    const match = location.pathname.split('/settings')[1] || ''
    const parts = match.replace(/^\//, '').split('/').filter(Boolean)
    const tab = parts[0] || 'camera-config'
    const sub = parts[1] || ''
    return { activeTabKey: tab, activeSubKey: sub }
  }, [location.pathname])

  useEffect(() => {
    // if no tab in URL, push default
    if (!location.pathname.match(/\/settings\//)) {
      navigate('/settings/webrtc', { replace: true })
    }
  }, [location.pathname, navigate])

  const tabDef = SETTINGS_REGISTRY.find(t => t.key === activeTabKey) ?? SETTINGS_REGISTRY[0]
  const submenu = tabDef.submenu

  const redirect = SETTINGS_REDIRECTS[activeSubKey ? `${activeTabKey}/${activeSubKey}` : activeTabKey]
  const activeSub = activeSubKey ? submenu.find((s) => s.slug === activeSubKey) : undefined
  const panel = activeSub?.panel ?? (activeSubKey ? undefined : tabDef.panel)

  return (
    <section className="space-y-4">
      {/* Top Tabs */}
      <div className="bg-[var(--accent)] text-white px-3 py-2 text-sm flex items-center gap-4 overflow-x-auto">
        {SETTINGS_REGISTRY.map((t) => (
          <NavLink
            key={t.key}
            to={t.submenu.length === 0 ? `/settings/${t.key}` : `/settings/${t.key}/${t.submenu[0].slug}`}
            className={() => `px-2 py-1 rounded whitespace-nowrap ${location.pathname.startsWith(`/settings/${t.key}`) ? 'bg-white/15' : 'opacity-90 hover:opacity-100'}`}
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
              const active = location.pathname === `/settings/${tabDef.key}/${s.slug}`
              return (
                <NavLink
                  key={s.slug}
                  to={`/settings/${tabDef.key}/${s.slug}`}
                  className={`block px-2 py-2 rounded ${active ? 'bg-[var(--panel-2)] text-[var(--text)]' : 'text-[var(--text-dim)] hover:text-[var(--text)] hover:bg-[var(--panel-2)]'}`}
                >
                  {s.label}
                </NavLink>
              )
            })}
          </aside>
        )}

        {/* Content Area */}
        <div className={`p-4 bg-[var(--panel)] flex-1`}>
          <nav aria-label="Breadcrumb" className="text-xs text-[var(--text-dim)] mb-3">
            Configuration <span className="mx-1">/</span> {tabDef.label}
            {activeSubKey && (
              <>
                <span className="mx-1">/</span>
                {activeSub?.label ?? activeSubKey}
              </>
            )}
          </nav>
          {redirect ? (
            <Navigate to={redirect} replace />
          ) : panel ? (
            panel()
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
