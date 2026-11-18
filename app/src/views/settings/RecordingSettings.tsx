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
import { Save, FolderOpen, Clock, Shield, HardDrive, Archive, Link2, Trash2, RefreshCw, AlertTriangle, RotateCcw } from 'lucide-react'
import { useSnackbar } from '../../components/Snackbar'
import { UsageBar } from '../../components/ui/stats'
import { RecordingPauseSetting } from './RecordingPauseSetting'
import { useDateFormat, type DateFormatters } from '../../i18n'
import { useConfirm } from '../../components/ui/ConfirmDialog'

interface OrphanIdentity {
    camera_uuid?: string | null
    camera_id?: number | null
    camera_name?: string | null
    ip_address?: string | null
}

interface OrphanTree {
    id: string
    original_dir?: string | null
    reason?: string | null
    quarantined_at?: string | null
    identity?: OrphanIdentity | null
    file_count: number
    total_bytes: number
    earliest?: string | null
    latest?: string | null
    suggested_camera_id?: number | null
}

interface CameraOption {
    id: number
    name?: string | null
    ip_address?: string | null
}

function formatBytes(bytes: number): string {
    if (!bytes) return '0 B'
    const units = ['B', 'KB', 'MB', 'GB', 'TB']
    const i = Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), units.length - 1)
    return `${(bytes / Math.pow(1024, i)).toFixed(i === 0 ? 0 : 1)} ${units[i]}`
}

/** Where recordings reach the host disk (GET /recordings/storage, #976). */
interface StorageMount {
    // Compose bind-mounts RECORDINGS_PATH: the host folder is chosen in .env.
    docker_managed: boolean
    host_path: string | null
    container_path: string | null
    // The path saved on this page, or null for the default.
    custom_path: string | null
    // ok | other_mount (an operator-added volume) | container_layer (lost on recreate)
    path_status: 'ok' | 'other_mount' | 'container_layer'
}

function apiErrorMessage(e: unknown, fallback: string): string {
    const err = e as { response?: { data?: { detail?: unknown } }; message?: string } | null
    const detail = err?.response?.data?.detail
    return typeof detail === 'string' ? detail : err?.message || fallback
}

function formatDay(iso: string | null | undefined, fmt: DateFormatters): string {
    if (!iso) return '?'
    try {
        return fmt.date(iso)
    } catch {
        return iso
    }
}

