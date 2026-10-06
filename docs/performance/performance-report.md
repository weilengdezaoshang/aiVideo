这份报告汇总 aiVideo 的画布交互、网页加载、前端体积、保存、后端接口和采集链路。我们按场景说明等待发生在哪里，再区分已经测到的改善、出现的退化和仍然缺少的数据。

多节点平移已经测到改善：在最新构建的组合对照中，2 万个文字节点完成 60 次平移的观测耗时从 10.746 秒降到 2.178 秒，按本轮报告口径减少 79.7%。这组实验同时开启了可见区域剔除、Worker 和分块缓存，不能把全部收益归给其中一个模块。

大文件读取、真实文字编辑和稳定内存收益仍然需要分别处理。12 MiB 文档只有 500 个节点的时候，组合优化没有明显缩短首次打开时间；文字编辑的最新对照也没有证明整体提速。后端保存和列表接口另有随文档规模增长的等待，这部分不会随着画布渲染优化自动消失。

## 怎样阅读这些指标

| 指标 | 在场景中的含义 | 阅读时需要保留的条件 |
|---|---|---|
| 操作完成观测值 | 从开始操作到渲染器工作结束，再等待两次浏览器帧回调 | 含控制通信或检查等待；没有确认像素真正上屏 |
| rAF 帧推进频率 | 操作期间浏览器每秒推进多少次帧回调 | 越接近本次 60 Hz 环境的 60 次/s 越稳定；不等于 GPU 实际呈现帧率 |
| 帧间隔 P95 | 95% 的已观察帧间隔不超过该值 | 越低越好；少量长间隔会让操作出现停顿 |
| 长任务 | 主线程连续占用超过 50 ms 的任务 | 记录次数和耗时，帮助找到卡顿窗口 |
| 首次打开就绪 | 导航、读取文档、解析、字体和渲染器首次工作完成 | 不能解释成单个绘制函数的耗时 |
| FCP / LCP | 首个内容出现 / 观察窗口中最大内容出现的时间 | LCP 不覆盖 Canvas 内容；不能代替画布可操作时间 |
| CLS | 页面元素意外移动的程度 | 是观察窗口的值，不代表完整访问生命周期 |
| INP | 浏览器 SDK 记录的点击、键盘等交互响应指标 | 短时验收快照不代表线上用户分布，也不能代替连续平移测试 |
| JS 堆 / 位图估算 | JavaScript 对象占用的采样 / 按像素数计算的 RGBA 缓冲大小 | 都不是浏览器总内存，不能直接证明泄漏已经修复 |
| 函数自身 / 含子调用耗时 | 函数本身的工作 / 整条同步调用链的工作 | 父子调用可能嵌套，累计时间不能直接相加 |

实验至少分成三类：架构优化的前后对照、当前实现的基线、采集机制的验收。只有相同协议、场景和环境下的有效对照，才用于计算对应场景的收益。下文保留三类结果的区别。

## 历史画布优化

各行来自不同的独立实验，不能把百分比叠加，也不能把不同版本的数字当成同一次前后对比。

| 场景 / 改动 | 基线 | 优化后 | 变化与结论 |
|---|---:|---:|---|
| 5000 文字，固定 60 次平移，开启可见区域剔除 | 3204.10 ms / 3.20410 s | 2400.00 ms / 2.40000 s | 操作完成观测值耗时减少约 25.1% |
| 同一剔除实验，绘制优化组节点 | 5000 | 3850 | 减少 23%；优化组数不等于 GPU 实际绘制数 |
| 同一剔除实验，交互 rAF 帧推进代理 | 36.91 次/s | 51.47 次/s | 不是 GPU 呈现帧率 |
| 单张离屏缓存 → 分块缓存，真实文字编辑 | 61.95 ms / 0.06195 s | 61.50 ms / 0.06150 s | 仅减少 0.45 ms，未证明明显提速 |
| 同一分块实验，单次编辑实际请求重画面积 | 7,930,656 像素 | 524,288 像素 | 面积减少 93.4%，不代表耗时同幅下降 |
| 同一分块实验，低缩放位图保留估算 | 30.25 MiB | 5.00 MiB | RGBA 估算下降；不是总内存 |
| 同一分块实验，高缩放编辑后位图保留估算 | 30.25 MiB | 52.00 MiB | 反而增长，尚未解决旧比例缓存保留问题 |
| 对象增量维护，独立 Node 逻辑试验 | 3.122 ms / 0.003122 s | 0.047 ms / 0.000047 s | 维护逻辑减少约 98.5%；未包含 React、投影、真实 Canvas、Worker |
| 同一增量实验，单对象修改的字体分类测量调用 | 5000 次 | 1 次 | 工作量下降，经新版浏览器 36 次编辑核对 |
| 最新增量更新，浏览器真实编辑完整操作完成观测值 | 59.15 ms / 0.05915 s | 61.45 ms / 0.06145 s | 慢约 3.9%；没有整体编辑提速证据 |
| 同一增量实验，编辑后缓存估算中位数 | 52 MiB | 52 MiB | 未证明稳定内存收益 |

