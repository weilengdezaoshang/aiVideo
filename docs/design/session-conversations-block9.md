# 块 9：画布多轮会话设计（已确认实施）

## 现状与复用

Session、Message、Client 表已存在，含消息序号、处理状态、摘要游标、在线租约；当前 start() 却直接创建 Run，活动 Run 存在时返回 session_has_active_run。复用这些表、块 2 的 outbox/定时器/消费框架、块 8 的 CascadePlanner/原始输出持久化/审核员、现有生成队列/积分/MediaHost/画布 CAS。当前 user_interrupt 会停止 Run，需要区分新会话插话与旧取消语义。

最近产物目前是启动时计算的有限 JSON 列表；资产版本表与 append_asset_version 已有，但工人 publish 每次 create_asset。现有 /api/events 用 Redis 唤醒后查库广播快照，没有可续传 id；旧单 Agent 事件接口已有 after/Last-Event-ID 和有序重放，可复用其协议。notify daemon 已支持 core.changed。现有工具参数尚未提供 seed。

## 范围与模块

- 新增 core/sessions：消息受理、合并与派发、输入修订、产物索引/指代、摘要/决定、重做、版本查询、SSE/通知。
- 修改 supervisor：从输入修订组装上下文，读取纠正来源，处理插话边界；复用现有 Planner 和唯一审核入口。
- 必要修改 execution：提交前修订校验、首次 submitted 标记、编辑追加版本。扣费/生成/存储/画布写入仍只有工人可执行。
- 必要修改 tools/provider 适配：仅给确实支持种子的能力增加 seed schema 与传递，发布新目录版本；旧 Run 的目录锁不变。
- 复用 production SSE、notify daemon、文档 CAS 与评测后台。新增前端 TypeScript 会话 store、恢复视图及版本切换/比较组件，只做必要接线；现有画布文件有用户未提交改动，不覆盖或整文件替换。
- 旧单 Agent/手动画布入口仍可用；本块不引入助手、不实现自主失败重规划。

## 数据契约与迁移

| 对象 | 增补内容与约束 |
| --- | --- |
| Session | active_run_id、input_revision、event_cursor、dispatch_at、version；活动 Session 的 workspace/canvas/user 唯一约束，同用户多端共用，团队成员会话默认不混合 |
| Message | client_id/client_message_id、请求哈希、intent、关联/纠正 Run、归属 input_revision；保留现有 sequence 和 pending/claimed/processed/cancelled；同幂等键不同内容返回冲突 |
| Run 输入修订（新增） | run_id/revision 唯一；按序 message_id、有效请求、选择/引用快照、明确用户纠正、确认决定引用、上下文哈希；只追加，不改冻结 Run.snapshot/invariants |
| Run/Plan/Task | 当前及归属 input_revision；Run 增加首次 submitted 时间/任务标记；Plan 记录来源修订，旧计划保留；Task 提交校验修订及审核准入 |
| Session 产物索引（新增） | session_id/ordinal 唯一，task_id/产物端口唯一；完成时间、core_asset/version/stored_asset、run/task、画布节点、位置快照；可分页，不以有限 recent_assets JSON 为唯一记录 |
| 确认决定（新增） | 来源消息/确认任务、决定值、版本、明确取代关系；用户后续明确修改才可取代，模型摘要无权改写 |
| 摘要修订（新增） | 覆盖消息序号范围、正文、决定引用、输入哈希、写单/调用记录；结果先落库再 CAS 推进 summary_cursor，原消息不删 |
| Session 事件（新增） | session_id/sequence 唯一，event_id 唯一、causation_id、run/task/message/version id；与状态在同事务写入，队列载荷只带 id |
| Client/通知 | 复用 Client 心跳、租约、游标；持久化站内完成通知，终态事件/接收用户唯一去重，可查询与标记已读 |
| 资产版本 | 复用 CoreAssetVersion/CoreAssetLineage，编辑绑定已有资产版本后追加；首次编辑上传图时为原图建立初始版本，再追加结果；旧存储对象不覆盖 |

active_run_id 是可恢复缓存指针，不加到 Run 的外键，避免消息入队因 FK 的 KEY SHARE 等待被工人锁住的 Run；Run.session_id 的归属外键及每 Session 活动 Run 唯一索引仍保留，读取缓存时验证归属。

