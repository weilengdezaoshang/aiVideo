import { mkdir, rename, writeFile, rm } from 'node:fs/promises'
import { join } from 'node:path'
import { randomUUID } from 'node:crypto'
import type { TraceWriter } from '../ports/index.js'
import { agentTraceV1Schema, type AgentTraceV1 } from './schema.js'

export class FileTraceWriter implements TraceWriter {
  constructor(private directory: string) {}
  async write(trace: AgentTraceV1) {
    const validated = agentTraceV1Schema.parse(trace)
    await mkdir(this.directory, { recursive: true })
    const path = join(this.directory, `${validated.runId}.json`)
    const temporary = `${path}.${randomUUID()}.tmp`
    try {
      await writeFile(temporary, JSON.stringify(validated, null, 2) + '\n', { flag: 'wx' })
      await rename(temporary, path)
    } finally {
      await rm(temporary, { force: true })
    }
    return { path }
  }
}
