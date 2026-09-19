import './dom.js'
import test from 'node:test'
import assert from 'node:assert/strict'
import { fireEvent, screen, waitFor } from '@testing-library/dom'
import { createAudioTracks } from '../apps/web/canvas/flows/audio-tracks.js'
import { createDocStore } from '../apps/web/canvas/state/doc-store.js'
import { emptyTimeline } from '../apps/web/canvas/state/timeline.js'
import { testWindow } from './dom.js'

test('导入音频使用实际时长，编辑可撤销，超出画面结尾显示警告', async () => {
  document.body.innerHTML = '<section></section>'
  const store = createDocStore({
    id: 'audio-ui',
    name: '音轨',
    objects: {},
    order: [],
    timeline: emptyTimeline(),
  })
  const original = globalThis.fetch
  globalThis.fetch = async () =>
    new Response(JSON.stringify({ asset: { id: 'a', durationSec: 2 } }), { status: 201 })
  const view = createAudioTracks(document.querySelector('section')!, store)
  try {
    fireEvent.change(screen.getByLabelText('导入 WAV 音频'), {
      target: { files: [new testWindow.File(['wave'], '旁白.wav', { type: 'audio/wav' })] },
    })
    await waitFor(() => assert.equal(store.doc.timeline!.audioClips?.length, 1))
    assert.equal(store.doc.timeline!.audioClips![0].outFrame, 60)
    assert.match(document.body.textContent!, /超出画面结尾/)
    fireEvent.change(screen.getByLabelText('旁白.wav 起点'), { target: { value: '1' } })
    assert.equal(store.doc.timeline!.audioClips![0].startFrame, 30)
    store.undo()
    assert.equal(store.doc.timeline!.audioClips![0].startFrame, 0)
    fireEvent.change(screen.getByLabelText('旁白.wav 出点'), { target: { value: '4' } })
    assert.equal(store.doc.timeline!.audioClips![0].outFrame, 60)
    fireEvent.click(screen.getByRole('button', { name: '删除音轨', hidden: true }))
    assert.equal(store.doc.timeline!.audioClips!.length, 0)
    store.undo()
    assert.equal(store.doc.timeline!.audioClips!.length, 1)
  } finally {
    view.dispose()
    globalThis.fetch = original
    document.body.innerHTML = ''
  }
})
