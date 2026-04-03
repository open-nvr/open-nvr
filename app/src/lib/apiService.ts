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

// This file serves as an aggregator for backward compatibility.
// New code should import directly from @/services/*

import { authService } from '../services/authService'
import { cameraService } from '../services/cameraService'
import { mediaMtxService } from '../services/mediaMtxService'
import { recordingService } from '../services/recordingService'
import { systemService } from '../services/systemService'
import { userService } from '../services/userService'
import { aiService } from '../services/aiService'
import { cloudService } from '../services/cloudService'
import { cloudStreamingService } from '../services/cloudStreamingService'
import { integrationService } from '../services/integrationService'
import { mediaSourceService } from '../services/mediaSourceService'
import { complianceService } from '../services/complianceService'

export const apiService = {
  ...authService,
  ...cameraService,
  ...mediaMtxService,
  ...recordingService,
  ...systemService,
  ...userService,
  ...aiService,
  ...cloudService,
  ...cloudStreamingService,
  ...integrationService,
  ...mediaSourceService,
  ...complianceService,
}

