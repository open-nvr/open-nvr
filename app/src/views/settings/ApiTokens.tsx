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

import { useCallback, useEffect, useState } from 'react'
import { useSnackbar } from '../../components/Snackbar'
import { Badge, Button, ErrorCard, Skeleton } from '../../components/ui'
import { apiService } from '../../lib/apiService'
import { extractApiError } from '../../lib/apiError'
import { usePermissions } from '../../hooks/usePermissions'
import { useTranslation } from '../../i18n'

type ApiToken = {
  id: number
  name: string
  prefix: string
  owner_user_id: number
  scopes: string[]
  camera_ids: number[] | null
  allowed_cidrs: string[] | null
  expires_at: string | null
  created_at: string | null
  last_used_at: string | null
  last_used_ip: string | null
  revoked_at: string | null
}

type Camera = { id: number; name: string }

// Mirrors services/api_tokens.ALLOWED_SCOPES on the server, which has the
// final say: it refuses any scope not in its list or not held by the creator.
const SCOPES = [
  'cameras.view', 'live.view', 'recordings.view', 'alerts.view', 'settings.view',
  'alerts.manage', 'ptz.control', 'cameras.manage', 'events.create', 'apps.view',
  'apps.actions', 'recordings.pause', 'settings.manage',
] as const

// What a Home Assistant install needs for cameras, sensors and notifications.
const HA_PRESET = ['cameras.view', 'live.view', 'recordings.view', 'alerts.view', 'settings.view']

const EXPIRY_OPTIONS = [0, 30, 90, 365]

const inputCls = 'w-full bg-[var(--panel)] border border-[var(--border)] px-3 py-2 rounded text-sm'

function tokenStatus(tok: ApiToken): 'active' | 'revoked' | 'expired' {
  if (tok.revoked_at) return 'revoked'
  if (tok.expires_at && new Date(tok.expires_at).getTime() <= Date.now()) return 'expired'
  return 'active'
}

const STATUS_BADGE = { active: 'success', revoked: 'neutral', expired: 'warning' } as const

/**
 * Long-lived API tokens for Home Assistant and other clients.
 *
 * A token can only reach the API routes listed for tokens on the server, only
 * with the scopes it was given, and never with more than its creator holds.
 * The secret is shown once, right after creation; only its hash is stored.
 */
