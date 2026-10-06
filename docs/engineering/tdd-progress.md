# TDD 进度记录

> 每个行为一轮 RED → GREEN → REFACTOR 记录。RED 记录命令与失败原因,GREEN 记录最小实现,
> REFACTOR 记录结构整理与验证命令。禁止先实现后补测试。

## 基线检查(2026-09-12,以当时工作区为基线)

| 检查 | 命令 | 结果 |
| --- | --- | --- |
| TypeScript | `npm run typecheck` | ✅ 通过 |
| ESLint | `npm run lint` | ✅ 通过 |
| 前端测试 | `npm test`(node --test + tsx) | ✅ 113 通过 / 0 失败 |
| 后端测试 | `.venv/bin/python -m pytest -q` | ⚠️ 149 通过 / 3 失败(见下) |
| Ruff | `ruff check apps/api/backend tests/backend ...` | ❌ 6 处错误(F841/F401/F811 预存) |
| 后端 verify | `npm run verify:backend` | ❌ 因 ruff 失败 |

基线失败分类:

- `test_http.py::test_live_http_and_sse` — **确定性失败,迁移回归**。测试用
  `subprocess.Popen([sys.executable, "-c", ...])` 直接启动真实 uvicorn,子进程环境未设置
  PYTHONPATH(pytest 的 `pythonpath` ini 只作用于测试进程本身),`from backend.app import
  create_app` 报 `ModuleNotFoundError`。对比生产启动器 scripts/backend.py:15 显式设置了
  PYTHONPATH。修复属于测试基建,见第 0 轮。
- `test_replay_live.py::test_replay02_prompt_change_fails_at_boundary_with_diff`、
  `test_export_backup_p5.py::test_restore01_backup_restore_replay` — 首轮全量运行双失败
  (期望 `回放完整性校验失败`,实际 `未消费交互 2 条`);随后复跑全部通过。事后经 mtime 核实,
  `evaluation/{scheduler,runner,gates}.py` 与这两个测试文件在本会话进行期间被并行修改
  (同时新增了 test_scheduler_budget_gate.py),判定为并行编辑造成的中间状态,而非稳定 flaky;
  全量后端测试最终 175/175 通过。结论:后续在该区域工作前需重新确认工作区状态。

## TDD 循环记录

### 第 0 轮:恢复真实 TCP HTTP+SSE 回归测试(test 基建修复)

- **行为**:测试必须能在 monorepo 布局下启动真实 uvicorn 子进程并完成 REST+SSE 冒烟。
- **RED**:命令 `.venv/bin/python -m pytest tests/backend/test_http.py::test_live_http_and_sse -q`
  → 失败:`ModuleNotFoundError: No module named 'backend'`(子进程缺 PYTHONPATH,
  assert process.poll() is None 触发)。失败来自迁移引入的测试基建缺陷,非环境损坏。
- **GREEN**:tests/backend/test_http.py 构造子进程 env 时按 scripts/backend.py:15 的方式
  追加 `PYTHONPATH=<root>/apps/api<os.pathsep><root>`,与生产启动路径一致。
- **REFACTOR**:无(单行修复,无可整理重复)。
- **验证**:`.venv/bin/python -m pytest tests/backend/test_http.py -q` 通过;全量 pytest 通过。

### 第 1 轮:历史裁剪不得删除画布仍引用的生成产物

- **行为**:已知问题 #1(storage.py `History.save` 裁剪与 `History.remove` 删除文件前
  不检查画布引用)。回归要求:历史裁剪或删除后,画布引用的产物仍然可用。
- **背景事实**:
  - 生成产物落盘 `data/images/img_{24hex}.{ext}`,历史记录 `url=/images/{file}`;
  - 画布对象直接存 `src=/images/...`(generation-reducer.ts imageRecordToAsset,assetId 为 null);
  - 裁剪上限 500 条未收藏记录,`History.save`(storage.py:256-264)对被裁掉记录直接 unlink;
  - `DELETE /api/images/{id}` → `History.remove`(storage.py:272-278)同样直接 unlink。
- **RED**:新增 `tests/backend/test_history_references.py`:
  - `test_prune_keeps_files_still_referenced_by_canvas`(裁剪后画布引用文件仍存在)→ 失败:
    文件被删除;
  - `test_delete_history_keeps_file_referenced_by_canvas`(删除单条历史后画布引用文件仍存在)
    → 失败:文件被删除;
  - 两条守卫测试(无引用文件仍会被清理)在 RED 阶段即为绿色,防止修复时矫枉过正。
