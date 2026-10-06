import './dom.js'
import test, { afterEach } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { cleanup, render, screen } from '@testing-library/react'
import { AgentEvidence } from '../apps/web/canvas-next/agent-evidence.js'
import { parseEnvelope } from '../apps/web/canvas/agent/contracts.js'

afterEach(cleanup)

type Case = { name: string; model: string; valid: boolean; value: unknown }
const cases = JSON.parse(
  readFileSync(new URL('./fixtures/agent-contracts/cases.json', import.meta.url), 'utf8'),
) as Case[]
const envelope = (name: string) => {
  const item = cases.find((candidate) => candidate.name === name)
  assert.ok(item, name)
  return { raw: item.value, parsed: parseEnvelope(item.value) }
}

test('成功信封显示覆盖情况、编号事实、必需建议，原始数据默认折叠', () => {
  const { raw, parsed } = envelope('ok envelope')
  const { container } = render(<AgentEvidence envelope={parsed} raw={raw} />)
  assert.match(screen.getByText(/部分结果：仅返回前 50 个/).textContent ?? '', /63/)
  assert.ok(screen.getByText('E2.f1'))
  assert.match(
    container.querySelector('#agent-fact-E2\\.f1')?.textContent ?? '',
    /图片对象共 63 个：63/,
  )
  assert.match(container.querySelector('#agent-fact-E2\\.f3')?.textContent ?? '', /512 × 512/)
  const required = container.querySelector('[data-priority="required"]')
  assert.match(required?.textContent ?? '', /必需.*还有 13 个图片对象未列出.*list_canvas_elements/)
  assert.equal(container.querySelector('details')?.hasAttribute('open'), false)
})

test('错误信封显示错误码、是否可重试、参数字段与恢复建议', () => {
  const { raw, parsed } = envelope('error envelope')
  render(<AgentEvidence envelope={parsed} raw={raw} />)
  assert.match(screen.getByText(/OBJECT_OUT_OF_SCOPE/).textContent ?? '', /修正后可重试/)
  assert.match(screen.getByText(/获取有效对象 ID/).textContent ?? '', /list_canvas_elements/)
})

test('重复信封指向原证据，不显示为新结果', () => {
  const { raw, parsed } = envelope('duplicate envelope')
  render(<AgentEvidence envelope={parsed} raw={raw} />)
  assert.match(screen.getByText(/与 E4 重复/).textContent ?? '', /未再次执行/)
})

test('无法解析的旧输出退回原始 JSON，文本不会成为 HTML', () => {
  const raw = { width: 1024, comment: '<img src=x onerror=alert(1)>' }
  const { container } = render(<AgentEvidence envelope={parseEnvelope(raw)} raw={raw} />)
  assert.match(screen.getByText(/"width": 1024/).textContent ?? '', /1024/)
  assert.equal(container.querySelector('img'), null)
})

test('画布文字只作为事实取值显示，不会被当作 HTML', () => {
  const { raw } = envelope('ok envelope')
  const forged = structuredClone(raw) as { result: { evidence: { value: unknown }[] } }
  forged.result.evidence[0].value = '<img src=x onerror=alert(1)>'
  const { container } = render(<AgentEvidence envelope={parseEnvelope(forged)} raw={forged} />)
  assert.equal(container.querySelector('img'), null)
  assert.match(container.textContent ?? '', /<img src=x/)
})
