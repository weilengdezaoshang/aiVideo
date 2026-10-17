# 例句库人工审核清单（P0）

> 对应文件：`exemplars/exemplars.yaml`（`exemplars_version: 0.2.0`）  
> 用途：产品 / 运营勾选 Approve；未勾选或标拒的不进正式检索库。  
> 日期：2026-10-16  
> 相对 0.1.0：**新增抠图/去背景 + 复杂场景**；新增条目标 `🆕`。

## 如何审核

1. 按分组逐条看：`id | 原文 | l2 | tags | notes`（`🆕` = 0.2.0 新增）。
2. **通过**：把 `- [ ]` 改成 `- [x]`。
3. **拒绝 / 改标签**：用下方「审核回复模板」列出 `id` + 原因。
4. **弱动态 / 冲突 / 真多步暗示**：`l2: null` + `l2_candidates`，线上 **clarify 或拆步**，不要静默选边；P0 **不做**「先改图再视频」可执行复合路径。
5. **style_as_art / cinematic still**：图路径，**禁止**标成视频。
6. **抠图（matting/cutout）**：默认 `image_edit`，`needs_image: true`。
7. **一步 I2V**：仅 feature 打开时走 `image_to_video`。
8. 全部勾选通过前，**禁止** auto-ingest 线上点击进库。

### 审核回复模板（复制回聊）

```text
例句库审核（v0.2.0）：
- 通过：全部 / 除下列外全部 / 仅通过 🆕 抠图与复杂场景
- 拒绝：id1（原因）, id2（原因）
- 改标签：id3 → 新 l2 / 新 tags
- 增补建议：（可选）
```

## 数量一览

- **总计**：402 条（其中 🆕 新增 **130** 条）

| L2 / 桶 | 条数 |
|---|---:|
| `image_edit` | 161 |
| `text_to_image` | 63 |
| `image_to_multiframe` | 54 |
| `AMBIGUOUS(weak_motion)` | 46 |
| `text_to_video` | 30 |
| `image_to_video` | 18 |
| `other` | 15 |
| `unsupported` | 15 |

| lang / L2 | 条数 |
|---|---:|
| en / `AMBIGUOUS(weak_motion)` | 15 |
| en / `image_edit` | 49 |
| en / `image_to_multiframe` | 16 |
| en / `image_to_video` | 6 |
| en / `other` | 5 |
| en / `text_to_image` | 17 |
| en / `text_to_video` | 9 |
| en / `unsupported` | 5 |
| zh / `AMBIGUOUS(weak_motion)` | 31 |
| zh / `image_edit` | 112 |
| zh / `image_to_multiframe` | 38 |
| zh / `image_to_video` | 12 |
| zh / `other` | 10 |
| zh / `text_to_image` | 46 |
| zh / `text_to_video` | 21 |
| zh / `unsupported` | 10 |

特殊标签：`matting`=50，`cutout`=43，`compound`=68，`style_as_art`=48，`weak_motion`=37，`negation_video`=26，`one_step_i2v`=18，`ref_utterance`=10，`outpaint`=8，`upscale`=12，`conflict`=5，`multi_step_hint`=3。

---

## 🆕 抠图 / 去背景（matting & cutout）

共 35 条（中文 25 / 英文 10；其中 🆕 35）

格式：`- [ ] id | text | l2 | tags | notes`

### 中文

- [ ] 🆕 `mat_zh_001` | 把这张图抠出来 | `image_edit` | matting,cutout | 抠主体
- [ ] 🆕 `mat_zh_002` | 去背景 | `image_edit` | matting,cutout,remove_bg | 
- [ ] 🆕 `mat_zh_003` | 去掉背景，留人物 | `image_edit` | matting,cutout,remove_bg | 
- [ ] 🆕 `mat_zh_004` | 扣人像 | `image_edit` | matting,cutout | 
- [ ] 🆕 `mat_zh_005` | 把人像抠出来 | `image_edit` | matting,cutout | 
- [ ] 🆕 `mat_zh_006` | 换成透明底 | `image_edit` | matting,cutout,transparent | 
- [ ] 🆕 `mat_zh_007` | 抠图成透明背景 | `image_edit` | matting,cutout,transparent | 
- [ ] 🆕 `mat_zh_008` | 去底，换成白底 | `image_edit` | matting,cutout,white_bg | 
- [ ] 🆕 `mat_zh_009` | 把背景换成纯白 | `image_edit` | matting,cutout,white_bg | 
- [ ] 🆕 `mat_zh_010` | 抠出主体，背景透明 | `image_edit` | matting,cutout,transparent | 
- [ ] 🆕 `mat_zh_011` | 帮我抠图 | `image_edit` | matting,cutout | 
- [ ] 🆕 `mat_zh_012` | 去掉杂乱背景，只留产品 | `image_edit` | matting,cutout,product | 
- [ ] 🆕 `mat_zh_013` | 产品抠图，透明底 PNG | `image_edit` | matting,cutout,transparent,product | 
- [ ] 🆕 `mat_zh_014` | 把宠物抠出来放到透明背景 | `image_edit` | matting,cutout,transparent | 
- [ ] 🆕 `mat_zh_015` | 精细抠头发丝，去背景 | `image_edit` | matting,cutout,remove_bg | 精细抠图
- [ ] 🆕 `mat_zh_016` | 一键抠图去背景 | `image_edit` | matting,cutout,remove_bg | 
- [ ] 🆕 `mat_zh_017` | 把前景人物单独抠出来 | `image_edit` | matting,cutout | 
- [ ] 🆕 `mat_zh_018` | 去掉背景换成灰色工作室背景 | `image_edit` | matting,cutout | 
- [ ] 🆕 `mat_zh_019` | 扣掉背景只要人 | `image_edit` | matting,cutout,remove_bg | 
- [ ] 🆕 `mat_zh_020` | 给这张做蒙版抠图 | `image_edit` | matting,cutout | 
- [ ] 🆕 `mat_zh_021` | 提取主体，背景删掉 | `image_edit` | matting,cutout | 
- [ ] 🆕 `mat_zh_022` | 换成透明底方便后期叠加 | `image_edit` | matting,cutout,transparent | 
- [ ] 🆕 `mat_zh_023` | 商品白底图：去背景换白色 | `image_edit` | matting,cutout,white_bg,product | 
- [ ] 🆕 `mat_zh_024` | 把路人抠掉只留主角…不对，去背景留主角 | `image_edit` | matting,cutout | 口语纠错仍是抠图
- [ ] 🆕 `mat_zh_025` | 选中区域抠出来 | `image_edit` | matting,cutout,ref_utterance | 

### English

- [ ] 🆕 `mat_en_001` | Cut out the subject | `image_edit` | matting,cutout | 
- [ ] 🆕 `mat_en_002` | Remove the background | `image_edit` | matting,cutout,remove_bg | 
- [ ] 🆕 `mat_en_003` | Extract the person, transparent background | `image_edit` | matting,cutout,transparent | 
- [ ] 🆕 `mat_en_004` | Make a white-background product cutout | `image_edit` | matting,cutout,white_bg,product | 
- [ ] 🆕 `mat_en_005` | Matting: keep the subject only | `image_edit` | matting,cutout | 
- [ ] 🆕 `mat_en_006` | Remove bg and export with alpha | `image_edit` | matting,cutout,transparent | 
- [ ] 🆕 `mat_en_007` | Cut out the portrait cleanly | `image_edit` | matting,cutout | 
- [ ] 🆕 `mat_en_008` | Knock out the background to pure white | `image_edit` | matting,cutout,white_bg | 
- [ ] 🆕 `mat_en_009` | Isolate the product on transparent PNG | `image_edit` | matting,cutout,transparent,product | 
- [ ] 🆕 `mat_en_010` | Extract subject from this image | `image_edit` | matting,cutout | 

