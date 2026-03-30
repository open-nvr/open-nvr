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

export const systemService = {
  // Health & Stats
  getHealth: () => api.get('/health'),
  getDashboardStats: () => api.get('/api/v1/dashboard/stats'),
  getAlerts: (params: Record<string, any> = {}) => api.get('/api/v1/alerts/', { params }),
  
  // Power
  systemShutdown: () => api.post('/api/v1/system/shutdown', ''),
  systemReboot: () => api.post('/api/v1/system/reboot', ''),

  // Suricata
  getSuricataEveLogs: (params: { limit?: number; skip?: number; only_alerts?: boolean } = {}) =>
    api.get('/api/v1/suricata/logs/eve', { params }),
  getSuricataFastLogs: (params: { limit?: number; skip?: number } = {}) =>
    api.get('/api/v1/suricata/logs/fast', { params }),
  getSuricataStats: (params: { start?: string; end?: string; limit?: number } = {}) =>
    api.get('/api/v1/suricata/stats', { params }),

  // Audit
  getAuditLogs: (params: Record<string, any> = {}) => api.get('/api/v1/audit-logs/', { params }),

  // Firewall
  listFirewallRules: () => api.get('/api/v1/security/firewall/rules'),
  createFirewallRule: (payload: any) => api.post('/api/v1/security/firewall/rules', payload),
  updateFirewallRule: (ruleId: number, payload: any) => api.put(`/api/v1/security/firewall/rules/${ruleId}`, payload),
  deleteFirewallRule: (ruleId: number) => api.delete(`/api/v1/security/firewall/rules/${ruleId}`),
  getSecuritySetting: (key: string) => api.get(`/api/v1/security/settings/${key}`),
  setSecuritySetting: (key: string, value: any) => api.put(`/api/v1/security/settings/${key}`, { key, value }),

  // Cloud & Firmware
  getCloudSettings: () => api.get('/api/v1/cloud/settings'),
  updateCloudSettings: (payload: any) => api.put('/api/v1/cloud/settings', payload),
  testCloudConnectivity: () => api.post('/api/v1/cloud/test', ''),
  getSystemInfo: () => api.get('/api/v1/firmware/system-info'),
  getUpdateStatus: () => api.get('/api/v1/firmware/update-status'),
  getAutoUpdateSettings: () => api.get('/api/v1/firmware/auto-update'),
  updateAutoUpdateSettings: (payload: any) => api.put('/api/v1/firmware/auto-update', payload),
  checkUpdatesManual: () => api.post('/api/v1/firmware/check-updates', ''),
  applyUpdates: () => api.post('/api/v1/firmware/apply-updates', ''),

  // WebRTC
  getWebRTCSettings: () => api.get('/api/v1/webrtc/settings'),
  updateWebRTCSettings: (payload: any) => api.put('/api/v1/webrtc/settings', payload),
  getWebRTCClientConfig: () => api.get('/api/v1/webrtc/rtc-config'),

  // Integrations
  getIntegrations: () => api.get('/api/v1/integrations/'),
  createIntegration: (payload: any) => api.post('/api/v1/integrations/', payload),
  updateIntegration: (id: number, payload: any) => api.put(`/api/v1/integrations/${id}`, payload),
  deleteIntegration: (id: number) => api.delete(`/api/v1/integrations/${id}`, {}),
  testIntegration: (id: number) => api.post(`/api/v1/integrations/test/${id}`, {}),

  // General Settings
  getGeneralSystem: () => api.get('/api/v1/general/system'),
  updateGeneralSystem: (payload: any) => api.put('/api/v1/general/system', payload),
  getGeneralNetwork: () => api.get('/api/v1/general/network'),
  updateGeneralNetwork: (payload: any) => api.put('/api/v1/general/network', payload),
  getCameraLAN: () => api.get('/api/v1/network/camera-lan'),
  updateCameraLAN: (payload: any) => api.put('/api/v1/network/camera-lan', payload),
  isolateCameraLAN: () => api.post('/api/v1/network/camera-lan/isolate', ''),
  getUplink: () => api.get('/api/v1/network/uplink'),
  updateUplink: (payload: any) => api.put('/api/v1/network/uplink', payload),
  getGeneralAlarm: () => api.get('/api/v1/general/alarm'),
  updateGeneralAlarm: (payload: any) => api.put('/api/v1/general/alarm', payload),
  getGeneralRs232: () => api.get('/api/v1/general/rs232'),
  updateGeneralRs232: (payload: any) => api.put('/api/v1/general/rs232', payload),
  getGeneralLiveView: () => api.get('/api/v1/general/live-view'),
  updateGeneralLiveView: (payload: any) => api.put('/api/v1/general/live-view', payload),
  getGeneralExceptions: () => api.get('/api/v1/general/exceptions'),
  updateGeneralExceptions: (payload: any) => api.put('/api/v1/general/exceptions', payload),
  getGeneralUser: () => api.get('/api/v1/general/user'),
  updateGeneralUser: (payload: any) => api.put('/api/v1/general/user', payload),
  getGeneralPos: () => api.get('/api/v1/general/pos'),
  updateGeneralPos: (payload: any) => api.put('/api/v1/general/pos', payload),
  getWindowSettings: () => api.get('/api/v1/general/window-settings'),
  updateWindowSettings: (payload: any) => api.put('/api/v1/general/window-settings', payload),

  // AI
  checkKaiCHealth: () => api.get('/api/v1/ai-models/health'),
  getCapabilities: () => api.get('/api/v1/ai-models/capabilities'),
}
