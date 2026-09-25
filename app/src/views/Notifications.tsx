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

// Notifications — the first-class page for the alert-notifier app.
//
// Gates asks "is it open, and can I open it". This page asks the one
// question a notifier exists to answer: "would an alert reach me right
// now, and if one didn't, why not". So the hierarchy is coverage first
// — a strip nobody can scroll past — then the channels that carry the
// alerts, then the rules that choose between them, then the log that
// explains an alert that never arrived.
//
// Three honesty rules the app's contract forces on us. A channel has
// three-plus states, never two: "never verified" is a channel that has
// not errored because it has never been used, and drawing it green is
// the lie that ends in silence at 3am. A test that returned HTTP 200 is
// not proof a phone buzzed, so the test is only half the control — the
// other half is a human pressing "I got it". And every way the site can
// go quiet — a pause, quiet hours, a disarmed site, dry run — is a
// coverage gap that has to be visible without being hunted for.
//
// Read + act, like Gates: editing channels and rules is the Configure
// form in the App Catalog, and this page links there rather than
// growing a second editor that can disagree with it.

import { useEffect, useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  BellOff, BellRing, CheckCheck, ChevronRight, Clock, FlaskConical, ImageIcon, Inbox,
  ListOrdered, Moon, Pencil, Play, RefreshCw, Send, Settings2, ShieldCheck, Stethoscope,
  TriangleAlert, Volume2, VolumeX,
} from 'lucide-react'
import { Link } from 'react-router-dom'
import { apiService } from '../lib/apiService'
import { useTranslation } from '../i18n'
import {
  Badge, Button, Card, CardContent, CardHeader, CardTitle,
  EmptyState, PageHeader, Skeleton, type BadgeVariant,
} from '../components/ui'
import type { RegisteredApp } from './AppCatalog'
import { AppNoCamerasBanner } from './apps/AppSetup'

export const NOTIFICATIONS_CAPABILITY = 'notifications'

export function findNotifierApp(apps: RegisteredApp[] | undefined): RegisteredApp | null {
  return (
    (apps ?? []).find(
      (a) => a.enabled && ((a.manifest as any)?.provides ?? []).includes(NOTIFICATIONS_CAPABILITY)
    ) ?? null
  )
}

/* ------------------------- The app's contract ------------------------- */

type ChannelState = 'healthy' | 'failing' | 'unverified' | 'misconfigured'
type RecentKind = 'delivered' | 'failed' | 'suppressed' | 'muted' | 'info'

type ChannelRow = {
  name: string
  type: string
  address: string
  state: ChannelState
  status: string
  confirmed: boolean
  delivered: number
  failed: number
  last_ok: number
  last_error: string
  can_attach: boolean
  can_edit: boolean
  can_probe: boolean
}

type RuleRow = {
  position: number
  name: string
  /** The sentence the backend already composed — we never re-phrase it. */
  reads: string
  to: string[]
  enabled: boolean
  catch_all: boolean
  would_match: number
  /** Non-empty = the name of the broader rule that swallows this one. */
  never_fires: string
}

type RecentRow = {
  message: string
  time: number
  level: string
  detail: string
  kind: RecentKind
}

type NotifierState = {
  delivered_total?: number
  suppressed_total?: number
  failure_total?: number
  dropped_total?: number
  today?: { delivered?: number; suppressed?: number }
  health?: { failing?: number; unverified?: number; problem?: boolean }
  channels?: ChannelRow[]
  rules?: RuleRow[]
  quiet?: {
    enabled?: boolean
    active?: boolean
    mode?: string
    breakthrough?: string
    ends_at?: string | null
    holding?: number
    windows?: string[]
  }
  /** `scopes` is minutes REMAINING per scope, measured when the snapshot
   *  was taken — not an absolute deadline. The countdown below is that
   *  number minus how long ago we fetched it. */
  mute?: { active?: boolean; scopes?: Record<string, number> }
  site_mode?: string
  respect_site_mode?: boolean
  grouping?: { wait_seconds?: number; pending?: number; inhibit_seconds?: number }
  min_severity?: string
  timezone?: string
  dry_run?: boolean
  base_url?: string
  queue_depth?: number
  alerts_seen?: number
  recent?: RecentRow[]
}

type BacktestResult = {
  ok?: boolean
  error?: string
  alerts_considered?: number
  since?: string
  matches?: Record<string, number>
  never_fires?: { rule: string; covered_by: string }[]
  note?: string
}

/* ----------------------------- Helpers ------------------------------ */