文字编辑旧/新版交替各三轮，每轮 12 次，共 72 次：旧版每轮中位数为 94.05、55.30、59.15 ms；新版为 62.90、61.45、60.55 ms。采用各轮中位数的中位数，只有三对样本，不作统计显著性结论。

历史环境：Apple M2、16 GiB、Chrome 154、headless、1440×900、DPR 2。大部分结果为固定种子文字文档。操作完成观测值包含控制通信、渲染队列稳定与两次 rAF 机会，不是像素真正上屏时刻，也不是 INP。不同阶段的 CPU / 等待可以嵌套或重叠，不能全部相加。

原始证据根目录：`/Users/weilengdezaoshang/.codex/visualizations/2026/09/30/01a0f0fa-9cf1-76d3-87d7-c3b2d63ea9fe/aivideo-performance`。

- 可见区域剔除：`spatial-culling/report.html`、`spatial-culling/clean/`。
- 分块缓存：`tiled-cache/summary.json`、`tiled-cache/clean/`、`tiled-cache/memory/`。
- 最新增量维护：`incremental-document/report.md`、`incremental-document/paired/`、`incremental-document/maintenance-proof.json`。缺少公共样式的旧试验已排除，正式结果使用 paired 数据。
- 采集链路验收：`telemetry-collection/report.html`、`telemetry-collection/export/events.jsonl`。528 条全部落盘、3 个 Mock 任务关联成功；该结果证明采集有效，不是优化收益。

## 大文件与多节点的复测条件

复测已完成：主矩阵 72 次操作，另有固定 500 节点的 6 次文件大小对照。分别覆盖“节点数增加”与“文档 JSON 增大”，防止将文件读取成本与节点绘制成本混为一谈。

本轮控制组与优化组使用相同最新源码和构建：控制组为 Konva、关闭剔除；优化组为 Worker + tiles + 剔除。因此这是当前构建下实验开关的对照，不是旧源码和新源码的历史比较。线上采集关闭，两组均使用相同 V6 基准采集器。

## 大文件与多节点的复测结果

主矩阵：4 个场景 × 2 个模式 × 3 个动作 × 3 轮 = **72 次有效动作**。固定工作量平移均完整观察到 60 次移动；环境、场景、构建与采集器指纹一致，所有动作正确性检查通过。baseline=Konva+剔除关闭+single；optimized=Worker+剔除开启+tiles。组合对照不能单独归因到某一个模块。线上遥测关闭。

### 平移和全选的等待

操作完成观测值：操作开始 → 渲染器 ready/pending 清空 → 两次 rAF；包含 CDP 通信和稳定性等待，未包含 GPU 上屏确认。表中各项为三轮中位数。

| 场景 | 响应体大小 | 60 次平移：控制 → 优化组 | 耗时变化 | 全选：控制 → 优化组 |
|---|---:|---:|---:|---:|
| text-5000 | 0.641 MiB | 3.175 s → 2.142 s | 减少 32.5% | 124.30 ms → 128.10 ms |
| text-10000 | 1.288 MiB | 5.525 s → 2.156 s | 减少 61.0% | 151.60 ms → 123.70 ms |
| text-20000 | 2.620 MiB | 10.746 s → 2.178 s | 减少 79.7% | 226.50 ms → 121.80 ms |
| large-json-12mib-500 | 12.062 MiB | 1.166 s → 1.139 s | 减少 2.3% | 116.60 ms → 118.50 ms |

### 平移卡顿和首次打开

首次打开就绪包括导航、文档读取、JSON 解析、字体检查和渲染器首次缓存就绪；不是纯渲染函数耗时。缓存冷启动按每次导航清浏览器缓存处理，不等于操作系统磁盘缓存清空。

| 场景 | 首次打开就绪：控制 → 优化组 | 平移纯交互 rAF：控制 → 优化组 | 帧间隔 P95：控制 → 优化组 | 采集窗口长任务数：控制 → 优化组 | 挂载节点：控制 → 优化组 |
|---|---:|---:|---:|---:|---:|
| text-5000 | 0.550 s → 0.655 s | 41.09 → 59.89 次/s | 33.40 → 16.80 ms | 1 → 0 | 5000 → 3850 |
| text-10000 | 1.000 s → 0.855 s | 13.12 → 59.91 次/s | 116.70 → 16.80 ms | 35 → 0 | 10000 → 3850 |
| text-20000 | 1.986 s → 1.224 s | 5.73 → 59.86 次/s | 300.00 → 16.70 ms | 19 → 0 | 20000 → 3850 |
| large-json-12mib-500 | 0.680 s → 0.680 s | 60.04 → 60.05 次/s | 16.80 → 16.80 ms | 0 → 0 | 500 → 500 |

