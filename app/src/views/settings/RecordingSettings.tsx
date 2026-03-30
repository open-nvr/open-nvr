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

import { useState, useEffect } from 'react'
import { apiService } from '../../lib/apiService'
import { Save, FolderOpen, Clock, Shield, HardDrive } from 'lucide-react'
import { useSnackbar } from '../../components/Snackbar'

export function RecordingSettings() {
    const { showError, showSuccess } = useSnackbar()
    const [loading, setLoading] = useState(false)
    const [path, setPath] = useState('')
    
    // Retention settings
    const [retentionDays, setRetentionDays] = useState(30)
    const [protectFlagged, setProtectFlagged] = useState(true)
    const [minFreeSpace, setMinFreeSpace] = useState<number | ''>('')

    useEffect(() => {
        loadSettings()
    }, [])

    const loadSettings = async () => {
        try {
            setLoading(true)
            const [storageRes, retentionRes] = await Promise.all([
                apiService.getRecordingStorage(),
                apiService.getRecordingRetention()
            ])
            setPath(storageRes.data.root_path || storageRes.data.recordings_base_path || '')
            
            // Load retention settings
            const retention = retentionRes.data
            setRetentionDays(retention.retention_days ?? 30)
            setProtectFlagged(retention.protect_flagged ?? true)
            setMinFreeSpace(retention.min_free_space_gb ?? '')
        } catch (e: any) {
            showError(e?.message || 'Failed to load recording settings')
        } finally {
            setLoading(false)
        }
    }

    const handleSaveStorage = async (e: React.FormEvent) => {
        e.preventDefault()
        try {
            setLoading(true)
            await apiService.updateRecordingStorage({ recordings_base_path: path })
            showSuccess('Storage settings saved successfully')
        } catch (e: any) {
            showError(e?.message || 'Failed to save storage settings')
        } finally {
            setLoading(false)
        }
    }

    const handleSaveRetention = async (e: React.FormEvent) => {
        e.preventDefault()
        try {
            setLoading(true)
            const payload = {
                retention_days: retentionDays,
                protect_flagged: protectFlagged,
                min_free_space_gb: minFreeSpace === '' ? null : minFreeSpace
            }
            await apiService.updateRecordingRetention(payload)
            showSuccess('Retention settings saved successfully')
        } catch (e: any) {
            showError(e?.message || 'Failed to save retention settings')
        } finally {
            setLoading(false)
        }
    }

    return (
        <div className="p-6 max-w-4xl">
            <h2 className="text-xl font-semibold mb-6 flex items-center gap-2">
                <FolderOpen className="text-[var(--accent)]" />
                Recording Settings
            </h2>

            {/* Storage Settings */}
            <form onSubmit={handleSaveStorage} className="space-y-6 mb-8">
                <div className="bg-[var(--panel)] border border-neutral-800 rounded-lg p-6">
                    <h3 className="text-lg font-medium mb-4 flex items-center gap-2">
                        <HardDrive size={20} />
                        Storage Location
                    </h3>
                    
                    <p className="text-sm text-[var(--text-dim)] mb-6">
                        Configure where camera recordings will be stored. If not specified, a default path is auto-detected:
                        {' '}<strong>Docker: /app/recordings</strong> | <strong>Local: ./recordings</strong>
                        <br />
                        Changing this will affect new recordings. Existing recordings will remain in their original location.
                    </p>

                    <div className="space-y-4">
                        <div>
                            <label className="block text-sm font-medium mb-1">
                                Recording Path <span className="text-neutral-500 text-xs">(optional, uses auto-detected default if empty)</span>
                            </label>
                            <input
                                type="text"
                                value={path}
                                onChange={(e) => setPath(e.target.value)}
                                className="w-full px-3 py-2 bg-[var(--bg-2)] border border-neutral-700 rounded focus:border-[var(--accent)] focus:outline-none"
                                placeholder="Docker: /app/recordings  |  Local: ./recordings or D:/Recordings"
                            />
                            <p className="text-xs text-[var(--text-dim)] mt-1">
                                <strong>Docker:</strong> Use container path (e.g., /app/recordings). Ensure it matches your docker-compose.yml volume mount.
                                <br />
                                <strong>Local:</strong> Use relative (./recordings) or absolute path (D:/Recordings). Directory will be auto-created.
                            </p>
                        </div>
                    </div>
                </div>

                <div className="flex justify-end">
                    <button
                        type="submit"
                        disabled={loading}
                        className="px-4 py-2 bg-[var(--accent)] text-white rounded hover:bg-[var(--accent)]/90 disabled:opacity-50 flex items-center gap-2"
                    >
                        <Save size={16} />
                        {loading ? 'Saving...' : 'Save Storage Settings'}
                    </button>
                </div>
            </form>

            {/* Retention Settings */}
            <form onSubmit={handleSaveRetention} className="space-y-6">
                <div className="bg-[var(--panel)] border border-neutral-800 rounded-lg p-6">
                    <h3 className="text-lg font-medium mb-4 flex items-center gap-2">
                        <Clock size={20} />
                        Retention Policy
                    </h3>
                    <p className="text-sm text-[var(--text-dim)] mb-6">
                        Configure how long recordings are kept before automatic cleanup.
                    </p>

                    <div className="space-y-6">
                        {/* Retention Days */}
                        <div>
                            <label className="block text-sm font-medium mb-1">Retention Days</label>
                            <input
                                type="number"
                                min={0}
                                max={3650}
                                value={retentionDays}
                                onChange={(e) => setRetentionDays(parseInt(e.target.value) || 0)}
                                className="w-full px-3 py-2 bg-[var(--bg-2)] border border-neutral-700 rounded focus:border-[var(--accent)] focus:outline-none"
                                placeholder="30"
                            />
                            <p className="text-xs text-[var(--text-dim)] mt-1">
                                Recordings older than this will be deleted automatically. Set to 0 to keep all recordings.
                            </p>
                        </div>

                        {/* Protect Flagged */}
                        <div className="flex items-center gap-3">
                            <input
                                type="checkbox"
                                id="protect-flagged"
                                checked={protectFlagged}
                                onChange={(e) => setProtectFlagged(e.target.checked)}
                                className="w-4 h-4 text-[var(--accent)] bg-[var(--bg-2)] border-neutral-700 rounded focus:ring-[var(--accent)]"
                            />
                            <label htmlFor="protect-flagged" className="text-sm font-medium flex items-center gap-2">
                                <Shield size={16} className="text-[var(--accent)]" />
                                Protect Flagged Recordings
                            </label>
                        </div>
                        <p className="text-xs text-[var(--text-dim)] ml-7">
                            When enabled, flagged or bookmarked recordings will never be deleted automatically.
                        </p>

                        {/* Minimum Free Space */}
                        <div>
                            <label className="block text-sm font-medium mb-1">Minimum Free Space (GB)</label>
                            <input
                                type="number"
                                min={1}
                                max={100000}
                                value={minFreeSpace}
                                onChange={(e) => setMinFreeSpace(e.target.value === '' ? '' : parseInt(e.target.value))}
                                className="w-full px-3 py-2 bg-[var(--bg-2)] border border-neutral-700 rounded focus:border-[var(--accent)] focus:outline-none"
                                placeholder="Leave empty to disable"
                            />
                            <p className="text-xs text-[var(--text-dim)] mt-1">
                                If free space falls below this threshold, oldest recordings will be deleted until space is freed.
                                Leave empty to disable this feature.
                            </p>
                        </div>
                    </div>
                </div>

                <div className="flex justify-end">
                    <button
                        type="submit"
                        disabled={loading}
                        className="px-4 py-2 bg-[var(--accent)] text-white rounded hover:bg-[var(--accent)]/90 disabled:opacity-50 flex items-center gap-2"
                    >
                        <Save size={16} />
                        {loading ? 'Saving...' : 'Save Retention Settings'}
                    </button>
                </div>
            </form>
        </div>
    )
}
