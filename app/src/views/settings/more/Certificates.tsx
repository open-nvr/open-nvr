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
import { apiService } from '../../../lib/apiService'
import { useAuth } from '../../../auth/AuthContext'

export function MoreCertificates() {
  const { user: me } = useAuth()
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [cfg, setCfg] = useState<any>({})

  const canAdmin = !!me?.is_superuser

  useEffect(() => {
    if (!canAdmin) return
    (async () => {
      try {
        setLoading(true)
        setError(null)
        const { data } = await apiService.getMediaSourceSettings()
        setCfg(data)
      } catch (e: any) {
        setError(e?.data?.detail || e?.message || 'Failed to load settings')
      } finally {
        setLoading(false)
      }
    })()
  }, [canAdmin])

  const save = async () => {
    if (!canAdmin) return
    try {
      setLoading(true)
      setError(null)
      await apiService.updateMediaSourceSettings({
        tls_cert_pem: cfg.tls_cert_pem || null,
        tls_key_pem: cfg.tls_key_pem || null,
        tls_ca_bundle_pem: cfg.tls_ca_bundle_pem || null,
      })
      const { data } = await apiService.getMediaSourceSettings()
      setCfg(data)
    } catch (e: any) {
      setError(e?.data?.detail || e?.message || 'Failed to save')
    } finally {
      setLoading(false)
    }
  }

  const readFileText = async (file: File | null) => {
    if (!file) return ''
    return await file.text()
  }

  const onFilePick = async (e: React.ChangeEvent<HTMLInputElement>, key: 'tls_cert_pem' | 'tls_key_pem' | 'tls_ca_bundle_pem') => {
    const file = e.target.files && e.target.files[0]
    const text = await readFileText(file || null)
    setCfg({ ...cfg, [key]: text })
  }

  if (!canAdmin) return <div className="text-sm text-amber-400">Admin only.</div>

  return (
    <div className="space-y-3">
      <div className="flex items-center gap-2">
        <h2 className="text-base font-semibold">Certificates</h2>
        <button className="ml-auto px-2 py-1 bg-[var(--accent)] text-white" onClick={save} disabled={loading}>Save</button>
      </div>
      {error && <div className="text-sm text-red-400">{error}</div>}

      <div className="grid grid-cols-2 gap-3 text-sm">
        <div className="border border-neutral-700 bg-[var(--panel-2)] p-2 space-y-2">
          <div className="text-[var(--text-dim)]">TLS Certificate (PEM)</div>
          <input type="file" accept=".pem,.crt,.cer,.txt" onChange={(e)=>onFilePick(e,'tls_cert_pem')} />
          <textarea className="w-full h-40 bg-[var(--panel)] border border-neutral-700 px-2 py-1" value={cfg.tls_cert_pem||''} onChange={(e)=>setCfg({...cfg, tls_cert_pem:e.target.value})} placeholder="-----BEGIN CERTIFICATE-----\n..." />
          <div className="text-[var(--text-dim)] text-xs">Paste or upload your certificate in PEM format.</div>
        </div>

        <div className="border border-neutral-700 bg-[var(--panel-2)] p-2 space-y-2">
          <div className="text-[var(--text-dim)]">TLS Private Key (PEM)</div>
          <input type="file" accept=".pem,.key,.txt" onChange={(e)=>onFilePick(e,'tls_key_pem')} />
          <textarea className="w-full h-40 bg-[var(--panel)] border border-neutral-700 px-2 py-1" value={cfg.tls_key_pem||''} onChange={(e)=>setCfg({...cfg, tls_key_pem:e.target.value})} placeholder="-----BEGIN PRIVATE KEY-----\n..." />
          <div className="text-[var(--text-dim)] text-xs">Paste or upload your private key in PEM format.</div>
        </div>

        <div className="border border-neutral-700 bg-[var(--panel-2)] p-2 space-y-2 col-span-2">
          <div className="text-[var(--text-dim)]">CA Bundle (optional, PEM)</div>
          <input type="file" accept=".pem,.crt,.cer,.txt" onChange={(e)=>onFilePick(e,'tls_ca_bundle_pem')} />
          <textarea className="w-full h-32 bg-[var(--panel)] border border-neutral-700 px-2 py-1" value={cfg.tls_ca_bundle_pem||''} onChange={(e)=>setCfg({...cfg, tls_ca_bundle_pem:e.target.value})} placeholder="-----BEGIN CERTIFICATE-----\n..." />
          <div className="text-[var(--text-dim)] text-xs">Include intermediates if needed.</div>
        </div>
      </div>
    </div>
  )
}


