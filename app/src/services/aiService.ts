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
  runRecordingInference: (data: any) =>
    api.post('/api/v1/ai-models/inference/recording', data),

  // AI Detection Results
  getDetectionResults: (params?: any) =>
    api.get('/api/v1/ai-detection-results', { params }),
  deleteDetectionResult: (id: number) =>
    api.delete(`/api/v1/ai-detection-results/${id}`),
  deleteOldDetectionResults: (days: number) =>
    api.delete(`/api/v1/ai-detection-results/bulk/older-than/${days}`),
}
