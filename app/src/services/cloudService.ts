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

export const cloudService = {
  // Credentials
  getCloudCredentials: () => api.get('/api/v1/cloud-providers/credentials'),
  createCloudCredential: (data: any) =>
    api.post('/api/v1/cloud-providers/credentials', data),
  deleteCloudCredential: (id: string) =>
    api.delete(`/api/v1/cloud-providers/credentials/${id}`),

  // Models
  getCloudModels: () => api.get('/api/v1/cloud-providers/models'),
  createCloudModel: (data: any) =>
    api.post('/api/v1/cloud-providers/models', data),
  deleteCloudModel: (id: number) =>
    api.delete(`/api/v1/cloud-providers/models/${id}`),
  getCloudModel: (id: number) =>
    api.get(`/api/v1/cloud-providers/models/${id}`),

  // Quotas
  getQuota: (provider: string) =>
    api.get(`/api/v1/cloud-providers/quotas/${provider}`),
  updateQuota: (provider: string, data: any) =>
    api.patch(`/api/v1/cloud-providers/quotas/${provider}`, data),
}