新约束上线前检查历史重复 Session；如存在冲突，迁移给出明确错误与离线整理指引，不静默合并用户历史。所有新增迁移支持空库升级/回滚。

## 消息、合并与提交的原子边界

1. API 先锁 Session，分配数据库 sequence，保存消息及 outbox/派发时间，再返回 202。重复 client_message_id 返回同一消息；不同文本复用同键返回 409。
2. 首条消息先收集，静默合并窗口建议默认 1.2 秒，连续输入最多等待时间单独配置。窗口内的三条消息保存为有序原话，不能简单只取最后一句。1 秒内“狗→不对兔子→再暗一点”只创建一个 Run，Planner 收到完整语义链。
3. 派发器使用数据库租约及非阻塞扫描。Session 活动 Run 指针与 Run 创建同事务完成；重启读取数据库继续，不用进程内计时器维护事实。
4. 活动 Run 从未 submitted 时，窗口内消息产生新输入修订；旧 Planner 调用若已发生，其原始输出和用量照常记账，但旧修订不得入队/提交。新修订使用新 Invocation 幂等键，不覆盖原调用，不以恢复名义重调旧模型请求。
5. 与工人争抢时以 Run 行锁及首次 submitted 标记为边界：工人先提交则按插话处理；消息先更新修订则旧 Task 无法提交。不能只检查当前有没有 submitted 状态，因为已成功任务也属于“曾开始执行”。
6. 合并窗口外若仍有活动 Run，消息仍进入该 Run 的输入队列，不直接开重复 Run；终态后按序派发下一轮。活动顶层 Run 另加数据库唯一保护，助手 Run 不受该顶层约束影响。
7. 新入口与原 start() 共用 Session 解析和锁序；调整原先“先锁画布再锁 Session”的受理方式，避免新旧入口互相死锁。消息控制按 Session→Run；工人提交在 Run 内做修订校验，不反向获取 Session 锁。

## 插话与纠正

- 新消息与 user_interrupt 同事务写入 outbox，只附 message_id 等 id。新会话插话按消息记录识别；旧取消入口兼容原语义，显式取消统一用 user_cancelled，不能让一句“换成蓝色”走 stop(cancelled)。
- 暂停旧计划尚未提交的后续步骤；已经提交的任务不重提、不重复预扣，按现有服务商能力结算并保留产物。到安全边界后，在同一 Run 新建输入修订和 Plan 版本，针对最新产物执行用户的新要求。
- 新 Plan 仍过全部审核，受同一个 Run 的剩余积分/token/思考预算和 scope 限制。内容拦截不自动重试，失败不自动改模态。本块只处理用户明确发起的需求修订，不增加自主失败重规划。
- “不是这个意思”关联上一个 Run 的全部 Plan 版本和结果，自动复用现有 feedback 机制标记 correction/负样本；仅选中的那一个父目标被引用，不混入其他 Session。活动 Run 内纠正不另建 Run，上一轮已结束时创建有 correction_of_run_id 的下一轮。
- 确认过的决定单独保存；后来的明确用户纠正可创建有证据的取代记录。审核员读取对应输入修订的有效决定，不能直接改旧冻结 invariants，也不能让模型自行解除硬约束或扩大预算。

## 指代、编辑、摘要与重做

- 指代：代码依据选中对象、Session 产物序号和画布几何处理“刚才那个/第二张/左边那个”；“第二张”默认指当前明确的一批产物中的第二个，无批次上下文时询问，不任意跨历史选图。“左边”取当前可读画布的几何关系，模型不算坐标。权限过滤后候选不唯一或无可用产物则澄清。
- 编辑：有明确已有产物时优先选择实际可用的 image_edit/inpaint 等能力；没有局部蒙版/区域信息时不伪造局部重绘参数。编辑追加资产版本及血缘，原图保留。
- 摘要：达到配置阈值后异步压缩旧对话，保留近期原文、确认决定和仍有用的产物引用。模型摘要先持久化；压缩失败可用确定性摘录回退，不妨碍会话受理，不删除用户原话。摘要不得成为新执行指令。
- 重做：不用 Planner 重新理解，复制明确的上一可执行 Plan 为新 Run，保留工具锁、顺序、依赖和非随机参数；生成步骤只更换代码生成的种子。只读/确定性步骤不伪造随机参数。所有步骤重新审核、生成重新扣费，开始前说明预计再次扣分。
- 种子缺口：只为实际服务商支持的工具开放 seed，新工具目录版本不会更改已锁旧 Run。旧计划/能力不能保证按种子重做时返回明确不支持原因，不把偷偷改提示词或换模型当成“只换种子”。
- 版本：历史/对比接口返回有权限的两个自有资产版本；切换通过工人执行确定性画布命令，并带文档 revision/锚点 CAS，冲突返回明确结果。切换既有版本不重新生成、不扣生成积分。

