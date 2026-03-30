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

export const mediaSourceService = {
  getMediaSourceSettings: () => api.get('/api/v1/media-source/settings'),
  updateMediaSourceSettings: (data: any) =>
    api.put('/api/v1/media-source/settings', data),
  uploadMediaSourceSettings: (files: FormData | { cert_file?: File; key_file?: File; ca_bundle_file?: File }) => {
    if (files instanceof FormData) {
      return api.post('/api/v1/media-source/settings/upload', files)
    }
    const form = new FormData()
    if (files.cert_file) form.append('cert_file', files.cert_file)
    if (files.key_file) form.append('key_file', files.key_file)
    if (files.ca_bundle_file) form.append('ca_bundle_file', files.ca_bundle_file)
    return api.post('/api/v1/media-source/settings/upload', form)
  },
}