/** Unix seconds → "14:02", 24-hour, to match the app's own clock strings. */
function hhmm(ts: number | null | undefined): string {
  if (!ts) return '—'
  const d = new Date(ts * 1000)
  return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`
}

/** Four channel states, and the label always carries the word: an
 *  operator scanning this list must be able to tell "never verified"
 *  from "healthy" without trusting a colour. */
const CHANNEL_UI: Record<ChannelState, { key: string; hint: string; variant: BadgeVariant }> = {
  healthy:      { key: 'notifications.channels.state.healthy',      hint: 'notifications.channels.stateHint.healthy',      variant: 'success' },
  failing:      { key: 'notifications.channels.state.failing',      hint: 'notifications.channels.stateHint.failing',      variant: 'destructive' },
  unverified:   { key: 'notifications.channels.state.unverified',   hint: 'notifications.channels.stateHint.unverified',   variant: 'warning' },
  misconfigured:{ key: 'notifications.channels.state.misconfigured',hint: 'notifications.channels.stateHint.misconfigured',variant: 'destructive' },
}

/** What the log entry was, as a dot colour and a word. A suppression is
 *  dim, not red: holding back the tenth motion alert of a burst is the
 *  product working, and an operator who sees red for it stops reading. */
const KIND_UI: Record<RecentKind, { key: string; color: string }> = {
  delivered:  { key: 'notifications.recent.kind.delivered',  color: 'var(--ok)' },
  failed:     { key: 'notifications.recent.kind.failed',     color: 'var(--danger)' },
  suppressed: { key: 'notifications.recent.kind.suppressed', color: 'var(--text-dim)' },
  muted:      { key: 'notifications.recent.kind.muted',      color: 'var(--warn)' },
  info:       { key: 'notifications.recent.kind.info',       color: 'var(--accent)' },
}

const SEVERITY_KEY: Record<string, string> = {
  info: 'notifications.severity.info',
  low: 'notifications.severity.low',
  medium: 'notifications.severity.medium',
  high: 'notifications.severity.high',
  critical: 'notifications.severity.critical',
}

const QUIET_MODE_KEY: Record<string, string> = {
  hold: 'notifications.quiet.mode.hold',
  silent: 'notifications.quiet.mode.silent',
  drop: 'notifications.quiet.mode.drop',
}

/** A channel that cannot deliver goes first — it is the coverage gap. */
function byUrgency(a: ChannelRow, b: ChannelRow): number {
  const rank = (c: ChannelRow) =>
    c.state === 'failing' || c.state === 'misconfigured' ? 0 : c.state === 'unverified' ? 1 : 2
  return rank(a) - rank(b) || a.name.localeCompare(b.name)
}

/** A one-second clock, only while something is actually counting down. */
function useTicker(active: boolean): number {
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    if (!active) return
    setNow(Date.now())
    const id = window.setInterval(() => setNow(Date.now()), 1000)
    return () => window.clearInterval(id)
  }, [active])
  return now
}

/* ------------------------------ Page ------------------------------ */

export function Notifications() {
  const { t } = useTranslation()
  const qc = useQueryClient()

  const appsQuery = useQuery({
    queryKey: ['apps'],
    queryFn: async () => {
      const { data } = await apiService.getApps()
      return (Array.isArray(data) ? data : []) as RegisteredApp[]
    },
    retry: 0,
  })
  const app = findNotifierApp(appsQuery.data)

  const statusQuery = useQuery({
    queryKey: ['app-status', app?.id],
    queryFn: async () => {
      const { data } = await apiService.getAppStatus(app!.id)
      return data as { state?: NotifierState }
    },
    enabled: Boolean(app),
    retry: 0,
    // A pause ticks down in real time and a test is something somebody
    // is standing there waiting for, so the poll has to be quick enough
    // that the page never argues with the phone in their hand.
    refetchInterval: 5000,
  })
  const state: NotifierState = statusQuery.data?.state ?? {}

  // Keyed by channel, so testing one channel does not grey out the
  // buttons on the one below it.
  const act = useMutation({
    mutationFn: async ({ action, params }: { action: string; params: Record<string, unknown> }) =>
      apiService.invokeAppAction(app!.id, action, params),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['app-status', app?.id] }),
  })
  const run = (action: string, params: Record<string, unknown>) => act.mutate({ action, params })
  const busyChannel = act.isPending ? (act.variables?.params.channel as string | undefined) : undefined
  const busyAction = act.isPending ? act.variables?.action : undefined

  // Which channels the operator has just tested. HTTP 200 is not proof a
  // phone buzzed, so a test is only half a control: it puts the other
  // half — "I got it" — in front of the person who can answer it.
  const [tested, setTested] = useState<Record<string, boolean>>({})
  const sendTest = (channel: string) => {
    act.mutate(
      { action: 'test', params: { channel } },
      { onSuccess: () => setTested((prev) => ({ ...prev, [channel]: true })) }
    )
  }
  const confirmChannel = (channel: string) => {
    act.mutate(
      { action: 'confirm', params: { channel } },
      { onSuccess: () => setTested((prev) => ({ ...prev, [channel]: false })) }
    )
  }

  const backtest = useMutation({
    mutationFn: async () => {
      const { data } = await apiService.invokeAppAction(app!.id, 'backtest', {})
      return data as BacktestResult
    },
  })

  const channels = useMemo(() => [...(state.channels ?? [])].sort(byUrgency), [state.channels])
  const rules = useMemo(
    () => [...(state.rules ?? [])].sort((a, b) => a.position - b.position),
    [state.rules]
  )
  const recent = state.recent ?? []

  if (!appsQuery.isPending && !app) {
    return (
      <section className="space-y-4">
        <PageHeader title={t('notifications.title')} description={t('notifications.description')} />
        <EmptyState
          icon={<BellRing size={28} />}
          title={t('notifications.noApp')}
          description={t('notifications.noAppHelp')}
        />
      </section>
    )
  }

  return (
    <section className="space-y-4">
      <PageHeader
        title={t('notifications.title')}
        description={t('notifications.description')}
        actions={
          <>
            <Button size="sm" variant="outline" onClick={() => statusQuery.refetch()}>
              <RefreshCw size={14} /> {t('notifications.refresh')}
            </Button>
            {app && (
              <Link to={`/app-catalog/${app.id}`}>
                <Button size="sm" variant="outline"><Settings2 size={14} /> {t('notifications.configure')}</Button>
              </Link>
            )}
          </>
        }
      />

      <AppNoCamerasBanner app={app} />

      {/* ── Coverage ── The one thing on this page that must never be
          scrolled past: is anything reaching anyone right now. */}
      <CoverageStrip
        state={state}
        channels={channels}
        pending={statusQuery.isPending}
        fetchedAt={statusQuery.dataUpdatedAt}
        busyAction={busyAction}
        run={run}
      />

      {/* ── Channels ── */}
      <Card>
        <CardHeader>
          <Send size={16} className="text-[var(--text-dim)]" />
          <CardTitle>{t('notifications.channels.title')}</CardTitle>
          <span className="text-xs text-[var(--text-dim)]">{t('notifications.channels.sub')}</span>
          <span className="ml-auto flex items-center gap-2">
            {statusQuery.isFetching && (
              <span className="text-xs text-[var(--text-dim)]">{t('notifications.updating')}</span>
            )}
            <Button
              size="sm"
              variant="outline"
              disabled={act.isPending || channels.length === 0}
              onClick={() => run('check', {})}
              title={t('notifications.channels.action.checkHint')}
            >
              <Stethoscope size={14} /> {t('notifications.channels.action.check')}
            </Button>
          </span>
        </CardHeader>
        <CardContent>
          {statusQuery.isPending ? (
            <div className="grid grid-cols-1 lg:grid-cols-2 gap-3">
              <Skeleton className="h-40" /><Skeleton className="h-40" />
            </div>
          ) : channels.length === 0 ? (
            <EmptyState
              icon={<BellOff size={24} />}
              title={t('notifications.channels.empty.title')}
              description={t('notifications.channels.empty.body')}
              action={app ? (
                <Link to={`/app-catalog/${app.id}`}>
                  <Button size="sm" variant="primary"><Settings2 size={14} /> {t('notifications.configure')}</Button>
                </Link>
              ) : undefined}
            />
          ) : (
            <div className="grid grid-cols-1 lg:grid-cols-2 gap-3">
              {channels.map((c) => (
                <ChannelCard
                  key={c.name}
                  channel={c}
                  busy={busyChannel === c.name}
                  justTested={Boolean(tested[c.name])}
                  onTest={() => sendTest(c.name)}
                  onConfirm={() => confirmChannel(c.name)}
                />
              ))}
            </div>
          )}
        </CardContent>
      </Card>

      {/* ── Rules ── Read top to bottom, first match wins. */}
      <Card>
        <CardHeader>
          <ListOrdered size={16} className="text-[var(--text-dim)]" />
          <CardTitle>{t('notifications.rules.title')}</CardTitle>
          <span className="text-xs text-[var(--text-dim)]">{t('notifications.rules.sub')}</span>
          <span className="ml-auto">
            <Button
              size="sm"
              variant="outline"
              disabled={backtest.isPending || rules.length === 0}
              onClick={() => backtest.mutate()}
              title={t('notifications.rules.action.backtestHint')}
            >
              <FlaskConical size={14} /> {t('notifications.rules.action.backtest')}
            </Button>
          </span>
        </CardHeader>
        <CardContent className="space-y-3">
          {statusQuery.isPending ? (
            <Skeleton className="h-28" />
          ) : rules.length === 0 ? (
            <EmptyState
              icon={<ListOrdered size={24} />}
              title={t('notifications.rules.empty.title')}
              description={t('notifications.rules.empty.body')}
            />
          ) : (
            <ol className="divide-y divide-[var(--border)] list-none p-0 m-0">
              {rules.map((r) => <RuleRowItem key={`${r.position}-${r.name}`} rule={r} />)}
            </ol>
          )}

          {backtest.data && <BacktestPanel result={backtest.data} />}

          {/* Editing lives in the Configure form, so the rule list and
              the thing that writes it can never disagree. */}
          {app && (
            <p className="m-0 flex items-start gap-2 text-xs text-[var(--text-dim)]">
              <Pencil size={13} className="shrink-0 mt-0.5" aria-hidden />
              <span>
                {t('notifications.rules.editHint')}{' '}
                <Link className="underline" to={`/app-catalog/${app.id}`}>
                  {t('notifications.rules.editLink')}
                </Link>
              </span>
            </p>
          )}
        </CardContent>
      </Card>

      {/* ── Activity ── "Why didn't I get that alert" is answered here. */}
      <Card>
        <CardHeader>
          <Clock size={16} className="text-[var(--text-dim)]" />
          <CardTitle>{t('notifications.recent.title')}</CardTitle>
          <span className="text-xs text-[var(--text-dim)]">{t('notifications.recent.sub')}</span>
        </CardHeader>
        <CardContent>
          {statusQuery.isPending ? (
            <Skeleton className="h-24" />
          ) : recent.length === 0 ? (
            <div className="text-sm text-[var(--text-dim)] py-4 text-center">{t('notifications.recent.empty')}</div>
          ) : (
            <ol className="divide-y divide-[var(--border)] list-none p-0 m-0">
              {recent.map((e, i) => <RecentRowItem key={`${e.time}-${i}`} entry={e} />)}
            </ol>
          )}
        </CardContent>
      </Card>

      {/* ── The settings that shape everything above ── */}
      <SettingsLine state={state} />
    </section>
  )
}

/* ----------------------------- Pieces ----------------------------- */

/** Coverage. Everything that can make this site silent, in one strip:
 *  channel health, quiet hours, a pause with its live countdown, the
 *  site's arm state, dry run — plus today's counts and the controls to
 *  pause and resume. */
function CoverageStrip({ state, channels, pending, fetchedAt, busyAction, run }: {
  state: NotifierState
  channels: ChannelRow[]
  pending: boolean
  /** When this snapshot was taken — the pause countdown is relative to it. */
  fetchedAt: number
  busyAction: string | undefined
  run: (action: string, params: Record<string, unknown>) => void
}) {
  const { t } = useTranslation()
  const quiet = state.quiet ?? {}
  const mute = state.mute ?? {}
  const health = state.health ?? {}
  const today = state.today ?? { delivered: 0, suppressed: 0 }
  const failing = health.failing ?? 0
  const unverified = health.unverified ?? 0
  const scopes = Object.entries(mute.scopes ?? {})
  const paused = Boolean(mute.active) && scopes.length > 0
  const now = useTicker(paused)
  const disarmed = Boolean(state.respect_site_mode) && state.site_mode === 'disarmed'

  // The headline, worst first. A site with no channels at all is the
  // same gap as a site whose only channel is broken — say so in the
  // same voice, at the same weight.
  const gap =
    channels.length === 0
      ? { tone: 'danger' as const, title: t('notifications.coverage.none.title'), body: t('notifications.coverage.none.body') }
      : failing > 0
        ? {
            tone: 'danger' as const,
            title: failing === 1
              ? t('notifications.coverage.failing.titleOne')
              : t('notifications.coverage.failing.title', { count: failing }),
            body: t(failing === 1 ? 'notifications.coverage.failing.bodyOne' : 'notifications.coverage.failing.body'),
          }
        : paused
          ? { tone: 'warn' as const, title: t('notifications.coverage.paused.title'), body: t('notifications.coverage.paused.body') }
          : disarmed
            ? { tone: 'warn' as const, title: t('notifications.coverage.disarmed.title'), body: t('notifications.coverage.disarmed.body') }
            : unverified > 0
              ? {
                  tone: 'warn' as const,
                  title: unverified === 1
                    ? t('notifications.coverage.unverified.titleOne')
                    : t('notifications.coverage.unverified.title', { count: unverified }),
                  body: t('notifications.coverage.unverified.body'),
                }
              : {
                  tone: 'ok' as const,
                  title: channels.length === 1
                    ? t('notifications.coverage.ok.titleOne')
                    : t('notifications.coverage.ok.title', { count: channels.length }),
                  body: t('notifications.coverage.ok.body'),
                }

  const border =
    gap.tone === 'danger' ? 'border-[var(--danger)]/60'
      : gap.tone === 'warn' ? 'border-[var(--warn)]/60'
        : undefined
  const headColor =
    gap.tone === 'danger' ? 'text-[var(--danger)]'
      : gap.tone === 'warn' ? 'text-[var(--warn)]'
        : 'text-[var(--ok)]'
  const HeadIcon = gap.tone === 'ok' ? ShieldCheck : TriangleAlert

  return (
    <Card className={border}>
      <CardContent className="space-y-3 py-3">
        <div className="flex items-start gap-2">
          <HeadIcon size={18} className={`${headColor} shrink-0 mt-0.5`} aria-hidden />
          <div className="min-w-0">
            <div className={`font-semibold ${headColor}`} role="status">
              {pending ? t('notifications.coverage.loading') : gap.title}
            </div>
            {!pending && <div className="text-sm text-[var(--text-dim)]">{gap.body}</div>}
          </div>
        </div>

        {/* ── Commissioning mode ── The page looks alive in dry run; this
            is the only thing that says no phone will ever buzz. */}
        {state.dry_run && (
          <div
            role="status"
            className="rounded border border-[var(--warn)]/50 bg-[var(--warn)]/10 px-3 py-2 text-sm flex items-start gap-2"
          >
            <FlaskConical size={15} className="text-[var(--warn)] shrink-0 mt-0.5" aria-hidden />
            <span>
              <b>{t('notifications.dryRun.title')}</b>{' '}
              <span className="text-[var(--text-dim)]">{t('notifications.dryRun.body')}</span>
            </span>
          </div>
        )}

        {/* ── Every pause, with the time it has left ── */}
        {paused && (
          <ul className="list-none p-0 m-0 space-y-1">
            {scopes.map(([scope, minutes]) => {
              const left = Math.max(0, minutes * 60 - (now - fetchedAt) / 1000)
              return (
                <li key={scope} className="text-sm flex flex-wrap items-center gap-2">
                  <VolumeX size={14} className="text-[var(--warn)] shrink-0" aria-hidden />
                  <span>
                    {scope === '*'
                      ? t('notifications.pause.scopeAll')
                      : t('notifications.pause.scopeCamera', { camera: scope })}
                  </span>
                  <span className="tabular-nums text-[var(--warn)] font-medium">
                    {t('notifications.pause.remaining', { time: countdown(left, t) })}
                  </span>
                  <Button
                    size="sm"
                    variant="outline"
                    disabled={busyAction === 'unmute'}
                    onClick={() => run('unmute', { camera: scope === '*' ? '' : scope })}
                    title={t('notifications.action.resumeHint')}
                  >
                    <Play size={13} /> {t('notifications.action.resume')}
                  </Button>
                </li>
              )
            })}
          </ul>
        )}

        {/* ── The rest of the state, as pills ── */}
        <div className="flex flex-wrap items-center gap-1.5">
          <Badge variant="success" title={t('notifications.todayDeliveredHint')}>
            <CheckCheck size={12} aria-hidden /> {t('notifications.todayDelivered', { count: today.delivered ?? 0 })}
          </Badge>
          <Badge variant="neutral" title={t('notifications.todaySuppressedHint')}>
            <BellOff size={12} aria-hidden /> {t('notifications.todaySuppressed', { count: today.suppressed ?? 0 })}
          </Badge>
          {quiet.active ? (
            <Badge variant="warning" title={t('notifications.quiet.activeHint')}>
              <Moon size={12} aria-hidden />{' '}
              {quiet.ends_at
                ? t('notifications.quiet.activeUntil', { time: quiet.ends_at })
                : t('notifications.quiet.active')}
              {quiet.mode ? ` · ${t(QUIET_MODE_KEY[quiet.mode] ?? 'notifications.quiet.mode.hold')}` : ''}
            </Badge>
          ) : quiet.enabled ? (
            <Badge variant="neutral" title={(quiet.windows ?? []).join(' · ') || t('notifications.quiet.offHint')}>
              <Moon size={12} aria-hidden /> {t('notifications.quiet.scheduled')}
            </Badge>
          ) : null}
          {(quiet.holding ?? 0) > 0 && (
            <Badge variant="warning" title={t('notifications.quiet.holdingHint')}>
              <Inbox size={12} aria-hidden />{' '}
              {quiet.holding === 1
                ? t('notifications.quiet.holdingOne')
                : t('notifications.quiet.holding', { count: quiet.holding ?? 0 })}
            </Badge>
          )}
          {quiet.breakthrough && (
            <Badge variant="neutral" title={t('notifications.quiet.breakthroughHint')}>
              <Volume2 size={12} aria-hidden />{' '}
              {t('notifications.quiet.breakthrough', {
                severity: t(SEVERITY_KEY[quiet.breakthrough] ?? 'notifications.severity.critical'),
              })}
            </Badge>
          )}
          {state.respect_site_mode && (
            <Badge
              variant={disarmed ? 'warning' : state.site_mode === 'unknown' ? 'neutral' : 'success'}
              title={disarmed ? t('notifications.site.disarmedHint') : t('notifications.site.hint')}
            >
              <ShieldCheck size={12} aria-hidden />{' '}
              {t('notifications.site.mode', { mode: siteModeLabel(state.site_mode, t) })}
            </Badge>
          )}
          {(state.queue_depth ?? 0) > 0 && (
            <Badge variant="neutral" title={t('notifications.queuedHint')}>
              {t('notifications.queued', { count: state.queue_depth ?? 0 })}
            </Badge>
          )}
          {(state.dropped_total ?? 0) > 0 && (
            <Badge variant="destructive" title={t('notifications.droppedHint')}>
              {t('notifications.dropped', { count: state.dropped_total ?? 0 })}
            </Badge>
          )}
        </div>

        {/* ── Pause and resume ── A pause always expires; the durations
            are the whole control, so there is no way to ask for one
            that never ends. */}
        <div className="flex flex-wrap items-center gap-1.5 pt-1">
          <span className="text-xs text-[var(--text-dim)]" id="pause-all-label">
            {t('notifications.action.pauseAll')}
          </span>
          {[15, 60, 240].map((minutes) => (
            <Button
              key={minutes}
              size="sm"
              variant="outline"
              disabled={busyAction === 'mute'}
              aria-describedby="pause-all-label"
              aria-label={t('notifications.action.pauseAllLong', { minutes })}
              title={t('notifications.action.pauseAllHint')}
              onClick={() => run('mute', { minutes, camera: '' })}
            >
              {minutes < 60
                ? t('notifications.duration.minutes', { count: minutes })
                : t('notifications.duration.hours', { count: minutes / 60 })}
            </Button>
          ))}
          {paused && (
            <Button
              size="sm"
              variant="primary"
              disabled={busyAction === 'unmute'}
              onClick={() => run('unmute', { camera: '' })}
              title={t('notifications.action.resumeAllHint')}
            >
              <Play size={14} /> {t('notifications.action.resumeAll')}
            </Button>
          )}
        </div>
      </CardContent>
    </Card>
  )
}

/** One channel: whether it can deliver, what it can carry, and the two
 *  halves of proving it — send a test, then have a human say it landed. */
function ChannelCard({ channel: c, busy, justTested, onTest, onConfirm }: {
  channel: ChannelRow
  busy: boolean
  justTested: boolean
  onTest: () => void
  onConfirm: () => void
}) {
  const { t } = useTranslation()
  const ui = CHANNEL_UI[c.state] ?? CHANNEL_UI.unverified
  const broken = c.state === 'failing' || c.state === 'misconfigured'
  const caps: { key: string; hint: string; icon: React.ReactNode }[] = []
  if (c.can_attach) caps.push({ key: 'notifications.channels.cap.photos', hint: 'notifications.channels.cap.photosHint', icon: <ImageIcon size={11} aria-hidden /> })
  if (c.can_edit) caps.push({ key: 'notifications.channels.cap.edit', hint: 'notifications.channels.cap.editHint', icon: <Pencil size={11} aria-hidden /> })
  if (c.can_probe) caps.push({ key: 'notifications.channels.cap.probe', hint: 'notifications.channels.cap.probeHint', icon: <Stethoscope size={11} aria-hidden /> })

  return (
    <Card className={broken ? 'border-[var(--danger)]/60' : undefined}>
      <CardHeader>
        <CardTitle className="truncate">{c.name}</CardTitle>
        <Badge variant={ui.variant} title={t(ui.hint)}>{t(ui.key)}</Badge>
        {c.confirmed && (
          <Badge variant="neutral" title={t('notifications.channels.confirmedHint')}>
            <CheckCheck size={11} aria-hidden /> {t('notifications.channels.confirmed')}
          </Badge>
        )}
      </CardHeader>

      <CardContent className="space-y-2">
        {/* The app composes this sentence itself; it is the most
            accurate thing we have about the channel. */}
        <div className="text-sm">{c.status}</div>

        <div className="text-[11px] text-[var(--text-dim)] font-mono break-words">
          {[c.type, c.address].filter((v) => v && v !== '—').join(' · ') || '—'}
        </div>

        <div className="text-xs text-[var(--text-dim)] tabular-nums">
          {t('notifications.channels.delivered', { count: c.delivered })}
          {' · '}
          <span className={c.failed > 0 ? 'text-[var(--danger)]' : undefined}>
            {t('notifications.channels.failed', { count: c.failed })}
          </span>
          {' · '}
          {c.last_ok
            ? t('notifications.channels.lastOk', { time: hhmm(c.last_ok) })
            : t('notifications.channels.lastOkNever')}
        </div>

        {caps.length > 0 && (
          <ul className="flex flex-wrap gap-1.5 list-none p-0 m-0">
            {caps.map((cap) => (
              <li key={cap.key}>
                <Badge variant="neutral" title={t(cap.hint)}>{cap.icon} {t(cap.key)}</Badge>
              </li>
            ))}
          </ul>
        )}

        {/* A failing channel says WHY in words. The raw transport error
            goes behind a disclosure — it is what gets pasted into a bug
            report, not what tells an operator what to do next. */}
        {broken && c.last_error && (
          <div className="rounded border border-[var(--danger)]/50 bg-[var(--danger)]/10 px-2 py-1.5 text-xs space-y-1">
            <div className="flex items-start gap-1.5">
              <TriangleAlert size={13} className="text-[var(--danger)] shrink-0 mt-0.5" aria-hidden />
              <span>
                <b className="text-[var(--danger)]">
                  {c.state === 'misconfigured'
                    ? t('notifications.channels.cause.misconfigured')
                    : t('notifications.channels.cause.failing')}
                </b>{' '}
                <span className="text-[var(--text-dim)]">
                  {c.state === 'misconfigured'
                    ? t('notifications.channels.cause.misconfiguredBody')
                    : t('notifications.channels.cause.failingBody')}
                </span>
              </span>
            </div>
            <details>
              <summary className="cursor-pointer text-[var(--text-dim)]">
                {t('notifications.channels.cause.detail')}
              </summary>
              <div className="font-mono break-words pt-1">{c.last_error}</div>
            </details>
          </div>
        )}

        {/* ── Proving it ── */}
        <div className="space-y-1.5 pt-1">
          <Button
            size="sm"
            variant={c.state === 'unverified' ? 'primary' : 'outline'}
            className="w-full justify-center"
            disabled={busy}
            onClick={onTest}
            aria-label={t('notifications.channels.action.testLong', { name: c.name })}
            title={t('notifications.channels.action.testHint')}
          >
            <Send size={14} /> {t('notifications.channels.action.test')}
          </Button>

          {justTested && (
            <p className="m-0 text-xs text-[var(--text-dim)]">{t('notifications.channels.testSent')}</p>
          )}

          {!c.confirmed && (
            <Button
              size="sm"
              variant={justTested ? 'primary' : 'ghost'}
              className="w-full justify-center"
              disabled={busy}
              onClick={onConfirm}
              aria-label={t('notifications.channels.action.confirmLong', { name: c.name })}
              title={t('notifications.channels.action.confirmHint')}
            >
              <CheckCheck size={14} /> {t('notifications.channels.action.confirm')}
            </Button>
          )}
        </div>
      </CardContent>
    </Card>
  )
}

/** One rule, read as the sentence the backend composed, with the two
 *  things that tell an operator whether it is doing anything: how many
 *  recent alerts it would have caught, and whether a broader rule above
 *  it means it can never fire at all. */
function RuleRowItem({ rule: r }: { rule: RuleRow }) {
  const { t } = useTranslation()
  const dead = Boolean(r.never_fires)
  return (
    <li className="py-2 flex items-start gap-3 text-sm">
      <span className="tabular-nums text-[var(--text-dim)] shrink-0 w-6 text-right">{r.position}.</span>
      <div className="min-w-0 flex-1 space-y-1">
        <div className={`flex flex-wrap items-center gap-x-2 gap-y-1 ${r.enabled ? '' : 'opacity-60'}`}>
          <span className="font-medium">{r.reads}</span>
          {r.catch_all && (
            <Badge variant="neutral" title={t('notifications.rules.pinnedHint')}>{t('notifications.rules.pinned')}</Badge>
          )}
          {!r.enabled && (
            <Badge variant="neutral" title={t('notifications.rules.disabledHint')}>{t('notifications.rules.disabled')}</Badge>
          )}
          {dead && (
            <Badge variant="warning" title={t('notifications.rules.neverFiresHint')}>
              {t('notifications.rules.neverFires', { name: r.never_fires })}
            </Badge>
          )}
        </div>
        <div className="flex flex-wrap items-center gap-x-2 gap-y-1 text-xs text-[var(--text-dim)]">
          {/* `reads` is a whole sentence and already ends in the
              destination, so repeating "to X" under it reads as a
              stutter. Only the nothing-case is worth saying twice. */}
          {r.to.length === 0 && <span>{t('notifications.rules.nowhere')}</span>}
          <span className="tabular-nums" title={t('notifications.rules.wouldMatchHint')}>
            {r.would_match === 0
              ? t('notifications.rules.wouldMatchNone')
              : r.would_match === 1
                ? t('notifications.rules.wouldMatchOne')
                : t('notifications.rules.wouldMatch', { count: r.would_match })}
          </span>
        </div>
      </div>
    </li>
  )
}

/** What the rules would have done to the alerts this app has actually
 *  seen — run through the same matcher the live path uses. */
function BacktestPanel({ result }: { result: BacktestResult }) {
  const { t } = useTranslation()
  if (result.ok === false) {
    return (
      <div className="rounded border border-[var(--danger)]/50 bg-[var(--danger)]/10 px-3 py-2 text-sm">
        {result.error || t('notifications.rules.backtest.failed')}
      </div>
    )
  }
  const matches = Object.entries(result.matches ?? {})
  const considered = result.alerts_considered ?? 0
  return (
    <div className="rounded border border-[var(--border)] px-3 py-2 text-sm space-y-2">
      <div className="font-medium">
        {considered === 0
          ? t('notifications.rules.backtest.none')
          : t('notifications.rules.backtest.considered', { count: considered })}
        {result.since ? <span className="text-[var(--text-dim)]"> · {t('notifications.rules.backtest.since', { time: result.since })}</span> : null}
      </div>
      {matches.length > 0 && (
        <ul className="list-none p-0 m-0 space-y-0.5">
          {matches.map(([name, count]) => (
            <li key={name} className="flex items-center gap-2 text-xs">
              <ChevronRight size={12} className="text-[var(--text-dim)] shrink-0" aria-hidden />
              <span className="truncate">{name}</span>
              <span className="tabular-nums text-[var(--text-dim)] ml-auto">
                {count === 1
                  ? t('notifications.rules.wouldMatchOne')
                  : t('notifications.rules.wouldMatch', { count })}
              </span>
            </li>
          ))}
        </ul>
      )}
      {(result.never_fires ?? []).length > 0 && (
        <ul className="list-none p-0 m-0 space-y-0.5">
          {(result.never_fires ?? []).map((s) => (
            <li key={s.rule} className="text-xs text-[var(--warn)]">
              {t('notifications.rules.neverFiresRow', { name: s.rule, covered: s.covered_by })}
            </li>
          ))}
        </ul>
      )}
      {result.note && <p className="m-0 text-xs text-[var(--text-dim)]">{result.note}</p>}
    </div>
  )
}

/** One line of the delivery log. A suppression carries its REASON in
 *  the open — that is the whole point of keeping this list. */
function RecentRowItem({ entry: e }: { entry: RecentRow }) {
  const { t } = useTranslation()
  const ui = KIND_UI[e.kind] ?? KIND_UI.info
  const failed = e.kind === 'failed'
  return (
    <li className="py-2 flex items-start gap-3 text-sm">
      <span className="mt-1.5 h-2.5 w-2.5 rounded-full shrink-0" style={{ background: ui.color }}
            role="img" aria-label={t(ui.key)} />
      <span className="tabular-nums text-[var(--text-dim)] shrink-0 w-11 whitespace-nowrap">{hhmm(e.time)}</span>
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-center gap-x-2 gap-y-0.5">
          <span className={`font-medium ${failed ? 'text-[var(--danger)]' : ''}`}>{t(ui.key)}</span>
          <span className="min-w-0 break-words">{e.message}</span>
          {e.level && (
            <span className="text-xs text-[var(--text-dim)] ml-auto">
              {t(SEVERITY_KEY[e.level] ?? 'notifications.severity.info')}
            </span>
          )}
        </div>
        {e.detail && (
          <div className="text-xs text-[var(--text-dim)] mt-0.5 break-words">
            {e.kind === 'suppressed'
              ? <>{t('notifications.recent.why')} {e.detail}</>
              : e.detail}
          </div>
        )}
      </div>
    </li>
  )
}

/** The settings that decide everything above, stated plainly so nobody
 *  has to open the Configure form to find out why an alert was held. */
function SettingsLine({ state }: { state: NotifierState }) {
  const { t } = useTranslation()
  const grouping = state.grouping ?? {}
  const parts: string[] = []
  if (state.min_severity) {
    parts.push(t('notifications.meta.minSeverity', {
      severity: t(SEVERITY_KEY[state.min_severity] ?? 'notifications.severity.high'),
    }))
  }
  if ((grouping.wait_seconds ?? 0) > 0) {
    parts.push(t('notifications.meta.grouping', { seconds: grouping.wait_seconds ?? 0 }))
  }
  if ((grouping.inhibit_seconds ?? 0) > 0) {
    parts.push(t('notifications.meta.inhibit', { seconds: grouping.inhibit_seconds ?? 0 }))
  }
  if ((grouping.pending ?? 0) > 0) {
    parts.push(t('notifications.meta.pending', { count: grouping.pending ?? 0 }))
  }
  if (state.timezone) parts.push(t('notifications.meta.timezone', { zone: state.timezone }))
  if ((state.alerts_seen ?? 0) > 0) {
    parts.push(t('notifications.meta.alertsSeen', { count: state.alerts_seen ?? 0 }))
  }
  if (parts.length === 0) return null
  return (
    <p className="m-0 flex items-start gap-2 px-1 text-xs text-[var(--text-dim)]">
      <BellRing size={13} className="shrink-0 mt-0.5" aria-hidden />
      <span>{parts.join(' · ')}</span>
    </p>
  )
}

/* --------------------------- Small stuff --------------------------- */

function siteModeLabel(mode: string | undefined, t: (k: string, v?: Record<string, string | number>) => string): string {
  if (mode === 'disarmed') return t('notifications.site.disarmed')
  if (mode === 'armed' || mode === 'armed_away' || mode === 'armed_home') return t('notifications.site.armed')
  return t('notifications.site.unknown')
}

/** Seconds left → a countdown a person can read at a glance. */
function countdown(seconds: number, t: (k: string, v?: Record<string, string | number>) => string): string {
  if (seconds <= 0) return t('notifications.duration.ending')
  if (seconds < 60) return t('notifications.duration.seconds', { count: Math.ceil(seconds) })
  const total = Math.ceil(seconds / 60)
  const hours = Math.floor(total / 60)
  const minutes = total % 60
  if (hours <= 0) return t('notifications.duration.minutes', { count: minutes })
  return t('notifications.duration.hoursMinutes', { hours, minutes: String(minutes).padStart(2, '0') })
}
