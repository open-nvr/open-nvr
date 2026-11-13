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

import { useEffect, useMemo, useState } from 'react'
import { KeyRound, Trash2, UserCheck, UserPlus, UserCog } from 'lucide-react'
import { apiService } from '../../lib/apiService'
import { extractApiError } from '../../lib/apiError'
import { useAuth } from '../../auth/AuthContext'
import { useTranslation } from '../../i18n'
import { Modal } from '../../components/Modal'

type User = {
  id: number
  username: string
  email: string
  first_name?: string
  last_name?: string
  is_active: boolean
  is_superuser: boolean
  role_id: number
  mfa_enabled?: boolean
  auth_provider?: string | null
}

// A single sign-on account's identity provider owns its MFA: nothing to reset here.
const isSsoOnly = (u: User) => u.auth_provider === 'oidc' && !u.mfa_enabled

type UserForm = {
  username: string
  email: string
  first_name?: string
  last_name?: string
  password?: string
  role_id: number
  is_active?: boolean
  is_superuser?: boolean
}

type Role = {
  id: number
  name: string
  description?: string
}

export function UsersManager() {
  const { t } = useTranslation()
  const { user: me } = useAuth()
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [users, setUsers] = useState<User[]>([])
  const [total, setTotal] = useState(0)
  const [userView, setUserView] = useState<'active' | 'deactivated'>('active')
  const [limit, setLimit] = useState(20)
  const [page, setPage] = useState(1)
  const skip = useMemo(() => (page - 1) * limit, [page, limit])

  const [editing, setEditing] = useState<User | null>(null)
  const [showCreateDialog, setShowCreateDialog] = useState(false)
  const [showEditDialog, setShowEditDialog] = useState(false)
  // Permanent deletion and reactivation require the admin's TOTP code.
  const [confirmAction, setConfirmAction] = useState<{ kind: 'delete' | 'permanent-delete' | 'activate' | 'reset-mfa'; user: User } | null>(null)
  const [mfaCode, setMfaCode] = useState('')

  const [form, setForm] = useState<UserForm>({ username: '', email: '', password: '', role_id: 1, first_name: '', last_name: '', is_superuser: false })
  // TOTP code for the MFA-gated writes inside the create/edit dialogs
  // (creating a superuser, promoting or demoting one).
  const [formMfaCode, setFormMfaCode] = useState('')
  const [roles, setRoles] = useState<Role[]>([])

  const canAdmin = !!me?.is_superuser
  const isActiveView = userView === 'active'

  useEffect(() => {
    if (!canAdmin) return
    ;(async () => {
      try {
        setLoading(true)
        setError(null)
  const { data } = await apiService.getUsers({ skip, limit, active_only: false, is_active: isActiveView })
  setUsers(data.users)
  setTotal(data.total ?? 0)
  // Load roles for dropdown
  const rolesRes = await apiService.getRoles()
  setRoles((rolesRes.data && (rolesRes.data as any).roles) ? (rolesRes.data as any).roles : (Array.isArray(rolesRes.data) ? rolesRes.data : []))
      } catch (e: any) {
        setError(extractApiError(e, 'Failed to load users'))
      } finally {
        setLoading(false)
      }
    })()
  }, [canAdmin, skip, limit, isActiveView])

  const resetForm = () => { setForm({ username: '', email: '', password: '', role_id: 1, first_name: '', last_name: '', is_superuser: false }); setFormMfaCode('') }

  const onCreate = async (e: React.FormEvent) => {
    e.preventDefault()
    try {
      setLoading(true)
      setError(null)
      const payload: any = {
        username: form.username,
        email: form.email,
        password: form.password,
        role_id: Number(form.role_id),
        first_name: form.first_name || null,
        last_name: form.last_name || null,
        is_active: true,
        is_superuser: !!form.is_superuser,
      }
      await apiService.createUser(payload, form.is_superuser ? formMfaCode.trim() : undefined)
      setShowCreateDialog(false)
      resetForm()
      // refresh
  const { data } = await apiService.getUsers({ skip, limit, active_only: false, is_active: isActiveView })
  setUsers(data.users)
  setTotal(data.total ?? 0)
    } catch (e: any) {
      setError(extractApiError(e, 'Failed to create user'))
    } finally {
      setLoading(false)
    }
  }

  const onUpdate = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!editing) return
    try {
      setLoading(true)
      setError(null)
      const payload: any = {
        username: form.username,
        email: form.email,
        first_name: form.first_name || null,
        last_name: form.last_name || null,
        role_id: Number(form.role_id),
        is_active: form.is_active,
      }
      const superuserChanged = !!form.is_superuser !== !!editing.is_superuser
      if (superuserChanged) payload.is_superuser = !!form.is_superuser
      // An admin password reset rides the same update; MFA-gated like a promotion.
      const passwordReset = !!(form.password && form.password.length > 0)
      if (passwordReset) payload.password = form.password
      // Remove undefined fields
      Object.keys(payload).forEach((k) => payload[k] === undefined && delete payload[k])
      await apiService.updateUser(editing.id, payload, superuserChanged || passwordReset ? formMfaCode.trim() : undefined)
      setShowEditDialog(false)
      setEditing(null)
      resetForm()
  const { data } = await apiService.getUsers({ skip, limit, active_only: false, is_active: isActiveView })
  setUsers(data.users)
  setTotal(data.total ?? 0)
    } catch (e: any) {
      setError(extractApiError(e, 'Failed to update user'))
    } finally {
      setLoading(false)
    }
  }

  const onConfirmAction = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!confirmAction) return
    try {
      setLoading(true)
      setError(null)
      if (confirmAction.kind === 'delete') {
        await apiService.deleteUser(confirmAction.user.id)
      } else if (confirmAction.kind === 'permanent-delete') {
        await apiService.permanentlyDeleteUser(confirmAction.user.id, mfaCode.trim())
      } else if (confirmAction.kind === 'reset-mfa') {
        await apiService.resetUserMfa(confirmAction.user.id, mfaCode.trim())
      } else {
        await apiService.activateUser(confirmAction.user.id, mfaCode.trim())
      }
      setConfirmAction(null)
      setMfaCode('')
      const { data } = await apiService.getUsers({ skip, limit, active_only: false, is_active: isActiveView })
      setUsers(data.users)
      setTotal(data.total ?? 0)
    } catch (e: any) {
      const fallback = confirmAction.kind === 'delete'
        ? 'Failed to delete user'
        : confirmAction.kind === 'permanent-delete'
          ? 'Failed to permanently delete user'
          : confirmAction.kind === 'reset-mfa'
            ? 'Failed to reset two-factor sign-in'
            : 'Failed to activate user'
      setError(extractApiError(e, fallback))
    } finally {
      setLoading(false)
    }
  }

  const startEdit = (u: User) => {
    setEditing(u)
    setShowCreateDialog(false)
    setError(null)
    setForm({
      username: u.username,
      email: u.email,
      first_name: u.first_name || '',
      last_name: u.last_name || '',
      role_id: u.role_id,
      is_active: u.is_active,
      is_superuser: u.is_superuser,
    })
    setFormMfaCode('')
    setShowEditDialog(true)
  }

  if (!canAdmin) {
    return <div className="text-sm text-[var(--warn)]">{t('admin.only')}</div>
  }

  const totalPages = Math.max(1, Math.ceil(total / limit))
  const hasNext = page < totalPages

  return (
    <div className="space-y-3">
      <div className="flex items-center gap-2">
        <h2 className="text-base font-semibold">{t('admin.users')}</h2>
        <div className="ml-auto flex items-center gap-2 text-sm">
          <select className="bg-[var(--panel-2)] border border-[var(--border)] px-2 py-1" value={limit} onChange={(e) => { setPage(1); setLimit(Number(e.target.value)) }}>
            {[10, 20, 50].map(n => <option key={n} value={n}>{n}/page</option>)}
          </select>
          <button className="px-2 py-1 bg-[var(--accent)] text-white" onClick={() => { setShowCreateDialog(true); setEditing(null); resetForm(); setError(null) }}>{t('admin.addUser')}</button>
        </div>
      </div>

      <div className="flex gap-1 border-b border-[var(--border)]" role="tablist" aria-label="User status">
        {(['active', 'deactivated'] as const).map((view) => (
          <button
            key={view}
            type="button"
            role="tab"
            aria-selected={userView === view}
            className={`px-4 py-2 text-sm border-b-2 ${userView === view ? 'border-[var(--accent)] text-[var(--accent)] font-semibold' : 'border-transparent text-[var(--text-dim)] hover:text-[var(--text)]'}`}
            onClick={() => { setPage(1); setUserView(view); setError(null) }}
          >
            {view === 'active' ? 'Active users' : 'Deactivated users'}
          </button>
        ))}
      </div>

      {error && <div className="text-sm text-[var(--danger)]">{error}</div>}

      {/* Create User Dialog */}
      {showCreateDialog && (
        <Modal
          open
          title={<><UserPlus size={18} /> {t('admin.addUser')}</>}
          onClose={() => { setShowCreateDialog(false); resetForm(); setError(null) }}
          widthClassName="w-[calc(100vw-2rem)] max-w-lg"
          bodyClassName="p-0 flex flex-col"
          closeOnBackdrop={false}
        >
        <form onSubmit={onCreate} className="flex flex-col flex-1 min-h-0">
          <div className="p-4 overflow-auto flex-1 space-y-4">
            {error && (
              <div className="p-2 bg-[color-mix(in_oklab,var(--danger)_14%,transparent)] border border-[color-mix(in_oklab,var(--danger)_45%,var(--border))] text-[var(--danger)] text-sm">{error}</div>
            )}
            <div className="grid grid-cols-2 gap-3">
              <label className="flex flex-col gap-1">
                <span className="text-xs text-[var(--text-dim)]">Username *</span>
                <input type="text" className="bg-[var(--bg-2)] border border-[var(--border)] px-3 py-2 text-sm" placeholder="e.g., jsmith" value={form.username} onChange={(e) => setForm({ ...form, username: e.target.value })} required minLength={3} />
                <span className="text-[10px] text-[var(--text-dim)]">At least 3 characters.</span>
              </label>
              <label className="flex flex-col gap-1">
                <span className="text-xs text-[var(--text-dim)]">Email *</span>
                <input type="email" className="bg-[var(--bg-2)] border border-[var(--border)] px-3 py-2 text-sm" placeholder="user@example.com" value={form.email} onChange={(e) => setForm({ ...form, email: e.target.value })} required />
              </label>
              <label className="flex flex-col gap-1">
                <span className="text-xs text-[var(--text-dim)]">First Name</span>
                <input type="text" className="bg-[var(--bg-2)] border border-[var(--border)] px-3 py-2 text-sm" value={form.first_name || ''} onChange={(e) => setForm({ ...form, first_name: e.target.value })} />
              </label>
              <label className="flex flex-col gap-1">
                <span className="text-xs text-[var(--text-dim)]">Last Name</span>
                <input type="text" className="bg-[var(--bg-2)] border border-[var(--border)] px-3 py-2 text-sm" value={form.last_name || ''} onChange={(e) => setForm({ ...form, last_name: e.target.value })} />
              </label>
              <label className="flex flex-col gap-1">
                <span className="text-xs text-[var(--text-dim)]">Password *</span>
                <input type="password" className="bg-[var(--bg-2)] border border-[var(--border)] px-3 py-2 text-sm" value={form.password || ''} onChange={(e) => setForm({ ...form, password: e.target.value })} required minLength={8} />
                <span className="text-[10px] text-[var(--text-dim)]">At least 8 characters, with an uppercase letter, a lowercase letter, and a number.</span>
              </label>
              <label className="flex flex-col gap-1">
                <span className="text-xs text-[var(--text-dim)]">Role *</span>
                <select className="bg-[var(--bg-2)] border border-[var(--border)] px-3 py-2 text-sm" value={form.role_id} onChange={(e) => setForm({ ...form, role_id: Number(e.target.value) })} required>
                  {roles.map(r => <option key={r.id} value={r.id}>{r.name}</option>)}
                </select>
              </label>
              <label className="flex items-center gap-2 mt-5 text-sm" title="A superuser holds every permission and sees every camera, regardless of role">
                <input type="checkbox" className="accent-[var(--accent)]" checked={!!form.is_superuser} onChange={(e) => setForm({ ...form, is_superuser: e.target.checked })} /> Superuser
              </label>
              {form.is_superuser && (
                <label className="flex flex-col gap-1">
                  <span className="text-xs text-[var(--text-dim)]">Your MFA code * (required to create a superuser)</span>
                  <input className="bg-[var(--bg-2)] border border-[var(--border)] px-3 py-2 text-sm font-mono" inputMode="numeric" maxLength={6} value={formMfaCode} onChange={(e) => setFormMfaCode(e.target.value.replace(/\D/g, ''))} required />
                </label>
              )}
            </div>
          </div>
          <div className="flex items-center justify-end gap-2 p-4 border-t border-[var(--border)]">
            <button type="button" className="px-4 py-2 text-sm border border-[var(--border)] hover:bg-[var(--panel-2)]" onClick={() => { setShowCreateDialog(false); resetForm(); setError(null) }}>{t('admin.cancel')}</button>
            <button type="submit" className="px-4 py-2 text-sm bg-[var(--accent)] text-white disabled:opacity-50" disabled={loading || (!!form.is_superuser && formMfaCode.length !== 6)}>{loading ? t('common.loading') : t('admin.createUser')}</button>
          </div>
        </form>
        </Modal>
      )}

      {/* Edit User Dialog */}
      {showEditDialog && editing && (
        <Modal
          open
          title={<><UserCog size={18} /> {t('admin.editUser')}: {editing.username}</>}
          onClose={() => { setShowEditDialog(false); setEditing(null); resetForm(); setError(null) }}
          widthClassName="w-[calc(100vw-2rem)] max-w-lg"
          bodyClassName="p-0 flex flex-col"
          closeOnBackdrop={false}
        >
        <form onSubmit={onUpdate} className="flex flex-col flex-1 min-h-0">
          <div className="p-4 overflow-auto flex-1 space-y-4">
            {error && (
              <div className="p-2 bg-[color-mix(in_oklab,var(--danger)_14%,transparent)] border border-[color-mix(in_oklab,var(--danger)_45%,var(--border))] text-[var(--danger)] text-sm">{error}</div>
            )}
            <div className="grid grid-cols-2 gap-3">
              <label className="flex flex-col gap-1">
                <span className="text-xs text-[var(--text-dim)]">Username</span>
                <input type="text" className="bg-[var(--bg-2)] border border-[var(--border)] px-3 py-2 text-sm opacity-50" value={form.username} disabled />
              </label>
              <label className="flex flex-col gap-1">
                <span className="text-xs text-[var(--text-dim)]">Email *</span>
                <input type="email" className="bg-[var(--bg-2)] border border-[var(--border)] px-3 py-2 text-sm" value={form.email} onChange={(e) => setForm({ ...form, email: e.target.value })} required />
              </label>
              <label className="flex flex-col gap-1">
                <span className="text-xs text-[var(--text-dim)]">First Name</span>
                <input type="text" className="bg-[var(--bg-2)] border border-[var(--border)] px-3 py-2 text-sm" value={form.first_name || ''} onChange={(e) => setForm({ ...form, first_name: e.target.value })} />
              </label>
              <label className="flex flex-col gap-1">
                <span className="text-xs text-[var(--text-dim)]">Last Name</span>
                <input type="text" className="bg-[var(--bg-2)] border border-[var(--border)] px-3 py-2 text-sm" value={form.last_name || ''} onChange={(e) => setForm({ ...form, last_name: e.target.value })} />
              </label>
              <label className="flex flex-col gap-1">
                <span className="text-xs text-[var(--text-dim)]">Role *</span>
                <select className="bg-[var(--bg-2)] border border-[var(--border)] px-3 py-2 text-sm" value={form.role_id} onChange={(e) => setForm({ ...form, role_id: Number(e.target.value) })} required>
                  {roles.map(r => <option key={r.id} value={r.id}>{r.name}</option>)}
                </select>
              </label>
              <label className="flex items-center gap-2 mt-5 text-sm" title={me?.id === editing.id ? 'You cannot deactivate your own account' : !editing.is_active ? 'Use the Activate button to reactivate (requires MFA code)' : undefined}>
                <input type="checkbox" className="accent-[var(--accent)]" checked={!!form.is_active} onChange={(e) => setForm({ ...form, is_active: e.target.checked })} disabled={me?.id === editing.id || !editing.is_active} /> Active
              </label>
              <label className="flex items-center gap-2 mt-5 text-sm" title={me?.id === editing.id ? 'You cannot change your own superuser status' : 'A superuser holds every permission and sees every camera, regardless of role'}>
                <input type="checkbox" className="accent-[var(--accent)]" checked={!!form.is_superuser} onChange={(e) => setForm({ ...form, is_superuser: e.target.checked })} disabled={me?.id === editing.id} /> Superuser
              </label>
              <label className="flex flex-col gap-1 col-span-2">
                <span className="text-xs text-[var(--text-dim)]">{t('admin.newPassword')}</span>
                <input type="password" autoComplete="new-password" className="bg-[var(--bg-2)] border border-[var(--border)] px-3 py-2 text-sm" placeholder={t('admin.newPasswordHint')} value={form.password || ''} onChange={(e) => setForm({ ...form, password: e.target.value })} minLength={8} />
              </label>
              {(!!form.is_superuser !== !!editing.is_superuser || !!form.password) && (
                <label className="flex flex-col gap-1">
                  <span className="text-xs text-[var(--text-dim)]">Your MFA code * (required to {form.password ? 'reset the password' : form.is_superuser ? 'promote' : 'demote'})</span>
                  <input className="bg-[var(--bg-2)] border border-[var(--border)] px-3 py-2 text-sm font-mono" inputMode="numeric" maxLength={6} value={formMfaCode} onChange={(e) => setFormMfaCode(e.target.value.replace(/\D/g, ''))} required />
                </label>
              )}
            </div>
          </div>
          <div className="flex items-center justify-end gap-2 p-4 border-t border-[var(--border)]">
            <button type="button" className="px-4 py-2 text-sm border border-[var(--border)] hover:bg-[var(--panel-2)]" onClick={() => { setShowEditDialog(false); setEditing(null); resetForm(); setError(null) }}>{t('admin.cancel')}</button>
            <button type="submit" className="px-4 py-2 text-sm bg-[var(--accent)] text-white disabled:opacity-50" disabled={loading || ((!!form.is_superuser !== !!editing.is_superuser || !!form.password) && formMfaCode.length !== 6)}>{loading ? t('common.loading') : t('admin.updateUser')}</button>
          </div>
        </form>
        </Modal>
      )}

      {/* Permanent deletion and reactivation are MFA-gated; soft deletion is reversible. */}
      {confirmAction && (
        <Modal
          open
          title={<span className={`font-semibold flex items-center gap-2 ${confirmAction.kind === 'activate' ? 'text-[var(--ok)]' : 'text-[var(--danger)]'}`}>{confirmAction.kind === 'activate' ? <UserCheck size={18} /> : confirmAction.kind === 'reset-mfa' ? <KeyRound size={18} /> : <Trash2 size={18} />} {confirmAction.kind === 'activate' ? 'Reactivate user?' : confirmAction.kind === 'reset-mfa' ? 'Reset two-factor sign-in?' : confirmAction.kind === 'delete' ? 'Delete user?' : 'Delete permanently?'}</span>}
          onClose={() => { setConfirmAction(null); setMfaCode(''); setError(null) }}
          widthClassName="w-[calc(100vw-2rem)] max-w-md"
          bodyClassName="p-0 flex flex-col"
          closeOnBackdrop={false}
        >
        <form onSubmit={onConfirmAction} className="flex flex-col flex-1 min-h-0">
          <div className="p-4 overflow-auto flex-1 space-y-4">
            {error && (
              <div className="p-2 bg-[color-mix(in_oklab,var(--danger)_14%,transparent)] border border-[color-mix(in_oklab,var(--danger)_45%,var(--border))] text-[var(--danger)] text-sm">{error}</div>
            )}
            <div className="p-3 bg-[var(--bg-2)] border border-[var(--border)] text-sm">
              {confirmAction.kind === 'delete' ? (
                <>Delete <span className="font-semibold">{confirmAction.user.username}</span> ({confirmAction.user.email})? This will deactivate their account, so they can no longer sign in. You can reactivate them later or permanently delete them from Deactivated users.</>
              ) : confirmAction.kind === 'permanent-delete' ? (
                <>Permanently delete <span className="font-semibold">{confirmAction.user.username}</span> ({confirmAction.user.email})? This cannot be undone. Required records must be resolved first, and historical audit records will be retained.</>
              ) : confirmAction.kind === 'reset-mfa' ? (
                <>Reset two-factor sign-in for <span className="font-semibold">{confirmAction.user.username}</span> ({confirmAction.user.email})? They are signed out everywhere, and at their next sign-in they set up a new authenticator app. Until they do, their password alone gets them to that setup screen, so do this when they are ready to sign in. API tokens they created keep working; revoke those under API Tokens if needed.</>
              ) : (
                <>You are about to reactivate <span className="font-semibold">{confirmAction.user.username}</span> ({confirmAction.user.email}). They will be able to log in again.</>
              )}
            </div>
            {confirmAction.kind !== 'delete' && (
              <label className="flex flex-col gap-1">
                <span className="text-xs text-[var(--text-dim)]">Your MFA code *</span>
                <input
                  type="text"
                  inputMode="numeric"
                  autoComplete="one-time-code"
                  maxLength={6}
                  className="bg-[var(--bg-2)] border border-[var(--border)] px-3 py-2 text-sm tracking-widest"
                  placeholder="123456"
                  value={mfaCode}
                  onChange={(e) => setMfaCode(e.target.value.replace(/\D/g, ''))}
                  autoFocus
                  required
                />
                <span className="text-[10px] text-[var(--text-dim)]">Enter the 6-digit code from your authenticator app to confirm this action.</span>
              </label>
            )}
          </div>
          <div className="flex items-center justify-end gap-2 p-4 border-t border-[var(--border)]">
            <button type="button" className="px-4 py-2 text-sm border border-[var(--border)] hover:bg-[var(--panel-2)]" onClick={() => { setConfirmAction(null); setMfaCode(''); setError(null) }}>Cancel</button>
            <button type="submit" className={`px-4 py-2 text-sm text-white disabled:opacity-50 ${confirmAction.kind === 'activate' ? 'bg-[var(--ok)] hover:bg-[color-mix(in_oklab,var(--ok)_14%,transparent)]' : 'bg-[var(--critical)] hover:bg-[var(--critical)]'}`} disabled={loading || (confirmAction.kind !== 'delete' && mfaCode.length !== 6)}>
              {confirmAction.kind === 'activate'
                ? (loading ? 'Reactivating...' : 'Reactivate user')
                : confirmAction.kind === 'delete'
                  ? (loading ? 'Deleting...' : 'Delete user')
                  : confirmAction.kind === 'reset-mfa'
                    ? (loading ? 'Resetting...' : 'Reset 2FA')
                    : (loading ? 'Deleting...' : 'Permanently Delete User')}
            </button>
          </div>
        </form>
        </Modal>
      )}

      <div className="overflow-auto border border-[var(--border)]">
        <table className="w-full text-sm">
          <thead className="bg-[var(--panel-2)] text-left">
            <tr>
              <th className="p-2">{t('admin.username')}</th>
              <th className="p-2">{t('admin.email')}</th>
              <th className="p-2">{t('admin.role')}</th>
              <th className="p-2">{t('admin.active')}</th>
              <th className="p-2">{t('admin.superuser')}</th>
              <th className="p-2">2FA</th>
              <th className="p-2">{t('admin.actions')}</th>
            </tr>
          </thead>
          <tbody>
            {users.map((u) => (
              <tr key={u.id} className="odd:bg-[var(--bg-2)] even:bg-[var(--panel)]">
                <td className="p-2">{u.username}</td>
                <td className="p-2">{u.email}</td>
                <td className="p-2">{roles.find(r => r.id === u.role_id)?.name ?? u.role_id}</td>
                <td className="p-2">
                  <span className={`inline-flex items-center text-xs px-2 py-0.5  ${u.is_active ? 'bg-[color-mix(in_oklab,var(--ok)_14%,transparent)] text-[var(--ok)]' : 'bg-[var(--panel-2)] text-[var(--text-dim)]'}`}>
                    {u.is_active ? t('admin.active') : t('admin.inactive')}
                  </span>
                </td>
                <td className="p-2">{u.is_superuser ? t('common.yes') : t('common.no')}</td>
                <td className="p-2 text-xs">{isSsoOnly(u) ? <span className="text-[var(--text-dim)]">SSO</span> : u.mfa_enabled ? 'Enrolled' : <span className="text-[var(--text-dim)]">Not set up</span>}</td>
                <td className="p-2 space-x-2">
                  <button className="px-2 py-1 border border-[var(--border)] bg-[var(--panel-2)]" onClick={() => startEdit(u)}>{t('admin.edit')}</button>
                  {isActiveView ? (
                    me?.id !== u.id && (
                      <button className="px-2 py-1 border border-[color-mix(in_oklab,var(--danger)_45%,var(--border))] text-[var(--danger)]" onClick={() => { setConfirmAction({ kind: 'delete', user: u }); setMfaCode(''); setError(null) }}>Delete</button>
                    )
                  ) : (
                    <button className="px-2 py-1 border border-[color-mix(in_oklab,var(--ok)_45%,var(--border))] bg-[color-mix(in_oklab,var(--ok)_14%,transparent)] text-[var(--ok)]" onClick={() => { setConfirmAction({ kind: 'activate', user: u }); setMfaCode(''); setError(null) }}>{t('admin.activateUser')}</button>
                  )}
                  {me?.id !== u.id && u.mfa_enabled && !isSsoOnly(u) && (
                    <button className="px-2 py-1 border border-[var(--border)] bg-[var(--panel-2)]" title="Remove their authenticator; they set up a new one at next sign-in" onClick={() => { setConfirmAction({ kind: 'reset-mfa', user: u }); setMfaCode(''); setError(null) }}>Reset 2FA</button>
                  )}
                  {!isActiveView && me?.id !== u.id && (
                    <button className="px-2 py-1 border border-[color-mix(in_oklab,var(--danger)_45%,var(--border))] text-[var(--danger)]" onClick={() => { setConfirmAction({ kind: 'permanent-delete', user: u }); setMfaCode(''); setError(null) }}>Delete permanently</button>
                  )}
                </td>
              </tr>
            ))}
            {users.length === 0 && (
              <tr>
                <td colSpan={7} className="p-3 text-center text-[var(--text-dim)]">{t('admin.noUsers')}</td>
              </tr>
            )}
          </tbody>
        </table>
      </div>

      <div className="flex items-center gap-2 text-sm">
        <button className="px-2 py-1 border border-[var(--border)] bg-[var(--panel-2)]" disabled={page <= 1} onClick={() => setPage((p) => Math.max(1, p - 1))}>{t('admin.previous')}</button>
        <span>{t('admin.page')} {page} / {totalPages}</span>
        <button className="px-2 py-1 border border-[var(--border)] bg-[var(--panel-2)]" disabled={!hasNext} onClick={() => setPage((p) => p + 1)}>{t('admin.next')}</button>
      </div>
    </div>
  )
}