- **GREEN**(最小实现):storage.py 新增 `referenced_history_files(root)`:扫描
  `root/documents/*.json` 文本,收集 `img_[0-9a-f]{24}.{ext}` 文件名;任一文档不可读时返回
  None 表示"引用状态未知"。`History.save` 裁剪与 `History.remove` 删除文件前检查:
  引用状态未知或文件仍被引用 → 只裁历史索引,保留文件。
- **设计取舍**:
  - 被引用文件保留后历史索引条目仍被裁掉 → 文件成为"暂无索引的孤儿",统一交给资产服务
    GC(阶段 2/3,引用登记 + 宽限期)。历史列表按上限收敛,不因被引用而突破 500。
  - 逐代扫文档是 O(文档数) 文本扫描,当前规模(≤数百文档、每文档 ≤5MB)可接受;
    引用登记表(AssetReference)落地后替换为 O(1) 查询。
  - 显式删除不返回 409 冲突,保持既有 `{"removed": record}` 契约。
- **REFACTOR**:引用扫描函数独立、可单测;`History` 不依赖 `Documents` 类型,仅依赖目录约定。
- **验证**:`.venv/bin/python -m pytest tests/backend/test_history_references.py tests/backend/test_contracts.py tests/backend/test_api.py -q` 通过;全量 pytest 通过;ruff 对改动文件无新错误。

### 第 2 轮:轮询超时且已获取上游任务 ID → 进入可对账的 unknown 状态

- **行为**:已知问题 #3。`Jobs.run` 异常分支(jobs.py)把 TimeoutError 一律标 `failed`;
  而 `resume()` 对账只认 `unknown` 状态 → 已创建上游付费任务后轮询超时即失去对账入口。
- **RED**:tests/backend/test_contracts.py
  `test_polling_timeout_with_external_task_enters_reconcilable_unknown`:
  假 Provider 上报 `upstream-task-1` 后抛 TimeoutError,断言 job.status == "unknown" 且
  `resume()` 对账时第二次调用收到同一 external_task_id(只查询不重提)。
  失败输出:`assert 'failed' == 'unknown'`。失败来自目标缺陷。
- **GREEN**:jobs.py 异常分支增加分支条件(TimeoutError + status running + externalTaskId 存在)
  → 标 `unknown`、message「查询上游超时,上游结果未知,请对账」、progress=None、记录
  `provider.submit status=unknown` 步骤;其余保持 failed。
- **REFACTOR**:无(单分支增加);复用既有 resume/对账通道,未新增状态。
- **验证**:test_contracts.py 24/24 → 全量 pytest 177/177。

### 第 3 轮:取消运行中任务时尽力取消上游并标注"待确认"

- **行为**:已知问题 #4。`Jobs.cancel` 对 running 任务只做本地 `task.cancel()`,
  已派发上游的任务不尝试 `provider.cancel_external`(unknown 分支反而有)。
- **RED**:tests/backend/test_contracts.py `test_cancel_running_job_attempts_upstream_cancel`:
  假 Provider 记录 cancel_external 调用,任务上报 externalTaskId 后长轮询被取消,
  断言 `cancel_calls == ["upstream-task-1"]` 且 message 含「待确认」。
  失败输出:`assert [] == ['upstream-task-1']`。
- **GREEN**:
  - `Jobs.cancel` running 分支:存在 externalTaskId 时打结构化标记 `upstreamCancel="pending"`,
    message「已取消,上游停止结果待确认」,并 fire-and-forget 调用 `_cancel_external_quietly`;
  - `Jobs.run` 的 CancelledError 分支消费该标记,保留待确认文案。
  - 用结构化标记而非解析文案判断语义(遵循"不用文案正则判断恢复动作")。
- **REFACTOR**:无;状态仍是 failed(完整"取消待确认/确认取消"状态机留给阶段 2)。
- **验证**:test_contracts.py 25/25 → 全量 pytest 177/177;ruff 通过。

### 第 4 轮:GET 文档成功不再无条件清除本机草稿

- **行为**:已知问题 #2。bootDocument(app.ts)在 openDocument 成功后无条件
  `clearLocalDraft`;而本地 revision 只在保存成功后同步,revision 对比无法区分
  「保存已确认」与「保存失败/冲突/beacon 未送达」——未确认草稿会被静默销毁。
