import assert from 'node:assert/strict'
import test from 'node:test'
import { isAdoptable, pickEvictions } from '../src/lib/liveStreamsPolicy.ts'

test('at most `max` stay parked, the oldest closed first', () => {
  const e = [
    { key: 'c', parkedAt: 30 },
    { key: 'a', parkedAt: 10 },
    { key: 'b', parkedAt: 20 },
  ]
  assert.deepEqual(pickEvictions(e, 3), [])
  assert.deepEqual(pickEvictions(e, 2), ['a'])
  assert.deepEqual(pickEvictions(e, 1), ['a', 'b'])
  assert.deepEqual(pickEvictions([], 9), [])
})

test('only a connected connection with live video is adopted', () => {
  assert.equal(isAdoptable('connected', ['live']), true)
  assert.equal(isAdoptable('disconnected', ['live']), false)
  assert.equal(isAdoptable('connected', ['ended']), false)
  assert.equal(isAdoptable('connected', []), false)
})
