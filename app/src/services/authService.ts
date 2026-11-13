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
  // V-001 / M0 C-1: setup_token is minted at server startup and printed
  // to stdout exactly once. Without it the backend refuses the request
  // (HTTP 403) to close the bootstrap-race window where any LAN attacker
  // could claim the admin account.
  firstTimeSetup: (
    username: string,
    password: string,
    setup_token: string,
  ) =>
    api.post('/api/v1/auth/first-time-setup', {
      username,
      password,
      setup_token,
    }),
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
  // Ways in besides a password, for the login page: e-mailed reset, single sign-on.
  recoveryOptions: () => api.get('/api/v1/auth/recovery-options'),
  forgotPassword: (email: string) => api.post('/api/v1/auth/forgot', { email }),
  resetPassword: (token: string, password: string) => api.post('/api/v1/auth/reset', { token, password }),
  oidcExchange: (code: string) => api.post('/api/v1/auth/oidc/exchange', { code }),
  myPermissions: () => api.get('/api/v1/users/me/permissions'),
  mfaSetup: (responseType: 'json' | 'text' | 'blob' = 'json') =>
    api.post('/api/v1/auth/mfa/setup', '', { responseType }),
  mfaVerify: (code: string) => api.post('/api/v1/auth/mfa/verify', { code }),
  // Your own authenticators (Settings > Two-factor sign-in). Once you have a
  // working one, changes need your password and a current code.
  mfaFactors: () => api.get('/api/v1/auth/mfa/factors'),
  mfaBeginTotp: (body: { label?: string; current_password?: string }, mfaCode?: string) =>
    api.post('/api/v1/auth/mfa/factors/totp', body, mfaCode ? { headers: { 'X-MFA-Code': mfaCode } } : undefined),
  mfaConfirmTotp: (factorId: number, body: { code: string; label?: string }) =>
    api.post(`/api/v1/auth/mfa/factors/${factorId}/confirm`, body),
  mfaUpdateFactor: (factorId: number, body: { label?: string; enabled?: boolean; current_password: string }, mfaCode: string) =>
    api.patch(`/api/v1/auth/mfa/factors/${factorId}`, body, { headers: { 'X-MFA-Code': mfaCode } }),
  mfaRegenerateRecoveryCodes: (currentPassword: string, mfaCode: string) =>
    api.post('/api/v1/auth/mfa/recovery-codes', { current_password: currentPassword }, { headers: { 'X-MFA-Code': mfaCode } }),
  mfaRemoveFactor: (factorId: number, currentPassword: string, mfaCode: string) =>
    api.delete(`/api/v1/auth/mfa/factors/${factorId}`, { headers: { 'X-MFA-Code': mfaCode } }, { current_password: currentPassword }),
  logout: () => api.post('/api/v1/auth/logout', ''),
  refreshToken: (refresh_token: string) => api.post('/api/v1/auth/refresh', { refresh_token }),
}