- **特征测试先行**(记录现状):提取纯函数 `draftMatchesDocument`、`resolveDraftOnOpen`
  (document-api.ts),先按现状实现(有草稿即清除),特征测试「GET 成功且存在草稿时一律清除」
  通过。
- **RED**(改为目标行为后):
  - `GET 文档成功:草稿与服务器一致才清除,含未保存内容时保留` → 失败
    (现状对不一致草稿也返回 clearDraft: true);
  - `保存成功(保存确认)后清除本机草稿`(autosave 成功路径)→ 失败(草稿仍在);
  - 守卫:`保存失败后保留本机草稿,刷新后仍可恢复` 通过(防止矫枉过正)。
- **GREEN**:
  - `resolveDraftOnOpen` 三态契约:`open-server(+clearDraft)` / `needs-choice(携带 draft)`;
  - autosave.ts flush 成功后 `clearLocalDraft`(保存确认即草稿使命完成);
  - app.ts bootDocument 按三态处置:needs-choice 时弹「发现未保存的本机草稿」选择层
    (恢复草稿 / 打开服务器版本,均不销毁草稿);一致草稿静默清除;
  - index.html 增加 `#doc-server-open` 按钮;showDocProblem 同步隐藏新按钮。
- **REFACTOR**:处置策略集中在 document-api 纯函数(可测),app.ts 只做接线。
- **验证**:canvas-document-api.test.ts 9/9;npm run typecheck/lint/build:web 通过;
  前端全量 137/137。浏览器端手动验证列入阶段 4(真实浏览器场景)。

### 第 5 轮:提交校验共享化与 pre-commit 按路径选测

- **行为**:commit-msg 校验器与 CI 共用单一实现;pre-commit 按暂存路径选取快速测试,
  NUL 分隔清单覆盖删除/重命名/中文空格文件名(不照搬 studycommit 的 ACMR 过滤)。
- **RED**:stub 实现(`validateCommitMessage` 恒拒、`pickSuites` 恒空)+ tests/git-hooks.test.ts
  19 项断言 → 全部失败(模块级 ReferenceError 与逐项断言失败)。
- **GREEN**:
  - `scripts/commit-lint.mjs`:`validateCommitMessage`(type/scope 白名单、模糊 scope 拒绝、
    中文描述、英文句点、issue 编号拒绝、`!` 必须正文 BREAKING CHANGE、Merge/Revert 放行)
    + CLI(文件模式给 hook、`--range` 模式给 CI);
  - `scripts/pre-commit-test.mjs`:`readStagedFiles`(`git diff --cached --name-only -z`)
    + `pickSuites`(backend/web 套件映射,负向断言排除 tests/backend 误入 web)
    + 执行器(backend: 对暂存 .py 先 ruff check,再 pytest -q -x;web: npm test);
  - `.husky/commit-msg` 改为 `exec node scripts/commit-lint.mjs "$1"`;
  - `.husky/pre-commit` 增加 `node scripts/pre-commit-test.mjs`;
  - `.github/workflows/ci.yml` 新增 commit-messages job(--range 校验 PR/push 区间)。
- **修复过程中的问题**:`\p{Han}` 经 tsx 转换后报 Invalid property name → 改用 CJK 区间
  `[\u4e00-\u9fff]`(同样只判断"含中文字符");临时仓库 hook 集成测试需 `chmod 0o755`。
- **明确限制**:pre-commit 测试运行于工作区状态而非暂存快照(脚本头与本文档均声明);
  干净 checkout 的最终门禁是 CI。`scripts/e2e-smoke.mjs` 存在预存格式问题,未顺手改
  (不混入无关格式化,留待该文件下次被修改时处理)。
- **验证**:git-hooks 测试 19/19;CLI 实测(合法 0 / 非法 1 / `--range HEAD~3..HEAD` 3 条合规);
  typecheck 通过;前端全量 156/156。

### 第 6 轮:统一错误契约与请求日志(§八/§九)

- **行为**:响应保留既有 `error` 文案,新增 `code`/`recovery`/`details`/`traceId` 增量字段;
  Worker 边界把异常分类为稳定 errorCode;500 安全兜底且堆栈只进日志;
  请求 ID 贯穿响应头/错误 payload/访问日志,ContextVar 隔离并发。
- **RED**(对既有实现失败):tests/backend/test_errors.py 前 4 项与 test_observability.py
  前 3 项 —— `KeyError: 'code'`(404 无结构化字段)、500 响应缺 INTERNAL 兜底字段、
  `KeyError: 'X-Request-ID'`(响应头未回显)、访问日志缺失。