控制台打印的 rAF FPS 是全窗口值，包含完成检查与收尾；本表改用纯交互窗口。rAF 不是 GPU FPS。两万节点控制组纯交互帧数不足 30，本次只展示其描述值，不计算 FPS 提升百分比。帧节奏比较中，2 万节点控制组的三轮都未达到帧数要求；其他场景和优化组均有三轮合格样本。

### 文档读取和内存采样

HTTP 响应体耗时取同源 ResourceTiming.responseEnd-startTime，包含等待和完整下载，不包含 JSON.parse 和后续渲染。下列 JS 堆只是采样，未强制 GC、非进程总内存，不能据此认定泄漏已修复或总内存收益。

| 场景 | 响应体读取：控制 → 优化组 | 平移后 JS 堆：控制 → 优化组 | 优化组位图 RGBA 估算 |
|---|---:|---:|---:|
| text-5000 | 67.80 → 61.80 ms | 34.50 → 22.97 MiB | 5.00 MiB |
| text-10000 | 123.10 → 125.40 ms | 76.02 → 51.17 MiB | 6.00 MiB |
| text-20000 | 255.20 → 264.70 ms | 138.15 → 33.76 MiB | 6.00 MiB |
| large-json-12mib-500 | 100.80 → 98.50 ms | 19.24 → 19.75 MiB | 8.00 MiB |

### 固定 500 节点的大文件对照

额外 6 次测量，均为 Konva+剔除关闭，节点几何/文字相同，只增加一条未激活聊天栏中的合法 chat 文本。这是文档 JSON 体积测试，不是图片/视频解码或巨型可见文本渲染测试。每种大小三次导航、三次全选。

| 文档 | 响应体大小 | 响应体读取中位数 | 首次打开就绪中位数 | 全选完成中位数 |
|---|---:|---:|---:|---:|
| text-500 | 0.062 MiB | 7.50 ms | 0.163 s | 114.30 ms |
| large-json-12mib-500 | 12.062 MiB | 94.50 ms | 0.683 s | 122.90 ms |

### 这组实验能够说明什么

- 多节点持续平移的组合优化有效，不能由此推导所有动作同幅提速；全选和首次打开分别看表。
- JSON 文件变大和节点变多是两个独立维度：前者增加读取/解析成本，后者增加对象维护、React/Konva 及绘制成本。分块缓存不会消除整个文档的下载和解析。
- 本轮只有简单单行文字，混合图片/视频、旋转、复杂字体可能保守回退；未验证大型媒体解码或 GPU 显存。
- 未做旧源码重编译。本轮数字不能替换历史旧构建的结果，也不与旧轮数据混算收益。只有三轮，没有统计显著性结论。
- 当前缩放为自动适配内容；两万节点下可见挂载约 3850 个。这不等于两万节点同时在高缩放下可见，也不证明高缩放缓存预算问题解决。

## 画布复测证据

原始记录、截图、环境与指纹：[scale-2026-10-03](/Users/weilengdezaoshang/.codex/visualizations/2026/09/30/01a0f0fa-9cf1-76d3-87d7-c3b2d63ea9fe/aivideo-performance/scale-2026-10-03)；机器汇总：[summary.json](/Users/weilengdezaoshang/.codex/visualizations/2026/09/30/01a0f0fa-9cf1-76d3-87d7-c3b2d63ea9fe/aivideo-performance/scale-2026-10-03/summary.json)。

```sh
npm run build:web
.venv/bin/python scripts/perf/server.py --port 52109 --counts 5000,10000,20000 --large-mib 12
node --import tsx scripts/perf/run.ts --base http://127.0.0.1:52109 --renderer konva --culling off --cache-layout single --actions pan,pan-work,select-all --repeats 1 --out /tmp/scale-baseline-round-0
node --import tsx scripts/perf/run.ts --base http://127.0.0.1:52109 --renderer worker --culling on --cache-layout tiles --actions pan,pan-work,select-all --repeats 1 --out /tmp/scale-optimized-round-0
```
顺序为第1轮控制→优化组、第2轮优化组→控制、第3轮控制→优化组，前后都清缓存，使用相同构建；固定大文件控制另用 `--counts 500 --large-mib 12` 和 `--actions select-all --repeats 3`。fixture 自动创建独立 Mock 配置和数据，不读取真实 Provider 密钥。
验证：整仓 verify 通过（322 前端 / 331 后端，2 跳过）；CLI 新增 actions 过滤另有 typecheck/lint 检查。


## 五、构建体积与网页加载基线

这部分记录 2026 年 10 月 3 日采集的前端构建体积和网页加载结果。资源压缩、共享依赖、功能延迟加载和缓存策略尚未形成同条件下的优化后数据，因此这里只记录基线与待验证的收益，不把预估写成已完成的优化。