---

## 🆕 复杂场景（compound / conflict / multi-step hint / ref）

共 95 条（中文 60 / 英文 35；其中 🆕 95）

格式：`- [ ] id | text | l2 | tags | notes`

### 中文

- [ ] 🆕 `cx_zh_001` | 把衣服换成红的，整体改成水彩 | `image_edit` | compound,local_edit,style_transfer | image_edit
- [ ] 🆕 `cx_zh_002` | 头发改金色，再做成油画质感 | `image_edit` | compound,local_edit,style_transfer | 
- [ ] 🆕 `cx_zh_003` | 背景换成海边，画面改成日系动漫风 | `image_edit` | compound,local_edit,style_transfer,style_as_art | 
- [ ] 🆕 `cx_zh_004` | 去掉水印，同时提亮一点 | `image_edit` | compound,cleanup,adjust | 
- [ ] 🆕 `cx_zh_005` | 美颜磨皮，再改成暖色滤镜 | `image_edit` | compound,retouch,style_transfer | 
- [ ] 🆕 `cx_zh_006` | 把左边路人去掉，整体改成水墨风 | `image_edit` | compound,local_edit,style_transfer | 
- [ ] 🆕 `cx_zh_007` | 衣服换成西装，表情改微笑，保持原构图 | `image_edit` | compound,local_edit | 
- [ ] 🆕 `cx_zh_008` | 局部重绘帽子，并提高清晰度 | `image_edit` | compound,inpaint,upscale | 
- [ ] 🆕 `cx_zh_009` | 换成夜景背景，再加一层赛博朋克静图风格 | `image_edit` | compound,style_as_art,local_edit | 仍为改图非视频
- [ ] 🆕 `cx_zh_010` | 裁成竖版，改成极简插画风静图 | `image_edit` | compound,crop,style_as_art,layout | 
- [ ] 🆕 `cx_zh_011` | 不要视频，只要把背景抠掉 | `image_edit` | compound,negation,negation_video,matting,cutout | 否定+抠图
- [ ] 🆕 `cx_zh_012` | 别做成视频，去背景换成透明底 | `image_edit` | compound,negation_video,matting,cutout,transparent | 
- [ ] 🆕 `cx_zh_013` | 不要动起来，就抠个人像出来 | `image_edit` | compound,negation_video,matting,cutout | 
- [ ] 🆕 `cx_zh_014` | 只要静图：扣掉背景换白底 | `image_edit` | compound,negation_video,matting,white_bg | 
- [ ] 🆕 `cx_zh_015` | 别出短片，把水印去掉并变清晰 | `image_edit` | compound,negation_video,cleanup,upscale | 
- [ ] 🆕 `cx_zh_016` | 活一点，日系风 | `ambiguous→image_edit,image_to_multiframe` | weak_motion,needs_clarify_candidate,compound,style_as_art | 弱动态+风格，P0 应 clarify
- [ ] 🆕 `cx_zh_017` | 动起来，顺便赛博朋克一点 | `ambiguous→image_edit,image_to_multiframe` | weak_motion,needs_clarify_candidate,compound,style_as_art | P0 应 clarify 或拆步
- [ ] 🆕 `cx_zh_018` | 更有生命力，水彩质感 | `ambiguous→image_edit,image_to_multiframe` | weak_motion,needs_clarify_candidate,compound | P0 应 clarify
- [ ] 🆕 `cx_zh_019` | 灵动一点，别改画风 | `ambiguous→image_edit,image_to_multiframe` | weak_motion,needs_clarify_candidate,compound,preserve_style | 动起来但别改画风
- [ ] 🆕 `cx_zh_020` | 加点动感，保持现在的插画风格 | `ambiguous→image_edit,image_to_multiframe` | weak_motion,needs_clarify_candidate,compound,preserve_style | P0 应 clarify
- [ ] 🆕 `cx_zh_021` | 画面太平了，日漫风再活泼些 | `ambiguous→image_edit,image_to_multiframe` | weak_motion,needs_clarify_candidate,compound,style_as_art | P0 应 clarify
- [ ] 🆕 `cx_zh_022` | 做成 5 秒视频，赛博朋克 | `image_to_multiframe` | compound,clear_video,duration | image_to_multiframe
- [ ] 🆕 `cx_zh_023` | 把这张做成 8 秒短视频，电影感运镜 | `image_to_multiframe` | compound,clear_video,camera,duration | 
- [ ] 🆕 `cx_zh_024` | 图生视频 5 秒，让头发飘动，保持原画风 | `image_to_multiframe` | compound,clear_video,motion,preserve_style,duration | 
- [ ] 🆕 `cx_zh_025` | 基于这张首帧生成 5 秒视频，缓慢推进镜头 | `image_to_multiframe` | compound,clear_video,first_frame,camera,duration | 
- [ ] 🆕 `cx_zh_026` | 做成短视频，日系清新，大约五秒 | `image_to_multiframe` | compound,clear_video,duration | 
- [ ] 🆕 `cx_zh_027` | 把海报做成可播放的 5s 视频，霓虹灯闪烁 | `image_to_multiframe` | compound,clear_video,motion,duration | 
- [ ] 🆕 `cx_zh_028` | 一张竖版海报，标题留白，极简 | `text_to_image` | compound,generation,poster,layout | text_to_image
- [ ] 🆕 `cx_zh_029` | 生成横版 banner，白底，产品居中，少文字 | `text_to_image` | compound,generation,banner,product | 
- [ ] 🆕 `cx_zh_030` | 画一张方形封面：二次元少女，干净背景，留 logo 位 | `text_to_image` | compound,generation,style_as_art,poster | 
- [ ] 🆕 `cx_zh_031` | 出一张 4K 写实室内效果图，暖光，无人物 | `text_to_image` | compound,generation | 
- [ ] 🆕 `cx_zh_032` | 生成一张电商主图：白底、耳机特写、顶部留标题区 | `text_to_image` | compound,generation,product,white_bg | 
- [ ] 🆕 `cx_zh_033` | 画一张儿童绘本内页：狐狸，柔和配色，右侧留文案空白 | `text_to_image` | compound,generation,layout | 
- [ ] 🆕 `cx_zh_034` | 动起来但别改画风 | `ambiguous→image_edit,image_to_multiframe` | weak_motion,needs_clarify_candidate,compound,preserve_style | 有图：clarify；候选改图↔视频
- [ ] 🆕 `cx_zh_035` | 做成视频，但画风不要变 | `image_to_multiframe` | compound,clear_video,preserve_style | 明确要视频+保风格
- [ ] 🆕 `cx_zh_036` | 用上面那张 | `ambiguous→image_edit,image_to_multiframe,text_to_image` | ref_utterance,vague,needs_clarify_candidate | 仅指代，P0 应 clarify 或等下句
- [ ] 🆕 `cx_zh_037` | 选中的这张去背景 | `image_edit` | ref_utterance,matting,cutout,remove_bg | 
- [ ] 🆕 `cx_zh_038` | 就刚才那张，换成水彩 | `image_edit` | ref_utterance,style_transfer | 
- [ ] 🆕 `cx_zh_039` | 用画布上选中的图抠人像 | `image_edit` | ref_utterance,matting,cutout | 
- [ ] 🆕 `cx_zh_040` | 上面那张变清晰一点 | `image_edit` | ref_utterance,upscale,enhance | 
- [ ] 🆕 `cx_zh_041` | 把选中节点做成视频 | `image_to_multiframe` | ref_utterance,clear_video | 指代+明确视频
- [ ] 🆕 `cx_zh_042` | 做成视频但不要动 | `ambiguous→image_edit,image_to_multiframe` | conflict,needs_clarify_candidate,compound | 冲突，P0 应 clarify
- [ ] 🆕 `cx_zh_043` | 要视频，但又别有动态 | `ambiguous→image_edit,image_to_multiframe` | conflict,needs_clarify_candidate,compound | 冲突
- [ ] 🆕 `cx_zh_044` | 生成短片，画面保持完全静止 | `ambiguous→image_edit,image_to_multiframe` | conflict,needs_clarify_candidate,compound | 冲突；或理解为轻微呼吸感需澄清
- [ ] 🆕 `cx_zh_045` | 去皱，皮肤光滑一点 | `image_edit` | retouch | 
- [ ] 🆕 `cx_zh_046` | 美颜，瘦一点脸 | `image_edit` | retouch | 
- [ ] 🆕 `cx_zh_047` | 提亮暗部，别过曝 | `image_edit` | adjust,retouch | 
- [ ] 🆕 `cx_zh_048` | 去掉脸上的斑点 | `image_edit` | retouch,local_edit | 
- [ ] 🆕 `cx_zh_049` | 去水印并稍微锐化 | `image_edit` | cleanup,enhance | 
- [ ] 🆕 `cx_zh_050` | 祛痘美颜一键修图 | `image_edit` | retouch | 
- [ ] 🆕 `cx_zh_051` | 向外扩图，补全左右两边 | `image_edit` | outpaint | 
- [ ] 🆕 `cx_zh_052` | 扩成更宽的横构图 | `image_edit` | outpaint,layout | 
- [ ] 🆕 `cx_zh_053` | 把画布向外延展一圈 | `image_edit` | outpaint | 
- [ ] 🆕 `cx_zh_054` | 变清晰，放大到高清 | `image_edit` | upscale,enhance | 
- [ ] 🆕 `cx_zh_055` | 超分辨率，细节更清楚 | `image_edit` | upscale,enhance | 
- [ ] 🆕 `cx_zh_056` | 扩图成竖版，同时提高清晰度 | `image_edit` | outpaint,upscale,compound | 
- [ ] 🆕 `cx_zh_057` | outpaint 补全头顶被裁掉的部分 | `image_edit` | outpaint | 
- [ ] 🆕 `cx_zh_058` | 文生视频：雨夜霓虹街道，行人撑伞，大约 5 秒 | `text_to_video` | compound,generation,duration | 
- [ ] 🆕 `cx_zh_059` | 先改成水彩再做成视频 | `ambiguous→image_edit,image_to_multiframe` | compound,multi_step_hint,needs_clarify_candidate | P0 应 clarify 或拆步；禁止当真复合可执行路径
- [ ] 🆕 `cx_zh_060` | 抠完背景再让人物动起来做成视频 | `ambiguous→image_edit,image_to_multiframe` | compound,multi_step_hint,matting,needs_clarify_candidate | P0 应 clarify 或拆步

