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

type DetectionResult = {
  id: number
  model_id: number
  model_name?: string
  camera_id?: number
  camera_name?: string
  task: string
  label?: string
  confidence?: number
  bbox_x?: number
  bbox_y?: number
  bbox_width?: number
  bbox_height?: number
  count?: number
  caption?: string
  latency_ms?: number
  annotated_image_uri?: string
  executed_at?: string
  created_at: string
}

type AIModel = {
  id: number
  name: string
  task: string
}

type Camera = {
  id: number
  name: string
  ip_address?: string
  port?: number
  location?: string
  status?: string
  manufacturer?: string
  model?: string
  is_active?: boolean
}

type CameraStats = {
  camera_id: number
  camera_name: string
  total_detections: number
  latest_detection?: string
}

export function AIDetectionResults() {
  const { user } = useAuth()
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [results, setResults] = useState<DetectionResult[]>([])
  const [models, setModels] = useState<AIModel[]>([])
  const [cameras, setCameras] = useState<Camera[]>([])
  const [selectedCameraId, setSelectedCameraId] = useState<number | null>(null)
  const [showCameraDialog, setShowCameraDialog] = useState(false)
  const [dialogCamera, setDialogCamera] = useState<Camera | null>(null)
  
  // Filters
  const [filters, setFilters] = useState({
    model_id: '',
    task: '',
    limit: 100,
  })

  // Load models for filter
  useEffect(() => {
    loadModels()
    loadCameras()
  }, [])

  // Load results
  useEffect(() => {
    loadResults()
  }, [filters, selectedCameraId])

  // Auto-refresh results every 5 seconds for real-time updates
  useEffect(() => {
    const interval = setInterval(() => {
      loadResults()
    }, 5000); // Refresh every 5 seconds

    return () => clearInterval(interval);
  }, [filters, selectedCameraId])

  async function loadModels() {
    try {
      const res = await apiService.getAIModels({ limit: 200 })
      setModels(res.data)
    } catch (e) {
      console.error('Failed to load models:', e)
    }
  }

  async function loadCameras() {
    try {
      const res = await apiService.getCameras({ limit: 200 })
      setCameras(res.data.cameras || res.data || [])
    } catch (e) {
      console.error('Failed to load cameras:', e)
    }
  }

  async function loadResults() {
    try {
      setLoading(true)
      setError(null)
      const params: any = { limit: filters.limit }
      if (filters.model_id) params.model_id = parseInt(filters.model_id)
      if (filters.task) params.task = filters.task
      if (selectedCameraId) params.camera_id = selectedCameraId
      
      const res = await apiService.getDetectionResults(params)
      setResults(res.data)
    } catch (e: any) {
      setError(e?.data?.detail || e?.message || 'Failed to load detection results')
    } finally {
      setLoading(false)
    }
  }

  // Auto-dismiss notices
  useEffect(() => {
    if (notice) {
      const timer = setTimeout(() => setNotice(null), 5000)
      return () => clearTimeout(timer)
    }
  }, [notice])

  async function handleDelete(id: number) {
    if (!confirm('Are you sure you want to delete this detection result?')) return

    try {
      setLoading(true)
      setError(null)
      await apiService.deleteDetectionResult(id)
      setNotice('Detection result deleted successfully')
      await loadResults()
    } catch (e: any) {
      setError(e?.data?.detail || e?.message || 'Failed to delete detection result')
    } finally {
      setLoading(false)
    }
  }

  async function handleDeleteOld(days: number) {
    if (!confirm(`Are you sure you want to delete all results older than ${days} days?`)) return

    try {
      setLoading(true)
      setError(null)
      const res = await apiService.deleteOldDetectionResults(days)
      setNotice(res.data.message)
      await loadResults()
    } catch (e: any) {
      setError(e?.data?.detail || e?.message || 'Failed to delete old results')
    } finally {
      setLoading(false)
    }
  }

  function formatConfidence(confidence?: number) {
    if (!confidence) return '-'
    const pct = (confidence * 100).toFixed(1)
    const colorClass = 
      confidence >= 0.8 ? 'text-green-400' :
      confidence >= 0.6 ? 'text-yellow-400' :
      'text-orange-400'
    return <span className={`font-medium ${colorClass}`}>{pct}%</span>
  }

  function formatBBox(result: DetectionResult) {
    if (!result.bbox_x || !result.bbox_y) return '-'
    return (
      <div className="text-xs font-mono">
        <div>X: {result.bbox_x}, Y: {result.bbox_y}</div>
        <div className="text-[var(--text-dim)]">
          {result.bbox_width} × {result.bbox_height}
        </div>
      </div>
    )
  }

  function getUniqueTask(results: DetectionResult[]) {
    return Array.from(new Set(results.map(r => r.task)))
  }

  function handleCameraClick(camera: Camera) {
    setDialogCamera(camera)
    setShowCameraDialog(true)
    setSelectedCameraId(camera.id)
  }

  // Calculate camera statistics
  const cameraStats = cameras.map(camera => {
    const cameraResults = results.filter(r => r.camera_id === camera.id)
    return {
      camera_id: camera.id,
      camera_name: camera.name,
      total_detections: cameraResults.length,
      latest_detection: cameraResults.length > 0 ? cameraResults[0]?.created_at : undefined
    }
  }).filter(stat => stat.total_detections > 0 || selectedCameraId === stat.camera_id)

  // Get filtered results for selected camera
  const filteredResults = selectedCameraId 
    ? results.filter(r => r.camera_id === selectedCameraId)
    : results

  return (
    <div className="flex gap-4 h-[calc(100vh-8rem)]">
      {/* Main Content */}
      <section className="flex-1 flex flex-col overflow-hidden">
      {/* Fixed Header */}
      <div className="flex items-center justify-between mb-4">
        <h1 className="text-lg font-semibold">
          AI Detection Results
          {selectedCameraId && (
            <span className="ml-2 text-sm text-[var(--text-dim)]">
              - {cameras.find(c => c.id === selectedCameraId)?.name}
            </span>
          )}
        </h1>
        <div className="flex gap-2">
          <button
            onClick={() => loadResults()}
            className="px-3 py-1 bg-[var(--panel)] border border-neutral-700 rounded text-sm hover:bg-[var(--panel-2)]"
          >
            Refresh
          </button>
          {user?.is_superuser && (
            <button
              onClick={() => handleDeleteOld(7)}
              className="px-3 py-1 bg-red-600/20 border border-red-600/50 rounded text-red-300 hover:bg-red-600/30 text-sm"
            >
              Delete Old (7d+)
            </button>
          )}
        </div>
      </div>

      {/* Notifications */}
      {notice && (
        <div className="p-2 rounded bg-green-500/10 border border-green-500/30 text-green-300 text-sm mb-4">
          {notice}
        </div>
      )}
      {error && (
        <div className="p-2 rounded bg-red-500/10 border border-red-500/30 text-red-300 text-sm mb-4">
          {error}
        </div>
      )}

      {/* Fixed Filters */}
      <div className="border border-neutral-700 bg-[var(--panel-2)] p-3 rounded mb-4">
        <div className="grid grid-cols-1 md:grid-cols-4 gap-3">
          <div>
            <label className="block text-xs text-[var(--text-dim)] mb-1">Filter by Model</label>
            <select
              className="select w-full text-sm"
              value={filters.model_id}
              onChange={(e) => setFilters({ ...filters, model_id: e.target.value })}
            >
              <option value="">All Models</option>
              {models.map((model) => (
                <option key={model.id} value={model.id}>
                  {model.name}
                </option>
              ))}
            </select>
          </div>

          <div>
            <label className="block text-xs text-[var(--text-dim)] mb-1">Filter by Task</label>
            <select
              className="select w-full text-sm"
              value={filters.task}
              onChange={(e) => setFilters({ ...filters, task: e.target.value })}
            >
              <option value="">All Tasks</option>
              {getUniqueTask(results).map((task) => (
                <option key={task} value={task}>
                  {task}
                </option>
              ))}
            </select>
          </div>

          <div>
            <label className="block text-xs text-[var(--text-dim)] mb-1">Limit</label>
            <select
              className="select w-full text-sm"
              value={filters.limit}
              onChange={(e) => setFilters({ ...filters, limit: parseInt(e.target.value) })}
            >
              <option value="50">50</option>
              <option value="100">100</option>
              <option value="200">200</option>
              <option value="500">500</option>
            </select>
          </div>

          <div className="flex items-end">
            <button
              onClick={() => setFilters({ model_id: '', task: '', limit: 100 })}
              className="px-3 py-1 bg-[var(--panel)] border border-neutral-700 rounded text-sm hover:bg-[var(--panel-2)] w-full"
            >
              Clear Filters
            </button>
          </div>
        </div>
      </div>

      {/* Scrollable Results Table */}
      <div className="flex-1 overflow-hidden border border-neutral-700 bg-[var(--panel-2)] rounded">
        <div className="p-3 border-b border-neutral-700">
          <h2 className="text-md font-medium">Detection Results ({filteredResults.length})</h2>
        </div>

        {loading ? (
          <div className="p-4 text-center text-sm text-[var(--text-dim)]">Loading...</div>
        ) : filteredResults.length === 0 ? (
          <div className="p-4 text-center text-sm text-[var(--text-dim)]">
            No detection results found. Run inference to generate results.
          </div>
        ) : (
          <div className="overflow-auto h-[calc(100%-3.5rem)]">
            <table className="w-full text-sm">
              <thead className="bg-[var(--panel)] text-[var(--text-dim)] sticky top-0 z-10">
                <tr>
                  <th className="text-left p-3">ID</th>
                  <th className="text-left p-3">Model</th>
                  <th className="text-left p-3">Task</th>
                  <th className="text-left p-3">Label</th>
                  <th className="text-left p-3">Confidence</th>
                  <th className="text-left p-3">BBox</th>
                  <th className="text-left p-3">Count</th>
                  <th className="text-left p-3">Latency</th>
                  <th className="text-left p-3">Timestamp</th>
                  <th className="text-center p-3">Actions</th>
                </tr>
              </thead>
              <tbody>
                {filteredResults.map((result) => (
                  <tr
                    key={result.id}
                    className="border-t border-neutral-700 hover:bg-[var(--panel)]/50"
                  >
                    <td className="p-3 font-mono text-xs text-[var(--text-dim)]">
                      #{result.id}
                    </td>
                    <td className="p-3">
                      <div className="text-xs">
                        <div className="font-medium">{result.model_name || `Model ${result.model_id}`}</div>
                        <div className="text-[var(--text-dim)]">Cam {result.camera_id || 'N/A'}</div>
                      </div>
                    </td>
                    <td className="p-3">
                      <span className="text-xs bg-blue-500/20 text-blue-300 px-2 py-1 rounded">
                        {result.task}
                      </span>
                    </td>
                    <td className="p-3">
                      {result.label ? (
                        <span className="text-xs bg-green-500/20 text-green-300 px-2 py-1 rounded">
                          {result.label}
                        </span>
                      ) : result.caption ? (
                        <div className="text-xs italic max-w-xs truncate" title={result.caption}>
                          {result.caption}
                        </div>
                      ) : (
                        <span className="text-neutral-500">-</span>
                      )}
                    </td>
                    <td className="p-3">{formatConfidence(result.confidence)}</td>
                    <td className="p-3">{formatBBox(result)}</td>
                    <td className="p-3 text-center">
                      {result.count !== null && result.count !== undefined ? (
                        <span className="font-bold text-green-400">{result.count}</span>
                      ) : (
                        <span className="text-neutral-500">-</span>
                      )}
                    </td>
                    <td className="p-3">
                      {result.latency_ms ? (
                        <span className={`text-xs ${
                          result.latency_ms < 200 ? 'text-green-400' :
                          result.latency_ms < 500 ? 'text-yellow-400' :
                          'text-orange-400'
                        }`}>
                          {result.latency_ms}ms
                        </span>
                      ) : (
                        <span className="text-neutral-500">-</span>
                      )}
                    </td>
                    <td className="p-3 text-xs text-[var(--text-dim)]">
                      {new Date(result.created_at).toLocaleString()}
                    </td>
                    <td className="p-3 text-center">
                      {user?.is_superuser && (
                        <button
                          onClick={() => handleDelete(result.id)}
                          className="px-2 py-1 bg-red-600/20 border border-red-600/50 rounded text-red-300 hover:bg-red-600/30 text-xs"
                        >
                          Delete
                        </button>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </section>

    {/* Right Sidebar - Camera List */}
    <aside className="w-80 border-l border-neutral-700 bg-[var(--panel-2)] overflow-auto">
      <div className="sticky top-0 bg-[var(--panel-2)] border-b border-neutral-700 p-3 z-10">
        <h2 className="text-md font-medium mb-2">Cameras</h2>
        <button
          onClick={() => setSelectedCameraId(null)}
          className={`w-full px-3 py-2 text-sm rounded border transition-colors ${
            selectedCameraId === null
              ? 'bg-[var(--accent)] border-[var(--accent)] text-white'
              : 'bg-[var(--panel)] border-neutral-700 hover:bg-[var(--panel-2)]'
          }`}
        >
          <div className="flex items-center justify-between">
            <span>All Cameras</span>
            <span className="text-xs opacity-75">{results.length} results</span>
          </div>
        </button>
      </div>

      <div className="p-3 space-y-2">
        {cameras.length === 0 ? (
          <div className="text-sm text-[var(--text-dim)] text-center py-4">
            No cameras found
          </div>
        ) : (
          cameras.map((camera) => {
            const stats = cameraStats.find(s => s.camera_id === camera.id)
            const count = results.filter(r => r.camera_id === camera.id).length
            const isSelected = selectedCameraId === camera.id

            return (
              <button
                key={camera.id}
                onClick={() => handleCameraClick(camera)}
                className={`w-full text-left p-3 rounded border transition-colors ${
                  isSelected
                    ? 'bg-[var(--accent)]/20 border-[var(--accent)] hover:bg-[var(--accent)]/30'
                    : 'bg-[var(--panel)] border-neutral-700 hover:bg-[var(--bg-2)]'
                }`}
              >
                <div className="flex items-start justify-between mb-1">
                  <div className="font-medium text-sm truncate flex-1">{camera.name}</div>
                  <span className={`ml-2 text-xs px-2 py-0.5 rounded ${
                    count > 0 ? 'bg-green-500/20 text-green-400' : 'bg-gray-500/20 text-gray-400'
                  }`}>
                    {count}
                  </span>
                </div>
                <div className="text-xs text-[var(--text-dim)]">
                  ID: {camera.id}
                </div>
                {stats?.latest_detection && (
                  <div className="text-[10px] text-[var(--text-dim)] mt-1">
                    Latest: {new Date(stats.latest_detection).toLocaleTimeString()}
                  </div>
                )}
              </button>
            )
          })
        )}
      </div>
    </aside>

    {/* Camera Details Dialog */}
    {showCameraDialog && dialogCamera && (
      <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60" onClick={() => setShowCameraDialog(false)}>
        <div className="bg-[var(--panel-2)] border border-neutral-700 rounded-lg shadow-2xl w-full max-w-2xl mx-4" onClick={(e) => e.stopPropagation()}>
          <div className="flex items-center justify-between p-4 border-b border-neutral-700">
            <h2 className="text-lg font-semibold">Camera Details</h2>
            <button
              onClick={() => setShowCameraDialog(false)}
              className="p-1 hover:bg-[var(--panel)] rounded transition-colors"
            >
              <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
              </svg>
            </button>
          </div>

          <div className="p-4 space-y-4 max-h-[70vh] overflow-auto">
            {/* Camera Info */}
            <div className="space-y-3">
              <h3 className="text-sm font-medium text-[var(--text-dim)] uppercase tracking-wide">Camera Information</h3>
              <div className="grid grid-cols-2 gap-3 text-sm">
                <div>
                  <div className="text-xs text-[var(--text-dim)] mb-1">Name</div>
                  <div className="font-medium">{dialogCamera.name}</div>
                </div>
                <div>
                  <div className="text-xs text-[var(--text-dim)] mb-1">ID</div>
                  <div className="font-mono text-xs">{dialogCamera.id}</div>
                </div>
                {dialogCamera.ip_address && (
                  <div>
                    <div className="text-xs text-[var(--text-dim)] mb-1">IP Address</div>
                    <div className="font-mono text-xs">{dialogCamera.ip_address}:{dialogCamera.port || 554}</div>
                  </div>
                )}
                {dialogCamera.status && (
                  <div>
                    <div className="text-xs text-[var(--text-dim)] mb-1">Status</div>
                    <div>
                      <span className={`inline-block px-2 py-0.5 rounded text-xs ${
                        dialogCamera.status === 'active' || dialogCamera.status === 'provisioned' 
                          ? 'bg-green-500/20 text-green-400' 
                          : 'bg-gray-500/20 text-gray-400'
                      }`}>
                        {dialogCamera.status}
                      </span>
                    </div>
                  </div>
                )}
                {dialogCamera.manufacturer && (
                  <div>
                    <div className="text-xs text-[var(--text-dim)] mb-1">Manufacturer</div>
                    <div>{dialogCamera.manufacturer}</div>
                  </div>
                )}
                {dialogCamera.model && (
                  <div>
                    <div className="text-xs text-[var(--text-dim)] mb-1">Model</div>
                    <div>{dialogCamera.model}</div>
                  </div>
                )}
                {dialogCamera.location && (
                  <div>
                    <div className="text-xs text-[var(--text-dim)] mb-1">Location</div>
                    <div>{dialogCamera.location}</div>
                  </div>
                )}
                <div>
                  <div className="text-xs text-[var(--text-dim)] mb-1">Active</div>
                  <div>{dialogCamera.is_active ? 'Yes' : 'No'}</div>
                </div>
              </div>
            </div>

            {/* Detection Statistics */}
            <div className="space-y-3">
              <h3 className="text-sm font-medium text-[var(--text-dim)] uppercase tracking-wide">Detection Statistics</h3>
              <div className="grid grid-cols-3 gap-3">
                <div className="bg-[var(--panel)] p-3 rounded border border-neutral-700">
                  <div className="text-2xl font-bold text-green-400">
                    {results.filter(r => r.camera_id === dialogCamera.id).length}
                  </div>
                  <div className="text-xs text-[var(--text-dim)] mt-1">Total Detections</div>
                </div>
                <div className="bg-[var(--panel)] p-3 rounded border border-neutral-700">
                  <div className="text-2xl font-bold text-blue-400">
                    {new Set(results.filter(r => r.camera_id === dialogCamera.id).map(r => r.model_id)).size}
                  </div>
                  <div className="text-xs text-[var(--text-dim)] mt-1">Models Used</div>
                </div>
                <div className="bg-[var(--panel)] p-3 rounded border border-neutral-700">
                  <div className="text-2xl font-bold text-purple-400">
                    {new Set(results.filter(r => r.camera_id === dialogCamera.id).map(r => r.task)).size}
                  </div>
                  <div className="text-xs text-[var(--text-dim)] mt-1">Task Types</div>
                </div>
              </div>
            </div>

            {/* Models Running on Camera */}
            <div className="space-y-3">
              <h3 className="text-sm font-medium text-[var(--text-dim)] uppercase tracking-wide">Models & Tasks</h3>
              <div className="space-y-2">
                {Array.from(new Set(results.filter(r => r.camera_id === dialogCamera.id).map(r => r.model_id))).map(modelId => {
                  const model = models.find(m => m.id === modelId)
                  const modelResults = results.filter(r => r.camera_id === dialogCamera.id && r.model_id === modelId)
                  const tasks = Array.from(new Set(modelResults.map(r => r.task)))
                  const avgLatency = modelResults.reduce((sum, r) => sum + (r.latency_ms || 0), 0) / modelResults.length

                  return (
                    <div key={modelId} className="bg-[var(--panel)] p-3 rounded border border-neutral-700">
                      <div className="flex items-start justify-between mb-2">
                        <div>
                          <div className="font-medium text-sm">{model?.name || `Model ${modelId}`}</div>
                          <div className="flex gap-1 mt-1">
                            {tasks.map(task => (
                              <span key={task} className="text-xs bg-blue-500/20 text-blue-300 px-2 py-0.5 rounded">
                                {task}
                              </span>
                            ))}
                          </div>
                        </div>
                        <span className="text-xs bg-green-500/20 text-green-400 px-2 py-1 rounded">
                          {modelResults.length} detections
                        </span>
                      </div>
                      {avgLatency > 0 && (
                        <div className="text-xs text-[var(--text-dim)]">
                          Avg. Latency: <span className={avgLatency < 200 ? 'text-green-400' : avgLatency < 500 ? 'text-yellow-400' : 'text-orange-400'}>
                            {avgLatency.toFixed(0)}ms
                          </span>
                        </div>
                      )}
                    </div>
                  )
                })}
                {results.filter(r => r.camera_id === dialogCamera.id).length === 0 && (
                  <div className="text-sm text-[var(--text-dim)] text-center py-4">
                    No detection results for this camera
                  </div>
                )}
              </div>
            </div>

            {/* Recent Detections */}
            {results.filter(r => r.camera_id === dialogCamera.id).length > 0 && (
              <div className="space-y-3">
                <h3 className="text-sm font-medium text-[var(--text-dim)] uppercase tracking-wide">Recent Detections</h3>
                <div className="space-y-2 max-h-48 overflow-auto">
                  {results.filter(r => r.camera_id === dialogCamera.id).slice(0, 5).map(result => (
                    <div key={result.id} className="bg-[var(--panel)] p-2 rounded border border-neutral-700 text-xs">
                      <div className="flex items-center justify-between mb-1">
                        <span className="font-medium">{result.model_name || `Model ${result.model_id}`}</span>
                        <span className="text-[var(--text-dim)]">{new Date(result.created_at).toLocaleString()}</span>
                      </div>
                      <div className="flex gap-2">
                        <span className="bg-blue-500/20 text-blue-300 px-2 py-0.5 rounded">{result.task}</span>
                        {result.label && (
                          <span className="bg-green-500/20 text-green-300 px-2 py-0.5 rounded">{result.label}</span>
                        )}
                        {result.confidence && (
                          <span className="text-[var(--text-dim)]">Confidence: {(result.confidence * 100).toFixed(1)}%</span>
                        )}
                      </div>
                    </div>
                  ))}
                </div>
              </div>
            )}
          </div>

          <div className="flex justify-end gap-2 p-4 border-t border-neutral-700">
            <button
              onClick={() => setShowCameraDialog(false)}
              className="px-4 py-2 bg-[var(--panel)] border border-neutral-700 rounded hover:bg-[var(--bg-2)] transition-colors text-sm"
            >
              Close
            </button>
          </div>
        </div>
      </div>
    )}
  </div>
  )
}
