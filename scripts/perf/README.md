# 性能采集入口

当前 PostgreSQL / RabbitMQ / Redis / Celery 后端使用独立协议 `durable-backend-baseline-v1`：

```sh
.venv/bin/python scripts/perf/collect_durable_baseline.py \
  --output /tmp/aivideo-durable-baseline-new-run --repeats 50
```

需要项目 `.venv` 依赖、Docker 及 loopback 访问权限。脚本新建临时隔离基础设施，仅用合成数据和 Mock，自动迁移与清理；输出目录必须为空。默认测当前鉴权 API 读接口、SSE 和真实 prefork 执行链路。`--stages chain` 可在新空库单独测链路；单独采样与带背景数据的完整采样需要分开解释。原始请求、SQL、执行计划、事件 trace、源码/采集器指纹都会保留。

报告与口径见 [2026-10-05 当前后端基线](../../docs/performance/durable-baseline-2026-10-05/README.md)。`backend_baseline.py` 使用历史 sandbox；旧后端采样与本协议不能混合计算架构优化收益。下方为独立的画布采样协议。

# 画布性能采集 V3

目的：让改造前后有相同输入工作量、可追溯原始记录和正确性校验。这个工具采集实测值，不把架构目标当作收益。

## 运行

需要 Node 22+（全局 WebSocket）、项目依赖、`.venv` 和 Chrome。默认 Chrome 路径为 macOS 安装路径，其它平台通过 `--chrome PATH` 指定。

```sh
npm run build:web
.venv/bin/python scripts/perf/server.py --port 7815
```

服务绑定 loopback，每次创建独立临时数据目录，显式使用 Mock 配置，不读取真实 Provider 密钥。固定种子场景为 500 和 5000 个文字对象。保留旧数据和旧报告，不与本协议混用（协议为 canvas-native-v3）。

另一个终端采集：

```sh
node --import tsx scripts/perf/run.ts --label baseline --repeats 3 --dpr 2 --out /tmp/canvas-baseline
```

每个场景执行静置、固定输入流平移、固定工作量平移、全选。可用 `--scene text-5000 --action pan` 选测。每次打开全新页面并清空缓存/本地草稿，文档打开并稳定 2 秒后计时。当前是文字场景的稳态测试，不是冷启动或图片/视频就绪验证。

改造后重新构建，保持相同条件和采集代码：

```sh
npm run build:web
node --import tsx scripts/perf/run.ts --label candidate --repeats 3 --dpr 2 --out /tmp/canvas-candidate
node --import tsx scripts/perf/compare.ts /tmp/canvas-baseline /tmp/canvas-candidate
```

两个结果目录必须是不同目录。结果支持分开机器运行，但比较器会检查环境与场景兼容性。正式评估应交替跑旧/新构建，至少 3 轮；短样本只用于诊断，不宣称统计显著。记录同一设备负载、供电、刷新率和温度状态，避免边跑全仓测试边做正式测量。

## 输入与有效性

- 输入由浏览器外部的 Node 时钟通过 CDP 发送，产生真实、可信的浏览器事件。
- 平移固定 60 次移动、120×60 屏幕像素、约 3 秒轨迹，输入次数不依赖页面 FPS。
- 输入调度不等待 CDP 的传输确认。确认延迟只记录为运输诊断，不当作页面处理时间。
- 外部调度超过 100ms 期限时不追赶突发发送；标记节奏不合格，保留全部记录。
- 固定输入流平移允许浏览器合并中间移动，但实际坐标必须是发送轨迹的有序子集，起止操作次数及最终位移必须正确。报告实际观察次数，不能把事件合并误判成采集失败。
- `pan-work` 逐次等待确认并核对页面已观察到该移动，要求完整60次输入。它没有固定输入节奏，只比较固定工作量完成代理耗时，不比较其FPS。
- 隐藏页面、非可信输入、轨迹不符、动作未生效时不计算体验收益。
- 输入节奏超过期限时，固定输入流FPS比较无效。固定工作量及全选的完整结果通过校验后，独立比较完成代理耗时。

浏览器运行在独立临时 profile，使用它自己公布的随机调试端口，明确设置前台和焦点，结束后关闭自身进程。不会连接用户已有浏览器调试端口。默认 headless；它是可重复测试环境，不代表所有有窗口/GPU设备的绝对表现。

## 无探针与诊断分开

`clean` 模式不包装业务/绘制函数，也不订阅机制计数器；有低开销 rAF、输入和 PerformanceObserver 采集。它仍不是完全零扰动，正式比较必须使用相同采集器。

```sh
node --import tsx scripts/perf/run.ts --label diagnosis --mode diagnostic --scene text-5000 --action pan --repeats 1 --out /tmp/canvas-diagnosis
```

`diagnostic` 额外提供：

- `documentNotifications`、`viewNotifications` 的动作窗口内次数。
- `Konva.Layer.drawScene/drawHit` 的准确调用数、累计包含内部调用耗时、单次最大值。
- Chrome CPU `.cpuprofile` 文件，用于采样定位函数/调用栈；CPU采样并不是每个函数准确调用次数。

诊断模式不计算体验收益百分比。以后新增投影/缓存计数器时，只在诊断模式接入；没有采集的字段保留缺失，不填 0。当前绘制计数覆盖该 Konva 后端；更换后端必须给新后端增加对应计数，不能用“旧函数消失”证明总绘制工作消失。

## 输出和指标含义

每轮输出 JSON、动作完成后的截图，诊断另有 CPU profile。环境文件记录浏览器版本、CPU、内存、DPR、场景 checksum、源文件/构建/采集器 hash。JSON 保留每个帧间隔、原生输入、计划/实际发送/确认时刻、Event Timing、长任务、初末状态和无效原因。