### English

- [ ] 🆕 `cx_en_001` | Change the shirt to red and make the whole image watercolor | `image_edit` | compound,local_edit,style_transfer | 
- [ ] 🆕 `cx_en_002` | Remove the watermark and brighten the photo | `image_edit` | compound,cleanup,adjust | 
- [ ] 🆕 `cx_en_003` | Swap background to beach, keep anime still style | `image_edit` | compound,style_as_art,local_edit | 
- [ ] 🆕 `cx_en_004` | Retouch face smoother and apply a warm filter | `image_edit` | compound,retouch | 
- [ ] 🆕 `cx_en_005` | No video — just remove the background | `image_edit` | compound,negation_video,matting,cutout | 
- [ ] 🆕 `cx_en_006` | Don't animate; cut out the portrait on transparent bg | `image_edit` | compound,negation_video,matting,transparent | 
- [ ] 🆕 `cx_en_007` | Still image only: knock out bg to white | `image_edit` | compound,negation_video,matting,white_bg | 
- [ ] 🆕 `cx_en_008` | make it livelier in anime style | `ambiguous→image_edit,image_to_multiframe` | weak_motion,needs_clarify_candidate,compound,style_as_art | clarify
- [ ] 🆕 `cx_en_009` | animate a bit but don't change the art style | `ambiguous→image_edit,image_to_multiframe` | weak_motion,needs_clarify_candidate,compound,preserve_style | P0 clarify / 拆步
- [ ] 🆕 `cx_en_010` | bring it to life, watercolor feel | `ambiguous→image_edit,image_to_multiframe` | weak_motion,needs_clarify_candidate,compound | clarify
- [ ] 🆕 `cx_en_011` | Make a 5-second video, cyberpunk vibe | `image_to_multiframe` | compound,clear_video,duration | 
- [ ] 🆕 `cx_en_012` | Turn this into an 8s cinematic clip with slow push-in | `image_to_multiframe` | compound,clear_video,camera,duration | 
- [ ] 🆕 `cx_en_013` | Image to multiframe 5s, hair blowing, keep original style | `image_to_multiframe` | compound,clear_video,motion,preserve_style,duration | 
- [ ] 🆕 `cx_en_014` | A vertical poster, leave space for title, minimalist | `text_to_image` | compound,generation,poster,layout | 
- [ ] 🆕 `cx_en_015` | Square cover art: anime girl, clean bg, logo margin | `text_to_image` | compound,generation,style_as_art | 
- [ ] 🆕 `cx_en_016` | E-commerce hero shot: white bg, headset close-up, top title band | `text_to_image` | compound,generation,product,white_bg | 
- [ ] 🆕 `cx_en_017` | Animate this but keep the same art style | `ambiguous→image_edit,image_to_multiframe` | weak_motion,needs_clarify_candidate,compound,preserve_style | clarify
- [ ] 🆕 `cx_en_018` | Make a video from this image without changing the style | `image_to_multiframe` | compound,clear_video,preserve_style | 
- [ ] 🆕 `cx_en_019` | Use the selected image and remove the background | `image_edit` | ref_utterance,matting,cutout | 
- [ ] 🆕 `cx_en_020` | That one above — make it watercolor | `image_edit` | ref_utterance,style_transfer | 
- [ ] 🆕 `cx_en_021` | The selected node: upscale and sharpen | `image_edit` | ref_utterance,upscale,enhance | 
- [ ] 🆕 `cx_en_022` | Remove the background then make it a cinematic still | `image_edit` | compound,matting,cutout,style_transfer,hard_negative_video | remove bg + cinematic STILL → 改图，非视频
- [ ] 🆕 `cx_en_023` | Cut out the subject and apply a film-still grade, not a video | `image_edit` | compound,matting,negation_video,style_transfer | 
- [ ] 🆕 `cx_en_024` | Knock out bg, white backdrop, product hero still | `image_edit` | compound,matting,white_bg,product | 
- [ ] 🆕 `cx_en_025` | Make a video but don't move anything | `ambiguous→image_edit,image_to_multiframe` | conflict,needs_clarify_candidate,compound | conflict → clarify
- [ ] 🆕 `cx_en_026` | I want a clip that stays completely static | `ambiguous→image_edit,image_to_multiframe` | conflict,needs_clarify_candidate,compound | conflict → clarify
- [ ] 🆕 `cx_en_027` | Remove wrinkles, smoother skin | `image_edit` | retouch | 
- [ ] 🆕 `cx_en_028` | Brighten the shadows without blowing highlights | `image_edit` | adjust,retouch | 
- [ ] 🆕 `cx_en_029` | Remove acne and lightly retouch | `image_edit` | retouch | 
- [ ] 🆕 `cx_en_030` | Outpaint to widen the frame | `image_edit` | outpaint | 
- [ ] 🆕 `cx_en_031` | Extend the canvas on all sides | `image_edit` | outpaint | 
- [ ] 🆕 `cx_en_032` | Upscale to HD and sharpen details | `image_edit` | upscale,enhance | 
- [ ] 🆕 `cx_en_033` | Outpaint vertically and upscale | `image_edit` | outpaint,upscale,compound | 
- [ ] 🆕 `cx_en_034` | Text to video: rainy neon street, 5 seconds, slow dolly | `text_to_video` | compound,generation,duration,camera | 
- [ ] 🆕 `cx_en_035` | First remove the background, then animate it into a video | `ambiguous→image_edit,image_to_multiframe` | compound,multi_step_hint,matting,needs_clarify_candidate | P0 clarify / split steps; not true multi-step path

