import { z } from 'zod'
import type { Tool } from '../ports/index.js'
import type { DagStep } from './scheduler.js'
import { toolCapabilities } from './scheduler.js'

export const capabilitySchema = z
  .object({
    id: z.string().min(1),
    enabled: z.boolean(),
    outputType: z.enum(['image', 'video']),
    tiers: z.array(z.enum(['preview', 'final'])).min(1),
    maxConcurrency: z.number().int().min(1).max(32),
    maxRefImages: z.number().int().min(0).max(16),
    binding: z
      .object({
        version: z.string().min(1),
        toolName: z.string().min(1),
        adapter: z.enum(['mock', 'cloud', 'comfyui']),
        modelId: z.string().min(1),
        credentialRef: z.string().min(1).optional(),
      })
      .strict()
      .optional(),
  })
  .strict()
export type Capability = z.infer<typeof capabilitySchema>
export class CapabilityError extends Error {
  constructor(
    readonly code:
      'capability_unavailable' | 'capability_unbound' | 'spec_not_allowed' | 'prod_mock',
    message: string,
  ) {
    super(message)
  }
}

/** Deployment bindings stay server-side; the public menu exposes availability and limits. */
export class CapabilityCatalog {
  private entries: Map<string, Capability>
  constructor(
    entries: readonly Capability[],
    private production = false,
  ) {
    const parsed = entries.map((entry) => capabilitySchema.parse(entry))
    this.entries = new Map(parsed.map((entry) => [entry.id, entry]))
    if (this.entries.size !== parsed.length) {
      throw new Error('能力目录 ID 重复')
    }
    for (const entry of parsed) {
      if (production && entry.enabled && entry.binding?.adapter === 'mock') {
        throw new CapabilityError('prod_mock', '生产环境禁止 Mock 能力绑定')
      }
    }
  }
  public() {
    return [...this.entries.values()].map(({ id, enabled, tiers, outputType, maxRefImages }) => ({
      id,
      enabled: enabled && !!this.entries.get(id)?.binding,
      tiers,
      outputType,
      maxRefImages,
    }))
  }
  resolve(
    step: DagStep,
    refs: number,
  ): Capability & { binding: NonNullable<Capability['binding']> } {
    const entry = this.entries.get(step.capabilityId)
    if (!entry?.enabled) {
      throw new CapabilityError('capability_unavailable', '该创作能力暂不可用')
    }
    if (!entry.binding || entry.binding.toolName !== step.toolName) {
      throw new CapabilityError('capability_unbound', '该创作能力尚未绑定可执行工具')
    }
    if (this.production && (entry.binding.adapter === 'mock' || !entry.binding.credentialRef)) {
      throw new CapabilityError('prod_mock', '生产能力必须使用真实绑定与凭据引用')
    }
    if (!entry.tiers.includes(step.tier) || refs > entry.maxRefImages) {
      throw new CapabilityError('spec_not_allowed', '参考图数量或生成阶段超出允许范围')
    }
    return { ...entry, binding: entry.binding }
  }
}

/** Local fixture catalog is explicit and never usable to bootstrap production. */
export function developmentCatalog(tools: readonly Tool[]): CapabilityCatalog {
  const entries = new Map<string, Capability>()
  for (const tool of tools) {
    const id = toolCapabilities[tool.name] ?? tool.name
    if (entries.has(id)) {
      continue
    }
    entries.set(id, {
      id,
      enabled: true,
      outputType: id.startsWith('video.') ? 'video' : 'image',
      tiers: ['preview', 'final'],
      maxConcurrency: tool.parallelSafe ? 4 : 1,
      maxRefImages: 16,
      binding: {
        version: 'development-v1',
        toolName: tool.name,
        adapter: tool.adapter ?? 'mock',
        modelId: 'fixture',
      },
    })
  }
  return new CapabilityCatalog([...entries.values()])
}
