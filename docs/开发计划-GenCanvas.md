# GenCanvas 开发计划:模块拆分与技术调研结论

> 依据:《PRD-生成式创作画布》v0.1 + 低保真线框图 S1–S17 · 2026-09-06
> 本文档是开发顺序的唯一锚点;每个 TODO = 一个提交,提交前必跑 `npm run verify` 并做自评审(对照 PRD 验收要点 + 重读 diff)。

---

## 一、技术调研结论(开工前置项核验)

### 1. 画布引擎:Konva 10(维持 PRD 选型)
- npm latest = **10.3.3**;UMD 构建(`konva.min.js`)自托管 vendor 引入,零构建成立,不依赖运行时 CDN(自部署可离线)。
- 决策:下载到 `web/vendor/konva/konva.min.js`,license 文件随附。

### 2. UI 组件库:Shoelace 2(满足"组件库 + 成熟开源"且不破坏零构建)
- PRD §8.1 明确零构建、不上 Vite;同时本计划要求使用成熟组件库。**Shoelace(web components,MIT,~20k star)** 是两者交集:原生 ESM、无需打包器、无 React/Vue 绑定。
- vendor 其 `dist/cdn` 目录(含主题 CSS + autoloader),图标系统用内置 lucide 资源。
- 决策:弹窗/下拉/按钮/输入/tooltip 等一律用 Shoelace;画布内交互仍归 Konva。若后续前端超 ~3k 行按 PRD 升级 esbuild,Shoelace 可无缝带入。

### 3. SVG 矢量化(P1,F9):npm 包名有坑
- ⚠️ **npm 上的 `vtracer@1.0.8` 不是 visioncortex 的 vtracer**,是无关的日志库——PRD §8.1"官方 npm WASM 包"表述有误。
- 正确选择:**`vtracer-webapp@0.4.0`**(visioncortex/vtracer 官方仓库的 wasm-pack 产物,MIT/Apache-2.0,tgz 81K,含 `vtracer_webapp_bg.wasm` + JS glue + d.ts)。
- API 核实(读 Rust 源码):`ColorImageConverter.new_with_string(jsonParams)` 从 DOM `canvas_id` 读像素、结果 SVG 写入 `svg_id` 元素;`init()` + `tick()`(返回 false 结束)+ `progress()`。参数:`mode / hierarchical / filter_speckle / color_precision / layer_difference / corner_threshold / length_threshold / max_iterations / splice_threshold / path_precision`。
- 结论:**浏览器端可行**,需 vendor wasm + 薄封装(隐藏 canvas/svg 元素 + rAF 分片 tick 防卡 UI)。备选 `vectortracer@0.1.2` API 更友好(ImageData 进 / getResult 出)但只暴露二值转换器,无彩色量化 → 不采用。超大图服务端 CLI 降级维持 PRD 方案,P1 阶段先不做。
- ⚠️ **实施修正(T15 实测,最终结论:弃用 vtracer)**:落地时发现两处硬伤——① 官方入口 `vtracer_webapp.js` 是 wasm-bindgen **bundler 目标**(`import * from './xx.wasm'` 仅打包器可解析),需手写 `WebAssembly.instantiate` + `__wbg_set_wasm` 回注 shim(已验证可行);② **致命**:visioncortex 0.9 的 wasm 聚类对大量"参数×图片"组合**确定性 panic**(`unreachable`;实测同一图片颜色层数 6/7 可过、2/3/4/5/8 必炸,与加载方式、中止时序无关,参数空间无法绕开,前端不可修)。
- **改用 `imagetracerjs@1.2.6`**(Unlicense 公有领域,纯 JS 47KB,经典脚本挂 `window.ImageTracer`,零构建、无 wasm、无 panic):`imagedataToSVG(imagedata, { numberofcolors, ltres, qtres, pathomit, viewbox })`,同步执行,源图下采样 ≤1024px 后单次通常 <300ms。S12 滑杆映射:颜色层数→numberofcolors、细节→ltres/qtres、简化→pathomit。浏览器 E2E 全流程验证通过(转换/滑杆联动/串行队列健壮性)。

