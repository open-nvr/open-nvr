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
import { RecordingBrowser } from '../components/RecordingBrowser'
import { Cloud } from 'lucide-react'

type AIModel = {
  id: number
  name: string
  model_name: string
  task: string
  config?: string
  enabled: boolean
  source_type: string  // "live" or "recording"
  assigned_camera_id?: number | null
  recording_path?: string | null
  inference_interval?: number
  created_at: string
  updated_at?: string
}

type Camera = {
  id: number
  name: string
  source_url?: string
}

type CloudCredential = {
  id: string
  provider: string
  account_info?: Record<string, any>
  created_at: string
}

type CloudModel = {
  id: number
  name: string
  provider: string
  credential_id: string
  model_id: string
  task: string
  config?: string  // JSON string
  enabled: boolean
  created_at: string
}

const AVAILABLE_MODELS = [
  { value: 'yolov8', label: 'YOLOv8' },
  { value: 'yolov11', label: 'YOLOv11' },
  { value: 'blip', label: 'BLIP (Image Captioning)' },
  { value: 'insightface', label: 'InsightFace (Face Recognition)' },
]

const AVAILABLE_TASKS = [
  { value: 'person_detection', label: 'Person Detection' },
  { value: 'person_counting', label: 'Person Counting' },
  { value: 'scene_description', label: 'Scene Description' },
  { value: 'face_detection', label: 'Face Detection' },
  { value: 'face_recognition', label: 'Face Recognition' },
  { value: 'face_verify', label: 'Face Verification' },
  { value: 'watchlist_check', label: 'Watchlist Check' },
]

