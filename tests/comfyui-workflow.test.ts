import test from 'node:test'
import assert from 'node:assert/strict'
import {
  buildLtxvVideoWorkflow,
  buildWanVideoWorkflow,
  buildWorkflow,
} from '../src/services/providers/comfyui-provider.js'
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
  kind: 'image',
  durationSec: 4,
  fps: 16,
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

const videoParams: GenParams = { ...params, kind: 'video', durationSec: 4, fps: 16 }

test('LTXV 图生视频:ImgToVideo → 帧率条件 → KSampler → SaveVideo', () => {
  const wf = buildLtxvVideoWorkflow(videoParams, 42, 'ltx-video-2b.safetensors', 'ref_1.png')
  assert.equal(wf['10'].class_type, 'LoadImage')
  assert.equal(wf['12'].class_type, 'LTXVImgToVideo')
  // 帧数 = 4s * 16fps = 64,规整为 8n+1 = 65
  assert.equal(wf['12'].inputs.length, 65)
  assert.equal(wf['12'].inputs.start_image, undefined)
  // 帧率条件作用在 ImgToVideo 输出的条件上,latent 取其第 3 个输出
  assert.deepEqual(wf['13'].inputs.positive, ['12', 0])
  assert.deepEqual(wf['13'].inputs.negative, ['12', 1])
  assert.equal(wf['13'].inputs.frame_rate, 16)
  assert.deepEqual(wf['3'].inputs.positive, ['13', 0])
  assert.deepEqual(wf['3'].inputs.latent_image, ['12', 2])
  assert.equal(wf['24'].class_type, 'CreateVideo')
  assert.deepEqual(wf['24'].inputs.images, ['8', 0])
  assert.equal(wf['25'].class_type, 'SaveVideo')
  assert.deepEqual(wf['25'].inputs.video, ['24', 0])
})

test('LTXV 文生视频:EmptyLTXVLatentVideo + 文本条件直接进入帧率节点', () => {
  const wf = buildLtxvVideoWorkflow(videoParams, 42, 'ltx-video-2b.safetensors')
  assert.equal(wf['12'].class_type, 'EmptyLTXVLatentVideo')
  assert.deepEqual(wf['13'].inputs.positive, ['6', 0])
  assert.deepEqual(wf['3'].inputs.latent_image, ['12', 0])
  assert.equal(wf['10'], undefined)
})

test('LTXV 尺寸按 32 的倍数就近规整', () => {
  const wf = buildLtxvVideoWorkflow({ ...videoParams, width: 520, height: 300 }, 1, 'm')
  assert.equal(wf['12'].inputs.width, 512)
  assert.equal(wf['12'].inputs.height, 288)
})

test('Wan 单模型图生视频:UNETLoader + CLIPLoader(wan) + WanImageToVideo', () => {
  const wf = buildWanVideoWorkflow(
    { ...videoParams, steps: 12, width: 520, height: 512 },
    42,
    { model: 'wan_i2v.safetensors', clip: 'umt5.safetensors', vae: 'wan_vae.safetensors' },
    'ref_1.png',
  )
  assert.equal(wf['1'].class_type, 'UNETLoader')
  assert.equal(wf['2'].inputs.type, 'wan')
  assert.equal(wf['2'].inputs.clip_name, 'umt5.safetensors')
  // 520 规整为 16 的倍数 528;帧数 64 已是 4 的倍数
  assert.equal(wf['12'].inputs.width, 528)
  assert.equal(wf['12'].inputs.length, 64)
  assert.deepEqual(wf['12'].inputs.start_image, ['10', 0])
  assert.deepEqual(wf['14'].inputs.model, ['1', 0])
  assert.deepEqual(wf['22'].inputs.model, ['14', 0])
  assert.deepEqual(wf['22'].inputs.positive, ['12', 0])
  assert.deepEqual(wf['22'].inputs.latent_image, ['12', 2])
  assert.deepEqual(wf['23'].inputs.samples, ['22', 0])
  assert.deepEqual(wf['23'].inputs.vae, ['3', 0])
})

test('Wan 文生视频:EmptyHunyuanLatentVideo + 文本条件', () => {
  const wf = buildWanVideoWorkflow(videoParams, 42, {
    model: 'wan_t2v.safetensors',
    clip: 'c',
    vae: 'v',
  })
  assert.equal(wf['12'].class_type, 'EmptyHunyuanLatentVideo')
  assert.deepEqual(wf['22'].inputs.positive, ['6', 0])
  assert.deepEqual(wf['22'].inputs.negative, ['7', 0])
})

test('Wan 双模型:SplitSigmas 中点分段,低噪阶段不再加噪', () => {
  const wf = buildWanVideoWorkflow(
    { ...videoParams, steps: 20 },
    42,
    {
      model: 'wan_high.safetensors',
      lowNoise: 'wan_low.safetensors',
      clip: 'c',
      vae: 'v',
    },
    'ref_1.png',
  )
  assert.equal(wf['15'].class_type, 'UNETLoader')
  assert.equal(wf['15'].inputs.unet_name, 'wan_low.safetensors')
  assert.deepEqual(wf['17'].inputs.model, ['14', 0])
  assert.equal(wf['18'].class_type, 'SplitSigmas')
  assert.equal(wf['18'].inputs.step, 10)
  assert.equal(wf['20'].inputs.add_noise, true)
  assert.equal(wf['20'].inputs.noise_seed, 42)
  assert.deepEqual(wf['20'].inputs.sigmas, ['18', 0])
  assert.equal(wf['21'].inputs.add_noise, false)
  assert.deepEqual(wf['21'].inputs.sigmas, ['18', 1])
  assert.deepEqual(wf['21'].inputs.latent_image, ['20', 0])
  assert.deepEqual(wf['23'].inputs.samples, ['21', 0])
  assert.equal(wf['22'], undefined)
})
