# 抠图 detect 本机性能剖析

测试日期：2026-10-17（Asia/Shanghai）。仅测速、采集和报告；没有修改产品代码、交互、默认模型或正在运行的 8443 服务，没有调用生成 Provider。

## 结论

1. **最大瓶颈是 CPU 推理**：默认模型热请求 `t_infer` 中位数 **21,475.8 ms**，逐请求占 handler 总耗时的中位数 **99.88%**；其中实际 ONNX `run()` 中位数 **21,208.1 ms**。抽 A 通道与 PNG 编码仅 **19.1 ms**。
2. 默认模型冷请求客户端中位数 **27.90 秒**，热请求 **21.56 秒**，两组中位数相差 **6.34 秒**。这不是纯加载因果差值：实际 `new_session()` 冷段中位数 **3.92 秒**，首次 import 另约 **0.79 秒**；存在明显运行波动。
3. `isnet-general-use` 热请求中位数 **1.39 秒**，约 **15.5 倍**速度、减少 **93.6%** 延迟。优先安排轻量模型的主体质量对照验收；本次仅作性能对照，**默认仍是 birefnet-portrait，不能静默替换**。

## 环境与测试边界

- 用户本机 Mac，Apple M2、16 GiB RAM、macOS 26.4.1、arm64；Python 3.11.15，rembg 2.0.77，onnxruntime 1.29.0，Pillow 12.3.0。
- `onnxruntime.get_available_providers()`：`CoreMLExecutionProvider`、`AzureExecutionProvider`、`CPUExecutionProvider`。**每次真实会话 `inner_session.get_providers()` 均只有 `CPUExecutionProvider`**；没有走 CoreML/MPS。可用列表不代表实际启用列表。
- 本机 rembg `sessions/base.py` 默认回退到 CPU；应用 `new_session(model)` 没有显式传 providers。`OMP_NUM_THREADS`、`ORT_NUM_THREADS`、`OPENBLAS_NUM_THREADS`、`MKL_NUM_THREADS`、`NUMBA_NUM_THREADS` 均未设置。
- 两份权重测试前已缓存：birefnet-portrait 约 928 MiB，isnet-general-use 约 170 MiB；**冷启动不含下载**，也没有清操作系统文件页缓存。冷指新进程、空 rembg session、首次 import，API 进程启动本身在请求计时前完成。
- 本机开发入口是 `https://localhost:8443/`，`npm run dev:stack` 启动；手动 API 热重载入口为 `npm run dev -- --env-file /absolute/path/to/dev.env`。`production.py` 工厂读取 `CUTOUT_MODEL`，在进程启动前设置；本次仅对子进程设置它。
- **HTTP 矩阵运行于独立回环地址、随机端口、单进程 FastAPI/Uvicorn**。通过 AST 提取当前工作区 `production.py` 的 detect handler 原样执行；沿用真实 `Recognition.detect`、单推理线程、取消 watcher、真实 rembg 和 JSON/base64 响应。
- 测试替代了鉴权、数据库资产查找与 Redis：固定本地身份、固定样本路径、始终无取消标记的本地 Redis 替身；源图实际从磁盘异步读取。`Rembg` 子类保持原调用顺序，仅加时钟与 ONNX run 透传代理。**这是隔离 HTTP 性能基线，不是完整 8443/数据库链路端到端测量**；没有伪造 mask 或调用 Provider。
- `t_server_total` 从 handler 调用前至 handler 完成（含 watcher 收尾），不含框架最终 JSON 序列化；客户端包含真实 HTTP、完整响应读取、JSON/base64 解析与蒙版尺寸验证，不含浏览器绘制或原图上传。后处理会略微扩大 client/server 差值。不能据此排除真实浏览器、OIDC、PG、Redis 或 TLS 链路的额外延迟。
- 顺序：先默认模型、后对照模型；每模型按 small/mid/large/portrait，每样本重启进程后 cold 1 + hot 3；合计 **32 次全部 HTTP 200**，无 429、504 或 OOM。顺序未随机化，非空闲专机；中途 load average 约 6.72/8.36/6.70，存在其他本机进程。每格仅 3 次 hot，不适合声称精确 p95 或尺寸回归系数。

## 样本

