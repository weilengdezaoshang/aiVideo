import {
  AgentRequestSchema,
  type AgentRequest as CanvasRequest,
} from '../../shared/agent-protocol.js'

/** Captured synchronously before save/network awaits. Never includes node content. */
export type CanvasRequestSnapshot = {
  selection: readonly string[]
  highlights: readonly string[]
  nodeFingerprints?: Readonly<Record<string, string>>
  viewport: CanvasRequest['viewport']
  clientId: string
  tabId: string
}
export function captureRequestContext(snapshot: CanvasRequestSnapshot): CanvasRequestSnapshot {
  if (
    snapshot.selection.length !== snapshot.highlights.length ||
    [...snapshot.selection]
      .sort()
      .some((id, index) => id !== [...snapshot.highlights].sort()[index])
  ) {
    throw new Error('SELECTION_MISMATCH：高亮与选区不一致，请重新选择')
  }
  return structuredClone(snapshot)
}
export function buildRequestContext(input: {
  requestId: string
  conversationId: string
  documentId: string
  revision: number
  message: string
  snapshot: CanvasRequestSnapshot
}): CanvasRequest {
  return AgentRequestSchema.parse({
    schema_version: 1,
    request_id: input.requestId,
    conversation_id: input.conversationId,
    doc_id: input.documentId,
    doc_version: input.revision,
    client_ts: Date.now(),
    client_id: input.snapshot.clientId,
    tab_id: input.snapshot.tabId,
    message_text: input.message,
    selection: {
      ids: [...input.snapshot.selection],
      count: input.snapshot.selection.length,
      truncated: false,
    },
    viewport: input.snapshot.viewport,
  })
}

let tabId: string | undefined
export function requestIdentity(storage: Storage): { clientId: string; tabId: string } {
  const clientId = storage.getItem('gencanvas.agent.client-id') ?? crypto.randomUUID()
  storage.setItem('gencanvas.agent.client-id', clientId)
  return { clientId, tabId: (tabId ??= crypto.randomUUID()) }
}

/** 只比较发送时捕获的节点，保存期间换选区不会拒发。 */
export function assertRequestNodesUnchanged(
  captured: CanvasRequestSnapshot,
  current: CanvasRequestSnapshot,
): void {
  for (const [id, fingerprint] of Object.entries(captured.nodeFingerprints ?? {})) {
    if (current.nodeFingerprints?.[id] !== fingerprint) {
      throw new Error('REFERENCE_CHANGED：参考节点内容已变化，请重新发送')
    }
  }
}
