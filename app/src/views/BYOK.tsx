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

import { useEffect, useState } from 'react'
import { apiService } from '../lib/apiService'
import { useAuth } from '../auth/AuthContext'
import { KeyRound, Upload, FileText, Shield, CheckCircle, AlertCircle, Loader2, Info, X } from 'lucide-react'

type KeyItem = { id: string; name: string; description?: string; cert_pem?: string; key_pem?: string; created_at?: string }

export function BYOK() {
  const { user } = useAuth()
  const canAdmin = !!user?.is_superuser
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [draft, setDraft] = useState<KeyItem>({ id: '', name: '', description: '', cert_pem: '', key_pem: '' })
  const [uploadNames, setUploadNames] = useState<{ cert?: string; key?: string; ca?: string }>({})
  const [activeTab, setActiveTab] = useState<'paste' | 'upload'>('paste')
  const [showInfo, setShowInfo] = useState(false)

  useEffect(() => {
    ;(async () => {
      try {
        setLoading(true)
        setError(null)
        const { data } = await apiService.getMediaSourceSettings()
        const single: KeyItem = {
          id: 'default',
          name: 'Default Certificate',
          description: data?.tls_cert_description || '',
          cert_pem: data?.tls_cert_pem || '',
          key_pem: data?.tls_key_pem || '',
        }
        setDraft(single)
      } catch (e: any) {
        setError(e?.data?.detail || e?.message || 'Failed to load keys')
      } finally { setLoading(false) }
    })()
  }, [])

  useEffect(() => {
    if (notice) {
      const t = setTimeout(() => setNotice(null), 5000)
      return () => clearTimeout(t)
    }
  }, [notice])

  async function save() {
    if (!canAdmin) return
    try {
      setLoading(true)
      setError(null)
      await apiService.updateMediaSourceSettings({ 
        tls_cert_pem: draft.cert_pem || null, 
        tls_key_pem: draft.key_pem || null,
        tls_cert_description: draft.description || null
      })
      setNotice('Certificate saved successfully')
    } catch (e: any) {
      setError(e?.data?.detail || e?.message || 'Failed to save')
    } finally { setLoading(false) }
  }

  async function handleFile(e: React.ChangeEvent<HTMLInputElement>, field: 'cert_pem' | 'key_pem') {
    const file = e.target.files && e.target.files[0]
    if (!file) return
    const text = await file.text()
    setDraft({ ...draft, [field]: text })
    setNotice(`${field === 'cert_pem' ? 'Certificate' : 'Private key'} loaded from file`)
  }

  async function handleUpload() {
    try {
      setLoading(true)
      setError(null)
      const certInput = document.getElementById('byok-upload-cert') as HTMLInputElement | null
      const keyInput = document.getElementById('byok-upload-key') as HTMLInputElement | null
      const caInput = document.getElementById('byok-upload-ca') as HTMLInputElement | null
      
      const certFile = certInput?.files?.[0]
      const keyFile = keyInput?.files?.[0]
      const caFile = caInput?.files?.[0]
      
      if (!certFile && !keyFile) {
        setError('Please select at least a certificate or key file')
        return
      }
      
      await apiService.uploadMediaSourceSettings({
        cert_file: certFile,
        key_file: keyFile,
        ca_bundle_file: caFile,
      })
      setNotice('Files uploaded successfully')
      setUploadNames({})
      // Refresh data
      const { data } = await apiService.getMediaSourceSettings()
      setDraft({
        ...draft,
        cert_pem: data?.tls_cert_pem || '',
        key_pem: data?.tls_key_pem || '',
      })
    } catch (e: any) {
      setError(e?.data?.detail || e?.message || 'Upload failed')
    } finally { setLoading(false) }
  }

  async function clearCertificates() {
    if (!canAdmin) return
    if (!confirm('Are you sure you want to clear all certificates? This will remove the current TLS configuration.')) return
    try {
      setLoading(true)
      setError(null)
      await apiService.updateMediaSourceSettings({
        tls_cert_pem: null,
        tls_key_pem: null,
        tls_cert_description: null,
      })
      setDraft({ ...draft, cert_pem: '', key_pem: '', description: '' })
      setUploadNames({})
      setNotice('Certificates cleared successfully')
    } catch (e: any) {
      setError(e?.data?.detail || e?.message || 'Failed to clear certificates')
    } finally {
      setLoading(false)
    }
  }

  const hasCert = !!draft.cert_pem
  const hasKey = !!draft.key_pem
  const isComplete = hasCert && hasKey

  return (
    <section className="space-y-6">
      {/* Info Dialog */}
      {showInfo && (
        <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50">
          <div className="bg-[var(--panel)] border border-neutral-700 p-6 max-w-lg w-full mx-4">
            <div className="flex items-center justify-between mb-4">
              <h3 className="text-lg font-semibold flex items-center gap-2">
                <Info size={20} className="text-[var(--accent)]" />
                About BYOK
              </h3>
              <button onClick={() => setShowInfo(false)} className="text-[var(--text-dim)] hover:text-[var(--text)]">
                <X size={20} />
              </button>
            </div>
            <div className="space-y-4 text-sm">
              <div>
                <h4 className="font-medium text-[var(--text)] mb-1">What is BYOK?</h4>
                <p className="text-[var(--text-dim)]">
                  BYOK (Bring Your Own Key) allows you to use your own TLS certificates for encrypting 
                  video data when streaming or recording to cloud servers over the internet.
                </p>
              </div>
              <div>
                <h4 className="font-medium text-[var(--text)] mb-1">When do you need this?</h4>
                <ul className="text-[var(--text-dim)] list-disc list-inside space-y-1">
                  <li>Pushing recordings to a remote cloud server</li>
                  <li>Streaming video to an external server over the internet</li>
                  <li>Enterprise compliance requirements (HIPAA, PCI-DSS, SOC2)</li>
                </ul>
              </div>
              <div>
                <h4 className="font-medium text-[var(--text)] mb-1">When is this NOT needed?</h4>
                <ul className="text-[var(--text-dim)] list-disc list-inside space-y-1">
                  <li>Local-only deployments (NVR on same network as cameras)</li>
                  <li>No cloud streaming or recording configured</li>
                </ul>
              </div>
              <div>
                <h4 className="font-medium text-[var(--text)] mb-1">How to use?</h4>
                <ol className="text-[var(--text-dim)] list-decimal list-inside space-y-1">
                  <li>Configure cloud recording or streaming server IP first</li>
                  <li>Obtain a TLS certificate and private key from your CA</li>
                  <li>Paste the PEM content or upload the files</li>
                  <li>Click "Save Certificate" to apply</li>
                </ol>
              </div>
            </div>
            <div className="mt-6 flex justify-end">
              <button 
                onClick={() => setShowInfo(false)} 
                className="px-4 py-2 bg-[var(--accent)] text-white"
              >
                Got it
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Header */}
      <div className="flex items-start justify-between">
        <div>
          <h1 className="text-xl font-semibold flex items-center gap-2">
            <KeyRound className="text-[var(--accent)]" size={24} />
            Customer Keys (BYOK)
            <button 
              onClick={() => setShowInfo(true)} 
              className="text-[var(--text-dim)] hover:text-[var(--accent)] transition-colors"
              title="Learn about BYOK"
            >
              <Info size={18} />
            </button>
          </h1>
          <p className="text-sm text-[var(--text-dim)] mt-1">
            TLS certificates for encrypting cloud streaming and recording connections
          </p>
        </div>
        <div className={`px-3 py-1.5 text-xs font-medium flex items-center gap-1.5 ${
          isComplete ? 'bg-green-500/20 text-green-400 border border-green-500/30' : 
          hasCert || hasKey ? 'bg-amber-500/20 text-amber-400 border border-amber-500/30' : 
          'bg-neutral-500/20 text-neutral-400 border border-neutral-500/30'
        }`}>
          {isComplete ? <CheckCircle size={14} /> : <AlertCircle size={14} />}
          {isComplete ? 'Configured' : hasCert || hasKey ? 'Incomplete' : 'Not Configured'}
        </div>
      </div>

      {/* Alerts */}
      {notice && (
        <div className="p-3 bg-green-500/10 border border-green-500/30 text-green-300 text-sm flex items-center gap-2">
          <CheckCircle size={16} />
          {notice}
        </div>
      )}
      {error && (
        <div className="p-3 bg-red-500/10 border border-red-500/30 text-red-300 text-sm flex items-center gap-2">
          <AlertCircle size={16} />
          {error}
          <button className="ml-auto text-xs underline" onClick={() => setError(null)}>Dismiss</button>
        </div>
      )}

      {/* Tab Selector */}
      <div className="flex gap-2 border-b border-neutral-700">
        <button
          className={`px-4 py-2 text-sm font-medium border-b-2 -mb-px transition-colors ${
            activeTab === 'paste' 
              ? 'border-[var(--accent)] text-[var(--accent)]' 
              : 'border-transparent text-[var(--text-dim)] hover:text-[var(--text)]'
          }`}
          onClick={() => setActiveTab('paste')}
        >
          <FileText size={14} className="inline mr-2" />
          Paste PEM Content
        </button>
        <button
          className={`px-4 py-2 text-sm font-medium border-b-2 -mb-px transition-colors ${
            activeTab === 'upload' 
              ? 'border-[var(--accent)] text-[var(--accent)]' 
              : 'border-transparent text-[var(--text-dim)] hover:text-[var(--text)]'
          }`}
          onClick={() => setActiveTab('upload')}
        >
          <Upload size={14} className="inline mr-2" />
          Upload Files
        </button>
      </div>

      {/* Paste Tab */}
      {activeTab === 'paste' && (
        <div className="space-y-4">
          <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
            {/* Certificate */}
            <div className="border border-neutral-700 bg-[var(--panel-2)] overflow-hidden">
              <div className="px-4 py-3 border-b border-neutral-700 flex items-center justify-between">
                <div className="font-medium flex items-center gap-2">
                  <FileText size={16} className="text-[var(--accent)]" />
                  Certificate (PEM)
                </div>
                {hasCert && <span className="text-xs text-green-400 flex items-center gap-1"><CheckCircle size={12} /> Loaded</span>}
              </div>
              <div className="p-4 space-y-3">
                <label className="block">
                  <span className="text-xs text-[var(--text-dim)] mb-1 block">Load from file</span>
                  <input 
                    type="file" 
                    accept=".pem,.crt,.cer,.txt" 
                    onChange={(e) => handleFile(e, 'cert_pem')}
                    className="text-sm file:mr-3 file:py-1.5 file:px-3 file:border-0 file:bg-[var(--accent)] file:text-white file:cursor-pointer"
                  />
                </label>
                <textarea 
                  className="w-full h-48 bg-[var(--panel)] border border-neutral-700 px-3 py-2 text-sm font-mono resize-none focus:outline-none focus:border-[var(--accent)]" 
                  value={draft.cert_pem || ''} 
                  onChange={(e) => setDraft({ ...draft, cert_pem: e.target.value })} 
                  placeholder="-----BEGIN CERTIFICATE-----&#10;MIIDXTCCAkWgAwIBAgIJAJC1...&#10;-----END CERTIFICATE-----" 
                />
                <p className="text-xs text-[var(--text-dim)]">
                  Provide a valid X.509 certificate in PEM format. This certificate will be used for TLS encryption.
                </p>
              </div>
            </div>

            {/* Private Key */}
            <div className="border border-neutral-700 bg-[var(--panel-2)] overflow-hidden">
              <div className="px-4 py-3 border-b border-neutral-700 flex items-center justify-between">
                <div className="font-medium flex items-center gap-2">
                  <KeyRound size={16} className="text-amber-400" />
                  Private Key (PEM)
                </div>
                {hasKey && <span className="text-xs text-green-400 flex items-center gap-1"><CheckCircle size={12} /> Loaded</span>}
              </div>
              <div className="p-4 space-y-3">
                <label className="block">
                  <span className="text-xs text-[var(--text-dim)] mb-1 block">Load from file</span>
                  <input 
                    type="file" 
                    accept=".pem,.key,.txt" 
                    onChange={(e) => handleFile(e, 'key_pem')}
                    className="text-sm file:mr-3 file:py-1.5 file:px-3 file:border-0 file:bg-[var(--accent)] file:text-white file:cursor-pointer"
                  />
                </label>
                <textarea 
                  className="w-full h-48 bg-[var(--panel)] border border-neutral-700 px-3 py-2 text-sm font-mono resize-none focus:outline-none focus:border-[var(--accent)]" 
                  value={draft.key_pem || ''} 
                  onChange={(e) => setDraft({ ...draft, key_pem: e.target.value })} 
                  placeholder="-----BEGIN PRIVATE KEY-----&#10;MIIEvgIBADANBgkqhkiG9w0B...&#10;-----END PRIVATE KEY-----" 
                />
                <p className="text-xs text-[var(--text-dim)]">
                  Paste the corresponding private key in PEM format. Keep this key secure and never share it.
                </p>
              </div>
            </div>
          </div>

          {/* Description & Actions */}
          <div className="border border-neutral-700 bg-[var(--panel-2)] p-4 space-y-4">
            <div>
              <label className="text-sm text-[var(--text-dim)] mb-1 block">Description (optional)</label>
              <input 
                className="w-full bg-[var(--panel)] border border-neutral-700 px-3 py-2 text-sm focus:outline-none focus:border-[var(--accent)]" 
                placeholder="e.g., Production certificate for cloud streaming - expires Dec 2025" 
                value={draft.description || ''} 
                onChange={(e) => setDraft({ ...draft, description: e.target.value })} 
              />
            </div>
            <div className="flex items-center gap-3">
              <button 
                className="px-4 py-2 bg-[var(--accent)] text-white font-medium disabled:opacity-50 flex items-center gap-2" 
                onClick={save} 
                disabled={!canAdmin || loading}
              >
                {loading ? <Loader2 size={16} className="animate-spin" /> : <CheckCircle size={16} />}
                Save Certificate
              </button>
              {(hasCert || hasKey) && (
                <button 
                  className="px-4 py-2 bg-red-500/20 text-red-400 border border-red-500/30 font-medium hover:bg-red-500/30 transition-colors" 
                  onClick={clearCertificates}
                >
                  Clear Certificates
                </button>
              )}
              {!canAdmin && (
                <span className="text-xs text-amber-400">Admin privileges required to modify certificates</span>
              )}
            </div>
          </div>
        </div>
      )}

      {/* Upload Tab */}
      {activeTab === 'upload' && (
        <div className="border border-neutral-700 bg-[var(--panel-2)] p-6 space-y-6">
          <div className="text-center">
            <Upload size={40} className="mx-auto text-[var(--text-dim)] mb-3" />
            <h3 className="font-medium mb-1">Upload Certificate Files</h3>
            <p className="text-sm text-[var(--text-dim)]">Select your certificate, private key, and optionally a CA bundle file</p>
          </div>

          <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
            {/* Cert Upload */}
            <div className="border border-dashed border-neutral-600 p-4 text-center hover:border-[var(--accent)] transition-colors">
              <input
                id="byok-upload-cert"
                type="file"
                accept=".pem,.crt,.cer,.txt"
                className="hidden"
                onChange={(e) => setUploadNames((n) => ({ ...n, cert: e.target.files?.[0]?.name || '' }))}
              />
              <label htmlFor="byok-upload-cert" className="cursor-pointer block">
                <FileText size={24} className="mx-auto text-[var(--accent)] mb-2" />
                <div className="text-sm font-medium mb-1">Certificate</div>
                <div className="text-xs text-[var(--text-dim)]">
                  {uploadNames.cert || 'Click to select .pem, .crt, .cer'}
                </div>
                {uploadNames.cert && <CheckCircle size={14} className="mx-auto mt-2 text-green-400" />}
              </label>
            </div>

            {/* Key Upload */}
            <div className="border border-dashed border-neutral-600 p-4 text-center hover:border-[var(--accent)] transition-colors">
              <input
                id="byok-upload-key"
                type="file"
                accept=".pem,.key,.txt"
                className="hidden"
                onChange={(e) => setUploadNames((n) => ({ ...n, key: e.target.files?.[0]?.name || '' }))}
              />
              <label htmlFor="byok-upload-key" className="cursor-pointer block">
                <KeyRound size={24} className="mx-auto text-amber-400 mb-2" />
                <div className="text-sm font-medium mb-1">Private Key</div>
                <div className="text-xs text-[var(--text-dim)]">
                  {uploadNames.key || 'Click to select .pem, .key'}
                </div>
                {uploadNames.key && <CheckCircle size={14} className="mx-auto mt-2 text-green-400" />}
              </label>
            </div>

            {/* CA Bundle Upload */}
            <div className="border border-dashed border-neutral-600 p-4 text-center hover:border-[var(--accent)] transition-colors">
              <input
                id="byok-upload-ca"
                type="file"
                accept=".pem,.crt,.cer,.txt"
                className="hidden"
                onChange={(e) => setUploadNames((n) => ({ ...n, ca: e.target.files?.[0]?.name || '' }))}
              />
              <label htmlFor="byok-upload-ca" className="cursor-pointer block">
                <Shield size={24} className="mx-auto text-blue-400 mb-2" />
                <div className="text-sm font-medium mb-1">CA Bundle <span className="text-[var(--text-dim)]">(optional)</span></div>
                <div className="text-xs text-[var(--text-dim)]">
                  {uploadNames.ca || 'Click to select CA chain'}
                </div>
                {uploadNames.ca && <CheckCircle size={14} className="mx-auto mt-2 text-green-400" />}
              </label>
            </div>
          </div>

          <div className="flex justify-center">
            <button
              className="px-6 py-2.5 bg-[var(--accent)] text-white font-medium disabled:opacity-50 flex items-center gap-2"
              onClick={handleUpload}
              disabled={!canAdmin || loading || (!uploadNames.cert && !uploadNames.key)}
            >
              {loading ? <Loader2 size={16} className="animate-spin" /> : <Upload size={16} />}
              Upload Files
            </button>
          </div>

          <p className="text-xs text-[var(--text-dim)] text-center">
            Files are securely stored on the server and will be used for TLS encryption.
          </p>
        </div>
      )}

    </section>
  )
}
