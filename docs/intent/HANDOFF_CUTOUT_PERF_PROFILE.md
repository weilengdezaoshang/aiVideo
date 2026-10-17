# 转交：抠图识别链路测速 · 定位瓶颈

> 日期：2026-10-17  
> 仓库：aiVideo（本机路径优先找用户 Mac 上的工作区；box 上有一份 `/workspace/aivideo` 可对照）  
> 背景：用户反馈「识别图片特别慢」，Mac（Apple Silicon）本地跑 rembg，默认模型 `birefnet-portrait`  
> 目标：用数据判断瓶颈在哪一步，再给优化建议；**本任务只测速与报告，不改产品逻辑**（可加临时打点，测完可 revert）

---

## 一句话

给 `POST /api/cutout/detect` → rembg → mask 整条链路打分段计时，用多尺寸样本跑冷启动/热启动，输出 CSV + 瓶颈结论。

---

## 复制给实现 / 测速 Agent

```text
你是 aiVideo「抠图识别性能剖析」Agent。中文输出。只做测速、采集、报告；不要改产品交互、不要换默认模型上线、不要接 Provider 冒充抠图。

# 背景
- 用户 Mac 本地识别偏慢。
- 现状：进程内 rembg；默认 `CUTOUT_MODEL=birefnet-portrait`；`backend/cutout.py` 里 session 懒加载；`ThreadPoolExecutor(max_workers=1)`；detect 与 apply 分离。
- 关键链路见 `docs/cutout-flow.md`：UI → POST /api/cutout/detect/{assetId} → rembg 推理线程 → 返回 mask PNG →（可选）POST /api/cutout/apply。

# 成功标准
1. 产出一份报告 `docs/intent/PERF_CUTOUT_DETECT.md`（或仓库同等位置）+ 原始数据 `docs/intent/perf-cutout-detect.csv`。
2. 报告必须用实测数字回答：「总耗时里哪一段占比最高」「冷启动 vs 热启动差多少」「分辨率如何影响」「换轻量模型能快多少（仅对照，不改默认）」。
3. 给出 P0 优化建议（按收益排序），每条对应数据证据。
4. 打点代码若提交，用独立分支/明确标注 TEMP；默认以可 revert 的方式留在本地即可。

# 禁区
- 不要为了「看起来快」静默降级到错误主体或假 mask。
- 不要把测速脚本依赖必须登录的云 Provider。
- 不要一次改一堆产品代码；临时打点要集中、好删。

# 要测的分段（缺一不可，单位 ms）
对每一次 detect 请求记录：

| span | 含义 |
|------|------|
| t_client_total | 浏览器/脚本从发 detect 到收齐 mask 的墙钟 |
| t_http_read_body | API 读入源图 bytes |
| t_queue_wait | 若单线程忙，排队等待（当前 429 则记为 blocked，另计） |
| t_session_load | rembg `new_session`（仅冷启动有） |
| t_infer | `remove(...)` 纯推理 |
| t_alpha_encode | PIL 抽 A 通道并编码 PNG |
| t_server_total | API handler 内总耗时 |
| t_apply | 若顺带测 apply：`apply_mask`（可选，单独表） |

额外字段（每行 CSV）：
- run_id, ts, cold_start(bool), model, source_w, source_h, source_bytes, mask_bytes
- device_hint（cpu / mps / coreml / unknown——从 onnxruntime providers 或环境探测）
- py_version, rembg_version, onnxruntime_version（能取则取）
- sample_name（见样本集）
- note（429/504/错误信息）

# 样本集（至少跑这些；没有现成图就用仓库/画布已有资产或生成纯色+人像近似图）
1. S-small：长边 ≈ 512
2. S-mid：长边 ≈ 1024
3. S-large：长边 ≈ 2048（若 OOM/超时记失败，不要强行）
4. S-portrait：真实人像一张（用户画布或 `docs`/`fixtures` 里已有图优先）
每张图：cold 1 次 + hot 3 次（重启进程清 session 后再 cold）。

# 模型对照（只测，不改默认上线）
在同一批样本上对比环境变量 `CUTOUT_MODEL`：
1. `birefnet-portrait`（当前默认）——必测
2. 一个明显更轻的：`u2netp` 或 `isnet-general-use`（仓库/rembg 已支持哪个用哪个）——对照
不要一次测超过 3 个模型。

# 实施步骤
1. 定位本机 aiVideo 仓库与启动方式（dev API 端口、如何设 `CUTOUT_MODEL`）。
2. 读 `backend/cutout.py`、`backend/app.py`（或 `production.py`）里 detect/apply 路径，确认当前有无打点。
3. **最小侵入打点**（二选一，优先 A）：
   A. 在 `Rembg.__call__` 与 `Recognition.detect` 内用 `time.perf_counter()` 记 span，写到 stderr JSON 行或临时文件 `/tmp/cutout-perf.jsonl`；响应头可加 `X-Cutout-Perf: {...}`（仅 DEV）。
   B. 若不愿改代码：用脚本直接调 `Rembg`/`Recognition`（绕过 HTTP）测 session_load+infer+encode；另用 curl 测一条完整 HTTP 作对照。两者都要有。
4. 探测推理后端：打印 onnxruntime `get_available_providers()`；Mac 上是否 CoreMLExecutionProvider / CPUExecutionProvider。写入报告。
5. 跑样本矩阵 → 汇总 CSV。
6. 写报告：表格 + 瓶颈判定规则：
   - 若 cold 的 t_session_load > 总耗时 40% → 瓶颈=模型加载（建议常驻预热）
   - 若 hot 的 t_infer 占比最高且随分辨率近似线性/超线性涨 → 瓶颈=推理（建议缩图再映射 / 换轻量预览模型）
   - 若 t_client_total ≫ t_server_total → 瓶颈=网络/前端读图/主线程
   - 若频发 429 → 瓶颈=单 worker 排队
   - 若只有 CPU provider → 记录「未走加速」为高优先级假设
7. 报告末尾给「下一步优化清单」（P0/P1），每条绑数据；不要空泛建议。

# 验收清单
- [ ] CSV 含 cold/hot × 多尺寸 × ≥2 模型
- [ ] 报告写明最大占比 span 名称与中位数 ms
- [ ] 写明 onnxruntime providers
- [ ] 优化建议 ≥3 条且可执行
- [ ] 打点可关闭或已说明如何 revert

# 交付
1. 聊天里先给结论三句话（瓶颈是什么、冷/热大概多少秒、第一优先改什么）
2. 附 `PERF_CUTOUT_DETECT.md` + `perf-cutout-detect.csv` 路径
3. 若用户 Mac 可达，拷一份到 Downloads
```

---

## 测完后预期优化方向（给测速 Agent 对照，不是本任务要做的）

| 若数据指向… | P0 动作 |
|-------------|--------|
| session 冷加载很重 | 进程启动预热 `new_session`；健康检查里 warm-up |
| infer 随分辨率暴涨 | detect 前长边缩到 768/1024，坐标/mask 再放大 |
| 只有 CPU EP | 查 onnxruntime-silicon / CoreML EP 能否启用 |
| 单线程 429 | 队列+进度事件；或限制并发提示，勿盲目加 worker |
| 轻量模型快一个数量级且人像可接受 | 预览用轻模型、确认后再 birefnet（样片→正片） |

---

## 相关文件

- `apps/api/backend/cutout.py` — rembg 懒加载、单推理线程
- `apps/api/backend/app.py` / `production.py` — `/api/cutout/detect`、`apply`
- `docs/cutout-flow.md` — 时序
- 默认模型：`CUTOUT_MODEL`，缺省 `birefnet-portrait`
