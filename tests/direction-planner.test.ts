import test from 'node:test'
import assert from 'node:assert/strict'
import { planDirections } from '../src/services/direction-planner.js'

test('四方向共享主体与硬约束,变化维度互不相同', () => {
  const plan = planDirections('一只戴墨镜的猫在沙滩上,赛博朋克风格,不要出现人类')
  assert.equal(plan.directions.length, 4)
  // 共同主体保留
  for (const d of plan.directions) {
    assert.ok(d.prompt.includes('一只戴墨镜的猫在沙滩上'), `方向「${d.title}」丢失主体`)
    assert.ok(d.prompt.includes('赛博朋克风格'), `方向「${d.title}」丢失风格约束`)
    assert.ok(d.prompt.includes('不要出现人类'), `方向「${d.title}」丢失显式约束`)
  }
  const dims = new Set(plan.directions.map((d) => d.dimension))
  assert.equal(dims.size, 4)
  const titles = new Set(plan.directions.map((d) => d.title))
  assert.equal(titles.size, 4)
  // 提示词彼此不同(方向语义互异的结构下限)
  const prompts = new Set(plan.directions.map((d) => d.prompt))
  assert.equal(prompts.size, 4)
  // 槽位固定 0..3
  assert.deepEqual(
    plan.directions.map((d) => d.slot),
    [0, 1, 2, 3],
  )
})

test('风格词进入硬约束而不是被方向替换;确定性输出', () => {
  const plan = planDirections('水彩风格的港口小镇')
  assert.deepEqual(plan.constraints, ['水彩风格的港口小镇'])
  assert.equal(plan.subject, '水彩风格的港口小镇')
  for (const d of plan.directions) {
    assert.ok(d.prompt.includes('水彩'))
  }
  // 同输入同输出(评估集 A04 的可复现前提)
  assert.deepEqual(planDirections('水彩风格的港口小镇'), plan)
})

test('补充描述跟随所有方向;空输入也有安全输出', () => {
  const plan = planDirections('山间木屋,清晨,必须避开人物')
  assert.equal(plan.subject, '山间木屋')
  assert.deepEqual(plan.constraints, ['必须避开人物'])
  for (const d of plan.directions) {
    assert.ok(d.prompt.includes('清晨'))
  }
  const empty = planDirections('   ')
  assert.equal(empty.directions.length, 4)
})
