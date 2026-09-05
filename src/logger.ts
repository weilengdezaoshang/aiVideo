type LogLevel = 'debug' | 'info' | 'warn' | 'error'

const LEVEL_ORDER: Record<LogLevel, number> = { debug: 10, info: 20, warn: 30, error: 40 }

const envLevel = process.env.LOG_LEVEL as LogLevel | undefined
const threshold = envLevel && envLevel in LEVEL_ORDER ? LEVEL_ORDER[envLevel] : LEVEL_ORDER.info

/** Error 直接 stringify 会变成 {},统一展开为 name/message/stack 方便排障。 */
function errorReplacer(_key: string, value: unknown): unknown {
  return value instanceof Error
    ? { name: value.name, message: value.message, stack: value.stack }
    : value
}

function emit(level: LogLevel, message: string, extra?: Record<string, unknown>): void {
  if (LEVEL_ORDER[level] < threshold) {
    return
  }
  const suffix = extra === undefined ? '' : ` ${JSON.stringify(extra, errorReplacer)}`
  const line = `${new Date().toISOString()} ${level.toUpperCase().padEnd(5)} ${message}${suffix}`
  if (level === 'error') {
    console.error(line)
  } else if (level === 'warn') {
    console.warn(line)
  } else {
    console.log(line)
  }
}

/** 极简结构化日志:LOG_LEVEL 环境变量控制级别,默认 info。 */
export const logger: Record<LogLevel, (message: string, extra?: Record<string, unknown>) => void> =
  {
    debug: (message, extra) => emit('debug', message, extra),
    info: (message, extra) => emit('info', message, extra),
    warn: (message, extra) => emit('warn', message, extra),
    error: (message, extra) => emit('error', message, extra),
  }
