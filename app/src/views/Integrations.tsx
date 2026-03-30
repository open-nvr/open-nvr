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

import React, { useEffect, useState } from 'react'
import { apiService } from '../lib/apiService'
import { useAuth } from '../auth/AuthContext'
import { useSnackbar } from '../components/Snackbar'
import { Modal } from '../components/Modal'
import { Plus, Trash, Settings, Activity, CheckCircle, AlertCircle, Play, Plug } from 'lucide-react'

type IntegrationType = 'webhook' | 'slack' | 'teams' | 'email' | 'mqtt' | 's3' | 'syslog' | 'prometheus'

const INTEGRATION_TYPES: { value: IntegrationType; label: string }[] = [
  { value: 'webhook', label: 'Webhook' },
  { value: 'slack', label: 'Slack' },
  { value: 'teams', label: 'Microsoft Teams' },
  { value: 'email', label: 'Email (SMTP)' },
  { value: 'mqtt', label: 'MQTT' },
  { value: 's3', label: 'S3 Storage' },
  { value: 'syslog', label: 'Syslog / SIEM' },
  { value: 'prometheus', label: 'Prometheus' },
]

export function Integrations() {
  const { user } = useAuth()
  const canAdmin = !!user?.is_superuser
  const { showSuccess, showError } = useSnackbar()
  const [integrations, setIntegrations] = useState<any[]>([])
  const [loading, setLoading] = useState(false)
  const [testingId, setTestingId] = useState<number | null>(null)

  // Modal State
  const [showModal, setShowModal] = useState(false)
  const [editingId, setEditingId] = useState<number | null>(null)

  // Form State
  const [name, setName] = useState('')
  const [type, setType] = useState<IntegrationType>('webhook')
  const [enabled, setEnabled] = useState(true)
  const [config, setConfig] = useState<any>({})

  const fetchIntegrations = async () => {
    try {
      setLoading(true)
      const { data } = await apiService.getIntegrations()
      // API returns snake_case, but JS prefers it too usually if API does.
      // Based on schema, pydantic models return snake_case.
      setIntegrations(data || [])
    } catch (e: any) {
      console.error(e)
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    fetchIntegrations()
  }, [])

  const resetForm = () => {
    setName('')
    setType('webhook')
    setEnabled(true)
    setConfig({})
    setEditingId(null)
  }

  const openCreate = () => {
    resetForm()
    setShowModal(true)
  }

  const openEdit = (integration: any) => {
    setEditingId(integration.id)
    setName(integration.name)
    setType(integration.type)
    setEnabled(integration.enabled)
    setConfig(integration.config || {})
    setShowModal(true)
  }

  const handleDelete = async (id: number) => {
    if (!confirm('Are you sure you want to delete this integration?')) return
    try {
      await apiService.deleteIntegration(id)
      showSuccess('Integration deleted')
      fetchIntegrations()
    } catch (e: any) {
      showError(e.response?.data?.detail || 'Failed to delete integration')
    }
  }

  const handleTest = async (id: number) => {
    try {
      setTestingId(id)
      const { data } = await apiService.testIntegration(id)
      showSuccess(data.message || 'Test successful')
    } catch (e: any) {
      showError(e.response?.data?.detail || 'Test failed')
    } finally {
      setTestingId(null)
    }
  }

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    try {
      setLoading(true)
      const payload = {
        name,
        type,
        enabled,
        config
      }

      if (editingId) {
        await apiService.updateIntegration(editingId, payload)
        showSuccess('Integration updated')
      } else {
        await apiService.createIntegration(payload)
        showSuccess('Integration created')
      }
      setShowModal(false)
      fetchIntegrations()
    } catch (e: any) {
      showError(e.response?.data?.detail || 'Failed to save integration')
    } finally {
      setLoading(false)
    }
  }

  // Define dynamic form fields based on type
  const renderConfigFields = () => {
    switch (type) {
      case 'webhook':
        return (
          <>
            <Field label="Target URL">
              <input className="input" value={config.url || ''} onChange={e => setConfig({...config, url: e.target.value})} placeholder="https://api.example.com/hook" required />
            </Field>
            <Field label="Secret Token (Optional)">
              <input className="input" value={config.secret || ''} onChange={e => setConfig({...config, secret: e.target.value})} placeholder="x-auth-token" />
            </Field>
            <Field label="Events">
              <EventsMatrix value={config.events || {}} onChange={v => setConfig({...config, events: v})} />
            </Field>
          </>
        )
      case 'slack':
        return (
          <>
            <Field label="Webhook URL">
              <input className="input" value={config.webhook_url || ''} onChange={e => setConfig({...config, webhook_url: e.target.value})} placeholder="https://hooks.slack.com/services/..." required />
            </Field>
            <div className="grid grid-cols-2 gap-2">
              <Field label="Channel (Optional)">
                <input className="input" value={config.channel || ''} onChange={e => setConfig({...config, channel: e.target.value})} placeholder="#alerts" />
              </Field>
              <Field label="Mention (Optional)">
                <input className="input" value={config.mention || ''} onChange={e => setConfig({...config, mention: e.target.value})} placeholder="@user or @here" />
              </Field>
            </div>
            <Field label="Events">
              <EventsMatrix value={config.events || {}} onChange={v => setConfig({...config, events: v})} />
            </Field>
          </>
        )
      case 'teams':
        return (
          <>
            <Field label="Webhook URL">
              <input className="input" value={config.webhook_url || ''} onChange={e => setConfig({...config, webhook_url: e.target.value})} placeholder="https://outlook.office.com/webhook/..." required />
            </Field>
            <Field label="Events">
              <EventsMatrix value={config.events || {}} onChange={v => setConfig({...config, events: v})} />
            </Field>
          </>
        )
      case 'email':
        return (
          <div className="space-y-3">
             <div className="grid grid-cols-2 gap-3">
                <Field label="SMTP Host">
                  <input className="input" value={config.smtp_host || ''} onChange={e => setConfig({...config, smtp_host: e.target.value})} placeholder="smtp.gmail.com" required />
                </Field>
                <Field label="Port">
                  <input type="number" className="input" value={config.smtp_port || 587} onChange={e => setConfig({...config, smtp_port: parseInt(e.target.value)})} />
                </Field>
                <Field label="Username">
                  <input className="input" value={config.username || ''} onChange={e => setConfig({...config, username: e.target.value})} />
                </Field>
                <Field label="Password">
                  <input type="password" className="input" value={config.password || ''} onChange={e => setConfig({...config, password: e.target.value})} />
                </Field>
             </div>
             <label className="flex items-center gap-2">
               <input type="checkbox" className="accent-[var(--accent)]" checked={config.use_tls !== false} onChange={e => setConfig({...config, use_tls: e.target.checked})} />
               Use TLS/SSL
             </label>
             <Field label="From Address">
               <input className="input" value={config.from_addr || ''} onChange={e => setConfig({...config, from_addr: e.target.value})} placeholder="nvr@example.com" required />
             </Field>
             <Field label="Recipients (comma separated)">
               <input className="input" value={config.to_addrs || ''} onChange={e => setConfig({...config, to_addrs: e.target.value})} placeholder="admin@example.com, security@example.com" required />
             </Field>
          </div>
        )
      case 'mqtt':
        return (
          <div className="space-y-3">
            <Field label="Broker URL">
              <input className="input" value={config.broker_url || ''} onChange={e => setConfig({...config, broker_url: e.target.value})} placeholder="mqtt://localhost:1883" required />
            </Field>
            <div className="grid grid-cols-2 gap-3">
              <Field label="Username">
                <input className="input" value={config.username || ''} onChange={e => setConfig({...config, username: e.target.value})} />
              </Field>
              <Field label="Password">
                 <input type="password" className="input" value={config.password || ''} onChange={e => setConfig({...config, password: e.target.value})} />
              </Field>
            </div>
            <Field label="Topic Prefix">
              <input className="input" value={config.topic_prefix || 'opennvr'} onChange={e => setConfig({...config, topic_prefix: e.target.value})} />
            </Field>
            <Field label="Events">
              <EventsMatrix value={config.events || {}} onChange={v => setConfig({...config, events: v})} />
            </Field>
          </div>
        )
       case 's3':
        return (
          <div className="space-y-3">
             <Field label="Endpoint">
                <input className="input" value={config.endpoint || ''} onChange={e => setConfig({...config, endpoint: e.target.value})} placeholder="https://s3.amazonaws.com" required />
             </Field>
             <div className="grid grid-cols-2 gap-3">
               <Field label="Access Key">
                 <input className="input" value={config.access_key || ''} onChange={e => setConfig({...config, access_key: e.target.value})} required />
               </Field>
               <Field label="Secret Key">
                 <input type="password" className="input" value={config.secret_key || ''} onChange={e => setConfig({...config, secret_key: e.target.value})} required />
               </Field>
               <Field label="Bucket Name">
                 <input className="input" value={config.bucket || ''} onChange={e => setConfig({...config, bucket: e.target.value})} required />
               </Field>
               <Field label="Region">
                 <input className="input" value={config.region || 'auto'} onChange={e => setConfig({...config, region: e.target.value})} />
               </Field>
             </div>
             <label className="flex items-center gap-2">
               <input type="checkbox" className="accent-[var(--accent)]" checked={!!config.upload_recordings} onChange={e => setConfig({...config, upload_recordings: e.target.checked})} />
               Upload Recordings
             </label>
             <label className="flex items-center gap-2">
               <input type="checkbox" className="accent-[var(--accent)]" checked={!!config.upload_snapshots} onChange={e => setConfig({...config, upload_snapshots: e.target.checked})} />
               Upload Snapshots
             </label>
          </div>
        )
      default:
        return <div className="text-[var(--text-dim)] italic p-2">Configuration not available for this type yet.</div>
    }
  }

  return (
    <div className="space-y-6 animate-in fade-in duration-300">
      <div className="flex items-center justify-between">
        <div>
           <h1 className="text-xl font-semibold flex items-center gap-2">
            <Plug /> Integrations
           </h1>
           <p className="text-[var(--text-dim)]">Connect third-party services for alerts, storage, and monitoring.</p>
        </div>
        {canAdmin && (
          <button className="btn btn-primary flex items-center gap-2" onClick={openCreate}>
            <Plus size={16} /> Add Integration
          </button>
        )}
      </div>

      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
        {integrations.map((item) => (
           <div key={item.id} className="card relative group hover:border-[var(--accent)] transition-colors">
              <div className="flex items-start justify-between mb-2">
                 <div className="flex items-center gap-2">
                    <StatusIcon enabled={item.enabled} />
                    <span className="font-medium text-lg truncate" title={item.name}>{item.name}</span>
                 </div>
                 <div className="flex items-center gap-1 opacity-0 group-hover:opacity-100 transition-opacity">
                    {canAdmin && (
                       <>
                         <button className="p-1.5 hover:bg-[var(--bg-hover)] rounded" onClick={() => handleTest(item.id)} title="Test Connection" disabled={testingId === item.id}>
                           {testingId === item.id ? <Activity size={16} className="animate-pulse" /> : <Play size={16} />}
                         </button>
                         <button className="p-1.5 hover:bg-[var(--bg-hover)] rounded" onClick={() => openEdit(item)} title="Configure">
                           <Settings size={16} />
                         </button>
                         <button className="p-1.5 hover:bg-red-900/30 text-red-400 rounded" onClick={() => handleDelete(item.id)} title="Delete">
                           <Trash size={16} />
                         </button>
                       </>
                    )}
                 </div>
              </div>
              <div className="text-sm text-[var(--text-dim)] capitalize mb-4">
                 {INTEGRATION_TYPES.find(t => t.value === item.type)?.label || item.type}
              </div>
              {/* Mini status indicator configuration summary */}
              <div className="text-xs text-[var(--text-dim)] bg-[var(--bg)] p-2 rounded truncate font-mono h-8 flex items-center">
                {getSummary(item)}
              </div>
           </div>
        ))}
        
        {integrations.length === 0 && !loading && (
           <div className="col-span-full py-12 text-center border-2 border-dashed border-[var(--border)] rounded-lg text-[var(--text-dim)]">
             <div className="flex justify-center mb-2"><Plug size={32} /></div>
             <p>No integrations configured.</p>
             {canAdmin && <button className="text-[var(--accent)] hover:underline mt-2" onClick={openCreate}>Add your first one</button>}
           </div>
        )}
      </div>

      {/* Configuration Modal */}
      {showModal && (
        <Modal open={showModal} title={editingId ? 'Edit Integration' : 'Add Integration'} onClose={() => setShowModal(false)}>
           <form onSubmit={handleSubmit} className="space-y-4">
              <Field label="Integration Type">
                 <select className="input" value={type} onChange={e => {setType(e.target.value as IntegrationType); setConfig({})}} disabled={!!editingId} >
                    {INTEGRATION_TYPES.map(t => (
                      <option key={t.value} value={t.value}>{t.label}</option>
                    ))}
                 </select>
              </Field>
              
              <Field label="Name">
                 <input className="input" value={name} onChange={e => setName(e.target.value)} placeholder="e.g. Ops Team Slack" required />
              </Field>
              
              <div className="p-4 bg-[var(--bg)] rounded border border-[var(--border)] max-h-[50vh] overflow-y-auto">
                 {renderConfigFields()}
              </div>
              
              <label className="flex items-center gap-2">
                 <input type="checkbox" className="accent-[var(--accent)]" checked={enabled} onChange={e => setEnabled(e.target.checked)} />
                 <span className={enabled ? 'text-green-400' : 'text-[var(--text-dim)]'}>{enabled ? 'Enabled' : 'Disabled'}</span>
              </label>

              <div className="flex justify-end gap-2 mt-6">
                <button type="button" className="btn" onClick={() => setShowModal(false)}>Cancel</button>
                <button type="submit" className="btn btn-primary" disabled={loading}>
                  {loading ? 'Saving...' : (editingId ? 'Save Changes' : 'Create Integration')}
                </button>
              </div>
           </form>
        </Modal>
      )}
    </div>
  )
}

