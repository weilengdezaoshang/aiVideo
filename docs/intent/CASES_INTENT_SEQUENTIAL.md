# 案例册：单步复杂 / 弱动态 / 顺位意图（产品审阅）

> 日期：2026-10-16  
> 配套：`HANDOFF_INTENT_P2_SEQUENTIAL.md`、`HANDOFF_INTENT_RAG_FEEDBACK_LOOP.md`、`exemplars/exemplars.yaml` v0.2.0  
> 说明：只列期望行为，不写实现。`expected_p2` 均指 **INTENT_MULTI_STEP=true**；开关 false 时顺位句行为与 `expected_p0` 相同。

**列说明**

| 列 | 含义 |
|----|------|
| id | 用例编号 |
| utterance | 用户原话（中/英） |
| expected_p0 | P0（单步 RAG）：应执行的 L2/path 或 clarify |
| expected_p2 | P2 开多步后：单步同左，或 `A→B` 白名单连跑 |
| notes | 备注 |

**图例**

- `image_edit`：改图（含风格、局部、抠图）  
- `matting`：抠图/去背景（仍属 image_edit）  
- `t2i`：text_to_image  
- `multiframe`：image_to_multiframe（有图做视频默认）  
- `t2v`：text_to_video  
- `clarify`：必须问用户，不得静默定边  
- `reject`：明确失败 / unsupported，不硬跑  

---

## 1. 单步复杂 C（多约束仍是一件事）

| id | utterance | expected_p0 | expected_p2 | notes |
|----|-----------|-------------|-------------|-------|
| C-01 | 把衣服换成红的，整体水彩风 | image_edit | 同左 | 多槽位同 L2 |
| C-02 | 去背景，透明底，尽量保留发丝 | image_edit (matting) | 同左 | 抠图 |
| C-03 | 别做成视频，换成油画质感 | image_edit | 同左 | 否定视频 |
| C-04 | 动画风格海报 | image_edit 或 t2i + style | 同左 | 风格≠视频 |
| C-05 | 5 秒赛博朋克视频 | multiframe + duration+style（有图）/ t2v（无图） | 同左 | 单步视频 |
| C-06 | 画一只橘猫，水彩风（无选中图） | t2i | 同左 | |
| C-07 | 不要背景只要人物抠出来 | image_edit (matting) | 同左 | |
| C-08 | remove background and keep hair detail | image_edit (matting) | 同左 | 英文 |
| C-09 | 把左下角 logo 去掉，颜色再鲜艳一点 | image_edit | 同左 | 局部+全局 |
| C-10 | 白底商品图，阴影淡一点 | image_edit (matting/白底) | 同左 | 电商抠图 |
| C-11 | 换成日系动漫风，不要动构图 | image_edit + style | 同左 | style_as_art |
| C-12 | 只要静图水彩，不要任何动画 | image_edit | 同左 | 双否定动态 |
| C-13 | make this watercolor, keep the face | image_edit | 同左 | 英文改图 |
| C-14 | 抠成 PNG 透明底，边缘干净点 | image_edit (matting) | 同左 | |
| C-15 | 人物换成西装，背景虚化 | image_edit | 同左 | 多约束 |
| C-16 | 生成一张赛博朋克街头夜景（无图） | t2i | 同左 | |
| C-17 | 把这张做成竖版海报排版 | image_edit 或 unsupported | 同左 | 排版若未产品化可 unsupported |
| C-18 | cut out the product on white background | image_edit (matting) | 同左 | |
| C-19 | 别生成视频，帮我修一下曝光 | image_edit | 同左 | |
| C-20 | anime style portrait poster | image_edit/t2i | 同左 | 硬负例：非视频 |

---

## 2. 弱动态 W（P0/P2 都应澄清，不拆多步）

| id | utterance | expected_p0 | expected_p2 | notes |
|----|-----------|-------------|-------------|-------|
| W-01 | 动起来 | clarify | clarify（禁止自动两步） | E01 |
| W-02 | 活一点 | clarify | 同左 | |
| W-03 | animate this | clarify 或视频族 | 同左 | E04 |
| W-04 | 让人物稍微动一动 | clarify | 同左 | |
| W-05 | 有点动态的感觉 | clarify | 同左 | 极弱 |
| W-06 | 生动一些 | clarify | 同左 | 也可被理解成改图氛围 |
| W-07 | make it more alive | clarify | 同左 | |
| W-08 | 给我弄动一点 | clarify | 同左 | 口语 |
| W-09 | 眼睛会眨一下那种 | clarify | 同左 | 细节动态 |
| W-10 | add a bit of motion | clarify | 同左 | |

