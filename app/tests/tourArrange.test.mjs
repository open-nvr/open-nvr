import assert from 'node:assert/strict'
import test from 'node:test'
import { addCamera, chunk, closeGaps, fill, firstEmpty, fit, moveTo, placeAt, placeMessage, placeOutcome, removeAt, tidy } from '../src/lib/tourArrange.ts'

// Most pages below are a 3×3: nine tiles.
const T = 9

test('removing a camera leaves its tile empty; no other camera moves', () => {
  assert.deepEqual(removeAt([1, 2, 3, 4], 1), [1, null, 3, 4])
  assert.deepEqual(removeAt([1, 2], 5), [1, 2])
})

test('removing the last camera trims the empty tiles at the end', () => {
  assert.deepEqual(removeAt([1, null, 3], 2), [1])
  assert.deepEqual(tidy([null, 1, null, null]), [null, 1])
})

test('dropping a tile on another swaps the two', () => {
  assert.deepEqual(moveTo([1, 2, 3, 4], 0, 2, T), [3, 2, 1, 4])
  assert.deepEqual(moveTo([1, 2, 3], 1, 1, T), [1, 2, 3])
})

test('dropping a tile on an empty tile moves it exactly there', () => {
  assert.deepEqual(moveTo([1, null, 3], 0, 1, T), [null, 1, 3])
  assert.deepEqual(moveTo([1, 2, 3], 0, 5, T), [null, 2, 3, null, null, 1])
  assert.deepEqual(moveTo([1, 2, 3], 2, 6, T), [1, 2, null, null, null, null, 3])
})

test('nothing moves past the last tile', () => {
  assert.deepEqual(moveTo([1, 2, 3], 0, 3, 4), [null, 2, 3, 1])
  assert.deepEqual(moveTo([1, 2, 3], 0, 4, 4), [1, 2, 3])
})

test('an empty tile cannot be dragged', () => {
  assert.deepEqual(moveTo([1, null, 3], 1, 0, T), [1, null, 3])
})

test('a picked camera replaces the one on its tile', () => {
  assert.deepEqual(placeAt([1, 2, 3], 9, 1, T), [1, 9, 3])
})

test('a picked camera on an empty tile goes on that tile', () => {
  assert.deepEqual(placeAt([1, null, 3], 9, 1, T), [1, 9, 3])
  assert.deepEqual(placeAt([1, 2, 3], 9, 5, T), [1, 2, 3, null, null, 9])
  assert.deepEqual(placeAt([1, 2, 3], 9, 3, 3), [1, 2, 3])
})

test('a camera already on the page is moved, never shown twice', () => {
  assert.deepEqual(placeAt([1, 2, 3], 3, 0, T), [3, 2, 1])
  assert.deepEqual(placeAt([1, 2, 3], 1, 4, T), [null, 2, 3, null, 1])
  assert.deepEqual(placeAt([1, 2, 3], 2, 1, T), [1, 2, 3])
})

test('the first empty tile is a gap, or past the last camera while there is room', () => {
  assert.equal(firstEmpty([1, null, 3], 4), 1)
  assert.equal(firstEmpty([1, 2, 3], 4), 3)
  assert.equal(firstEmpty([1, 2, 3, 4], 4), null)
  assert.equal(firstEmpty([], 4), 0)
})

test('adding fills the first empty tile; a full page is unchanged', () => {
  assert.deepEqual(addCamera([1, null, 3, null, 5], 9, T), [1, 9, 3, null, 5])
  assert.deepEqual(addCamera([1, 2], 9, T), [1, 2, 9])
  assert.deepEqual(addCamera([1, 2], 2, T), [1, 2])
  assert.deepEqual(addCamera([1, 2, 3, 4], 9, 4), [1, 2, 3, 4])
})