function StatusIcon({ enabled }: { enabled: boolean }) {
  return enabled ? 
    <CheckCircle size={18} className="text-green-500" /> : 
    <AlertCircle size={18} className="text-[var(--text-dim)]" />
}

function Field({ label, children }: { label: string, children: React.ReactNode }) {
  return (
    <label className="block mb-3">
      <div className="text-sm font-medium mb-1 text-[var(--text-dim)]">{label}</div>
      {children}
    </label>
  )
}

// Helper for generating readable summary for the card
function getSummary(item: any) {
  const c = item.config
  switch (item.type) {
    case 'webhook': return c.url
    case 'slack': return c.webhook_url ? '.../' + c.webhook_url.split('/').slice(-2).join('/') : ''
    case 'teams': return c.webhook_url ? '.../' + c.webhook_url.split('/').slice(-1)[0].substring(0, 10) + '...' : ''
    case 'email': return c.smtp_host ? `${c.username}@${c.smtp_host}` : ''
    case 'mqtt': return `${c.broker_url} (${c.topic_prefix})`
    case 's3': return `s3://${c.bucket}`
    default: return item.type
  }
}

function EventsMatrix({ value, onChange }: { value: any, onChange: (v: any) => void }) {
  const events = [
    'camera.online', 'camera.offline', 
    'motion.detected', 'object.detected',
    'recording.started', 'incident.created'
  ]
  return (
    <div className="grid grid-cols-2 gap-2 text-xs">
      {events.map(evt => (
        <label key={evt} className="flex items-center gap-2 cursor-pointer hover:bg-[var(--bg-hover)] p-1 rounded select-none">
          <input type="checkbox" className="accent-[var(--accent)]" checked={!!value[evt]} onChange={e => onChange({...value, [evt]: e.target.checked})} />
          {evt}
        </label>
      ))}
    </div>
  )
}
