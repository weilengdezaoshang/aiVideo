import assert from 'node:assert/strict'
import { test } from 'node:test'
import { fileURLToPath } from 'node:url'
import { stat } from 'node:fs/promises'
import { createApplication } from '../src/app/create-app.js'
import { reviewerOptionsFromEnv } from '../src/adapters/reviewer/factory.js'
import { MemoryTraceWriter } from '../src/adapters/memory/index.js'
import type { LoggedEvent } from '../src/domain/model.js'
import { toolName } from '../src/tools/text2image.js'

// 即使开发机有 key，普通 CI/test 也不发付费请求；必须另行显式 opt in。
const reason =
  process.env.REAL_REVIEWER_SMOKE !== '1'
    ? '真实审核冒烟需显式 REAL_REVIEWER_SMOKE=1；默认 CI 跳过'
    : !process.env.REAL_REVIEWER_API_KEY ||
        !process.env.REAL_REVIEWER_MODEL ||
        !process.env.REAL_REVIEWER_API
      ? 'MARK_SKIP_IF_NO_KEY：未配置真实审核 API Key/模型/API'
      : false

test(
  '可选真实图片审核：好夹具 accepted，Trace reviewer 为真实模型',
  { skip: reason, timeout: 20000 },
  async () => {
    const path = fileURLToPath(
      new URL('../../../docs/reviews/v0.5/visual-src/assets/tea.jpg', import.meta.url),
    )
    const bytes = (await stat(path)).size
    const traces = new MemoryTraceWriter()
    const config = reviewerOptionsFromEnv({
      ...process.env,
      REVIEWER_MODE: 'real',
      REVIEW_ENABLED: 'true',
    })
    const app = createApplication({
      ...config,
      maxReviewRetry: 0,
      llmDelayMs: 0,
      streamCharDelayMs: 0,
      ports: { traces },
      tools: [
        {
          name: toolName,
          async execute() {
            return {
              artifact: {
                kind: 'artifact',
                id: 'real-tea-fixture',
                type: 'image',
                url: 'https://fixture.invalid/tea.jpg',
                status: 'success',
              },
              credits: 0,
              metadata: {
                localPath: path,
                fixtureId: 'tea-photo',
                width: 1400,
                height: 2488,
                bytes,
              },
            }
          },
        },
      ],
    })
    const response = await app.api.request('/v1/conversation', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        prompt: '请交付附件中的茶饮照片：主体完整，保持原图竖幅比例，不要求新增文字或图形。',
      }),
    })
    assert.equal(response.status, 200)
    const events = (await response.text()).split('\n\n').flatMap((frame) => {
      const line = frame.split('\n').find((item) => item.startsWith('data:'))
      return line ? [JSON.parse(line.slice(5)) as LoggedEvent] : []
    })
    const call = traces
      .get(events[0]!.runId)!
      .turns[0]!.toolCalls.find((call) => call.name === toolName)!
    assert.equal(call.reviewOutcome, 'accepted', call.evidence?.review?.issues.join('；'))
    assert.equal(call.evidence?.review?.reviewer, process.env.REAL_REVIEWER_MODEL)
    assert.notEqual(call.evidence?.review?.reviewer, 'stub')
  },
)