### 1. 构建产物体积

本次使用空的临时目录构建，排除了旧文件残留。统计范围是编译生成的文件，不包含直接提供的 HTML、CSS、图片、字体、Python 依赖或容器镜像。

| 范围 | 原始字节 | 逐文件 gzip 合计 | 逐文件 Brotli 合计 |
|---|---:|---:|---:|
| 全部产物 | 9,154,912（8.73 MiB） | 2,191,618 | 1,768,045 |
| source map | 7,299,464（6.96 MiB） | 1,618,803 | 1,288,277 |
| 不含 source map | 1,855,448（1.77 MiB） | 572,815（559.4 KiB） | 479,768（468.5 KiB） |

source map 是将打包后的代码映射回源码的调试文件。发布的时候单独保留这些文件，可以缩小部署目录；浏览器正常运行通常不会下载它们，因此不能把这部分体积下降理解为页面加载提速。

本次共生成 166 个文件，两次独立构建的大小、压缩大小和 SHA-256 完全一致。此前现有构建目录的 9,781,534 字节包含 626,622 字节旧产物残留，后续体积对比采用上表的干净构建结果。

gzip 使用 level 9，Brotli 使用 quality 11。压缩总量是每个文件独立压缩后的合计，用于固定条件下的体积比较。单个页面实际下载哪些文件、服务器是否启用压缩、浏览器是否命中缓存，需要以请求记录为准。

### 2. 网页采集条件与缓存含义

本次采集的是网页桌面端，使用独立的 Chrome headless 浏览器。窗口为 1440×900，DPR 为 1，CPU 不降速；下载限制为 10 Mbps，上传为 2 Mbps，延迟为 40 ms。服务使用隔离的 Mock 配置和临时数据。工作台显示两个文字文档，画布加载固定 500 个文字对象。

冷缓存（cold）表示清空浏览器缓存后打开页面，模拟首次访问。热缓存（warm）表示保留前一次访问的 HTTP 缓存，再次打开同一页面；应用本地存储会清空，以保持文档输入一致。热缓存仍然遵循服务器的缓存策略，资源可能重新验证或下载。

三个页面分别采集三轮冷缓存和三轮热缓存，共 18 轮，全部通过就绪和请求错误检查。下面的时间与下载量均为有效轮次的中位数。每页冷缓存独立，不包含跨页面共享缓存的收益。

### 3. 传输量与页面加载

| 页面 | 缓存 | 下载量 KiB | JS 下载量 KiB | FCP ms | LCP 观察值 ms | DOM 就绪代理 ms | CLS 观察值 |
|---|---|---:|---:|---:|---:|---:|---:|
| 落地页 | 冷缓存 | 1,201.3 | 229.6 | 548 | 548 | 538 | 0.0000 |
| 落地页 | 热缓存 | 231.1 | 229.6 | 364 | 364 | 345 | 0.0000 |
| 工作台 | 冷缓存 | 12,080.5 | 277.8 | 412 | 412 | 488 | 0.0334 |
| 工作台 | 热缓存 | 1,000.9 | 277.8 | 396 | 456 | 456 | 0.0334 |
| 画布，500 个文字对象 | 冷缓存 | 702.5 | 626.3 | 696 | 864 | 874 | 0.0017 |
| 画布，500 个文字对象 | 热缓存 | 692.1 | 626.3 | 680 | 844 | 862 | 0.0017 |

下载量来自浏览器收到的完成请求，包含 HTML、API、字体、图片、JS、CSS 和接收侧协议开销，排除 SSE 与 WebSocket。1 KiB 为 1,024 字节，沿用原始汇总中的数值，并明确其单位。

FCP 表示首个内容出现的时间；LCP 观察值表示采样结束前最后一个最大内容绘制优化组，Canvas 内容不属于其优化组。CLS 观察值描述采样期间的布局偏移，不代表完整访问生命周期。

DOM 就绪代理通过页面结构判断：落地页标题出现，工作台项目加载骨架消失并出现输入框，画布文档报告 500 个对象且 Canvas 已挂载；之后再等待一次 rAF。它能用于相同条件下的复测，但没有证明实际操作完成或像素已经呈现在屏幕上。

### 4. 已确认的资源问题

工作台四张示例 PNG 的接收量合计 9,647,247 字节，约 9.20 MiB，占冷缓存传输的主要部分。图片压缩和尺寸调整应当优先验证，再分别检查首屏与完整资源加载的变化。

三个页面的 JS 响应均未启用传输压缩，热缓存下的 JS 下载量与冷缓存基本相同。这为压缩和缓存优化提供了对比依据，但只描述本次 Mock 服务，不能据此判断生产反向代理的行为。

### 5. 后续优化的指标与收益口径

