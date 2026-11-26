import assert from 'node:assert/strict'
import test from 'node:test'
import { layerOf } from '../src/lib/overlayLayers.ts'

const app = 'app:gold-bar-desk'

test('an app label with live numbers stays one layer as they change', () => {
  assert.equal(layerOf(app, 'Trays: 24').key, layerOf(app, 'Trays: 25').key)
  assert.equal(layerOf(app, 'In hand: 1').key, layerOf(app, 'In hand: 2').key)
  assert.equal(layerOf(app, 'Trays: 24').name, 'Trays')
})

test('the verdict line is one layer whatever the counts or the verdict', () => {
  const a = layerOf(app, '24 of 27 in view · all accounted for')
  const b = layerOf(app, '26 of 28 in view · POSSIBLE MISSING BAR')
  assert.equal(a.key, b.key)
  assert.equal(a.name, '24 of 27 in view')
})

test('different kinds of box are different layers', () => {
  const keys = new Set(['Bar', 'Bar (hidden)', 'Bar (partly hidden)', 'On the balance', 'Trays: 3']
    .map((l) => layerOf(app, l).key))
  assert.equal(keys.size, 5)
})

test('the detector is layered by class, grouped apart from apps', () => {
  const p = layerOf('tier0', 'person')
  assert.equal(p.group, 'Detector')
  assert.equal(p.name, 'Person')
  assert.notEqual(p.key, layerOf('tier0', 'car').key)
  assert.equal(layerOf(app, 'Bar').group, 'gold-bar-desk')
})

test('a layer the app names wins over the label', () => {
  const a = layerOf(app, 'Trays: 24', 'Zone counts')
  const b = layerOf(app, 'In hand: 1', 'Zone counts')
  assert.equal(a.key, b.key)
  assert.equal(a.name, 'Zone counts')
  assert.notEqual(a.key, layerOf(app, 'Trays: 24').key)
})

test('the detector ignores a layer and stays by class', () => {
  assert.equal(layerOf('tier0', 'person', 'X').key, layerOf('tier0', 'person').key)
})

test('the same words from two apps are two layers', () => {
  assert.notEqual(layerOf('app:a', 'Zone').key, layerOf('app:b', 'Zone').key)
})
