import { Hono } from 'hono'
import { cors } from 'hono/cors'
import { streamSSE } from 'hono/streaming'
import { bodyLimit } from 'hono/body-limit'
import { z } from 'zod'
import {
  RateLimitError,
  ResumeError,
  type ConversationService,
} from '../domain/conversation-service.js'
const canvasSchema = z
  .object({
    projectId: z.string(),
    nodeId: z.string().optional(),
    nodeType: z.string().optional(),
    assetUrls: z.array(z.string()).max(16).optional(),
    assetVersions: z.array(z.string()).optional(),
    hasImage: z.boolean(),
    selectedGroupIds: z.array(z.string()).optional(),
    width: z.number().positive().optional(),
    height: z.number().positive().optional(),
  })
  .strict()
const conversationSchema = z.union([
  z
    .object({
      prompt: z.string().trim().min(1).max(4000),
      conversation_id: z.uuid().optional(),
      imageRefs: z.array(z.string().min(1)).max(16).optional(),
      canvas: canvasSchema.optional(),
    })
    .strict(),
  z
    .object({
      action: z.literal('resume_interrupt'),
      conversation_id: z.uuid(),
      confirmId: z.string().min(1),
      approved: z.boolean(),
      clarification: z
        .object({
          imageRefs: z.array(z.string().min(1)).min(1).max(1).optional(),
          goal: z.enum(['image', 'video']).optional(),
        })
        .strict()
        .optional(),
    })
    .strict(),
])
const stopSchema = z.object({ conversation_id: z.uuid(), turn_uuid: z.uuid() }).strict()
export function createApi(service: ConversationService, origins: string[], placeholder: string) {
  const api = new Hono()
  api.use(
    '*',
    cors({
      origin: (origin) => (origins.includes(origin) ? origin : null),
      allowMethods: ['POST', 'GET', 'OPTIONS'],
      allowHeaders: ['Content-Type'],
    }),
  )
  api.use(
    '/v1/*',
    bodyLimit({ maxSize: 32 * 1024, onError: (c) => c.json({ error: '请求体过大' }, 413) }),
  )
  api.get('/v1/capabilities', (c) => c.json({ capabilities: service.capabilities() }))
  api.get('/health', (c) => c.json({ status: 'ok', stage: 'S3', provider: 'stub' }))
  api.get('/mock/image.svg', (c) => {
    c.header('Content-Type', 'image/svg+xml')
    return c.body(placeholder)
  })
  api.post('/v1/conversation', async (c) => {
    const parsed = conversationSchema.safeParse(await c.req.json().catch(() => null))
    if (!parsed.success) {
      return c.json({ error: '请求格式不合法', issues: parsed.error.issues }, 400)
    }
    try {
      const prepared =
        'action' in parsed.data
          ? await service.prepareResume(
              parsed.data.conversation_id,
              parsed.data.confirmId,
              parsed.data.approved,
              parsed.data.clarification,
            )
          : {
              turn: await service.prepare(
                parsed.data.prompt,
                parsed.data.conversation_id,
                parsed.data.imageRefs,
                { canvas: parsed.data.canvas },
              ),
              resume: undefined,
            }
      const { turn, resume } = prepared
      return streamSSE(c, async (stream) => {
        let finished = false
        const stop = () => {
          if (!finished && turn.status === 'running') {
            void service.disconnect(turn.conversationId, turn.turnId).catch(console.error)
          }
        }
        stream.onAbort(stop)
        const requestSignal = c.req.raw.signal
        requestSignal.addEventListener('abort', stop, { once: true })
        if (requestSignal.aborted) {
          stop()
        }
        try {
          await service.run(
            turn,
            async (event) => {
              await stream.writeSSE({
                event: event.kind,
                id: `${event.runId}:${event.sequence}`,
                data: JSON.stringify(event),
              })
            },
            resume,
          )
        } finally {
          finished = true
          requestSignal.removeEventListener('abort', stop)
        }
      })
    } catch (error: unknown) {
      if (error instanceof ResumeError) {
        return c.json({ error: error.message }, error.status)
      }
      if (error instanceof RateLimitError) {
        c.header('Retry-After', String(Math.ceil(error.retryAfterMs / 1000)))
        return c.json({ error: error.message, retryAfterMs: error.retryAfterMs }, 429)
      }
      console.error(error)
      return c.json({ error: '无法创建创作会话' }, 500)
    }
  })
  api.post('/v1/stop_stream', async (c) => {
    const parsed = stopSchema.safeParse(await c.req.json().catch(() => null))
    if (!parsed.success) {
      return c.json({ error: '请求格式不合法' }, 400)
    }
    const turn = await service.stop(parsed.data.conversation_id, parsed.data.turn_uuid)
    return turn
      ? c.json({
          conversation_id: turn.conversationId,
          turn_uuid: turn.turnId,
          status: turn.status,
        })
      : c.json({ error: '会话轮次不存在' }, 404)
  })
  return api
}