源图：`data/validation/portrait-cutout-preview.png`，1024×1024，已有双人人像照片内容、白色背景；不代表复杂背景或未经处理的相机原片。`S-portrait` 使用其 RGB 重编码版本；前三档由同图 Lanczos 缩放，控制内容变量。2048 为上采样，不能用它推断更高信息量照片的质量。`S-mid` 与 `S-portrait` 内容相同，但使用独立新进程，作为重复观测，不宣称四个独立场景。

| 样本 | 尺寸 | PNG 源字节 |
|---|---:|---:|
| S-small | 512×512 | 209,298 |
| S-mid | 1024×1024 | 683,400 |
| S-large | 2048×2048 | 1,919,144 |
| S-portrait | 1024×1024 | 683,400 |

源图与生成样本 SHA-256、版本和 providers 见 `perf-cutout-environment.json`。原始图片及模型不复制进仓库；缩放样本在 `/tmp/aivideo-cutout-perf/`，重跑脚本可重新生成。

## 分段定义与汇总（ms）

`t_http_read_body` 在此特指 API 读取源资产 bytes（`asyncio.to_thread(path.read_bytes)`）；detect 请求本身不上传图片 body。完整服务的 owned_asset 数据库查询未纳入此隔离值。

`t_queue_wait` 是 submit 到推理线程开始执行的调度等待。当前系统忙时立即 429，不保留请求队列；429 应标记 blocked、推理等未发生字段留空，不以 0 冒充一次推理成功。本矩阵串行请求，不能用零 429 推断多人并发无拥塞。

按要求，`t_infer` 测量完整 `remove(...)`；它实际上含 rembg 解码、归一化、推理、蒙版后处理与 RGBA PNG 输出，不能严格称为纯 ONNX。额外字段 `t_ort_run` 才是实际 `InferenceSession.run()` 墙钟（不含 session 创建）；它是 `t_infer` 的子段，不能重复相加。`t_alpha_encode` 是应用打开 remove 输出、转换 RGBA、提取 A、编码灰度 PNG。新增 `t_import` 独立记录懒 import，避免把它算进加载或漏掉。

以下为各段独立中位数，因此各列相加不一定等于 total 中位数；占比使用逐请求比值再取中位数。

| 模型/状态（次数） | client total | 读源图 | 调度等待 | session load | remove | alpha encode | server total | import | ONNX run |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| birefnet-portrait / cold (4) | 27,895.2 | 0.9 | 0.1 | 3,918.4 | 22,574.1 | 23.3 | 27,865.7 | 792.2 | 22,333.1 |
| birefnet-portrait / hot (12) | 21,558.5 | 2.6 | 0.1 | 0.0 | 21,475.8 | 19.1 | 21,532.9 | 0.0 | 21,208.1 |
| isnet-general-use / cold (4) | 2,342.1 | 0.9 | 0.1 | 320.9 | 1,314.6 | 14.8 | 2,337.6 | 683.4 | 1,195.8 |
| isnet-general-use / hot (12) | 1,388.6 | 0.3 | 0.0 | 0.0 | 1,363.3 | 14.5 | 1,385.1 | 0.0 | 1,188.7 |

## 冷热与尺寸矩阵（ms）

每格 cold 为单次、hot 为 3 次中位数；差值为同一格 cold 减 hot。由于机器与首次推理波动，差值可小于零，不能将其等同于模型加载时间。

| 模型 | 样本 | cold client | hot client 中位 | hot client 范围 | cold−hot | cold session | hot remove | hot alpha |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| birefnet-portrait | S-small | 33,960.1 | 23,964.2 | 19,400.1–29,166.8 | 9,995.9 | 5,167.9 | 23,942.0 | 9.8 |
| birefnet-portrait | S-mid | 41,091.5 | 21,750.0 | 20,600.6–26,413.0 | 19,341.5 | 4,075.0 | 21,654.8 | 30.5 |
| birefnet-portrait | S-large | 21,830.2 | 21,367.0 | 20,634.5–34,279.0 | 463.2 | 3,761.9 | 21,296.9 | 56.9 |
| birefnet-portrait | S-portrait | 20,135.4 | 21,042.3 | 20,537.0–22,481.3 | -906.8 | 3,548.7 | 21,011.5 | 16.3 |
| isnet-general-use | S-small | 2,251.8 | 1,242.6 | 1,194.9–1,818.8 | 1,009.2 | 323.8 | 1,236.2 | 4.4 |
| isnet-general-use | S-mid | 2,553.0 | 1,320.6 | 1,317.0–1,456.7 | 1,232.4 | 332.7 | 1,299.8 | 14.5 |
| isnet-general-use | S-large | 2,432.4 | 1,489.4 | 1,482.2–1,492.2 | 943.0 | 313.4 | 1,428.4 | 52.3 |
| isnet-general-use | S-portrait | 2,221.0 | 1,266.7 | 1,257.0–1,759.6 | 954.3 | 318.1 | 1,249.8 | 14.5 |