## 多端、断线和通知

- 单一数据库序号定义消息顺序；设备只提交 client_message_id，不提交权威序号。客户端按 message/event id 去重，按版本拒绝回退。
- 新增 Session SSE，复用既有 Last-Event-ID/after 协议与认证；Redis 仅为唤醒提示，断线按数据库 Session 事件重放。无游标、过旧或不合法游标返回带一致水位的恢复快照，再重放该水位之后的事件，避免订阅/取快照间丢更新。
- 回到画布获取 summary、确认决定、未处理消息、活动/近期 Run 与产物索引。在线端都订阅同一 Session；不依赖发消息的窗口保存进度。
- 长任务完成写持久化站内通知并经现有 notify/SSE 广播；离线后回来仍可见。本块不擅自增加邮件、短信、浏览器推送权限或其他外部通知。

## 接口建议

- POST /api/core/sessions：按画布解析/创建会话，认证后限定同 workspace/user。
- POST /api/core/sessions/{id}/messages：幂等入队，返回 message_id/sequence/dispatch_at/已有关联 run_id（尚未创建时可空）。
- GET /api/core/sessions/{id}：恢复快照、摘要、决定、Run 和队列状态。
- GET /api/core/sessions/{id}/events：带游标的 SSE；Client 心跳/离线接口复用在线端表。
- GET /api/core/sessions/{id}/artifacts：产物序号、批次和版本分页索引。
- POST /api/core/sessions/{id}/redo：显式 Plan 引用、幂等键、预算；不接模型解释。
- GET /api/core/assets/{id}/versions、GET /compare：历史与比较数据；POST /switch：提交确定性工人命令，带画布 CAS。
- GET /api/core/notifications、POST /{id}/read：持久化站内通知。

## 配置、验证与提交

Session 配置统一包含：合并窗口、最大合并等待、队列上限、扫描批量、租约/心跳/重试、摘要长度与近期保留量、产物候选上限、SSE 重放保留/批量、慢任务通知阈值、种子范围等。数值不散落业务代码。

测试按模块单测和显式隔离 PG/Redis/RabbitMQ 集成分层；生成服务使用既有 FakeProvider，Planner 用 Fake/协议替身，摘要用假接口。至少包含：

1. 三条消息在 1 秒内合并为一个 Run，任务参数表现为更暗的兔子，原话与纠正来源完整。
2. 消息并发与 Task submitted 竞争的两个顺序；旧修订/迟到模型输出不能执行。
3. submitted 后插话只关联当前 Run，安全边界前不重复提交/扣费；明确取消仍有效。
4. 两端并发输入得到相同序列，两端 SSE 内容一致；断线重放与快照交界不丢事件。
5. 摘要/模型原始输出持久化前后崩溃恢复；确认决定不被压缩丢弃。
6. 纠正关联上轮计划/结果并产生负样本；跨用户、跨 Session 引用被拒绝。
7. 重做零次重新理解，仅种子变化，审核/扣分各一次；不支持种子时明确拒绝。
8. 编辑版本只追加，血缘完整；切换/比较及 CAS 冲突；完成通知重复投递去重。
9. 指代评测至少 36 条，覆盖时间、序号、左右、选择优先、歧义、失效资产与跨 Session 隔离，接入既有评测后台，规则结果与真实 LLM 成绩分开报告。

最后运行 npm run verify、当前 HTTP/存储集成和原 37 个可靠性场景，并将新用例加入回归。按“持久化/受理→主管/工人接线→多端恢复与版本 UI→评测回归”分批提交，采用仓库规定的 type(模块): 中文描述. 格式。

本设计已获用户确认，使用 Grok 隔离工作树开发并由 Codex 独立检查与验证。运行配置与验证命令见 docs/runbooks/session-conversations.md。
