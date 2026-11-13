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

export const userService = {
  // Users
  getUsers: (params: Record<string, any> = {}) => api.get('/api/v1/users/', { params }),
  getUser: (userId: number) => api.get(`/api/v1/users/${userId}`),
  // Changing is_superuser (or creating a superuser) requires current MFA.
  updateUser: (userId: number, payload: any, mfaCode?: string) =>
    api.put(`/api/v1/users/${userId}`, payload, mfaCode ? { headers: { 'X-MFA-Code': mfaCode } } : undefined),
  createUser: (payload: any, mfaCode?: string) =>
    api.post('/api/v1/users/', payload, mfaCode ? { headers: { 'X-MFA-Code': mfaCode } } : undefined),
  // Soft deletion is reversible. Permanent deletion and reactivation require current MFA.
  deleteUser: (userId: number) => api.delete(`/api/v1/users/${userId}`),
  permanentlyDeleteUser: (userId: number, mfaCode: string) => api.delete(`/api/v1/users/${userId}/permanent`, { headers: { 'X-MFA-Code': mfaCode } }),
  activateUser: (userId: number, mfaCode: string) => api.post(`/api/v1/users/${userId}/activate`, undefined, { headers: { 'X-MFA-Code': mfaCode } }),
  // Remove another user's second factor: they enrol a new authenticator at next sign-in.
  resetUserMfa: (userId: number, mfaCode: string) => api.post(`/api/v1/users/${userId}/mfa/reset`, undefined, { headers: { 'X-MFA-Code': mfaCode } }),
  userMfaFactors: (userId: number) => api.get(`/api/v1/users/${userId}/mfa/factors`),
  setUserMfaFactor: (userId: number, factorId: number, enabled: boolean, mfaCode: string) =>
    api.patch(`/api/v1/users/${userId}/mfa/factors/${factorId}`, { enabled }, { headers: { 'X-MFA-Code': mfaCode } }),
  removeUserMfaFactor: (userId: number, factorId: number, mfaCode: string) =>
    api.delete(`/api/v1/users/${userId}/mfa/factors/${factorId}`, { headers: { 'X-MFA-Code': mfaCode } }),
  
  // Roles
  getRoles: () => api.get('/api/v1/roles/'),
  createRole: (payload: { name: string; description?: string }) => api.post('/api/v1/roles/', payload),
  updateRole: (roleId: number, payload: { name?: string; description?: string }) => api.put(`/api/v1/roles/${roleId}`, payload),
  deleteRole: (roleId: number) => api.delete(`/api/v1/roles/${roleId}`),

  // Permissions
  getPermissions: () => api.get('/api/v1/permissions/'),
  createPermission: (payload: { name: string; description?: string }) => api.post('/api/v1/permissions/', payload),
  updatePermission: (permissionId: number, payload: { name?: string; description?: string }) => api.put(`/api/v1/permissions/${permissionId}`, payload),
  deletePermission: (permissionId: number) => api.delete(`/api/v1/permissions/${permissionId}`),
  getRolePermissions: (roleId: number) => api.get(`/api/v1/permissions/roles/${roleId}`),
  setRolePermissions: (roleId: number, permissionIds: number[]) => api.put(`/api/v1/permissions/roles/${roleId}`, { permission_ids: permissionIds }),

  // Self
  usersMe: () => api.get('/api/v1/users/me'),
  updateUsersMe: (payload: any) => api.put('/api/v1/users/me', payload),

  // Password Policy
  getPasswordPolicy: () => api.get('/api/v1/password-policy/'),
  updatePasswordPolicy: (payload: any) => api.put('/api/v1/password-policy/', payload),
}
