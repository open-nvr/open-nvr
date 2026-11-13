import assert from 'node:assert/strict'
import test from 'node:test'
import { autoGrid, buildPages, cycleSeconds, formatDuration, pageGroups, wrapIndex } from '../src/lib/tourPlan.ts'

const grid = (cols, rows) => ({
  name: `${cols}x${rows}`,
  gridCols: cols,
  gridRows: rows,
  tiles: Array.from({ length: cols * rows }, (_, i) => ({ row: Math.floor(i / cols), col: i % cols, rowSpan: 1, colSpan: 1 })),
})
const LAYOUTS = { wall: grid(5, 4), '2x2': grid(2, 2) }
const resolve = (id) => LAYOUTS[id] ?? null
const ids = (n, from = 1) => Array.from({ length: n }, (_, i) => from + i)

test('each page is one screenful, in order', () => {
  const pages = buildPages([
    { label: 'North', layout_id: 'wall', duration_s: 60, camera_ids: ids(20) },
    { label: 'South', layout_id: '2x2', duration_s: 30, camera_ids: ids(3, 21) },
  ], resolve)
  assert.deepEqual(pages.map((p) => [p.index, p.label, p.cameraIds.length]), [[0, 'North', 20], [1, 'South', 3]])
  assert.equal(pages[0].durationMs, 60_000)
})

test('a page with fewer cameras than tiles keeps the chosen layout (the preview draws the same)', () => {
  const [short, two] = buildPages([
    { label: '', layout_id: 'wall', duration_s: 30, camera_ids: ids(6) },
    { label: '', layout_id: '2x2', duration_s: 30, camera_ids: ids(2) },
  ], resolve)
  assert.equal(short.layout, LAYOUTS.wall)
  assert.equal(two.layout, LAYOUTS['2x2'])
  assert.equal(short.cameraIds.length, 6)
})

test('a page never shows more cameras than its layout has tiles', () => {
  // The server splits such a page; a layout changed since is cut, not overflowed.
  const [page] = buildPages([{ label: '', layout_id: '2x2', duration_s: 30, camera_ids: ids(6) }], resolve)
  assert.deepEqual(page.cameraIds, [1, 2, 3, 4])
})

test('a page with no visible camera is skipped, but keeps its index', () => {
  const pages = buildPages([
    { label: 'North', layout_id: '2x2', duration_s: 30, camera_ids: [] },
    { label: 'South', layout_id: '2x2', duration_s: 30, camera_ids: [7] },
  ], resolve)
  assert.deepEqual(pages.map((p) => [p.index, p.label]), [[1, 'South']])
})

test('a tour with every page empty has no pages', () => {
  assert.deepEqual(buildPages([{ label: '', layout_id: '2x2', duration_s: 30, camera_ids: [] }], resolve), [])
  assert.deepEqual(buildPages([], resolve), [])
})

test('Arrange keeps a page of empty tiles only, so the wall stays on it', () => {
  const spec = [{ label: '', layout_id: '2x2', duration_s: 30, camera_ids: [null, null] }, { label: '', layout_id: '2x2', duration_s: 30, camera_ids: [9] }]
  assert.deepEqual(buildPages(spec, resolve).map((p) => p.index), [1])
  assert.deepEqual(buildPages(spec, resolve, { keepEmptyPages: true }).map((p) => p.index), [0, 1])
})

test('a missing layout falls back to an automatic grid', () => {
  for (const page of [
    { label: '', layout_id: 'gone', duration_s: 30, camera_ids: ids(6) },
    { label: '', layout_id: 'wall', duration_s: 30, camera_ids: ids(6), layout_missing: true },
  ]) {
    const [shown] = buildPages([page], resolve)
    assert.equal(shown.layoutFallback, true)
    assert.equal(shown.layout.tiles.length >= 6, true)
    assert.equal(shown.cameraIds.length, 6)
  }
})

test('the automatic grid is near-square and capped at 5x5', () => {
  assert.deepEqual([autoGrid(1).gridCols, autoGrid(1).gridRows], [1, 1])
  assert.deepEqual([autoGrid(6).gridCols, autoGrid(6).gridRows], [3, 2])
  assert.deepEqual([autoGrid(20).gridCols, autoGrid(20).gridRows], [5, 4])
  assert.equal(autoGrid(400).tiles.length, 25)
})

test('page keys stay the same for an unchanged tour and change when cameras change', () => {
  const page = { label: 'North', layout_id: '2x2', duration_s: 30, camera_ids: [1, 2] }
  assert.equal(buildPages([page], resolve)[0].key, buildPages([{ ...page }], resolve)[0].key)
  assert.notEqual(buildPages([page], resolve)[0].key, buildPages([{ ...page, camera_ids: [1, 3] }], resolve)[0].key)
})

test('moving through the pages wraps around in both directions', () => {
  assert.equal(wrapIndex(2, 1, 3), 0)
  assert.equal(wrapIndex(0, -1, 3), 2)
  assert.equal(wrapIndex(1, 1, 3), 2)
  assert.equal(wrapIndex(0, 1, 0), 0)
})

test('cycle length and durations read like a person says them', () => {
  const pages = buildPages([
    { label: '', layout_id: '2x2', duration_s: 60, camera_ids: ids(4) },
    { label: '', layout_id: '2x2', duration_s: 60, camera_ids: ids(4, 5) },
    { label: '', layout_id: 'wall', duration_s: 30, camera_ids: ids(3) },
  ], resolve)
  assert.equal(cycleSeconds(pages), 150)
  assert.equal(formatDuration(45), '45 s')
  assert.equal(formatDuration(150), '2 min 30 s')
  assert.equal(formatDuration(120), '2 min')
  assert.equal(formatDuration(3900), '1 h 5 min')
})

test('an empty tile stays empty: the cameras after it keep their tiles', () => {
  const [page] = buildPages([{ label: '', layout_id: '2x2', duration_s: 30, camera_ids: [1, null, 3] }], resolve)
  assert.deepEqual(page.cameraIds, [1, null, 3])
})

test('consecutive pages under one label are listed as one group', () => {
  const p = (label, camera_ids, extra = {}) => ({ label, layout_id: '2x2', duration_s: 30, camera_ids, ...extra })
  assert.deepEqual(pageGroups([
    p('North', [1, 2, 3, 4]), p('North', [5, null, 6]), p('North', [1]),
    p('', [7]), p('', [8]),
    p('South', [9], { layout_missing: true }), p('North', [2]),
  ]), [
    { first: 0, count: 3, label: 'North', cameras: 6, layoutMissing: false },
    { first: 3, count: 1, label: '', cameras: 1, layoutMissing: false },
    { first: 4, count: 1, label: '', cameras: 1, layoutMissing: false },
    { first: 5, count: 1, label: 'South', cameras: 1, layoutMissing: true },
    { first: 6, count: 1, label: 'North', cameras: 1, layoutMissing: false },
  ])
})
