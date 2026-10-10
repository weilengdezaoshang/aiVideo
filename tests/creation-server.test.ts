import { test } from 'node:test'
import assert from 'node:assert/strict'
import { spawnSync } from 'node:child_process'
test('创作服务端 S1/S2/S3 ports、HTTP、Trace 与停止契约', () => {
  const result = spawnSync(
    process.execPath,
    [
      '--import',
      'tsx',
      '--test',
      'apps/server/tests/conversation.test.ts',
      'apps/server/tests/ports-contract.test.ts',
      'apps/server/tests/s3.test.ts',
    ],
    { encoding: 'utf8' },
  )
  assert.equal(result.status, 0, result.stdout + result.stderr)
})
