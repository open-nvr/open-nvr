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
import { api } from '../../lib/api'
import { useAuth } from '../../auth/AuthContext'
import { useSnackbar } from '../../components/Snackbar'
import { Link } from 'react-router-dom'

export function MediaServerManager() {
  const { user: me } = useAuth()
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [setupData, setSetupData] = useState<any>(null)
  const [globalConfig, setGlobalConfig] = useState<any>(null)
  const [activePaths, setActivePaths] = useState<any[]>([])
  const [recordings, setRecordings] = useState<any[]>([])
  const [selectedTab, setSelectedTab] = useState<'setup' | 'streams' | 'recordings' | 'config'>('setup')

  const canAdmin = !!me?.is_superuser

  useEffect(() => {
    if (!canAdmin) return
    loadData()
  }, [canAdmin, selectedTab])

  const loadData = async () => {
    if (!canAdmin) return
    setLoading(true)
    setError(null)
    
    try {
  // Load setup data
      if (selectedTab === 'setup') {
  const { data } = await api.get('/api/v1/mediamtx/setup')
        setSetupData(data)
      }
      
      // Load global config
      if (selectedTab === 'config') {
        try {
          const { data } = await api.get('/api/v1/mediamtx/admin/global')
          setGlobalConfig(data)
        } catch (e: any) {
          if (e.response?.status !== 404) throw e
          setGlobalConfig({ status: 'not_configured' })
        }
      }
      
      // Load active streams
      if (selectedTab === 'streams') {
        try {
          const { data } = await api.get('/api/v1/mediamtx/admin/paths/list')
          setActivePaths(data?.details?.items || [])
        } catch (e: any) {
          if (e.response?.status !== 404) throw e
          setActivePaths([])
        }
      }
      
      // Load recordings
      if (selectedTab === 'recordings') {
        try {
          const { data } = await api.get('/api/v1/mediamtx/admin/recordings/list')
          setRecordings(data?.details?.items || [])
        } catch (e: any) {
          if (e.response?.status !== 404) throw e
          setRecordings([])
        }
      }
    } catch (e: any) {
  setError(e?.data?.detail || e?.message || 'Failed to load media server data')
    } finally {
      setLoading(false)
    }
  }

  const pushRTSPStream = async (cameraId: number, rtspUrl: string, enableRecording: boolean = false) => {
    try {
      setError(null)
  const { data } = await api.post(`/api/v1/mediamtx/admin/streams/push/${cameraId}`, null, {
        params: { rtsp_url: rtspUrl, enable_recording: enableRecording }
      })
      
      if (data.status === 'ok') {
        await loadData()
        return data
      } else {
        throw new Error(data.message || 'Failed to push RTSP stream')
      }
    } catch (e: any) {
      setError(e?.data?.detail || e?.message || 'Failed to push RTSP stream')
      throw e
    }
  }

  if (!canAdmin) return <div className="text-sm text-[var(--warn)]">Admin only.</div>

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
  <h2 className="text-lg font-semibold">Media Server Management</h2>
        <button 
          className="px-3 py-1 bg-[var(--accent)] text-white text-sm" 
          onClick={loadData} 
          disabled={loading}
        >
          {loading ? 'Loading...' : 'Refresh'}
        </button>
      </div>

      {error && (
        <div className="bg-[color-mix(in_oklab,var(--danger)_14%,transparent)] border border-[color-mix(in_oklab,var(--danger)_45%,var(--border))] text-[var(--danger)] p-3 text-sm">
          {error}
        </div>
      )}

      {/* Tab Navigation */}
      <div className="flex border-b border-[var(--border)]">
        {[
          { key: 'setup', label: 'Setup' },
          { key: 'streams', label: 'Active Streams' },
          { key: 'recordings', label: 'Recordings' },
          { key: 'config', label: 'Configuration' },
        ].map((tab) => (
          <button
            key={tab.key}
            className={`px-4 py-2 text-sm border-b-2 transition-colors ${
              selectedTab === tab.key
                ? 'border-[var(--accent)] text-[var(--accent)]'
                : 'border-transparent text-[var(--text-dim)] hover:text-[var(--text)]'
            }`}
            onClick={() => setSelectedTab(tab.key as any)}
          >
            {tab.label}
          </button>
        ))}
      </div>

      {/* Tab Content */}
      <div className="mt-4">
        {selectedTab === 'setup' && <SetupTab setupData={setupData} />}
        {selectedTab === 'streams' && <StreamsTab activePaths={activePaths} onPushStream={pushRTSPStream} />}
        {selectedTab === 'recordings' && <RecordingsTab recordings={recordings} />}
        {selectedTab === 'config' && (
          <p className="text-xs text-[var(--text-dim)] mb-3">
            Read-only view. To change these settings, open <Link to="/updates" className="text-[var(--accent)] hover:underline">Media Server Config</Link>.
          </p>
        )}
        {selectedTab === 'config' && <ConfigTab globalConfig={globalConfig} onReload={loadData} />}
      </div>
    </div>
  )
}

