# Git 提交规范(aiVideo)

> hook 强制结构校验(commit-msg),内容是否为准确中文动宾表达由人工/审查补充;
> 正则不能完整判断语义与简繁体,不宣称其做到。

## 格式

```text
type(scope): 中文描述.
```

- 结尾英文句点 `.`;主题行不含作者、日期、issue 编号、测试结果。
- 技术标识符可保留英文(如 `fix(queue): 修复 TrialResult 未关联 runId 的问题.`)。

## type

feat、fix、docs、refactor、test、perf、style、build、ci、chore、revert。

## scope(必填)

- 小写英文,可含数字或连字符。
- 通用:`server`(API/服务端)、`web`(前端)、`provider`(生成后端)、`queue`(任务调度)、
  `store`(持久化)、`config`(配置)、`project`(仓库级配置与文档)。
- 明确新模块可用:`generation`、`assets`、`documents`、`editor`、`logging`、`errors`、
  `worker`、`contracts`、`infra`、`eval`。
- 禁止 `all`、`misc`、`other` 等模糊 scope;不沿用参考项目特有的 desktop/mobile scope。
- 一次提交只用一个最能代表主要改动的 scope。

## 描述

- 简体中文动宾结构,说明结果,不叙述实现过程。
- 禁止"修改代码""更新内容""处理问题"等模糊表达。

## 破坏性变更

```text
feat(contracts)!: 调整任务错误响应结构.

BREAKING CHANGE: 客户端需要读取 code 和 recovery 字段,
不再依据 error 文案判断恢复操作.
```

## Merge / Revert

- `Merge ...`、`Revert ...` 开头的主题行由 hook 直接放行(沿用现状)。

## 提交边界

- 一个提交一个完整目标;测试、实现与相关重构可同提交,提交时受影响测试为绿色。
- 不混入无关格式化、功能与历史迁移。
- 不提交密钥、私有 `.env`、`data/` 运行时产物、媒体与测试临时产物。
- 未经明确授权不实际创建提交;不自动推送、合并或部署。
- 不使用 `--no-verify` 或 `HUSKY=0` 绕过检查。

## hooks 与 CI

- pre-commit:`npx lint-staged`(prettier/eslint 修复暂存文件)→ `npm run typecheck`
  → `node scripts/pre-commit-test.mjs`(NUL 分隔暂存清单,按路径选取 backend/web 套件)。
  已知限制:测试运行于工作区状态而非暂存快照;干净 checkout 由 CI 把关。
- commit-msg:`exec node scripts/commit-lint.mjs "$1"`,与 CI 的 commit-messages job
  共用同一校验实现,不维护两套正则。
- CI 是最终完整门禁,不依赖 hooks 是否安装。
