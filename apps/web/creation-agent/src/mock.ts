import type { EventPayload, Step, Transport } from './model.js'
export function delay(ms: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    if (signal.aborted) {
      reject(new DOMException('已停止', 'AbortError'))
      return
    }
    const abort = () => {
      clearTimeout(timer)
      reject(new DOMException('已停止', 'AbortError'))
    }
    const timer = setTimeout(() => {
      signal.removeEventListener('abort', abort)
      resolve()
    }, ms)
    signal.addEventListener('abort', abort, { once: true })
  })
}
export function createMockTransport(speed = 1): Transport {
  type Progress = { complex: boolean; index: number; hasPlan: boolean }
  const runs = new Map<string, Progress>()
  let draft: Progress | undefined
  const gates = new Map<string, () => void>()
  return {
    async start(runId, complex, signal, emit, faults, prompt) {
      const continuing =
        draft &&
        /^(继续|接着做|接着|继续执行|继续生成|继续上次|resume)[。.!！]?$/i.test(
          prompt?.trim() ?? '',
        )
      const progress = continuing ? { ...draft! } : { complex, index: 0, hasPlan: false }
      draft = undefined
      complex = progress.complex
      runs.set(runId, progress)
      const send = (event: EventPayload) => {
        if (!signal.aborted) {
          emit({ ...event, runId })
        }
      }
      const wait = (ms: number) => delay(ms * speed, signal)
      const text = async (value: string) => {
        for (const char of value) {
          await wait(45)
          send({ type: 'text_delta', text: char })
        }
      }
      const steps = (active: number): Step[] =>
        ['策划广告与镜头', '生成三个镜头画面', '整理交付作品'].map((text, i) => ({
          id: `step-${i}`,
          text,
          status: i < active ? 'completed' : i === active ? 'in_progress' : 'pending',
        }))
      try {
        await wait(450)
        if (faults.connection) {
          throw new Error('连接失败，请稍后重试。')
        }
        send({ type: 'stream_start' })
        await text(
          continuing
            ? '接着上次计划继续。'
            : complex
              ? '交给策划。我会先拆解镜头，再为你生成咖啡广告画面。'
              : '好的，为你制作一张温暖、简洁的咖啡海报。',
        )
        if (complex) {
          progress.hasPlan = true
          if (!continuing) {
            send({ type: 'plan_update', steps: steps(0) })
            await wait(800)
          }
          send({ type: 'plan_update', steps: steps(1) })
          await new Promise<void>((resolve, reject) => {
            const abort = () => {
              gates.delete(runId)
              reject(new DOMException('已停止', 'AbortError'))
            }
            signal.addEventListener('abort', abort, { once: true })
            gates.set(runId, () => {
              signal.removeEventListener('abort', abort)
              gates.delete(runId)
              resolve()
            })
            send({
              type: 'confirm_required',
              confirm: {
                kind: 'confirm',
                confirmId: `${runId}-credits`,
                title: '确认生成三个广告镜头',
                estimateCredits: (3 - progress.index) * 40,
                summary: '生成 3 张镜头画面：咖啡豆特写、咖啡冲煮、晨光中的咖啡杯。',
                state: 'pending',
              },
            })
          })
        }
        for (let i = progress.index + 1; i <= (complex ? 3 : 1); i++) {
          const callId = `${runId}-tool-${i}`
          send({
            type: 'tool_start',
            callId,
            label: complex ? `正在生成镜头 ${i}` : '正在生成咖啡海报',
          })
          await wait(1400)
          if (faults.tool) {
            send({ type: 'tool_failure', callId, message: '生成失败，请重新发送需求。' })
            return
          }
          send({
            type: 'tool_success',
            callId,
            artifact: {
              kind: 'artifact',
              id: `${runId}-art-${i}`,
              type: 'image',
              url: `/coffee-${i}.svg`,
              status: 'success',
            },
          })
          progress.index = i
          await wait(400)
        }
        if (complex) {
          send({ type: 'plan_update', steps: steps(2) })
          await wait(450)
          send({ type: 'plan_update', steps: steps(3) })
          send({ type: 'text_phase' })
          await text('\n三个镜头画面已完成，可点击作品放大查看。')
        }
        send({ type: 'stream_complete' })
      } finally {
        gates.delete(runId)
        if (signal.aborted && progress.hasPlan && progress.index < 3) {
          draft = { ...progress }
        }
        runs.delete(runId)
      }
    },
    async resumeInterrupt(runId, _confirmId, signal, faults) {
      await delay(650 * speed, signal)
      if (faults.resume) {
        throw new Error('继续执行失败，请再次确认。')
      }
      // 下一次微任务才继续事件流，让调用者先写入 accepted 状态。
      const gate = gates.get(runId)
      if (!gate) {
        throw new Error('确认已失效。')
      }
      setTimeout(gate, 0)
    },
    stop(runId) {
      const progress = runs.get(runId)
      if (progress?.hasPlan && progress.index < 3) {
        draft = { ...progress }
      }
      gates.delete(runId)
    },
  }
}