function SetupTab({ setupData }: { setupData: any }) {
  const { showSuccess, showError } = useSnackbar()
  if (!setupData) return <div className="text-sm text-[var(--text-dim)]">Loading setup data...</div>

  return (
    <div className="space-y-4">
      <div className="bg-[var(--panel-2)] border border-[var(--border)] p-4">
  <h3 className="text-base font-medium mb-3">Media Server Setup Instructions</h3>
        <div className="space-y-2 text-sm">
          {setupData.instructions?.map((instruction: string, index: number) => (
            <div key={index} className="flex items-start gap-2">
              <span className="text-[var(--accent)] font-medium">{index + 1}.</span>
              <span>{instruction.replace(/^\d+\.\s*/, '')}</span>
            </div>
          ))}
        </div>
      </div>

      <div className="bg-[var(--panel-2)] border border-[var(--border)] p-4">
        <h3 className="text-base font-medium mb-3">Environment Variables</h3>
        <div className="grid gap-2 text-sm font-mono">
          {Object.entries(setupData.environment_variables || {}).map(([key, value]) => (
            <div key={key} className="flex items-center justify-between bg-[var(--panel)] border border-[var(--border)] p-2">
              <span className="text-[var(--accent)]">{key}</span>
              <span className="text-[var(--text-dim)]">{value as string}</span>
            </div>
          ))}
        </div>
      </div>

      <div className="bg-[var(--panel-2)] border border-[var(--border)] p-4">
  <h3 className="text-base font-medium mb-3">Media Server Configuration (YAML)</h3>
        <div className="bg-[var(--panel)] border border-[var(--border)] p-3">
          <pre className="text-xs overflow-x-auto text-[var(--text-dim)]">
            {setupData.configuration_yaml}
          </pre>
        </div>
        <button
          className="mt-2 px-3 py-1 bg-[var(--accent)] text-white text-sm"
          onClick={() => {
            navigator.clipboard.writeText(setupData.configuration_yaml).then(
              () => showSuccess('Configuration copied to clipboard!'),
              () => showError('Could not copy to the clipboard'),
            )
          }}
        >
          Copy to Clipboard
        </button>
      </div>
    </div>
  )
}