---

## 验收对齐（E01–E04）

共 11 条（中文 6 / 英文 5；其中 🆕 0）

格式：`- [ ] id | text | l2 | tags | notes`

### 中文

- [ ] `i2m_zh_001` | 把这张图做成视频 | `image_to_multiframe` | clear_video | 明确图生视频
- [ ] `sa_zh_001` | 动画风格海报 | `text_to_image` | style_as_art,hard_negative_video | E02：风格≠视频
- [ ] `sa_zh_004` | 把这张改成动画风格海报 | `image_edit` | style_as_art,hard_negative_video | E02 变体：有图改风格
- [ ] `wm_zh_001` | 活一点 | `ambiguous→image_edit,image_to_multiframe` | weak_motion,needs_clarify_candidate,weak_motion_ambiguous | E01：弱动态，线上应 clarify
- [ ] `wm_zh_002` | 动起来 | `ambiguous→image_edit,image_to_multiframe` | weak_motion,needs_clarify_candidate,weak_motion_ambiguous | E01：弱动态，线上应 clarify
- [ ] `neg_zh_001` | 别做成视频，换成水彩 | `image_edit` | negation,negation_video | E03

### English

- [ ] `i2m_en_001` | Turn this image into a video | `image_to_multiframe` | clear_video | 
- [ ] `sa_en_001` | Anime-style poster still image | `text_to_image` | style_as_art,hard_negative_video | E02 EN
- [ ] `wm_en_001` | animate | `ambiguous→image_edit,image_to_multiframe` | weak_motion,needs_clarify_candidate,weak_motion_ambiguous | E04：弱/强交界，有图应视频族或 clarify
- [ ] `wm_en_003` | animate this image | `ambiguous→image_edit,image_to_multiframe` | weak_motion,needs_clarify_candidate,weak_motion_ambiguous | E04 对齐
- [ ] `neg_en_001` | Don't make a video, just watercolor edit | `image_edit` | negation,negation_video | E03 EN

---

## 弱动态 / 需澄清（weak_motion，不含 cx_ 已列）

共 22 条（中文 16 / 英文 6；其中 🆕 0）

格式：`- [ ] id | text | l2 | tags | notes`

### 中文

- [ ] `wm_zh_003` | 更有生命力一点 | `ambiguous→image_edit,image_to_multiframe` | weak_motion,needs_clarify_candidate,weak_motion_ambiguous | 弱动态
- [ ] `wm_zh_004` | 让它活泼一些 | `ambiguous→image_edit,image_to_multiframe` | weak_motion,needs_clarify_candidate,weak_motion_ambiguous | 弱动态
- [ ] `wm_zh_005` | 感觉太死板了，灵动一点 | `ambiguous→image_edit,image_to_multiframe` | weak_motion,needs_clarify_candidate,weak_motion_ambiguous | 弱动态
- [ ] `wm_zh_006` | 再生动些 | `ambiguous→image_edit,image_to_multiframe` | weak_motion,needs_clarify_candidate,weak_motion_ambiguous | 弱动态
- [ ] `wm_zh_007` | 有点呆，让画面活起来 | `ambiguous→image_edit,image_to_multiframe` | weak_motion,needs_clarify_candidate,weak_motion_ambiguous | 弱动态
- [ ] `wm_zh_008` | 给点动态感 | `ambiguous→image_edit,image_to_multiframe` | weak_motion,needs_clarify_candidate,weak_motion_ambiguous | 弱动态
- [ ] `wm_zh_009` | 看起来更鲜活 | `ambiguous→image_edit,image_to_multiframe` | weak_motion,needs_clarify_candidate,weak_motion_ambiguous | 弱动态
- [ ] `wm_zh_010` | 别这么静止 | `ambiguous→image_edit,image_to_multiframe` | weak_motion,needs_clarify_candidate,weak_motion_ambiguous | 弱动态；注意与否定视频区分
- [ ] `wm_zh_011` | 加点动感 | `ambiguous→image_edit,image_to_multiframe` | weak_motion,needs_clarify_candidate,weak_motion_ambiguous | 弱动态
- [ ] `wm_zh_012` | 更有气息一点 | `ambiguous→image_edit,image_to_multiframe` | weak_motion,needs_clarify_candidate,weak_motion_ambiguous | 弱动态
- [ ] `wm_zh_013` | 让人物精神一点、活一点 | `ambiguous→image_edit,image_to_multiframe` | weak_motion,needs_clarify_candidate,weak_motion_ambiguous | 弱动态
- [ ] `wm_zh_014` | 画面太平了，动感一点 | `ambiguous→image_edit,image_to_multiframe` | weak_motion,needs_clarify_candidate,weak_motion_ambiguous | 弱动态
- [ ] `wm_zh_015` | 生机勃勃一些 | `ambiguous→image_edit,image_to_multiframe` | weak_motion,needs_clarify_candidate,weak_motion_ambiguous | 弱动态
- [ ] `wm_zh_016` | 让表情更生动 | `ambiguous→image_edit,image_to_multiframe` | weak_motion,needs_clarify_candidate,weak_motion_ambiguous | 弱动态；也可能是改图表情
- [ ] `wm_zh_017` | 整体灵动起来 | `ambiguous→image_edit,image_to_multiframe` | weak_motion,needs_clarify_candidate,weak_motion_ambiguous | 弱动态
- [ ] `wm_zh_018` | 有呼吸感一点 | `ambiguous→image_edit,image_to_multiframe` | weak_motion,needs_clarify_candidate,weak_motion_ambiguous | 弱动态

### English

- [ ] `wm_en_002` | animate this | `ambiguous→image_edit,image_to_multiframe` | weak_motion,needs_clarify_candidate,weak_motion_ambiguous | 弱动态
- [ ] `wm_en_004` | make it more lively | `ambiguous→image_edit,image_to_multiframe` | weak_motion,needs_clarify_candidate,weak_motion_ambiguous | 弱动态
- [ ] `wm_en_005` | give it some life | `ambiguous→image_edit,image_to_multiframe` | weak_motion,needs_clarify_candidate,weak_motion_ambiguous | 弱动态
- [ ] `wm_en_006` | make it less static | `ambiguous→image_edit,image_to_multiframe` | weak_motion,needs_clarify_candidate,weak_motion_ambiguous | 弱动态
- [ ] `wm_en_007` | more dynamic please | `ambiguous→image_edit,image_to_multiframe` | weak_motion,needs_clarify_candidate,weak_motion_ambiguous | 弱动态
- [ ] `wm_en_008` | bring it to life | `ambiguous→image_edit,image_to_multiframe` | weak_motion,needs_clarify_candidate,weak_motion_ambiguous | 弱动态

---