---

## 3. 顺位 / 复合 Q（P0 问清；P2 白名单才连跑）

| id | utterance | expected_p0 | expected_p2 | notes |
|----|-----------|-------------|-------------|-------|
| Q-01 | 先抠图去背景，再做成 5 秒视频 | clarify / 拆步 | matting → multiframe | 白名单；见 handoff JSON |
| Q-02 | 先改成水彩，再做成视频 | clarify | image_edit → multiframe | |
| Q-03 | 先画一只猫，再生成视频 | clarify 或引导补图 | t2i → multiframe | 无图 |
| Q-04 | 改完油画再动起来 | clarify | edit → multiframe 或第二段再 clarify | 「动起来」仍弱 |
| Q-05 | 先做成视频再改水彩 | clarify / reject | **reject**（非白名单） | 禁止视频→改图 |
| Q-06 | 先抠图，再水彩，再视频 | clarify 拆分 | ≤2 步或 clarify | max_steps=2 |
| Q-07 | 抠完透明底然后 animate | clarify | matting → multiframe | |
| Q-08 | first cut out the subject, then make a 5s video | clarify | matting → multiframe | 英文 |
| Q-09 | 先修脸再出 5 秒短片 | clarify | image_edit → multiframe | |
| Q-10 | 帮我去个背景，然后做成会动的 | clarify | matting → multiframe | 口语顺位 |
| Q-11 | 先文生一张海报，再转成视频 | clarify | t2i → multiframe | |
| Q-12 | 水彩改完之后生成视频看看 | clarify | edit → multiframe | |
| Q-13 | remove bg first, then animate it | clarify | matting → multiframe | |
| Q-14 | 先做成视频，再抠图导出 | clarify / reject | **reject** | 非白名单 |
| Q-15 | 一键：抠图+做视频 | clarify（是否两步） | 确认后 matting → multiframe | 需确认 |
| Q-16 | 改成赛博朋克做成视频（无「先/再」） | 单步 multiframe+style **或** clarify | 同左（不强制两步） | S08：非真顺位 |
| Q-17 | 先放大清晰度，再生成 3 秒视频 | clarify | edit → multiframe | |
| Q-18 | 先画个logo，再让它转起来 | clarify | t2i → multiframe | |
| Q-19 | 把人抠出来以后做个走路视频 | clarify | matting → multiframe | |
| Q-20 | edit to watercolor first, then video | clarify | edit → multiframe | |
| Q-21 | 先视频后改图再视频 | reject / clarify | **reject** | 超长+非法序 |
| Q-22 | 只要抠图，暂时不要视频 | image_edit (matting) | 同左 | 顺位否决→单步 |
| Q-23 | 先问清楚：是要改图还是视频？（元话） | other / clarify | 同左 | 非执行句 |
| Q-24 | 抠图后直接出片，5 秒 | clarify | matting → multiframe | 「出片」=视频 |

---

## 4. 能力 / 边界（抽测）

| id | utterance | expected_p0 | expected_p2 | notes |
|----|-----------|-------------|-------------|-------|
| B-01 | 做成视频（视频能力关） | 明确失败 | 同左；多步则第二步能力失败且不降级静图 | E06 |
| B-02 | 先抠再视频（无有效图） | 不能硬 edit；引导补图或 t2i 路径 | 若走 t2i→multiframe 须无图可行 | |
| B-03 | embedding 挂了 +「动起来」 | clarify/兜底，不赌视频 | 同左 | E05 |
| B-04 | INTENT_MULTI_STEP=false + Q-01 | clarify | （N/A，同 p0） | S01 |

---

## 5. 统计

| 分组 | 条数 |
|------|-----:|
| C 单步复杂（含抠图/英文） | 20 |
| W 弱动态 | 10 |
| Q 顺位/复合 | 24 |
| B 边界抽测 | 4 |
| **合计** | **58** |

---

## 6. 产品审阅勾选（可选回贴）

```text
案例册审核（CASES_INTENT_SEQUENTIAL）：
- 通过：全部 / 除下列外全部
- 拒绝：id（原因）
- 改期望：id → 新 expected_p0 / expected_p2
- 增补口语：
```

---

## 7. 一句话

**一件事**（多形容词、抠图）→ 单步；**动起来** → 问；**先 A 再 B** → P0 只问，P2 开白名单才最多两步连跑。
