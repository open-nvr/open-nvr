/**
 * Copyright (c) 2026 OpenNVR
 * SPDX-License-Identifier: AGPL-3.0-or-later
 *
 * Sites: cameras at other locations, relayed to this server by a small agent
 * at each site (issue #925, cloud ingest). The agent dials out over 443, so
 * the site needs no port forwarded; it is enrolled once with a one-time code.
 */

import { useEffect, useRef, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { toDataURL } from 'qrcode'
import { AlertTriangle, Check, CheckCircle2, Copy, Gauge, Globe, KeyRound, Link2, Loader2, Plus, RefreshCw, Settings, Share2, ShieldAlert, Stethoscope, Trash2, Unplug, Video, XCircle } from 'lucide-react'
import { useTranslation } from '../i18n'
import { useConfirm } from '../components/ui/ConfirmDialog'
import { useSnackbar } from '../components/Snackbar'
import { usePermissions } from '../hooks/usePermissions'
import { extractApiError } from '../lib/apiError'
import { queryClient } from '../lib/queries'
import { Badge, Button, EmptyState } from '../components/ui'
import { Field, Input, Select } from '../components/ui/form'
import { PageTitleBar } from '../components/ui/layout'
import { Modal } from '../components/Modal'
import { SegmentedControl } from '../components/ui/SegmentedControl'
import {
  AGENT_DOWNLOADS, cameraRelayState, siteService, siteStatus, siteUplinkKbps, suggestedRtspUrl,
  type DiscoveredDevice, type EnrolCode, type SetupLink, type Site,
} from '../services/siteService'
import { AddCamerasDialog } from './SiteAddCameras'

const SITES_KEY = ['sites']

function CopyBox({ text, label }: { text: string; label: string }) {
  const [done, setDone] = useState(false)
  return (
    <div className="flex items-start gap-2">
      <code className="flex-1 break-all text-xs bg-[var(--bg)] border border-[var(--border)] px-2 py-1.5 font-mono">{text}</code>
      <Button
        size="sm"
        variant="outline"
        title={`Copy ${label}`}
        onClick={() => {
          void navigator.clipboard?.writeText(text).then(() => {
            setDone(true)
            window.setTimeout(() => setDone(false), 1500)
          })
        }}
      >
        {done ? <Check size={14} /> : <Copy size={14} />}
      </Button>
    </div>
  )
}

const LINK_HOURS = [
  { value: 1, label: '1 hour' },
  { value: 24, label: '1 day' },
  { value: 72, label: '3 days' },
  { value: 168, label: '7 days' },
]

/** The setup link: one link for the person at the site, instead of a code and
 *  a fingerprint (services/site_setup.py). Made when the dialog opens. */
function SetupLinkPanel({ site }: { site: Site }) {
  const { showError } = useSnackbar()
  const { data: status } = useQuery({ queryKey: ['site-setup-status'], queryFn: () => siteService.setupStatus() })
  const [hours, setHours] = useState(72)
  const [link, setLink] = useState<SetupLink | null>(null)
  const [qr, setQr] = useState<string>('')
  const made = useRef<string | null>(null)
  const started = useRef(false)

  const make = async (h: number) => {
    try {
      const fresh = await siteService.newSetupLink(site.id, h)
      // One usable link at a time from this dialog: the previous one goes.
      if (made.current) await siteService.revokeSetupLink(site.id, made.current).catch(() => {})
      made.current = fresh.token_id
      setLink(fresh)
      void queryClient.invalidateQueries({ queryKey: ['site-setup-links', site.id] })
    } catch (e) {
      showError(extractApiError(e, 'Could not make a setup link'))
    }
  }
  useEffect(() => {
    // Once per dialog (React may run this effect twice in development).
    if (status?.ready && !started.current) {
      started.current = true
      void make(hours)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [status?.ready])
  useEffect(() => {
    if (link?.url) void toDataURL(link.url, { margin: 1, width: 180 }).then(setQr).catch(() => setQr(''))
  }, [link?.url])

  if (!status) return null
  if (!status.ready) {
    return (
      <p className="text-[var(--text-dim)]">
        <Link2 size={13} className="inline mr-1" />
        A setup link (one link instead of these commands) needs this server to have a public, trusted
        certificate: {status.detail}
      </p>
    )
  }
  if (!link?.url) return <p className="text-[var(--text-dim)]">Making a setup link…</p>
  const share = typeof navigator.share === 'function'
  return (
    <div className="space-y-2 border border-[var(--border)] p-3 bg-[var(--panel-2)]">
      <p className="font-medium">Send this link to the person at the site</p>
      <p className="text-[var(--text-dim)]">
        It opens a page that shows them what to download and run on a machine there. Nothing else to type.
      </p>
      <CopyBox text={link.url} label="the setup link" />
      <div className="flex flex-wrap items-center gap-3">
        {qr && <img src={qr} alt="QR code of the setup link" width={120} height={120} />}
        <div className="space-y-2">
          {share && (
            <Button size="sm" variant="outline"
              onClick={() => { void navigator.share({ title: `Set up ${site.name}`, url: link.url }).catch(() => {}) }}>
              <Share2 size={14} /> Share
            </Button>
          )}
          <div className="flex items-center gap-2 text-xs">
            <span>Valid for</span>
            <Select value={String(hours)} onChange={(e) => { const h = Number(e.target.value); setHours(h); void make(h) }}
              aria-label="How long the link works">
              {LINK_HOURS.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
            </Select>
          </div>
          <p className="text-xs text-[var(--text-dim)]">
            Works once, until {new Date(link.expires_at).toLocaleString()}. Anyone with the link can set up
            this site, so send it only to that person.
          </p>
        </div>
      </div>
    </div>
  )
}

/** Setup links of a site: who used one, and the ones still open. */
function SetupLinks({ site }: { site: Site }) {
  const { showError } = useSnackbar()
  const { data: links = [] } = useQuery({
    queryKey: ['site-setup-links', site.id],
    queryFn: () => siteService.setupLinks(site.id),
    refetchInterval: 15000,
  })
  const shown = links.filter((l) => l.state === 'active' || l.state === 'used').slice(0, 4)
  if (shown.length === 0) return null
  return (
    <ul className="text-xs text-[var(--text-dim)] space-y-1">
      {shown.map((l) => (
        <li key={l.token_id} className="flex items-center gap-2">
          <Link2 size={12} />
          {l.state === 'used' ? (
            <span>Setup link used {l.used_at ? new Date(l.used_at).toLocaleString() : ''}
              {l.used_hostname ? ` by ${l.used_hostname}` : ''}{l.used_ip ? ` from ${l.used_ip}` : ''}</span>
          ) : (
            <>
              <span>Setup link open until {new Date(l.expires_at).toLocaleString()}</span>
              <Button size="sm" variant="ghost" title="This link stops working"
                onClick={async () => {
                  try {
                    await siteService.revokeSetupLink(site.id, l.token_id)
                    void queryClient.invalidateQueries({ queryKey: ['site-setup-links', site.id] })
                  } catch (e) { showError(extractApiError(e, 'Could not revoke the link')) }
                }}>
                Revoke
              </Button>
            </>
          )}
        </li>
      ))}
    </ul>
  )
}

function EnrolDialog({ site, code, onClose }: { site: Site; code: EnrolCode; onClose: () => void }) {
  const expires = new Date(code.enrol_expires_at)
  const setups = code.setup ?? []
  const [os, setOs] = useState<string>(() =>
    /windows/i.test(navigator.userAgent) ? 'windows' : 'linux')
  const setup = setups.find((s) => s.os === os) ?? setups[0]
  // Same origin as this page: the SPA is served by the server it talks to.
  const base = '/api/v1/site-agent/download/'
  return (
    <Modal open title={`Set up the agent at "${site.name}"`} onClose={onClose}>
      <div className="space-y-3 text-sm">
        <SetupLinkPanel site={site} />
        <p className="font-medium">Or set it up yourself</p>
        <p>
          On a machine at the site that can reach its cameras (a small Linux box, a Raspberry Pi, a
          Windows PC):
        </p>
        {setup ? (
          <>
            <SegmentedControl
              label="Operating system of the site machine"
              options={setups.map((s) => ({ value: s.os, label: s.label }))}
              value={setup.os}
              onChange={setOs}
            />
            <p className="font-medium">1. Download the agent from this server</p>
            <CopyBox text={setup.download} label="the download command" />
            <p className="font-medium">2. Enrol it</p>
            <CopyBox text={setup.enrol} label="the enrol command" />
            <p className="font-medium">3. Start it</p>
            <CopyBox text={setup.run} label="the start command" />
          </>
        ) : (
          <CopyBox text={code.enrol_command} label="the enrol command" />
        )}
        <p className="text-[var(--text-dim)]">
          The code <b>{code.enrol_code}</b> works once, until {expires.toLocaleTimeString()}. To keep the
          agent running after a reboot, install it as a service (see the docs). It only dials out, on
          port 443: the site needs no port forwarded.
        </p>
        <p className="text-[var(--text-dim)]">
          Or download it here and copy it over:{' '}
          {AGENT_DOWNLOADS.map((d, i) => (
            <span key={d.platform}>
              {i > 0 && ' · '}
              <a className="underline" href={base + d.platform} download>{d.label}</a>
            </span>
          ))}
          . Releases on GitHub carry the same builds.
        </p>
        {site.server_pin && (
          <p className="text-[var(--text-dim)]">
            The pin ({site.server_pin.slice(0, 18)}…) is this server's certificate fingerprint. It lets
            the download and the agent trust this server even with its self-signed certificate.
          </p>
        )}
      </div>
    </Modal>
  )
}

function AddCameraDialog({ site, device, onClose }: { site: Site; device: DiscoveredDevice | null; onClose: () => void }) {
  const { showSuccess, showError } = useSnackbar()
  const [name, setName] = useState(device?.name || '')
  const [mainUrl, setMainUrl] = useState(device ? suggestedRtspUrl(device) : 'rtsp://')
  const [subUrl, setSubUrl] = useState('')
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [controlHost, setControlHost] = useState(device?.ip || '')
  const [busy, setBusy] = useState(false)

  const submit = async () => {
    setBusy(true)
    try {
      await siteService.addCamera(site.id, name.trim(), {
        main_url: mainUrl.trim(),
        sub_url: subUrl.trim() || null,
        username: username.trim() || null,
        password: password || null,
        control_host: controlHost.trim() || null,
      })
      showSuccess(`${name} added; the site's agent starts relaying it within seconds`)
      void queryClient.invalidateQueries({ queryKey: SITES_KEY })
      void queryClient.invalidateQueries({ queryKey: ['cameras'] })
      onClose()
    } catch (e) {
      showError(extractApiError(e, 'Could not add the camera'))
    } finally {
      setBusy(false)
    }
  }

  return (
    <Modal
      open
      title={`Add a camera at "${site.name}"`}
      onClose={onClose}
      footer={
        <div className="flex justify-end gap-2">
          <Button variant="outline" onClick={onClose}>Cancel</Button>
          <Button variant="primary" disabled={busy || !name.trim() || mainUrl.trim().length < 10} onClick={submit}>Add camera</Button>
        </div>
      }
    >
      <div className="space-y-3">
        {device && (
          <p className="text-xs text-[var(--text-dim)]">
            Found by the site's agent at {device.ip}{device.hardware ? ` (${device.hardware})` : ''}. Check the stream
            path in the camera's manual or web page.
          </p>
        )}
        <Field label="Name" required>
          <Input value={name} onChange={(e) => setName(e.target.value)} placeholder="Gate" />
        </Field>
        <Field label="Main stream URL on the site's network" hint="rtsp://… as the camera serves it at the site. No user or password in the URL: use the fields below." required>
          <Input value={mainUrl} onChange={(e) => setMainUrl(e.target.value)} placeholder="rtsp://192.168.1.64:554/Streaming/Channels/101" />
        </Field>
        <Field label="Sub stream URL (optional)" hint="A smaller stream, for the live grid and detection. Saves uplink.">
          <Input value={subUrl} onChange={(e) => setSubUrl(e.target.value)} placeholder="rtsp://192.168.1.64:554/Streaming/Channels/102" />
        </Field>
        <div className="grid grid-cols-2 gap-2">
          <Field label="Camera user">
            <Input value={username} onChange={(e) => setUsername(e.target.value)} autoComplete="off" />
          </Field>
          <Field label="Camera password" hint="Stored encrypted; sent only to this site's agent.">
            <Input type="password" value={password} onChange={(e) => setPassword(e.target.value)} autoComplete="new-password" />
          </Field>
        </div>
        <Field label="Control address (optional)" hint="Where the camera answers ONVIF and PTZ, if not the stream's address (an NVR channel, an encoder). Leave empty to use the stream's.">
          <Input value={controlHost} onChange={(e) => setControlHost(e.target.value)} placeholder="192.168.1.64" />
        </Field>
      </div>
    </Modal>
  )
}

function UplinkDialog({ site, onClose }: { site: Site; onClose: () => void }) {
  const { showSuccess, showError } = useSnackbar()
  const [policy, setPolicy] = useState<'always' | 'sub_only'>(site.live_policy === 'sub_only' ? 'sub_only' : 'always')
  const [cap, setCap] = useState(site.uplink_cap_kbps ? String(site.uplink_cap_kbps) : '')
  const noSub = site.cameras.filter((c) => !c.sub_url).length
  const test = site.uplink_test
  const testUplink = async () => {
    try {
      await siteService.uplinkTest(site.id)
      void queryClient.invalidateQueries({ queryKey: SITES_KEY })
    } catch (e) {
      showError(extractApiError(e, 'Could not start the test'))
    }
  }
  const save = async () => {
    try {
      await siteService.update(site.id, { live_policy: policy, uplink_cap_kbps: cap.trim() ? Number(cap) : null })
      showSuccess('Uplink settings saved; the site follows within seconds')
      void queryClient.invalidateQueries({ queryKey: SITES_KEY })
      onClose()
    } catch (e) {
      showError(extractApiError(e, 'Could not save'))
    }
  }
  return (
    <Modal open title={`Uplink for "${site.name}"`} onClose={onClose}
      footer={
        <div className="flex justify-end gap-2">
          <Button variant="outline" onClick={onClose}>Cancel</Button>
          <Button variant="primary" disabled={!!cap.trim() && !(Number(cap) >= 0)} onClick={save}>Save</Button>
        </div>
      }>
      <div className="space-y-3 text-sm">
        <div className="flex flex-wrap items-center gap-2">
          <span>
            Free upload: {test?.pending ? 'testing…' : test?.kbps != null
              ? <b>{(test.kbps / 1000).toFixed(1)} Mbit/s</b> : 'not tested yet'}
            {test?.at && !test.pending ? <span className="text-[var(--text-dim)]"> ({new Date(test.at).toLocaleString()})</span> : null}
          </span>
          <span className="flex-1" />
          <Button size="sm" variant="outline" disabled={!site.online || !!test?.pending} onClick={testUplink}
            title="The site's agent uploads throwaway data for 5 seconds (at most 10 MB, once an hour)">
            Test the uplink
          </Button>
        </div>
        <Field label="What the site sends" hint={policy === 'sub_only'
          ? `Each camera's sub stream only, into its main path: live view and recording at sub-stream quality.${noSub ? ` ${noSub} camera(s) without a sub stream keep sending their main stream.` : ''}`
          : "Each camera's main stream, and its sub stream when it has one."}>
          <Select value={policy} onChange={(e) => setPolicy(e.target.value as 'always' | 'sub_only')}>
            <option value="always">Main and sub streams (full quality)</option>
            <option value="sub_only">Sub streams only (thin uplink)</option>
          </Select>
        </Field>
        <Field label="Uplink cap (kbps, optional)" hint="Over this, the agent pauses main streams (newest camera first) and brings them back when there is room. Sub streams are never paused. Empty: no cap.">
          <Input inputMode="numeric" value={cap} onChange={(e) => setCap(e.target.value.replace(/[^0-9]/g, ''))} placeholder="e.g. 8000" />
        </Field>
      </div>
    </Modal>
  )
}

/** "Run checks": the site's agent tests, from the site, the server, the
 *  video route and each camera, and the answers are shown as they come. */
function ChecksDialog({ siteId, onClose }: { siteId: number; onClose: () => void }) {
  const { data: sites = [] } = useQuery({ queryKey: SITES_KEY, queryFn: siteService.list, refetchInterval: 2000 })
  const site = sites.find((s) => s.id === siteId)
  const checks = site?.checks
  return (
    <Modal open title={`Checks from "${site?.name ?? ''}"`} onClose={onClose}
      footer={<div className="flex justify-end"><Button variant="outline" onClick={onClose}>Close</Button></div>}>
      <div className="space-y-2 text-sm">
        {!checks || checks.pending ? (
          <p className="flex items-center gap-2"><Loader2 size={14} className="animate-spin" /> The site's agent is running the checks…</p>
        ) : (
          <>
            <ul className="divide-y divide-[var(--border)] border border-[var(--border)]">
              {checks.items.map((it, i) => (
                <li key={i} className="flex items-start gap-2 px-2 py-1.5">
                  {it.ok ? <CheckCircle2 size={15} className="text-[var(--ok)] mt-0.5" /> : <XCircle size={15} className="text-[var(--danger)] mt-0.5" />}
                  <div className="flex-1">
                    <div className="font-medium">{it.name}</div>
                    {it.detail && <div className="text-xs text-[var(--text-dim)] break-all">{it.detail}</div>}
                  </div>
                  {it.ms != null && <span className="text-xs text-[var(--text-dim)]">{it.ms} ms</span>}
                </li>
              ))}
            </ul>
            {checks.at && <p className="text-xs text-[var(--text-dim)]">Run {new Date(checks.at * 1000).toLocaleString()}.</p>}
          </>
        )}
      </div>
    </Modal>
  )
}

/** Per-site switches: its alerts, and its agent's self-update. */
function SiteSettingsDialog({ site, onClose }: { site: Site; onClose: () => void }) {
  const { showSuccess, showError } = useSnackbar()
  const [offline, setOffline] = useState(site.alert_offline !== false)
  const [down, setDown] = useState(site.alert_camera_down !== false)
  const [updates, setUpdates] = useState(site.agent_updates !== false)
  const save = async () => {
    try {
      await siteService.update(site.id, { alert_offline: offline, alert_camera_down: down, agent_updates: updates })
      showSuccess('Saved')
      void queryClient.invalidateQueries({ queryKey: SITES_KEY })
      onClose()
    } catch (e) {
      showError(extractApiError(e, 'Could not save'))
    }
  }
  const row = (checked: boolean, set: (v: boolean) => void, title: string, hint: string) => (
    <label className="flex items-start gap-2">
      <input type="checkbox" className="mt-1" checked={checked} onChange={(e) => set(e.target.checked)} />
      <span><span className="font-medium">{title}</span><br /><span className="text-xs text-[var(--text-dim)]">{hint}</span></span>
    </label>
  )
  return (
    <Modal open title={`Settings for "${site.name}"`} onClose={onClose}
      footer={<div className="flex justify-end gap-2"><Button variant="outline" onClick={onClose}>Cancel</Button><Button variant="primary" onClick={save}>Save</Button></div>}>
      <div className="space-y-3 text-sm">
        {row(offline, setOffline, 'Alert when the site is offline', 'Its agent has not checked in for 5 minutes (power, internet or the machine is down).')}
        {row(down, setDown, 'Alert when one of its cameras is down', 'A camera of this site has not been relayed for 10 minutes, with the reason.')}
        {row(updates, setUpdates, 'Update the agent automatically', 'To builds the OpenNVR release signed, when this server ships a newer one. Off: the agent stays as it is until it is set up again.')}
      </div>
    </Modal>
  )
}

/** A new user/password for the site's cameras, after it was changed on them. */
function CameraPasswordDialog({ site, onClose }: { site: Site; onClose: () => void }) {
  const { showSuccess, showError } = useSnackbar()
  const [picked, setPicked] = useState<Record<number, boolean>>(() => Object.fromEntries(site.cameras.map((c) => [c.id, true])))
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const ids = site.cameras.filter((c) => picked[c.id]).map((c) => c.id)
  const save = async () => {
    try {
      const res = await siteService.setCameraCredentials(site.id, {
        camera_ids: ids, username: username.trim() || null, password: password || null,
      })
      showSuccess(`${res.changed.length} camera(s) updated; the site's agent reconnects them with the new password`)
      void queryClient.invalidateQueries({ queryKey: SITES_KEY })
      onClose()
    } catch (e) {
      showError(extractApiError(e, 'Could not save'))
    }
  }
  return (
    <Modal open title={`Camera password at "${site.name}"`} onClose={onClose}
      footer={
        <div className="flex justify-end gap-2">
          <Button variant="outline" onClick={onClose}>Cancel</Button>
          <Button variant="primary" disabled={ids.length === 0 || (!username.trim() && !password)} onClick={save}>
            Save for {ids.length} camera{ids.length === 1 ? '' : 's'}
          </Button>
        </div>
      }>
      <div className="space-y-3 text-sm">
        <p className="text-[var(--text-dim)]">
          For after the password was changed on the cameras themselves: OpenNVR uses the new one from now on.
          Leave the user empty to keep each camera's.
        </p>
        <ul className="border border-[var(--border)] divide-y divide-[var(--border)] max-h-48 overflow-auto">
          {site.cameras.map((c) => (
            <li key={c.id} className="flex items-center gap-2 px-2 py-1">
              <input type="checkbox" checked={!!picked[c.id]} aria-label={c.name}
                onChange={(e) => setPicked({ ...picked, [c.id]: e.target.checked })} />
              <span>{c.name}</span>
              <span className="text-xs text-[var(--text-dim)]">{c.lan_host}</span>
            </li>
          ))}
        </ul>
        <div className="grid grid-cols-2 gap-2">
          <Field label="User (optional)"><Input value={username} onChange={(e) => setUsername(e.target.value)} autoComplete="off" /></Field>
          <Field label="New password"><Input type="password" value={password} onChange={(e) => setPassword(e.target.value)} autoComplete="new-password" /></Field>
        </div>
      </div>
    </Modal>
  )
}

function SiteCard({ site, canManage, onCode, onAddCameras }: {
  site: Site
  canManage: boolean
  onCode: (site: Site, code: EnrolCode) => void
  onAddCameras: (site: Site) => void
}) {
  const confirm = useConfirm()
  const { showSuccess, showError } = useSnackbar()
  const status = siteStatus(site)
  const [uplinkOpen, setUplinkOpen] = useState(false)
  const [passwordOpen, setPasswordOpen] = useState(false)
  const [checksOpen, setChecksOpen] = useState(false)
  const [settingsOpen, setSettingsOpen] = useState(false)
  const uplink = siteUplinkKbps(site)
  const refresh = () => queryClient.invalidateQueries({ queryKey: SITES_KEY })

  const act = async (fn: () => Promise<unknown>, ok: string, fail: string) => {
    try {
      await fn()
      showSuccess(ok)
      void refresh()
    } catch (e) {
      showError(extractApiError(e, fail))
    }
  }

  return (
    <div className="border border-[var(--border)] bg-[var(--panel)]">
      {site.approval_pending && (
        <div className="flex flex-wrap items-center gap-2 px-3 py-2 border-b border-[var(--border)] bg-[var(--panel-2)] text-sm">
          <ShieldAlert size={15} className="text-[var(--warning,orange)]" />
          <span>
            A new agent took over this site
            {site.pending_agent?.hostname ? <> on <b>{site.pending_agent.hostname}</b></> : null}
            {site.pending_agent?.ip ? <> from {site.pending_agent.ip}</> : null}. It gets no cameras (and none of
            their passwords) until you approve it.
          </span>
          <span className="flex-1" />
          {canManage && (
            <>
              <Button size="sm" variant="primary"
                onClick={() => act(() => siteService.approveAgent(site.id), 'Agent approved', 'Could not approve the agent')}>
                Approve
              </Button>
              <Button size="sm" variant="danger"
                onClick={async () => {
                  if (!(await confirm({ title: `Revoke "${site.name}"?`, message: 'The new agent is cut off. Cameras and recordings stay.', confirmLabel: 'Revoke', danger: true }))) return
                  await act(() => siteService.revoke(site.id), 'Site revoked', 'Could not revoke the site')
                }}>
                Not mine: revoke
              </Button>
            </>
          )}
        </div>
      )}
      <div className="flex flex-wrap items-center gap-2 px-3 py-2 border-b border-[var(--border)]">
        <Globe size={15} className="text-[var(--accent)]" />
        <span className="font-semibold">{site.name}</span>
        <Badge variant={status.variant} title={status.hint}>{status.label}</Badge>
        {site.enrolled && site.hostname && (
          <span className="text-xs text-[var(--text-dim)]">{site.hostname} · {site.agent_version}</span>
        )}
        {site.online && (
          <span className="text-xs text-[var(--text-dim)]" title="What the site sends upstream now">
            ↑ {Math.round(uplink)} kbps{site.uplink_cap_kbps ? ` / ${site.uplink_cap_kbps} cap` : ''}
            {site.live_policy === 'sub_only' ? ' · sub streams only' : ''}
          </span>
        )}
        <span className="flex-1" />
        {canManage && (
          <>
            <Button size="sm" variant="outline" title="A setup link (or a one-time code) to set up, or replace, this site's agent"
              onClick={async () => {
                try { onCode(site, await siteService.newCode(site.id)); void refresh() }
                catch (e) { showError(extractApiError(e, 'Could not make a code')) }
              }}>
              <KeyRound size={14} /> Set up agent
            </Button>
            <Button size="sm" variant="outline" disabled={!site.enrolled}
              title="Find the site's cameras, check them with one password, add them in one go"
              onClick={() => onAddCameras(site)}>
              <Plus size={14} /> Add cameras
            </Button>
            <Button size="sm" variant="outline" title="What the site sends, an uplink cap, and an uplink test" onClick={() => setUplinkOpen(true)}>
              <Gauge size={14} /> Uplink
            </Button>
            {site.enrolled && (
              <Button size="sm" variant="outline" disabled={!site.online}
                title="The site's agent tests, from the site: this server, the video route and each camera"
                onClick={async () => {
                  try { await siteService.runChecks(site.id); setChecksOpen(true); void refresh() }
                  catch (e) { showError(extractApiError(e, 'Could not run the checks')) }
                }}>
                <Stethoscope size={14} /> Run checks
              </Button>
            )}
            <Button size="sm" variant="ghost" title="Alerts and automatic updates for this site" onClick={() => setSettingsOpen(true)}>
              <Settings size={14} />
            </Button>
            {site.cameras.length > 0 && (
              <Button size="sm" variant="outline" title="A new user/password for the site's cameras" onClick={() => setPasswordOpen(true)}>
                <KeyRound size={14} /> Camera password
              </Button>
            )}
            {site.enrolled && (
              <Button size="sm" variant="danger" title="Cut the agent off now; its streams stop, cameras and recordings stay"
                onClick={async () => {
                  if (!(await confirm({ title: `Revoke "${site.name}"?`, message: 'Its agent is cut off at once and its live streams stop. Cameras and recordings stay; a new enrol code lets the agent back.', confirmLabel: 'Revoke', danger: true }))) return
                  await act(() => siteService.revoke(site.id), 'Site revoked', 'Could not revoke the site')
                }}>
                <Unplug size={14} /> Revoke
              </Button>
            )}
            {site.cameras.length === 0 && (
              <Button size="sm" variant="ghost" title="Delete the site"
                onClick={async () => {
                  if (!(await confirm({ title: `Delete "${site.name}"?`, message: 'The site and its agent credential are removed.', confirmLabel: 'Delete', danger: true }))) return
                  await act(() => siteService.remove(site.id), 'Site deleted', 'Could not delete the site')
                }}>
                <Trash2 size={14} />
              </Button>
            )}
          </>
        )}
      </div>

      {uplinkOpen && <UplinkDialog site={site} onClose={() => setUplinkOpen(false)} />}
      {passwordOpen && <CameraPasswordDialog site={site} onClose={() => setPasswordOpen(false)} />}
      {checksOpen && <ChecksDialog siteId={site.id} onClose={() => setChecksOpen(false)} />}
      {settingsOpen && <SiteSettingsDialog site={site} onClose={() => setSettingsOpen(false)} />}
      {(site.health?.problems?.length ?? 0) > 0 && (
        <ul className="px-3 py-1.5 border-b border-[var(--border)] text-sm space-y-0.5">
          {site.health!.problems.map((p) => (
            <li key={p.code} className="flex items-start gap-2 text-[var(--warn)]">
              <AlertTriangle size={14} className="mt-0.5 shrink-0" /> {p.text}
            </li>
          ))}
        </ul>
      )}
      <div className="px-3 py-2 space-y-2 text-sm">
        {canManage && <SetupLinks site={site} />}
        {site.cameras.length === 0 ? (
          <p className="text-[var(--text-dim)]">No cameras yet.</p>
        ) : (
          <ul className="divide-y divide-[var(--border)]">
            {site.cameras.map((c) => {
              const st = cameraRelayState(site, c.id)
              const variant = st?.state === 'live' ? 'success' : st?.state === 'error' ? 'destructive' : st?.state === 'uplink_cap' ? 'warning' : 'neutral'
              const label = st?.state === 'uplink_cap' ? 'paused (cap)' : st ? st.state : (site.online ? 'waiting' : '—')
              return (
                <li key={c.id} className="flex items-center gap-2 py-1.5">
                  <Video size={14} className="text-[var(--text-dim)]" />
                  <span className="font-medium">{c.name}</span>
                  <span className="text-xs text-[var(--text-dim)]">{c.lan_host}</span>
                  <Badge variant={variant} title={[st?.detail, st?.relay === 'ffmpeg' ? 'relayed by ffmpeg (the built-in relay could not handle this camera)' : null].filter(Boolean).join(' · ') || undefined}>{label}</Badge>
                  {st?.problem && st.state !== 'live' && <span className="text-xs text-[var(--warn)]">{st.problem.text}</span>}
                  {st?.relay === 'ffmpeg' && <span className="text-xs text-[var(--text-dim)]">via ffmpeg</span>}
                  {st?.kbps ? <span className="text-xs text-[var(--text-dim)]">{Math.round(st.kbps)} kbps</span> : null}
                  <span className="flex-1" />
                  {canManage && (
                    <Button size="sm" variant="ghost" title="Stop relaying this camera from the site (the camera stays)"
                      onClick={async () => {
                        if (!(await confirm({ title: `Unlink "${c.name}"?`, message: 'The site stops relaying it. The camera and its recordings stay.', confirmLabel: 'Unlink' }))) return
                        await act(() => siteService.unlink(site.id, c.id), 'Camera unlinked', 'Could not unlink the camera')
                      }}>
                      <Unplug size={14} />
                    </Button>
                  )}
                </li>
              )
            })}
          </ul>
        )}
        {(site.found?.length ?? site.discovered.length) > 0 && (
          <div className="flex items-center gap-2">
            <span className="text-xs text-[var(--text-dim)]">
              {site.found?.length ?? site.discovered.length} found on the site's network, not added yet.
            </span>
            {canManage && (
              <Button size="sm" variant="outline" onClick={() => onAddCameras(site)}>
                <Plus size={13} /> Add them
              </Button>
            )}
          </div>
        )}
      </div>
    </div>
  )
}

export function Sites() {
  const { t } = useTranslation()
  const { showError } = useSnackbar()
  const { hasPermission } = usePermissions()
  const canManage = hasPermission('cameras.manage')
  const { data: sites = [], isLoading, refetch, isFetching } = useQuery({
    queryKey: SITES_KEY,
    queryFn: siteService.list,
    refetchInterval: 5000,
  })
  const [creating, setCreating] = useState(false)
  const [newName, setNewName] = useState('')
  const [enrol, setEnrol] = useState<{ site: Site; code: EnrolCode } | null>(null)
  const [adding, setAdding] = useState<{ site: Site; device: DiscoveredDevice | null } | null>(null)
  const [bulk, setBulk] = useState<Site | null>(null)

  const create = async () => {
    try {
      const made = await siteService.create(newName.trim())
      setCreating(false)
      setNewName('')
      setEnrol({ site: made, code: made })
      void queryClient.invalidateQueries({ queryKey: SITES_KEY })
    } catch (e) {
      showError(extractApiError(e, 'Could not create the site'))
    }
  }

  return (
    <div className="space-y-3">
      <div className="border border-[var(--border)] bg-[var(--panel-2)]">
        <PageTitleBar
          icon={<Globe size={16} />}
          title={t('nav.sites')}
          description={t('sites.description')}
          actions={
            <>
              <Button size="sm" variant="ghost" onClick={() => refetch()} title="Refresh" disabled={isFetching}>
                <RefreshCw size={14} />
              </Button>
              {canManage && (
                <Button size="sm" variant="primary" onClick={() => setCreating(true)}>
                  <Plus size={14} /> Add site
                </Button>
              )}
            </>
          }
        />
      </div>

      {!isLoading && sites.length === 0 ? (
        <EmptyState
          icon={<Globe size={28} />}
          title="No sites yet"
          description="A site is a place with cameras behind its own router: a shop, a branch, a warehouse. A small agent there relays its cameras here over port 443; nothing needs forwarding at the site."
          action={canManage ? <Button variant="primary" onClick={() => setCreating(true)}><Plus size={14} /> Add site</Button> : undefined}
        />
      ) : (
        <div className="space-y-3">
          {sites.map((s) => (
            <SiteCard key={s.id} site={s} canManage={canManage}
              onCode={(site, code) => setEnrol({ site, code })}
              onAddCameras={(site) => setBulk(site)} />
          ))}
        </div>
      )}

      {creating && (
        <Modal open title="Add a site" onClose={() => setCreating(false)}
          footer={
            <div className="flex justify-end gap-2">
              <Button variant="outline" onClick={() => setCreating(false)}>Cancel</Button>
              <Button variant="primary" disabled={!newName.trim()} onClick={create}>Create and get the enrol code</Button>
            </div>
          }>
          <Field label="Site name" required>
            <Input value={newName} onChange={(e) => setNewName(e.target.value)} placeholder="Shop 12, MG Road" autoFocus
              onKeyDown={(e) => { if (e.key === 'Enter' && newName.trim()) void create() }} />
          </Field>
        </Modal>
      )}
      {enrol && <EnrolDialog site={enrol.site} code={enrol.code} onClose={() => setEnrol(null)} />}
      {adding && <AddCameraDialog site={adding.site} device={adding.device} onClose={() => setAdding(null)} />}
      {bulk && (
        <AddCamerasDialog siteId={bulk.id} onClose={() => setBulk(null)}
          onAddByUrl={() => { const s = bulk; setBulk(null); setAdding({ site: s, device: null }) }} />
      )}
    </div>
  )
}
