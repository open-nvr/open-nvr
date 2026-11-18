import assert from 'node:assert/strict'
import test from 'node:test'
import { planMerge, wearLabel } from '../src/lib/overlayMerge.ts'

const t0 = (id) => ({ source: 'tier0', lostAt: null, track: { id, label: 'person', score: 0.9 } })
const app = (track, label, extra = {}) => ({
  source: 'app:attendly', lostAt: null, track: { id: null, label, score: null, track }, ...extra,
})

test('an app box naming an on-screen track labels that box and is not drawn itself', () => {
  const e = new Map([['tier0|person#7', t0(7)], ['app:attendly|Asha@0', app(7, 'Asha · 1008')]])
  const plan = planMerge(e)
  assert.equal(plan.labelled.get('tier0|person#7').track.label, 'Asha · 1008')
  assert.deepEqual([...plan.hidden], ['app:attendly|Asha@0'])
})

test('a track id matches across number and string', () => {
  const e = new Map([['tier0|person#7', t0(7)], ['a', app('7', 'Asha')]])
  assert.equal(planMerge(e).labelled.size, 1)
})

test('an app box whose track is not on screen draws on its own', () => {
  const e = new Map([['tier0|person#7', t0(7)], ['a', app(9, 'Ravi')]])
  const plan = planMerge(e)
  assert.equal(plan.labelled.size, 0)
  assert.equal(plan.hidden.size, 0)
})

test('an app box with no track reference is untouched', () => {
  const e = new Map([['tier0|person#7', t0(7)], ['a', app(null, 'zone')]])
  assert.equal(planMerge(e).hidden.size, 0)
})

test('a fading app box no longer labels the track', () => {
  const e = new Map([['tier0|person#7', t0(7)], ['a', app(7, 'Asha', { lostAt: 5 })]])
  assert.equal(planMerge(e).labelled.size, 0)
})

test('the first app to claim a track keeps the label; the second is still hidden', () => {
  const e = new Map([['tier0|person#7', t0(7)], ['a', app(7, 'Asha')], ['b', { ...app(7, 'Other'), source: 'app:x' }]])
  const plan = planMerge(e)
  assert.equal(plan.labelled.get('tier0|person#7').track.label, 'Asha')
  assert.deepEqual([...plan.hidden].sort(), ['a', 'b'])
})

test('the worn label drops the detector id and score', () => {
  const worn = wearLabel({ id: 7, label: 'person', score: 0.9, stationary: true }, { id: null, label: 'Asha · 1008', score: null })
  assert.deepEqual(worn, { id: null, label: 'Asha · 1008', score: null, stationary: true })
})
