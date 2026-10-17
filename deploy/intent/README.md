# 意图批准产物挂载目录

生产 Compose 将此目录只读挂载到 `/run/config/intent`。默认没有批准库或索引，不启用 RAG。

经人工审校与 shadow 验证后，放入同版本的例句 YAML 和索引 JSON，显式配置容器内路径 `INTENT_EXEMPLARS` / `INTENT_INDEX`，再重启 API。使用 `INTENT_ARTIFACTS_DIR` 可替换宿主目录。候选、隐私原文和运行时导出不得提交到此目录。回滚时 YAML 与索引必须成对切换，详见根 README。
