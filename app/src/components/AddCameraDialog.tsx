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

import { useEffect, useRef, useState } from 'react'
import { apiService } from '../lib/apiService'
import { duplicateCameraNames, isDuplicateCameraError } from '../services/cameraService'
import { QrScanner } from './QrScanner'
import { Modal } from './Modal'
import { Button, EmptyState } from './ui'
import { Camera, ChevronDown, CheckCircle, Loader2, Plus, Search, SearchX, Video, X } from 'lucide-react'

type DiscoveredCamera = { ip: string; scheme?: string; service_urls?: string[] }

// "http · port 80" style subtitle for a discovered device. The server sends
// {ip, scheme, service_urls}; the port lives inside the service URL.
function deviceSubtitle(cam: DiscoveredCamera): string {
  const scheme = cam.scheme || 'http'
  let port = ''
  try {
    const u = new URL(cam.service_urls?.[0] || '')
    port = u.port || (u.protocol === 'https:' ? '443' : '80')
  } catch {
    port = scheme === 'https' ? '443' : '80'
  }
  return `${scheme} · port ${port}`
}

export function AddCameraDialog({
  onClose,
  onCameraAdded,
  onCameraSelected,
  existingCameras = [],
  title = 'Add Camera to Tile',
}: {
  onClose: () => void
  onCameraAdded: (cameraId?: number) => void
  onCameraSelected?: (cameraId: number) => void
  existingCameras?: Array<{id: number, name: string}>
  title?: string
}) {
  const [mode, setMode] = useState<'discover' | 'select' | 'manual'>('discover')
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [searchQuery, setSearchQuery] = useState('')

  // ONVIF discovery state
  const [discovering, setDiscovering] = useState(false)
  const [discoveredCameras, setDiscoveredCameras] = useState<DiscoveredCamera[]>([])
  const [selectedCamera, setSelectedCamera] = useState<{ip: string, name?: string} | null>(null)

  // Scan range: one always-visible panel. `configuredCidr` tracks the saved
  // Camera LAN range so the "Save as Camera LAN & scan" button only appears
  // when the input actually moves the boundary.
  const [scanInfo, setScanInfo] = useState<{cidrs: string[], source: string} | null>(null)
  const [suggestedCidrs, setSuggestedCidrs] = useState<string[]>([])
  // No range configured or detectable (e.g. bridge container without a host
  // IP hint) — prompt for one instead of scanning nothing.
  const [noAutoRange, setNoAutoRange] = useState(false)
  const [rangeInput, setRangeInput] = useState('')
  const [configuredCidr, setConfiguredCidr] = useState<string | null>(null)
  const [savingRange, setSavingRange] = useState(false)

  // Authentication step
  const [authStep, setAuthStep] = useState(false)
  const [credentials, setCredentials] = useState({ username: 'admin', password: '' })
  const [authenticating, setAuthenticating] = useState(false)
  const [profiles, setProfiles] = useState<Array<{token: string, name: string, stream_uri?: string, width?: number, height?: number}>>([])
  const [selectedProfile, setSelectedProfile] = useState<string>('')
  const [rtspUrl, setRtspUrl] = useState('')
  const [scanQr, setScanQr] = useState(false)
  const [deviceInfo, setDeviceInfo] = useState<{
    manufacturer?: string
    model?: string
    firmwareversion?: string
    serialnumber?: string
    hardwareid?: string
  } | null>(null)
  const [cameraName, setCameraName] = useState('')

  // Manual entry form
  const [form, setForm] = useState({
    name: '',
    ip_address: '',
    port: 554,
    username: '',
    password: '',
    rtsp_url: '',
  })

  // Duplicate-camera 409: hold the payload so "Add Anyway" can retry with force.
  const [duplicatePrompt, setDuplicatePrompt] = useState<{ payload: any; names: string[] } | null>(null)

  // Shared create path for both the discovered and manual flows: create,
  // retry provisioning if it didn't stick, surface the duplicate confirm.
  const createAndFinish = async (payload: any, force = false) => {
    setLoading(true)
    setError(null)
    try {
      const response = await apiService.createCamera(payload, { force })
      const newCameraId = response?.data?.id

      // The server already auto-provisions on create; only retry from here
      // if that didn't stick (e.g. MediaMTX was briefly unreachable).
      // Unconditionally re-provisioning an already-provisioned camera forces
      // a path replace that can drop the stream it just started.
      if (newCameraId && response?.data?.mediamtx_provisioned !== true) {
        try {
          await apiService.provisionCameraMediaMTX(newCameraId, { enable_recording: true })
        } catch (e) {
          console.warn('Auto-provision failed, camera added but not streaming:', e)
        }
      }

      setDuplicatePrompt(null)
      onCameraAdded(newCameraId)
    } catch (e: any) {
      if (!force && isDuplicateCameraError(e)) {
        setDuplicatePrompt({ payload, names: duplicateCameraNames(e) })
      } else {
        setError(
          (typeof e?.data?.detail === 'string' ? e.data.detail : null) ||
            e?.message ||
            'Failed to add camera'
        )
      }
    } finally {
      setLoading(false)
    }
  }

  // Discover cameras on network via ONVIF. `overrideCidr` scans that range
  // once without touching the saved Camera LAN. Starting a new scan while one
  // is in flight supersedes it: the sequence token makes the old request's
  // completion a no-op, and aborting the HTTP request makes the SERVER stop
  // its sweep too (it single-flights scans and cancels on disconnect), so an
  // operator can re-aim a scan mid-sweep without wedging the next one.
  const scanSeqRef = useRef(0)
  const scanAbortRef = useRef<AbortController | null>(null)
  const [scanStopped, setScanStopped] = useState(false)

  // Stop the running scan: invalidate its sequence token and abort the HTTP
  // request so the connection drops (the server cancels its sweep).
  const cancelScan = () => {
    if (!discovering) return
    scanSeqRef.current++
    scanAbortRef.current?.abort()
    setDiscovering(false)
    setScanStopped(true)
  }

  const handleDiscover = async (overrideCidr?: string) => {
    const seq = ++scanSeqRef.current
    scanAbortRef.current?.abort()
    const abort = new AbortController()
    scanAbortRef.current = abort
    setDiscovering(true)
    setScanStopped(false)
    setError(null)
    setDiscoveredCameras([])
    setNoAutoRange(false)
    // Show the target range while the scan is still running.
    if (overrideCidr) setScanInfo({ cidrs: [overrideCidr], source: 'override' })

    try {
      const response = await apiService.onvifDiscover(
        overrideCidr ? { cidr: overrideCidr } : undefined,
        abort.signal,
      )
      if (seq !== scanSeqRef.current) return
      const data = response?.data || {}
      const devices = data.devices || []
      setScanInfo({ cidrs: data.scan_cidrs || [], source: data.source || 'configured' })
      if (data.source === 'configured' && data.scan_cidrs?.[0]) {
        setConfiguredCidr(data.scan_cidrs[0])
      }
      setSuggestedCidrs(data.suggested_cidrs || [])
      setDiscoveredCameras(devices.map((d: any) => ({
        ip: d.ip || d.host || d.address,
        scheme: d.scheme || 'http',
        service_urls: d.service_urls || [],
      })))
    } catch (e: any) {
      if (seq !== scanSeqRef.current || e?.name === 'AbortError') return
      setError(e?.data?.detail || e?.message || 'Discovery failed. Try manual entry.')
    } finally {
      if (seq === scanSeqRef.current) setDiscovering(false)
    }
  }

  // One Scan button: an input matching the saved Camera LAN scans normally;
  // anything else is a one-off override scan of that range.
  const handleScanClick = () => {
    const cidrVal = rangeInput.trim()
    if (cidrVal && cidrVal !== configuredCidr) {
      handleDiscover(cidrVal)
    } else {
      handleDiscover()
    }
  }

  // Persist a new primary Camera LAN subnet, then rescan with it. Unlike a
  // one-off scan this moves the boundary cameras can be connected/added on.
  const handleSaveRangeAndScan = async () => {
    const cidrVal = rangeInput.trim()
    if (!cidrVal) return
    setSavingRange(true)
    setError(null)
    try {
      const cur = await apiService.getCameraLAN()
      const settings = cur?.data?.settings || {}
      await apiService.updateCameraLAN({
        interface_name: settings.interface_name || 'eth0',
        subnet_cidr: cidrVal,
      })
      setConfiguredCidr(cidrVal)
      setScanInfo({ cidrs: [cidrVal], source: 'configured' })
      await handleDiscover()
    } catch (e: any) {
      setError(e?.data?.detail || e?.message || 'Failed to update Camera LAN.')
    } finally {
      setSavingRange(false)
    }
  }

  // Start discovery on mount. Fetch the scan plan first (fast) so the target
  // range is visible while the scan itself (slow) is still running — and so a
  // "nothing to scan" answer becomes a prompt for a range instead of an error.
  useEffect(() => {
    if (mode !== 'discover') return
    let cancelled = false
    ;(async () => {
      try {
        const res = await apiService.onvifDiscoverPlan()
        if (cancelled) return
        const cidrs: string[] = res?.data?.scan_cidrs || []
        if (cidrs.length === 0) {
          setNoAutoRange(true)
          return
        }
        setScanInfo({ cidrs, source: res?.data?.source || 'configured' })
        if (res?.data?.source === 'configured' && cidrs[0]) setConfiguredCidr(cidrs[0])
      } catch {
        // Plan endpoint unavailable — fall through and just scan.
      }
      if (!cancelled) handleDiscover()
    })()
    return () => { cancelled = true; scanAbortRef.current?.abort() }
  }, [])

  // Keep the range input prefilled with the active range, but never clobber
  // what the operator typed.
  useEffect(() => {
    if (scanInfo?.cidrs[0] && !rangeInput) setRangeInput(scanInfo.cidrs[0])
  }, [scanInfo])

  // Select a discovered camera and show auth step
  const handleSelectDiscovered = (camera: {ip: string, name?: string}) => {
    setSelectedCamera(camera)
    setAuthStep(true)
    setError(null)
    setCameraName('') // Reset camera name for user input
  }

  // Authenticate and get RTSP URL using HTTP Digest (Hikvision compatible)
  const handleAuthenticate = async () => {
    if (!selectedCamera || !credentials.username || !credentials.password) {
      setError('Username and password are required')
      return
    }

    setAuthenticating(true)
    setError(null)

    try {
      // Use new onvifConnect endpoint which uses HTTP Digest auth
      // This works with Hikvision and other devices that don't support WS-Security
      const response = await apiService.onvifConnect(selectedCamera.ip, {
        username: credentials.username,
        password: credentials.password,
        port: 80
      })

      const data = response?.data || {}
      const profileList = data.profiles || []

      if (profileList.length > 0) {
        setProfiles(profileList)
        setDeviceInfo(data.device_info || null)

        // Select first profile that has a stream URI
        const firstWithUri = profileList.find((p: any) => p.stream_uri) || profileList[0]
        setSelectedProfile(firstWithUri.token)
        setRtspUrl(firstWithUri.stream_uri || '')

        if (!firstWithUri.stream_uri) {
          setError('Could not get RTSP URL. Check camera settings.')
        }
      } else {
        setError('No stream profiles found. Check credentials.')
      }
    } catch (e: any) {
      const detail = e?.response?.data?.detail || e?.data?.detail || e?.message || ''
      if (detail.includes('401') || detail.toLowerCase().includes('authentication')) {
        setError('Authentication failed. Check username and password.')
      } else if (detail.includes('timeout') || detail.includes('connect')) {
        setError('Cannot connect to camera. Check IP address and network.')
      } else {
        setError(detail || 'Connection failed. Check credentials and network.')
      }
    } finally {
      setAuthenticating(false)
    }
  }

  // Handle profile change - use stored stream_uri from profiles list
  const handleProfileChange = (profileToken: string) => {
    setSelectedProfile(profileToken)
    const profile = profiles.find(p => p.token === profileToken)
    if (profile?.stream_uri) {
      setRtspUrl(profile.stream_uri)
    }
  }

  // Helper to embed credentials into RTSP URL
  const embedCredentialsInRtspUrl = (url: string, username: string, password: string): string => {
    try {
      // Parse the URL
      const urlObj = new URL(url)
      // URL-encode the password (handle special chars like @)
      const encodedPassword = encodeURIComponent(password)
      // Set credentials
      urlObj.username = username
      urlObj.password = encodedPassword
      return urlObj.toString()
    } catch {
      // If URL parsing fails, try manual insertion
      if (url.startsWith('rtsp://')) {
        const encodedPassword = encodeURIComponent(password)
        return url.replace('rtsp://', `rtsp://${username}:${encodedPassword}@`)
      }
      return url
    }
  }

  // Add discovered camera
  const handleAddDiscoveredCamera = async () => {
    if (!selectedCamera || !rtspUrl) {
      setError('RTSP URL not available')
      return
    }

    if (!cameraName.trim()) {
      setError('Camera name is required')
      return
    }

    // Embed credentials into RTSP URL for MediaMTX
    const rtspWithCredentials = embedCredentialsInRtspUrl(rtspUrl, credentials.username, credentials.password)

    await createAndFinish({
      name: cameraName.trim(),
      ip_address: selectedCamera.ip,
      port: 554,
      username: credentials.username,
      password: credentials.password,
      rtsp_url: rtspWithCredentials,
      // ONVIF device metadata
      manufacturer: deviceInfo?.manufacturer || undefined,
      model: deviceInfo?.model || undefined,
      firmware_version: deviceInfo?.firmwareversion || undefined,
      serial_number: deviceInfo?.serialnumber || undefined,
      hardware_id: deviceInfo?.hardwareid || undefined,
    })
  }

  // Add manual camera
  const handleAddManualCamera = async () => {
    if (!form.name.trim() || !form.ip_address.trim()) {
      setError('Name and IP address are required')
      return
    }

    // rtsp_url is optional. If provided, the server embeds the username/password
    // into it (URL-encoding specials like "@") when they aren't already there.
    // If left blank, the server derives the URL from the IP + credentials.
    // Either way it back-fills device identity.
    await createAndFinish({
      name: form.name,
      ip_address: form.ip_address,
      port: form.port,
      username: form.username || undefined,
      password: form.password || undefined,
      rtsp_url: form.rtsp_url || undefined,
    })
  }

  const handleSelectExisting = (cameraId: number) => {
    onCameraSelected?.(cameraId)
    onClose()
  }


  const filteredCameras = existingCameras.filter(c =>
    c.name.toLowerCase().includes(searchQuery.toLowerCase())
  )

  const saveMovesBoundary =
    rangeInput.trim() !== '' && rangeInput.trim() !== configuredCidr

  const scanStatus = discovering
    ? 'Scanning network...'
    : scanStopped
    ? 'Scan stopped'
    : noAutoRange
    ? 'No network range to scan'
    : scanInfo
    ? `Found ${discoveredCameras.length} camera(s)`
    : ''

  const footer = (
    <div className="space-y-2">
      {/* Duplicate-camera confirm (409 from create) */}
      {duplicatePrompt && (
        <div className="p-3 border border-amber-600 bg-amber-950/40 text-sm">
          <div className="mb-2">
            A camera with this IP address or stream URL is already added
            {duplicatePrompt.names.length > 0 && (
              <>: <span className="font-medium">{duplicatePrompt.names.join(', ')}</span></>
            )}
            . Add it again anyway?
          </div>
          <div className="flex justify-end gap-2">
            <Button onClick={() => setDuplicatePrompt(null)} disabled={loading}>
              Cancel
            </Button>
            <button
              className="px-3 py-1 text-sm bg-amber-600 text-white disabled:opacity-50"
              onClick={() => createAndFinish(duplicatePrompt.payload, true)}
              disabled={loading}
            >
              {loading ? 'Adding...' : 'Add Anyway'}
            </button>
          </div>
        </div>
      )}
      <div className="flex items-center justify-end gap-2">
        <Button onClick={onClose}>Cancel</Button>
        {mode === 'discover' && authStep && rtspUrl && (
          <Button
            variant="primary"
            onClick={handleAddDiscoveredCamera}
            disabled={loading || !cameraName.trim()}
          >
            {loading ? 'Adding...' : 'Add Camera'}
          </Button>
        )}
        {mode === 'manual' && (
          <Button
            variant="primary"
            onClick={handleAddManualCamera}
            disabled={loading || !form.name.trim() || !form.ip_address.trim()}
          >
            {loading ? 'Adding...' : 'Add Camera'}
          </Button>
        )}
      </div>
    </div>
  )

  return (
    <Modal
      open
      onClose={onClose}
      title={<><Video size={16} /> {title}</>}
      widthClassName="w-full max-w-3xl mx-4"
      bodyClassName="p-0 flex flex-col"
      footer={footer}
    >
      {/* Tab Switcher */}
      <div className="flex border-b border-neutral-700">
        <button
          className={`flex-1 px-3 py-2 text-xs ${mode === 'discover' ? 'bg-[var(--accent)]/20 text-[var(--accent)] border-b-2 border-[var(--accent)]' : 'text-[var(--text-dim)] hover:bg-[var(--panel-2)]'}`}
          onClick={() => { setMode('discover'); setAuthStep(false); setSelectedCamera(null); setError(null); }}
        >
          <Search size={12} className="inline mr-1" />
          Discover
        </button>
        <button
          className={`flex-1 px-3 py-2 text-xs ${mode === 'manual' ? 'bg-[var(--accent)]/20 text-[var(--accent)] border-b-2 border-[var(--accent)]' : 'text-[var(--text-dim)] hover:bg-[var(--panel-2)]'}`}
          onClick={() => { setMode('manual'); setError(null); }}
        >
          <Plus size={12} className="inline mr-1" />
          Manual
        </button>
        {existingCameras.length > 0 && (
          <button
            className={`flex-1 px-3 py-2 text-xs ${mode === 'select' ? 'bg-[var(--accent)]/20 text-[var(--accent)] border-b-2 border-[var(--accent)]' : 'text-[var(--text-dim)] hover:bg-[var(--panel-2)]'}`}
            onClick={() => { setMode('select'); setError(null); }}
          >
            <Camera size={12} className="inline mr-1" />
            Existing
          </button>
        )}
      </div>

      {/* Content */}
      <div className="p-4 flex-1 min-h-0">
        {error && (
          <div className="mb-4 p-2 bg-red-900/20 border border-red-800 text-red-400 text-sm">
            {error}
          </div>
        )}

        {/* DISCOVER MODE — scan panel | results, side by side. The results
            column is the single scroller here (the panel fits statically). */}
        {mode === 'discover' && !authStep && (
          <div className="grid grid-cols-1 md:grid-cols-[260px_1fr] gap-4 md:h-[420px]">
            {/* Scan panel */}
            <div className="space-y-3 md:border-r md:border-neutral-800 md:pr-4">
              <label className="flex flex-col gap-1">
                <span className="text-xs text-[var(--text-dim)] uppercase tracking-wide">Scan range</span>
                <input
                  className="w-full px-2 py-1.5 text-sm bg-[var(--bg-2)] border border-neutral-600 focus:border-[var(--accent)] outline-none"
                  placeholder="e.g. 192.168.1.0/24"
                  value={rangeInput}
                  onChange={(e) => setRangeInput(e.target.value)}
                  disabled={savingRange}
                  autoFocus={noAutoRange}
                />
              </label>
              {scanInfo && (
                <div className="text-xs text-[var(--text-dim)]">
                  {scanInfo.source === 'override' ? 'One-off range'
                    : scanInfo.source === 'auto-detected' ? 'Auto-detected'
                    : 'From Camera LAN settings'}
                </div>
              )}
              {noAutoRange && (
                <div className="text-xs text-[var(--text-dim)]">
                  Couldn't auto-detect your camera network. Enter the subnet your
                  cameras are on.
                </div>
              )}

              {/* One Scan/Stop button — the operator must never be locked out
                  of their own scan. A new Scan mid-sweep supersedes it, on the
                  server too. */}
              {discovering ? (
                <Button
                  className="w-full justify-center hover:border-red-500 hover:text-red-400"
                  onClick={cancelScan}
                >
                  <X size={14} />
                  Stop
                </Button>
              ) : (
                <Button
                  variant="primary"
                  className="w-full justify-center"
                  onClick={handleScanClick}
                  disabled={savingRange}
                >
                  <Search size={14} />
                  Scan
                </Button>
              )}
              {saveMovesBoundary && (
                <div className="space-y-1">
                  <Button
                    className="w-full justify-center"
                    onClick={handleSaveRangeAndScan}
                    disabled={savingRange || !rangeInput.trim()}
                    title="Saving updates the Camera LAN — it defines which network cameras can be reached on. A one-off Scan only looks there; cameras found outside the saved Camera LAN can't be added until it's updated."
                  >
                    {savingRange && <Loader2 size={12} className="animate-spin" />}
                    Save as Camera LAN & scan
                  </Button>
                  <div className="text-[10px] text-[var(--text-dim)]">
                    Saving updates which network cameras can be added on.
                  </div>
                </div>
              )}

              {suggestedCidrs.length > 0 && (
                <div className="space-y-1">
                  <div className="text-xs text-[var(--text-dim)]">This host also appears to be on:</div>
                  {suggestedCidrs.map((s) => (
                    <button
                      key={s}
                      className="block w-full text-left px-2 py-1 text-xs bg-[var(--bg-2)] border border-neutral-700 hover:border-[var(--accent)]"
                      onClick={() => { setRangeInput(s); handleDiscover(s) }}
                    >
                      Scan {s}
                    </button>
                  ))}
                </div>
              )}

              {scanStatus && (
                <div className="text-xs text-[var(--text-dim)] flex items-center gap-1.5">
                  {discovering && <Loader2 size={12} className="animate-spin" />}
                  {scanStatus}
                </div>
              )}
            </div>

            {/* Results column */}
            <div className="min-h-0 md:overflow-auto thin-scroll space-y-2">
              {discovering && discoveredCameras.length === 0 && (
                <div className="text-center py-10">
                  <Loader2 size={24} className="animate-spin mx-auto mb-2 text-[var(--accent)]" />
                  <div className="text-sm text-[var(--text-dim)]">Discovering ONVIF cameras...</div>
                  {scanInfo && scanInfo.cidrs.length > 0 && (
                    <div className="text-xs text-[var(--text-dim)] mt-1">Scanning {scanInfo.cidrs.join(', ')}</div>
                  )}
                </div>
              )}

              {!discovering && discoveredCameras.length === 0 && (
                <EmptyState
                  icon={<SearchX size={28} />}
                  title={
                    scanStopped
                      ? 'Scan stopped'
                      : noAutoRange
                      ? 'No network range to scan'
                      : `No cameras found${scanInfo && scanInfo.cidrs.length > 0 ? ` in ${scanInfo.cidrs.join(', ')}` : ''}`
                  }
                  description={
                    scanStopped
                      ? 'Adjust the range on the left, or Scan again.'
                      : 'Check the scan range, try a suggested one, or use Manual entry.'
                  }
                />
              )}

              {discoveredCameras.map((camera, i) => (
                <button
                  key={i}
                  className="w-full text-left px-3 py-3 bg-[var(--bg-2)] border border-neutral-700 hover:border-[var(--accent)] flex items-center gap-3 transition-colors"
                  onClick={() => handleSelectDiscovered(camera)}
                >
                  <div className="w-10 h-10 bg-[var(--panel)] border border-neutral-600 flex items-center justify-center">
                    <Camera size={20} className="text-[var(--accent)]" />
                  </div>
                  <div className="flex-1">
                    <div className="text-sm font-medium">{camera.ip}</div>
                    <div className="text-xs text-[var(--text-dim)]">{deviceSubtitle(camera)}</div>
                  </div>
                  <ChevronDown size={16} className="text-[var(--text-dim)] -rotate-90" />
                </button>
              ))}
            </div>
          </div>
        )}

        {/* DISCOVER MODE - AUTH STEP */}
        {mode === 'discover' && authStep && selectedCamera && (
          <div className="space-y-4">
            <button
              className="text-xs text-[var(--accent)] flex items-center gap-1 hover:underline"
              onClick={() => { setAuthStep(false); setSelectedCamera(null); setProfiles([]); setRtspUrl(''); setCameraName(''); }}
            >
              ← Back to camera list
            </button>

            <div className="p-3 bg-[var(--bg-2)] border border-neutral-700">
              <div className="flex items-center gap-3">
                <Camera size={24} className="text-[var(--accent)]" />
                <div>
                  <div className="font-medium">{selectedCamera.name || 'ONVIF Camera'}</div>
                  <div className="text-xs text-[var(--text-dim)]">{selectedCamera.ip}</div>
                </div>
              </div>
            </div>

            <label className="flex flex-col gap-1">
              <span className="text-xs text-[var(--text-dim)]">Camera Name *</span>
              <input
                type="text"
                className="bg-[var(--bg-2)] border border-neutral-700 px-3 py-2 text-sm"
                placeholder="e.g., Front Door, Lobby"
                value={cameraName}
                onChange={(e) => setCameraName(e.target.value)}
              />
            </label>

            <div className="grid grid-cols-2 gap-3">
              <label className="flex flex-col gap-1">
                <span className="text-xs text-[var(--text-dim)]">Username</span>
                <input
                  type="text"
                  className="bg-[var(--bg-2)] border border-neutral-700 px-3 py-2 text-sm"
                  value={credentials.username}
                  onChange={(e) => setCredentials(c => ({ ...c, username: e.target.value }))}
                />
              </label>
              <label className="flex flex-col gap-1">
                <span className="text-xs text-[var(--text-dim)]">Password</span>
                <input
                  type="password"
                  className="bg-[var(--bg-2)] border border-neutral-700 px-3 py-2 text-sm"
                  value={credentials.password}
                  onChange={(e) => setCredentials(c => ({ ...c, password: e.target.value }))}
                />
              </label>
            </div>

            {!rtspUrl && (
              <Button
                variant="primary"
                className="w-full justify-center"
                onClick={handleAuthenticate}
                disabled={authenticating || !credentials.password}
              >
                {authenticating ? (
                  <>
                    <Loader2 size={14} className="animate-spin" />
                    Connecting...
                  </>
                ) : (
                  <>
                    <CheckCircle size={14} />
                    Connect & Get Stream
                  </>
                )}
              </Button>
            )}

            {profiles.length > 0 && (
              <label className="flex flex-col gap-1">
                <span className="text-xs text-[var(--text-dim)]">Stream Profile</span>
                <select
                  className="bg-[var(--bg-2)] border border-neutral-700 px-3 py-2 text-sm"
                  value={selectedProfile}
                  onChange={(e) => handleProfileChange(e.target.value)}
                >
                  {profiles.map(p => (
                    <option key={p.token} value={p.token}>
                      {p.name || p.token}
                      {p.width && p.height ? ` (${p.width}x${p.height})` : ''}
                    </option>
                  ))}
                </select>
              </label>
            )}

            {deviceInfo && (
              <div className="p-3 bg-[var(--bg-2)] border border-neutral-700 text-xs space-y-1">
                <div className="font-medium text-sm mb-2">Device Information</div>
                <div className="grid grid-cols-2 gap-x-4 gap-y-1 text-[var(--text-dim)]">
                  <span>Manufacturer:</span>
                  <span className="text-[var(--text)]">{deviceInfo.manufacturer || 'Unknown'}</span>
                  <span>Model:</span>
                  <span className="text-[var(--text)]">{deviceInfo.model || 'Unknown'}</span>
                  {deviceInfo.serialnumber && (
                    <>
                      <span>Serial Number:</span>
                      <span className="text-[var(--text)] font-mono">{deviceInfo.serialnumber}</span>
                    </>
                  )}
                  {deviceInfo.firmwareversion && (
                    <>
                      <span>Firmware:</span>
                      <span className="text-[var(--text)]">{deviceInfo.firmwareversion}</span>
                    </>
                  )}
                  {deviceInfo.hardwareid && (
                    <>
                      <span>Hardware ID:</span>
                      <span className="text-[var(--text)] font-mono">{deviceInfo.hardwareid}</span>
                    </>
                  )}
                </div>
              </div>
            )}

            {rtspUrl && (
              <div className="p-3 bg-green-900/20 border border-green-700">
                <div className="flex items-center gap-2 text-green-400 text-sm mb-1">
                  <CheckCircle size={14} />
                  Stream URL Retrieved
                </div>
                <div className="text-xs font-mono text-[var(--text-dim)] break-all">{rtspUrl}</div>
              </div>
            )}
          </div>
        )}

        {/* MANUAL MODE */}
        {mode === 'manual' && (
          <div className="space-y-4">
            <div className="grid grid-cols-2 gap-3">
              <label className="flex flex-col gap-1">
                <span className="text-xs text-[var(--text-dim)]">Camera Name *</span>
                <input
                  type="text"
                  className="bg-[var(--bg-2)] border border-neutral-700 px-3 py-2 text-sm"
                  placeholder="e.g., Front Door"
                  value={form.name}
                  onChange={(e) => setForm(f => ({ ...f, name: e.target.value }))}
                />
              </label>
              <label className="flex flex-col gap-1">
                <span className="text-xs text-[var(--text-dim)]">IP Address *</span>
                <input
                  type="text"
                  className="bg-[var(--bg-2)] border border-neutral-700 px-3 py-2 text-sm"
                  placeholder="192.168.1.100"
                  value={form.ip_address}
                  onChange={(e) => setForm(f => ({ ...f, ip_address: e.target.value }))}
                />
              </label>
            </div>

            <div className="grid grid-cols-3 gap-3">
              <label className="flex flex-col gap-1">
                <span className="text-xs text-[var(--text-dim)]">Port</span>
                <input
                  type="number"
                  className="bg-[var(--bg-2)] border border-neutral-700 px-3 py-2 text-sm"
                  value={form.port}
                  onChange={(e) => setForm(f => ({ ...f, port: parseInt(e.target.value) || 554 }))}
                />
              </label>
              <label className="flex flex-col gap-1">
                <span className="text-xs text-[var(--text-dim)]">Username</span>
                <input
                  type="text"
                  className="bg-[var(--bg-2)] border border-neutral-700 px-3 py-2 text-sm"
                  placeholder="admin"
                  value={form.username}
                  onChange={(e) => setForm(f => ({ ...f, username: e.target.value }))}
                />
              </label>
              <label className="flex flex-col gap-1">
                <span className="text-xs text-[var(--text-dim)]">Password</span>
                <input
                  type="password"
                  className="bg-[var(--bg-2)] border border-neutral-700 px-3 py-2 text-sm"
                  value={form.password}
                  onChange={(e) => setForm(f => ({ ...f, password: e.target.value }))}
                />
              </label>
            </div>

            <label className="flex flex-col gap-1">
              <span className="text-xs text-[var(--text-dim)]">RTSP URL</span>
              <div className="flex gap-2">
                <input
                  type="text"
                  className="flex-1 bg-[var(--bg-2)] border border-neutral-700 px-3 py-2 text-sm font-mono text-xs"
                  placeholder="rtsp://192.168.1.100:554/stream1"
                  value={form.rtsp_url}
                  onChange={(e) => setForm(f => ({ ...f, rtsp_url: e.target.value }))}
                />
                <button
                  type="button"
                  className="px-3 py-2 border border-neutral-700 bg-[var(--panel-2)] text-xs whitespace-nowrap"
                  onClick={() => setScanQr(true)}
                  title="Scan the QR from the OpenNVR Cam app"
                >
                  Scan QR
                </button>
              </div>
              <span className="text-[10px] text-[var(--text-dim)]">
                Optional. No need to put credentials in the URL — the
                Username/Password above are added automatically. Leave blank to
                build the URL from the IP + credentials.
              </span>
            </label>
          </div>
        )}

        {/* SELECT EXISTING MODE — the Modal body is the single scroller. */}
        {mode === 'select' && (
          <div className="space-y-3">
            <div className="relative">
              <Search size={16} className="absolute left-3 top-1/2 -translate-y-1/2 text-neutral-500" />
              <input
                type="text"
                className="w-full bg-[var(--bg-2)] border border-neutral-700 pl-10 pr-3 py-2 text-sm"
                placeholder="Search cameras..."
                value={searchQuery}
                onChange={(e) => setSearchQuery(e.target.value)}
              />
            </div>

            <div className="space-y-1">
              {filteredCameras.length === 0 ? (
                <div className="text-center text-sm text-[var(--text-dim)] py-8">
                  {existingCameras.length === 0
                    ? 'No cameras available. Add a new camera first.'
                    : 'No cameras match your search.'}
                </div>
              ) : (
                filteredCameras.map(camera => (
                  <button
                    key={camera.id}
                    className="w-full text-left px-3 py-2 bg-[var(--bg-2)] border border-neutral-700 hover:border-[var(--accent)] flex items-center gap-3 transition-colors"
                    onClick={() => handleSelectExisting(camera.id)}
                  >
                    <div className="w-8 h-8 bg-[var(--panel)] border border-neutral-600 flex items-center justify-center">
                      <Camera size={16} className="text-[var(--text-dim)]" />
                    </div>
                    <div>
                      <div className="text-sm font-medium">{camera.name}</div>
                      <div className="text-xs text-[var(--text-dim)]">Camera ID: {camera.id}</div>
                    </div>
                  </button>
                ))
              )}
            </div>
          </div>
        )}
      </div>

      {scanQr && (
        <QrScanner
          title="Scan the QR from the OpenNVR Cam app"
          onResult={(text) => { setForm(f => ({ ...f, rtsp_url: text })); setScanQr(false) }}
          onClose={() => setScanQr(false)}
        />
      )}
    </Modal>
  )
}
