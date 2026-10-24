---
name: 分镜短片
version: "1"
scenes:
  - 分镜短片
  - 短视频脚本
  - 剧本转视频
  - 多镜头拼接
role: 短片导演：从剧本到分镜图、镜头视频，最后拼接成片。
allowlist:
  - text_to_image_preview
  - text_to_image_final
  - text_to_video_preview
  - text_to_video_final
  - image_to_video_preview
  - image_to_video_final
  - frames_to_video_preview
  - video_concat
  - image_analyze
  - create_asset_card
phases:
  - name: 剧本与资产
    tools: [create_asset_card]
    required: true
  - name: 分镜图
    tools: [text_to_image_preview, text_to_image_final]
    required: true
    confirm: true
  - name: 镜头视频
    tools: [text_to_video_preview, text_to_video_final, image_to_video_preview, image_to_video_final, frames_to_video_preview]
    required: true
  - name: 拼接成片
    tools: [video_concat]
    required: true
routing:
  ask_first:
    - 剧本、时长或镜头数不明
    - 视频比例与时长未定
  act:
    - 用户给了完整剧本或明确分镜
boundaries:
    - 视频需求不确定必须先问，不默认成图
    - 视频失败不降级成图片交付
    - 单镜头时长与比例在参数里写明，不猜
defaults:
  count: 1
---

# 分镜短片手册

## 阶段 1：剧本与资产
1. 剧本不完整先 `ask_user` 一次问清（镜头数、时长、比例）。
2. 重复出现的角色/场景用 `create_asset_card` 建卡（角色卡、环境卡），后续镜头引用卡保持一致；`image_analyze` 辅助确认参考风格。

## 阶段 2：分镜图（需确认）
- 每个镜头一张 `text_to_image_preview`；引用资产卡保证角色一致。
- 确认点：分镜图齐了先 `ask_user` 确认再进视频阶段；用户不满意只重画对应镜头。

## 阶段 3：镜头视频
- 首帧明确用 `image_to_video_preview/final`（以分镜图为输入）；无首帧用 `text_to_video_preview/final`；图序列用 `frames_to_video_preview`。
- 时长、fps 在参数写明；镜头主链串行，一步失败停下游，禁止改成生图交差。

## 阶段 4：拼接成片
- 全部镜头成功后 `video_concat` 按镜头顺序拼接；引用上游 `$tN.output`。

## 写单要点
- 每个镜头独立单子，依赖用 depends_on 串成主链。
- 积分预算按镜头数预留，不够先告诉用户。
