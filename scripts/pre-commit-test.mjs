// pre-commit 按暂存路径选取快速测试:lint-staged 管格式修复,本脚本管测试选择。
// 限制(明确声明):测试运行于工作区状态而非暂存快照(工作区可能含未暂存修改);
// 干净 checkout 的最终门禁由 CI 承担。不用 git add .,不修改暂存区。
// 文件清单用 NUL 分隔(git diff --cached --name-only -z),覆盖新增/修改/删除/重命名,
// 不做 ACMR 过滤(删除与新旧路径都参与匹配),正确处理空格与中文文件名。

import { spawnSync } from 'node:child_process'
import { dirname, join } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..')

// 首个匹配的套件决定归属;后端路径必须先于 tests/ 的宽匹配(用负向先行断言排除)。
const SUITES = [
  {
    name: 'backend',
    // 后端代码、后端测试、启动器与依赖锁定 → 全量后端快速套件(-x 首败即停)。
    test: /^(apps\/api\/backend\/|tests\/backend\/|scripts\/backend\.py$|scripts\/eval\.py$|requirements[^/]*\.txt$)/,
  },
  {
    name: 'web',
    // 前端、前端测试、共享配置与 hook 脚本本身 → 全量前端套件(秒级,含 hook 测试)。
    test: /^(apps\/web\/|tests\/(?!backend\/)|apps\/api\/[^/]+\.tsx?$|package(-lock)?\.json$|tsconfig[^/]*\.json$|eslint\.config\.mjs$|scripts\/(build-react|e2e-smoke)\.(ts|mjs)$|scripts\/commit-lint\.mjs$|scripts\/pre-commit-test\.mjs$|\.husky\/)/,
  },
]

/**
 * 读取暂存文件清单(NUL 分隔;包含删除路径;保留空格与中文文件名)。
 * @param {string} cwd git 仓库根目录
 * @returns {string[]}
 */
export function readStagedFiles(cwd) {
  const result = spawnSync('git', ['diff', '--cached', '--name-only', '-z'], {
    cwd,
    encoding: 'buffer',
  })
  if (result.status !== 0) {
    throw new Error(`无法读取暂存清单:${result.stderr.toString()}`)
  }
  return result.stdout.toString('utf8').split('\0').filter(Boolean)
}

/**
 * 按暂存路径选取要运行的测试套件名。
 * @param {string[]} files
 * @returns {string[]}
 */
export function pickSuites(files) {
  const selected = new Set()
  for (const file of files) {
    for (const suite of SUITES) {
      if (suite.test.test(file)) {
        selected.add(suite.name)
      }
    }
  }
  return [...selected]
}

/** 项目 venv 内的 Python(与 scripts/backend.py 同一解析规则)。 */
function pythonExe() {
  return join(ROOT, '.venv', process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python')
}

/**
 * 每个套件的执行命令。
 * @param {string} name
 * @param {string[]} stagedFiles
 * @returns {{ cmd: string, args: string[] }[]}
 */
function suiteCommands(name, stagedFiles) {
  if (name === 'backend') {
    const pyFiles = stagedFiles.filter((f) => f.endsWith('.py'))
    const commands = []
    if (pyFiles.length > 0) {
      commands.push({
        cmd: pythonExe(),
        args: ['-m', 'ruff', 'check', '--no-cache', ...pyFiles],
      })
    }
    commands.push({ cmd: pythonExe(), args: ['-m', 'pytest', '-q', '-x', 'tests/backend'] })
    return commands
  }
  if (name === 'web') {
    return [{ cmd: 'npm', args: ['test'] }]
  }
  return []
}

/**
 * @param {string[]} argv 命令行参数(--print 仅打印将执行的命令)
 * @returns {number} 退出码
 */
function main(argv) {
  if (argv[0] === '--print') {
    const files = readStagedFiles(process.cwd())
    for (const name of pickSuites(files)) {
      for (const { cmd, args } of suiteCommands(name, files)) {
        console.log([cmd, ...args].join(' '))
      }
    }
    return 0
  }
  const files = readStagedFiles(process.cwd())
  const suites = pickSuites(files)
  // Git hooks export repository-local variables. Tests that create temporary
  // repositories must not inherit the caller's index, worktree or Git directory.
  const testEnvironment = { ...process.env }
  for (const key of Object.keys(testEnvironment)) {
    if (key.startsWith('GIT_')) {
      delete testEnvironment[key]
    }
  }
  if (suites.length === 0) {
    console.log('pre-commit:暂存变更无需运行测试(纯文档或无匹配路径)')
    return 0
  }
  for (const name of suites) {
    for (const { cmd, args } of suiteCommands(name, files)) {
      console.log(`pre-commit:[${name}] ${[cmd, ...args].join(' ')}`)
      const result = spawnSync(cmd, args, {
        cwd: ROOT,
        stdio: 'inherit',
        env: testEnvironment,
      })
      if (result.status !== 0) {
        console.error(`pre-commit:套件 ${name} 失败(退出码 ${result.status ?? '信号中断'})`)
        return 1
      }
    }
  }
  return 0
}

if (import.meta.url === pathToFileURL(process.argv[1] || '').href) {
  process.exit(main(process.argv.slice(2)))
}
