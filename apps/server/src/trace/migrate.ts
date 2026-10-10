import type { AgentTraceV1, LegacyTrace } from './schema.js'
import { agentTraceV1Schema, legacyTraceSchema } from './schema.js'

function eventType(raw: unknown): string {
  if (!raw || typeof raw !== 'object') {
    return 'unknown'
  }
  const e = raw as Record<string, unknown>
  if (e.kind === 'system' && typeof e.type === 'string') {
    return e.type
  }
  if (e.kind === 'text_delta') {
    return 'text_delta'
  }
  if (e.kind === 'message') {
    return 'message'
  }
  if (typeof e.type === 'string') {
    return e.type
  }
  return 'unknown'
}

function eventSeq(raw: unknown, fallback: number): number {
  if (
    raw &&
    typeof raw === 'object' &&
    typeof (raw as { sequence?: unknown }).sequence === 'number'
  ) {
    return (raw as { sequence: number }).sequence
  }
  if (raw && typeof raw === 'object' && typeof (raw as { seq?: unknown }).seq === 'number') {
    return (raw as { seq: number }).seq
  }
  return fallback
}

function eventAt(raw: unknown, fallback: string): string {
  if (raw && typeof raw === 'object') {
    const o = raw as { timestamp?: string; at?: string }
    if (typeof o.timestamp === 'string') {
      return o.timestamp
    }
    if (typeof o.at === 'string') {
      return o.at
    }
  }
  return fallback
}

/** 将旧 schemaVersion:1 / 缺 version 的 Trace 转为 AgentTraceV1 */
export function migrateLegacyTrace(input: unknown): AgentTraceV1 {
  if (input && typeof input === 'object' && (input as { version?: unknown }).version === '1') {
    return agentTraceV1Schema.parse(input)
  }
  const legacy = legacyTraceSchema.parse(input) as LegacyTrace
  const events = Array.isArray(legacy.events) ? legacy.events : []
  const spans = Array.isArray(legacy.spans) ? legacy.spans : []
  const toolCalls = spans
    .filter((s): s is Record<string, unknown> => !!s && typeof s === 'object')
    .filter((s) => typeof s.name === 'string' && s.name !== 'llm.host')
    .map((s, i) => ({
      id: `legacy-tool-${i}`,
      agent: legacy.agent || 'host',
      name: String(s.name),
      args: {},
      resultSummary: typeof s.detail === 'string' ? s.detail : String(s.status ?? ''),
      status:
        s.status === 'failed'
          ? ('failed' as const)
          : s.status === 'cancelled'
            ? ('cancelled' as const)
            : ('finished' as const),
      credits: i === 0 ? legacy.credits : 0,
    }))
  const started = Date.parse(legacy.startedAt)
  const ended = Date.parse(legacy.endedAt)
  const latencyMs =
    Number.isFinite(started) && Number.isFinite(ended) ? Math.max(0, ended - started) : 0
  const outcome =
    legacy.status === 'completed'
      ? ('success' as const)
      : legacy.status === 'cancelled'
        ? ('cancelled' as const)
        : ('failed' as const)
  const trace: AgentTraceV1 = {
    version: '1',
    runId: legacy.runId,
    conversationId: legacy.conversation_id,
    startedAt: legacy.startedAt,
    finishedAt: legacy.endedAt,
    turns: [
      {
        turnId: legacy.turn_uuid,
        agents: [{ name: legacy.agent || 'host', steps: Math.max(1, toolCalls.length + 1) }],
        events: events.map((e, i) => ({
          seq: eventSeq(e, i + 1),
          type: eventType(e),
          at: eventAt(e, legacy.startedAt),
        })),
        toolCalls,
        interrupts: [],
        outcome,
        metrics: {
          steps: Math.max(1, toolCalls.length + 1),
          credits: legacy.credits,
          latencyMs,
        },
      },
    ],
  }
  return agentTraceV1Schema.parse(trace)
}
