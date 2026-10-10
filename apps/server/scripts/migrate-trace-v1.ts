#!/usr/bin/env node
/**
 * 显式迁移：旧 Trace（schemaVersion:1）→ AgentTraceV1（version:"1"）
 * 不会在服务启动时自动改写。用法：
 *   npx tsx apps/server/scripts/migrate-trace-v1.ts [--dry-run] [dir]
 * 默认目录：仓库根 eval/runs
 */
import { readdir, readFile, writeFile, rename, rm, mkdir } from 'node:fs/promises'
import { join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { migrateLegacyTrace } from '../src/trace/migrate.js'

const args = process.argv.slice(2)
const dryRun = args.includes('--dry-run')
const dirArg = args.find((a) => !a.startsWith('--'))
const defaultDir = fileURLToPath(new URL('../../../eval/runs/', import.meta.url))
const directory = resolve(dirArg ?? defaultDir)

async function main() {
  await mkdir(directory, { recursive: true })
  const files = (await readdir(directory)).filter((f) => f.endsWith('.json'))
  let migrated = 0
  let skipped = 0
  for (const file of files) {
    const path = join(directory, file)
    const raw = JSON.parse(await readFile(path, 'utf8')) as { version?: string }
    if (raw.version === '1') {
      skipped += 1
      console.log(`skip (already v1): ${file}`)
      continue
    }
    const next = migrateLegacyTrace(raw)
    if (dryRun) {
      console.log(
        `dry-run would migrate: ${file} → version=1 conversationId=${next.conversationId}`,
      )
      migrated += 1
      continue
    }
    const tmp = `${path}.migrating.tmp`
    await writeFile(tmp, JSON.stringify(next, null, 2) + '\n')
    await rename(tmp, path)
    await rm(tmp, { force: true })
    migrated += 1
    console.log(`migrated: ${file}`)
  }
  console.log(`done. migrated=${migrated} skipped=${skipped} dryRun=${dryRun} dir=${directory}`)
}

main().catch((error) => {
  console.error(error)
  process.exit(1)
})
