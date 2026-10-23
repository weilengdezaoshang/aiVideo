// Git hooks 门禁测试:提交信息校验器与 pre-commit 路径选测。
// 集成测试使用临时 Git 仓库,不在真实仓库制造测试提交。

import { spawnSync } from 'node:child_process'
import { chmodSync, mkdtempSync, mkdirSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import test from 'node:test'
import assert from 'node:assert/strict'

import { validateCommitMessage } from '../scripts/commit-lint.mjs'
import { pickSuites, readStagedFiles } from '../scripts/pre-commit-test.mjs'

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..')

/* ---------- commit-lint:结构校验 ---------- */

test('合法提交信息通过校验', () => {
  for (const subject of [
    'fix(store): 保留画布引用的历史生成文件.',
    'feat(web): 封装 Axios 请求与异常转换.',
    'test(queue): 补充重复投递和取消竞态测试.',
    'ci(project): 增加前后端类型与契约检查.',
  ]) {
    const result = validateCommitMessage(subject)
    assert.equal(result.ok, true, `${subject} 应通过:${result.reason ?? ''}`)
  }
})

test('scope 缺失被拒绝', () => {
  assert.equal(validateCommitMessage('feat: 新增功能.').ok, false)
})

test('非法 type 被拒绝', () => {
  assert.equal(validateCommitMessage('wip(store): 进行中的修改.').ok, false)
  assert.equal(validateCommitMessage('FEATURE(store): 新增功能.').ok, false)
})

test('模糊 scope(all/misc/other)被拒绝', () => {
  for (const scope of ['all', 'misc', 'other']) {
    const subject = `feat(${scope}): 新增功能.`
    assert.equal(validateCommitMessage(subject).ok, false, subject)
  }
})

test('大写 scope 被拒绝', () => {
  assert.equal(validateCommitMessage('feat(Server): 新增功能.').ok, false)
})

test('英文描述被拒绝', () => {
  assert.equal(validateCommitMessage('feat(store): add new feature.').ok, false)
})

test('缺少结尾句点被拒绝', () => {
  assert.equal(validateCommitMessage('feat(store): 新增功能').ok, false)
  assert.equal(validateCommitMessage('feat(store): 新增功能。').ok, false, '中文句点也不合规')
})

test('主题行含 issue 编号被拒绝', () => {
  assert.equal(validateCommitMessage('fix(store): 修复 #123 的问题.').ok, false)
})

test('破坏性变更缺少 BREAKING CHANGE 说明被拒绝,有说明通过', () => {
  const missing = validateCommitMessage('feat(contracts)!: 调整任务错误响应结构.')
  assert.equal(missing.ok, false, missing.reason ?? '')
  const complete = validateCommitMessage(
    'feat(contracts)!: 调整任务错误响应结构.\n\nBREAKING CHANGE: 客户端需要读取 code 和 recovery 字段.',
  )
  assert.equal(complete.ok, true, complete.reason ?? '')
})

test('Merge/Revert 主题行直接放行', () => {
  assert.equal(validateCommitMessage("Merge branch 'main' into feature").ok, true)
  assert.equal(
    validateCommitMessage('Revert "feat(store): 新增功能."\n\nThis reverts commit abc1234.').ok,
    true,
  )
})

test('空消息或纯空白被拒绝', () => {
  assert.equal(validateCommitMessage('').ok, false)
  assert.equal(validateCommitMessage('\n\n').ok, false)
})

test('正文可携带英文技术标识符,不影响主题校验', () => {
  const message = 'fix(queue): 修复 TrialResult 未关联 runId 的问题.\n\nDetails: jobId, attemptId.'
  assert.equal(validateCommitMessage(message).ok, true)
})

/* ---------- pre-commit-test:路径选测 ---------- */

test('选测:Planner 提示词与计划器变更触发离线门禁', () => {
  assert.deepEqual(pickSuites(['apps/api/backend/evaluation/planner_messages.py']), [
    'planner',
    'backend',
    'reliability',
  ])
  assert.deepEqual(pickSuites(['apps/api/backend/core/supervisor/prompts/supervisor-v1.txt']), [
    'planner',
    'backend',
    'reliability',
  ])
  assert.deepEqual(pickSuites(['apps/api/backend/core/supervisor/context.py']), [
    'planner',
    'backend',
    'reliability',
  ])
  assert.deepEqual(pickSuites(['tests/fixtures/llm-planner-cases.jsonl']), [
    'planner',
    'reliability',
  ])
})

test('选测:后端路径选中 backend 套件', () => {
  assert.deepEqual(pickSuites(['apps/api/backend/jobs.py']), ['backend', 'reliability'])
  assert.deepEqual(pickSuites(['tests/backend/test_api.py']), ['backend', 'reliability'])
  assert.deepEqual(pickSuites(['requirements.txt']), ['backend', 'reliability'])
})

test('选测:前端路径选中 web 套件,tests/backend 不误入 web', () => {
  assert.deepEqual(pickSuites(['apps/web/canvas/app.ts']), ['web', 'reliability'])
  assert.deepEqual(pickSuites(['tests/canvas-state.test.ts']), ['web', 'reliability'])
  assert.deepEqual(pickSuites(['package.json', 'tsconfig.json']), ['web', 'reliability'])
})

test('选测:公共基础设施变更扩大到 web 套件(hook 脚本测试随全量前端运行)', () => {
  assert.deepEqual(pickSuites(['scripts/commit-lint.mjs']), ['web', 'reliability'])
  assert.deepEqual(pickSuites(['.husky/pre-commit']), ['web', 'reliability'])
})

test('选测:删除与重命名路径参与匹配,空格与中文文件名不漏检', () => {
  assert.deepEqual(pickSuites(['apps/web/canvas/画布 应用.ts']), ['web', 'reliability'])
  assert.deepEqual(pickSuites(['apps/api/backend/old storage.py']), ['backend', 'reliability'])
})

test('选测:每次提交包含可靠性回归，文档变更也不跳过', () => {
  assert.deepEqual(pickSuites(['docs/engineering/tdd-progress.md', 'README.md']), ['reliability'])
  assert.deepEqual(pickSuites(['LICENSE']), ['reliability'])
  assert.deepEqual(pickSuites([]), ['reliability'])
})

test('readStagedFiles:NUL 分隔读取,覆盖删除与中文空格文件名', () => {
  const repo = mkdtempSync(join(tmpdir(), 'aivideo-hooks-'))
  try {
    const git = (...args: string[]) =>
      spawnSync('git', args, { cwd: repo, encoding: 'utf8' }).status
    assert.equal(git('init', '-q'), 0)
    assert.equal(git('config', 'user.email', 'test@example.com'), 0)
    assert.equal(git('config', 'user.name', 'test'), 0)
    writeFileSync(join(repo, '画布 应用.ts'), 'export {}\n')
    mkdirSync(join(repo, 'sub'))
    writeFileSync(join(repo, 'sub', 'removed.py'), 'x = 1\n')
    assert.equal(git('add', '-A'), 0)
    assert.equal(git('commit', '-qm', 'feat(store): 初始提交.'), 0)
    // 新增 + 删除
    writeFileSync(join(repo, '新文件 需求.txt'), 'x\n')
    rmSync(join(repo, 'sub', 'removed.py'))
    assert.equal(git('add', '-A'), 0)

    const staged = readStagedFiles(repo)
    assert.ok(staged.includes('新文件 需求.txt'), '中文与空格文件名完整保留')
    assert.ok(staged.includes('sub/removed.py'), '删除文件也出现在暂存清单中')
  } finally {
    rmSync(repo, { recursive: true, force: true })
  }
})

test('pre-commit --print:删除 Python 文件跳过 Ruff 路径但仍选中后端测试', () => {
  const repo = mkdtempSync(join(tmpdir(), 'aivideo-hooks-'))
  try {
    const git = (...args: string[]) =>
      spawnSync('git', args, { cwd: repo, encoding: 'utf8' }).status
    assert.equal(git('init', '-q'), 0)
    assert.equal(git('config', 'user.email', 'test@example.com'), 0)
    assert.equal(git('config', 'user.name', 'test'), 0)
    const backend = join(repo, 'apps', 'api', 'backend')
    mkdirSync(backend, { recursive: true })
    writeFileSync(join(backend, 'removed.py'), 'x = 1\n')
    assert.equal(git('add', '-A'), 0)
    assert.equal(git('commit', '-qm', 'feat(server): 初始提交.'), 0)
    rmSync(join(backend, 'removed.py'))
    assert.equal(git('add', '-A'), 0)

    const printCommands = () => {
      const result = spawnSync(
        process.execPath,
        [join(ROOT, 'scripts', 'pre-commit-test.mjs'), '--print'],
        {
          cwd: repo,
          encoding: 'utf8',
        },
      )
      assert.equal(result.status, 0, result.stderr)
      return result.stdout
    }
    const deletionOnly = printCommands()
    assert.match(deletionOnly, /-m pytest -q -x tests\/backend/)
    assert.doesNotMatch(deletionOnly, /ruff|removed\.py/)
    assert.match(deletionOnly, /backend\.evaluation\.reliability\.cli/)

    // 只存在于临时工作区的新增文件，确保路径检查使用暂存仓库而非脚本目录。
    writeFileSync(join(backend, 'new-hook-regression.py'), 'x = 2\n')
    assert.equal(git('add', '-A'), 0)
    const mixed = printCommands()
    assert.match(mixed, /-m ruff check --no-cache apps\/api\/backend\/new-hook-regression\.py/)
    assert.doesNotMatch(mixed, /removed\.py/)
    assert.match(mixed, /-m pytest -q -x tests\/backend/)
  } finally {
    rmSync(repo, { recursive: true, force: true })
  }
})

test('集成:临时仓库中 commit-msg hook 阻断不合规提交并放行合规提交', () => {
  const repo = mkdtempSync(join(tmpdir(), 'aivideo-hooks-'))
  try {
    const git = (args: string[], options: { encoding?: 'utf8' | 'buffer' } = {}) =>
      spawnSync('git', args, { cwd: repo, ...options })
    assert.equal(git(['init', '-q']).status, 0)
    assert.equal(git(['config', 'user.email', 'test@example.com']).status, 0)
    assert.equal(git(['config', 'user.name', 'test']).status, 0)
    // hook 目录指向真实仓库的校验器(绝对路径),模拟 core.hooksPath 安装
    const hooks = join(repo, '.githooks')
    mkdirSync(hooks)
    const hookPath = join(hooks, 'commit-msg')
    writeFileSync(
      hookPath,
      `#!/bin/sh\nexec node '${join(ROOT, 'scripts', 'commit-lint.mjs')}' "$1"\n`,
    )
    chmodSync(hookPath, 0o755)
    assert.equal(git(['config', 'core.hooksPath', hooks]).status, 0)

    writeFileSync(join(repo, 'a.txt'), 'a\n')
    assert.equal(git(['add', '-A']).status, 0)
    const bad = git(['commit', '-qm', 'update code'])
    assert.notEqual(bad.status, 0, '不合规提交信息应被阻断')

    const good = git(['commit', '-qm', 'feat(store): 新增初始提交.'])
    assert.equal(good.status, 0, `合规提交应放行:${good.stderr.toString()}`)
  } finally {
    rmSync(repo, { recursive: true, force: true })
  }
})
