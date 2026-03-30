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
import { Download, RefreshCw, FileText, Activity, Camera, Database, HardDrive, AlertTriangle } from 'lucide-react'
import { apiService } from '../lib/apiService'
import { Card, CardHeader, CardTitle, CardContent, Skeleton } from './Dashboard'

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

export function Compliance() {
  const [summary, setSummary] = useState<ComplianceSummary | null>(null)
  const [coverage, setCoverage] = useState<RecordingCoverage | null>(null)
  const [accessAudit, setAccessAudit] = useState<AccessAudit | null>(null)
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
      const response = await apiService.exportComplianceCSV({ days: coverageDays })
      
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

