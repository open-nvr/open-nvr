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

import { api } from '../lib/api'

export const authService = {
  checkSetup: () => api.post('/api/v1/auth/check-setup'),
  firstTimeSetup: (username: string, password: string) =>
    api.post('/api/v1/auth/first-time-setup', { username, password }),
  register: (username: string, email: string, password: string) =>
    api.post('/api/v1/auth/register', { username, email, password }),
  login: (username: string, password: string) => {
    const form = new URLSearchParams()
    form.append('grant_type', '')
    form.append('username', username)
    form.append('password', password)
    form.append('scope', '')
    form.append('client_id', '')
    form.append('client_secret', '')
    return api.post('/api/v1/auth/login', form, {
      headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
    })
  },
  loginJson: (username: string, password: string, code?: string) =>
    api.post('/api/v1/auth/login-json', { username, password, code }),
  me: () => api.get('/api/v1/auth/me'),
  myPermissions: () => api.get('/api/v1/users/me/permissions'),
  mfaSetup: (responseType: 'json' | 'text' | 'blob' = 'json') =>
    api.post('/api/v1/auth/mfa/setup', '', { responseType }),
  mfaVerify: (code: string) => api.post('/api/v1/auth/mfa/verify', { code }),
  mfaDisable: () => api.post('/api/v1/auth/mfa/disable', ''),
  logout: () => api.post('/api/v1/auth/logout', ''),
  refreshToken: (refresh_token: string) => api.post('/api/v1/auth/refresh', { refresh_token }),
}
