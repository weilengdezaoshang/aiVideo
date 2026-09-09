import { existsSync } from 'node:fs'
import { spawn, spawnSync } from 'node:child_process'
import path from 'node:path'

const root = process.cwd()
const venv = path.join(root, '.venv-cutout')
const python = path.join(venv, 'bin', 'python')

if (!existsSync(python)) {
  const candidate = process.env.CUTOUT_PYTHON || '/opt/homebrew/bin/python3.11'
  const created = spawnSync(candidate, ['-m', 'venv', venv], { stdio: 'inherit' })
  if (created.status !== 0) {
    process.exit(created.status || 1)
  }
}

const probe = spawnSync(python, ['-c', 'import rembg, PIL'], { stdio: 'ignore' })
if (probe.status !== 0) {
  const installed = spawnSync(
    python,
    ['-m', 'pip', 'install', '-r', path.join(root, 'services/cutout/requirements.txt')],
    { stdio: 'inherit' },
  )
  if (installed.status !== 0) {
    process.exit(installed.status || 1)
  }
}

const child = spawn(python, [path.join(root, 'services/cutout/server.py')], {
  stdio: 'inherit',
  env: { ...process.env, CUTOUT_PORT: process.env.CUTOUT_PORT || '7100' },
})
child.on('exit', (code) => process.exit(code ?? 0))
for (const signal of ['SIGINT', 'SIGTERM']) {
  process.on(signal, () => child.kill(signal))
}