| 优化手段 | 需要对比的指标 | 当前可以说明的收益 |
|---|---|---|
| 发布时排除 source map | 部署目录原始字节 | 按本次产物，8.73 MiB 可降至 1.77 MiB，减少约 80%；尚未修改发布流程 |
| 构建前清理旧文件 | 干净构建体积、发布文件清单 | 本次已排除 626,622 字节残留，不能重复计入后续优化收益 |
| 图片压缩、调整尺寸 | 图片请求字节、冷缓存总下载量、LCP 观察值 | 四张 PNG 是已确认的优化对象；转换后的体积和视觉质量仍需验证 |
| gzip 或 Brotli | 实际请求字节、资源下载时间 | 基线画布 JS 为 641,005 字节，离线 gzip level 9 为 196,362 字节；服务器收益需要实测 |
| 内容哈希文件名与长缓存 | 热缓存 JS 下载量、热访问就绪时间 | 缓存有效且版本未变的时候，可避免重新下载页面 JS；尚未验证部署策略 |
| 共享依赖 | 运行代码总量、跨页面下载量 | 当前没有拆包后的数据，不能给出收益比例 |
| 功能延迟加载 | 首屏 JS 下载量、就绪时间、首次打开功能的等待时间 | 需要同时检查首屏收益和功能首次打开的成本，当前没有对应操作基线 |

体积下降、网络传输减少和页面等待缩短分别记录。三轮样本只提供初步描述，不能证明小幅变化显著；正式发布收益需要增加样本，并保持设备、网络、缓存、场景与采集器一致。

本次尚未覆盖真实公网、生产代理、业务图片与视频、INP、实际操作完成时刻，以及 JS 解析和执行的专项分析。这些指标需要补充对应实验，不从现有数据推算。

### 6. 原始记录与复测

构建体积：[原始 JSON](bundle-baseline-2026-10-03.json)、[采集口径与命令](bundle-size.md)。网页加载：[汇总表](web-baseline-2026-10-03/summary.md)、[环境记录](web-baseline-2026-10-03/environment.json)、[指标定义与复测说明](web-load.md)。逐轮请求和截图保存在网页基线目录中。

完成构建和检查后，再开始正式采样。采样期间不运行构建或全仓测试，保留原始基线，并使用新的目录保存优化后结果。

```sh
node --import tsx scripts/perf/bundle-size.ts capture /tmp/bundle-candidate.json
node --import tsx scripts/perf/bundle-size.ts compare docs/performance/bundle-baseline-2026-10-03.json /tmp/bundle-candidate.json

.venv/bin/python scripts/perf/server.py --port 7815
# 另一个终端
node --import tsx scripts/perf/web-load.ts --repeats 3 --out /tmp/web-candidate
node --import tsx scripts/perf/web-compare.ts docs/performance/web-baseline-2026-10-03 /tmp/web-candidate
```

这里保留的源码与构建来自当时有未提交修改的工作区，环境记录中的 HEAD 不能单独复现它们；复测需要保留对应源码快照，并核对构建哈希。第五节与前面的画布交互实验使用不同的环境、构建和测量协议，不合并计算收益。


## 网页首次加载

网页加载基线覆盖落地页、工作台和 500 节点画布。桌面浏览器的视口为 1440 × 900、DPR 为 1；下载限制为 10 Mbps，上传为 2 Mbps，延迟为 40 ms。每个页面分别采集 3 次冷访问和 3 次热访问，表中是有效轮次的中位数。

| 页面 | 缓存 | 有效轮数 | 下载 KB | JS 下载 KB | FCP ms | LCP 观察值 ms | DOM 就绪代理 ms | CLS 观察值 |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| landing | cold | 3/3 | 1201.3 | 229.6 | 548 | 548 | 538 | 0.0000 |
| landing | warm | 3/3 | 231.1 | 229.6 | 364 | 364 | 345 | 0.0000 |
| workspace | cold | 3/3 | 12080.5 | 277.8 | 412 | 412 | 488 | 0.0334 |
| workspace | warm | 3/3 | 1000.9 | 277.8 | 396 | 456 | 456 | 0.0334 |
| canvas-text-500 | cold | 3/3 | 702.5 | 626.3 | 696 | 864 | 874 | 0.0017 |
| canvas-text-500 | warm | 3/3 | 692.1 | 626.3 | 680 | 844 | 862 | 0.0017 |


工作台冷访问下载约 12 MB，其中四张示例 PNG 合计约 9.20 MiB。三个页面的 JavaScript 响应均未启用压缩，热访问的 JavaScript 下载量与冷访问接近。这些结果来自本地服务；生产反向代理的压缩和缓存策略需要独立验证。目前只有加载基线，还没有对应优化后的收益数据。

## 前端构建体积

独立临时目录中的生产构建共有 166 个文件。重复构建的文件大小、压缩大小和 SHA-256 一致，A/A 对照差值为 0。下面的压缩大小是逐文件独立压缩后求和，并不等于一次页面访问的下载量。

