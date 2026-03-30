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

export const complianceService = {
  getComplianceSummary: () => api.get('/api/v1/compliance/summary'),
  getRecordingCoverage: (params?: any) =>
    api.get('/api/v1/compliance/recording-coverage', { params }),
  getAccessAudit: (params?: any) =>
    api.get('/api/v1/compliance/access-audit', { params }),
  exportComplianceReport: (days: number) =>
    api.get('/api/v1/compliance/export', {
      params: { days },
      responseType: 'blob',
    }),
}
