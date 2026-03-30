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
  updateUser: (userId: number, payload: any) => api.put(`/api/v1/users/${userId}`, payload),
  createUser: (payload: any) => api.post('/api/v1/users/', payload),
  deleteUser: (userId: number) => api.delete(`/api/v1/users/${userId}`),
  
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
