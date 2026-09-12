// 提交信息校验器:hook(commit-msg)与 CI 共用同一实现,避免两套正则不一致。
// 机器只校验结构与可判定规则;内容是否为准确的中文动宾表达由审查补充,
// 正则不能完整判断语义与简繁体,不宣称其做到。

import { spawnSync } from 'node:child_process'
import { readFileSync } from 'node:fs'
import { pathToFileURL } from 'node:url'

const TYPES = 'feat|fix|docs|refactor|test|perf|style|build|ci|chore|revert'
const FUZZY_SCOPES = new Set(['all', 'misc', 'other'])
const SUBJECT_RE = new RegExp(`^(${TYPES})\\(([a-z0-9][a-z0-9-]*)\\)(!)?: (.+)$`)

/**
 * 校验完整提交信息(主题行 + 可选正文)。
 * @param {string} message
 * @returns {{ ok: boolean, reason?: string }}
 */
export function validateCommitMessage(message) {
  const firstLine = (message ?? '').split('\n', 1)[0].trimEnd()
  if (!firstLine.trim()) {
    return { ok: false, reason: '提交信息为空' }
  }
  if (/^(Merge |Revert )/.test(firstLine)) {
    return { ok: true }
  }
  const match = SUBJECT_RE.exec(firstLine)
  if (!match) {
    return { ok: false, reason: '格式应为 type(scope): 中文描述.' }
  }
  const [, , scope, bang, subject] = match
  if (FUZZY_SCOPES.has(scope)) {
    return { ok: false, reason: `禁止使用模糊 scope:${scope}` }
  }
  // CJK 统一表意文字区间:只判断"描述含中文字符",不判断简繁体与语义。
  if (!/[\u4e00-\u9fff]/.test(subject)) {
    return { ok: false, reason: '描述需使用简体中文' }
  }
  if (!subject.endsWith('.')) {
    return { ok: false, reason: '描述需以英文句点结尾' }
  }
  if (/#\d+/.test(subject)) {
    return { ok: false, reason: '主题行不含 issue 编号,需要时写入正文' }
  }
  if (bang && !/^BREAKING CHANGE: /m.test(message)) {
    return { ok: false, reason: '破坏性变更需在正文以 "BREAKING CHANGE: " 说明迁移方式' }
  }
  return { ok: true }
}

/**
 * CI 模式:校验一个提交区间内的每条主题行(与 hook 用同一校验器)。
 * @param {string} range git 提交区间,如 origin/main..HEAD
 * @returns {number} 退出码
 */
function lintRange(range) {
  const result = spawnSync('git', ['log', '--format=%s', range], { encoding: 'utf8' })
  if (result.status !== 0) {
    console.error(`无法读取提交区间 ${range}:${result.stderr}`)
    return 2
  }
  const subjects = result.stdout.split('\n').filter((line) => line.trim())
  if (subjects.length === 0) {
    console.log(`区间 ${range} 内没有提交,视为通过`)
    return 0
  }
  let failed = 0
  for (const subject of subjects) {
    const verdict = validateCommitMessage(subject)
    if (!verdict.ok) {
      failed += 1
      console.error(`✗ ${subject}\n  ${verdict.reason}`)
    }
  }
  if (failed > 0) {
    console.error(`共 ${failed} 条提交信息不合规`)
    return 1
  }
  console.log(`✓ ${subjects.length} 条提交信息合规`)
  return 0
}

/**
 * CLI:git hook 传消息文件路径;CI 传 --range <base>..<head>。
 * @param {string[]} argv 命令行参数
 * @returns {number} 退出码
 */
export function main(argv) {
  const first = argv[0]
  if (first === '--range') {
    if (!argv[1]) {
      console.error('用法:commit-lint.mjs --range <base>..<head>')
      return 2
    }
    return lintRange(argv[1])
  }
  if (!first) {
    console.error('用法:commit-lint.mjs <提交信息文件路径> | --range <base>..<head>')
    return 2
  }
  const verdict = validateCommitMessage(readFileSync(first, 'utf8'))
  if (!verdict.ok) {
    console.error(`✗ 提交信息不符合规范:${verdict.reason}`)
    console.error('  示例:feat(provider): 新增 ComfyUI 图生图支持.')
    return 1
  }
  return 0
}

if (import.meta.url === pathToFileURL(process.argv[1] || '').href) {
  process.exit(main(process.argv.slice(2)))
}