export function AIModelsBYOM() {
  const { user } = useAuth()
  const canAdmin = !!user?.is_superuser
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [models, setModels] = useState<AIModel[]>([])
  const [cameras, setCameras] = useState<Camera[]>([])
  const [runningModels, setRunningModels] = useState<Set<number>>(new Set())
  const [inferenceLoading, setInferenceLoading] = useState<Set<number>>(new Set())
  
  // Form state
  const [formData, setFormData] = useState({
    name: '',
    model_name: 'yolov8',
    task: 'person_detection',
    config: '',
    enabled: true,
    source_type: 'live',  // "live" or "recording"
    assigned_camera_id: null as number | null,
    recording_path: null as string | null,
    inference_interval: 2,
  })

  // Recording browser state
  const [showRecordingBrowser, setShowRecordingBrowser] = useState(false)
  const [selectedRecording, setSelectedRecording] = useState<{
    camera_id: number;
    session_id: string;
    segments: string[];
    start_time?: string;
    end_time?: string;
  } | null>(null)

  // Editing state
  const [editingId, setEditingId] = useState<number | null>(null)

  // Cloud AI Dialog state
  const [showCloudDialog, setShowCloudDialog] = useState(false)
  const [cloudTab, setCloudTab] = useState<'credentials' | 'models'>('credentials')
  const [cloudCredentials, setCloudCredentials] = useState<CloudCredential[]>([])
  const [cloudModels, setCloudModels] = useState<CloudModel[]>([])
  const [cloudLoading, setCloudLoading] = useState(false)
  const [showAddCredential, setShowAddCredential] = useState(false)
  const [showAddCloudModel, setShowAddCloudModel] = useState(false)
  const [newCredential, setNewCredential] = useState({ provider: 'huggingface', token: '', account_info: '' })
  const [newCloudModel, setNewCloudModel] = useState({
    name: '',
    provider: 'huggingface',
    credential_id: '',
    model_id: '',
    task: 'image-to-text',
    config: '{}',
    enabled: true
  })

  // Load models, cameras, and running inference status
  useEffect(() => {
    loadModels()
    loadCameras()
    loadRunningInference()
    loadCloudModels()
  }, [])
  
  // Debug: Log when recording_path changes
  useEffect(() => {
    console.log('[DEBUG] formData.recording_path:', formData.recording_path);
    console.log('[DEBUG] formData.assigned_camera_id:', formData.assigned_camera_id);
  }, [formData.recording_path, formData.assigned_camera_id]);
  
  // Poll for running inference status every 5 seconds
  useEffect(() => {
    const interval = setInterval(loadRunningInference, 5000)
    return () => clearInterval(interval)
  }, [])

  async function loadModels() {
    try {
      setLoading(true)
      setError(null)
      const res = await apiService.getAIModels({ limit: 200 })
      setModels(res.data)
    } catch (e: any) {
      setError(e?.data?.detail || e?.message || 'Failed to load AI models')
    } finally {
      setLoading(false)
    }
  }

  async function loadCameras() {
    try {
      const { data } = await apiService.getCameras({ limit: 200, active_only: true })
      const cameraList = Array.isArray(data?.cameras) ? data.cameras : []
      
      // Fetch camera configs to get RTSP URLs
      const camerasWithUrls = await Promise.all(
        cameraList.map(async (cam: any) => {
          try {
            const configRes = await apiService.getCameraConfig(cam.id)
            return {
              id: cam.id,
              name: cam.name || `Camera ${cam.id}`,
              source_url: configRes?.data?.source_url || null,
            }
          } catch {
            return {
              id: cam.id,
              name: cam.name || `Camera ${cam.id}`,
              source_url: null,
            }
          }
        })
      )
      setCameras(camerasWithUrls)
    } catch (e) {
      console.error('Failed to load cameras:', e)
    }
  }

  // Load running inference status from backend
  async function loadRunningInference() {
    try {
      const res = await apiService.getRunningInference()
      const runningIds = res.data.models.map((m: any) => m.id)
      setRunningModels(new Set(runningIds))
    } catch (e) {
      // Silently fail - we'll retry on next poll
      console.error('Failed to load running inference status:', e)
    }
  }

  // Start inference for a model (backend manages it)
  async function startInference(model: AIModel) {
    if (!model.assigned_camera_id) {
      setError('Please assign a camera to this model first')
      return
    }

    setInferenceLoading(prev => new Set(prev).add(model.id))
    
    try {
      const res = await apiService.startModelInference(model.id)
      setNotice(res.data.message)
      await loadRunningInference() // Refresh status
    } catch (e: any) {
      setError(e?.data?.detail || e?.message || 'Failed to start inference')
    } finally {
      setInferenceLoading(prev => {
        const next = new Set(prev)
        next.delete(model.id)
        return next
      })
    }
  }

  // Stop inference for a model (backend manages it)
  async function stopInference(modelId: number) {
    setInferenceLoading(prev => new Set(prev).add(modelId))
    
    try {
      const res = await apiService.stopModelInference(modelId)
      setNotice(res.data.message)
      await loadRunningInference() // Refresh status
    } catch (e: any) {
      setError(e?.data?.detail || e?.message || 'Failed to stop inference')
    } finally {
      setInferenceLoading(prev => {
        const next = new Set(prev)
        next.delete(modelId)
        return next
      })
    }
  }

  // Toggle inference on/off
  async function toggleInference(model: AIModel) {
    if (runningModels.has(model.id)) {
      await stopInference(model.id)
    } else {
      await startInference(model)
    }
  }

  // Poll for recording inference completion
  function pollRecordingCompletion(modelId: number) {
    const interval = setInterval(async () => {
      try {
        const statusRes = await apiService.getInferenceStatus(modelId);
        if (!statusRes.data.running) {
          // Recording inference completed
          setRunningModels(prev => {
            const next = new Set(prev);
            next.delete(modelId);
            return next;
          });
          clearInterval(interval);
          setNotice('Recording analysis completed! Check AI Detection Results page.');
        }
      } catch (e) {
        // If API fails, assume it's done
        clearInterval(interval);
        setRunningModels(prev => {
          const next = new Set(prev);
          next.delete(modelId);
          return next;
        });
      }
    }, 3000); // Poll every 3 seconds

    // Clean up after 30 minutes (in case it gets stuck)
    setTimeout(() => clearInterval(interval), 30 * 60 * 1000);
  }

  // Analyze recording (one-time processing)
  async function analyzeRecording(model: AIModel) {
    if (!model.recording_path || !model.assigned_camera_id) {
      setError('Recording path or camera ID is missing')
      return
    }

    setInferenceLoading(prev => new Set(prev).add(model.id))
    
    try {
      // Find the selected recording data (if available from session selection)
      const recordingData = selectedRecording && 
                            selectedRecording.camera_id === model.assigned_camera_id
                            ? selectedRecording
                            : null;

      const payload: any = {
        camera_id: model.assigned_camera_id,
        model_name: model.model_name,
        task: model.task,
        frame_interval: 30, // Extract 1 frame every 30 frames (adjustable)
        options: model.config ? JSON.parse(model.config) : {},
        model_id: model.id
      };

      // Use session-based data if available, otherwise fall back to legacy path
      if (recordingData?.session_id && recordingData?.segments) {
        payload.session_id = recordingData.session_id;
        payload.segments = recordingData.segments;
        
        // Include time range if selected
        if (recordingData.start_time && recordingData.end_time) {
          payload.start_time = recordingData.start_time;
          payload.end_time = recordingData.end_time;
        }
      } else {
        // Legacy: single recording path
        payload.recording_path = model.recording_path;
      }
      
      const res = await apiService.runRecordingInference(payload)
      const data = res.data
      
      // Add model to running state
      setRunningModels(prev => new Set(prev).add(model.id));
      
      let message = `Recording analysis started! Analyzing ${data.frames_to_analyze} frames`;
      
      if (data.segments_count > 1) {
        message += ` from ${data.segments_count} segments`;
      }
      
      if (data.time_range?.start && data.time_range?.end) {
        const start = new Date(data.time_range.start).toLocaleTimeString();
        const end = new Date(data.time_range.end).toLocaleTimeString();
        message += ` (${start} - ${end})`;
      }
      
      message += `. Estimated time: ${Math.round(data.estimated_time_seconds / 60)} minutes. `;
      message += `Results will appear in real-time on AI Detection Results page.`;
      
      setNotice(message);
      
      // Poll for completion
      pollRecordingCompletion(model.id);
    } catch (e: any) {
      setError(e?.data?.detail || e?.message || 'Failed to start recording analysis')
    } finally {
      setInferenceLoading(prev => {
        const next = new Set(prev)
        next.delete(model.id)
        return next
      })
    }
  }

  // Auto-dismiss notices
  useEffect(() => {
    if (notice) {
      const timer = setTimeout(() => setNotice(null), 5000)
      return () => clearTimeout(timer)
    }
  }, [notice])

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault()
    if (!canAdmin) return

    try {
      setLoading(true)
      setError(null)

      console.log('Submitting form with data:', formData);

      // Validate JSON config if provided
      if (formData.config) {
        try {
          JSON.parse(formData.config)
        } catch {
          setError('Invalid JSON in config field')
          setLoading(false)
          return
        }
      }

      // Validate recording source
      if (formData.source_type === 'recording') {
        if (!formData.recording_path || formData.recording_path.trim() === '') {
          setError('Please select a valid recording file by clicking Browse. Empty or invalid recording paths are not allowed.')
          setLoading(false)
          return
        }
      }

      if (editingId) {
        // Update existing model
        await apiService.updateAIModel(editingId, formData)
        setNotice('Model updated successfully')
        setEditingId(null)
      } else {
        // Create new model
        await apiService.createAIModel(formData)
        setNotice('Model created successfully')
      }

      // Reset form
      setFormData({
        name: '',
        model_name: 'yolov8',
        task: 'person_detection',
        config: '',
        enabled: true,
        source_type: 'live',
        assigned_camera_id: null,
        recording_path: null,
        inference_interval: 2,
      })

      // Reload models
      await loadModels()
    } catch (e: any) {
      setError(e?.data?.detail || e?.message || 'Failed to save model')
    } finally {
      setLoading(false)
    }
  }

  async function handleDelete(id: number, name: string) {
    if (!canAdmin) return
    if (!confirm(`Are you sure you want to delete "${name}"?`)) return

    // Stop inference if running
    if (runningModels.has(id)) {
      stopInference(id)
    }

    try {
      setLoading(true)
      setError(null)
      await apiService.deleteAIModel(id)
      setNotice(`Model "${name}" deleted successfully`)
      await loadModels()
    } catch (e: any) {
      setError(e?.data?.detail || e?.message || 'Failed to delete model')
    } finally {
      setLoading(false)
    }
  }

  function handleEdit(model: AIModel) {
    setFormData({
      name: model.name,
      model_name: model.model_name,
      task: model.task,
      config: model.config || '',
      enabled: model.enabled,
      source_type: model.source_type || 'live',
      assigned_camera_id: model.assigned_camera_id || null,
      recording_path: model.recording_path || null,
      inference_interval: model.inference_interval || 2,
    })
    setEditingId(model.id)
    // Scroll to form
    window.scrollTo({ top: 0, behavior: 'smooth' })
  }

  function handleCancelEdit() {
    setEditingId(null)
    setFormData({
      name: '',
      model_name: 'yolov8',
      task: 'person_detection',
      config: '',
      enabled: true,
      source_type: 'live',
      assigned_camera_id: null,
      recording_path: null,
      inference_interval: 2,
    })
  }

  async function toggleEnabled(id: number, currentState: boolean) {
    if (!canAdmin) return
    try {
      await apiService.updateAIModel(id, { enabled: !currentState })
      setNotice('Model status updated')
      await loadModels()
    } catch (e: any) {
      setError(e?.data?.detail || e?.message || 'Failed to update model status')
    }
  }

  // Cloud AI functions
  async function loadCloudCredentials() {
    try {
      setCloudLoading(true)
      const { data } = await apiService.getCloudCredentials()
      setCloudCredentials(data)
    } catch (e: any) {
      setError(e?.data?.detail || e?.message || 'Failed to load cloud credentials')
    } finally {
      setCloudLoading(false)
    }
  }

  async function loadCloudModels() {
    try {
      setCloudLoading(true)
      const { data } = await apiService.getCloudModels()
      setCloudModels(data)
    } catch (e: any) {
      setError(e?.data?.detail || e?.message || 'Failed to load cloud models')
    } finally {
      setCloudLoading(false)
    }
  }

  async function addCloudCredential() {
    try {
      setCloudLoading(true)
      const accountInfo = newCredential.account_info ? JSON.parse(newCredential.account_info) : undefined
      await apiService.createCloudCredential({
        provider: newCredential.provider,
        token: newCredential.token,
        account_info: accountInfo
      })
      setNotice('Cloud credential added successfully')
      setShowAddCredential(false)
      setNewCredential({ provider: 'huggingface', token: '', account_info: '' })
      loadCloudCredentials()
    } catch (e: any) {
      setError(e?.data?.detail || e?.message || 'Failed to add credential')
    } finally {
      setCloudLoading(false)
    }
  }

  async function deleteCloudCredential(id: string) {
    if (!confirm('Delete this credential? Associated models will also be deleted.')) return
    try {
      setCloudLoading(true)
      await apiService.deleteCloudCredential(id)
      setNotice('Credential deleted')
      loadCloudCredentials()
      loadCloudModels()
    } catch (e: any) {
      setError(e?.data?.detail || e?.message || 'Failed to delete credential')
    } finally {
      setCloudLoading(false)
    }
  }

  async function addCloudModel() {
    try {
      setCloudLoading(true)
      await apiService.createCloudModel({
        name: newCloudModel.name,
        provider: newCloudModel.provider,
        credential_id: newCloudModel.credential_id,
        model_id: newCloudModel.model_id,
        task: newCloudModel.task,
        config: newCloudModel.config,
        enabled: newCloudModel.enabled
      })
      setNotice('Cloud model configured successfully')
      setShowAddCloudModel(false)
      setNewCloudModel({ name: '', provider: 'huggingface', credential_id: '', model_id: '', task: 'image-to-text', config: '{}', enabled: true })
      loadCloudModels()
    } catch (e: any) {
      setError(e?.data?.detail || e?.message || 'Failed to add cloud model')
    } finally {
      setCloudLoading(false)
    }
  }

  async function deleteCloudModel(id: number) {
    if (!confirm('Delete this cloud model configuration?')) return
    try {
      setCloudLoading(true)
      await apiService.deleteCloudModel(id)
      setNotice('Cloud model deleted')
      loadCloudModels()
    } catch (e: any) {
      setError(e?.data?.detail || e?.message || 'Failed to delete cloud model')
    } finally {
      setCloudLoading(false)
    }
  }

  function openCloudDialog() {
    setShowCloudDialog(true)
    loadCloudCredentials()
    loadCloudModels()
  }

  return (
    <section className="space-y-4">
      <div className="flex items-center justify-between">
        <h1 className="text-lg font-semibold">AI Models</h1>
        <button
          onClick={openCloudDialog}
          className="flex items-center gap-2 px-3 py-2 rounded bg-blue-500/10 text-blue-400 border border-blue-500/30 hover:bg-blue-500/20 text-sm"
        >
          <Cloud size={16} />
          Cloud AI
        </button>
      </div>

      {notice && (
        <div className="p-2 rounded bg-green-500/10 border border-green-500/30 text-green-300 text-sm">
          {notice}
        </div>
      )}
      {error && (
        <div className="p-2 rounded bg-red-500/10 border border-red-500/30 text-red-300 text-sm">
          {error}
        </div>
      )}

      {/* Add/Edit Model Form */}
      <div className="border border-neutral-700 bg-[var(--panel-2)] p-4 rounded">
        <h2 className="text-md font-medium mb-3">
          {editingId ? 'Edit Model' : 'Add New Model'}
        </h2>
        <form onSubmit={handleSubmit} className="space-y-3">
          <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
            <div>
              <label className="block text-sm text-[var(--text-dim)] mb-1">
                Model Name <span className="text-red-400">*</span>
              </label>
              <input
                type="text"
                className="input w-full"
                placeholder="e.g., Person Detector 1"
                value={formData.name}
                onChange={(e) => setFormData({ ...formData, name: e.target.value })}
                required
                disabled={!canAdmin || loading}
              />
            </div>

            <div>
              <label className="block text-sm text-[var(--text-dim)] mb-1">
                AI Model <span className="text-red-400">*</span>
              </label>
              <select
                className="select w-full"
                value={formData.model_name}
                onChange={(e) => setFormData({ ...formData, model_name: e.target.value })}
                disabled={!canAdmin || loading}
              >
                <optgroup label="Local Models">
                  {AVAILABLE_MODELS.map((m) => (
                    <option key={m.value} value={m.value}>
                      {m.label}
                    </option>
                  ))}
                </optgroup>
                {cloudModels.length > 0 && (
                  <optgroup label="☁️ Cloud Models">
                    {cloudModels.filter(m => m.enabled).map((m) => (
                      <option key={`cloud-${m.id}`} value={`cloud:${m.id}`}>
                        ☁️ {m.name} ({m.task})
                      </option>
                    ))}
                  </optgroup>
                )}
              </select>
            </div>
          </div>

          <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
            <div>
              <label className="block text-sm text-[var(--text-dim)] mb-1">
                Task <span className="text-red-400">*</span>
              </label>
              <select
                className="select w-full"
                value={formData.task}
                onChange={(e) => setFormData({ ...formData, task: e.target.value })}
                disabled={!canAdmin || loading}
              >
                {AVAILABLE_TASKS.map((t) => (
                  <option key={t.value} value={t.value}>
                    {t.label}
                  </option>
                ))}
              </select>
            </div>

            <div className="flex items-end">
              <label className="flex items-center gap-2 text-sm">
                <input
                  type="checkbox"
                  className="accent-[var(--accent)]"
                  checked={formData.enabled}
                  onChange={(e) => setFormData({ ...formData, enabled: e.target.checked })}
                  disabled={!canAdmin || loading}
                />
                <span>Enabled</span>
              </label>
            </div>
          </div>

          <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
            <div>
              <label className="block text-sm text-[var(--text-dim)] mb-1">
                Source Type <span className="text-red-400">*</span>
              </label>
              <select
                className="select w-full"
                value={formData.source_type}
                onChange={(e) => setFormData({ 
                  ...formData, 
                  source_type: e.target.value,
                  assigned_camera_id: null,
                  recording_path: null
                })}
                disabled={!canAdmin || loading}
              >
                <option value="live">Live Camera Feed</option>
                <option value="recording">Recorded Video</option>
              </select>
            </div>

            <div className="flex items-end">
              <label className="flex items-center gap-2 text-sm">
                <input
                  type="checkbox"
                  className="accent-[var(--accent)]"
                  checked={formData.enabled}
                  onChange={(e) => setFormData({ ...formData, enabled: e.target.checked })}
                  disabled={!canAdmin || loading}
                />
                <span>Enabled</span>
              </label>
            </div>
          </div>

          {/* Live Camera Selection */}
          {formData.source_type === 'live' && (
            <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
              <div>
                <label className="block text-sm text-[var(--text-dim)] mb-1">
                  Assigned Camera <span className="text-red-400">*</span>
                </label>
                <select
                  className="select w-full"
                  value={formData.assigned_camera_id || ''}
                  onChange={(e) =>
                    setFormData({
                      ...formData,
                      assigned_camera_id: e.target.value ? Number(e.target.value) : null,
                    })
                  }
                  disabled={!canAdmin || loading}
                >
                  <option value="">Select a camera...</option>
                {cameras.map((cam) => (
                  <option key={cam.id} value={cam.id}>
                    {cam.name} {cam.source_url ? '' : '(No RTSP URL)'}
                  </option>
                ))}
              </select>
              <div className="text-xs text-[var(--text-dim)] mt-1">
                Select a camera to enable automatic inference
              </div>
            </div>

            <div>
              <label className="block text-sm text-[var(--text-dim)] mb-1">
                Inference Interval (seconds)
              </label>
              <input
                type="number"
                className="input w-full"
                min="1"
                max="60"
                value={formData.inference_interval}
                onChange={(e) =>
                  setFormData({ ...formData, inference_interval: Number(e.target.value) })
                }
                disabled={!canAdmin || loading}
              />
              <div className="text-xs text-[var(--text-dim)] mt-1">
                How often to run inference (1-60 seconds)
              </div>
            </div>
          </div>
          )}

          {/* Recording Selection */}
          {formData.source_type === 'recording' && (
            <div>
              <label className="block text-sm text-[var(--text-dim)] mb-1">
                Recording File <span className="text-red-400">*</span>
              </label>
              <div className="flex gap-2">
                <input
                  type="text"
                  className="input w-full"
                  placeholder="Select a recording file..."
                  value={formData.recording_path || ''}
                  readOnly
                  disabled={!canAdmin || loading}
                />
                <button
                  type="button"
                  className="btn btn-secondary"
                  onClick={() => {
                    setShowRecordingBrowser(true)
                  }}
                  disabled={!canAdmin || loading}
                >
                  Browse
                </button>
              </div>
              {formData.recording_path && (
                <div className="text-xs text-green-400 mt-1">
                  ✓ Recording selected: {formData.recording_path}
                </div>
              )}
            </div>
          )}

          <div>
            <label className="block text-sm text-[var(--text-dim)] mb-1">
              Additional Config (JSON)
            </label>
            <textarea
              className="textarea w-full h-20"
              placeholder='{"confidence": 0.5}'
              value={formData.config}
              onChange={(e) => setFormData({ ...formData, config: e.target.value })}
              disabled={!canAdmin || loading}
            />
            <div className="text-xs text-[var(--text-dim)] mt-1">
              Optional: JSON object with additional parameters
            </div>
          </div>

          <div className="flex gap-2">
            <button
              type="submit"
              className="px-4 py-2 bg-[var(--accent)] text-white rounded disabled:opacity-50"
              disabled={!canAdmin || loading || !formData.name}
            >
              {editingId ? 'Update Model' : 'Add Model'}
            </button>
            {editingId && (
              <button
                type="button"
                onClick={handleCancelEdit}
                className="px-4 py-2 bg-[var(--panel)] border border-neutral-700 rounded"
              >
                Cancel
              </button>
            )}
          </div>
        </form>
      </div>

      {/* Models Table */}
      <div className="border border-neutral-700 bg-[var(--panel-2)] rounded overflow-hidden">
        <div className="p-3 border-b border-neutral-700">
          <h2 className="text-md font-medium">Configured Models ({models.length})</h2>
        </div>

        {loading && models.length === 0 ? (
          <div className="p-4 text-center text-sm text-[var(--text-dim)]">Loading...</div>
        ) : models.length === 0 ? (
          <div className="p-4 text-center text-sm text-[var(--text-dim)]">
            No models configured yet. Add one using the form above.
          </div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead className="bg-[var(--panel)] text-[var(--text-dim)]">
                <tr>
                  <th className="text-left p-3">Name</th>
                  <th className="text-left p-3">Model</th>
                  <th className="text-left p-3">Task</th>
                  <th className="text-left p-3">Source</th>
                  <th className="text-center p-3">Interval</th>
                  <th className="text-center p-3">Status</th>
                  <th className="text-center p-3">Inference</th>
                  <th className="text-center p-3">Actions</th>
                </tr>
              </thead>
              <tbody>
                {models.map((model) => (
                  <tr
                    key={model.id}
                    className="border-t border-neutral-700 hover:bg-[var(--panel)]/50"
                  >
                    <td className="p-3 font-medium">{model.name}</td>
                    <td className="p-3">
                      <span className="text-xs bg-blue-500/20 text-blue-300 px-2 py-1 rounded">
                        {model.model_name}
                      </span>
                    </td>
                    <td className="p-3 text-[var(--text-dim)]">{model.task}</td>
                    <td className="p-3">
                      {model.source_type === 'live' ? (
                        model.assigned_camera_id ? (
                          <div className="text-xs">
                            <div className="text-[var(--accent)] font-medium">📹 Live</div>
                            <div className="text-[var(--text-dim)]">
                              {cameras.find((c) => c.id === model.assigned_camera_id)?.name ||
                                `Camera ${model.assigned_camera_id}`}
                            </div>
                          </div>
                        ) : (
                          <span className="text-xs text-neutral-500">No camera</span>
                        )
                      ) : (
                        <div className="text-xs">
                          <div className="text-purple-400 font-medium">🎬 Recording</div>
                          <div className="text-[var(--text-dim)] truncate max-w-[150px]" title={model.recording_path || ''}>
                            {model.recording_path ? model.recording_path.split('/').pop() : 'Not selected'}
                          </div>
                        </div>
                      )}
                    </td>
                    <td className="p-3 text-center text-xs text-[var(--text-dim)]">
                      {model.source_type === 'live' && model.inference_interval ? `${model.inference_interval}s` : '-'}
                    </td>
                    <td className="p-3 text-center">
                      <button
                        onClick={() => toggleEnabled(model.id, model.enabled)}
                        disabled={!canAdmin}
                        className={`px-2 py-1 rounded text-xs font-medium ${
                          model.enabled
                            ? 'bg-green-500/20 text-green-300'
                            : 'bg-red-500/20 text-red-300'
                        } disabled:opacity-50`}
                      >
                        {model.enabled ? 'Enabled' : 'Disabled'}
                      </button>
                    </td>
                    <td className="p-3 text-center">
                      {model.source_type === 'live' ? (
                        // Live camera inference control
                        model.assigned_camera_id && model.enabled ? (
                          <>
                            <button
                              onClick={() => toggleInference(model)}
                              disabled={inferenceLoading.has(model.id)}
                              className={`px-3 py-1 rounded text-xs font-medium disabled:opacity-50 ${
                                runningModels.has(model.id)
                                  ? 'bg-red-600 hover:bg-red-700 text-white'
                                  : 'bg-green-600 hover:bg-green-700 text-white'
                              }`}
                            >
                              {inferenceLoading.has(model.id) ? (
                                '⏳ Loading...'
                              ) : runningModels.has(model.id) ? (
                                '⏹ Stop'
                              ) : (
                                '▶ Start'
                              )}
                            </button>
                            {runningModels.has(model.id) && !inferenceLoading.has(model.id) && (
                              <div className="text-[10px] text-green-400 mt-1">● Running</div>
                            )}
                          </>
                        ) : (
                          <span className="text-xs text-neutral-500">
                            {!model.enabled ? 'Disabled' : 'No camera'}
                          </span>
                        )
                      ) : (
                        // Recording inference - shows running status like live
                        model.recording_path && model.enabled ? (
                          <>
                            <button
                              onClick={() => runningModels.has(model.id) ? stopInference(model.id) : analyzeRecording(model)}
                              disabled={!canAdmin || inferenceLoading.has(model.id)}
                              className={`px-3 py-1 rounded text-xs font-medium disabled:opacity-50 ${
                                runningModels.has(model.id)
                                  ? 'bg-red-600 hover:bg-red-700 text-white'
                                  : 'bg-purple-600 hover:bg-purple-700 text-white'
                              }`}
                            >
                              {inferenceLoading.has(model.id) ? (
                                '⏳ Loading...'
                              ) : runningModels.has(model.id) ? (
                                '⏹ Stop'
                              ) : (
                                '🎬 Analyze'
                              )}
                            </button>
                            {runningModels.has(model.id) && !inferenceLoading.has(model.id) && (
                              <div className="text-[10px] text-purple-400 mt-1">● Processing</div>
                            )}
                          </>
                        ) : (
                          <span className="text-xs text-neutral-500">
                            {!model.enabled ? 'Disabled' : 'No recording'}
                          </span>
                        )
                      )}
                    </td>
                    <td className="p-3 text-center">
                      <div className="flex items-center justify-center gap-2">
                        <button
                          onClick={() => handleEdit(model)}
                          disabled={!canAdmin}
                          className="px-2 py-1 bg-blue-600/20 border border-blue-600/50 rounded text-blue-300 hover:bg-blue-600/30 disabled:opacity-50 text-xs"
                        >
                          Edit
                        </button>
                        <button
                          onClick={() => handleDelete(model.id, model.name)}
                          disabled={!canAdmin}
                          className="px-2 py-1 bg-red-600/20 border border-red-600/50 rounded text-red-300 hover:bg-red-600/30 disabled:opacity-50 text-xs"
                        >
                          Delete
                        </button>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {/* Recording Browser Modal */}
      {showRecordingBrowser && (
        <RecordingBrowser
          cameras={cameras}
          onSelect={(data) => {
            console.log('RecordingBrowser onSelect data:', data);
            setSelectedRecording(data);
            // Ensure we have segments with valid paths
            if (data.segments && data.segments.length > 0) {
              // Filter out empty or invalid paths
              const validSegments = data.segments.filter(seg => seg && seg.trim() !== '');
              
              if (validSegments.length > 0) {
                console.log('Setting recording_path to:', validSegments[0]);
                setFormData(prev => ({
                  ...prev,
                  assigned_camera_id: data.camera_id,
                  recording_path: validSegments[0] // Store first valid segment as primary path
                }));
                setShowRecordingBrowser(false);
              } else {
                console.error('No valid segment paths in selected recording:', data);
                setError('Selected recording has no valid file paths. The recording may be incomplete or corrupted. Please ensure the recording has finished and try another one.');
              }
            } else {
              console.error('No segments in selected recording:', data);
              setError('Selected recording has no segments. Please choose another recording.');
            }
          }}
          onClose={() => setShowRecordingBrowser(false)}
        />
      )}

      {/* Cloud AI Dialog */}
      {showCloudDialog && (
        <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50 p-4">
          <div className="bg-[var(--panel)] border border-neutral-700 rounded-lg max-w-4xl w-full max-h-[90vh] overflow-hidden flex flex-col">
            <div className="p-4 border-b border-neutral-700 flex justify-between items-center">
              <h3 className="text-lg font-medium">Cloud AI Providers</h3>
              <button
                onClick={() => setShowCloudDialog(false)}
                className="text-[var(--text-dim)] hover:text-[var(--text)]"
              >
                ✕
              </button>
            </div>

            {/* Tabs */}
            <div className="flex gap-2 px-4 pt-4 border-b border-neutral-700">
              <button
                onClick={() => setCloudTab('credentials')}
                className={`px-4 py-2 text-sm font-medium border-b-2 transition-colors ${
                  cloudTab === 'credentials'
                    ? 'border-[var(--accent)] text-[var(--accent)]'
                    : 'border-transparent text-[var(--text-dim)] hover:text-[var(--text)]'
                }`}
              >
                Credentials
              </button>
              <button
                onClick={() => setCloudTab('models')}
                className={`px-4 py-2 text-sm font-medium border-b-2 transition-colors ${
                  cloudTab === 'models'
                    ? 'border-[var(--accent)] text-[var(--accent)]'
                    : 'border-transparent text-[var(--text-dim)] hover:text-[var(--text)]'
                }`}
              >
                Models
              </button>
            </div>

            <div className="flex-1 overflow-y-auto p-4">
              {/* Credentials Tab */}
              {cloudTab === 'credentials' && (
                <div className="space-y-4">
                  <div className="flex justify-between items-center">
                    <p className="text-sm text-[var(--text-dim)]">
                      Manage API credentials for cloud AI providers. Tokens are encrypted at rest.
                    </p>
                    <button
                      onClick={() => setShowAddCredential(true)}
                      className="px-3 py-1.5 bg-[var(--accent)] text-white rounded text-sm hover:opacity-90"
                    >
                      Add Credential
                    </button>
                  </div>

                  {showAddCredential && (
                    <div className="border border-neutral-700 bg-[var(--panel-2)] p-4 rounded space-y-3">
                      <h4 className="font-medium text-sm">New Credential</h4>
                      <div className="space-y-3">
                        <div>
                          <label className="block text-xs text-[var(--text-dim)] mb-1">Provider</label>
                          <select
                            className="select w-full"
                            value={newCredential.provider}
                            onChange={(e) => setNewCredential({ ...newCredential, provider: e.target.value })}
                          >
                            <option value="huggingface">Hugging Face</option>
                          </select>
                        </div>
                        <div>
                          <label className="block text-xs text-[var(--text-dim)] mb-1">API Token</label>
                          <input
                            type="password"
                            className="input w-full"
                            placeholder="hf_xxxxxxxxxxxxxxxxxxxx"
                            value={newCredential.token}
                            onChange={(e) => setNewCredential({ ...newCredential, token: e.target.value })}
                          />
                        </div>
                        <div>
                          <label className="block text-xs text-[var(--text-dim)] mb-1">
                            Account Info (JSON, optional)
                          </label>
                          <textarea
                            className="textarea w-full h-16"
                            placeholder='{"email": "user@example.com"}'
                            value={newCredential.account_info}
                            onChange={(e) => setNewCredential({ ...newCredential, account_info: e.target.value })}
                          />
                        </div>
                        <div className="flex gap-2">
                          <button
                            onClick={addCloudCredential}
                            disabled={cloudLoading}
                            className="px-3 py-1.5 bg-[var(--accent)] text-white rounded text-sm hover:opacity-90 disabled:opacity-50"
                          >
                            {cloudLoading ? 'Adding...' : 'Add'}
                          </button>
                          <button
                            onClick={() => setShowAddCredential(false)}
                            className="px-3 py-1.5 border border-neutral-700 rounded text-sm hover:bg-[var(--panel-2)]"
                          >
                            Cancel
                          </button>
                        </div>
                      </div>
                    </div>
                  )}

                  <div className="space-y-2">
                    {cloudCredentials.length === 0 && !cloudLoading && (
                      <div className="text-center py-8 text-sm text-[var(--text-dim)] border border-neutral-700 rounded">
                        No credentials configured. Add your first credential to get started.
                      </div>
                    )}
                    {cloudCredentials.map((cred) => (
                      <div key={cred.id} className="border border-neutral-700 bg-[var(--panel-2)] p-3 rounded flex justify-between items-center">
                        <div>
                          <div className="font-medium text-sm capitalize">{cred.provider}</div>
                          <div className="text-xs text-[var(--text-dim)]">
                            ID: {cred.id.slice(0, 8)}... • Created: {new Date(cred.created_at).toLocaleDateString()}
                          </div>
                          {cred.account_info && (
                            <div className="text-xs text-[var(--text-dim)] mt-1">
                              {JSON.stringify(cred.account_info)}
                            </div>
                          )}
                        </div>
                        <button
                          onClick={() => deleteCloudCredential(cred.id)}
                          className="px-3 py-1 bg-red-500/10 text-red-400 border border-red-500/30 rounded text-xs hover:bg-red-500/20"
                        >
                          Delete
                        </button>
                      </div>
                    ))}
                  </div>
                </div>
              )}

              {/* Models Tab */}
              {cloudTab === 'models' && (
                <div className="space-y-4">
                  <div className="flex justify-between items-center">
                    <p className="text-sm text-[var(--text-dim)]">
                      Configure cloud AI models linked to your credentials.
                    </p>
                    <button
                      onClick={() => setShowAddCloudModel(true)}
                      className="px-3 py-1.5 bg-[var(--accent)] text-white rounded text-sm hover:opacity-90"
                      disabled={cloudCredentials.length === 0}
                    >
                      Add Model
                    </button>
                  </div>

                  {showAddCloudModel && (
                    <div className="border border-neutral-700 bg-[var(--panel-2)] p-4 rounded space-y-3">
                      <h4 className="font-medium text-sm">Configure Cloud Model</h4>
                      <div className="grid grid-cols-2 gap-3">
                        <div>
                          <label className="block text-xs text-[var(--text-dim)] mb-1">Credential</label>
                          <select
                            className="select w-full"
                            value={newCloudModel.credential_id}
                            onChange={(e) => setNewCloudModel({ ...newCloudModel, credential_id: e.target.value })}
                          >
                            <option value="">Select credential...</option>
                            {cloudCredentials.map((c) => (
                              <option key={c.id} value={c.id}>
                                {c.provider} - {c.id.slice(0, 8)}...
                              </option>
                            ))}
                          </select>
                        </div>
                        <div>
                          <label className="block text-xs text-[var(--text-dim)] mb-1">Model Name</label>
                          <input
                            className="input w-full"
                            placeholder="My BLIP Model"
                            value={newCloudModel.name}
                            onChange={(e) => setNewCloudModel({ ...newCloudModel, name: e.target.value })}
                          />
                        </div>
                        <div>
                          <label className="block text-xs text-[var(--text-dim)] mb-1">Model ID</label>
                          <input
                            className="input w-full"
                            placeholder="Salesforce/blip-image-captioning-base"
                            value={newCloudModel.model_id}
                            onChange={(e) => setNewCloudModel({ ...newCloudModel, model_id: e.target.value })}
                          />
                        </div>
                        <div>
                          <label className="block text-xs text-[var(--text-dim)] mb-1">Task</label>
                          <select
                            className="select w-full"
                            value={newCloudModel.task}
                            onChange={(e) => setNewCloudModel({ ...newCloudModel, task: e.target.value })}
                          >
                            <option value="image-to-text">Image to Text</option>
                            <option value="image-classification">Image Classification</option>
                            <option value="object-detection">Object Detection</option>
                            <option value="text-generation">Text Generation</option>
                          </select>
                        </div>
                        <div className="col-span-2">
                          <label className="block text-xs text-[var(--text-dim)] mb-1">
                            Config (JSON)
                          </label>
                          <textarea
                            className="textarea w-full h-16"
                            placeholder='{"num_beams": 5}'
                            value={newCloudModel.config}
                            onChange={(e) => setNewCloudModel({ ...newCloudModel, config: e.target.value })}
                          />
                        </div>
                        <div className="col-span-2">
                          <label className="flex items-center gap-2 text-xs text-[var(--text-dim)]">
                            <input
                              type="checkbox"
                              checked={newCloudModel.enabled}
                              onChange={(e) => setNewCloudModel({ ...newCloudModel, enabled: e.target.checked })}
                            />
                            Enabled
                          </label>
                        </div>
                      </div>
                      <div className="flex gap-2">
                        <button
                          onClick={addCloudModel}
                          disabled={cloudLoading}
                          className="px-3 py-1.5 bg-[var(--accent)] text-white rounded text-sm hover:opacity-90 disabled:opacity-50"
                        >
                          {cloudLoading ? 'Adding...' : 'Add'}
                        </button>
                        <button
                          onClick={() => setShowAddCloudModel(false)}
                          className="px-3 py-1.5 border border-neutral-700 rounded text-sm hover:bg-[var(--panel-2)]"
                        >
                          Cancel
                        </button>
                      </div>
                    </div>
                  )}

                  <div className="space-y-2">
                    {cloudModels.length === 0 && !cloudLoading && (
                      <div className="text-center py-8 text-sm text-[var(--text-dim)] border border-neutral-700 rounded">
                        {cloudCredentials.length === 0
                          ? 'Add credentials first, then configure your models.'
                          : 'No models configured. Add your first cloud model.'}
                      </div>
                    )}
                    {cloudModels.map((model) => (
                      <div key={model.id} className="border border-neutral-700 bg-[var(--panel-2)] p-3 rounded">
                        <div className="flex justify-between items-start">
                          <div className="flex-1">
                            <div className="font-medium text-sm">{model.name}</div>
                            <div className="text-xs text-[var(--text-dim)] mt-1">
                              Model: {model.model_id}
                            </div>
                            <div className="text-xs text-[var(--text-dim)]">
                              Task: {model.task} • Provider: {model.provider}
                            </div>
                          </div>
                          <button
                            onClick={() => deleteCloudModel(model.id)}
                            className="px-3 py-1 bg-red-500/10 text-red-400 border border-red-500/30 rounded text-xs hover:bg-red-500/20"
                          >
                            Delete
                          </button>
                        </div>
                      </div>
                    ))}
                  </div>
                </div>
              )}
            </div>
          </div>
        </div>
      )}
    </section>
  )
}