export function ApiTokens() {
  const { t } = useTranslation()
  const { showSuccess, showError } = useSnackbar()
  const { hasPermission, loading: permsLoading } = usePermissions()
  const canManage = hasPermission('api_tokens.manage')

  const [tokens, setTokens] = useState<ApiToken[]>([])
  const [cameras, setCameras] = useState<Camera[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState('')
  const [creating, setCreating] = useState(false)
  const [secret, setSecret] = useState<{ id: number; name: string; token: string } | null>(null)

  const [name, setName] = useState('')
  const [scopes, setScopes] = useState<string[]>([])
  const [allCameras, setAllCameras] = useState(true)
  const [cameraIds, setCameraIds] = useState<number[]>([])
  const [cidrs, setCidrs] = useState('')
  const [expiryDays, setExpiryDays] = useState(0)

  // Only the first load shows the skeleton: reloading after a create must not
  // unmount the one-time secret.
  const load = useCallback(async () => {
    setError('')
    try {
      const [tok, cams] = await Promise.all([apiService.listApiTokens(), apiService.getCameras()])
      setTokens(tok.data.tokens || [])
      const list = Array.isArray(cams.data) ? cams.data : cams.data?.cameras || []
      setCameras(list.map((c: any) => ({ id: c.id, name: c.name })))
    } catch (e: any) {
      setError(extractApiError(e, t('apiTokens.failedLoad')))
    } finally {
      setLoading(false)
    }
  }, [t])

  useEffect(() => {
    if (permsLoading) return
    if (canManage) load()
    else setLoading(false)
  }, [permsLoading, canManage, load])

  const resetForm = () => {
    setName('')
    setScopes(HA_PRESET.filter((s) => hasPermission(s)))
    setAllCameras(true)
    setCameraIds([])
    setCidrs('')
    setExpiryDays(0)
  }

  const openForm = () => {
    resetForm()
    setCreating(true)
  }

  const toggleIn = <T,>(list: T[], v: T) => (list.includes(v) ? list.filter((x) => x !== v) : [...list, v])

  const create = async () => {
    setBusy('create')
    try {
      const allowed = cidrs.split(/[\s,]+/).map((c) => c.trim()).filter(Boolean)
      const { data } = await apiService.createApiToken({
        name: name.trim(),
        scopes,
        camera_ids: allCameras ? null : cameraIds,
        allowed_cidrs: allowed.length ? allowed : null,
        expires_in_days: expiryDays || null,
      })
      setSecret({ id: data.id, name: data.name, token: data.token })
      setCreating(false)
      await load()
    } catch (e: any) {
      showError(extractApiError(e, t('apiTokens.failedCreate')))
    } finally {
      setBusy('')
    }
  }

  const revoke = async (tok: ApiToken) => {
    if (!window.confirm(t('apiTokens.confirmRevoke', { name: tok.name }))) return
    setBusy(String(tok.id))
    try {
      await apiService.revokeApiToken(tok.id)
      setSecret((cur) => (cur?.id === tok.id ? null : cur))
      showSuccess(t('apiTokens.revoked', { name: tok.name }))
      await load()
    } catch (e: any) {
      showError(extractApiError(e, t('apiTokens.failedRevoke')))
    } finally {
      setBusy('')
    }
  }

  const copy = async (text: string) => {
    try {
      // navigator.clipboard is unavailable on plain-HTTP LAN deployments.
      if (navigator.clipboard?.writeText) {
        await navigator.clipboard.writeText(text)
      } else {
        const ta = document.createElement('textarea')
        ta.value = text
        ta.style.position = 'fixed'
        ta.style.opacity = '0'
        document.body.appendChild(ta)
        ta.select()
        const ok = document.execCommand('copy')
        document.body.removeChild(ta)
        if (!ok) throw new Error('copy command rejected')
      }
      showSuccess(t('apiTokens.copied'))
    } catch {
      showError(t('apiTokens.copyFailed'))
    }
  }

  if (loading || permsLoading) return <Skeleton className="h-40 w-full" />
  if (!canManage) {
    return <p className="text-sm text-[var(--text-dim)]">{t('apiTokens.noPermission')}</p>
  }
  if (error) return <ErrorCard message={error} onRetry={load} />

  const cameraName = (id: number) => cameras.find((c) => c.id === id)?.name ?? `#${id}`
  const canSubmit =
    name.trim().length > 0 && scopes.length > 0 && (allCameras || cameraIds.length > 0) && busy !== 'create'

  return (
    <div className="space-y-4">
      <div className="flex items-start justify-between gap-4">
        <p className="text-xs text-[var(--text-dim)] max-w-2xl">{t('apiTokens.description')}</p>
        {!creating && (
          <Button variant="primary" data-testid="api-token-new" onClick={openForm}>
            {t('apiTokens.new')}
          </Button>
        )}
      </div>

      {secret && (
        <div className="rounded border border-emerald-600/40 bg-emerald-600/10 p-4 space-y-2">
          <div className="text-sm font-medium">{t('apiTokens.createdTitle', { name: secret.name })}</div>
          <p className="text-xs text-[var(--text-dim)]">{t('apiTokens.shownOnce')}</p>
          <div className="flex items-center gap-2">
            <code data-testid="api-token-secret" className="flex-1 font-mono text-xs break-all bg-[var(--panel)] border border-[var(--border)] rounded px-3 py-2 select-all">
              {secret.token}
            </code>
            <Button onClick={() => copy(secret.token)}>{t('apiTokens.copy')}</Button>
          </div>
          <div className="flex justify-end">
            <Button variant="outline" onClick={() => setSecret(null)}>
              {t('apiTokens.done')}
            </Button>
          </div>
        </div>
      )}

      {creating && (
        <div className="border border-[var(--border)] rounded p-4 space-y-4">
          <label className="block space-y-1">
            <span className="text-sm">{t('apiTokens.name')}</span>
            <input
              data-testid="api-token-name"
              className={inputCls}
              maxLength={64}
              value={name}
              placeholder={t('apiTokens.namePlaceholder')}
              onChange={(e) => setName(e.target.value)}
            />
          </label>

          <fieldset className="space-y-2">
            <legend className="text-sm">{t('apiTokens.scopes')}</legend>
            <div className="grid gap-1 sm:grid-cols-2">
              {SCOPES.map((s) => {
                const held = hasPermission(s)
                return (
                  <label key={s} className={`flex items-start gap-2 text-sm ${held ? '' : 'opacity-50'}`}>
                    <input
                      type="checkbox"
                      className="accent-[var(--accent)] mt-1"
                      disabled={!held}
                      checked={scopes.includes(s)}
                      onChange={() => setScopes(toggleIn(scopes, s))}
                    />
                    <span>
                      <span className="font-mono text-xs">{s}</span>
                      <span className="block text-xs text-[var(--text-dim)]">
                        {held ? t(`apiTokens.scope.${s}`) : t('apiTokens.scopeNotHeld')}
                      </span>
                    </span>
                  </label>
                )
              })}
            </div>
          </fieldset>

          <fieldset className="space-y-2">
            <legend className="text-sm">{t('apiTokens.cameras')}</legend>
            <label className="flex items-center gap-2 text-sm">
              <input type="radio" className="accent-[var(--accent)]" checked={allCameras} onChange={() => setAllCameras(true)} />
              {t('apiTokens.allCameras')}
            </label>
            <label className="flex items-center gap-2 text-sm">
              <input type="radio" className="accent-[var(--accent)]" checked={!allCameras} onChange={() => setAllCameras(false)} />
              {t('apiTokens.selectedCameras')}
            </label>
            {!allCameras && (
              <div className="grid gap-1 pl-6 sm:grid-cols-2">
                {cameras.map((c) => (
                  <label key={c.id} className="flex items-center gap-2 text-sm">
                    <input
                      type="checkbox"
                      className="accent-[var(--accent)]"
                      checked={cameraIds.includes(c.id)}
                      onChange={() => setCameraIds(toggleIn(cameraIds, c.id))}
                    />
                    {c.name}
                  </label>
                ))}
              </div>
            )}
          </fieldset>

          <div className="grid gap-4 sm:grid-cols-2">
            <label className="block space-y-1">
              <span className="text-sm">{t('apiTokens.allowedNetworks')}</span>
              <input
                className={inputCls}
                value={cidrs}
                placeholder="192.168.1.0/24"
                onChange={(e) => setCidrs(e.target.value)}
              />
              <span className="block text-xs text-[var(--text-dim)]">{t('apiTokens.allowedNetworksHint')}</span>
            </label>
            <label className="block space-y-1">
              <span className="text-sm">{t('apiTokens.expires')}</span>
              <select className={inputCls} value={expiryDays} onChange={(e) => setExpiryDays(Number(e.target.value))}>
                {EXPIRY_OPTIONS.map((d) => (
                  <option key={d} value={d}>
                    {d ? t('apiTokens.expiresInDays', { days: d }) : t('apiTokens.never')}
                  </option>
                ))}
              </select>
            </label>
          </div>

          <div className="flex justify-end gap-2">
            <Button variant="outline" onClick={() => setCreating(false)}>
              {t('apiTokens.cancel')}
            </Button>
            <Button variant="primary" data-testid="api-token-create" disabled={!canSubmit} onClick={create}>
              {busy === 'create' ? t('apiTokens.creating') : t('apiTokens.create')}
            </Button>
          </div>
        </div>
      )}

      <div className="overflow-auto border border-[var(--border)] rounded">
        <table className="w-full text-sm">
          <thead>
            <tr className="text-left text-[var(--text-dim)] border-b border-[var(--border)]">
              <th className="px-3 py-2 font-medium">{t('apiTokens.name')}</th>
              <th className="px-3 py-2 font-medium">{t('apiTokens.scopes')}</th>
              <th className="px-3 py-2 font-medium">{t('apiTokens.cameras')}</th>
              <th className="px-3 py-2 font-medium">{t('apiTokens.lastUsed')}</th>
              <th className="px-3 py-2 font-medium">{t('apiTokens.expires')}</th>
              <th className="px-3 py-2 font-medium">{t('apiTokens.status')}</th>
              <th className="px-3 py-2 font-medium text-right" />
            </tr>
          </thead>
          <tbody>
            {tokens.length === 0 && (
              <tr>
                <td colSpan={7} className="px-3 py-6 text-center text-[var(--text-dim)]">
                  {t('apiTokens.empty')}
                </td>
              </tr>
            )}
            {tokens.map((tok) => {
              const st = tokenStatus(tok)
              return (
                <tr key={tok.id} data-testid="api-token-row" className="border-b border-[var(--border)]/60 align-top">
                  <td className="px-3 py-2">
                    <div>{tok.name}</div>
                    <div className="text-xs text-[var(--text-dim)] font-mono">onvr_{tok.prefix}_…</div>
                  </td>
                  <td className="px-3 py-2">
                    <div className="flex flex-wrap gap-1">
                      {tok.scopes.map((s) => (
                        <span key={s} className="font-mono text-[11px] border border-[var(--border)] rounded px-1">
                          {s}
                        </span>
                      ))}
                    </div>
                  </td>
                  <td className="px-3 py-2 text-xs">
                    {tok.camera_ids === null ? t('apiTokens.all') : tok.camera_ids.map(cameraName).join(', ')}
                    {tok.allowed_cidrs && (
                      <div className="text-[var(--text-dim)] font-mono">{tok.allowed_cidrs.join(', ')}</div>
                    )}
                  </td>
                  <td className="px-3 py-2 text-xs text-[var(--text-dim)]">
                    {tok.last_used_at ? new Date(tok.last_used_at).toLocaleString() : t('apiTokens.neverUsed')}
                    {tok.last_used_ip && <div className="font-mono">{tok.last_used_ip}</div>}
                  </td>
                  <td className="px-3 py-2 text-xs text-[var(--text-dim)]">
                    {tok.expires_at ? new Date(tok.expires_at).toLocaleDateString() : t('apiTokens.never')}
                  </td>
                  <td className="px-3 py-2">
                    <Badge variant={STATUS_BADGE[st]}>{t(`apiTokens.status.${st}`)}</Badge>
                  </td>
                  <td className="px-3 py-2 text-right">
                    {st !== 'revoked' && (
                      <Button variant="danger" disabled={busy === String(tok.id)} onClick={() => revoke(tok)}>
                        {t('apiTokens.revoke')}
                      </Button>
                    )}
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>
    </div>
  )
}
