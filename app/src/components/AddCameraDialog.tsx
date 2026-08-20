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
import { Badge, Button, EmptyState } from './ui'
import { Camera, ChevronDown, CheckCircle, Loader2, Plus, RefreshCw, Search, SearchX, Video, X } from 'lucide-react'

type DiscoveredCamera = { ip: string; scheme?: string; service_urls?: string[] }

// The device's ONVIF port lives inside its service URL. The server sends
// {ip, scheme, service_urls}; both the subtitle and the connect call need
// the real port (connect used to hardcode 80, breaking non-80 devices).
function devicePort(cam: DiscoveredCamera): number {
  const scheme = cam.scheme || 'http'
  try {
    const u = new URL(cam.service_urls?.[0] || '')
    return Number(u.port) || (u.protocol === 'https:' ? 443 : 80)
  } catch {
    return scheme === 'https' ? 443 : 80
  }
}

// "http · port 80" style subtitle for a discovered device.
function deviceSubtitle(cam: DiscoveredCamera): string {
  return `${cam.scheme || 'http'} · port ${devicePort(cam)}`
}

// IPv4 address → its /24, e.g. "192.168.0.106" → "192.168.0.0/24".
function subnet24(ip: string): string | null {
  const m = ip.match(/^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.\d{1,3}$/)
  return m ? `${m[1]}.${m[2]}.${m[3]}.0/24` : null
}