function StreamsTab({ activePaths, onPushStream }: { activePaths: any[]; onPushStream: (cameraId: number, rtspUrl: string, enableRecording: boolean) => Promise<any> }) {
  const [showPushForm, setShowPushForm] = useState(false)
  const [pushForm, setPushForm] = useState({
    cameraId: '',
    rtspUrl: '',
    enableRecording: false
  })
  const [pushing, setPushing] = useState(false)

  const handlePushStream = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!pushForm.cameraId || !pushForm.rtspUrl) return

    setPushing(true)
    try {
      await onPushStream(parseInt(pushForm.cameraId), pushForm.rtspUrl, pushForm.enableRecording)
      setPushForm({ cameraId: '', rtspUrl: '', enableRecording: false })
      setShowPushForm(false)
    } catch (e) {
      // Error handled in parent
    } finally {
      setPushing(false)
    }
  }

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h3 className="text-base font-medium">Active Streams ({activePaths.length})</h3>
        <button
          className="px-3 py-1 bg-[var(--accent)] text-white text-sm"
          onClick={() => setShowPushForm(true)}
        >
          Push RTSP Stream
        </button>
      </div>

      {showPushForm && (
        <form onSubmit={handlePushStream} className="bg-[var(--panel-2)] border border-[var(--border)] p-4 space-y-3">
          <h4 className="font-medium">Push RTSP Stream to Media Server</h4>
          <div className="grid grid-cols-2 gap-3">
            <div>
              <label className="block text-sm text-[var(--text-dim)] mb-1">Camera ID</label>
              <input
                type="number"
                required
                className="w-full bg-[var(--panel)] border border-[var(--border)] px-3 py-2"
                value={pushForm.cameraId}
                onChange={(e) => setPushForm({ ...pushForm, cameraId: e.target.value })}
                placeholder="1"
              />
            </div>
            <div>
              <label className="block text-sm text-[var(--text-dim)] mb-1">RTSP URL</label>
              <input
                type="url"
                required
                className="w-full bg-[var(--panel)] border border-[var(--border)] px-3 py-2"
                value={pushForm.rtspUrl}
                onChange={(e) => setPushForm({ ...pushForm, rtspUrl: e.target.value })}
                placeholder="rtsp://camera-ip/stream"
              />
            </div>
          </div>
          <div>
            <label className="flex items-center gap-2">
              <input
                type="checkbox"
                checked={pushForm.enableRecording}
                onChange={(e) => setPushForm({ ...pushForm, enableRecording: e.target.checked })}
                className="accent-[var(--accent)]"
              />
              <span className="text-sm">Enable Recording</span>
            </label>
          </div>
          <div className="flex gap-2">
            <button
              type="submit"
              disabled={pushing}
              className="px-3 py-1 bg-[var(--accent)] text-white text-sm disabled:opacity-50"
            >
              {pushing ? 'Pushing...' : 'Push Stream'}
            </button>
            <button
              type="button"
              onClick={() => setShowPushForm(false)}
              className="px-3 py-1 bg-[var(--panel)] border border-[var(--border)] text-sm"
            >
              Cancel
            </button>
          </div>
        </form>
      )}

      {activePaths.length === 0 ? (
        <div className="bg-[var(--panel-2)] border border-[var(--border)] p-4 text-center text-[var(--text-dim)]">
          No active streams found. Push an RTSP stream to get started.
        </div>
      ) : (
        <div className="bg-[var(--panel-2)] border border-[var(--border)] overflow-x-auto">
          <table className="w-full text-sm">
            <thead className="bg-[var(--panel)] border-b border-[var(--border)]">
              <tr>
                <th className="text-left p-3">Path Name</th>
                <th className="text-left p-3">Source</th>
                <th className="text-left p-3">Ready</th>
                <th className="text-left p-3">Readers</th>
                <th className="text-left p-3">Recording</th>
              </tr>
            </thead>
            <tbody>
              {activePaths.map((path: any, index: number) => (
                <tr key={index} className="border-b border-[var(--border)] last:border-b-0">
                  <td className="p-3 font-mono text-[var(--accent)]">
                    {typeof path.name === 'string' ? path.name : (path.name || 'Unknown')}
                  </td>
                  <td className="p-3 text-[var(--text-dim)]">
                    {path.source && typeof path.source === 'object' 
                      ? `${path.source.type || 'Unknown'} (${path.source.id || 'N/A'})`
                      : path.source || 'N/A'
                    }
                  </td>
                  <td className="p-3">
                    <span className={`px-2 py-1 rounded text-xs ${
                      path.ready ? 'bg-[color-mix(in_oklab,var(--ok)_14%,transparent)] text-[var(--ok)]' : 'bg-[color-mix(in_oklab,var(--danger)_14%,transparent)] text-[var(--danger)]'
                    }`}>
                      {path.ready ? 'Ready' : 'Not Ready'}
                    </span>
                  </td>
                  <td className="p-3">{path.readerCount || 0}</td>
                  <td className="p-3">
                    <span className={`px-2 py-1 rounded text-xs ${
                      path.record ? 'bg-[color-mix(in_oklab,var(--accent)_14%,transparent)] text-[var(--accent)]' : 'bg-[var(--badge-neutral-bg)] text-[var(--text-dim)]'
                    }`}>
                      {path.record ? 'Enabled' : 'Disabled'}
                    </span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}

function RecordingsTab({ recordings }: { recordings: any[] }) {
  return (
    <div className="space-y-4">
  <h3 className="text-base font-medium">Media Server Recordings ({recordings.length})</h3>
      
      {recordings.length === 0 ? (
        <div className="bg-[var(--panel-2)] border border-[var(--border)] p-4 text-center text-[var(--text-dim)]">
          No recordings found via media server API.
        </div>
      ) : (
        <div className="bg-[var(--panel-2)] border border-[var(--border)] overflow-x-auto">
          <table className="w-full text-sm">
            <thead className="bg-[var(--panel)] border-b border-[var(--border)]">
              <tr>
                <th className="text-left p-3">Path</th>
                <th className="text-left p-3">Segment</th>
                <th className="text-left p-3">Duration</th>
                <th className="text-left p-3">Size</th>
                <th className="text-left p-3">Actions</th>
              </tr>
            </thead>
            <tbody>
              {recordings.map((recording: any, index: number) => (
                <tr key={index} className="border-b border-[var(--border)] last:border-b-0">
                  <td className="p-3 font-mono text-[var(--accent)]">
                    {typeof recording.path === 'string' ? recording.path : (recording.path || 'Unknown')}
                  </td>
                  <td className="p-3 text-[var(--text-dim)]">
                    {typeof recording.segment === 'string' ? recording.segment : (recording.segment || 'Unknown')}
                  </td>
                  <td className="p-3">{recording.duration || 'N/A'}</td>
                  <td className="p-3">{recording.size || 'N/A'}</td>
                  <td className="p-3">
                    <button className="px-2 py-1 bg-[color-mix(in_oklab,var(--danger)_14%,transparent)] text-[var(--danger)] text-xs">
                      Delete
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}

function ConfigTab({ globalConfig, onReload }: { globalConfig: any; onReload: () => void }) {
  if (!globalConfig) return <div className="text-sm text-[var(--text-dim)]">Loading configuration...</div>

  if (globalConfig.status === 'not_configured') {
    return (
      <div className="bg-[color-mix(in_oklab,var(--warn)_14%,transparent)] border border-[color-mix(in_oklab,var(--warn)_45%,var(--border))] text-[var(--warn)] p-4">
        <h3 className="font-medium mb-2">Media Server Not Configured</h3>
        <p className="text-sm mb-3">
          Media server API is not accessible. Please check your environment variables and ensure the media server is running.
        </p>
        <button
          className="px-3 py-1 bg-[var(--accent)] text-white text-sm"
          onClick={onReload}
        >
          Retry Connection
        </button>
      </div>
    )
  }

  return (
    <div className="space-y-4">
  <h3 className="text-base font-medium">Media Server Global Configuration</h3>
      
      <div className="bg-[var(--panel-2)] border border-[var(--border)] p-4">
        <div className="bg-[var(--panel)] border border-[var(--border)] p-3">
          <pre className="text-xs overflow-x-auto text-[var(--text-dim)]">
            {JSON.stringify(globalConfig.details, null, 2)}
          </pre>
        </div>
      </div>
    </div>
  )
}
