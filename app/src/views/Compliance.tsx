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

import { useEffect, useState, useCallback } from 'react'
import { Download, RefreshCw, FileText, Activity, Camera, Database, HardDrive, AlertTriangle, ShieldAlert, ShieldCheck, Mail } from 'lucide-react'
import { toDataURL } from 'qrcode'

// Where a phone that scans the panel's QR lands. `ref` tags the lead source.
const ASSESSMENT_URL = 'https://opennvr.org/contact?ref=nvr-889'
import { apiService } from '../lib/apiService'
import { Card, CardHeader, CardTitle, CardContent, Skeleton } from '../components/ui'

interface ComplianceSummary {
  total_cameras: number
  online_cameras: number
  degraded_cameras: number
  offline_cameras: number
  recording_enabled: number
  retention_days: number
  total_storage_mb: number
  total_recordings: number
  timestamp: string
}

interface CoverageItem {
  camera_id: number
  camera_name: string
  date: string
  recording_count: number
  total_duration_seconds: number
  total_duration_hours: number
}

interface RecordingCoverage {
  start_date: string
  end_date: string
  days: number
  coverage: CoverageItem[]
}

interface AccessLog {
  id: number
  timestamp: string
  action: string
  entity_type: string | null
  entity_id: string | null
  details: string | null
  ip: string | null
  user_id: number | null
  username: string | null
}

interface AccessAudit {
  logs: AccessLog[]
  total: number
  days: number
}

interface SecurityFlag { code: string; severity: 'high' | 'medium' | 'low'; label: string }
interface SecurityCameraRow {
  id: number; name: string; ip: string; manufacturer: string; model: string
  covered: { parent: string; kind: string } | null
  flags: SecurityFlag[]
}
interface SecurityCheck {
  posture: 'ok' | 'attention' | 'covered_vendor'
  covered_vendor_found: boolean
  summary: { cameras: number; covered_vendor: number; internet_exposed: number; plaintext_stream: number; weak_credentials: number }
  cameras: SecurityCameraRow[]
  note: string
  generated_at?: string
}

function Badge({ children, variant = 'neutral' }: { children: React.ReactNode; variant?: 'success' | 'warning' | 'destructive' | 'neutral' | 'info' }) {
  const styles = {
    success: 'bg-green-900/50 text-green-400',
    warning: 'bg-yellow-900/50 text-yellow-400',
    destructive: 'bg-red-900/50 text-red-400',
    neutral: 'bg-gray-900/50 text-gray-400',
    info: 'bg-blue-900/50 text-blue-400',
  } as const
  return <span className={`inline-flex items-center gap-1 rounded px-2 py-0.5 text-[11px] ${styles[variant]}`}>{children}</span>
}

function Button({ children, onClick, disabled, className = '' }: { children: React.ReactNode; onClick?: () => void; disabled?: boolean; className?: string }) {
  return (
    <button 
      onClick={onClick} 
      disabled={disabled} 
      className={`inline-flex items-center gap-2 rounded border border-neutral-700 bg-[var(--panel-2)] px-3 py-1.5 text-sm hover:bg-[var(--panel)] disabled:opacity-50 ${className}`}
    >
      {children}
    </button>
  )
}

function KpiCard({ icon, label, value, tone = 'neutral' }: { icon: React.ReactNode; label: string; value: string | number; tone?: 'neutral' | 'success' | 'warning' | 'destructive' }) {
  const toneCls = {
    neutral: 'text-slate-300',
    success: 'text-emerald-300',
    warning: 'text-amber-300',
    destructive: 'text-red-300',
  } as const
  
  return (
    <Card>
      <CardHeader>
        <div className={`p-2 rounded-md bg-[var(--bg-2)] ${toneCls[tone]}`}>{icon}</div>
        <div className="ml-2">
          <div className="text-xs uppercase tracking-wide text-[var(--text-dim)]">{label}</div>
          <div className="text-xl font-semibold text-[var(--text)]">{value}</div>
        </div>
      </CardHeader>
    </Card>
  )
}

