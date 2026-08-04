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

import { api } from '../lib/api'

export const aiService = {
  // Model Management (CRUD)
  getAIModels: (params?: any) => api.get('/api/v1/ai-model-management', { params }),
  getAIModel: (id: number) => api.get(`/api/v1/ai-model-management/${id}`),
  createAIModel: (data: any) =>
    api.post('/api/v1/ai-model-management', data),
  updateAIModel: (id: number, data: any) =>
    api.put(`/api/v1/ai-model-management/${id}`, data),
  deleteAIModel: (id: number) =>
    api.delete(`/api/v1/ai-model-management/${id}`),

  // Background Inference Management
  startModelInference: (id: number) =>
    api.post(`/api/v1/ai-model-management/${id}/start-inference`),
  stopModelInference: (id: number) =>
    api.post(`/api/v1/ai-model-management/${id}/stop-inference`),
  getInferenceStatus: (id: number) =>
    api.get(`/api/v1/ai-model-management/${id}/inference-status`),
  getRunningInference: () =>
    api.get('/api/v1/ai-model-management/inference/running'),

  // Inference & Health
  checkKAIHealth: () => api.get('/api/v1/ai-models/health'),
  getCapabilities: () => api.get('/api/v1/ai-models/capabilities'),
  getTaskSchema: (task?: string) =>
    api.get('/api/v1/ai-models/schema', { params: { task } }),
  runInference: (data: any) => api.post('/api/v1/ai-models/inference', data),
  getFleetMetrics: () =>
    api.get('/api/v1/ai-models/adapters-metrics'),
  getAdapterMetrics: (name: string) =>
    api.get(`/api/v1/ai-models/adapters/${encodeURIComponent(name)}/metrics`),
  getTier0Metrics: () =>
    api.get('/api/v1/ai-models/tier0-metrics'),
  getTier0Gate: () => api.get('/api/v1/ai-models/tier0-gate'),
  setTier0Gate: (mode: 'off' | 'shadow' | 'enforce') =>
    api.put('/api/v1/ai-models/tier0-gate', { mode }),

  // Adapter permission approval (AI Adapter Contract v1 governance).
  getAdapterPermissions: (name: string) =>
    api.get(`/api/v1/ai-models/adapters/${encodeURIComponent(name)}/permissions`),
  grantAdapterPermissions: (name: string, keys: string[]) =>
    api.post(`/api/v1/ai-models/adapters/${encodeURIComponent(name)}/permissions/grant`, { keys }),
  revokeAdapterPermissions: (name: string, keys: string[]) =>
    api.post(`/api/v1/ai-models/adapters/${encodeURIComponent(name)}/permissions/revoke`, { keys }),
  approveAllAdapterPermissions: (name: string) =>
    api.post(`/api/v1/ai-models/adapters/${encodeURIComponent(name)}/permissions/approve-all`),
  runRecordingInference: (data: any) =>
    api.post('/api/v1/ai-models/inference/recording', data),

  // AI Detection Results
  getDetectionResults: (params?: any) =>
    api.get('/api/v1/ai-detection-results', { params }),
  deleteDetectionResult: (id: number) =>
    api.delete(`/api/v1/ai-detection-results/${id}`),
  deleteOldDetectionResults: (days: number) =>
    api.delete(`/api/v1/ai-detection-results/bulk/older-than/${days}`),

  // Live events WebSocket: mint a short-lived, single-use ticket so the
  // long-lived JWT never has to ride in the ws URL (which leaks into logs).
  createEventsWsTicket: () =>
    api.post('/api/v1/events/ws-ticket'),
}
