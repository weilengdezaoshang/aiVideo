# 后端诊断基线 · 2026-10-04

## 范围和环境

本次采集四类基线：CPU 火焰图、函数级 CPU/墙钟耗时、真实 PostgreSQL 慢查询与执行计划、生产请求到队列/Worker/落盘/Redis/SSE 的时序。

所有数据、账号、凭据均为合成测试数据。未连接业务数据库、读取 config.json 或调用付费 Provider。PostgreSQL 16、Redis 7、RabbitMQ 3.13 为本次新建独立容器，采集结束已删除；两个 Celery solo Worker 及四个 daemon 进程已退出。没有修改后端业务代码，插桩只在基准进程里生效。

系统为本机 macOS arm64，Python 3.11；Docker 29.5.2，容器未额外限制 CPU/内存。具体版本、Git revision、工作树状态、脚本和数据 SHA-256 见 environment.json。工作树已有未提交变更，本次结果对应工作树而非纯 HEAD。

这是一组本地可复核诊断数据，不是生产容量或 SLA 验收。请求和 SSE 边界使用真实生产 ASGI 应用，基础设施为真实本地 Docker；不包含公网往返、OIDC 登录、浏览器渲染、GPU/rembg 原生栈、真实模型、读副本、锁争用与连接池等待分布。

## CPU 火焰图和函数级耗时

暖文件缓存，100/1000 份画布，每份约 100KB。每轮调用三次 Documents.list 和三次 History.save。cProfile 采用当前线程 CPU 时钟；另一次独立 sys.setprofile 采集 Python/C 边界调用栈，导出按线程 CPU 时间加权的火焰图。它是确定性插桩，不是 py-spy 原生栈采样。Profiler 有额外开销，绝对耗时应与前一份未插桩基线分别看待。

| 画布数 | cProfile 线程 CPU | 同轮墙钟 | 正则 findall 自耗 CPU | 占该轮 CPU |
|---:|---:|---:|---:|---:|
| 100 | 323.4ms | 324.2ms | 256.4ms | 79.3% |
| 1000 | 3205.1ms | 3208.4ms | 2569.2ms | 80.2% |

1,000 份画布场景中，正则查找是 CPU 主热点，其次为 JSON 解码。它对应 storage.referenced_history_files 的全文档引用扫描。这个占比只代表上述存储工作负载，不能推广为整个后端所有业务的 CPU 占比。

- cpu-1000.flame.svg：可直接查看，横向宽度代表 CPU 归属，鼠标悬停显示函数和累计 CPU。
- cpu-1000.speedscope.json：可导入兼容 Speedscope 的查看器，无需上传到外部服务。
- functions-1000.json：全部函数的调用次数、自耗 CPU、累计 CPU。
- functions-1000.pstats：标准 cProfile 原始数据。
- chain-100/1000.json：带 job/trace ID 的函数墙钟 spans；provider.generate 耗时包含异步等待以及事件循环受其他任务阻塞的时间，不能视为纯模型计算。

## 慢查询与查询放大

在空白 PostgreSQL 上运行仓库 migrations 0001–0008，合成 1 万和 10 万后台任务；另外有 100 个 batch 父任务和 400 个子任务、1000 份每份文本 100KB 的画布、10000 条 published Outbox。每个生产读接口运行 11 次，第一轮暖缓存，剩余 10 次记入分位数。读取鉴权通过测试会话，SQL 数含会话和成员查询。

| 后台任务数 | 接口 | 请求墙钟 P50 | 请求墙钟 P95 | 每请求 SQL 数 |
|---:|---|---:|---:|---:|
| 10000 | /api/documents | 261.22ms | 494.94ms | 3 |
| 10000 | /api/jobs | 121.1ms | 241.7ms | 104 |
| 100000 | /api/documents | 270.66ms | 466.47ms | 3 |
| 100000 | /api/jobs | 95.93ms | 173.69ms | 104 |

任务列表存在 batch N+1：100 个父任务分别查询子任务，加基础列表与鉴权，共 104 条 SQL。单条子查询本身很快，但总查询次数和数据库往返放大了请求成本。

文档列表只返回摘要，却读取 500 份完整画布，包含约 50MB 文本。10 万任务场景 pg_stat_statements 记录该查询平均约 202ms。其索引查找的 EXPLAIN ANALYZE 只有约 0.13ms，两者并不矛盾：EXPLAIN 不向客户端返回整个结果，未覆盖相同的 TOAST 数据物化、JSON 输出和传输成本。不能因为执行计划走索引就认定接口不慢。

| 后台任务数 | 模型名额计数 EXPLAIN | 全局任务状态聚合 EXPLAIN |
|---:|---:|---:|
| 10000 | 1.33ms | 2.30ms |
| 100000 | 10.89ms | 26.68ms |

10 万任务时，slot_scope 计数执行计划为 Seq Scan：返回 100 行占用记录，同时滤掉 100400 行，缓存命中 2140 个块。本轮没有物理读，因此该结果不代表冷磁盘场景。状态聚合也是全表扫描。