- **GREEN**:
  - `backend/errors.py`:ErrorCode/Recovery 枚举、AppError 类型族(参数/不存在/冲突/
    队列满/额度/上游认证/限流/超时/不可用/未知/存储/损坏)、`status_contract`(迁移期
    HTTPException 按状态映射)、`classify_exception`(仅依据异常类型与 HTTP 状态码,
    不解析文案)、INTERNAL_RESPONSE 安全兜底;
  - app.py:注册 AppError 处理器,四类既有处理器补充结构化字段并注入 traceId;
    internal 处理器改用 `logging.exception`(堆栈进日志);
  - jobs.py 失败分支写入 `errorCode`/`recovery`(unknown 分支为 UPSTREAM_UNKNOWN/reconcile);
  - storage.py:history.json 损坏时抛 DataCorruptedError 快速失败,不再静默回空
    (避免下一次 persist 覆盖原数据,§八.10);
  - `backend/observability.py`:ContextVar 请求 ID、JsonFormatter、键名/键值对脱敏、
    访问日志;app.py 请求中间件回显 X-Request-ID 并记录 method/path/status/durationMs;
    `__main__` 接入 setup_logging(SWARMUI_LOG_FORMAT=json 开启 JSON 行)。
- **修正过程**:access log 误用 `response.status`(实际为 `status_code`);JsonFormatter
  直连 record 时回退读取 ContextVar;`/api/generate` 走显式 ValueError 校验而非
  RequestValidationError(该处理器当前无公开路由触发,保留为防御性实现)。
- **验证**:test_errors 8/8、test_observability 6/6;全量 pytest 193/193;ruff 通过。
- **取舍**:Job 取消路径不加 errorCode(取消非错误);类型化异常逐路由替换属阶段 2
  边界迁移;traces.classify 文案正则仅服务遥测,不参与恢复动作判断。

### 第 7 轮:ORM、事务与应用服务边界(补充要求 §三/§十五.1)

- **环境**:Docker `postgres:16-alpine` 隔离实例(127.0.0.1:55433,aivideo_test);
  发现并规避端口冲突(55432 已被 codeden 项目 PG 占用)。
- **依赖**(显式声明,§二.7):sqlalchemy 2.0.52 / alembic 1.20.0 / psycopg 3.3.5 /
  pydantic-settings 2.15.0 / greenlet 3.x(异步必需,不再依赖传递依赖);版本入 lock。
- **RED**:tests/backend/test_persistence.py 先行,对空 Schema 运行 ——
  `alembic upgrade head` 无版本可执行,查询报 `relation "asset_references" does not exist`
  (缺失的正是迁移与表结构行为)。
- **GREEN**:
  - `infrastructure/database.py`:DatabaseSettings(pydantic-settings,env 前缀
    AIVIDEO_DB_)、异步 Engine/Session 工厂、连接池与四类超时字段、按进程生命周期注释;
  - `infrastructure/orm.py`:workspaces/documents/generation_requests/jobs/attempts/
    assets/asset_references/outbox 八表;幂等唯一约束(workspace, request_id)、
    outbox(status, next_attempt_at)、jobs(status, next_poll_at) 等索引;
  - `infrastructure/repositories.py`:Repository 不 commit;幂等登记冲突经保存点
    翻译为 ConflictError(原因链保留);outbox 领取用 FOR UPDATE SKIP LOCKED;
  - `infrastructure/uow.py`:AsyncUnitOfWork,支持 factory/settings/默认三种来源,
    settings 路径按实例建引擎并在退出时 dispose;
  - Alembic:alembic.ini + env.py + 手写 0001(对照模型逐表审查,非自动生成)。
- **修复过程**:Job 缺 params 列(测试先于实现暴露);greenlet 缺失;psycopg3 连接
  参数应为 connect_timeout;FK 校验暴露测试需先登记 workspace(外键归属即业务要求);
  转换测试的回滚设计缺陷(过期改写须在独立事务验证)。
- **验证**(真实 PostgreSQL):5/5 通过 —— 迁移后模型漂移=0(compare_metadata)、
  任务+幂等+Outbox 同事务原子提交/回滚、同键不同参数冲突且跨 workspace 隔离、
  state_version 条件转换且终态不被过期版本改写、分页排序稳定;base↔head 升降级
  在每个用例执行;全量 pytest 203/203;ruff 通过。
- **边界说明**:异步侧(API)先行;Celery 同步侧与其生命周期隔离属 §十五.2 验证,
  复用同一批模型与迁移。