## 风格≠视频硬负例（style_as_art 专节）

共 21 条（中文 16 / 英文 5；其中 🆕 0）

格式：`- [ ] id | text | l2 | tags | notes`

### 中文

- [ ] `sa_zh_002` | 做一张二次元风静图封面 | `text_to_image` | style_as_art,hard_negative_video | 封面静图
- [ ] `sa_zh_003` | 赛博朋克风格封面图 | `text_to_image` | style_as_art,hard_negative_video | 
- [ ] `sa_zh_005` | 换成日漫插画风格的静帧 | `image_edit` | style_as_art,hard_negative_video | 
- [ ] `sa_zh_006` | 生成一张吉卜力风格风景静图 | `text_to_image` | style_as_art,hard_negative_video | 
- [ ] `sa_zh_007` | 画一张厚涂油画风角色立绘 | `text_to_image` | style_as_art,hard_negative_video | 
- [ ] `sa_zh_008` | 做成像素游戏封面静图 | `text_to_image` | style_as_art,hard_negative_video | 
- [ ] `sa_zh_009` | 改成美漫漫画风单页 | `image_edit` | style_as_art,hard_negative_video | 
- [ ] `sa_zh_010` | 生成蒸汽波美学壁纸静图 | `text_to_image` | style_as_art,hard_negative_video | 
- [ ] `sa_zh_011` | 做一张 3D 渲染风产品静物图 | `text_to_image` | style_as_art,hard_negative_video | 
- [ ] `sa_zh_012` | 换成水墨国风海报构图 | `image_edit` | style_as_art,hard_negative_video | 
- [ ] `sa_zh_013` | 画一张扁平插画风格图标集 | `text_to_image` | style_as_art,hard_negative_video | 
- [ ] `sa_zh_014` | 生成一张霓虹赛博招牌静帧 | `text_to_image` | style_as_art,hard_negative_video | 
- [ ] `sa_zh_015` | 把照片改成手办摄影棚风格静图 | `image_edit` | style_as_art,hard_negative_video | 
- [ ] `sa_zh_016` | 出一张韩系清新写真风格封面 | `text_to_image` | style_as_art,hard_negative_video | 
- [ ] `sa_zh_017` | 做成 Lo-Fi 插画风桌面壁纸 | `text_to_image` | style_as_art,hard_negative_video | 
- [ ] `sa_zh_018` | 改成线稿+赛璐璐上色静图 | `image_edit` | style_as_art,hard_negative_video | 

### English

- [ ] `sa_en_002` | Make a cyberpunk cover art, still frame | `text_to_image` | style_as_art,hard_negative_video | 
- [ ] `sa_en_003` | Change this to Ghibli-style still illustration | `image_edit` | style_as_art,hard_negative_video | 
- [ ] `sa_en_004` | Generate a vaporwave wallpaper still | `text_to_image` | style_as_art,hard_negative_video | 
- [ ] `sa_en_005` | Comic-book style single-page still | `text_to_image` | style_as_art,hard_negative_video | 
- [ ] `sa_en_006` | Turn into pixel-art game cover, not a video | `image_edit` | style_as_art,hard_negative_video | 

---

## 否定视频 → image_edit（不含 cx_）

共 15 条（中文 11 / 英文 4；其中 🆕 0）

格式：`- [ ] id | text | l2 | tags | notes`

### 中文

- [ ] `neg_zh_002` | 不要视频，只要静图 | `image_edit` | negation,negation_video | 
- [ ] `neg_zh_003` | 别动，就改个风格 | `image_edit` | negation,negation_video | 
- [ ] `neg_zh_004` | 不要做成视频，改成油画 | `image_edit` | negation,negation_video | 
- [ ] `neg_zh_005` | 只要静态图，不要动画视频 | `image_edit` | negation,negation_video | 
- [ ] `neg_zh_006` | 别生成视频，把背景换掉就行 | `image_edit` | negation,negation_video | 
- [ ] `neg_zh_007` | 不要动起来，修一下脸就行 | `image_edit` | negation,negation_video | 
- [ ] `neg_zh_008` | 别出视频，换成二次元静图风格 | `image_edit` | negation,negation_video | 
- [ ] `neg_zh_009` | 不需要视频，提高清晰度即可 | `image_edit` | negation,negation_video | 
- [ ] `neg_zh_010` | 不要多帧，就出一张静图改稿 | `image_edit` | negation,negation_video | 
- [ ] `neg_zh_011` | 别做成短片，改成黑白素描 | `image_edit` | negation,negation_video | 
- [ ] `neg_zh_012` | 只要改图，不要图生视频 | `image_edit` | negation,negation_video | 

### English

- [ ] `neg_en_002` | No video, still image only | `image_edit` | negation,negation_video | 
- [ ] `neg_en_003` | Don't animate, only change the style | `image_edit` | negation,negation_video | 
- [ ] `neg_en_004` | Not a video — retouch the face please | `image_edit` | negation,negation_video | 
- [ ] `neg_en_005` | Skip video, just upscale the image | `image_edit` | negation,negation_video | 

---

## image_edit（基线改图，不含 mat_/cx_/neg_/sa_）

共 49 条（中文 37 / 英文 12；其中 🆕 0）

格式：`- [ ] id | text | l2 | tags | notes`

### 中文

- [ ] `ie_zh_001` | 把这张改成水彩风格 | `image_edit` | style_transfer | 改图+风格
- [ ] `ie_zh_002` | 换成油画质感 | `image_edit` | style_transfer | 
- [ ] `ie_zh_003` | 改成日系动漫风 | `image_edit` | style_transfer,style_as_art | 风格词，非做视频
- [ ] `ie_zh_004` | 做成赛博朋克风格 | `image_edit` | style_transfer,style_as_art | 静图风格
- [ ] `ie_zh_005` | 换成黑白素描 | `image_edit` | style_transfer | 
- [ ] `ie_zh_006` | 改成中国水墨画 | `image_edit` | style_transfer | 
- [ ] `ie_zh_007` | 变成像素风 | `image_edit` | style_transfer | 
- [ ] `ie_zh_008` | 换成二次元插画风格 | `image_edit` | style_transfer,style_as_art | 
- [ ] `ie_zh_009` | 改成写实照片质感 | `image_edit` | style_transfer | 
- [ ] `ie_zh_010` | 做成波普艺术风格 | `image_edit` | style_transfer | 
- [ ] `ie_zh_011` | 把背景换成海边 | `image_edit` | local_edit | 
- [ ] `ie_zh_012` | 换一个天空背景 | `image_edit` | local_edit | 
- [ ] `ie_zh_013` | 把背景去掉换成纯白 | `image_edit` | local_edit,remove_bg | 
- [ ] `ie_zh_014` | 换成夜景背景 | `image_edit` | local_edit | 
- [ ] `ie_zh_015` | 把人物衣服改成红色 | `image_edit` | local_edit | 
- [ ] `ie_zh_016` | 把头发改成金色长发 | `image_edit` | local_edit | 
- [ ] `ie_zh_017` | 去掉图里的水印 | `image_edit` | local_edit,cleanup | 
- [ ] `ie_zh_018` | 把脸上的痘痘修掉 | `image_edit` | local_edit,retouch | 
- [ ] `ie_zh_019` | 美颜一下，皮肤光滑点 | `image_edit` | retouch | 
- [ ] `ie_zh_020` | 把这张图变清晰一点 | `image_edit` | enhance,upscale | 
- [ ] `ie_zh_021` | 提高分辨率，放大细节 | `image_edit` | enhance,upscale | 
- [ ] `ie_zh_022` | 给这张图加个滤镜，暖色调 | `image_edit` | style_transfer | 
- [ ] `ie_zh_023` | 把亮度调高一点 | `image_edit` | adjust | 
- [ ] `ie_zh_024` | 修一下构图，裁成方形 | `image_edit` | crop | 
- [ ] `ie_zh_025` | 把左边那个人去掉 | `image_edit` | local_edit | 
- [ ] `ie_zh_026` | 在右下角加一只猫 | `image_edit` | local_edit | 
- [ ] `ie_zh_027` | 把文字改成「欢迎光临」 | `image_edit` | local_edit | 
- [ ] `ie_zh_028` | 改成竖版海报构图 | `image_edit` | layout | 
- [ ] `ie_zh_029` | 把这张图改成插画风 | `image_edit` | style_transfer,style_as_art | 
- [ ] `ie_zh_030` | 换成蒸汽波风格 | `image_edit` | style_transfer | 
- [ ] `ie_zh_031` | 把饱和度提高，颜色更鲜艳 | `image_edit` | adjust | 
- [ ] `ie_zh_032` | 修图：去噪、锐化 | `image_edit` | enhance,retouch | 
- [ ] `ie_zh_033` | 把人物表情改成微笑 | `image_edit` | local_edit | 
- [ ] `ie_zh_034` | 换成冬天下雪的场景 | `image_edit` | local_edit | 
- [ ] `ie_zh_035` | 把这张做成盲盒潮玩风格静图 | `image_edit` | style_transfer,style_as_art | 强调静图
- [ ] `ie_zh_036` | 局部重绘：只改帽子 | `image_edit` | local_edit,inpaint | 
- [ ] `ie_zh_037` | 把照片变成漫画分镜单帧静图 | `image_edit` | style_transfer,style_as_art | 单帧静图非视频

