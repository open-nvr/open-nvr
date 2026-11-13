import assert from 'node:assert/strict'
import test from 'node:test'
import { addAlert, expire, matchAlert, MAX_ACTIVE } from '../src/lib/tourAlerts.ts'

const ON = { enabled: true, sources: ['app_alert', 'camera_event'], min_severity: 'medium', scope: 'tour', hold_s: 30, cooldown_s: 60 }
const TOUR = new Set([1, 2, 3])
const app = (camera_id, severity = 'high', extra = {}) => ({ event_type: 'app_alert', camera_id, task: 'intrusion', payload: { severity, alert_type: 'intrusion', ...extra } })
const cam = (camera_id, task = 'motion') => ({ event_type: 'camera_event', camera_id, task, payload: {} })

test('only the configured sources, severities and cameras raise an alert', () => {
  assert.equal(matchAlert(app(1), { ...ON, enabled: false }, TOUR), null)
  assert.deepEqual(matchAlert(app(1, 'high', { title: 'Person in vault' }), ON, TOUR), { cameraId: 1, title: 'Person in vault', severity: 'high' })
  assert.equal(matchAlert(app(1, 'low'), ON, TOUR), null)
  assert.equal(matchAlert(app(9), ON, TOUR), null)
  assert.deepEqual(matchAlert(app(9), { ...ON, scope: 'visible' }, TOUR)?.cameraId, 9)
  assert.equal(matchAlert(cam(2), { ...ON, sources: ['app_alert'] }, TOUR), null)
  assert.deepEqual(matchAlert(cam(2, 'line_crossing'), ON, TOUR), { cameraId: 2, title: 'Line crossing', severity: null })
})

test('housekeeping and malformed events never take over the wall', () => {
  assert.equal(matchAlert(cam(2, 'recording_stalled'), ON, TOUR), null)
  assert.equal(matchAlert({ event_type: 'camera_status', camera_id: 1 }, ON, TOUR), null)
  assert.equal(matchAlert({ event_type: 'app_alert', payload: { severity: 'critical' } }, ON, TOUR), null)
  // An unknown severity counts as high (the inbox's own rule).
  assert.equal(matchAlert(app(1, 'weird'), ON, TOUR)?.severity, 'high')
})

test('a repeat alarm on a camera on screen extends it; a new one joins newest first, at most four', () => {
  const none = new Map()
  let active = addAlert([], { cameraId: 1, title: 'A', severity: 'high' }, 1000, ON, none)
  active = addAlert(active, { cameraId: 2, title: 'B', severity: 'high' }, 2000, ON, none)
  active = addAlert(active, { cameraId: 1, title: 'A2', severity: 'critical' }, 5000, ON, none)
  assert.deepEqual(active.map((a) => [a.cameraId, a.title, a.until]), [[1, 'A2', 35000], [2, 'B', 32000]])
  for (const id of [3, 4, 5]) active = addAlert(active, { cameraId: id, title: 'X', severity: 'high' }, 6000, ON, none)
  assert.equal(active.length, MAX_ACTIVE)
  assert.deepEqual(active.map((a) => a.cameraId), [5, 4, 3, 1])
})

test('a camera whose takeover just ended waits out the cool-down', () => {
  const ended = new Map([[1, 10_000]])
  const hit = { cameraId: 1, title: 'A', severity: 'high' }
  assert.equal(addAlert([], hit, 50_000, ON, ended).length, 0)
  assert.equal(addAlert([], hit, 70_001, ON, ended).length, 1)
  assert.equal(addAlert([], hit, 11_000, { ...ON, cooldown_s: 0 }, ended).length, 1)
})

test('expiry splits what is still on screen from what ran out', () => {
  const active = [{ cameraId: 1, title: '', severity: null, at: 0, until: 100 }, { cameraId: 2, title: '', severity: null, at: 0, until: 50 }]
  const { kept, ended } = expire(active, 60)
  assert.deepEqual(kept.map((a) => a.cameraId), [1])
  assert.deepEqual(ended.map((a) => a.cameraId), [2])
})