### 第 8 轮:Celery 执行模型隔离验证(补充要求 §四/§十五.2,ADR-0003)

- **环境**:celery 5.6.3/kombu 5.6.2/billiard 4.2.4(版本入 requirements 与 lock);
  进程内真实 worker(solo 池 + memory broker)+ 真实 os.fork 探针 + 真实 PostgreSQL。
- **RED**:stub(AsyncRuntime 抛 NotImplementedError、celery_app 无契约配置)+
  tests/backend/test_celery_contract.py 7 项契约 → 7 failed(行为缺失,非导入失败)。
- **GREEN**:
  - `workers/runtime.py`:AsyncRuntime(专属线程循环跨任务复用、HTTPX/引擎循环线程内
    惰性创建并断言线程、run 硬超时取消且循环保持可用、close 幂等并 aclose/dispose、
    进程级单例);
  - `workers/celery_app.py`:契约配置(ignore_result/acks_late/reject_on_worker_lost/
    prefetch=1/持久化/confirm_publish/soft+hard time limit/max_tasks_per_child)+
    worker_process_init 预建 runtime、两类 shutdown 信号释放;
  - `workers/usecases.py` execute_generation_step:取消检查点(recovery=abandon 结构化
    标记)优先于一切派发,未派发上游时 upstream_cancelled 必为 None;
  - `workers/tasks.py`:轻量任务只拼装 runtime+用例;probe 任务以进程内观察点替代
    result backend(与"不启用 result backend"契约一致)。
- **验证中修复的三个真实问题**:
  1. fork 探针首版闭包误引父 runtime —— 子进程线程断言拒绝,恰好证明守卫生效;
     改为 make_touch(rt) 工厂后契约按预期通过(父 runtime 调度超时、子进程自建 exit 0);
  2. 探针记录到池子进程 envUrl=None 且 pid 不同 —— monkeypatch 的进程内环境变量
     不可依赖,数据库 URL 改为**固化进 celery conf**,任务从 conf 读取(生产语义更正确);
  3. ruff 报 2 处未使用导入,已清理。
- **验证**:契约测试 7/7(循环复用/客户端所有权与释放/硬超时后循环可用/fork 隔离/
  配置契约/真实 worker 中 async 用例执行/协作取消);全量 pytest 213/213;ruff 通过。
- **未验证(明确列出)**:RabbitMQ 发布确认与消费重投实测(§十五.3)、Redis 客户端
  生命周期(§十五.5)、Linux CI 复跑、prefork 多 worker 并发上限(§十五.4)。

### 第 9 轮:PG + Outbox + RabbitMQ 可靠投递闭环(补充要求 §五/§十五.3)

- **环境**:真实 RabbitMQ 3.13-alpine + PostgreSQL 16 容器与 Python 3.11 runner 同处
  `aivideo-test` docker 网络(执行方式见 docs/runbooks/integration-testing.md;
  本机 colima 的宿主端口转发对新建容器失效,故测试在容器内执行,实例地址经
  `AIVERO_TEST_*` 环境变量参数化)。
- **RED**:stub(messaging/dispatcher/consumer 抛 NotImplementedError)+
  tests/integration/test_delivery_loop.py 7 项 → ERROR/FAIL(行为缺失)。
- **GREEN**:
  - `infrastructure/messaging.py`:topic 交换机 + quorum 队列(DLX 死信拓扑)、
    `publish_event`(serializer=json + persistent + mandatory + confirm_publish
    transport option)、drain_raw 工具;
  - `infrastructure/outbox_dispatcher.py`:claim_and_mark_publishing(SKIP LOCKED
    原子领取并置 publishing)→ **事务外**发布(§三.7)→ 确认后批量标记 published /
    失败退避;连接级失败整批退避;`reclaim_stale_publishing` 回收发布中崩溃的滞留行;
  - `workers/consumer.py`:成功回调 ack;处理失败 reject(requeue=False) 进死信,
    不无限重投(§五.11/§九);消费幂等由用例条件状态转换保证;
  - `workers/usecases.py` execute_generation_step 扩展幂等语义(非 queued 即 skipped);
  - `workers/tasks.py` run_generation_step;OutboxRepository 补
    get/list_pending/count_by_status/mark_publishing/claim_and_mark_publishing/
    reclaim_stale_publishing。
