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

export const integrationService = {
  getIntegrations: () => api.get('/api/v1/integrations'),
  getIntegration: (id: number) => api.get(`/api/v1/integrations/${id}`),
  createIntegration: (data: any) => api.post('/api/v1/integrations', data),
  updateIntegration: (id: number, data: any) =>
    api.put(`/api/v1/integrations/${id}`, data),
  deleteIntegration: (id: number) =>
    api.delete(`/api/v1/integrations/${id}`),
  testIntegration: (id: number) =>
    api.post(`/api/v1/integrations/${id}/test`),
}