function SecuritySection({ data }: { data: SecurityCheck | null }) {
  // QR is rendered locally (no network) — encodes the assessment URL so an
  // operator on an offline box can scan it with their (connected) phone.
  const [qrUrl, setQrUrl] = useState<string>('')
  useEffect(() => {
    let alive = true
    toDataURL(ASSESSMENT_URL, { width: 160, margin: 1, color: { dark: '#000000', light: '#ffffff' } })
      .then((u) => { if (alive) setQrUrl(u) })
      .catch(() => {})
    return () => { alive = false }
  }, [])
  if (!data) return null
  const covered = data.covered_vendor_found
  const s = data.summary
  const flagged = data.cameras.filter((c) => c.flags.length > 0)
  const sevVariant = (sev: string) =>
    sev === 'high' ? 'destructive' : sev === 'medium' ? 'warning' : 'neutral'
  const stats: [string, number, string][] = [
    ['Covered-vendor', s.covered_vendor, s.covered_vendor ? 'text-red-400' : 'text-[var(--text)]'],
    ['Internet-exposed', s.internet_exposed, s.internet_exposed ? 'text-red-400' : 'text-[var(--text)]'],
    ['Plaintext stream', s.plaintext_stream, s.plaintext_stream ? 'text-yellow-400' : 'text-[var(--text)]'],
    ['Default username', s.weak_credentials, s.weak_credentials ? 'text-yellow-400' : 'text-[var(--text)]'],
  ]
  return (
    <Card>
      <CardHeader>
        <div className="flex items-center justify-between gap-3">
          <div className="flex items-center gap-2">
            {covered ? (
              <ShieldAlert size={18} className="text-red-400" />
            ) : data.posture === 'attention' ? (
              <ShieldAlert size={18} className="text-yellow-400" />
            ) : (
              <ShieldCheck size={18} className="text-green-400" />
            )}
            <div>
              <CardTitle>Security &amp; §889 Compliance</CardTitle>
              <div className="text-[11px] text-[var(--text-dim)] mt-0.5">
                Instant self-check · not an official §889 attestation
              </div>
            </div>
          </div>
          <Badge variant={covered ? 'destructive' : data.posture === 'attention' ? 'warning' : 'success'}>
            {covered ? 'Covered vendor found' : data.posture === 'attention' ? 'Needs attention' : 'No issues found'}
          </Badge>
        </div>
      </CardHeader>
      <CardContent>
        <div className="mb-4 text-[11px] leading-relaxed text-[var(--text-dim)]">
          A fast, automated snapshot from the cameras OpenNVR already manages — private (nothing leaves this box).
          It surfaces the <span className="text-[var(--text)]">obvious</span> covered-vendor and configuration risks.
          It is <span className="text-[var(--text)]">not</span> an official §889 attestation — the full OpenNVR assessment below is.
        </div>
        {covered && (
          <div className="mb-4 rounded border border-red-500/50 bg-red-900/20 p-4">
            <div className="text-sm font-medium text-red-300 mb-1">
              NDAA §889 covered-vendor cameras detected ({s.covered_vendor}).
            </div>
            <p className="text-xs text-[var(--text-dim)] mb-3">
              Running federal or state contracts? Covered equipment (Hikvision/Dahua and their
              affiliate brands) may require rip-and-replace and a signed §889 attestation.
            </p>
            <div className="text-xs text-[var(--text-dim)] mb-2">
              Get your official §889 assessment &amp; signed attestation — OpenNVR is offline-first, so reach us directly:
            </div>
            <div className="flex flex-wrap items-center gap-4">
              {qrUrl && (
                <div className="shrink-0 text-center">
                  <img
                    src={qrUrl}
                    alt="Scan to reach OpenNVR about a §889 assessment"
                    width={92}
                    height={92}
                    className="rounded bg-white p-1"
                  />
                  <div className="mt-1 text-[10px] text-[var(--text-dim)]">Scan from your phone</div>
                </div>
              )}
              <div className="flex flex-col gap-1.5">
                <a
                  href="mailto:contact@cryptovoip.in?subject=OpenNVR%20%C2%A7889%20assessment%20request"
                  className="inline-flex w-fit items-center gap-1.5 rounded bg-red-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-red-500"
                >
                  <Mail size={14} /> contact@cryptovoip.in
                </a>
                <span className="text-xs text-[var(--text-dim)]">
                  or, from a connected device, visit{' '}
                  <span className="text-[var(--text)]">opennvr.org/contact</span>
                </span>
              </div>
            </div>
          </div>
        )}

        <div className="grid grid-cols-2 md:grid-cols-4 gap-3 mb-4">
          {stats.map(([label, val, cls]) => (
            <div key={label} className="rounded border border-neutral-800 bg-[var(--panel-2)] p-3">
              <div className={`text-xl font-semibold ${cls}`}>{val}</div>
              <div className="mt-0.5 text-[11px] text-[var(--text-dim)]">{label}</div>
            </div>
          ))}
        </div>

        {flagged.length > 0 ? (
          <div className="space-y-2">
            {flagged.map((c) => (
              <div
                key={c.id}
                className="flex items-start justify-between gap-3 rounded border border-neutral-800 bg-[var(--panel-2)] p-2.5"
              >
                <div className="min-w-0">
                  <div className="truncate text-sm text-[var(--text)]">
                    {c.name} <span className="text-[var(--text-dim)]">· {c.ip}</span>
                  </div>
                  <div className="text-xs text-[var(--text-dim)]">
                    {[c.manufacturer, c.model].filter(Boolean).join(' ') || 'unidentified'}
                  </div>
                </div>
                <div className="flex flex-wrap justify-end gap-1">
                  {c.flags.map((f, i) => (
                    <Badge key={i} variant={sevVariant(f.severity)}>{f.label}</Badge>
                  ))}
                </div>
              </div>
            ))}
          </div>
        ) : (
          <div className="text-sm text-green-400">
            No covered-vendor or high-risk cameras found in your inventory.
          </div>
        )}

        <div className="mt-4 rounded border border-cyan-500/30 bg-[var(--panel-2)] p-3.5">
          <div className="text-sm font-semibold text-[var(--text)]">The full OpenNVR §889 Security Assessment</div>
          <div className="mt-0.5 mb-2.5 text-xs text-[var(--text-dim)]">
            A hands-on engagement led by our security team — not a self-serve scan. We assess every camera on
            your network, meet with your people, and deliver a signed report your contracting officer accepts.
          </div>
          <ul className="grid grid-cols-1 gap-x-4 gap-y-1 sm:grid-cols-2 text-xs text-[var(--text-dim)]">
            {[
              'A dedicated OpenNVR security SPOC — your single point of contact from kickoff to attestation',
              'Live working sessions with our expert team — findings walkthrough, remediation planning & Q&A',
              'Our deep automated Scout scan — 40+ checks across 12 categories, on every device',
              'Forensic OEM-rebrand detection — unmasks Hikvision/Dahua hidden behind other brands',
              'Live CVE & CISA-KEV threat intelligence on each device',
              'Active security testing — encryption, stream auth, network exposure & more',
              'A signed, audit-ready §889 attestation (PDF) auditors accept',
              'Continuous monitoring, drift alerts & re-attestation each cycle',
              'An expert remediation roadmap to a compliant, sovereign estate',
            ].map((t) => (
              <li key={t} className="flex gap-1.5">
                <span className="text-cyan-400">+</span>
                <span>{t}</span>
              </li>
            ))}
          </ul>
          <div className="mt-2.5 text-xs text-cyan-300">
            …and much more — a defensible compliance package, not just a scan.
          </div>
        </div>
      </CardContent>
    </Card>
  )
}