- **验证中抓到的三个真实缺陷(测试先于实现暴露)**:
  1. `outbox.status` 列宽 String(8) 装不下 'publishing' → 加宽为 String(16)
     (迁移 0001 尚未发布,直接修订,漂移测试把关);
  2. kombu publish 手工 content_type 触发 amqp frame 编码 struct.error →
     改用 `serializer="json"`;
  3. dispatcher 以属性访问 dict → 修正为键访问。
- **验证**(runner 容器内,真实 broker+PG+worker):19/19 ——
  闭环(Outbox→确认发布→消费者→worker→DB 终态,重复投递只执行一次)、
  无路由事件不标记成功且退避、broker 不可用保持 pending、双投递器 SKIP LOCKED
  无重复无遗漏、消息体契约(eventId/schemaVersion/payload,JSON 持久化)、
  滞留 publishing 回收、处理失败进死信可查询;宿主侧 12 passed + 7 skipped
  (broker 不可达显式跳过);tests/backend 205 passed;ruff 通过。
- **边界说明**:quorum 队列为本地单节点(§五.10 不宣称多节点容灾);消费确认超时、
  prefetch 压测与 API 受理切换(路由挂接 Outbox)属 §十五.4。

### 第 10 轮:分阶段任务、恢复、全局名额与死信恢复(补充要求 §五.1/§七/§八/§九/§十五.4)

- **RED**:tests/integration/test_lifecycle.py(5 项)+ test_deadletter.py(1 项)对 stub
  与缺列 schema 运行 → 6 failed(NotImplementedError / UndefinedColumn)。
- **GREEN**:
  - `services/acceptance.py`:受理用例 —— 任务+幂等记录+Outbox 事件**同一事务**提交
    (§五.1);幂等重放返回同一任务;同键不同参数 ConflictError;
  - `services/pipeline.py`:分阶段执行 —— 提交阶段(原子领取模型名额→登记上游任务 ID
    →安排 next_poll_at)与查询阶段(退避推进=base×2^attempt 有界+抖动;完成→资产登记
    →终态→释放名额;超截止→unknown+UPSTREAM_UNKNOWN+reconcile,绝不自动重提);
    `scan_due_polls` 调度器领取到期任务并同事务派发 job.poll_due;
  - `tools/deadletter.py`:死信列表/重放 CLI —— 重放前检查业务状态(终态拒绝)、
    重置 attempts、审计 JSONL 记录操作者/原因/原任务/动作(§九.9/10);
  - ORM/迁移:jobs 增加 slot_scope/slot_acquired_at(模型执行名额,§八.2);
  - repositories:set_poll_schedule/try_acquire_slot/release_slot/count_occupied_slots/
    claim_due_polls、requests.find、AssetRepository.add/count。
- **验证中抓到的三个真实缺陷**:
  1. 名额计数按 status='running' 统计,而领取时任务仍是 queued → 并发互相不可见,
     20 并发超发至 9/5 → 改为 slot_scope 非空即占用(与状态解耦);
  2. 仍超发:READ COMMITTED 下 UPDATE 的 EPQ 子查询对其他行用旧快照 →
     以 `pg_advisory_xact_lock(hashtext(scope))` 串行化同 scope 领取,
     20 并发恰好 5 提交/15 延迟(测试断言);
  3. 测试自身两处变量误用(import 缺失、outcome dict 误作 job_id)修正。
- **验证**:runner 容器内 tests/integration **25/25**(persistence 5 + celery 7 +
  投递闭环 7 + 生命周期 5 + 死信 1);宿主 18 passed + 7 skipped(broker 不可达显式
  跳过);tests/backend 207 passed;ruff/typecheck/eslint 通过。
- **边界说明**:§八 的 100 请求全链路 TDD 验收(多 worker 容量路由)待管线接入真实
  Provider 与 §十五.6 指标后执行;名额协调当前以 PG 咨询锁实现(正确性优先),
  Redis 协调在 §十五.5 引入后按 ADR-0002.4 记录评估,不静默偏离。

### 第 11 轮:Redis 任务状态缓存(补充要求 §十/§十五.5)

- **环境**:redis:7-alpine(maxmemory 64mb/allkeys-lru)加入 aivideo-test 网络;
  redis-py 5.3.1 声明为直接依赖并入 lock;runner 容器离线安装。