### English

- [ ] `ie_en_001` | Turn this into a watercolor painting | `image_edit` | style_transfer | 
- [ ] `ie_en_002` | Make it oil painting style | `image_edit` | style_transfer | 
- [ ] `ie_en_003` | Change the background to a beach | `image_edit` | local_edit | 
- [ ] `ie_en_004` | Remove the watermark | `image_edit` | cleanup | 
- [ ] `ie_en_005` | Upscale and sharpen this image | `image_edit` | enhance,upscale | 
- [ ] `ie_en_006` | Retouch the face, smoother skin | `image_edit` | retouch | 
- [ ] `ie_en_007` | Change her hair to blonde | `image_edit` | local_edit | 
- [ ] `ie_en_008` | Convert to black and white sketch | `image_edit` | style_transfer | 
- [ ] `ie_en_009` | Make this cyberpunk style still image | `image_edit` | style_transfer,style_as_art | 
- [ ] `ie_en_010` | Replace the sky with sunset | `image_edit` | local_edit | 
- [ ] `ie_en_011` | Add a cat in the corner | `image_edit` | local_edit | 
- [ ] `ie_en_012` | Crop to square poster layout | `image_edit` | crop,layout | 

---

## text_to_image（基线，不含 cx_/sa_）

共 38 条（中文 28 / 英文 10；其中 🆕 0）

格式：`- [ ] id | text | l2 | tags | notes`

### 中文

- [ ] `t2i_zh_001` | 画一只戴墨镜的橘猫 | `text_to_image` | generation | 
- [ ] `t2i_zh_002` | 生成一张赛博朋克城市夜景 | `text_to_image` | generation,style_as_art | 
- [ ] `t2i_zh_003` | 帮我画一个二次元少女立绘 | `text_to_image` | generation,style_as_art | 
- [ ] `t2i_zh_004` | 文生图：雪山脚下的木屋 | `text_to_image` | generation | 
- [ ] `t2i_zh_005` | 画一张产品海报，极简白底 | `text_to_image` | generation,poster | 
- [ ] `t2i_zh_006` | 生成一张中国风山水插画 | `text_to_image` | generation,style_as_art | 
- [ ] `t2i_zh_007` | 画一个可爱的盲盒潮玩角色 | `text_to_image` | generation | 
- [ ] `t2i_zh_008` | 出一张科幻机甲概念图 | `text_to_image` | generation | 
- [ ] `t2i_zh_009` | 画一张咖啡店室内效果图 | `text_to_image` | generation | 
- [ ] `t2i_zh_010` | 生成一张星空下的帐篷露营 | `text_to_image` | generation | 
- [ ] `t2i_zh_011` | 画一个logo：字母J，极简线条 | `text_to_image` | generation,logo | 
- [ ] `t2i_zh_012` | 出图：未来感地铁站 | `text_to_image` | generation | 
- [ ] `t2i_zh_013` | 画一张美食摄影：日式拉面特写 | `text_to_image` | generation | 
- [ ] `t2i_zh_014` | 生成一张儿童绘本风格插图 | `text_to_image` | generation | 
- [ ] `t2i_zh_015` | 画一张蒸汽波美学海报静图 | `text_to_image` | generation,style_as_art,poster | 
- [ ] `t2i_zh_016` | 帮我生成一张办公室场景人像 | `text_to_image` | generation | 
- [ ] `t2i_zh_017` | 画一只在月球上的宇航员猫 | `text_to_image` | generation | 
- [ ] `t2i_zh_018` | 出一张抽象几何构成图 | `text_to_image` | generation | 
- [ ] `t2i_zh_019` | 生成一张春季樱花大道 | `text_to_image` | generation | 
- [ ] `t2i_zh_020` | 画一张游戏角色三视图概念 | `text_to_image` | generation | 
- [ ] `t2i_zh_021` | 文生图 4K 写实人像写真 | `text_to_image` | generation | 
- [ ] `t2i_zh_022` | 画一张节日促销banner静图 | `text_to_image` | generation,banner | 
- [ ] `t2i_zh_023` | 生成一张水下珊瑚与热带鱼 | `text_to_image` | generation | 
- [ ] `t2i_zh_024` | 画一张复古港风街景插画 | `text_to_image` | generation | 
- [ ] `t2i_zh_025` | 出一张极简室内设计效果图 | `text_to_image` | generation | 
- [ ] `t2i_zh_026` | 画一张龙与城堡奇幻场景 | `text_to_image` | generation | 
- [ ] `t2i_zh_027` | 生成一张卡通表情包：开心脸 | `text_to_image` | generation,emoji | 
- [ ] `t2i_zh_028` | 画一张产品渲染：无线耳机白底 | `text_to_image` | generation,product | 

### English

- [ ] `t2i_en_001` | Generate an image of a cat wearing sunglasses | `text_to_image` | generation | 
- [ ] `t2i_en_002` | Create a cyberpunk city night scene | `text_to_image` | generation,style_as_art | 
- [ ] `t2i_en_003` | Draw an anime-style girl character sheet | `text_to_image` | generation,style_as_art | 
- [ ] `t2i_en_004` | Text to image: wooden cabin under snow mountains | `text_to_image` | generation | 
- [ ] `t2i_en_005` | Make a minimalist product poster on white background | `text_to_image` | generation,poster | 
- [ ] `t2i_en_006` | Generate a fantasy dragon and castle scene | `text_to_image` | generation | 
- [ ] `t2i_en_007` | Create a photorealistic ramen close-up food shot | `text_to_image` | generation | 
- [ ] `t2i_en_008` | Draw a logo with letter J in clean lines | `text_to_image` | generation,logo | 
- [ ] `t2i_en_009` | Generate a vaporwave aesthetic still poster | `text_to_image` | generation,style_as_art | 
- [ ] `t2i_en_010` | Create a children's book style illustration of a fox | `text_to_image` | generation | 

---

## text_to_video