export function RecordingSettings() {
    const confirm = useConfirm()
    const fmt = useDateFormat()
    const { showError, showSuccess, showWarning } = useSnackbar()
    const [loading, setLoading] = useState(false)
    const [path, setPath] = useState('')
    const [mount, setMount] = useState<StorageMount | null>(null)

    // Retention settings
    const [retentionDays, setRetentionDays] = useState(30)
    const [protectFlagged, setProtectFlagged] = useState(true)
    const [minFreeSpace, setMinFreeSpace] = useState<number | ''>('')
    // The floor in force when the field is empty (automatic: 5 % of the disk, 5 to 20 GB).
    const [autoFreeSpace, setAutoFreeSpace] = useState<number | null>(null)
    // What the gauge marks: the operator's floor, else the automatic one; none when rotation is off (0).
    const floorInForce = minFreeSpace !== '' ? (minFreeSpace > 0 ? minFreeSpace : null) : autoFreeSpace
    const [purgeOrphaned, setPurgeOrphaned] = useState(false)

    // Disk usage of the effective recordings volume (absent on older
    // backends or stat failure — the gauge is simply hidden then).
    const [diskInfo, setDiskInfo] = useState<{ total: number; used: number; free: number } | null>(null)

    // Orphaned (quarantined) recording trees — superuser only; the card is
    // hidden entirely when the list is empty or the API refuses access.
    const [orphans, setOrphans] = useState<OrphanTree[]>([])
    const [orphansAvailable, setOrphansAvailable] = useState(false)
    const [cameras, setCameras] = useState<CameraOption[]>([])
    const [attachTarget, setAttachTarget] = useState<Record<string, number | ''>>({})
    const [orphanBusy, setOrphanBusy] = useState<string | null>(null)

    useEffect(() => {
        loadSettings()
        loadOrphans()
    }, [])

    const loadSettings = async () => {
        try {
            setLoading(true)
            const [storageRes, retentionRes] = await Promise.all([
                apiService.getRecordingStorage(),
                apiService.getRecordingRetention()
            ])
            const s = storageRes.data
            // The saved path only; the default shows as the placeholder.
            setPath(s.custom_path ?? s.root_path ?? s.recordings_base_path ?? '')
            setMount(
                typeof s.docker_managed === 'boolean'
                    ? {
                          docker_managed: s.docker_managed,
                          host_path: s.host_path ?? null,
                          container_path: s.container_path ?? null,
                          custom_path: s.custom_path ?? null,
                          path_status: s.path_status ?? 'ok',
                      }
                    : null
            )
            setDiskInfo(
                s.disk_total != null && s.disk_used != null && s.disk_free != null
                    ? { total: s.disk_total, used: s.disk_used, free: s.disk_free }
                    : null
            )

            // Load retention settings
            const retention = retentionRes.data
            setRetentionDays(retention.retention_days ?? 30)
            setProtectFlagged(retention.protect_flagged ?? true)
            setMinFreeSpace(retention.min_free_space_gb ?? '')
            setAutoFreeSpace(retention.effective_min_free_space_gb ?? null)
            setPurgeOrphaned(retention.purge_orphaned_under_pressure ?? false)
        } catch (e: any) {
            showError(e?.message || 'Failed to load recording settings')
        } finally {
            setLoading(false)
        }
    }

    const loadOrphans = async () => {
        try {
            const [orphanRes, cameraRes] = await Promise.all([
                apiService.getRecordingOrphans(),
                apiService.getCameras({}),
            ])
            const items: OrphanTree[] = orphanRes.data?.items || []
            setOrphans(items)
            setOrphansAvailable(true)
            const cams: CameraOption[] = (cameraRes.data?.items || cameraRes.data || [])
            setCameras(Array.isArray(cams) ? cams : [])
            // Pre-select the suggested camera in each row's picker.
            const pre: Record<string, number | ''> = {}
            for (const o of items) pre[o.id] = o.suggested_camera_id ?? ''
            setAttachTarget(pre)
        } catch {
            // Non-superuser (403) or older backend — keep the card hidden.
            setOrphansAvailable(false)
        }
    }

    const handleAttach = async (orphan: OrphanTree) => {
        const camId = attachTarget[orphan.id]
        if (camId === '' || camId === undefined) {
            showError('Pick a camera to attach this footage to')
            return
        }
        const cam = cameras.find((c) => c.id === camId)
        const ok = await confirm({
            title: `Attach ${orphan.file_count} recording file(s) (${formatDay(orphan.earliest, fmt)} – ${formatDay(orphan.latest, fmt)}, ${formatBytes(orphan.total_bytes)}) to camera "${cam?.name || camId}"?`,
            message: "The footage will appear in that camera's playback timeline.",
            confirmLabel: 'Attach',
        })
        if (!ok) return
        try {
            setOrphanBusy(orphan.id)
            const res = await apiService.attachRecordingOrphan(orphan.id, camId as number)
            if (res.data?.fully_merged === false) {
                showSuccess(
                    `Attached with ${res.data.skipped_files?.length ?? 0} file(s) skipped (name collisions) — the remainder stays listed`
                )
            } else {
                showSuccess(`Footage attached (${res.data?.indexed_rows ?? 0} clips indexed)`)
            }
            await loadOrphans()
        } catch (e: any) {
            showError(e?.response?.data?.detail || e?.message || 'Attach failed')
        } finally {
            setOrphanBusy(null)
        }
    }

    const handleDeleteOrphan = async (orphan: OrphanTree) => {
        const ok = await confirm({
            title: `Permanently delete ${orphan.file_count} recording file(s) (${formatBytes(orphan.total_bytes)}) from "${orphan.id}"?`,
            message: 'This cannot be undone.',
            confirmLabel: 'Delete',
            danger: true,
        })
        if (!ok) return
        try {
            setOrphanBusy(orphan.id)
            await apiService.deleteRecordingOrphan(orphan.id)
            showSuccess('Orphaned footage deleted')
            await loadOrphans()
        } catch (e: any) {
            showError(e?.response?.data?.detail || e?.message || 'Delete failed')
        } finally {
            setOrphanBusy(null)
        }
    }

    const handleRescan = async () => {
        try {
            setOrphanBusy('__rescan__')
            const res = await apiService.rescanRecordingOrphans()
            const n = res.data?.quarantined ?? 0
            showSuccess(n ? `Rescan quarantined ${n} tree(s)` : 'Rescan found nothing new')
            await loadOrphans()
        } catch (e: any) {
            showError(e?.response?.data?.detail || e?.message || 'Rescan failed')
        } finally {
            setOrphanBusy(null)
        }
    }

    const handleSaveStorage = async (e: React.FormEvent) => {
        e.preventDefault()
        try {
            setLoading(true)
            const res = await apiService.updateRecordingStorage({ recordings_base_path: path.trim() || null })
            if (res?.data?.warning) showWarning(res.data.warning)
            else showSuccess('Storage settings saved successfully')
            await loadSettings()
        } catch (e: any) {
            showError(apiErrorMessage(e, 'Failed to save storage settings'))
        } finally {
            setLoading(false)
        }
    }

    // Back to the default location (the RECORDINGS_PATH mount under Docker).
    // Existing recordings stay where they are.
    const handleResetStorage = async () => {
        try {
            setLoading(true)
            await apiService.updateRecordingStorage({ recordings_base_path: null })
            showSuccess('Recording location reset to the default')
            await loadSettings()
        } catch (e: any) {
            showError(apiErrorMessage(e, 'Failed to reset the recording location'))
        } finally {
            setLoading(false)
        }
    }

    const dockerManaged = mount?.docker_managed === true
    // A path saved before this page explained the mount; the reset button
    // is the way out.
    const customPathOnDocker =
        dockerManaged && !!mount?.custom_path && mount.custom_path.replace(/\/+$/, '') !== mount.container_path

    const handleSaveRetention = async (e: React.FormEvent) => {
        e.preventDefault()
        try {
            setLoading(true)
            const payload = {
                retention_days: retentionDays,
                protect_flagged: protectFlagged,
                min_free_space_gb: minFreeSpace === '' ? null : minFreeSpace,
                purge_orphaned_under_pressure: purgeOrphaned
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

            <RecordingPauseSetting />

            {/* Storage Settings */}
            <form onSubmit={handleSaveStorage} className="space-y-6 mb-8">
                <div className="bg-[var(--panel)] border border-[var(--border)] p-6">
                    <h3 className="text-lg font-medium mb-4 flex items-center gap-2">
                        <HardDrive size={20} />
                        Storage Location
                    </h3>
                    
                    {dockerManaged ? (
                        <div className="space-y-4 mb-4">
                            <div>
                                <div className="text-sm font-medium mb-1">Recordings folder on this machine</div>
                                <code className="block w-full px-3 py-2 bg-[var(--bg-2)] border border-[var(--border)] break-all">
                                    {mount?.host_path}
                                </code>
                                {mount?.host_path?.startsWith('.') && (
                                    <p className="text-xs text-[var(--text-dim)] mt-1">Relative to the OpenNVR folder (where .env is).</p>
                                )}
                            </div>
                            <p className="text-sm text-[var(--text-dim)]">
                                To store recordings on another folder or drive, set <code>RECORDINGS_PATH</code> in
                                the <code>.env</code> file in your OpenNVR folder, then run <code>./start.sh up</code>
                                {' '}(<code>.\start.ps1 up</code> on Windows). OpenNVR runs in Docker, so the folder
                                is chosen there, not on this page. Existing recordings stay where they are; move them
                                yourself if you want them in the new place.
                            </p>

                            {customPathOnDocker && mount?.path_status === 'container_layer' && (
                                <div className="flex gap-3 p-3 border border-[var(--danger)]/50 bg-[var(--danger)]/10 text-sm">
                                    <AlertTriangle size={18} className="mt-0.5 shrink-0 text-[var(--danger)]" />
                                    <div className="space-y-2">
                                        <div>
                                            New recordings are going to <code>{mount.custom_path}</code>, a folder inside
                                            the OpenNVR containers rather than on this machine's disk. They are lost when
                                            the containers are recreated, for example on an update.
                                        </div>
                                        <button
                                            type="button"
                                            onClick={handleResetStorage}
                                            disabled={loading}
                                            className="px-3 py-1.5 border border-[var(--border)] bg-[var(--panel)] hover:border-[var(--accent)] disabled:opacity-50 flex items-center gap-2"
                                        >
                                            <RotateCcw size={14} /> Record to {mount.host_path} instead
                                        </button>
                                    </div>
                                </div>
                            )}
                            {customPathOnDocker && mount?.path_status !== 'container_layer' && (
                                <div className="flex gap-3 p-3 border border-[var(--border)] bg-[var(--bg-2)] text-sm">
                                    <div className="space-y-2">
                                        <div>
                                            New recordings are going to <code>{mount?.custom_path}</code>
                                            {mount?.path_status === 'other_mount'
                                                ? ', a volume added to the containers outside RECORDINGS_PATH. MediaMTX needs the same volume at the same path.'
                                                : ', a folder inside the RECORDINGS_PATH mount.'}
                                        </div>
                                        <button
                                            type="button"
                                            onClick={handleResetStorage}
                                            disabled={loading}
                                            className="px-3 py-1.5 border border-[var(--border)] bg-[var(--panel)] hover:border-[var(--accent)] disabled:opacity-50 flex items-center gap-2"
                                        >
                                            <RotateCcw size={14} /> Use the default location
                                        </button>
                                    </div>
                                </div>
                            )}
                        </div>
                    ) : (
                        <>
                            <p className="text-sm text-[var(--text-dim)] mb-6">
                                Configure where camera recordings will be stored. Changing this affects new
                                recordings; existing recordings stay in their original location.
                            </p>
                            <div className="mb-4">
                                <label className="block text-sm font-medium mb-1">
                                    Recording Path <span className="text-[var(--text-dim)] text-xs">(leave empty for the default)</span>
                                </label>
                                <input
                                    type="text"
                                    value={path}
                                    onChange={(e) => setPath(e.target.value)}
                                    className="w-full px-3 py-2 bg-[var(--bg-2)] border border-[var(--border)] focus:border-[var(--accent)] focus:outline-none"
                                    placeholder="./recordings"
                                />
                                <p className="text-xs text-[var(--text-dim)] mt-1">
                                    A folder on this machine, relative to the server folder (./recordings) or absolute
                                    (/mnt/storage, D:/Recordings). It is created if missing.
                                </p>
                            </div>
                        </>
                    )}

                    <div className="space-y-4">
                        {diskInfo && diskInfo.total > 0 && (
                            <div>
                                <div className="flex items-baseline justify-between text-sm mb-1.5">
                                    <span className="font-medium">Disk usage</span>
                                    <span className="text-[var(--text-dim)]">
                                        {formatBytes(diskInfo.free)} free of {formatBytes(diskInfo.total)} ({Math.round((diskInfo.used / diskInfo.total) * 100)}% used)
                                    </span>
                                </div>
                                <UsageBar
                                    used={diskInfo.used}
                                    total={diskInfo.total}
                                    warnAt={floorInForce != null ? Math.max(0, 1 - (2 * floorInForce * 1024 ** 3) / diskInfo.total) : 0.8}
                                    critAt={floorInForce != null ? Math.max(0, 1 - (floorInForce * 1024 ** 3) / diskInfo.total) : 0.9}
                                />
                            </div>
                        )}
                    </div>
                </div>

                {!dockerManaged && (
                    <div className="flex justify-end">
                        <button
                            type="submit"
                            disabled={loading}
                            className="px-4 py-2 bg-[var(--accent)] text-white hover:bg-[var(--accent)]/90 disabled:opacity-50 flex items-center gap-2"
                        >
                            <Save size={16} />
                            {loading ? 'Saving...' : 'Save Storage Settings'}
                        </button>
                    </div>
                )}
            </form>

            {/* Retention Settings */}
            <form onSubmit={handleSaveRetention} className="space-y-6">
                <div className="bg-[var(--panel)] border border-[var(--border)] p-6">
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
                                className="w-full px-3 py-2 bg-[var(--bg-2)] border border-[var(--border)] focus:border-[var(--accent)] focus:outline-none"
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
                                className="w-4 h-4 text-[var(--accent)] bg-[var(--bg-2)] border-[var(--border)] focus:ring-[var(--accent)]"
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
                                min={0}
                                max={100000}
                                value={minFreeSpace}
                                onChange={(e) => setMinFreeSpace(e.target.value === '' ? '' : parseInt(e.target.value))}
                                className="w-full px-3 py-2 bg-[var(--bg-2)] border border-[var(--border)] focus:border-[var(--accent)] focus:outline-none"
                                placeholder={autoFreeSpace != null ? `Automatic: ${autoFreeSpace} GB` : 'Automatic'}
                            />
                            <p className="text-xs text-[var(--text-dim)] mt-1">
                                Below this much free space the oldest recordings are rotated out, oldest first, and recording
                                continues. Leave empty for automatic: 5 % of the disk, at least 5 GB and at most 20 GB
                                {autoFreeSpace != null ? ` (${autoFreeSpace} GB here)` : ''}. The floor is the room the
                                system, its database and an update have on a shared disk. Photos of events are never
                                deleted for space. Set 0 to turn rotation off.
                            </p>
                        </div>

                        {/* Purge quarantined footage under pressure */}
                        <div className="flex items-center gap-3">
                            <input
                                type="checkbox"
                                id="purge-orphaned"
                                checked={purgeOrphaned}
                                onChange={(e) => setPurgeOrphaned(e.target.checked)}
                                className="w-4 h-4 text-[var(--accent)] bg-[var(--bg-2)] border-[var(--border)] focus:ring-[var(--accent)]"
                            />
                            <label htmlFor="purge-orphaned" className="text-sm font-medium flex items-center gap-2">
                                <Archive size={16} className="text-[var(--accent)]" />
                                Purge Quarantined Footage Under Disk Pressure
                            </label>
                        </div>
                        <p className="text-xs text-[var(--text-dim)] ml-7">
                            When the free-space purge runs out of regular recordings, also delete quarantined
                            footage from the orphaned/ folder (oldest first). Off by default — quarantined footage
                            is normally kept recoverable.
                        </p>
                    </div>
                </div>

                <div className="flex justify-end">
                    <button
                        type="submit"
                        disabled={loading}
                        className="px-4 py-2 bg-[var(--accent)] text-white hover:bg-[var(--accent)]/90 disabled:opacity-50 flex items-center gap-2"
                    >
                        <Save size={16} />
                        {loading ? 'Saving...' : 'Save Retention Settings'}
                    </button>
                </div>
            </form>

            {/* Orphaned recordings — quarantined footage that could not be
                attributed to a current camera (e.g. after a database rebuild).
                Hidden when empty or when the user is not a superuser. */}
            {orphansAvailable && orphans.length > 0 && (
                <div className="mt-8 bg-[var(--panel)] border border-[color-mix(in_oklab,var(--warn)_45%,var(--border))] p-6">
                    <div className="flex items-center justify-between mb-4">
                        <h3 className="text-lg font-medium flex items-center gap-2">
                            <Archive size={20} className="text-[var(--warn)]" />
                            Orphaned Recordings
                        </h3>
                        <button
                            onClick={handleRescan}
                            disabled={orphanBusy !== null}
                            className="px-3 py-1.5 text-sm border border-[var(--border)] hover:border-[var(--accent)] disabled:opacity-50 flex items-center gap-2"
                        >
                            <RefreshCw size={14} className={orphanBusy === '__rescan__' ? 'animate-spin' : ''} />
                            Rescan
                        </button>
                    </div>
                    <p className="text-sm text-[var(--text-dim)] mb-4">
                        This footage was found on disk but could not be matched to a current camera
                        (usually after the database was rebuilt and cameras were re-added). It is safe
                        on disk and ages out with the normal retention policy. Attach each tree to the
                        camera it belongs to, or delete it.
                    </p>
                    <div className="overflow-x-auto">
                        <table className="w-full text-sm">
                            <thead>
                                <tr className="text-left text-[var(--text-dim)] border-b border-[var(--border)]">
                                    <th className="py-2 pr-4">Previous identity</th>
                                    <th className="py-2 pr-4">Footage</th>
                                    <th className="py-2 pr-4">Reason</th>
                                    <th className="py-2 pr-4">Attach to</th>
                                    <th className="py-2"></th>
                                </tr>
                            </thead>
                            <tbody>
                                {orphans.map((o) => (
                                    <tr key={o.id} className="border-b border-[var(--border)] align-middle">
                                        <td className="py-3 pr-4">
                                            <div className="font-medium">
                                                {o.identity?.camera_name || o.original_dir || o.id}
                                            </div>
                                            <div className="text-xs text-[var(--text-dim)]">
                                                {o.identity?.ip_address ? `IP ${o.identity.ip_address} · ` : ''}
                                                {o.original_dir || o.id}
                                            </div>
                                        </td>
                                        <td className="py-3 pr-4 whitespace-nowrap">
                                            <div>{o.file_count} files · {formatBytes(o.total_bytes)}</div>
                                            <div className="text-xs text-[var(--text-dim)]">
                                                {formatDay(o.earliest, fmt)} – {formatDay(o.latest, fmt)}
                                            </div>
                                        </td>
                                        <td className="py-3 pr-4 text-xs text-[var(--text-dim)] whitespace-nowrap">
                                            {o.reason === 'no-owner' && 'No matching camera'}
                                            {o.reason === 'uuid-mismatch' && 'Belonged to a different camera'}
                                            {o.reason === 'predates-camera' && 'Predates the current camera'}
                                            {!o.reason && 'Unknown'}
                                        </td>
                                        <td className="py-3 pr-4">
                                            <select
                                                value={attachTarget[o.id] ?? ''}
                                                onChange={(e) =>
                                                    setAttachTarget((prev) => ({
                                                        ...prev,
                                                        [o.id]: e.target.value === '' ? '' : parseInt(e.target.value),
                                                    }))
                                                }
                                                className="px-2 py-1.5 bg-[var(--bg-2)] border border-[var(--border)] focus:border-[var(--accent)] focus:outline-none"
                                            >
                                                <option value="">Select camera…</option>
                                                {cameras.map((c) => (
                                                    <option key={c.id} value={c.id}>
                                                        {c.name || `Camera ${c.id}`}
                                                        {c.id === o.suggested_camera_id ? ' (suggested)' : ''}
                                                    </option>
                                                ))}
                                            </select>
                                        </td>
                                        <td className="py-3 whitespace-nowrap">
                                            <div className="flex items-center gap-2 justify-end">
                                                <button
                                                    onClick={() => handleAttach(o)}
                                                    disabled={orphanBusy !== null || attachTarget[o.id] === '' || attachTarget[o.id] === undefined}
                                                    className="px-3 py-1.5 text-sm bg-[var(--accent)] text-white hover:bg-[var(--accent)]/90 disabled:opacity-50 flex items-center gap-1.5"
                                                >
                                                    <Link2 size={14} />
                                                    {orphanBusy === o.id ? 'Working…' : 'Attach'}
                                                </button>
                                                <button
                                                    onClick={() => handleDeleteOrphan(o)}
                                                    disabled={orphanBusy !== null}
                                                    className="px-3 py-1.5 text-sm border border-[color-mix(in_oklab,var(--danger)_45%,var(--border))] text-[var(--danger)] hover:bg-[color-mix(in_oklab,var(--danger)_14%,transparent)] disabled:opacity-50 flex items-center gap-1.5"
                                                >
                                                    <Trash2 size={14} />
                                                    Delete
                                                </button>
                                            </div>
                                        </td>
                                    </tr>
                                ))}
                            </tbody>
                        </table>
                    </div>
                </div>
            )}
        </div>
    )
}