| 范围 | 原始字节 | gzip 字节 | Brotli 字节 |
| --- | ---: | ---: | ---: |
| 全部产物 | 9,154,912 | 2,191,618 | 1,768,045 |
| source map | 7,299,464 | 1,618,803 | 1,288,277 |
| 不含 source map | 1,855,448 | 572,815 | 479,768 |

共 166 个文件。独立重复构建的全部文件大小、压缩大小和 SHA-256 均一致，A/A 差值为 0。此前对现有 `.web-build` 的统计为 9,781,534 字节；其中包含 626,622 字节的旧产物残留，因此使用本报告作为后续体积比较基线。


## 保存和后端接口

早期浏览器自动保存实验中，3 MB 文档的完整 flush 中位数为 230.2 ms，同步前处理为 130.5 ms，localStorage 写入为 15.4 ms。同步前处理包含克隆、序列化、草稿写入和请求启动边界。完整 flush 不包含此前的防抖等待，不能直接当成编辑到持久化确认的时间。

最新后端基线使用独立 Mock 服务和真实 HTTP，每份文档包含 10 万个 ASCII 文本字符，文件约 100202 字节。微基准和串行接口每项采集 20 次，混合请求为 5 轮、每轮 8 个并发请求。使用暖文件缓存，未控制其他宿主机活动。

| 画布数 | 文档列表 P50 | 保存结果 P50 | 空闲健康 P95 | 混合健康 P95 | 生成期间健康最大值 |
|---:|---:|---:|---:|---:|---:|
| 10 | 1.26 | 9.39 | 0.92 | 9.35 | 2.29 |
| 100 | 12.72 | 88.7 | 0.86 | 57.77 | 125.98 |
| 1000 | 131.47 | 887.41 | 0.83 | 570.82 | 900.16 |


文档数量增加的时候，列表读取和保存结果的等待近似线性增长。混合负载还拖慢了健康接口：1000 份文档的时候，混合健康检查 P95 为 570.82 ms，生成期间最大值为 900.16 ms。结果与全量历史引用扫描、同步持久化占用事件循环的代码路径一致。这是当前基线，没有后端优化后的对照。

早期独立 PostgreSQL 和 Redis 实验还记录了生产路由的查询开销。100 个父任务、每组 4 个子任务的时候，任务列表中位数为 239.7 ms，每请求包含鉴权在内执行 104 次 SELECT；20 个并发请求的 P95 为 907.2 ms。500 份、每份 1000 对象的文档列表中位数为 1062.0 ms。无变化 SSE 刷新仍执行 104 次 SELECT，约 1.18–1.43 秒的记录包含 1 秒刷新等待。这些是本机合成会话下的实验，HTTPX ASGI 调用不包含远程 TCP 时间，不能解释成线上延迟或本轮重新测量的结果。

## 函数开销如何定位

早期 DevTools 录制在约 10.19 秒鼠标移动窗口中观察到 65 个长任务，累计约 10.10 秒。面板选区包含操作前后等待，Scripting 分类合计 10.38 秒，因此不能把它归给某一个函数。原生 trace 没有获得可验证导出文件，保留的是面板记录和同期采样。

后续 V3 独立诊断中，Konva.Layer.drawScene 调用 60 次、含内部调用累计 1164.20 ms，单次最大 51.60 ms；drawHit 调用 60 次、累计 299.70 ms。诊断插桩有额外开销，这些数字用于定位，不用于计算体验收益。

后端 cProfile 对真实存储入口各采集 3 次同步调用，记录如下。含子调用耗时包括同步 I/O 等待和 profiler 开销，不是纯 CPU 时间。

| 函数及负载 | 调用次数 | 含子调用累计耗时 | 每次平均耗时 |
|---|---:|---:|---:|
| Documents.list，500 份 × 1000 对象 | 3 | 1.6352 s | 545.081 ms |
| referenced_history_files，100 份 × 3 MB | 3 | 7.7412 s | 2580.392 ms |
| re.Pattern.findall，同一引用扫描窗口 | 300 | 7.4260 s | 24.753 ms |
| Documents.save，3 MB | 3 | 0.1110 s | 37.014 ms |

这些调用链支持两个具体排查方向：前端减少每次操作处理的对象数量，后端减少列表和历史引用的全文档扫描。单次平均值不等于 P95，父子函数的累计耗时也不能相加。

## 离屏缓存与 Worker 的独立结果

离屏 Canvas 指在独立画布上预先绘制内容，再复用结果；Worker 把部分工作交给浏览器的后台线程。两者能减少重复工作或主线程竞争，收益仍然需要通过相同工作量对照确认。

