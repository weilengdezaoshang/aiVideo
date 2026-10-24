# 同一角色卡生成一致性基线记录

> 场景：同一张 subject/character 资产卡，生成 N 张图，人工抽检主体一致性。
> 本文件是完成标志「同一角色卡生成 5 张图，人工抽检一致性并记录基线」的记录表。

## 流程（复跑方法）

1. 准备工作区与一张角色卡：
   - 上传一张主体清晰的图片（触发异步识别，资产 `recognition` 字段会写入主体类别与描述）；
   - `POST /api/core/cards`（`card_kind=character`，`source_asset_id=<上传图>`，可 `starred=true`）。
2. 发起会话 Run，请求「让这个角色出现在 5 张海报里」。主管把卡解析进 `subject_cards` 参数，
   Worker 冻结 Run 版本并把卡的识别描述并入提示词（`resolved_parameters._card_refs` 留痕）。
3. 5 张产物落在画布后人工抽检。

Mock 环境链路验证（零成本、可自动化）见
`tests/integration/test_asset_cards_pg.py::test_worker_executes_card_task_and_card_parameter_generation`：
断言同一 Run 内卡引用被冻结为同一版本、提示词确定性合并、结果可审计。
Mock 生成是确定性占位图，**只能证明链路，不能证明视觉一致性**——视觉基线必须用真实模型跑。

## 基线记录表

| 日期 | 模型/绑定版本 | 卡 | 张数 | 一致性评分(1-5) | 主要缺陷 | 抽检人 | 产物 Run |
| --- | --- | --- | --- | --- | --- | --- | --- |
| （待真实模型环境补录） | | | | | | | |

评分口径：5=主体身份与细节全部一致；4=身份一致、细节漂移；3=身份偶有偏差；
2=常被认成另一角色；1=无关联。每行附 5 张产物的 Run id 以便追溯（血缘在
`core_asset_lineage`，卡引用在任务 `resolved_parameters._card_refs`）。

## 已知边界（V1）

- 卡对生成的影响目前只有「识别描述并入提示词」一种通道（能力表 `card_support.multi_reference=false`），
  主体外观保真受文字描述上限约束；多参考图通道接入后再复测并更新基线。