| 样本 | 默认 hot / 轻量 hot | 客户端延迟减少 |
|---|---:|---:|
| S-small | 19.3 倍 | 94.8% |
| S-mid | 16.5 倍 | 93.9% |
| S-large | 14.3 倍 | 93.0% |
| S-portrait | 16.6 倍 | 94.0% |

### 尺寸影响判断

- birefnet-portrait：512→2048，像素数 16 倍，hot client 23,964.2→21,367.0 ms（0.89 倍）；alpha encode 9.8→56.9 ms。
- isnet-general-use：512→2048，像素数 16 倍，hot client 1,242.6→1,489.4 ms（1.20 倍）；alpha encode 4.4→52.3 ms。

两模型 session 的 normalize 均固定为 **1024×1024**，预测 mask 再 resize 回原图尺寸。较大源图主要增加源图处理、RGBA 输出及 alpha 编码成本；本组数据没有显示推理随源像素数线性或超线性增长。默认模型尺寸间差异小于运行波动，不能声称大图更快，也不能把上传源图缩到 512 当作主收益方案；若要降低模型内部输入尺寸，须另做模型契约及边缘质量验证。

## 按预定规则判定瓶颈

- birefnet-portrait 冷启动加载占比 9.9%–17.6%，中位 16.2%，均未超过 40%；**不判为加载主导**。hot remove 占比中位 99.88%，**判为推理主导**；“随源分辨率线性增长”这一附加条件不成立。
- isnet-general-use 冷启动加载占比 12.9%–14.4%，中位 13.7%，均未超过 40%；**不判为加载主导**。hot remove 占比中位 98.82%，**判为推理主导**；“随源分辨率线性增长”这一附加条件不成立。
- birefnet-portrait hot 的 client−server 中位 11.5 ms，最大 65.2 ms；隔离回环测试未出现 client ≫ server。完整浏览器/开发服务的读图和主线程尚未测量，不下排除结论。
- isnet-general-use hot 的 client−server 中位 2.0 ms，最大 4.8 ms；隔离回环测试未出现 client ≫ server。完整浏览器/开发服务的读图和主线程尚未测量，不下排除结论。
- 429：串行矩阵 0/32，无频发证据；代码明确单槽忙时 429。不能将 `t_queue_wait` 的低值解读为并发容量充足。
- 加速：实际只有 CPU provider；**“未走加速”成立**，CoreML 能否提升、提升多少仍需实测，不能把“可用”写成“已加速”。

## 下一步优化清单（建议，未实施）

P0 按本次已量化的收益优先排序；未知收益项单列验证，不虚构加速倍数。