共 28 条（中文 20 / 英文 8；其中 🆕 0）

格式：`- [ ] id | text | l2 | tags | notes`

### 中文

- [ ] `t2v_zh_001` | 用文字生成一段海浪拍打礁石的视频 | `text_to_video` | generation | 
- [ ] `t2v_zh_002` | 文生视频：小猫在草地上奔跑 | `text_to_video` | generation | 
- [ ] `t2v_zh_003` | 生成一段城市延时摄影风格短视频 | `text_to_video` | generation | 
- [ ] `t2v_zh_004` | 做一段科幻飞船飞过星空的视频 | `text_to_video` | generation | 
- [ ] `t2v_zh_005` | 文生视频 8 秒：咖啡倒入杯子特写 | `text_to_video` | generation,duration | 
- [ ] `t2v_zh_006` | 生成一段火焰燃烧的慢动作视频 | `text_to_video` | generation | 
- [ ] `t2v_zh_007` | 用提示词做一个下雨天街道行人视频 | `text_to_video` | generation | 
- [ ] `t2v_zh_008` | 文生视频：樱花飘落的空镜 | `text_to_video` | generation | 
- [ ] `t2v_zh_009` | 生成一段无人机俯拍海岸线视频 | `text_to_video` | generation | 
- [ ] `t2v_zh_010` | 做一段抽象流体色彩流动短片 | `text_to_video` | generation | 
- [ ] `t2v_zh_011` | 文字生成视频：火车穿过隧道 | `text_to_video` | generation | 
- [ ] `t2v_zh_012` | 生成一段人物走路的侧面镜头视频 | `text_to_video` | generation | 
- [ ] `t2v_zh_013` | 文生视频：落日海岸 5 秒 | `text_to_video` | generation,duration | 
- [ ] `t2v_zh_014` | 做一段蒸汽从茶杯升起的短视频 | `text_to_video` | generation | 
- [ ] `t2v_zh_015` | 生成一段雪花缓缓落下的空镜视频 | `text_to_video` | generation | 
- [ ] `t2v_zh_016` | 文生视频：机械齿轮运转特写 | `text_to_video` | generation | 
- [ ] `t2v_zh_017` | 生成一段霓虹灯夜市人群走动视频 | `text_to_video` | generation | 
- [ ] `t2v_zh_018` | 用文字做一个产品旋转展示短视频 | `text_to_video` | generation,product | 
- [ ] `t2v_zh_019` | 文生视频：蝴蝶从花丛飞过 | `text_to_video` | generation | 
- [ ] `t2v_zh_020` | 生成一段云层快速流动的延时视频 | `text_to_video` | generation | 

### English

- [ ] `t2v_en_001` | Generate a video of ocean waves hitting rocks | `text_to_video` | generation | 
- [ ] `t2v_en_002` | Text to video: a kitten running on grass | `text_to_video` | generation | 
- [ ] `t2v_en_003` | Create a short cyberpunk street walking clip | `text_to_video` | generation | 
- [ ] `t2v_en_004` | Make an 8-second video of coffee pouring | `text_to_video` | generation,duration | 
- [ ] `t2v_en_005` | Generate a timelapse of clouds moving over mountains | `text_to_video` | generation | 
- [ ] `t2v_en_006` | Text-to-video: steam rising from a teacup | `text_to_video` | generation | 
- [ ] `t2v_en_007` | Create a product spin showcase video from text | `text_to_video` | generation,product | 
- [ ] `t2v_en_008` | Generate a short clip of cherry blossoms falling | `text_to_video` | generation | 

---

## image_to_multiframe（基线明确图生视频，不含 cx_）

共 40 条（中文 29 / 英文 11；其中 🆕 0）

格式：`- [ ] id | text | l2 | tags | notes`

### 中文

- [ ] `i2m_zh_002` | 根据这张图生成一段视频 | `image_to_multiframe` | clear_video | 
- [ ] `i2m_zh_003` | 让画面里的人物动起来，做成短视频 | `image_to_multiframe` | clear_video,motion | 
- [ ] `i2m_zh_004` | 把这张静图动画化成视频 | `image_to_multiframe` | clear_video | 
- [ ] `i2m_zh_005` | 从图生成多帧视频 | `image_to_multiframe` | clear_video | 
- [ ] `i2m_zh_006` | 让她眨眨眼、微微笑，输出视频 | `image_to_multiframe` | clear_video,motion | 
- [ ] `i2m_zh_007` | 把这张海报动起来做成 5 秒视频 | `image_to_multiframe` | clear_video,duration | 
- [ ] `i2m_zh_008` | 图生视频：云彩飘动、镜头缓慢推进 | `image_to_multiframe` | clear_video,camera | 
- [ ] `i2m_zh_009` | 基于选中图片生成一段短片 | `image_to_multiframe` | clear_video | 
- [ ] `i2m_zh_010` | 把这张变成可以播放的视频 | `image_to_multiframe` | clear_video | 
- [ ] `i2m_zh_011` | 让头发随风飘动，做成视频片段 | `image_to_multiframe` | clear_video,motion | 
- [ ] `i2m_zh_012` | 把静帧扩展成多帧动画视频 | `image_to_multiframe` | clear_video | 
- [ ] `i2m_zh_013` | 用这张图做一段产品展示视频 | `image_to_multiframe` | clear_video,product | 
- [ ] `i2m_zh_014` | 给这张图加镜头推拉，输出视频 | `image_to_multiframe` | clear_video,camera | 
- [ ] `i2m_zh_015` | 把人物做成走路循环短视频 | `image_to_multiframe` | clear_video,motion | 
- [ ] `i2m_zh_016` | 图转视频，时长大约 5 秒 | `image_to_multiframe` | clear_video,duration | 
- [ ] `i2m_zh_017` | 让水面波光粼粼，生成视频 | `image_to_multiframe` | clear_video,motion | 
- [ ] `i2m_zh_018` | 把这张风景图做成缓慢平移的视频 | `image_to_multiframe` | clear_video,camera | 
- [ ] `i2m_zh_019` | 选中图 → 生成短视频 | `image_to_multiframe` | clear_video | 
- [ ] `i2m_zh_020` | 把角色做成呼吸待机动画视频 | `image_to_multiframe` | clear_video,motion | 
- [ ] `i2m_zh_021` | 基于这张首帧生成后续视频 | `image_to_multiframe` | clear_video,first_frame | 首帧→多帧
- [ ] `i2m_zh_022` | 把这张照片做成电影感短镜头视频 | `image_to_multiframe` | clear_video | 
- [ ] `i2m_zh_023` | 让火焰跳动起来，输出成视频 | `image_to_multiframe` | clear_video,motion | 
- [ ] `i2m_zh_024` | 图生多帧：人物转头看向镜头 | `image_to_multiframe` | clear_video,motion | 
- [ ] `i2m_zh_025` | 把插画做成会动的短视频 | `image_to_multiframe` | clear_video | 
- [ ] `i2m_zh_026` | 用当前图片生成 8 秒视频 | `image_to_multiframe` | clear_video,duration | 
- [ ] `i2m_zh_027` | 把这张做成带运镜的视频片段 | `image_to_multiframe` | clear_video,camera | 
- [ ] `i2m_zh_028` | 静图转视频，人物轻微点头 | `image_to_multiframe` | clear_video,motion | 
- [ ] `i2m_zh_029` | 把这张封面图做成开场短视频 | `image_to_multiframe` | clear_video | 
- [ ] `i2m_zh_030` | 根据选中帧继续生成视频画面 | `image_to_multiframe` | clear_video,first_frame | 

### English

