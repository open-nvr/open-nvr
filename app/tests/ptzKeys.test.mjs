import assert from 'node:assert/strict'
import test from 'node:test'
import { buttonFor, digitOf, heldVector, homePreset, presetForDigit } from '../src/lib/ptzKeys.ts'

test('one arrow moves one way', () => {
  assert.deepEqual(heldVector(['ArrowRight']), [1, 0, 0])
  assert.deepEqual(heldVector(['ArrowDown']), [0, -1, 0])
})

test('two arrows make a diagonal, and the pad lights the diagonal button', () => {
  const v = heldVector(['ArrowUp', 'ArrowLeft'])
  assert.deepEqual(v, [-1, 1, 0])
  assert.equal(buttonFor(v), 'ul')
  assert.equal(buttonFor(heldVector(['ArrowDown', 'ArrowRight'])), 'dr')
})

test('opposite keys cancel, and nothing held is no movement', () => {
  assert.deepEqual(heldVector(['ArrowLeft', 'ArrowRight']), [0, 0, 0])
  assert.deepEqual(heldVector([]), [0, 0, 0])
  assert.equal(buttonFor([0, 0, 0]), null)
})

test('zoom keys work by key position, so Shift (fast) does not change them', () => {
  assert.deepEqual(heldVector(['Equal']), [0, 0, 1])
  assert.deepEqual(heldVector(['NumpadSubtract']), [0, 0, -1])
  assert.equal(buttonFor(heldVector(['Equal', 'ArrowUp'])), 'zi')
})

test('keys that are not PTZ keys are ignored', () => {
  assert.deepEqual(heldVector(['KeyA', 'Space']), [0, 0, 0])
})

test('digits from the top row and the numpad; 0 is not a preset key', () => {
  assert.equal(digitOf('Digit3'), 3)
  assert.equal(digitOf('Numpad9'), 9)
  assert.equal(digitOf('Digit0'), null)
  assert.equal(digitOf('KeyD'), null)
})

const presets = [
  { token: 'p-a', name: 'Gate' },
  { token: '7', name: 'Car park' },
  { token: 'x', name: 'Preset 2' },
  { token: 'y', name: 'HOME' },
]

test('a preset is found by the camera\'s own number first', () => {
  assert.equal(presetForDigit(presets, 7).name, 'Car park')
  assert.equal(presetForDigit(presets, 2).name, 'Preset 2')
})

test('otherwise by position in the camera\'s list', () => {
  assert.equal(presetForDigit(presets, 1).name, 'Gate')
  assert.equal(presetForDigit(presets, 4).name, 'HOME')
  assert.equal(presetForDigit(presets, 9), undefined)
})

test('Home is the preset named Home, any case, and nothing else', () => {
  assert.equal(homePreset(presets).token, 'y')
  assert.equal(homePreset([{ token: '1', name: 'Homestead' }]), undefined)
})