- rAF FPS / P95：页面帧推进代理及间隔，不是 GPU 呈现帧数，也不是某函数耗时。
- 完成耗时：从输入开始到序列结束及两次 rAF 机会，含固定轨迹时间与浏览器通信开销；不是纯计算耗时或严格像素呈现时刻。
- Event Timing：阈值为 16ms，数据有量化；最大观察值不是线上 INP。未观察到记录是 null，不代表 0ms。
- JS 堆：操作前后的描述性数值，不包括全部位图、媒体、GPU 内存，不证明泄漏。
- 不支持的指标为 null，不能当成 0。

比较器核对场景、轨迹、初始相机/选区/视口、DPR、Chrome 版本、机器与采集器。条件不一致、样本不足、诊断样本会拒绝计算收益比例，同时保留实测数值及原因。

当前覆盖文字画布的固定输入流、固定工作量平移与全选。冷启动、素材解码、视频、导出、保存、长时内存回收需要独立场景协议，不能从此处推导收益。原有 DPR1 函数采样与 DPR2 帧率数据也不能直接纳入 V2 比较。

## 方案证据审计（2026-10-01）

结论：V3 可以作为文字画布稳态交互的实验室基线，不能视为完整前端性能覆盖，也不能据此宣称架构改造收益。已重新读取24轮原始JSON、复算5000对象场景中位数，并运行4项采集有效性测试。动作校验通过只表示满足当前协议，不表示所有指标已经完备。

| 方案判断 | 当前证据 | 结论边界与下一项验证 |
|---|---|---|
| 大量文字对象时交互明显变慢 | 5000对象固定输入流23.45 rAF FPS，帧间隔P95 66.70ms；完整60次输入完成代理3431.65ms | 支撑该环境的卡顿结论，不代表真实用户总体；需要有窗口浏览器复核 |
| 绘制与命中图有优化空间 | 独立诊断中drawScene 60次/1164.20ms，drawHit 60次/299.70ms | 包含内部调用的同步墙钟耗时，不是GPU耗时或空间查询耗时；不能将两项简单解释为总体占比 |
| 鼠标操作导致大量React组件刷新 | 当前协议未采集组件提交次数、对象组件render次数或actualDuration | 暂无当前基线证据；先补React Profiler与对象更新计数，不能仅凭视图通知推断 |
| 平移不断修改文档 | 当前诊断文档通知0次、视图通知65次 | 当前证据不支持；模型分离属于架构约束，不能预测它会减少已有的0次文档通知 |
| 可见区域裁剪与空间索引能降低成本 | 未采集可见/候选/实际绘制对象数，drawHit测的是命中图绘制 | 需要对象数量计数与独立命中查询计时，再用开关实验验证 |
| 脏更新和离屏缓存能提高帧率 | 未实施，无缓存命中、失效、重画面积数据 | 属于待验证假设；采集重画量、缓存生成/复用耗时和内存预算，进行同协议A/B |
| Worker能降低主线程阻塞 | 未实施，无通信/排队/过期结果数据 | 采集主线程长帧、消息大小、队列等待、Worker工作时间和端到端反馈；单纯离屏不等于多线程 |
| 减少对象或组件能降低内存 | 只有操作前后JS堆描述值 | 不支持泄漏或总内存结论；重复开关文档并比较GC后堆快照，另记缓存位图字节估算 |

### 与前端及画布指标的对应

- 页面加载：尚未采集LCP、CLS和文档可交互时间。Canvas文档就绪需单独定义业务里程碑，不能假设LCP等于画布内容就绪。
- 离散交互：已有阈值16ms的Event Timing；不是页面生命周期INP，也不覆盖连续拖动全过程。参考[INP定义](https://web.dev/articles/inp)。
- 连续画布交互：已有rAF间隔分布，符合帧推进代理的用途；不是实际呈现帧。窗口从collector.start到stop，包含动作准备、操作、两次rAF及300ms收尾，不是纯拖动区间。参考[rAF定义](https://developer.mozilla.org/en-US/docs/Web/API/Window/requestAnimationFrame)。
- 长时间阻塞：已有Long Tasks；尚缺Long Animation Frames及Chrome Performance trace，不能从CPU profile单独还原布局、绘制与GPU呈现。参考[LoAF定义](https://developer.chrome.com/docs/web-platform/long-animation-frames)。
- 组件更新：尚缺[React Profiler](https://react.dev/reference/react/Profiler)记录；诊断有额外开销，与普通体验测试分开。
- 内存：尚缺重复循环与[堆快照对比](https://developer.chrome.com/docs/devtools/memory-problems/heap-snapshots/)。performance.memory不是完整浏览器内存；缓存字节估算也不等于实际GPU占用。

### 收益发布条件

1. 先补证据缺口，再选择相应优化；未测量的瓶颈写为假设。
2. 增加字体就绪检查和页面内操作/渲染完成版本标记；保持原有完成代理作为单独字段，不能重新命名为实际呈现时间。固定60次输入并不保证渲染了60帧。
3. 新增纯交互分段、标准Web Vitals、LoAF、组件与工作量计数时升级协议，并重新采集基线；新旧协议不混算。
4. 正式评估交替执行A/A和A/B，预热与缓存政策一致，显示每轮数值及波动。现有3轮中位数仅为初步描述，不能证明小幅改进显著；首轮正式实验建议每组至少10轮，并根据波动增加样本。
5. 使用有窗口浏览器验证用户体验，记录显示刷新率、供电及设备负载；headless结果单独报告。
6. 优化需同时满足结果正确、体验指标改善、目标工作量减少，并检查内存及其他场景没有明显退步。截图目前用于审查，不是自动视觉一致性证明。
7. 目前没有优化后的candidate结果，因此只能发布基线与瓶颈证据，不能发布“从X秒降到Y秒”。
