import assert from 'node:assert/strict'
import { test } from 'node:test'
import { evidenceOf, splitCitations } from '../apps/web/canvas/agent/citations.js'

test('splitCitations keeps order and finds envelope and fact citations', () => {
  assert.deepEqual(splitCitations('共 63 张[E2.f1]，首页 50 张[E2]。'), [
    { kind: 'text', text: '共 63 张' },
    { kind: 'ref', ref: 'E2.f1' },
    { kind: 'text', text: '，首页 50 张' },
    { kind: 'ref', ref: 'E2' },
    { kind: 'text', text: '。' },
  ])
})

test('splitCitations ignores lookalikes and plain text', () => {
  for (const text of ['没有引用', '[E]', '[e2]', '[E2.x1]', 'E2.f1', '[ E2 ]', '[E2.f1.f2]']) {
    assert.deepEqual(splitCitations(text), [{ kind: 'text', text }], text)
  }
  assert.deepEqual(splitCitations(''), [])
})

test('adjacent citations produce no empty text parts', () => {
  assert.deepEqual(splitCitations('[E1][E2.f3]'), [
    { kind: 'ref', ref: 'E1' },
    { kind: 'ref', ref: 'E2.f3' },
  ])
})

test('evidenceOf maps a fact reference to its envelope', () => {
  assert.equal(evidenceOf('E12.f3'), 'E12')
  assert.equal(evidenceOf('E4'), 'E4')
})