export function Compliance() {
  const [summary, setSummary] = useState<ComplianceSummary | null>(null)
  const [coverage, setCoverage] = useState<RecordingCoverage | null>(null)
  const [accessAudit, setAccessAudit] = useState<AccessAudit | null>(null)
  const [securityCheck, setSecurityCheck] = useState<SecurityCheck | null>(null)
  const [loading, setLoading] = useState(true)
  const [refreshing, setRefreshing] = useState(false)
  const [coverageDays, setCoverageDays] = useState(30)
  const [auditDays, setAuditDays] = useState(7)
  const [exportLoading, setExportLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const fetchData = useCallback(async () => {
    try {
      setRefreshing(true)
      setError(null)
      const [summaryRes, coverageRes, auditRes] = await Promise.all([
        apiService.getComplianceSummary(),
        apiService.getRecordingCoverage({ days: coverageDays }),
        apiService.getAccessAudit({ limit: 100, days: auditDays }),
      ])
      setSummary(summaryRes.data)
      setCoverage(coverageRes.data)
      setAccessAudit(auditRes.data)
      // Lite §889/security check — independent so an older server (no endpoint)
      // never blanks the rest of the page.
      try {
        const scRes = await apiService.getSecurityCheck()
        setSecurityCheck(scRes.data)
      } catch {
        setSecurityCheck(null)
      }
    } catch (error: any) {
      console.error('Error fetching compliance data:', error)
      setError(error?.response?.data?.detail || error?.message || 'Failed to load compliance data')
    } finally {
      setLoading(false)
      setRefreshing(false)
    }
  }, [coverageDays, auditDays])

  useEffect(() => {
    fetchData()
  }, [fetchData])

  const handleExport = async () => {
    try {
      setExportLoading(true)
      const response = await apiService.exportComplianceReport(coverageDays)
      
      // Create a blob from the response
      const blob = new Blob([response.data], { type: 'text/csv' })
      const url = window.URL.createObjectURL(blob)
      const link = document.createElement('a')
      link.href = url
      link.download = `compliance_report_${new Date().toISOString().split('T')[0]}.csv`
      document.body.appendChild(link)
      link.click()
      document.body.removeChild(link)
      window.URL.revokeObjectURL(url)
    } catch (error) {
      console.error('Error exporting CSV:', error)
    } finally {
      setExportLoading(false)
    }
  }

  if (loading) {
    return (
      <div className="space-y-4">
        <div className="flex items-center justify-between">
          <h1 className="text-2xl font-semibold text-[var(--text)]">Compliance & Reports</h1>
        </div>
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4">
          {[...Array(8)].map((_, i) => (
            <Skeleton key={i} className="h-24" />
          ))}
        </div>
      </div>
    )
  }

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-semibold text-[var(--text)]">Compliance & Reports</h1>
          <p className="text-sm text-[var(--text-dim)] mt-1">
            System compliance status and audit reports
          </p>
        </div>
        <div className="flex gap-2">
          <Button onClick={fetchData} disabled={refreshing}>
            <RefreshCw size={16} className={refreshing ? 'animate-spin' : ''} />
            Refresh
          </Button>
          <Button onClick={handleExport} disabled={exportLoading}>
            <Download size={16} />
            {exportLoading ? 'Exporting...' : 'Export CSV'}
          </Button>
        </div>
      </div>

      {/* Error Message */}
      {error && (
        <div className="bg-red-900/20 border border-red-500/50 rounded p-4 text-red-400 text-sm">
          <strong>Error:</strong> {error}
        </div>
      )}

      {/* Security & §889 lite check */}
      <SecuritySection data={securityCheck} />

      {/* KPI Cards */}
      {summary ? (
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4">
          <KpiCard
            icon={<Camera size={20} />}
            label="Total Cameras"
            value={summary.total_cameras}
          />
          <KpiCard
            icon={<Activity size={20} />}
            label="Online Cameras"
            value={summary.online_cameras}
            tone="success"
          />
          <KpiCard
            icon={<AlertTriangle size={20} />}
            label="Degraded"
            value={summary.degraded_cameras}
            tone={summary.degraded_cameras > 0 ? 'warning' : 'neutral'}
          />
          <KpiCard
            icon={<Camera size={20} />}
            label="Offline"
            value={summary.offline_cameras}
            tone={summary.offline_cameras > 0 ? 'destructive' : 'neutral'}
          />
          <KpiCard
            icon={<FileText size={20} />}
            label="Recording Enabled"
            value={summary.recording_enabled}
            tone={summary.recording_enabled > 0 ? 'success' : 'neutral'}
          />
          <KpiCard
            icon={<Database size={20} />}
            label="Retention Days"
            value={summary.retention_days}
          />
          <KpiCard
            icon={<HardDrive size={20} />}
            label="Storage (GB)"
            value={(summary.total_storage_mb / 1024).toFixed(2)}
          />
          <KpiCard
            icon={<FileText size={20} />}
            label="Total Recordings"
            value={summary.total_recordings}
          />
        </div>
      ) : !loading && !error ? (
        <div className="bg-yellow-900/20 border border-yellow-500/50 rounded p-4 text-yellow-400 text-sm">
          No summary data available. This could mean the backend is not responding or no cameras are configured.
        </div>
      ) : null}

      {/* Recording Coverage */}
      <Card>
        <CardHeader>
          <FileText size={18} />
          <CardTitle>Recording Coverage</CardTitle>
          <div className="ml-auto flex items-center gap-2">
            <label className="text-xs text-[var(--text-dim)]">Days:</label>
            <select 
              value={coverageDays}
              onChange={(e) => setCoverageDays(Number(e.target.value))}
              className="rounded border border-neutral-700 bg-[var(--panel-2)] px-2 py-1 text-sm"
            >
              <option value={7}>7</option>
              <option value={14}>14</option>
              <option value={30}>30</option>
              <option value={60}>60</option>
              <option value={90}>90</option>
            </select>
          </div>
        </CardHeader>
        <CardContent>
          {coverage && coverage.coverage.length > 0 ? (
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead className="border-b border-neutral-700">
                  <tr className="text-left text-xs uppercase text-[var(--text-dim)]">
                    <th className="pb-2">Camera</th>
                    <th className="pb-2">Date</th>
                    <th className="pb-2 text-right">Recordings</th>
                    <th className="pb-2 text-right">Duration (hrs)</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-neutral-800">
                  {coverage.coverage.slice(0, 50).map((item, idx) => (
                    <tr key={idx} className="hover:bg-[var(--bg-2)]">
                      <td className="py-2">
                        <div className="font-medium">{item.camera_name}</div>
                        <div className="text-xs text-[var(--text-dim)]">ID: {item.camera_id}</div>
                      </td>
                      <td className="py-2">{item.date}</td>
                      <td className="py-2 text-right">{item.recording_count}</td>
                      <td className="py-2 text-right">
                        <Badge variant={item.total_duration_hours >= 20 ? 'success' : item.total_duration_hours >= 10 ? 'warning' : 'destructive'}>
                          {item.total_duration_hours.toFixed(1)}
                        </Badge>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
              {coverage.coverage.length > 50 && (
                <div className="mt-4 text-center text-xs text-[var(--text-dim)]">
                  Showing first 50 of {coverage.coverage.length} entries
                </div>
              )}
            </div>
          ) : (
            <div className="text-center py-8 text-[var(--text-dim)]">
              No recording coverage data available
            </div>
          )}
        </CardContent>
      </Card>

      {/* Access Audit Log */}
      <Card>
        <CardHeader>
          <Activity size={18} />
          <CardTitle>Access Audit Log</CardTitle>
          <div className="ml-auto flex items-center gap-2">
            <label className="text-xs text-[var(--text-dim)]">Days:</label>
            <select 
              value={auditDays}
              onChange={(e) => setAuditDays(Number(e.target.value))}
              className="rounded border border-neutral-700 bg-[var(--panel-2)] px-2 py-1 text-sm"
            >
              <option value={1}>1</option>
              <option value={7}>7</option>
              <option value={14}>14</option>
              <option value={30}>30</option>
            </select>
          </div>
        </CardHeader>
        <CardContent>
          {accessAudit && accessAudit.logs.length > 0 ? (
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead className="border-b border-neutral-700">
                  <tr className="text-left text-xs uppercase text-[var(--text-dim)]">
                    <th className="pb-2">Timestamp</th>
                    <th className="pb-2">User</th>
                    <th className="pb-2">Action</th>
                    <th className="pb-2">Entity</th>
                    <th className="pb-2">IP Address</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-neutral-800">
                  {accessAudit.logs.map((log) => (
                    <tr key={log.id} className="hover:bg-[var(--bg-2)]">
                      <td className="py-2 text-xs">
                        {new Date(log.timestamp).toLocaleString()}
                      </td>
                      <td className="py-2">
                        <Badge variant="info">
                          {log.username || `User ${log.user_id || 'N/A'}`}
                        </Badge>
                      </td>
                      <td className="py-2">
                        <Badge 
                          variant={
                            log.action.includes('delete') ? 'destructive' :
                            log.action.includes('create') ? 'success' :
                            log.action.includes('update') ? 'warning' :
                            'neutral'
                          }
                        >
                          {log.action}
                        </Badge>
                      </td>
                      <td className="py-2 text-xs">
                        {log.entity_type && log.entity_id 
                          ? `${log.entity_type}:${log.entity_id}`
                          : '-'
                        }
                      </td>
                      <td className="py-2 text-xs text-[var(--text-dim)]">
                        {log.ip || '-'}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : (
            <div className="text-center py-8 text-[var(--text-dim)]">
              No audit logs available
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  )
}

