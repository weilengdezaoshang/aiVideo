# 前端产物体积基线

基线：`bundle-baseline-2026-10-03.json`。协议：`web-bundle-v1`。

采集使用空的临时目录执行现有 tsc 和生产 React 构建，不清理或修改运行中的 `.web-build`，不改变打包策略。因此排除旧产物残留。范围为编译生成的文件，不包含源码目录中直接提供的 HTML、CSS、vendor、图片、Python 依赖或容器镜像。

```sh
# 优化前保存新基线（拒绝覆盖已有报告）
node --import tsx scripts/perf/bundle-size.ts capture /tmp/bundle-baseline.json
# 优化后采集
node --import tsx scripts/perf/bundle-size.ts capture /tmp/bundle-candidate.json
# 输出字节差和节省百分比（负数表示变大），以及文件增删/大小变化
node --import tsx scripts/perf/bundle-size.ts compare docs/performance/bundle-baseline-2026-10-03.json /tmp/bundle-candidate.json
```

报告记录逐文件原始字节、SHA-256、gzip level 9、Brotli quality 11，以及 React bundle 中各输入的 bytesInOutput。总量分为全部产物、source map、运行文件。压缩总量为逐文件独立压缩之和，用于固定条件下比较；实际服务器压缩设置、请求范围和缓存会影响传输量。依赖贡献只表示打包器归因的原始字节，不表示独立压缩后的贡献。

采集记录 Node、esbuild、lockfile 哈希、HEAD 与工作区是否有修改。比较要求环境与 lockfile 一致；升级依赖时应在同一工具链重新采集旧/新版本，并单独说明依赖变更。当前基线来自有未提交修改的工作区，HEAD 不能完整复现它，应保留对应源码快照；逐文件哈希可以验证产物是否一致。

采集后再重复一次 A/A 比较，确认当前环境产物稳定。不要覆盖原始基线。新增拆包后，可用报告的文件哈希和大小核对共享 chunk；运行文件总量不等于单页面首屏下载量。

本基线不能证明加载时间改善。压缩和缓存优化需要另行采集同一浏览器、同一网络限制下的冷/热缓存请求字节与耗时；懒加载需要记录首页和首次打开功能的耗时。现有 `scripts/perf/README.md` 的画布协议采集稳态交互，不覆盖页面冷启动。

## 2026-10-03 实测

| 范围 | 原始字节 | gzip 字节 | Brotli 字节 |
| --- | ---: | ---: | ---: |
| 全部产物 | 9,154,912 | 2,191,618 | 1,768,045 |
| source map | 7,299,464 | 1,618,803 | 1,288,277 |
| 不含 source map | 1,855,448 | 572,815 | 479,768 |

共 166 个文件。独立重复构建的全部文件大小、压缩大小和 SHA-256 均一致，A/A 差值为 0。此前对现有 `.web-build` 的统计为 9,781,534 字节；其中包含 626,622 字节的旧产物残留，因此使用本报告作为后续体积比较基线。
