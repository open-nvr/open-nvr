import assert from 'node:assert/strict'
import test from 'node:test'
import { durationFieldError } from '../src/utils/mediamtxDuration.ts'

const field = 'recordPartDuration'
const withinLimit = `1.${'9'.repeat(1_000)}ns`
const beyondLimit = `1.${'9'.repeat(1_001)}ns`

test('duration parser preserves fractional nanosecond truncation at the limit', () => {
  assert.equal(durationFieldError(field, withinLimit), undefined)
  assert.equal(durationFieldError(field, '1.9ns'), undefined)
})

test('duration parser rejects fractional components beyond the shared limit', () => {
  assert.match(durationFieldError(field, beyondLimit) ?? '', /fraction exceeds 1000 digits/)
})
