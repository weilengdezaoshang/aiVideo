import './dom.js'
import React from 'react'
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { render, fireEvent, screen, cleanup, waitFor } from '@testing-library/react'
import { RunDiagnostics } from '../apps/web/evaluation/RunDiagnostics.js'

const runId = '00000000-0000-4000-8000-000000000001'
const keys = [
  'messages',
  'plans',
  'tasks',
  'calls',
  'attempts',
  'events',
  'deliveries',
  'alerts',
  'quarantines',
  'costs',
  'children',
]
function report() {
  return {
    run: { id: runId, status: 'completed' },
    invariants: [],
    sections: Object.fromEntries(keys.map((key) => [key, { items: [], next: null }])),
  }
}
test('排障页校验 Run ID，展示记录与空段，不写业务数据', async () => {
  const original = globalThis.fetch
  const calls: string[] = []
  globalThis.fetch = async (input) => {
    calls.push(String(input))
    return new Response(JSON.stringify(report()), { status: 200 })
  }
  try {
    render(<RunDiagnostics />)
    fireEvent.change(screen.getByLabelText('Run ID'), { target: { value: 'invalid' } })
    fireEvent.click(screen.getByRole('button', { name: '查询' }))
    assert.match(screen.getByRole('alert').textContent ?? '', /有效/)
    assert.equal(calls.length, 0)
    fireEvent.change(screen.getByLabelText('Run ID'), { target: { value: runId } })
    fireEvent.click(screen.getByRole('button', { name: '查询' }))
    await waitFor(() => assert.ok(screen.getByText(runId + ' · completed')))
    assert.equal(screen.getAllByText('未记录；历史数据不会补造记录。').length, 11)
    assert.equal(calls[0], '/api/internal/core/runs/' + runId)
  } finally {
    cleanup()
    globalThis.fetch = original
  }
})
test('排障页明确显示越权错误', async () => {
  const original = globalThis.fetch
  globalThis.fetch = async () => new Response('{}', { status: 403 })
  try {
    render(<RunDiagnostics />)
    fireEvent.change(screen.getByLabelText('Run ID'), { target: { value: runId } })
    fireEvent.click(screen.getByRole('button', { name: '查询' }))
    await waitFor(() => assert.match(screen.getByRole('alert').textContent ?? '', /管理员/))
  } finally {
    cleanup()
    globalThis.fetch = original
  }
})
test('排障页分页保留事件因果链接，并按记录 ID 去重', async () => {
  const original = globalThis.fetch
  const span = (id: string, eventType: string) => ({
    id,
    kind: 'events',
    run_id: runId,
    parent_run_id: null,
    task_id: null,
    trace_id: runId,
    at: '2026-10-22T00:00:00Z',
    data: { event_type: eventType },
    links: id === 'second' ? { causation_id: 'first' } : {},
  })
  const first = span('first', 'user_message')
  const next = span('second', 'plan_written')
  const initial = {
    ...report(),
    sections: { ...report().sections, events: { items: [first], next: 'seq:1' } },
  }
  globalThis.fetch = async (input, init) => {
    assert.ok(!init?.method || init.method === 'GET')
    const body = String(input).includes('/events?') ? { items: [first, next], next: null } : initial
    return new Response(JSON.stringify(body), { status: 200 })
  }
  try {
    render(<RunDiagnostics />)
    fireEvent.change(screen.getByLabelText('Run ID'), { target: { value: runId } })
    fireEvent.click(screen.getByRole('button', { name: '查询' }))
    const more = await screen.findByRole('button', { name: '加载更多因果事件时间线' })
    fireEvent.click(more)
    await waitFor(() => assert.equal(document.querySelectorAll('#trace-second').length, 1))
    assert.equal(document.querySelectorAll('#trace-first').length, 1)
    assert.equal(screen.getByRole('link', { name: 'first' }).getAttribute('href'), '#trace-first')
    assert.equal(screen.queryByRole('button', { name: '加载更多因果事件时间线' }), null)
  } finally {
    cleanup()
    globalThis.fetch = original
  }
})
