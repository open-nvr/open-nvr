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

import { NavLink, useLocation } from 'react-router-dom'
import { UsersManager } from './settings/UsersManager'
import { RolesManager } from './settings/RolesManager'
import { PermissionsManager } from './settings/PermissionsManager'
import { PasswordPolicy } from './settings/PasswordPolicy'

const SUBTABS = [
  { key: 'users', label: 'Users' },
  { key: 'roles', label: 'Roles' },
  { key: 'permissions', label: 'Permissions' },
  { key: 'password-policy', label: 'Password Policy' },
]

export function AccessControl() {
  const location = useLocation()
  const active = (location.pathname.split('/rbac/')[1] || '').replace(/\/$/, '') || 'users'
  return (
    <section className="space-y-4">
      <div className="flex items-center justify-between">
        <h1 className="text-lg font-semibold">Access Control (RBAC)</h1>
      </div>

      {/* Top-style navigation like Configuration tabs */}
      <div className="bg-[var(--accent)] text-white px-3 py-2 text-sm flex items-center gap-4">
        {SUBTABS.map((s) => (
          <NavLink
            key={s.key}
            to={`/rbac/${s.key}`}
            className={({ isActive }) => `px-2 py-1 rounded ${isActive ? 'bg-white/15' : 'opacity-90 hover:opacity-100'}`}
            end
          >
            {s.label}
          </NavLink>
        ))}
      </div>

      <div className="p-4 bg-[var(--panel)]">
        {active === 'users' ? (
          <UsersManager />
        ) : active === 'roles' ? (
          <RolesManager />
        ) : active === 'permissions' ? (
          <PermissionsManager />
        ) : active === 'password-policy' ? (
          <PasswordPolicy />
        ) : (
          <div className="text-sm text-[var(--text-dim)]">Select a section.</div>
        )}
      </div>
    </section>
  )
}


