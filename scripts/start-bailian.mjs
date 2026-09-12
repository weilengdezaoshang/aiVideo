import { spawn } from 'node:child_process'
import { existsSync } from 'node:fs'
import { createInterface } from 'node:readline/promises'

if (existsSync('.env.bailian.local') && typeof process.loadEnvFile === 'function') {
  process.loadEnvFile('.env.bailian.local')
}

async function ask(label) {
  const rl = createInterface({ input: process.stdin, output: process.stdout })
  try {
    return (await rl.question(label)).trim()
  } finally {
    rl.close()
  }
}

async function askSecret(label) {
  if (!process.stdin.isTTY || typeof process.stdin.setRawMode !== 'function') {
    return ask(label)
  }
  process.stdout.write(label)
  process.stdin.setRawMode(true)
  process.stdin.resume()
  process.stdin.setEncoding('utf8')
  return new Promise((resolve, reject) => {
    let value = ''
    const cleanup = () => {
      process.stdin.off('data', onData)
      process.stdin.setRawMode(false)
      process.stdin.pause()
    }
    const onData = (chunk) => {
      for (const char of chunk) {
        if (char === '\u0003') {
          cleanup()
          process.stdout.write('\n')
          reject(new Error('已取消'))
          return
        }
        if (char === '\r' || char === '\n') {
          cleanup()
          process.stdout.write('\n')
          resolve(value.trim())
          return
        }
        if (char === '\u007f' || char === '\b') {
          if (value) {
            value = value.slice(0, -1)
            process.stdout.write('\b \b')
          }
          continue
        }
        if (char >= ' ') {
          value += char
          process.stdout.write('*')
        }
      }
    }
    process.stdin.on('data', onData)
  })
}

function endpoint(value) {
  if (/^https?:\/\//.test(value)) {
    return value.replace(/\/+$/, '')
  }
  if (!/^[a-zA-Z0-9_-]+$/.test(value)) {
    throw new Error('Workspace ID 格式不正确')
  }
  return `https://${value}.cn-beijing.maas.aliyuncs.com/api/v1`
}

try {
  const configuredUrl = process.env.SWARMUI_CLOUD_BASE_URL?.trim()
  const workspace = configuredUrl ? '' : await ask('百炼北京地域 Workspace ID：')
  const apiKey =
    process.env.SWARMUI_IMAGE_API_KEY?.trim() || (await askSecret('百炼 API Key（输入不显示）：'))
  if (!apiKey) {
    throw new Error('API Key 不能为空')
  }

  const port = process.env.SWARMUI_PORT || '7802'
  const child = spawn('python3', ['scripts/backend.py', '--reload'], {
    stdio: 'inherit',
    env: {
      ...process.env,
      SWARMUI_PROVIDER: 'cloud',
      SWARMUI_CLOUD_VENDOR: 'aliyun',
      SWARMUI_CLOUD_MODEL: process.env.SWARMUI_CLOUD_MODEL || 'qwen-image-2.0',
      SWARMUI_CLOUD_BASE_URL: configuredUrl || endpoint(workspace),
      SWARMUI_IMAGE_API_KEY: apiKey,
      SWARMUI_CLOUD_TEXT_MODEL: '',
      SWARMUI_PORT: port,
    },
  })
  console.log(`启动后打开：http://127.0.0.1:${port}/canvas/`)
  child.on('exit', (code, signal) => {
    process.exitCode = code ?? (signal ? 1 : 0)
  })
} catch (error) {
  console.error(error instanceof Error ? error.message : String(error))
  process.exitCode = 1
}
