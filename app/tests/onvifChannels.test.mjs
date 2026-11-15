import assert from 'node:assert/strict'
import test from 'node:test'
import {
  bulkRowsForChannels,
  channelCameraName,
  channelSizeLabel,
  defaultChannelSelection,
  firstCreatedCameraId,
  isMultiChannel,
} from '../src/lib/onvifChannels.ts'

const profile = (token, uri, w, h) => ({ token, name: token, stream_uri: uri, width: w, height: h })

const channels = [
  {
    index: 1, name: 'Ch 1', source_token: 'VideoSource_1',
    main: profile('Profile_101', 'rtsp://10.0.0.5:554/Streaming/Channels/101?transportmode=unicast', 1920, 1080),
    sub: profile('Profile_102', 'rtsp://10.0.0.5:554/Streaming/Channels/102?transportmode=unicast', 640, 480),
    profiles: [], already_added: false,
  },
  {
    index: 2, name: 'Ch 2', source_token: 'VideoSource_2',
    main: profile('Profile_201', 'rtsp://10.0.0.5:554/Streaming/Channels/201', 2560, 1440),
    sub: null, profiles: [], already_added: true, already_added_camera: { id: 7, name: 'Lobby' },
  },
  // An empty slot: profiles but no stream URI.
  { index: 3, name: 'Ch 3', source_token: 'VideoSource_3', main: null, sub: null, profiles: [], already_added: false },
]

test('one channel is a camera, more is a recorder', () => {
  assert.equal(isMultiChannel([channels[0]]), false)
  assert.equal(isMultiChannel(channels), true)
  assert.equal(isMultiChannel(undefined), false)
})

test('channels already added or without a stream are not ticked by default', () => {
  assert.deepEqual([...defaultChannelSelection(channels)], [1])
})

test('names are prefix plus channel, within the limit', () => {
  assert.equal(channelCameraName('Hikvision DS-7616', 3), 'Hikvision DS-7616 Ch 3')
  assert.equal(channelCameraName('   ', 12), 'Ch 12')
  assert.equal(channelCameraName('x'.repeat(120), 1).length, 100)
})

test('the size label is the main stream, or nothing', () => {
  assert.equal(channelSizeLabel(channels[0]), '1920×1080')
  assert.equal(channelSizeLabel(channels[2]), '')
})

test('bulk rows carry raw URLs, the sub stream and the device identity', () => {
  const rows = bulkRowsForChannels(channels, new Set([1, 2, 3]), 'NVR', '10.0.0.5',
    { username: 'admin', password: 'p@ss' }, { manufacturer: 'Hikvision', model: 'DS-7616', serialnumber: 'S1' })
  assert.equal(rows.length, 2, 'the empty slot is left out even when ticked')
  assert.deepEqual(rows[0], {
    name: 'NVR Ch 1', ip_address: '10.0.0.5', port: 554, username: 'admin', password: 'p@ss',
    rtsp_url: 'rtsp://10.0.0.5:554/Streaming/Channels/101?transportmode=unicast',
    substream_url: 'rtsp://10.0.0.5:554/Streaming/Channels/102?transportmode=unicast',
    manufacturer: 'Hikvision', model: 'DS-7616', firmware_version: undefined, serial_number: 'S1', hardware_id: undefined,
  })
  assert.equal(rows[1].substream_url, undefined)
  assert.equal(rows[1].name, 'NVR Ch 2')
})

test('the first created camera is the lowest channel that was created', () => {
  const results = [
    { channel: 3, outcome: 'created', camera_id: 31 },
    { channel: 1, outcome: 'skipped', camera_id: null },
    { channel: 2, outcome: 'created', camera_id: 22 },
  ]
  assert.equal(firstCreatedCameraId(results), 22)
  assert.equal(firstCreatedCameraId([]), undefined)
  assert.equal(firstCreatedCameraId(undefined), undefined)
})
