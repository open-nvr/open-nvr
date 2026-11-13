/**
 * Copyright (c) 2026 OpenNVR
 * SPDX-License-Identifier: AGPL-3.0-or-later
 *
 * How people sign in: the OpenID Connect provider and the mail server the
 * password reset uses. Superuser only. Secrets are write-only: the server
 * says whether one is stored, never what it is.
 */
import { api } from '../lib/api'

export type MailSettings = {
  host: string
  port: number
  security: 'none' | 'starttls' | 'tls'
  username: string
  from_address: string
  from_name: string
  has_password: boolean
  configured: boolean
}

export type OidcSettings = {
  enabled: boolean
  issuer: string
  client_id: string
  scopes: string
  button_label: string
  auto_create: boolean
  default_role_id: number | null
  role_claim: string
  role_map: Record<string, string>
  superuser_values: string[]
  allow_local_login: boolean
  has_client_secret: boolean
  roles: { id: number; name: string }[]
}

export const authSettingsService = {
  getMailSettings: () => api.get('/api/v1/auth-settings/mail'),
  putMailSettings: (payload: Partial<MailSettings> & { password?: string | null }) =>
    api.put('/api/v1/auth-settings/mail', payload),
  testMail: (to: string) => api.post('/api/v1/auth-settings/mail/test', { to }),
  getOidcSettings: () => api.get('/api/v1/auth-settings/oidc'),
  putOidcSettings: (payload: Partial<Omit<OidcSettings, 'roles' | 'has_client_secret'>> & { client_secret?: string | null }) =>
    api.put('/api/v1/auth-settings/oidc', payload),
  testOidc: (issuer: string) => api.post('/api/v1/auth-settings/oidc/test', { issuer }),
}
