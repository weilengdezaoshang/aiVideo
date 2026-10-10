import { randomUUID } from 'node:crypto'
import { setTimeout as sleep } from 'node:timers/promises'
import type { Tool } from '../ports/index.js'
export const toolName = 'creative_agent_mcp_gen_text2image_v3'
export function createText2Image(baseUrl: string, delayMs = 700, credits = 40): Tool {
  return {
    name: toolName,
    estimateCredits: credits,
    async execute(_input, signal) {
      await sleep(delayMs, undefined, { signal })
      return {
        artifact: {
          kind: 'artifact',
          id: randomUUID(),
          type: 'image',
          url: `${baseUrl}/mock/image.svg`,
          status: 'success',
        },
        credits,
      }
    },
  }
}
export const placeholderSvg = `<svg xmlns="http://www.w3.org/2000/svg" width="560" height="700"><rect width="560" height="700" fill="#73543c"/><text x="45" y="100" fill="#f6e9cf" font-size="22" font-family="sans-serif">CREATIVE / COFFEE</text><text x="45" y="180" fill="#f6e9cf" font-size="48" font-family="sans-serif">让日常慢下来</text><ellipse cx="280" cy="540" rx="165" ry="35" fill="#201b17" opacity=".25"/><path d="M165 320 H355 L335 510 Q260 555 185 510 Z" fill="#f6e9cf"/><ellipse cx="260" cy="320" rx="95" ry="24" fill="#302219"/><text x="45" y="640" fill="#f6e9cf" font-size="20" font-family="sans-serif">一杯好咖啡，一段好时光。</text></svg>`
