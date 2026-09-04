import test from 'node:test'
import assert from 'node:assert/strict'
import { buildWorkflow } from '../src/services/providers/comfyui-provider.js'
import type { GenParams } from '../src/types.js'

const params: GenParams = {
  prompt: 'p',
  negativePrompt: 'n',
  model: 'ckpt.safetensors',
  width: 768,
  height: 512,
  steps: 25,
  cfgScale: 8,
  seed: 7,
  batchCount: 1,
  sampler: 'dpmpp_2m',
  scheduler: 'karras',
  denoise: 0.6,
}

test('txt2img 工作流:EmptyLatent + denoise 固定为 1', () => {
  const wf = buildWorkflow(params, 42)
  assert.equal(wf['5'].class_type, 'EmptyLatentImage')
  assert.equal(wf['5'].inputs.width, 768)
  assert.equal(wf['3'].inputs.denoise, 1)
  assert.equal(wf['3'].inputs.sampler_name, 'dpmpp_2m')
  assert.equal(wf['3'].inputs.scheduler, 'karras')
  assert.equal(wf['3'].inputs.seed, 42)
  assert.deepEqual(wf['3'].inputs.latent_image, ['5', 0])
  assert.equal(wf['10'], undefined)
})

test('img2img 工作流:LoadImage + VAEEncode + 参数 denoise', () => {
  const wf = buildWorkflow(params, 42, 'ref_1.png')
  assert.equal(wf['10'].class_type, 'LoadImage')
  assert.equal(wf['10'].inputs.image, 'ref_1.png')
  assert.equal(wf['11'].class_type, 'VAEEncode')
  assert.equal(wf['3'].inputs.denoise, 0.6)
  assert.deepEqual(wf['3'].inputs.latent_image, ['11', 0])
  assert.equal(wf['5'], undefined)
})
