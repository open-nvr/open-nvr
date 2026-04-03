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

import { apiService } from './apiService'

let lastOkAt = 0
let lastCheckAt = 0
let lastStatus: 'ok' | 'down' = 'down'

export async function isMediaMtxHealthy(maxCacheMs: number = 15000): Promise<boolean> {
  const now = Date.now()
  if (now - lastCheckAt < maxCacheMs) {
    return lastStatus === 'ok'
  }
  try {
    lastCheckAt = now
    const { data } = await apiService.mtxHealth()
    const healthy = data?.status === 'ok' || data === 'ok' || data?.healthy === true
    if (healthy) {
      lastOkAt = now
      lastStatus = 'ok'
      return true
    }
    lastStatus = 'down'
    return false
  } catch {
    lastStatus = 'down'
    return false
  }
}

export function lastMediaMtxOkAgo(): number {
  return lastOkAt ? Date.now() - lastOkAt : Infinity
}


