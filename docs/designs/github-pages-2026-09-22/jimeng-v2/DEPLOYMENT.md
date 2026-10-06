# GitHub Pages 发布记录

- 线上地址：https://weilengdezaoshang.github.io/aiVideo/
- 发布提交：23133b6b5825ce3cbd398b5fc3d1d0ed7bed5066
- Pages：https://github.com/weilengdezaoshang/aiVideo/actions/runs/35698747208 （success）
- CI：https://github.com/weilengdezaoshang/aiVideo/actions/runs/35698747212 （success）
- 独立发布工作树：`/tmp/frayune-pages.tzvGr5`，分支 `codex/publish-pages`，基于远端原 main `82f0ffe`。未推送当前开发工作区中尚未发布的应用提交或未提交改动。

远端仍是早期应用实现，本次发布官网、README 封面和产品介绍。线上安装区改为接入流程并明确标注新版代码尚未同步，避免给出当前公开仓库无法执行的 Python 安装命令。原远端 README 移至 `docs/legacy-readme.md` 并修正相对链接。当前开发工作区 README 继续保留开发版本说明，未以远端早期说明覆盖。

GitHub Pages Source 已启用 workflow，工作流仅上传 website。独立发布副本 npm run verify 通过，40 项测试通过；GitHub CI 和 Pages 部署成功。