- **RED**:stub + tests/integration/test_job_cache.py 8 项契约 → 8 failed。
- **GREEN**:`infrastructure/job_cache.py`:
  - JobCache:Redis 哈希存储安全快照(字段白名单,密钥/异常/媒体不入缓存);
    **Lua 条件写入**:按 (stateVersion, progressSeq, executionEpoch) 比较,
    旧回填/乱序事件/旧执行代次一律 stale 拒绝;键缺失时事件路径返回 missing
    (不得凭空恢复,§十.6),数据库回填路径 allow_create 才可创建;
  - TTL 分级:运行态 ttl_s / 终态 terminal_ttl_s(§十.10);
  - SnapshotStore:命中 → 同键 singleflight(进程内 per-key 锁)→ 信号量限并发
    回源数据库 → 条件回填;跨工作空间读取按快照归属校验拒绝(§十.13);
  - 缓存前缀 aivideo:cache: 与协调数据隔离,淘汰只影响缓存前缀(§十.14)。
- **验证中修复的实现缺陷**(Lua ARGV 契约调试):
  1. 循环 `HSET(ARGV[j+3],ARGV[j+4])` 相邻迭代重叠读取,写入整体错位 1 →
     改为按 kv 槽 2 步进 `ARGV[i*2+3],ARGV[i*2+4]`;
  2. `tonumber(HMGET 缺失字段=false)=nil` 且 `nil~=false` 导致比较崩溃 →
     显式存在性分支;
  3. allow_create/ttl 用固定 ARGV 下标在参数扩展后错位 → 统一改为显式位置
     (epoch 入参化)+ `ARGV[#ARGV]`/`ARGV[#ARGV-1]`。
  另修复一致性测试自身缺陷(db_reader 每次生成新 jobId)。
- **验证**:runner 容器内 tests/integration **33/33**(新增缓存 8 项:命中减少
  DB 读取、20 并发未命中合并为 1 次回源、旧回填不覆盖、乱序/旧代次拒绝、
  flush 后旧事件不凭空恢复、Redis 不可用降级且回源峰值≤3、跨工作空间拒绝、
  缓存与数据库最终一致);宿主 18 passed + 15 skipped(不可达基础设施显式跳过);
  tests/backend 252 passed;ruff/typecheck/eslint 通过。
- **边界说明**:Outbox→缓存更新的生产接线(§十.3"数据库提交后经 Outbox 更新缓存")
  依赖事件消费侧,归入 §十五.6 与 SSE 通知一起接线;本轮交付缓存原语与全部
  一致性/降级契约。

### 第 12 轮:缓存/通知生产接线、§八 容量验收、运行指标与权限接口(§十五.6 首批)

- **交付**:
  - `infrastructure/job_cache.py` 新增 `apply_event_with_backfill`:事件应用失败
    (键缺失/版本落后)时以数据库重新投影回填 —— 迟到旧事件无法凭空恢复,
    权威状态始终来自数据库(§十.3/§十.6 生产接线语义);
  - `infrastructure/notify.py` RedisNotify:任务状态发布到 Redis 频道,各 API 实例
    订阅并推送本地 SSE(§十.12 跨实例通知);通知只含标识与状态字段;
  - `services/pipeline.py` 新增 `project_snapshot`:Job 行 → 缓存安全快照投影;
  - `metrics.py`:counter/gauge 注册表;**高基数标签(jobId/attemptId 等)显式拒绝**;
    `collect_outbox_gauges`(待发送数量+最老年龄)/`collect_job_gauges`
    (queued/running/unknown)从数据库聚合,失败静默跳过本轮(不破坏业务);
  - `services/access.py`:可替换访问策略接口 + DevAllowPolicy(**production_ready=False
    显式声明非生产认证**,§十七.5);errors 增加 ForbiddenError(403/FORBIDDEN);
  - 修复 `WorkspaceRepository.ensure` 并发登记 PK 竞争(保存点冲突恢复)。
- **验证**(runner 容器内 tests/integration **36/36**):
  - 桥接:终态事件 → 缓存快照(completed+assetIds)+ Redis 频道通知(先订阅后发布,
    通知不含完整快照);
  - **§八 容量验收:100 请求、上游上限 5、8 Worker 并发 —— 恰 5 提交/95 排队/
    上游任务 ID 无重复(不重复付费);终态释放后排队任务可继续提交**;
  - 指标:计数/仪表聚合正确、高基数标签拒绝、Outbox 最老年龄 42s 可测;
  - 权限:默认策略放行但标记非生产;替换策略跨空间拒绝 → 403/FORBIDDEN。
