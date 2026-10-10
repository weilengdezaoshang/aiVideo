import { createStore } from 'zustand/vanilla'
import type { Faults, Message, Part, SessionStatus, StreamEvent, Transport } from './model.js'
import { createMockTransport } from './mock.js'
import { createServerTransport } from './server-transport.js'
export interface AgentState {
  mode: 'mock' | 'server'
  setMode(mode: 'mock' | 'server'): void
  messageIds: string[]
  messagesById: Record<string, Message>
  status: SessionStatus
  draft: string
  draftBackup: string
  toast: string
  confirming: boolean
  faults: Faults
  setDraft(text: string): void
  setFault(key: keyof Faults, value: boolean): void
  dismissToast(): void
  send(text?: string, complex?: boolean, source?: 'mock' | 'server'): void
  stop(): void
  confirm(accept: boolean): Promise<void>
}
export function createAgentStore(transport?: Transport) {
  const mock = createMockTransport()
  const server = transport ?? createServerTransport()
  let active: {
    runId: string
    messageId: string
    controller: AbortController
    transport: Transport
    textAllowed: boolean
  } | null = null
  let serial = 0
  return createStore<AgentState>((set, get) => {
    const update = (id: string, change: (message: Message) => Message) =>
      set((s) => ({ messagesById: { ...s.messagesById, [id]: change(s.messagesById[id]) } }))
    const appendText = (parts: Part[], text: string): Part[] => {
      const index = parts.findIndex((p) => p.kind === 'text')
      return index < 0
        ? [...parts, { kind: 'text', text }]
        : parts.map((p, i) =>
            i === index && p.kind === 'text' ? { ...p, text: p.text + text } : p,
          )
    }
    const finish = (status: Message['status'], error?: string) => {
      if (!active) {
        return
      }
      const previous = active
      active = null
      previous.controller.abort()

      update(previous.messageId, (m) => ({
        ...m,
        status,
        parts: [
          ...m.parts.map((p): Part =>
            p.kind === 'tool_status' && p.state === 'loading'
              ? {
                  ...p,
                  state: status === 'cancelled' ? 'cancelled' : 'failed',
                  label: status === 'cancelled' ? '已停止' : '生成失败',
                }
              : p.kind === 'plan' &&
                  (status === 'cancelled' ||
                    status === 'failed' ||
                    m.parts.some((part) => part.kind === 'confirm' && part.state === 'rejected'))
                ? {
                    ...p,
                    steps: p.steps.map((step) =>
                      step.status === 'in_progress'
                        ? { ...step, status: 'cancelled' as const }
                        : step,
                    ),
                  }
                : p.kind === 'confirm' && p.state === 'pending'
                  ? { ...p, state: 'expired' }
                  : p,
          ),
          ...(error ? [{ kind: 'error' as const, message: error }] : []),
        ],
      }))
      set({ status: 'idle', confirming: false })
    }
    const receive = (event: StreamEvent) => {
      if (!active || active.runId !== event.runId || active.controller.signal.aborted) {
        return
      }
      const id = active.messageId
      switch (event.type) {
        case 'stream_start':
          set({ status: 'streaming' })
          break
        case 'text_phase':
          active.textAllowed = true
          break
        case 'text_delta':
          if (active.textAllowed) {
            update(id, (m) => ({ ...m, parts: appendText(m.parts, event.text) }))
          }
          break
        case 'tool_start':
          active.textAllowed = false
          update(id, (m) => ({
            ...m,
            parts: [
              ...m.parts,
              {
                kind: 'tool_status',
                callId: event.callId,
                toolName: '生图',
                label: event.label,
                state: 'loading',
              },
            ],
          }))
          break
        case 'tool_success':
          update(id, (m) => ({
            ...m,
            parts: [
              ...m.parts.map((p) =>
                p.kind === 'tool_status' && p.callId === event.callId
                  ? { ...p, state: 'completed' as const, label: '生成完成' }
                  : p,
              ),
              event.artifact,
            ],
          }))
          break
        case 'tool_failure':
          update(id, (m) => ({
            ...m,
            parts: m.parts.map((p) =>
              p.kind === 'tool_status' && p.callId === event.callId
                ? { ...p, state: 'failed' as const }
                : p,
            ),
          }))
          finish('failed', event.message)
          break
        case 'plan_update':
          update(id, (m) => ({
            ...m,
            parts: m.parts.some((p) => p.kind === 'plan')
              ? m.parts.map((p) =>
                  p.kind === 'plan'
                    ? { ...p, steps: event.steps, explanation: event.explanation ?? p.explanation }
                    : p,
                )
              : [
                  ...m.parts,
                  {
                    kind: 'plan',
                    explanation: event.explanation ?? '分阶段完成你的创作需求',
                    steps: event.steps,
                  },
                ],
          }))
          break
        case 'confirm_required':
          update(id, (m) => ({
            ...m,
            parts: m.parts.some(
              (p) => p.kind === 'confirm' && p.confirmId === event.confirm.confirmId,
            )
              ? m.parts.map((p) =>
                  p.kind === 'confirm' && p.confirmId === event.confirm.confirmId
                    ? event.confirm
                    : p,
                )
              : [...m.parts, event.confirm],
          }))
          set({ status: 'awaiting_user', confirming: false })
          break
        case 'stream_complete':
          finish(event.status ?? 'completed')
          break
        case 'message_snapshot':
          update(id, (m) => ({ ...m, status: event.message.status, parts: event.message.parts }))
          if (!event.message.parts.some((p) => p.kind === 'confirm' && p.state === 'pending')) {
            set({ confirming: false })
          }
          break
        case 'stream_error':
          finish('failed', event.message)
          break
      }
    }
    return {
      mode: transport ? 'mock' : 'server',
      setMode: (mode) => {
        if (get().status === 'idle') {
          set({ mode })
        }
      },
      messageIds: [],
      messagesById: {},
      status: 'idle',
      draft: '',
      draftBackup: '',
      toast: '',
      confirming: false,
      faults: { connection: false, tool: false, resume: false },
      setDraft: (draft) => set({ draft }),
      setFault: (key, value) => set((s) => ({ faults: { ...s.faults, [key]: value } })),
      dismissToast: () => set({ toast: '' }),
      send(text = get().draft, complex = /30|三个|3个|广告/.test(text), source = get().mode) {
        if (get().status !== 'idle' || !text.trim()) {
          return
        }
        const runId = `run-${++serial}`
        const userId = `${runId}-user`
        const messageId = `${runId}-assistant`
        const controller = new AbortController()
        const selected = transport ?? (source === 'mock' ? mock : server)
        active = { runId, messageId, controller, textAllowed: true, transport: selected }
        set((s) => ({
          mode: source,
          status: 'sending',
          draftBackup: text,
          draft: '',
          toast: '',
          messageIds: [...s.messageIds, userId, messageId],
          messagesById: {
            ...s.messagesById,
            [userId]: {
              id: userId,
              role: 'user',
              status: 'completed',
              parts: [{ kind: 'text', text: text.trim() }],
            },
            [messageId]: { id: messageId, role: 'assistant', status: 'streaming', parts: [] },
          },
        }))
        void selected
          .start(runId, complex, controller.signal, receive, { ...get().faults }, text)
          .catch((error: unknown) => {
            if (active?.runId !== runId) {
              return
            }
            const connection = get().status === 'sending'
            const message = error instanceof Error ? error.message : '执行失败'
            finish('failed', message)
            set({ toast: message, ...(connection ? { draft: get().draftBackup } : {}) })
          })
      },
      stop: () => {
        const previous = active
        finish('cancelled')
        previous?.transport.stop(previous.runId)
      },
      async confirm(accept) {
        if (!active || get().status !== 'awaiting_user' || get().confirming) {
          return
        }
        const current = active
        const card = get().messagesById[current.messageId].parts.find(
          (p) => p.kind === 'confirm' && p.state === 'pending',
        )
        if (!card || card.kind !== 'confirm') {
          return
        }
        const changeCard = (state: typeof card.state) =>
          update(current.messageId, (m) => ({
            ...m,
            parts: m.parts.map((p) =>
              p.kind === 'confirm' && p.confirmId === card.confirmId ? { ...p, state } : p,
            ),
          }))
        if (!accept && !current.transport.remoteInterrupts) {
          changeCard('rejected')
          update(current.messageId, (m) => ({
            ...m,
            parts: appendText(m.parts, '\n已取消，未消耗积分。'),
          }))
          finish('completed')
          return
        }
        set({ confirming: true })
        const faults = { ...get().faults }
        set((s) => ({ faults: { ...s.faults, resume: false } }))
        try {
          await current.transport.resumeInterrupt(
            current.runId,
            card.confirmId,
            current.controller.signal,
            faults,
            accept,
          )
          if (active !== current || current.transport.remoteInterrupts) {
            return
          }
          changeCard('accepted')
          set({ status: 'streaming', confirming: false })
        } catch (error: unknown) {
          if (active !== current) {
            return
          }
          set({
            confirming: false,
            toast: error instanceof Error ? error.message : '继续执行失败，请重试。',
          })
        }
      },
    }
  })
}
export const agentStore = createAgentStore()