### 4. job 历史重启存续(PRD §9 高风险前置项):**不存续**
- `job-manager.ts` 的 `jobs` 为内存 Map,服务重启即清空;`pruneFinishedJobs` 上限 200。
- 影响:刷新页面后占位框查询 `GET /api/jobs/:id` 会 404,无法区分"服务重启丢任务"与"jobId 非法"。
- 对策(T5,后端 P0):任务状态落盘 `data/jobs.json`(状态变化时防抖写入);启动时读回,把重启时仍在 queued/running 的任务标记为 failed("服务已重启,任务中断")。这样对账语义确定:404 = jobId 非法,failed = 可重试,completed = 直接落位。PRD 的"查历史兜底"保留为第二道防线。

### 5. 缩略图管线:引入 `sharp`(运行时依赖)
- F1 验收(500 对象 + 50 张 2048px 图 ≥55fps)必须靠小纹理;2048 原图直接进 Konva 会导致显存/解码压力。
- docker 镜像是 `node:24-alpine`(musl),sharp ≥0.33 提供 linuxmusl arm64/x64 预编译,可用。
- 决策:`store.save` 管线同步产出 256 / 1024 两级 WebP 缩略图,`data/assets/<id>/` 三件套存储;失败降级为只存原图(不阻塞生成)。

### 6. 其余核实
- Konva 视频对象:Konva.Image + HTMLVideoElement 可行,点击播放/暂停由事件驱动;不引入额外播放器库。
- SSE:浏览器 EventSource 原生自动重连,服务端加 `retry: 3000` 提示即可满足"5s 内重连";重连后对账靠 `snapshot` 事件 + `GET /api/jobs?all=1`。
- 多标签页互踩:BroadcastChannel 探测,检测到多开 toast 警告(v1 约束,PRD §9)。
- express 12mb JSON 限制:蒙版/参考图改走二进制上传端点(`express.raw`),文档 JSON 本身很小,不受影响。

---

## 二、模块拆分

| # | 模块 | 职责 | 主要文件落点 |
| --- | --- | --- | --- |
| M1 | 资产与文档服务(server) | `data/assets` 存储 + 缩略图、二进制上传、文档 CRUD、job 落盘、clientRef | `src/assets.ts` `src/documents.ts` `src/services/job-manager.ts` `src/server.ts` |
| M2 | 画布核心(web) | Konva 舞台、相机(平移/缩放手势)、对象渲染、选择/框选/移动/删除、视口裁剪 | `web/canvas/`(engine 系列) |
| M3 | 状态层(web) | DocStore(可撤销)/ TransientStore、Command undo 栈、纯函数 reducer、SSE 客户端、对账、自动保存 | `web/canvas/`(state 系列,纯逻辑可 node:test) |
| M4 | 生成流程(web) | 提示词面板(t2i/i2i)、占位框状态机、螺旋落位、谱系元数据、右键菜单、失败/重试 | `web/canvas/`(flows 系列) |
| M5 | 局部重绘(web) | 双击进入重绘模式、笔刷/擦除/清空、蒙版上传、结果新对象 | `web/canvas/`(inpaint) |
| M6 | 图生视频(web) | i2v 参数面板、视频对象渲染与播放 | M2 + M4 扩展 |
| M7 | 配置与外围(web+server) | 首启引导 + BYOK 设置、provider 热切换、断线横幅、多开检测;P1:SVG 导出、素材库入口 | `web/canvas/`(settings)`src/server.ts` |

## 三、TODO 清单

> **T21(硅基流动深化)**:云端 i2i(FLUX.1-Kontext-dev)与蒙版局部重绘(FLUX.1-Fill-dev,
> 白 = 重绘,与画布蒙版语义一致);提示词中译英层(cloudTextModel 配置,Qwen 系,
> 确定性翻译不改写,失败降级原文),translated 字段进追踪(保留率可按 开/关 分组评测)。

> **T20(可评测可追踪)**:src/traces.ts 生成链路追踪(JSONL,含 promptHash/seed/排队与生成
> 分离计时/错误分类/费用估算,预留 origin=user|agent 给未来 agent 编排层)、删除反馈
> (POST /api/feedback)沉淀隐式保留率、GET /api/metrics 指标聚合、scripts/eval-prompts.mjs
> golden-prompts 评测集脚本(跨模型对比报告)。

> **T19(云端生图接入)**:CloudImagesProvider(zhipu/siliconflow/openai 三厂商适配,提示词直通、
> status 零计费、i2i/蒙版显式降级),config 扩展 cloudVendor/cloudBaseUrl/cloudModel,设置界面
> 云端分支 + 热切换后模型列表刷新。见 tests/cloud-provider.test.ts。(执行序,✅ = 已提交)

