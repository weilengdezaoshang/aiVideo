# TECH_LAYERED_INTENT v1.2 变更说明

相对 v1.1：

1. **问题定性**：生产复杂场景下纯正则不可行；写明现网双词表与复杂句失败模式。
2. **主路径**：L1/L2 改为 exemplar **embedding 检索** + 分数档位；正则降为强门闩（否定/极强形式/入口冲突）。
3. **流程**：③ 后增加 ④′ 门闩，再 ④⑤ embedding；仍只在 possibleL2 内投票。
4. **降级**：embedding 失败 → 门闩+兜底+clarify，禁止默认视频。
5. **分期**：P0 即上 embedding（不再等「上线后才上模型」）；P1 重排/校准/exemplar 回流。
6. **验收/埋点**：增 E01–E06 与 embed 字段。
7. **待确认**：增 embedding 部署方式、弱动态默认 clarify。

未改锁定项：总流程、不降级生图、multiframe 默认、CONTEXT_REQUIRED、clarify 绑定、L3 不推翻 L2、冲突以本文为准。
