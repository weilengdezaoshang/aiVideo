import { z } from 'zod'

const id = z.string().min(1).max(128)
const bbox = z.tuple([
  z.number().finite(),
  z.number().finite(),
  z.number().finite(),
  z.number().finite(),
])
export const MessagePartSchema = z.discriminatedUnion('type', [
  z.object({ type: z.literal('text'), text: z.string().min(1).max(8000) }),
  // 描述字段在协议边界剥离，模型只接收服务端 Doc 的元数据。
  z.object({ type: z.literal('mention'), id }),
])
/** v1 固定采用 B：PostgreSQL documents.revision 即持久化 snapshot_seq；不是 Yjs version。 */
export const AgentRequestSchema = z
  .object({
    schema_version: z.literal(1),
    request_id: z.string().uuid(),
    conversation_id: z.string().uuid(),
    doc_id: z.string().uuid(),
    doc_version: z.number().int().nonnegative(),
    client_ts: z.number().int().positive(),
    client_id: id,
    tab_id: id,
    message: z.array(MessagePartSchema).max(200).optional(),
    message_text: z.string().max(8000).optional(),
    mentions: z.array(z.object({ id })).max(20).default([]),
    attachments: z
      .array(z.object({ type: z.enum(['image', 'file']), ref: z.string().min(1).max(512) }))
      .max(10)
      .optional(),
    selection: z.object({
      ids: z.array(id).max(200),
      count: z.number().int().nonnegative(),
      truncated: z.boolean(),
      select_all: z.boolean().optional(),
      bbox: bbox.optional(),
      selection_token: id.optional(),
    }),
    viewport: z.object({ bbox, zoom: z.number().positive().max(64) }),
    focus_frame_id: id.optional(),
    active_page_id: id.optional(),
    current_tool: z.string().max(64).optional(),
    // 身份自声明与旧上下文禁止；其他未知字段由 Zod 默认 strip。
    user_id: z.never().optional(),
    role: z.never().optional(),
    can_edit: z.never().optional(),
    recent_ops: z.never().optional(),
    history: z.never().optional(),
    canvasContext: z.never().optional(),
  })
  .superRefine((v, ctx) => {
    const text = (v.message ?? [])
      .filter((p) => p.type === 'text')
      .map((p) => p.text)
      .join('')
    if (!v.message?.length && !v.message_text?.trim()) {
      ctx.addIssue({ code: 'custom', message: 'message or message_text required' })
    }
    if (text.length + (v.message_text?.length ?? 0) > 8000) {
      ctx.addIssue({ code: 'custom', message: 'PAYLOAD_TOO_LARGE' })
    }
    if (
      !v.selection.truncated &&
      !v.selection.select_all &&
      v.selection.count !== v.selection.ids.length
    ) {
      ctx.addIssue({ code: 'custom', path: ['selection'], message: 'SELECTION_MISMATCH' })
    }
    if (new Set(v.selection.ids).size !== v.selection.ids.length) {
      ctx.addIssue({ code: 'custom', path: ['selection'], message: 'SELECTION_MISMATCH' })
    }
    const refs = new Set(v.message?.filter((p) => p.type === 'mention').map((p) => p.id))
    if (v.mentions.some((m) => !refs.has(m.id))) {
      ctx.addIssue({ code: 'custom', path: ['mentions'], message: 'mention missing from message' })
    }
  })
export type AgentRequest = z.infer<typeof AgentRequestSchema>