V6 的独立对照中，5000 节点从 Konva 切换到 Worker，60 次平移的完成观测值为 3170.60 → 2191.10 ms，首次就绪为 540.62 → 615.64 ms，全选为 125.40 → 130.20 ms。将已经使用离屏缓存的方案再切换到 Worker，同一场景平移为 2158.20 → 2191.10 ms，没有测到额外提速。这些数据说明，不能把缓存带来的全部改善算成后台线程收益。

内存测试保留了 JS 堆采样、缓存位图估算和资源释放记录。旧的计数器重置样本已排除。高缩放下 52 MiB 的缓存保留问题、Worker 内存、浏览器进程内存和 GPU 显存仍需单独验收，当前没有稳定的总内存下降结论。

## 从前端到后端的采集验收

采集链路覆盖浏览器操作和请求、服务端 HTTP、排队、Mock 生成、素材持久化、任务完成，以及 SSE 到达记录。最终验收共收到并保存 528 条记录，丢弃、写入错误和拒绝均为 0，3 个 Mock 任务的前后端关联检查通过。

| 浏览器 SDK 指标 | 最新快照 |
|---|---:|
| TTFB | 8 ms |
| FCP | 144 ms |
| LCP | 336 ms |
| CLS | 0.0016625992972500855 unitless |
| INP | 136 ms |

这些是短时验收会话的最新快照，没有与旧实现进行相同场景对照，不能计算优化收益。浏览器和后端的时钟分别处理；阶段耗时可能重叠，不能直接相加推算端到端等待。Mock 生成的耗时也不代表真实模型速度。

## 数据还没有覆盖的场景

当前证据没有覆盖真实多人编辑、低端设备、真实公网尾延迟、长时间操作的内存增长和 GPU 呈现时间。早期图片与视频实验保留在附录，但没有同协议下最新优化版本的复测：50 张 2048 px 原图的文件体积为 6.61 MiB，1024 px 缩略图为 3.91 MiB；对应 RGBA 理论值为 800 MiB 和 200 MiB，不能解释成实际内存下降。视频实验的本机帧推进约为 58–60 次/s，也不能推广到 4K、低端设备或长时播放。

当前多节点复测使用简单单行文字和自动适配缩放。混合媒体、复杂字体、旋转对象、高缩放全可见，以及缓存回退需要继续按场景验收。文字编辑和大文件加载已经出现没有明显改善的结果，后续对照应保留这些结果，避免只汇总平移的提升。

## 报告与原始证据索引

[完整证据附录](/Users/weilengdezaoshang/Documents/项目/aiVideo/docs/performance/performance-evidence.md)聚合历次报告正文、HTML 表格、自动生成对照表和采集说明。正文中的最新结果与历史记录分开阅读；重复报告只作为同一实验的不同入口，不增加样本数量。带有 rejected 的原始目录、失败动作和已被新协议替代的结果保留供审计，不参与最新收益计算。

