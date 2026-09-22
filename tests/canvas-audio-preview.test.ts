import './dom.js'
import test from 'node:test'
import assert from 'node:assert/strict'
import { createAudioPreview } from '../apps/web/canvas/flows/audio-preview.js'

test('音轨按时间线偏移和源入点定位，并应用淡化和暂停', async () => {
  const proto = HTMLMediaElement.prototype
  const original = {
    play: proto.play,
    pause: proto.pause,
    load: proto.load,
    ready: Object.getOwnPropertyDescriptor(proto, 'readyState')!,
  }
  const played: HTMLMediaElement[] = []
  let pauses = 0
  proto.play = function () {
    played.push(this)
    return Promise.resolve()
  }
  proto.pause = () => {
    pauses++
  }
  proto.load = () => {}
  Object.defineProperty(proto, 'readyState', { configurable: true, get: () => 2 })
  const preview = createAudioPreview(() => {})
  const clip = {
    id: 'a',
    name: '旁白',
    source: { assetId: 'asset', url: '/voice.wav', durationFrames: 300 },
    startFrame: 30,
    inFrame: 60,
    outFrame: 120,
    volume: 0.8,
    fadeInFrames: 30,
    fadeOutFrames: 0,
  }
  try {
    preview.sync([clip], 15, true)
    assert.equal(played.length, 0)
    preview.sync([clip], 45, true)
    assert.equal(played[0].currentTime, 2.5)
    assert.equal(played[0].volume, 0.4)
    preview.pause()
    await Promise.resolve()
    assert.ok(pauses >= 2)
    preview.sync([clip], 60, false)
    assert.equal(played[0].currentTime, 3)
    preview.sync([], 0, false)
    assert.equal(played[0].getAttribute('src'), null)
  } finally {
    preview.dispose()
    proto.play = original.play
    proto.pause = original.pause
    proto.load = original.load
    Object.defineProperty(proto, 'readyState', original.ready)
  }
})

test('音频解码失败显示具体音轨，删除后清理错误回调', () => {
  const media = document.createElement('audio')
  media.pause = () => {}
  media.load = () => {}
  const messages: string[] = []
  const preview = createAudioPreview(
    (message) => messages.push(message),
    () => media,
  )
  const clip = {
    id: 'broken',
    name: '产品旁白',
    source: { assetId: 'a', url: '/missing.wav', durationFrames: 60 },
    startFrame: 0,
    inFrame: 0,
    outFrame: 60,
    volume: 1,
    fadeInFrames: 0,
    fadeOutFrames: 0,
  }
  preview.sync([clip], 0, true)
  media.dispatchEvent(new Event('error'))
  assert.match(messages[0], /产品旁白.*加载失败/)
  preview.sync([], 0, false)
  assert.equal(media.onerror, null)
  media.dispatchEvent(new Event('error'))
  assert.equal(messages.length, 1)
  preview.dispose()
})

test('暂停期间迟到的播放拒绝不会显示素材错误', async () => {
  const media = document.createElement('audio')
  media.pause = () => {}
  media.load = () => {}
  Object.defineProperty(media, 'readyState', { get: () => 2 })
  let rejectPlay!: (reason: Error) => void
  media.play = () =>
    new Promise<void>((_resolve, reject) => {
      rejectPlay = reject
    })
  const messages: string[] = []
  const preview = createAudioPreview(
    (message) => messages.push(message),
    () => media,
  )
  preview.sync(
    [
      {
        id: 'voice',
        name: '旁白',
        source: { assetId: 'a', url: '/voice.wav', durationFrames: 60 },
        startFrame: 0,
        inFrame: 0,
        outFrame: 60,
        volume: 1,
        fadeInFrames: 0,
        fadeOutFrames: 0,
      },
    ],
    0,
    true,
  )
  preview.pause()
  rejectPlay(new Error('The play request was interrupted by pause'))
  await new Promise<void>((resolve) => setTimeout(resolve, 0))
  assert.deepEqual(messages, [])
  preview.dispose()
})
