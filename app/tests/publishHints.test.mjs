import assert from 'node:assert/strict'
import test from 'node:test'
import { mediaPortHint } from '../src/lib/publishHints.ts'

test('the hint names the port and the server side, not the viewer', () => {
  const h = mediaPortHint(8189)
  assert.match(h, /port 8189 through \(UDP, and TCP\)/)
  assert.match(h, /in front of the server/)
  assert.match(h, /as well as 443/)
  assert.doesNotMatch(h, /ask your administrator/i)
})

test('the hint explains why it works from some networks and not others', () => {
  const h = mediaPortHint(8189)
  assert.match(h, /home broadband/)
  assert.match(h, /mobile data or a phone hotspot/)
  assert.match(h, /Still trying\.$/)
})

test('a public ICE port other than 8189 is the one named', () => {
  assert.match(mediaPortHint(18189), /port 18189/)
})

test('without a known port the hint still reads', () => {
  for (const p of [undefined, null, 0]) {
    const h = mediaPortHint(p)
    assert.match(h, /let the media port through/)
    assert.doesNotMatch(h, /undefined|null/)
  }
})