1. **P0：安排 isnet-general-use 的质量门槛对照，再决定是否提供明确选择或采用。** 相同样本热客户端从 21.56 s 降至 1.39 s，中位降幅 93.6%，每格快 14.3–19.3 倍。用同批复杂主体、发丝、透明边缘与多人物样本并排核对主体一致性与边缘；通过门槛再决定模型策略，不能只靠本次白底人像计时替换默认。验收：速度收益复现、无主体错误；本任务不改默认。
2. **P0：保持当前模型，验证进程常驻预热。** 默认冷段 session 加载中位 3.92 s、import 中位 0.79 s，合计约 4.71 s 可从首次交互请求移到启动阶段；全图预热还可能减少首次执行差异，但本次未单独量化。实现候选为后台启动时在同一识别 executor 预建 session，并以显式 ready 状态避免与用户请求争槽。验收：重启后首个请求无 session load；不提前加载/缓存用户 mask；评估启动耗时与常驻内存。不要承诺 cold−hot 的全部 6.34 s 都由预热消除。
3. **P0：做同模型显式 CoreML 的独立 A/B 验证。** 实际 CPU ONNX 热中位 21.21 s，占主要预算；环境已有 CoreMLExecutionProvider，但 rembg 默认未选它。只在实验子进程显式传 providers，记录实际 providers、节点回退、首次编译、hot 延迟和内存，并比较 mask 数值/视觉差异。收益尚未知，不能直接上线或声称 MPS 已支持；保留 CPU 对照。这是优先验证的加速假设。
4. **P1：固定负载并随机化顺序复测，增加热样本。** 默认模型热范围 19.40–34.28 s，同内容 1024 两组 hot 中位 21.75/21.04 s，冷−热还出现负值。先记录 CPU/内存/温度、实际 session 线程配置，独立子进程比较 OMP_NUM_THREADS=1/2/4/默认，至少每格 10 次；不要全局改用户线程环境。验收：报告分布、方差与相同质量下稳定收益。
5. **P1：补完整 8443 与浏览器链路、并发拒绝率。** 此次回环差值很小但未测 PG/Redis/鉴权和浏览器 decode/绘制；在独立开发测试工作区以现有登录身份测 detect、浏览器收图和长任务，再做 1/2/4 并发，逐行记录 429=blocked。默认单次热约 21.56 s，理论单槽约 2.8 次/分钟（仅预算估算，非吞吐压测）。先判断并发需求，再讨论容量；不要简单增加线程把 16 GiB Mac 推向 OOM。

不优先投入：仅减少源图片尺寸或抠图后的 alpha PNG 编码。默认 hot alpha 中位仅 19.1 ms，远小于 21.5 s 的 remove，优化上限不足以改变体感。未测 apply：它与 detect 分离，本报告不把合成耗时混入识别。

## 复现、关闭与交付

```sh
# 从仓库根目录；使用已有 .venv 与本地权重，不需要云账号
.venv/bin/python scripts/perf/cutout_detect.py
```

- **TEMP 打点只在独立脚本中**：`scripts/perf/cutout_detect.py`。不导入到应用入口，不改 `backend/cutout.py` 或 `production.py`，无需功能开关；不运行脚本即完全关闭。可删除此脚本及 `/tmp/aivideo-cutout-perf/` 清理实验，不要对已有用户改动执行 git reset/revert。
- 重跑会覆盖本报告同目录的 CSV 和环境 JSON；历史实验请先复制保存。模型参数只影响脚本子进程；正常服务模型不变。
- 原始数据：`docs/intent/perf-cutout-detect.csv`，32 行，全部包含要求字段；单位 ms，cold bool 为 True/False。额外保留 import、实际 ONNX run、实际 providers 和状态码。
- 环境与样本哈希：`docs/intent/perf-cutout-environment.json`。原始服务日志与末次请求 span JSON 在 `/tmp/aivideo-cutout-perf/`，CSV 是每次请求的持久汇总。
- 当前工作区有大量既有修改，均未作为本任务修改或提交。未新建提交、未切换分支，未修改 HTTP/存储契约，因此未连接独立集成测试数据库，也未访问生产数据或付费凭据。

## 验收

- [x] cold/hot × 三档尺寸及人像 × 两模型；每格 1 cold + 3 hot。
- [x] 最大占比 span、分段中位数、ONNX available/actual providers。
- [x] 冷热差、尺寸影响、轻量模型加速幅度均有原始数据。
- [x] ≥3 条具体优化建议，注明实测收益与待验证假设。
- [x] 集中 TEMP、默认关闭、清理方式明确；未改变产品行为。
- [ ] 完整既有 8443/PG/Redis/浏览器链路的各段实测：本次隔离基线不覆盖，后续补充。

仓库验证结果：`npm run verify` 全部通过（typecheck、前端构建、eslint、前端测试、creation-server 校验及后端检查）；后端 645 passed、2 skipped、1 条既有 Starlette/anyio 弃用警告。测速脚本通过 `py_compile`；CSV 经结构核验，32 行、8 组、每组 1 cold + 3 hot、七个必需 span 均为非负数，mask 均为可解码且与源图同尺寸的 PNG。
