/**
 * Copyright (c) 2026 OpenNVR
 * SPDX-License-Identifier: AGPL-3.0-or-later
 *
 * Access Control → Authentication: the site's identity provider (OpenID
 * Connect — Dex, Authentik, Keycloak, Zitadel, Entra ID, Google) and the
 * mail server the self-service password reset sends through. Both are
 * superuser settings; both keep their secret write-only.
 */
import { useCallback, useEffect, useState } from 'react'
import { CheckCircle2, Loader2, Mail, ShieldCheck } from 'lucide-react'
import { apiService } from '../../lib/apiService'
import { extractApiError } from '../../lib/apiError'
import { useSnackbar } from '../../components/Snackbar'
import { useTranslation } from '../../i18n'
import type { MailSettings, OidcSettings } from '../../services/authSettingsService'

const input = 'bg-[var(--bg-2)] border border-[var(--border)] px-3 py-2 text-sm w-full'
const label = 'text-xs text-[var(--text-dim)]'
const primary = 'px-4 py-2 text-sm bg-[var(--accent)] text-white disabled:opacity-50 flex items-center gap-2'
const secondary = 'px-4 py-2 text-sm border border-[var(--border)] hover:bg-[var(--panel-2)] disabled:opacity-50 flex items-center gap-2'

export function AuthenticationSettings() {
  return (
    <div className="space-y-8">
      <OidcSection />
      <MailSection />
    </div>
  )
}