数据库开启 pg_stat_statements、track_io_timing 和 20ms 慢查询日志。sql-10000/100000.json 保留 SQLAlchemy cursor 逐次耗时、SQL 文本（不含绑定参数）、服务端调用次数/累计/平均耗时、返回行数、缓存块与 I/O、EXPLAIN ANALYZE BUFFERS、表大小。postgres-slow-reads.json 保留慢读日志首行；完整多行 SQL 在 sql-*.json 中。Cursor 耗时不包含全部 ORM 实体创建和 HTTP JSON 序列化；请求墙钟包含这些步骤。

## 全链路时序

### 默认文件模式

并发提交 8 个 Mock 请求，并发槽位为 2；分别在 100 和 1000 份画布下采集。chain-*.json 包含 request/job/trace 关联以及队列等待、provider.generate、History.save、引用扫描和 JSON/fsync 子 span。chain-*.trace.json 可由兼容 Chrome Trace 的时间线查看器打开。trace-steps 和终态 traces 的原始 JSONL 一并保留。

### 生产模式

真实 PostgreSQL + RabbitMQ + Redis，两个独立 Celery solo Worker，运行原有 dispatcher/scheduler/bridge/notify daemon。并发四个 64×64 Mock 任务，保留前述 10 万条后台任务。ASGI SSE 在提交前完成订阅并读取快照，终点为 API 发出 completed job 事件；另一个独立 Redis 订阅者记录到达时间。尚未测浏览器网络接收或渲染。

| 阶段 | P50 | 最大值 |
|---|---:|---:|
| HTTP 受理 | 92.41ms | 120.11ms |
| 受理结束到首个 Worker 阶段 | 2721.25ms | 2724.46ms |
| 每任务 Provider 阶段总计 | 262.91ms | 282.18ms |
| 产物写盘 | 1.26ms | 2.44ms |
| 请求开始到数据库完成 | 3441.53ms | 3628.37ms |
| 数据库完成到 SSE 发出完成事件 | 636.65ms | 804.57ms |
| 数据库完成到 Redis 订阅者收到终态版本通知 | 8474.69ms | 16540.90ms |

本轮 Provider 本身约 263ms，首阶段派发前却等待约 2.7 秒，指向周期调度/投递链路；不能将全部等待归因于 Celery Worker 计算。

Redis 终态版本通知可明显晚于 SSE 完成事件。SSE 收到较早版本的通知或周期对账时会查询数据库最新状态，因此这些阶段不是必须串行完成的路径，不能把“完成→Redis终态通知”再加到“完成→SSE”上。本轮通知积压依然证明 notify 的消费能力需要独立关注，但不代表前端一定延迟相同时长。

production-chain.json 保留每个任务所有时间点、阶段统计、Redis 和 SSE 接收事件、跨进程函数 spans；production-chain.trace.json 为时间线。各 worker/daemon 的原始日志和 spans.jsonl 保留。采用 job_id 关联，所有进程在同一宿主机时钟上；没有把这套测试插桩当成已部署的 OpenTelemetry。

只有四个生产任务，因此这里只报告 P50 和最大值，不把四样本的 P95 当可靠生产尾延迟。

## 复跑

先确保 Docker 可用，给每次测量选择一个新的空输出目录：

```sh
.venv/bin/python scripts/perf/collect_backend_diagnostics.py \
  --output docs/performance/backend-diagnostics-next
```

入口依次运行 CPU/文件链路、SQL、生产链路三个采集器；使用随机容器名与回环端口、新数据库，不挂载业务数据。所有子进程由链路采集器清理，临时容器由入口 finally 清理。当前主目录中的数据由同样的三个采集器分别运行所得；统一入口的编排代码经过静态检查，尚未额外整体复跑，以免无必要重复测量。

单独 CPU/文件链路不需要 Docker：

```sh
.venv/bin/python scripts/perf/backend_diagnostics.py --output /tmp/aivideo-cpu-new
```

SQL 采集器要求专用空白 baseline 数据库，拒绝已有 public 表的数据库。生产链路采集器要求该 SQL 数据集和独立 broker/Redis；不要指向业务实例。具体参数见各脚本 --help。

## 验证与剩余指标

所有采集场景成功完成，新增脚本 Ruff check/format 通过。npm run verify 的 typecheck/build:web 通过，ESLint 被工作树已有 scripts/pre-commit-test.mjs:110 curly 错误中断，未进入全仓测试。没有修改该已有变更。

本次没有采集硬件级/原生栈采样、进程 RSS/CPU 连续时间序列、数据库锁等待/连接池排队分布、浏览器端端到端耗时、真实 Provider 阶段延迟。上述指标不能由当前图表推算。本次的确定结论是存储工作负载的 CPU 热点、读路径 SQL 放大/大字段返回成本以及真实调度与通知的等待。
