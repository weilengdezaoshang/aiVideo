import './dom.js'
import test, { afterEach } from 'node:test'
import assert from 'node:assert/strict'
import { cleanup, render, screen } from '@testing-library/react'
import { AgentPlan } from '../apps/web/canvas/agent-plan.js'
import type { AgentRun, AgentStep } from '../apps/web/canvas/agent/types.js'
afterEach(cleanup)

test('顺位过程按语义两步分组，底层关键帧保留各自状态', () => {
  const steps: AgentStep[] = ['s1_image', 's2_frame_0', 's2_video'].map((key, ordinal) => ({
    id: key,
    key,
    ordinal,
    planVersion: 1,
    title: key,
    intent: '提示词',
    tools: ['image_edit'],
    dependsOn: [],
    doneWhen: '完成',
    status: ordinal === 0 ? 'completed' : 'pending',
    summary: null,
    evidenceRefs: [],
    retries: 0,
  }))
  const run: AgentRun = {
    id: 'run',
    clientRequestId: 'request',
    status: 'running',
    message: '先改再视频',
    reply: null,
    error: null,
    modelId: 'host',
    baseRevision: 1,
    createdAt: '',
    updatedAt: '',
    modelCalls: [],
    toolCalls: [],
    plan: { goal: '先改再视频', version: 1, current: null },
    steps,
    intentTrace: {
      multi_step: true,
      steps: [
        { id: 's1', l2: 'image_edit' },
        { id: 's2', l2: 'image_to_multiframe' },
      ],
      step_results: [
        { id: 's1', l2: 'image_edit', status: 'completed' },
        { id: 's2', l2: 'image_to_multiframe', status: 'pending' },
      ],
    },
    media: {
      tier: 'preview',
      goalType: 'video',
      previewRunId: null,
      quoteCredits: 3,
      chargedCredits: 1,
      refundCredits: 0,
      completed: 1,
      total: 3,
      documentRevision: 1,
      cancelReason: null,
      children: steps.map((step, index) => ({
        stepId: step.key,
        intentStepId: index === 0 ? 's1' : 's2',
        title: step.title,
        retryCredits: 1,
        index: 0,
        status: step.status,
        assetId: null,
        errorCode: null,
        targetNode: 'node',
        attempt: 1,
      })),
    },
  }
  render(<AgentPlan run={run} />)
  assert.match(screen.getByText(/第 1 步/).textContent ?? '', /编辑图片.*已完成/)
  assert.match(screen.getByText(/第 2 步/).textContent ?? '', /生成视频.*待执行/)
  assert.ok(screen.getByText('s2_frame_0'))
})