- [x] **T0** 计划文档(本文)+ 调研结论落盘
- [x] **T1** `build(project)`: vendor Konva + Shoelace 进 `web/vendor/`(含 license),eslint/prettier 忽略;`web/canvas/index.html` 静态骨架(顶栏/空态/缩放控件/面板挂点),S3 线框对齐
- [x] **T2** `feat(store)`: 资产存储 + sharp 缩略图管线 + `POST /api/assets`(二进制)+ `GET /assets/:id/:variant`;测试
- [x] **T3** `feat(server)`: 文档 CRUD(`data/documents/`,防抖落盘语义,sendBeacon 可写)+ job 落盘恢复(重启 → failed"服务已重启")+ `clientRef` 透传;测试
- [x] **T4** `feat(web)`: 画布引擎——Konva 舞台、平移(空格/中键/双指)、光标中心缩放、网点背景、缩放控件;相机归 TransientStore
- [x] **T5** `feat(web)`: 状态层——DocStore/TransientStore、Command undo/redo(合并规则)、自动保存(2s 防抖 + beforeunload beacon)、加载恢复;纯逻辑 node:test
- [x] **T6** `feat(web)`: 对象渲染与交互——image/video/placeholder/error 四种 kind、单击/框选( bounds 求交)/拖动/Delete、视口裁剪 + 256 缩略图优先;500 对象压测脚本
- [x] **T7** `feat(web)`: SSE 客户端 + 纯函数 reducer(未知 jobId 丢弃)+ 加载/重连对账;六场景 node:test(提交/进度/落位/取消/失败重试/刷新恢复)
- [x] **T8** `feat(web)`: 文生图闭环——Tab 面板、生成 → 占位框(可拖动/悬浮取消)→ 进度 → 落位;顶栏进行中任务数
- [x] **T9** `feat(web)`: 右键菜单 + 以图生图(参考图挂载)+ 螺旋探测落位(纯函数测试)+ 谱系元数据 + 下载/删除
- [x] **T10** `feat(web)`: 失败卡/任务丢失卡 + 重试(谱系参数原样重发)
- [x] **T11** `feat(web)`: 局部重绘模式(蒙版笔刷/擦除/清空、区域提示词、蒙版二进制上传、结果新对象)
- [x] **T12** `feat(web)`: 图生视频面板 + 视频对象(点击播放)——M0 三原型到此全部可验收
- [x] **T13** `feat(config)`: BYOK 设置界面(S2/S2a/S2b)+ provider 热切换端点 + 首启引导(S1)→ **服务端(c50d07a:src/config.ts + /api/config*/JobManager.setProvider)与设置 UI(42c1696:settings.js + S1 引导)已完成;消费端(模型列表随 provider 刷新)归 T8/T9 接线**
- [x] **T14** `feat(web)`: 断线横幅 + 自动重连对账联动(S15/S16)+ 多开检测
- [x] **T15** `feat(web)`: P1——SVG 导出面板 → **完成(9e7d1f2):引擎最终采用 imagetracerjs(vtracer 因上游 wasm 聚类 panic 弃用,见 §3);S12 面板(原图/实时预览/三滑杆/导出下载)+ 右键菜单接线,浏览器 E2E 全链路验证通过**
- [x] **T16** `feat(web)`: P1——素材库入口与拖入画布(S17)
- [x] **T17** `feat(web)`: P1——变体批生成 ×4(4 独立任务)
- [ ] **T18** P2——提示词收藏复用 / 草稿模式 / 种子锁定(按 PRD §5.5)

> 提交模块名对照 AGENTS.md:`store`(持久化)、`server`(API)、`web`(前端)、`config`(配置)、`project`(仓库级)。

## 四、开发循环约定(每 TODO)

1. 实现(对照线框图屏号 + PRD 验收要点);
2. `npm run verify` 全绿;
3. 自评审:重读完整 diff,检查 PRD §8.3(undo 语义)、§8.2(未知 jobId 丢弃)、错误出口 `{ error }` 结构、中文文案;
4. 修复 → 再 verify → `git commit`(一个 TODO 一个提交);
5. 更新本文档勾选状态后进入下一 TODO。
