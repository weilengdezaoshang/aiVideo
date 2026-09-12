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

## 阶段 1 尚未完成项(后续继续)

- [ ] 统一后端错误契约(类型化异常 + 稳定错误码 + 全局处理器),当前仍为 HTTPException+中文文案。
- [ ] 结构化日志/请求上下文/脱敏(§九)。
- [ ] HTTPX 请求层收敛(超时分类、进程级复用已有,补测试 transport 注入与关闭验证)。
- [ ] 前端统一 Axios 请求层 + 稳定错误码映射 + Toast 去重(§七)。
- [ ] known issue #5(SSE 活跃快照缺席误判丢失)与 #6(评测阻塞,初核已异步执行,需回归固化)。
- [ ] scripts/e2e-smoke.mjs 预存格式问题(该文件下次被修改时顺手处理)。
- [ ] ci.yml push 事件 `github.event.before` 在新分支首推时可能为全零,--range 需容错。
