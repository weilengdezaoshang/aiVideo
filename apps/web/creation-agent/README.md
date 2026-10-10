# 对话式创作 Agent

独立 React 18 + TypeScript + Vite + Zustand + Tailwind 前端。沿用帧屿集暗色视觉，不依赖现有工作台的 React 19、API、登录或基础设施。生成能力均为 mock，支持本地脚本和服务端 Host；刷新清空前端会话。

## 启动

```sh
cd apps/web/creation-agent
npm install
npm run dev
```

打开终端显示的 Vite 地址。`npm run build` 进行类型检查并生成 `dist/`；`npm test` 运行独立 React 18 DOM 测试。不要提交 `node_modules/` 或 `dist/`。

根仓库检查需要先安装此独立包的依赖，再运行根目录 `npm run verify`。根测试包含状态测试和独立 DOM 测试入口。

## 服务端联调（S1/S2/S3）

在仓库根运行 `npm install && npm run dev:creation-server`，再启动此前端。
「对话来源」默认选中「服务端 Host」，普通发送通过 `http://127.0.0.1:8787/v1/conversation` 接收 SSE；停止通过 AbortController 和 `/v1/stop_stream` 取消。消息快照原地替换 parts，避免与文字 delta 重复。

服务端已实现 Host↔Planner、todo_plan 和积分中断/恢复。普通短需求直接出图；复杂广告/镜头需求生成唯一计划，并在 120 积分确认处结束当前 SSE，批准或取消会发新的 resume_interrupt POST，仍更新同一助手消息。两个演示按钮固定使用本地 mock，完整 P1 确认演示仍可运行。详情见 [服务端 README](../../server/README.md)。

## 演示

- **跑 P0 演示**：发送咖啡海报需求，流式回复、工具状态、一张作品、完成。
- **跑 P1 演示**：发送三镜头广告需求，更新唯一计划，停在 120 积分确认处。点击确认后在**同一条助手消息**继续生成三张作品、更新计划和总结。没有真实视频生成或积分扣除。
- **故障模拟**：连接失败恢复草稿；工具失败保留此前内容；下次恢复失败只触发一次，确认卡仍 pending，可再次确认。
- **停止**：在连接、生成或等待确认阶段可停止。已完成作品保留；迟到事件不会追加。取消确认显示“已取消，未消耗积分。”。
- 点击作品打开原生 dialog，Escape、关闭按钮或背景可退出，焦点返回作品按钮。

本地 SVG 为静态占位图，全部离线可用。选择「本地演示」时，普通输入包含“广告”“30”“三个”或“3个”走 P1，其余走 P0。

## 状态机与数据

`idle → sending → streaming ⇄ awaiting_user → idle`。非 idle 时禁用输入和演示发送；停止、错误、完成恢复 idle。

Zustand 保存 messageIds + messagesById、会话状态、草稿备份与确认提交状态。消息仅通过 `parts` 渲染，不维护第二份消息 DOM 数据。每条助手消息只有一个 text 与 plan part；总结追加到唯一 text part，因此显示在原文本位置。工具开始关闭文本阶段，`text_phase` 显式重新允许文本追加。

运行上下文包含运行 ID、助手消息 ID、AbortController。停止先使运行失效，再 abort 并调用可选 transport.stop；所有事件必须匹配活跃运行 ID。mock 等待支持 abort，事件不能写回停止的会话。

确认按钮同步设置 confirming，防止连点。resume 成功才写 accepted；失败保留 pending/awaiting_user。确认等待中的停止把卡标记 expired。

## 后端事件映射

下表为本地 mock 与 store 的内部事件；新增 server-transport 实现 `Transport`，将服务端 system/text_delta/message SSE 转成这些内部事件或 message_snapshot。当前 FastAPI HTTP 契约未修改。

| 事件 | 数据与行为 |
| --- | --- |
| stream_start | runId；sending → streaming |
| text_delta | runId、text；追加唯一 text part |
| text_phase | runId；工具结束后开启新的文本阶段 |
| tool_start | runId、callId、label；追加 loading 工具行 |
| tool_success | runId、callId、artifact；完成对应工具，追加作品 |
| tool_failure | runId、callId、message；工具失败，消息 failed，回 idle |
| plan_update | runId、steps；原地替换唯一计划步骤 |
| confirm_required | runId、confirm；awaiting_user，滚到卡片并聚焦 |
| stream_complete | runId；消息 completed，回 idle 并聚焦输入 |
| stream_error | runId、message；保留结果，消息 failed，回 idle |
| resume_interrupt | Transport.resumeInterrupt(runId, confirmId, signal, faults)；继续同一消息 |
| stop_stream（可选） | Transport.stop(runId)；本地先取消，可接 POST |

所有类型见 `src/model.ts`。工具行额外存 callId，区分三个同名生图调用。新增 transport 不应绕过 store 的运行 ID 检查。

## 组件

`App → MessageList → MessageBubble → parts/PartView`，卡片只读取 part。`Composer` 管理键盘与输入法，store 管理发送/确认/停止。计划、工具、错误、作品和确认均使用中文状态；减少动态效果偏好下关闭动画。

## 验证

状态测试覆盖空白/忙碌发送、乐观更新、唯一 text/plan、连接恢复草稿、确认防连点/重试/拒绝、停止隔离迟到事件与保留作品、工具失败。DOM 测试覆盖输入法/换行、禁用、焦点、上翻新内容提示和作品预览。

### 本次验证结果（2026-10-09）

- 根目录 `npm run verify`：通过。前端 360 个测试通过；后端 486 个通过、2 个跳过，有一条既有 Starlette/AnyIO 弃用提示。
- 独立 `npm run build`：通过；独立 DOM 测试 4 个通过；状态与真实 mock 流测试 6 个通过。
- 独立依赖 `npm audit`：0 个已知漏洞。
- 浏览器：检查了桌面 P0 出图，P1 等待确认/确认后完成三张作品、唯一计划卡，390px 移动端确认布局，以及作品放大、Escape 关闭和焦点返回。视觉为主 Agent 自检，未做独立视觉评审。

取消确认、停止或执行失败时，进行中的计划步骤转换为 `cancelled` 并显示“已取消”，已完成步骤保持完成，未开始步骤保持待执行；取消后的计划不再显示 spinner。`Step.status` 为此增加 `cancelled` 终态。
