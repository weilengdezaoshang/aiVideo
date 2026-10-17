import { intentResultSchema } from '../agent/intent.js'
import { planValidationSchema } from '../agent/plan.js'
import { z } from 'zod'
import { evidenceSchema } from '../agent/reflection.js'

/** 冻结的 AgentTraceV1（PORTS_AND_TRACE §5） */
export const agentTraceTurnV1Schema = z.object({
  turnId: z.string().min(1),
  intent: intentResultSchema.optional(),
  planValidation: planValidationSchema.optional(),
  agents: z.array(
    z.object({
      name: z.string().min(1),
      steps: z.number().int().nonnegative(),
    }),
  ),
  events: z.array(
    z.object({
      seq: z.number().int().nonnegative(),
      type: z.string(),
      at: z.string(),
    }),
  ),
  toolCalls: z.array(
    z.object({
      id: z.string().min(1),
      agent: z.string().min(1),
      name: z.string().min(1),
      args: z.unknown(),
      resultSummary: z.string(),
      status: z.enum([
        'streaming',
        'invoking',
        'finished',
        'failed',
        'cancelled',
        'wait_user_response',
      ]),
      credits: z.number().nonnegative().optional(),
      jobId: z.string().optional(),
      capabilityId: z.string().optional(),
      tier: z.enum(['preview', 'final']).optional(),
      bindingVersion: z.string().optional(),
      adapter: z.enum(['mock', 'cloud', 'comfyui']).optional(),
      modelId: z.string().optional(),
      dependsOn: z.array(z.string()).optional(),
      targetNodes: z.array(z.string()).optional(),
      parallelGroup: z.string().optional(),
      evidence: evidenceSchema.optional(),
      reviewHistory: z.array(evidenceSchema).optional(),
      reviewAttempts: z.number().int().nonnegative().optional(),
      reviewOutcome: z.enum(['accepted', 'retried', 'escalated']).optional(),
    }),
  ),
  interrupts: z.array(
    z.object({
      confirmId: z.string().optional(),
      reason: z.string(),
      resumed: z.boolean(),
    }),
  ),
  outcome: z.enum(['success', 'failed', 'cancelled', 'interrupted']),
  metrics: z.object({
    steps: z.number().int().nonnegative(),
    credits: z.number().nonnegative(),
    latencyMs: z.number().nonnegative(),
  }),
})

export const agentTraceV1Schema = z.object({
  version: z.literal('1'),
  runId: z.string().min(1),
  conversationId: z.string().min(1),
  gitSha: z.string().optional(),
  startedAt: z.string(),
  finishedAt: z.string(),
  turns: z.array(agentTraceTurnV1Schema).min(1),
})

export type AgentTraceTurnV1 = z.infer<typeof agentTraceTurnV1Schema>
export type AgentTraceV1 = z.infer<typeof agentTraceV1Schema>

/** @deprecated 旧落盘格式；仅供迁移工具识别 */
export const legacyTraceSchema = z.object({
  schemaVersion: z.literal(1),
  runId: z.string(),
  conversation_id: z.string(),
  turn_uuid: z.string(),
  agent: z.string(),
  prompt: z.string(),
  startedAt: z.string(),
  endedAt: z.string(),
  status: z.enum(['completed', 'failed', 'cancelled']),
  credits: z.number(),
  spans: z.array(z.unknown()).optional(),
  events: z.array(z.unknown()).optional(),
  output: z.unknown().optional(),
  error: z.string().optional(),
})

export type LegacyTrace = z.infer<typeof legacyTraceSchema>

/** 兼容旧测试名 */
export const traceSchema = agentTraceV1Schema
export type Trace = AgentTraceV1
