# 网页桌面端加载基线

协议 `web-load-v1`；原始报告目录 `web-baseline-2026-10-03/`。采集桌面 Chrome，不启用手机 viewport、触控或移动端 UA。实验环境为 headless 桌面浏览器，不能代替真实用户分布或有窗口浏览器的性能。

## 复测

先运行 `npm run build:web`，完成所有检查后再开始正式采样，不在采样期间执行构建或全仓测试。

```sh
.venv/bin/python scripts/perf/server.py --port 7815
# 另一个终端；目录必须不存在，避免覆盖基线
node --import tsx scripts/perf/web-load.ts --repeats 3 --out /tmp/web-candidate
node --import tsx scripts/perf/web-compare.ts docs/performance/web-baseline-2026-10-03 /tmp/web-candidate
```

服务只监听 loopback，配置为 Mock，使用独立临时数据目录，不读取实际 Provider 密钥。固定场景含 500 和 5000 个文字对象文档，工作台显示两个项目；画布加载 500 对象文档。测试文档使用固定时间字段，清除 localStorage 后重新打开同一场景。数据中不含图片/视频用户素材。

## 固定条件

- 1440×900 CSS 像素，DPR 1，CPU 降速系数 1。
- 下载 10 Mbps、上传 2 Mbps、延迟 40ms。CDP 吞吐单位为字节/秒。不是公网网络实测。
- 落地页 `/`、工作台 `/workspace`、画布 `/canvas?doc=固定ID&benchmark=1&mode=clean`。
- 每页交替采集 cold/warm，每种缓存 3 轮。cold 清空 HTTP 缓存、localStorage、cookie 和 service worker；warm 保留前一轮 HTTP 缓存，清空 localStorage。
- 热访问遵循实际 Cache-Control；`no-cache` 仍可能重新验证或下载，不假设传输为零。每页冷缓存独立，因此没有模拟跨页面共享缓存。
- DOM 就绪之后至少观察 5 秒，同时等待字体和有限资源请求结束，最长 90 秒。SSE/WebSocket 不参与资源完成判断或静态传输总量。

## 指标定义

| 指标 | 定义及用途 |
| --- | --- |
| networkBytes | CDP 完成请求 encodedDataLength 之和，含接收侧协议开销；命中浏览器缓存计 0。排除 SSE/WebSocket，包含 HTML/API/字体/图片/JS/CSS 等实际请求。不是网络抓包的全部上下行字节。 |
| jsNetworkBytes | Script 请求接收字节之和，验证压缩和缓存收益。 |
| jsDecodedBytes | Resource Timing 中 script 的 decodedBodySize 之和，辅助核对实际加载代码量。 |
| FCP | First Contentful Paint，首个内容绘制时间。 |
| lcpObservedMs | 截至观察结束的最后一个 LCP 候选及元素；不是用户离开页面后最终的现场 LCP。canvas 内容不在 LCP 候选中。 |
| clsObserved | 观察窗口内排除近期输入的布局偏移，取最大会话窗口值；不是完整访问生命周期 CLS。 |
| domReadyProxyMs | 落地页标题出现；工作台最近项目区域、textarea 出现且加载骨架消失；画布 bridge 报告 500 个对象、viewport 非零、canvas DOM 存在。之后再等待一次 rAF。该值是就绪结构代理，未证明交互完成或 GPU 实际呈现。 |
| longTaskTotalMs / longTaskBlockingMs | 观察窗口内长任务总时长 / 超过 50ms 部分之和。后者不是标准 Lighthouse TBT，不单独归因于 JS 解析。 |
| navigation / resources | TTFB、DOMContentLoaded、load 及每项资源原始 timing，供瓶颈分析。 |

每轮保存请求列表、状态码、缓存命中、Content-Encoding、Cache-Control、页面错误、浏览器 timing、字体时序、画布对象数量。首次 cold/warm 保存截图。`environment.json` 保存浏览器版本、CPU、内存、工具版本、采集器哈希、源码静态资源与构建产物哈希、场景哈希；当前工作区有未提交改动，单独 HEAD 不能复现源码，应保存对应源码快照。

比较器仅比较相同环境、采集器和场景，排除错误或超时轮次，不足 3 个有效样本时不计算收益；输出中位数和样本范围。3 轮属于初步实验基线，不证明小幅改善显著。正式发布收益建议增加轮数、交替 A/A 与 A/B，记录设备负载，并验证有窗口浏览器。响应协议开销和连接状态可造成字节小幅波动。

尚未采集：真实公网/生产反向代理、真实业务图片视频、INP、实际操作完成时刻、JS 解析/执行的专门 trace、功能首次打开时间。优化相应项前需补齐同场景基线，不能从本报告推算它们。

依据：[CDP Network](https://chromedevtools.github.io/devtools-protocol/tot/Network/)、[CDP Emulation](https://chromedevtools.github.io/devtools-protocol/tot/Emulation/)、[LCP 定义](https://web.dev/articles/lcp)。

## 本次观察

18 轮全部通过就绪及请求错误检查。四张工作台示例 PNG 的接收量合计 9,647,247 字节，约 9.20 MiB，是冷访问传输的主要部分。三个页面的 JS 响应 Content-Encoding 均为 identity；热访问的 JS 下载量与冷访问基本相同。压缩和缓存收益应以本地相同服务策略下的优化后请求实测验证，不能把 Mock 服务结果当作生产代理当前行为。

已通过 TypeScript 检查、新增采集脚本 ESLint、基准服务 Ruff 和 241 项前端测试。全仓 `npm run verify` 在已有 `scripts/pre-commit-test.mjs:110` 的 curly lint 错误处停止。