- [ ] `i2m_en_002` | Generate a short video from this picture | `image_to_multiframe` | clear_video | 
- [ ] `i2m_en_003` | Make the character move and export as video | `image_to_multiframe` | clear_video,motion | 
- [ ] `i2m_en_004` | Animate this still into a multiframe clip | `image_to_multiframe` | clear_video | 
- [ ] `i2m_en_005` | Create a 5-second video from the selected image | `image_to_multiframe` | clear_video,duration | 
- [ ] `i2m_en_006` | Add camera push-in and output a video | `image_to_multiframe` | clear_video,camera | 
- [ ] `i2m_en_007` | Make her blink and smile as a video | `image_to_multiframe` | clear_video,motion | 
- [ ] `i2m_en_008` | Use this as first frame and generate the rest of the video | `image_to_multiframe` | clear_video,first_frame | 
- [ ] `i2m_en_009` | Convert this poster into a playable short video | `image_to_multiframe` | clear_video | 
- [ ] `i2m_en_010` | Image to multiframe: hair blowing in the wind | `image_to_multiframe` | clear_video,motion | 
- [ ] `i2m_en_011` | Turn this landscape into a slow pan video | `image_to_multiframe` | clear_video,camera | 
- [ ] `i2m_en_012` | Generate an 8s product showcase video from this image | `image_to_multiframe` | clear_video,product | 

---

## image_to_video（一步 I2V）

共 18 条（中文 12 / 英文 6；其中 🆕 0）

格式：`- [ ] id | text | l2 | tags | notes`

### 中文

- [ ] `i2v_zh_001` | 一步图生视频，直接出成片 | `image_to_video` | one_step_i2v | 一步 I2V，需 feature flag
- [ ] `i2v_zh_002` | 用一步 I2V 把这张图生成视频 | `image_to_video` | one_step_i2v | 一步 I2V，需 feature flag
- [ ] `i2v_zh_003` | 开启一步图生视频模式跑这张 | `image_to_video` | one_step_i2v | 一步 I2V，需 feature flag
- [ ] `i2v_zh_004` | 直接图生视频，不要多步分镜 | `image_to_video` | one_step_i2v | 一步 I2V，需 feature flag
- [ ] `i2v_zh_005` | one-step image to video 这张图 | `image_to_video` | one_step_i2v | 一步 I2V，需 feature flag
- [ ] `i2v_zh_006` | 用单步 I2V 管道生成短视频 | `image_to_video` | one_step_i2v | 一步 I2V，需 feature flag
- [ ] `i2v_zh_007` | 图生视频（一步出片） | `image_to_video` | one_step_i2v | 一步 I2V，需 feature flag
- [ ] `i2v_zh_008` | 走一步式图生视频能力 | `image_to_video` | one_step_i2v | 一步 I2V，需 feature flag
- [ ] `i2v_zh_009` | 直接 I2V，别走多帧路径 | `image_to_video` | one_step_i2v | 一步 I2V，需 feature flag
- [ ] `i2v_zh_010` | 调用一步图转视频模型 | `image_to_video` | one_step_i2v | 一步 I2V，需 feature flag
- [ ] `i2v_zh_011` | 单阶段图生视频，时长 5 秒 | `image_to_video` | one_step_i2v,duration | 一步 I2V，需 feature flag
- [ ] `i2v_zh_012` | 用一步 I2V 把选中图做成视频 | `image_to_video` | one_step_i2v | 一步 I2V，需 feature flag

### English

- [ ] `i2v_en_001` | One-step image-to-video from this frame | `image_to_video` | one_step_i2v | one-step I2V; feature-gated
- [ ] `i2v_en_002` | Run single-pass I2V on the selected image | `image_to_video` | one_step_i2v | one-step I2V; feature-gated
- [ ] `i2v_en_003` | Direct image to video, not multiframe pipeline | `image_to_video` | one_step_i2v | one-step I2V; feature-gated
- [ ] `i2v_en_004` | Use one-shot I2V to make a clip | `image_to_video` | one_step_i2v | one-step I2V; feature-gated
- [ ] `i2v_en_005` | Single-stage image-to-video, 5 seconds | `image_to_video` | one_step_i2v,duration | one-step I2V; feature-gated
- [ ] `i2v_en_006` | Enable one-step I2V mode for this picture | `image_to_video` | one_step_i2v | one-step I2V; feature-gated

---

## other（问候/闲聊）

共 15 条（中文 10 / 英文 5；其中 🆕 0）

格式：`- [ ] id | text | l2 | tags | notes`

### 中文

- [ ] `gr_zh_001` | 你好 | `other` | greeting | 
- [ ] `gr_zh_002` | 在吗 | `other` | greeting | 
- [ ] `gr_zh_003` | 嗨 | `other` | greeting | 
- [ ] `gr_zh_004` | 早上好 | `other` | greeting | 
- [ ] `gr_zh_005` | 谢谢 | `other` | greeting,thanks | 
- [ ] `gr_zh_006` | 好的 | `other` | ack | 
- [ ] `gr_zh_007` | 收到 | `other` | ack | 
- [ ] `gr_zh_008` | 帮我看看 | `other` | vague,other | 
- [ ] `gr_zh_009` | 嗯 | `other` | ack | 
- [ ] `gr_zh_010` | 你是谁 | `other` | meta | 

### English

- [ ] `gr_en_001` | hi | `other` | greeting | 
- [ ] `gr_en_002` | hello | `other` | greeting | 
- [ ] `gr_en_003` | thanks | `other` | greeting,thanks | 
- [ ] `gr_en_004` | ok | `other` | ack | 
- [ ] `gr_en_005` | who are you | `other` | meta | 

---

## unsupported（能力外）

共 15 条（中文 10 / 英文 5；其中 🆕 0）

格式：`- [ ] id | text | l2 | tags | notes`

### 中文

- [ ] `un_zh_001` | 今天天气怎么样 | `unsupported` | out_of_scope,unsupported | 
- [ ] `un_zh_002` | 帮我写一段 Python 代码 | `unsupported` | out_of_scope,coding,unsupported | 
- [ ] `un_zh_003` | 推荐附近的餐厅 | `unsupported` | out_of_scope,unsupported | 
- [ ] `un_zh_004` | 帮我翻译这段英文 | `unsupported` | out_of_scope,unsupported | 
- [ ] `un_zh_005` | 现在几点了 | `unsupported` | out_of_scope,unsupported | 
- [ ] `un_zh_006` | 讲个笑话 | `unsupported` | out_of_scope,unsupported | 
- [ ] `un_zh_007` | 帮我下单买东西 | `unsupported` | out_of_scope,unsupported | 
- [ ] `un_zh_008` | 查一下股票行情 | `unsupported` | out_of_scope,unsupported | 
- [ ] `un_zh_009` | 设置明天早上的闹钟 | `unsupported` | out_of_scope,unsupported | 
- [ ] `un_zh_010` | 帮我写周报 | `unsupported` | out_of_scope,unsupported | 

### English

- [ ] `un_en_001` | What's the weather today? | `unsupported` | out_of_scope,unsupported | 
- [ ] `un_en_002` | Write a Python function for me | `unsupported` | out_of_scope,coding,unsupported | 
- [ ] `un_en_003` | Recommend a restaurant nearby | `unsupported` | out_of_scope,unsupported | 
- [ ] `un_en_004` | What time is it? | `unsupported` | out_of_scope,unsupported | 
- [ ] `un_en_005` | Tell me a joke | `unsupported` | out_of_scope,unsupported | 

---

## 审核签字（可选）

- 审核人：
- 日期：
- 结论：全部通过 / 带拒绝列表通过 / 仅通过 0.2.0 新增