function OidcSection() {
  const { t } = useTranslation()
  const { showError, showSuccess } = useSnackbar()
  const [cfg, setCfg] = useState<OidcSettings | null>(null)
  const [secret, setSecret] = useState('')
  const [roleMapText, setRoleMapText] = useState('')
  const [superText, setSuperText] = useState('')
  const [busy, setBusy] = useState(false)
  const [test, setTest] = useState<any>(null)

  const load = useCallback(async () => {
    try {
      const { data } = await apiService.getOidcSettings()
      setCfg(data as OidcSettings)
      setRoleMapText(Object.entries(data.role_map || {}).map(([k, v]) => `${k} = ${v}`).join('\n'))
      setSuperText((data.superuser_values || []).join(', '))
    } catch (e: any) {
      showError(extractApiError(e, t('authSettings.loadFailed')))
    }
  }, [showError, t])
  useEffect(() => { load() }, [load])

  if (!cfg) return <div className="text-sm text-[var(--text-dim)]">{t('common.loading')}</div>

  const parseRoleMap = () => {
    const map: Record<string, string> = {}
    roleMapText.split('\n').forEach((line) => {
      const [k, ...rest] = line.split('=')
      if (k && rest.length) map[k.trim()] = rest.join('=').trim()
    })
    return map
  }

  const save = async (patch: Record<string, unknown> = {}) => {
    setBusy(true)
    try {
      const payload: any = {
        enabled: cfg.enabled, issuer: cfg.issuer, client_id: cfg.client_id, scopes: cfg.scopes,
        button_label: cfg.button_label, auto_create: cfg.auto_create, default_role_id: cfg.default_role_id,
        role_claim: cfg.role_claim, role_map: parseRoleMap(),
        superuser_values: superText.split(',').map((s) => s.trim()).filter(Boolean),
        allow_local_login: cfg.allow_local_login, ...patch,
      }
      if (secret) payload.client_secret = secret
      const { data } = await apiService.putOidcSettings(payload)
      // Keeps the role list if a server answers without it (older cores did).
      setCfg((cur) => ({ ...cur, ...(data as OidcSettings), roles: data.roles ?? cur?.roles ?? [] }))
      setSecret('')
      showSuccess(t('authSettings.saved'))
    } catch (e: any) {
      showError(extractApiError(e, t('authSettings.saveFailed')))
    } finally {
      setBusy(false)
    }
  }

  const runTest = async () => {
    setBusy(true)
    setTest(null)
    try {
      const { data } = await apiService.testOidc(cfg.issuer)
      setTest(data)
    } catch (e: any) {
      showError(extractApiError(e, t('authSettings.oidcTestFailed')))
    } finally {
      setBusy(false)
    }
  }

  const redirectUri = `${window.location.origin}/api/v1/auth/oidc/callback`

  return (
    <section className="space-y-4">
      <div className="flex items-center gap-2 text-base font-semibold"><ShieldCheck size={18} /> {t('authSettings.oidcTitle')}</div>
      <p className="text-sm text-[var(--text-dim)]">{t('authSettings.oidcHelp')}</p>
      <label className="flex items-center gap-2 text-sm">
        <input type="checkbox" className="accent-[var(--accent)]" checked={cfg.enabled}
          onChange={(e) => setCfg({ ...cfg, enabled: e.target.checked })} /> {t('authSettings.oidcEnabled')}
      </label>
      <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
        <label className="flex flex-col gap-1 md:col-span-2">
          <span className={label}>{t('authSettings.issuer')}</span>
          <input className={input} placeholder="https://dex.example.com" value={cfg.issuer}
            onChange={(e) => setCfg({ ...cfg, issuer: e.target.value })} />
        </label>
        <label className="flex flex-col gap-1">
          <span className={label}>{t('authSettings.clientId')}</span>
          <input className={input} value={cfg.client_id} onChange={(e) => setCfg({ ...cfg, client_id: e.target.value })} />
        </label>
        <label className="flex flex-col gap-1">
          <span className={label}>{t('authSettings.clientSecret')} {cfg.has_client_secret ? `(${t('authSettings.secretStored')})` : ''}</span>
          <input className={input} type="password" autoComplete="off" value={secret} placeholder={cfg.has_client_secret ? '••••••••' : ''}
            onChange={(e) => setSecret(e.target.value)} />
        </label>
        <label className="flex flex-col gap-1">
          <span className={label}>{t('authSettings.scopes')}</span>
          <input className={input} value={cfg.scopes} onChange={(e) => setCfg({ ...cfg, scopes: e.target.value })} />
        </label>
        <label className="flex flex-col gap-1">
          <span className={label}>{t('authSettings.buttonLabel')}</span>
          <input className={input} value={cfg.button_label} onChange={(e) => setCfg({ ...cfg, button_label: e.target.value })} />
        </label>
        <label className="flex flex-col gap-1 md:col-span-2">
          <span className={label}>{t('authSettings.redirectUri')}</span>
          <code className="text-xs bg-[var(--bg-2)] border border-[var(--border)] px-3 py-2 select-all">{redirectUri}</code>
        </label>
      </div>
      <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
        <label className="flex items-center gap-2 text-sm">
          <input type="checkbox" className="accent-[var(--accent)]" checked={cfg.auto_create}
            onChange={(e) => setCfg({ ...cfg, auto_create: e.target.checked })} /> {t('authSettings.autoCreate')}
        </label>
        <label className="flex flex-col gap-1">
          <span className={label}>{t('authSettings.defaultRole')}</span>
          <select className={input} value={cfg.default_role_id ?? ''} onChange={(e) => setCfg({ ...cfg, default_role_id: e.target.value ? Number(e.target.value) : null })}>
            <option value="">—</option>
            {cfg.roles.map((r) => <option key={r.id} value={r.id}>{r.name}</option>)}
          </select>
        </label>
        <label className="flex flex-col gap-1">
          <span className={label}>{t('authSettings.roleClaim')}</span>
          <input className={input} value={cfg.role_claim} onChange={(e) => setCfg({ ...cfg, role_claim: e.target.value })} />
        </label>
        <label className="flex flex-col gap-1">
          <span className={label}>{t('authSettings.superuserValues')}</span>
          <input className={input} placeholder="nvr-admins" value={superText} onChange={(e) => setSuperText(e.target.value)} />
        </label>
        <label className="flex flex-col gap-1 md:col-span-2">
          <span className={label}>{t('authSettings.roleMap')}</span>
          <textarea className={`${input} font-mono min-h-[88px]`} placeholder={'nvr-operators = operator\nnvr-viewers = viewer'}
            value={roleMapText} onChange={(e) => setRoleMapText(e.target.value)} />
        </label>
        <label className="flex items-center gap-2 text-sm md:col-span-2" title={t('authSettings.allowLocalHint')}>
          <input type="checkbox" className="accent-[var(--accent)]" checked={cfg.allow_local_login}
            onChange={(e) => setCfg({ ...cfg, allow_local_login: e.target.checked })} /> {t('authSettings.allowLocal')}
        </label>
      </div>
      <div className="flex items-center gap-2">
        <button className={primary} disabled={busy} onClick={() => save()}>{busy && <Loader2 size={14} className="animate-spin" />}{t('common.save')}</button>
        <button className={secondary} disabled={busy || !cfg.issuer} onClick={runTest}>{t('authSettings.testProvider')}</button>
      </div>
      {test && (
        <div className="text-xs bg-[var(--bg-2)] border border-[var(--border)] p-3 space-y-1">
          <div className="flex items-center gap-1 text-[var(--ok)]"><CheckCircle2 size={14} /> {test.issuer}</div>
          <div>PKCE: {test.pkce ? 'S256' : '—'} · keys: {test.keys} · userinfo: {test.userinfo_endpoint ? 'yes' : 'no'}</div>
          {Array.isArray(test.claims_supported) && <div>claims: {test.claims_supported.join(', ')}</div>}
        </div>
      )}
    </section>
  )
}