| 编号 | 来源 | 类型 |
|---:|---|---|
| 1 | [docs/performance/web-baseline-2026-10-03/summary.md](/Users/weilengdezaoshang/Documents/项目/aiVideo/docs/performance/web-baseline-2026-10-03/summary.md) | 专项实测 |
| 2 | [docs/performance/web-load.md](/Users/weilengdezaoshang/Documents/项目/aiVideo/docs/performance/web-load.md) | 专项实测 |
| 3 | [docs/performance/bundle-size.md](/Users/weilengdezaoshang/Documents/项目/aiVideo/docs/performance/bundle-size.md) | 专项实测 |
| 4 | [docs/performance/backend-baseline.md](/Users/weilengdezaoshang/Documents/项目/aiVideo/docs/performance/backend-baseline.md) | 专项实测 |
| 5 | [docs/performance/canvas-optimization-2026-10-03.md](/Users/weilengdezaoshang/Documents/项目/aiVideo/docs/performance/canvas-optimization-2026-10-03.md) | 专项实测 |
| 6 | [incremental-document/report.html](/Users/weilengdezaoshang/.codex/visualizations/2026/09/30/01a0f0fa-9cf1-76d3-87d7-c3b2d63ea9fe/aivideo-performance/incremental-document/report.html) | 专项实测 |
| 7 | [native-v6/implementation-report.html](/Users/weilengdezaoshang/.codex/visualizations/2026/09/30/01a0f0fa-9cf1-76d3-87d7-c3b2d63ea9fe/aivideo-performance/native-v6/implementation-report.html) | 历史诊断与实现记录 |
| 8 | [report.html](/Users/weilengdezaoshang/.codex/visualizations/2026/09/30/01a0f0fa-9cf1-76d3-87d7-c3b2d63ea9fe/aivideo-performance/report.html) | 专项实测 |
| 9 | [spatial-culling/report.html](/Users/weilengdezaoshang/.codex/visualizations/2026/09/30/01a0f0fa-9cf1-76d3-87d7-c3b2d63ea9fe/aivideo-performance/spatial-culling/report.html) | 专项实测 |
| 10 | [telemetry-collection/report.html](/Users/weilengdezaoshang/.codex/visualizations/2026/09/30/01a0f0fa-9cf1-76d3-87d7-c3b2d63ea9fe/aivideo-performance/telemetry-collection/report.html) | 采集验收 |
| 11 | [tiled-cache/report.html](/Users/weilengdezaoshang/.codex/visualizations/2026/09/30/01a0f0fa-9cf1-76d3-87d7-c3b2d63ea9fe/aivideo-performance/tiled-cache/report.html) | 专项实测 |
| 12 | [incremental-document/report.md](/Users/weilengdezaoshang/.codex/visualizations/2026/09/30/01a0f0fa-9cf1-76d3-87d7-c3b2d63ea9fe/aivideo-performance/incremental-document/report.md) | 专项实测 |
| 13 | [native-v3/REPORT.md](/Users/weilengdezaoshang/.codex/visualizations/2026/09/30/01a0f0fa-9cf1-76d3-87d7-c3b2d63ea9fe/aivideo-performance/native-v3/REPORT.md) | 历史诊断与实现记录 |
| 14 | [native-v3/comparison-selfcheck.md](/Users/weilengdezaoshang/.codex/visualizations/2026/09/30/01a0f0fa-9cf1-76d3-87d7-c3b2d63ea9fe/aivideo-performance/native-v3/comparison-selfcheck.md) | 历史诊断与实现记录 |
| 15 | [native-v4/comparison-selfcheck.md](/Users/weilengdezaoshang/.codex/visualizations/2026/09/30/01a0f0fa-9cf1-76d3-87d7-c3b2d63ea9fe/aivideo-performance/native-v4/comparison-selfcheck.md) | 历史诊断与实现记录 |
| 16 | [native-v5/alternating-comparison.md](/Users/weilengdezaoshang/.codex/visualizations/2026/09/30/01a0f0fa-9cf1-76d3-87d7-c3b2d63ea9fe/aivideo-performance/native-v5/alternating-comparison.md) | 历史诊断与实现记录 |
| 17 | [native-v5/comparison.md](/Users/weilengdezaoshang/.codex/visualizations/2026/09/30/01a0f0fa-9cf1-76d3-87d7-c3b2d63ea9fe/aivideo-performance/native-v5/comparison.md) | 历史诊断与实现记录 |
| 18 | [native-v5/technical-plan.md](/Users/weilengdezaoshang/.codex/visualizations/2026/09/30/01a0f0fa-9cf1-76d3-87d7-c3b2d63ea9fe/aivideo-performance/native-v5/technical-plan.md) | 历史诊断与实现记录 |
| 19 | [native-v5/zero-baseline-selfcheck.md](/Users/weilengdezaoshang/.codex/visualizations/2026/09/30/01a0f0fa-9cf1-76d3-87d7-c3b2d63ea9fe/aivideo-performance/native-v5/zero-baseline-selfcheck.md) | 历史诊断与实现记录 |
| 20 | [native-v6/clean/cache/comparison.md](/Users/weilengdezaoshang/.codex/visualizations/2026/09/30/01a0f0fa-9cf1-76d3-87d7-c3b2d63ea9fe/aivideo-performance/native-v6/clean/cache/comparison.md) | 历史诊断与实现记录 |
| 21 | [native-v6/clean/cache-versus-worker.md](/Users/weilengdezaoshang/.codex/visualizations/2026/09/30/01a0f0fa-9cf1-76d3-87d7-c3b2d63ea9fe/aivideo-performance/native-v6/clean/cache-versus-worker.md) | 历史诊断与实现记录 |
| 22 | [native-v6/clean/worker/comparison.md](/Users/weilengdezaoshang/.codex/visualizations/2026/09/30/01a0f0fa-9cf1-76d3-87d7-c3b2d63ea9fe/aivideo-performance/native-v6/clean/worker/comparison.md) | 历史诊断与实现记录 |
| 23 | [spatial-culling/clean/comparison.md](/Users/weilengdezaoshang/.codex/visualizations/2026/09/30/01a0f0fa-9cf1-76d3-87d7-c3b2d63ea9fe/aivideo-performance/spatial-culling/clean/comparison.md) | 专项实测 |
| 24 | [telemetry-collection/collection-protocol.md](/Users/weilengdezaoshang/.codex/visualizations/2026/09/30/01a0f0fa-9cf1-76d3-87d7-c3b2d63ea9fe/aivideo-performance/telemetry-collection/collection-protocol.md) | 采集验收 |
| 25 | [tiled-cache/clean/pan-comparison.md](/Users/weilengdezaoshang/.codex/visualizations/2026/09/30/01a0f0fa-9cf1-76d3-87d7-c3b2d63ea9fe/aivideo-performance/tiled-cache/clean/pan-comparison.md) | 专项实测 |
