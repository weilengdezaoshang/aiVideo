import { test } from 'node:test'
import assert from 'node:assert/strict'
import { spawnSync } from 'node:child_process'
test('独立 React 18 创作界面 DOM 测试', () => {
  const result = spawnSync(
    'npm',
    ['test', '--prefix', 'apps/web/creation-agent', '--workspaces=false'],
    { encoding: 'utf8' },
  )
  assert.equal(result.status, 0, result.stdout + result.stderr)
})