function MailSection() {
  const { t } = useTranslation()
  const { showError, showSuccess } = useSnackbar()
  const [cfg, setCfg] = useState<MailSettings | null>(null)
  const [password, setPassword] = useState('')
  const [testTo, setTestTo] = useState('')
  const [busy, setBusy] = useState(false)

  const load = useCallback(async () => {
    try {
      const { data } = await apiService.getMailSettings()
      setCfg(data as MailSettings)
    } catch (e: any) {
      showError(extractApiError(e, t('authSettings.loadFailed')))
    }
  }, [showError, t])
  useEffect(() => { load() }, [load])

  if (!cfg) return null

  const save = async () => {
    setBusy(true)
    try {
      const payload: any = { host: cfg.host, port: cfg.port, security: cfg.security, username: cfg.username,
        from_address: cfg.from_address, from_name: cfg.from_name }
      if (password) payload.password = password
      const { data } = await apiService.putMailSettings(payload)
      setCfg(data as MailSettings)
      setPassword('')
      showSuccess(t('authSettings.saved'))
    } catch (e: any) {
      showError(extractApiError(e, t('authSettings.saveFailed')))
    } finally {
      setBusy(false)
    }
  }

  const sendTest = async () => {
    setBusy(true)
    try {
      await apiService.testMail(testTo)
      showSuccess(t('authSettings.mailTestSent'))
    } catch (e: any) {
      showError(extractApiError(e, t('authSettings.mailTestFailed')))
    } finally {
      setBusy(false)
    }
  }

  return (
    <section className="space-y-4">
      <div className="flex items-center gap-2 text-base font-semibold"><Mail size={18} /> {t('authSettings.mailTitle')}</div>
      <p className="text-sm text-[var(--text-dim)]">{t('authSettings.mailHelp')}</p>
      <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
        <label className="flex flex-col gap-1 md:col-span-2">
          <span className={label}>{t('authSettings.smtpHost')}</span>
          <input className={input} placeholder="smtp.example.com" value={cfg.host} onChange={(e) => setCfg({ ...cfg, host: e.target.value })} />
        </label>
        <label className="flex flex-col gap-1">
          <span className={label}>{t('authSettings.smtpPort')}</span>
          <input className={input} type="number" min={1} max={65535} value={cfg.port} onChange={(e) => setCfg({ ...cfg, port: Number(e.target.value) || 587 })} />
        </label>
        <label className="flex flex-col gap-1">
          <span className={label}>{t('authSettings.smtpSecurity')}</span>
          <select className={input} value={cfg.security} onChange={(e) => setCfg({ ...cfg, security: e.target.value as MailSettings['security'] })}>
            <option value="starttls">STARTTLS (587)</option>
            <option value="tls">TLS (465)</option>
            <option value="none">{t('authSettings.smtpNone')}</option>
          </select>
        </label>
        <label className="flex flex-col gap-1">
          <span className={label}>{t('authSettings.smtpUser')}</span>
          <input className={input} autoComplete="off" value={cfg.username} onChange={(e) => setCfg({ ...cfg, username: e.target.value })} />
        </label>
        <label className="flex flex-col gap-1">
          <span className={label}>{t('authSettings.smtpPassword')} {cfg.has_password ? `(${t('authSettings.secretStored')})` : ''}</span>
          <input className={input} type="password" autoComplete="new-password" value={password} placeholder={cfg.has_password ? '••••••••' : ''} onChange={(e) => setPassword(e.target.value)} />
        </label>
        <label className="flex flex-col gap-1 md:col-span-2">
          <span className={label}>{t('authSettings.fromAddress')}</span>
          <input className={input} type="email" placeholder="nvr@example.com" value={cfg.from_address} onChange={(e) => setCfg({ ...cfg, from_address: e.target.value })} />
        </label>
        <label className="flex flex-col gap-1">
          <span className={label}>{t('authSettings.fromName')}</span>
          <input className={input} value={cfg.from_name} onChange={(e) => setCfg({ ...cfg, from_name: e.target.value })} />
        </label>
      </div>
      <div className="flex flex-wrap items-center gap-2">
        <button className={primary} disabled={busy} onClick={save}>{busy && <Loader2 size={14} className="animate-spin" />}{t('common.save')}</button>
        <input className="bg-[var(--bg-2)] border border-[var(--border)] px-3 py-2 text-sm w-64" type="email" placeholder={t('authSettings.testTo')} value={testTo} onChange={(e) => setTestTo(e.target.value)} />
        <button className={secondary} disabled={busy || !testTo || !cfg.configured} onClick={sendTest}>{t('authSettings.sendTest')}</button>
      </div>
    </section>
  )
}
