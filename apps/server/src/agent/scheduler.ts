import { z } from 'zod'
import type { ToolCall, TodoPlanStep } from '../domain/model.js'
import { RoutingError } from './intent.js'

export const dagStepSchema = z
  .object({
    id: z.string().min(1).max(128),
    title: z.string().min(1).max(200),
    capabilityId: z.string().min(1).max(128),
    toolName: z.string().min(1).max(128),
    dependsOn: z.array(z.string().min(1)).max(32),
    targetNodes: z.array(z.string().min(1)).max(32),
    tier: z.enum(['preview', 'final']),
    parallelGroup: z.string().min(1).optional(),
  })
  .strict()
export type DagStep = z.infer<typeof dagStepSchema>
export const dagSchema = z.array(dagStepSchema).min(1).max(32)

export const toolCapabilities: Readonly<Record<string, string>> = {
  creative_agent_mcp_gen_text2image_v3: 'image.text_to_image',
  image_generate: 'image.text_to_image',
  subject_cutout: 'image.subject_cutout',
  cutout: 'image.subject_cutout',
  video_generate: 'video.text_to_video',
  frames_to_video: 'video.frames_to_video',
  image_to_video: 'video.image_to_video',
}

/** Legacy calls receive a conservative serial graph; explicit DAG metadata is never repaired. */
export function executionDag(calls: readonly ToolCall[]): DagStep[] {
  return dagSchema.parse(
    calls.map((call, index) =>
      call.dag
        ? { ...call.dag, id: call.id, toolName: call.name }
        : {
            id: call.id,
            title: `创作步骤 ${index + 1}`,
            capabilityId: toolCapabilities[call.name] ?? call.name,
            toolName: call.name,
            dependsOn: index ? [calls[index - 1]!.id] : [],
            targetNodes: [`output:${call.id}`],
            tier: 'preview',
          },
    ),
  )
}

/** Kahn layers plus reachability: two unordered steps cannot write the same node. */
export function validateDag(steps: readonly DagStep[]): string[] {
  const errors: string[] = []
  const byId = new Map(steps.map((step) => [step.id, step]))
  if (byId.size !== steps.length) {
    errors.push('计划步骤 ID 必须唯一')
  }
  for (const step of steps) {
    if (new Set(step.dependsOn).size !== step.dependsOn.length) {
      errors.push(`步骤 ${step.id} 存在重复依赖`)
    }
    if (!step.targetNodes.length || new Set(step.targetNodes).size !== step.targetNodes.length) {
      errors.push(`步骤 ${step.id} 的目标节点无效`)
    }
    for (const id of step.dependsOn) {
      if (id === step.id || !byId.has(id)) {
        errors.push(`步骤 ${step.id} 的依赖 ${id} 无效`)
      }
    }
  }
  if (errors.length) {
    return errors
  }
  const ancestors = new Map<string, Set<string>>()
  const remaining = [...steps]
  while (remaining.length) {
    const index = remaining.findIndex((step) => step.dependsOn.every((id) => ancestors.has(id)))
    if (index < 0) {
      return ['计划依赖存在环']
    }
    const [step] = remaining.splice(index, 1)
    const dependencies = new Set(step!.dependsOn)
    for (const id of step!.dependsOn) {
      for (const ancestor of ancestors.get(id)!) {
        dependencies.add(ancestor)
      }
    }
    ancestors.set(step!.id, dependencies)
  }
  for (let i = 0; i < steps.length; i++) {
    for (const other of steps.slice(i + 1)) {
      const step = steps[i]!
      if (
        step.targetNodes.some((node) => other.targetNodes.includes(node)) &&
        !ancestors.get(step.id)!.has(other.id) &&
        !ancestors.get(other.id)!.has(step.id)
      ) {
        errors.push(`步骤 ${step.id} 与 ${other.id} 并行写入同一节点`)
      }
    }
  }
  return errors
}

export function readySteps(
  steps: readonly DagStep[],
  progress: readonly TodoPlanStep[],
): DagStep[] {
  const errors = validateDag(steps)
  if (errors.length) {
    throw new RoutingError('plan_invalid', errors.join('；'))
  }
  const statuses = new Map(progress.map((step) => [step.id, step.status]))
  return steps.filter(
    (step) =>
      statuses.get(step.id) === 'pending' &&
      step.dependsOn.every((id) => statuses.get(id) === 'completed'),
  )
}

/** One limiter per Host instance, shared by all runs using the same immutable binding. */
export class BindingSlots {
  private pools = new Map<string, { active: number; waiters: (() => void)[] }>()
  async execute<T>(
    key: string,
    limit: number,
    signal: AbortSignal,
    work: () => Promise<T>,
  ): Promise<T> {
    signal.throwIfAborted()
    const pool = this.pools.get(key) ?? { active: 0, waiters: [] }
    this.pools.set(key, pool)
    if (pool.active >= limit) {
      await new Promise<void>((resolve, reject) => {
        const ready = () => {
          signal.removeEventListener('abort', abort)
          resolve()
        }
        const abort = () => {
          const index = pool.waiters.indexOf(ready)
          if (index >= 0) {
            pool.waiters.splice(index, 1)
          }
          reject(signal.reason ?? new Error('已取消'))
        }
        pool.waiters.push(ready)
        signal.addEventListener('abort', abort, { once: true })
        if (signal.aborted) {
          abort()
        }
      })
    } else {
      pool.active++
    }
    try {
      signal.throwIfAborted()
      return await work()
    } finally {
      const next = pool.waiters.shift()
      // Transfer the occupied slot directly to the FIFO waiter.
      if (next) {
        next()
      } else {
        pool.active--
      }
      if (!pool.active && !pool.waiters.length) {
        this.pools.delete(key)
      }
    }
  }
}