test('adding many fills the empty tiles and hands back what did not fit', () => {
  assert.deepEqual(fill([1, null, 3], [7, 8, 9, 10, 11], 4), { ids: [1, 7, 3, 8], rest: [9, 10, 11] })
  // Cameras already on the page, or given twice, are not added again.
  assert.deepEqual(fill([1, 2], [2, 5, 5, 1, 6], 3), { ids: [1, 2, 5], rest: [6] })
  assert.deepEqual(fill([], [1, 2], 4), { ids: [1, 2], rest: [] })
})

test('the cameras that did not fit are laid out as pages of the layout size', () => {
  assert.deepEqual(chunk([1, 2, 3, 4, 5], 2), [[1, 2], [3, 4], [5]])
  assert.deepEqual(chunk([], 4), [])
})

test('a smaller layout keeps the cameras on its tiles and hands back the rest', () => {
  assert.deepEqual(fit([1, 2, 3, 4, 5, null, 7], 4), { ids: [1, 2, 3, 4], rest: [5, 7] })
  assert.deepEqual(fit([1, null, null, null, 5], 3), { ids: [1], rest: [5] })
  assert.deepEqual(fit([1, 2], 4), { ids: [1, 2], rest: [] })
})

test('closing the gaps packs the cameras in order', () => {
  assert.deepEqual(closeGaps([null, 1, null, 3, 4]), [1, 3, 4])
})

test('the input list is never changed', () => {
  const ids = Object.freeze([1, null, 3])
  removeAt(ids, 0)
  moveTo(ids, 0, 1, T)
  placeAt(ids, 9, 1, T)
  addCamera(ids, 9, T)
  fill(ids, [7, 8], T)
  fit(ids, 1)
  closeGaps(ids)
  assert.deepEqual(ids, [1, null, 3])
})

test('placing says what happened, above all to a camera already there', () => {
  assert.deepEqual(placeOutcome([1, 2, 3], 9, 1, T), { kind: 'replaced', at: 1, replaced: 2 })
  assert.deepEqual(placeOutcome([1, 2, 3], 9, 7, T), { kind: 'added', at: 7 })
  assert.deepEqual(placeOutcome([1, null, 3], 9, 1, T), { kind: 'added', at: 1 })
  assert.deepEqual(placeOutcome([1, 2, 3], 3, 0, T), { kind: 'swapped', from: 2, at: 0, other: 1 })
  assert.deepEqual(placeOutcome([1, 2, 3], 1, 5, T), { kind: 'moved', from: 0, at: 5 })
  assert.deepEqual(placeOutcome([1, 2, 3], 2, 1, T), { kind: 'unchanged', at: 1 })
  assert.deepEqual(placeOutcome([1, 2, 3], 9, 3, 3), { kind: 'full' })
})

test('the outcome matches what placeAt does', () => {
  const ids = [1, null, 3, 4]
  for (const cam of [3, 4, 9]) for (const at of [0, 1, 3, 6]) {
    const after = placeAt(ids, cam, at, T)
    const o = placeOutcome(ids, cam, at, T)
    if (o.kind === 'unchanged') assert.deepEqual(after, ids)
    else if (o.kind !== 'full') assert.equal(after[o.at], cam)
  }
})

test('a camera already on the page is reported, a plain add is not', () => {
  const t = (key, v) => `${key} ${JSON.stringify(v)}`
  const nameOf = (id) => `cam${id}`
  assert.equal(placeMessage(t, [1, 2, 3], 9, 3, nameOf, T), null)
  assert.equal(placeMessage(t, [1, 2, 3], 3, 0, nameOf, T), 'tours.placeSwapped {"name":"cam3","from":3,"to":1,"other":"cam1"}')
  assert.equal(placeMessage(t, [1, 2, 3], 2, 1, nameOf, T), 'tours.placeSame {"name":"cam2","n":2}')
  assert.equal(placeMessage(t, [1, 2, 3], 9, 3, nameOf, 3), 'tours.placeFull {"max":3}')
})
