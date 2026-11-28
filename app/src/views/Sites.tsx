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
import { Check, Copy, Gauge, Globe, KeyRound, Link2, Plus, RefreshCw, Share2, ShieldAlert, Trash2, Unplug, Video } from 'lucide-react'
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
          Windows PC), with <b>ffmpeg</b> installed:
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

function SiteCard({ site, canManage, onCode, onAddCamera }: {
  site: Site
  canManage: boolean
  onCode: (site: Site, code: EnrolCode) => void
  onAddCamera: (site: Site, device: DiscoveredDevice | null) => void
}) {
  const confirm = useConfirm()
  const { showSuccess, showError } = useSnackbar()
  const status = siteStatus(site)
  const [uplinkOpen, setUplinkOpen] = useState(false)
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
            <Button size="sm" variant="outline" onClick={() => onAddCamera(site, null)}>
              <Plus size={14} /> Camera
            </Button>
            <Button size="sm" variant="outline" title="What the site sends, and an uplink cap" onClick={() => setUplinkOpen(true)}>
              <Gauge size={14} /> Uplink
            </Button>
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
      <div className="px-3 py-2 space-y-2 text-sm">
        {canManage && <SetupLinks site={site} />}
        {site.cameras.length === 0 ? (
          <p className="text-[var(--text-dim)]">No cameras yet.{site.discovered.length > 0 ? ' The agent found some on the site\'s network:' : ''}</p>
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
                  <Badge variant={variant} title={st?.detail || undefined}>{label}</Badge>
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
        {site.discovered.length > 0 && (
          <div>
            {site.cameras.length > 0 && <div className="text-xs text-[var(--text-dim)] mt-1">Found on the site's network, not added yet:</div>}
            <ul className="flex flex-wrap gap-2 mt-1">
              {site.discovered.map((d) => (
                <li key={d.ip}>
                  <Button size="sm" variant="outline" disabled={!canManage} onClick={() => onAddCamera(site, d)}
                    title={[d.hardware, d.location].filter(Boolean).join(' · ') || undefined}>
                    <Plus size={13} /> {d.name || d.ip}
                  </Button>
                </li>
              ))}
            </ul>
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
              onAddCamera={(site, device) => setAdding({ site, device })} />
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
    </div>
  )
}
