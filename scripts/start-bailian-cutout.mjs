import { spawn } from 'node:child_process'

const children = [
  spawn('npm', ['run', 'cutout'], { stdio: 'inherit' }),
  spawn('npm', ['run', 'bailian'], { stdio: 'inherit' }),
]

let stopping = false
function stop(signal = 'SIGTERM') {
  if (stopping) {
    return
  }
  stopping = true
  for (const child of children) {
    if (child.exitCode === null) {
      child.kill(signal)
    }
  }
}

for (const signal of ['SIGINT', 'SIGTERM']) {
  process.on(signal, () => stop(signal))
}
for (const child of children) {
  child.on('exit', (code) => {
    if (!stopping && code) {
      stop()
      process.exitCode = code
    }
  })
}