- **回归**:宿主 integration 20 passed + 16 skipped(基础设施不可达显式跳过);
  我负责的 backend 子集 50/50;ruff/typecheck/eslint 通过;前端 211/211。
  **注意**:宿主全量 tests/backend 当前在并行工作者的进行中文件
  (test_storyboard_runs.py,分镜功能)上挂起 —— 非本批改动,归属其作者处理。
- **调试记录**:桥接测试失败三连因 —— on_error/on_backfill 补丁未匹配文件实际文本
  (NameError 定位)、Pub/Sub 先发布后订阅收不到(订阅提前)、MissingGreenlet
  (UPDATE 后属性过期,显式 refresh)。

### 第 13 轮:兼容性、健康边界与开发编排(§十二/§十三/§七.6)

- **交付**:
  - `workers/consumer.py`:**schemaVersion 守卫** —— 未识别版本的事件不执行业务,
    直接进死信(§十二.4);`SUPPORTED_SCHEMA_VERSIONS` 白名单扩展即升级开关;
  - `app.py` 新增 `/api/health/ready`:liveness(/api/health 不变)与 readiness 分离
    (§十三.1)—— 数据库 down → 503 拒绝新的可靠受理(§十三.3,沿用统一错误契约,
    不含连接串);broker down → 200 + degraded(broker 不可用仍可落库受理,§十三.2);
  - `compose.dev.yaml`:api/worker/beat + postgres:16-alpine/rabbitmq:3.13-alpine/
    redis:7-alpine,版本固定、健康检查显式、依赖条件就绪;`docker compose config`
    校验通过;
  - `workers/celery_app.py` beat_schedule(§七.6 单一调度入口):Outbox 补投递(2s)/
    到期查询扫描(5s)/滞留回收(60s);重复触发由 SKIP LOCKED 领取与业务幂等兜底
    (§七.7);`workers/tasks.py` 三个轻量 ops 任务。
- **验证**:兼容性 2 项(未识别版本死信且不执行/当前版本正常执行,真实 broker);
  readiness 3 项(503 契约/components 分离/无内部细节泄漏);compose 3 项(服务齐备/
  版本固定/健康检查);runner 内 tests/integration **38/38**;宿主回归子集 57 passed、
  integration 20 passed + 18 skipped;ruff/typecheck/eslint 通过。
- **边界说明**:compose 栈的整体启动冒烟属部署验证(CI 或 `docker compose up`),
  单测以清单/版本/健康检查文本断言把关;OpenAPI 类型生成(§十二.1)待 API 受理
  切换(ADR-0002 阶段 C)时一并落地,避免为 JSON 旧契约生成第二套类型。

### 第 14 轮:指标暴露端点与 env 前缀统一(§十三/§十五.6 收尾)

- **交付**:
  - `metrics_api.py` + app include:`GET /api/metrics/runtime` —— 同步端点(采集内部
    asyncio.run,须在无运行循环的线程池线程执行,曾因 async def 端点内嵌套事件循环
    被 except 吞掉而 RED 定位);采集失败降级为进程指标,内部细节不进响应;
  - **修复 env 前缀拼写不一致的真实缺陷**:database.py 的 env_prefix 写成
    `AIVIDEO_DB_`(多 IDE),全仓(compose/celery/tests)均用 `AIVERO_DB_` ——
    一直被"显式传参"掩盖;本次由指标端点采集用例(setenv 依赖 env 前缀)暴露,
    统一为 `AIVERO_DB_*`,compose 的 worker 服务此前会因此回退默认库连接。
- **验证**:指标端点 3/3(渲染/采集/降级无泄漏);readiness 3/3;compose 3/3 +
  `docker compose config` 有效;runner 内 integration **38/38**;宿主回归子集
  60 passed + integration 20 passed/18 skipped;ruff/typecheck/eslint 通过。

## 阶段 1 尚未完成项(后续继续)

- [ ] 前端统一 Axios 请求层 + 稳定错误码映射 + Toast 去重(§七),消费新增的 code/recovery 字段。
- [ ] 类型化异常逐路由替换(队列满 → QueueFullError 等),随阶段 2 边界迁移进行。
- [ ] HTTPX 请求层收敛(超时分类、测试 transport 注入与关闭验证,§十)。
- [ ] known issue #5(SSE 活跃快照缺席误判丢失)与 #6(评测阻塞,初核已异步执行,需回归固化)。
- [ ] scripts/e2e-smoke.mjs 预存格式问题(该文件下次被修改时顺手处理)。
- [ ] ci.yml push 事件 `github.event.before` 在新分支首推时可能为全零,--range 需容错。