// The /connect 403 for an IP outside the configured Camera LAN. The other
// /connect 403 ("non-internal address") deliberately doesn't match — that
// one isn't fixable from here.
function parseLanBlock(e: any): { configured: string[] } | null {
  if (e?.status !== 403) return null
  const detail = typeof e?.data?.detail === 'string' ? e.data.detail : e?.message || ''
  const m = detail.match(/outside the configured Camera LAN subnet\(s\) \((.+)\)\./)
  if (!m) return null
  return { configured: m[1].split(',').map((s: string) => s.trim()).filter(Boolean) }
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

  // ONVIF discovery state. A selected camera keeps its parsed port so the
  // connect call targets the device's real ONVIF port. selectedCamera !== null
  // means we're on the connect step (no separate authStep flag).
  const [discovering, setDiscovering] = useState(false)
  const [discoveredCameras, setDiscoveredCameras] = useState<DiscoveredCamera[]>([])
  const [selectedCamera, setSelectedCamera] = useState<(DiscoveredCamera & {port: number}) | null>(null)

  // Scan range: one persistent toolbar. `configuredCidr` tracks the saved
  // Camera LAN range so the "Save as Camera LAN & scan" row only appears
  // when the input actually moves the boundary.
  const [scanInfo, setScanInfo] = useState<{cidrs: string[], source: string} | null>(null)
  // All of the host's detected LAN subnets (from /discover/plan, topped up by
  // scan responses) — always offered as one-click scan targets so the
  // operator can sweep a host network without knowing its range.
  const [hostCidrs, setHostCidrs] = useState<string[]>([])
  // The subnet the operator's own browser is on (from the server, which sees
  // the request's source address) — a live scan suggestion that can't go
  // stale, unlike the container's env-derived host hints.
  const [clientCidr, setClientCidr] = useState<string | null>(null)
  // No range configured or detectable (e.g. bridge container without a host
  // IP hint) — prompt for one instead of scanning nothing.
  const [noAutoRange, setNoAutoRange] = useState(false)
  const [rangeInput, setRangeInput] = useState('')
  const [configuredCidr, setConfiguredCidr] = useState<string | null>(null)
  // Scan-range combobox: the detected host networks live in a dropdown on the
  // range input itself, so picking a network and editing a range are the same
  // control instead of a separate button stack.
  const [rangeMenuOpen, setRangeMenuOpen] = useState(false)
  const [planRefreshing, setPlanRefreshing] = useState(false)
  const [savingRange, setSavingRange] = useState(false)
  const rangeBoxRef = useRef<HTMLDivElement | null>(null)
  const rangeInputRef = useRef<HTMLInputElement | null>(null)
  // ip → camera name for cameras already in the DB, so discovery results can
  // carry an "Already added" badge. Best-effort: fetch failure = no badges.
  const [addedByIp, setAddedByIp] = useState<Map<string, string>>(new Map())

  // Authentication step (shown while selectedCamera is set)
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

  // /connect 403 "outside the configured Camera LAN": offer a one-click
  // append of the camera's /24 to the LAN's scan_subnets, then retry.
  const [lanPrompt, setLanPrompt] = useState<{ ip: string; subnet: string; configured: string[] } | null>(null)
  const [lanPromptBusy, setLanPromptBusy] = useState(false)
  const [lanPromptError, setLanPromptError] = useState<string | null>(null)

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
      setHostCidrs(prev => Array.from(new Set([...prev, ...(data.suggested_cidrs || [])])))
      if (data.client_cidr) setClientCidr(data.client_cidr)
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

  // Persist the typed range into the Camera LAN's scan_subnets (leaving the
  // primary subnet untouched — the connect/add guards accept the union), then
  // rescan. Unlike a one-off scan this moves the boundary cameras can be
  // connected/added on. Same pattern as handleLanPromptFix.
  const handleSaveRangeAndScan = async () => {
    const cidrVal = rangeInput.trim()
    if (!cidrVal) return
    setSavingRange(true)
    setError(null)
    try {
      const cur = await apiService.getCameraLAN()
      const settings = cur?.data?.settings || {}
      const scanSubnets: string[] = Array.isArray(settings.scan_subnets) ? settings.scan_subnets : []
      if (!scanSubnets.includes(cidrVal)) {
        await apiService.updateCameraLAN({
          interface_name: settings.interface_name || 'eth0',
          scan_subnets: [...scanSubnets, cidrVal],
        })
      }
      await handleDiscover()
    } catch (e: any) {
      setError(e?.data?.detail || e?.message || 'Failed to update Camera LAN.')
    } finally {
      setSavingRange(false)
    }
  }

  // Re-fetch the scan plan — the "what networks am I on NOW?" call. The
  // server re-reads the launcher's host-IP hint file on every request and
  // derives the browser's own subnet from the connection, so this follows a
  // host that moved to another network without reopening the dialog. It
  // REPLACES the detected list (networks the host left drop out) but leaves
  // an in-flight scan's status untouched. Returns the plan's scan_cidrs, or
  // null when the endpoint is unavailable.
  const refreshPlan = async (): Promise<string[] | null> => {
    setPlanRefreshing(true)
    try {
      const res = await apiService.onvifDiscoverPlan()
      const data = res?.data || {}
      setHostCidrs(data.detected_cidrs || [])
      setClientCidr(data.client_cidr || null)
      const cidrs: string[] = data.scan_cidrs || []
      if (!discovering) {
        if (cidrs.length === 0) {
          setNoAutoRange(true)
        } else {
          setNoAutoRange(false)
          setScanInfo({ cidrs, source: data.source || 'configured' })
          if (data.source === 'configured' && cidrs[0]) setConfiguredCidr(cidrs[0])
        }
      }
      return cidrs
    } catch {
      return null
    } finally {
      setPlanRefreshing(false)
    }
  }

  // Start discovery on mount. Fetch the scan plan first (fast) so the target
  // range is visible while the scan itself (slow) is still running — and so a
  // "nothing to scan" answer becomes a prompt for a range instead of an error.
  useEffect(() => {
    if (mode !== 'discover') return
    let cancelled = false
    ;(async () => {
      const cidrs = await refreshPlan()
      if (cancelled) return
      // Empty plan → prompt for a range instead of scanning nothing.
      // null (plan endpoint unavailable) → just scan; the server resolves.
      if (cidrs && cidrs.length === 0) return
      handleDiscover()
    })()
    return () => { cancelled = true; scanAbortRef.current?.abort() }
  }, [])

  // Map of already-added camera IPs → names, to badge discovery results.
  // Purely advisory: on failure we just show no badges.
  useEffect(() => {
    let cancelled = false
    apiService.getCameras({ limit: 500 }).then((res: any) => {
      if (cancelled) return
      const map = new Map<string, string>()
      for (const c of res?.data?.cameras || []) {
        if (c?.ip_address) map.set(String(c.ip_address), String(c.name ?? ''))
      }
      setAddedByIp(map)
    }).catch(() => {})
    return () => { cancelled = true }
  }, [])

  // Keep the range input prefilled with the active range, but never clobber
  // what the operator typed.
  useEffect(() => {
    if (scanInfo?.cidrs[0] && !rangeInput) setRangeInput(scanInfo.cidrs[0])
  }, [scanInfo])

  // Close the range dropdown on any click outside it.
  useEffect(() => {
    if (!rangeMenuOpen) return
    const onDown = (e: MouseEvent) => {
      if (!rangeBoxRef.current?.contains(e.target as Node)) setRangeMenuOpen(false)
    }
    document.addEventListener('mousedown', onDown)
    return () => document.removeEventListener('mousedown', onDown)
  }, [rangeMenuOpen])

  // With nothing to scan, the range input is the only way forward — focus it.
  // (autoFocus can't do this: the input mounts before the plan resolves.)
  useEffect(() => {
    if (noAutoRange) rangeInputRef.current?.focus()
  }, [noAutoRange])

  // Select a discovered camera → connect step (keep the parsed ONVIF port).
  const handleSelectDiscovered = (camera: DiscoveredCamera) => {
    setSelectedCamera({ ...camera, port: devicePort(camera) })
    setError(null)
    setLanPrompt(null)
    setLanPromptError(null)
    setCameraName('') // Reset camera name for user input
  }

  // Leave the connect step back to the results list (results are kept).
  const resetConnectStep = () => {
    setSelectedCamera(null)
    setProfiles([])
    setSelectedProfile('')
    setRtspUrl('')
    setDeviceInfo(null)
    setCameraName('')
    setLanPrompt(null)
    setLanPromptError(null)
    setError(null)
  }

  // Changing credentials invalidates an established connection: clear the
  // connected state so the footer reverts to Connect (no stale profiles).
  const handleCredentialsChange = (patch: Partial<{username: string, password: string}>) => {
    setCredentials(c => ({ ...c, ...patch }))
    if (profiles.length > 0) {
      setProfiles([])
      setSelectedProfile('')
      setRtspUrl('')
      setDeviceInfo(null)
    }
  }

  // Authenticate and get RTSP URL using HTTP Digest (Hikvision compatible)
  const handleAuthenticate = async () => {
    if (!selectedCamera || !credentials.username || !credentials.password) {
      setError('Username and password are required')
      return
    }

    setAuthenticating(true)
    setError(null)
    setLanPrompt(null)
    setLanPromptError(null)

    try {
      // Use new onvifConnect endpoint which uses HTTP Digest auth
      // This works with Hikvision and other devices that don't support WS-Security
      const response = await apiService.onvifConnect(selectedCamera.ip, {
        username: credentials.username,
        password: credentials.password,
        port: selectedCamera.port,
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

        // Prefill the display name from device identity — but never clobber
        // a name the operator already typed.
        const di = data.device_info || {}
        const suggested = [di.manufacturer, di.model].filter(Boolean).join(' ') || selectedCamera.ip
        setCameraName(n => n || suggested)

        if (!firstWithUri.stream_uri) {
          setError('Could not get RTSP URL. Check camera settings.')
        }
      } else {
        setError('No stream profiles found. Check credentials.')
      }
    } catch (e: any) {
      const detail = e?.response?.data?.detail || e?.data?.detail || e?.message || ''
      const lanBlock = parseLanBlock(e)
      const subnet = lanBlock ? subnet24(selectedCamera.ip) : null
      if (lanBlock && subnet) {
        // Fixable in place: offer to extend the Camera LAN with this subnet.
        setLanPrompt({ ip: selectedCamera.ip, subnet, configured: lanBlock.configured })
      } else if (e?.status === 401 || detail.includes('401') || detail.toLowerCase().includes('authentication')) {
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

  // One-click fix for the LAN prompt: append the camera's subnet to the
  // Camera LAN's scan_subnets (leaving the primary subnet untouched — the
  // /connect guard accepts the union), then retry the connection.
  const handleLanPromptFix = async () => {
    if (!lanPrompt) return
    setLanPromptBusy(true)
    setLanPromptError(null)
    try {
      const cur = await apiService.getCameraLAN()
      const settings = cur?.data?.settings || {}
      const scanSubnets: string[] = Array.isArray(settings.scan_subnets) ? settings.scan_subnets : []
      if (!scanSubnets.includes(lanPrompt.subnet)) {
        await apiService.updateCameraLAN({
          interface_name: settings.interface_name || 'eth0',
          scan_subnets: [...scanSubnets, lanPrompt.subnet],
        })
      }
      setLanPrompt(null)
      await handleAuthenticate()
    } catch (e: any) {
      setLanPromptError(e?.data?.detail || e?.message || 'Failed to update Camera LAN.')
    } finally {
      setLanPromptBusy(false)
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

  // Dropdown options for the range input: the saved Camera LAN first, then
  // the browser's own network, then every other detected host network.
  const rangeOptions = Array.from(
    new Set([
      ...(configuredCidr ? [configuredCidr] : []),
      ...(clientCidr ? [clientCidr] : []),
      ...hostCidrs,
    ])
  )

  // Networks worth suggesting when a scan comes up empty: every known
  // network that wasn't part of the sweep.
  const suggestionCidrs = rangeOptions.filter(s => !(scanInfo?.cidrs || []).includes(s))

  // The toolbar button is Stop only while the running sweep still matches the
  // input; editing the input mid-scan flips it back to Scan so re-aiming is
  // one click (the server supersedes the old sweep). Enter always re-aims.
  const scanDirty =
    discovering && rangeInput.trim() !== '' && !(scanInfo?.cidrs || []).includes(rangeInput.trim())
  const showStop = discovering && !scanDirty

  // The typed range diverges from what a plain scan covers as "configured" —
  // offer to persist it into the Camera LAN (which is what gates connect/add).
  const saveMovesBoundary =
    !discovering &&
    !!configuredCidr &&
    rangeInput.trim() !== '' &&
    rangeInput.trim() !== configuredCidr &&
    !(scanInfo?.source === 'configured' && scanInfo.cidrs.includes(rangeInput.trim()))

  // One line under the toolbar; scan_cidrs from the server is the single
  // source of truth for what was actually swept.
  const statusText = discovering
    ? `Scanning ${scanInfo?.cidrs.join(', ') || '...'} — usually under a minute`
    : discoveredCameras.length > 0
    ? `Found ${discoveredCameras.length} camera${discoveredCameras.length === 1 ? '' : 's'}${scanInfo && scanInfo.cidrs.length > 0 ? ` in ${scanInfo.cidrs.join(', ')}` : ''}`
    : scanStopped
    ? 'Scan stopped.'
    : noAutoRange
    ? "Couldn't auto-detect your camera network — enter the subnet your cameras are on and press Scan."
    : scanInfo?.source === 'override'
    ? 'One-off range — the saved Camera LAN is unchanged.'
    : scanInfo?.source === 'auto-detected'
    ? 'Auto-detected from this host’s networks.'
    : scanInfo
    ? 'Default range from Camera LAN settings.'
    : ''

  // Connect step is derived, not tracked: a selected camera means we're on
  // it, and profiles arriving means the connection succeeded.
  const connected = profiles.length > 0

  // Single primary footer action per screen state, so the CTA never jumps
  // between body and footer.
  const primaryAction =
    mode === 'manual'
      ? {
          label: loading ? 'Adding...' : 'Add Camera',
          onClick: handleAddManualCamera,
          disabled: loading || !form.name.trim() || !form.ip_address.trim(),
        }
      : mode === 'discover' && selectedCamera && !connected
      ? {
          label: authenticating ? 'Connecting...' : 'Connect',
          onClick: handleAuthenticate,
          disabled: authenticating || !credentials.password || lanPromptBusy,
        }
      : mode === 'discover' && selectedCamera && connected
      ? {
          label: loading ? 'Adding...' : 'Add Camera',
          onClick: handleAddDiscoveredCamera,
          disabled: loading || !cameraName.trim() || !rtspUrl,
        }
      : null

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
      {/* Hidden while the duplicate prompt is open — its own Cancel /
          Add Anyway pair is the only decision on screen then (a second
          Cancel + Add Camera row underneath was confusing, and Add Camera
          would just re-trigger the same 409). */}
      {!duplicatePrompt && (
        <div className="flex items-center justify-end gap-2">
          <Button onClick={onClose}>Cancel</Button>
          {primaryAction && (
            <Button
              variant="primary"
              onClick={primaryAction.onClick}
              disabled={primaryAction.disabled}
            >
              {primaryAction.label}
            </Button>
          )}
        </div>
      )}
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
      <div className="flex border-b border-[var(--border)]" role="tablist">
        <button
          role="tab"
          aria-selected={mode === 'discover'}
          className={`flex-1 px-3 py-2 text-xs ${mode === 'discover' ? 'bg-[var(--accent)]/20 text-[var(--accent)] border-b-2 border-[var(--accent)]' : 'text-[var(--text-dim)] hover:bg-[var(--panel-2)]'}`}
          onClick={() => { setMode('discover'); resetConnectStep() }}
        >
          <Search size={12} className="inline mr-1" />
          Discover
        </button>
        <button
          role="tab"
          aria-selected={mode === 'manual'}
          className={`flex-1 px-3 py-2 text-xs ${mode === 'manual' ? 'bg-[var(--accent)]/20 text-[var(--accent)] border-b-2 border-[var(--accent)]' : 'text-[var(--text-dim)] hover:bg-[var(--panel-2)]'}`}
          onClick={() => { setMode('manual'); cancelScan(); setError(null) }}
        >
          <Plus size={12} className="inline mr-1" />
          Manual
        </button>
        {existingCameras.length > 0 && (
          <button
            role="tab"
            aria-selected={mode === 'select'}
            className={`flex-1 px-3 py-2 text-xs ${mode === 'select' ? 'bg-[var(--accent)]/20 text-[var(--accent)] border-b-2 border-[var(--accent)]' : 'text-[var(--text-dim)] hover:bg-[var(--panel-2)]'}`}
            onClick={() => { setMode('select'); cancelScan(); setError(null) }}
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

        {/* DISCOVER MODE — toolbar layout: one persistent scan bar (range
            combobox + Scan/Stop), a thin progress bar, a one-line status,
            and the results as the full-width single scroller. Controls never
            move between states. */}
        {mode === 'discover' && !selectedCamera && (
          <div className="flex flex-col gap-2 md:h-[420px]">
            {/* Scan bar — identical in every state. */}
            <div className="shrink-0">
              <div className="flex gap-2">
                  <div className="relative flex-1" ref={rangeBoxRef}>
                    <input
                      ref={rangeInputRef}
                      role="combobox"
                      aria-expanded={rangeMenuOpen}
                      aria-haspopup="listbox"
                      aria-controls="range-networks-listbox"
                      aria-label="Scan range (CIDR)"
                      className="w-full px-3 py-1.5 pr-8 text-sm font-mono bg-[var(--panel)] border border-[var(--border)] focus:border-[var(--accent)] outline-none"
                      placeholder="e.g. 192.168.1.0/24"
                      value={rangeInput}
                      onChange={(e) => setRangeInput(e.target.value)}
                      onKeyDown={(e) => {
                        if (e.key === 'Enter') {
                          e.preventDefault()
                          setRangeMenuOpen(false)
                          handleScanClick()
                        } else if (e.key === 'Escape' && rangeMenuOpen) {
                          // Close only the dropdown; without the menu open,
                          // Escape falls through and closes the modal.
                          e.preventDefault()
                          e.stopPropagation()
                          setRangeMenuOpen(false)
                        }
                      }}
                    />
                    {/* Opening the dropdown re-detects, so the list always
                        shows the CURRENT networks — a host that switched
                        WiFi mid-session is picked up right here. */}
                    <button
                      type="button"
                      className="absolute right-0 top-0 h-full px-2 text-[var(--text-dim)] hover:text-[var(--text)]"
                      onClick={() => {
                        const opening = !rangeMenuOpen
                        setRangeMenuOpen(opening)
                        if (opening) refreshPlan()
                      }}
                      aria-label="Choose a detected network"
                      title="Choose a detected network"
                    >
                      <ChevronDown size={14} className={`transition-transform ${rangeMenuOpen ? 'rotate-180' : ''}`} />
                    </button>
                    {rangeMenuOpen && (
                      <div
                        id="range-networks-listbox"
                        role="listbox"
                        className="absolute z-10 left-0 right-0 top-full mt-1 bg-[var(--panel)] border border-[var(--border)] shadow-lg"
                      >
                        <div className="flex items-center justify-between px-2 py-1 border-b border-[var(--border)]">
                          <span className="text-[10px] uppercase tracking-wide text-[var(--text-dim)]">Detected networks</span>
                          <button
                            type="button"
                            className="p-0.5 text-[var(--text-dim)] hover:text-[var(--text)] disabled:opacity-50"
                            onClick={() => refreshPlan()}
                            disabled={planRefreshing}
                            aria-label="Re-detect networks"
                            title="Re-detect the current networks"
                          >
                            <RefreshCw size={12} className={planRefreshing ? 'animate-spin' : ''} />
                          </button>
                        </div>
                        {rangeOptions.length === 0 && (
                          <div className="px-2 py-2 text-xs text-[var(--text-dim)]">
                            {planRefreshing ? (
                              'Detecting networks...'
                            ) : (
                              <>
                                No networks detected. Type a range above, or run{' '}
                                <code className="font-mono">start.sh refresh-net</code> (or{' '}
                                <code className="font-mono">start.ps1 refresh-net</code>) on the
                                host, then press <RefreshCw size={9} className="inline align-baseline" />.
                              </>
                            )}
                          </div>
                        )}
                        {rangeOptions.map((s) => (
                          <button
                            key={s}
                            type="button"
                            role="option"
                            aria-selected={s === rangeInput.trim()}
                            className="w-full flex items-center justify-between gap-2 px-2 py-1.5 text-xs text-left hover:bg-[var(--accent)]/15"
                            onClick={() => {
                              setRangeMenuOpen(false)
                              setRangeInput(s)
                              if (s === configuredCidr) handleDiscover()
                              else handleDiscover(s)
                            }}
                          >
                            <span className="font-mono">{s}</span>
                            <span className="text-[10px] text-[var(--text-dim)] whitespace-nowrap">
                              {s === configuredCidr ? 'Camera LAN' : s === clientCidr ? 'your network' : 'host network'}
                            </span>
                          </button>
                        ))}
                        {/* Recovery steps for "my network isn't listed" —
                            the one place the operator is guaranteed to be
                            looking when that happens. */}
                        {rangeOptions.length > 0 && (
                          <div className="px-2 py-1.5 border-t border-[var(--border)] text-[10px] leading-relaxed text-[var(--text-dim)]">
                            Don't see your network? Type its range above (e.g.
                            192.168.1.0/24) and Scan. If the host moved to a new
                            network, run <code className="font-mono">start.sh refresh-net</code> (or{' '}
                            <code className="font-mono">start.ps1 refresh-net</code>) on it,
                            then press <RefreshCw size={9} className="inline align-baseline" />.
                          </div>
                        )}
                      </div>
                    )}
                  </div>
                  {/* Stop while the sweep matches the input; a dirty input
                      flips it back to Scan so re-aiming is one click. */}
                  <Button
                    variant={showStop ? 'danger' : 'primary'}
                    className="shrink-0 min-w-[88px] justify-center"
                    onClick={() => (showStop ? cancelScan() : handleScanClick())}
                    disabled={!discovering && noAutoRange && !rangeInput.trim()}
                    aria-label={showStop ? 'Stop scan' : 'Scan'}
                  >
                    {showStop ? (
                      <>
                        <X size={14} />
                        Stop
                      </>
                    ) : (
                      <>
                        <Search size={14} />
                        Scan
                      </>
                    )}
                  </Button>
              </div>
              {/* Always-rendered 3px slot — the bar appears and disappears
                  with zero layout shift. */}
              <div className="h-[3px] mt-1.5" aria-hidden={!discovering}>
                {discovering && <div className="progress-indeterminate h-full" />}
              </div>
            </div>

            {/* Status line. */}
            <div className="shrink-0 min-h-[18px] flex items-center text-xs text-[var(--text-dim)]" aria-live="polite">
              <span className={`truncate${noAutoRange && !discovering ? ' text-[var(--text)]' : ''}`}>{statusText}</span>
            </div>

            {/* The typed range diverges from the saved Camera LAN — offer to
                persist it (that boundary gates which cameras can be added). */}
            {saveMovesBoundary && (
              <div className="shrink-0 flex items-center gap-2 rounded border border-[var(--border)] bg-[var(--panel-2)]/40 px-3 py-2 text-xs text-[var(--text-dim)]">
                <span className="flex-1 min-w-0 truncate">
                  Scans of <span className="font-mono">{rangeInput.trim()}</span> are one-off — the saved
                  Camera LAN (<span className="font-mono">{configuredCidr}</span>) is unchanged.
                </span>
                <Button
                  className="text-xs px-2 py-1 shrink-0"
                  onClick={handleSaveRangeAndScan}
                  disabled={savingRange}
                >
                  {savingRange && <Loader2 size={12} className="animate-spin" />}
                  Save as Camera LAN & scan
                </Button>
              </div>
            )}

            {/* Results — full width, the single scroller. */}
            <div className="flex-1 min-h-0 md:overflow-auto thin-scroll space-y-2">
              {/* The wait is a teachable moment: the three things that decide
                  whether a camera will appear at all, plus the escape hatch
                  for operators who already know the IP. (Results are always
                  empty mid-scan — handleDiscover clears them.) */}
              {discovering && (
                <div className="rounded border border-[var(--border)] bg-[var(--panel-2)]/40 p-3 space-y-2">
                  <div className="text-[10px] uppercase tracking-wide text-[var(--text-dim)]">While it scans</div>
                  <ul className="text-xs text-[var(--text-dim)] space-y-1 list-disc pl-4">
                    <li>Cameras must be powered on and connected to one of the scanned networks.</li>
                    <li>ONVIF must be enabled on the camera — usually under Settings → Network → ONVIF.</li>
                    <li>Camera on a different subnet? Change the range above and press Scan — the sweep re-aims instantly.</li>
                  </ul>
                  <div className="flex items-center justify-between gap-2 pt-0.5">
                    <span className="text-xs text-[var(--text-dim)]">Already know the camera's IP?</span>
                    <Button
                      variant="ghost"
                      className="text-xs px-2 py-1"
                      onClick={() => { setMode('manual'); cancelScan(); setError(null) }}
                    >
                      Add manually
                    </Button>
                  </div>
                </div>
              )}

              {!discovering && discoveredCameras.length === 0 && (
                noAutoRange ? (
                  <EmptyState
                    icon={<SearchX size={28} />}
                    title="No network range to scan"
                    description="Type the subnet your cameras are on in the bar above (e.g. 192.168.1.0/24), then press Scan."
                  />
                ) : scanStopped ? (
                  <EmptyState
                    icon={<SearchX size={28} />}
                    title="Scan stopped"
                    description="Adjust the range above, or scan again."
                    action={
                      <Button onClick={handleScanClick}>
                        <Search size={14} />
                        Rescan
                      </Button>
                    }
                  />
                ) : (
                  <EmptyState
                    icon={<SearchX size={28} />}
                    title={`No cameras found${scanInfo && scanInfo.cidrs.length > 0 ? ` in ${scanInfo.cidrs.join(', ')}` : ''}`}
                    description={
                      suggestionCidrs.length > 0
                        ? 'Try scanning another detected network:'
                        : 'Check the scan range above, or use Manual entry.'
                    }
                    action={
                      <div className="space-y-3">
                        <div className="flex flex-wrap items-center justify-center gap-2">
                          {suggestionCidrs.map((s) => (
                            <Button
                              key={s}
                              onClick={() => {
                                setRangeInput(s)
                                if (s === configuredCidr) handleDiscover()
                                else handleDiscover(s)
                              }}
                            >
                              <Search size={12} />
                              <span className="font-mono text-xs">{s}</span>
                              {s === clientCidr && (
                                <span className="text-[10px] text-[var(--text-dim)]">your network</span>
                              )}
                            </Button>
                          ))}
                          <Button variant="ghost" onClick={() => { setMode('manual'); setError(null) }}>
                            Add manually
                          </Button>
                        </div>
                        {/* Visible without opening the dropdown — the dropdown
                            repeats this for people already looking there. */}
                        <div className="text-[10px] leading-relaxed text-[var(--text-dim)] max-w-md mx-auto">
                          Your network missing here? Run{' '}
                          <code className="font-mono">start.sh refresh-net</code> (or{' '}
                          <code className="font-mono">start.ps1 refresh-net</code>) on the host,
                          then press <RefreshCw size={9} className="inline align-baseline" /> in the
                          scan-range dropdown above.
                        </div>
                      </div>
                    }
                  />
                )
              )}

              {discoveredCameras.map((camera) => (
                <button
                  key={camera.ip}
                  className="w-full flex items-center justify-between gap-4 rounded border border-[var(--border)] bg-[var(--panel-2)]/40 px-4 py-3 text-left hover:bg-[var(--panel-2)] transition-colors"
                  onClick={() => handleSelectDiscovered(camera)}
                >
                  <div className="flex items-center gap-3 min-w-0">
                    <Camera size={20} className="text-[var(--accent)] shrink-0" />
                    <div className="min-w-0">
                      <div className="text-sm font-medium flex items-center gap-2">
                        <span className="font-mono">{camera.ip}</span>
                        {addedByIp.has(camera.ip) && <Badge variant="info">Already added</Badge>}
                      </div>
                      <div className="text-xs text-[var(--text-dim)]">{deviceSubtitle(camera)}</div>
                    </div>
                  </div>
                  <ChevronDown size={16} className="text-[var(--text-dim)] -rotate-90 shrink-0" />
                </button>
              ))}
            </div>
          </div>
        )}

        {/* DISCOVER MODE — CONNECT STEP. Credentials first; the Name and
            stream details appear only after a successful connect. The single
            primary CTA lives in the footer (Connect → Add Camera). */}
        {mode === 'discover' && selectedCamera && (
          <div className="space-y-4">
            <button
              className="text-xs text-[var(--accent)] flex items-center gap-1 hover:underline"
              onClick={resetConnectStep}
            >
              ← Back to camera list
            </button>

            <div className="p-3 bg-[var(--bg-2)] border border-neutral-700">
              <div className="flex items-center gap-3">
                <Camera size={24} className="text-[var(--accent)]" />
                <div>
                  <div className="font-medium flex items-center gap-2">
                    ONVIF Camera
                    {addedByIp.has(selectedCamera.ip) && <Badge variant="info">Already added</Badge>}
                  </div>
                  <div className="text-xs text-[var(--text-dim)]">
                    {selectedCamera.ip} · {deviceSubtitle(selectedCamera)}
                  </div>
                </div>
              </div>
            </div>

            <div className="grid grid-cols-2 gap-3">
              <label className="flex flex-col gap-1">
                <span className="text-xs text-[var(--text-dim)]">Username</span>
                <input
                  type="text"
                  className="bg-[var(--bg-2)] border border-neutral-700 px-3 py-2 text-sm"
                  value={credentials.username}
                  onChange={(e) => handleCredentialsChange({ username: e.target.value })}
                />
              </label>
              <label className="flex flex-col gap-1">
                <span className="text-xs text-[var(--text-dim)]">Password</span>
                <input
                  type="password"
                  className="bg-[var(--bg-2)] border border-neutral-700 px-3 py-2 text-sm"
                  autoFocus
                  value={credentials.password}
                  onChange={(e) => handleCredentialsChange({ password: e.target.value })}
                  onKeyDown={(e) => {
                    if (e.key === 'Enter' && !connected && credentials.password && !authenticating) {
                      e.preventDefault()
                      handleAuthenticate()
                    }
                  }}
                />
              </label>
            </div>

            {/* Camera LAN block from /connect — fixable in place. */}
            {lanPrompt && (
              <div className="p-3 border border-amber-600 bg-amber-950/40 text-sm space-y-2">
                <div>
                  This camera ({lanPrompt.ip}) is outside the configured Camera LAN
                  {lanPrompt.configured.length > 0 && (
                    <> (<span className="font-mono">{lanPrompt.configured.join(', ')}</span>)</>
                  )}
                  . The Camera LAN controls which networks cameras can be connected on.
                </div>
                {lanPromptError && <div className="text-xs text-red-400">{lanPromptError}</div>}
                <div className="flex justify-end gap-2">
                  <Button onClick={() => { setLanPrompt(null); setLanPromptError(null) }} disabled={lanPromptBusy}>
                    Cancel
                  </Button>
                  <button
                    className="px-3 py-1 text-sm bg-amber-600 text-white disabled:opacity-50 inline-flex items-center gap-2"
                    onClick={handleLanPromptFix}
                    disabled={lanPromptBusy}
                  >
                    {lanPromptBusy && <Loader2 size={12} className="animate-spin" />}
                    Add {lanPrompt.subnet} to Camera LAN & retry
                  </button>
                </div>
              </div>
            )}

            {connected && (
              <>
                <div className="p-2.5 bg-green-900/20 border border-green-700">
                  <div className="flex items-center gap-2 text-green-400 text-sm">
                    <CheckCircle size={14} />
                    Connected{rtspUrl ? ' — stream found' : ''}
                  </div>
                  {rtspUrl && (
                    <details className="mt-1">
                      <summary className="text-xs text-[var(--text-dim)] cursor-pointer">Stream URL</summary>
                      <div className="text-xs font-mono text-[var(--text-dim)] break-all mt-1">{rtspUrl}</div>
                    </details>
                  )}
                </div>

                <label className="flex flex-col gap-1">
                  <span className="text-xs text-[var(--text-dim)]">Camera Name *</span>
                  <input
                    type="text"
                    className="bg-[var(--bg-2)] border border-neutral-700 px-3 py-2 text-sm"
                    placeholder="e.g., Front Door, Lobby"
                    autoFocus
                    value={cameraName}
                    onChange={(e) => setCameraName(e.target.value)}
                    onKeyDown={(e) => {
                      if (e.key === 'Enter' && cameraName.trim() && rtspUrl && !loading) {
                        e.preventDefault()
                        handleAddDiscoveredCamera()
                      }
                    }}
                  />
                </label>

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

                {deviceInfo && (
                  <details className="p-3 bg-[var(--bg-2)] border border-neutral-700 text-xs">
                    <summary className="font-medium text-sm cursor-pointer">
                      {[deviceInfo.manufacturer, deviceInfo.model].filter(Boolean).join(' ') || 'Device Information'}
                    </summary>
                    <div className="grid grid-cols-2 gap-x-4 gap-y-1 text-[var(--text-dim)] mt-2">
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
                  </details>
                )}
              </>
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
