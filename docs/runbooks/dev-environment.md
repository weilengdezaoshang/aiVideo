# 本地开发环境 vs 生产环境

仓库提供两套显式配置,启动方式互不混用:

| | 开发环境 | 生产环境 |
| --- | --- | --- |
| 入口 | `npm run dev:stack` / `npm run dev:stack:down` | `npm run stack:up`(先填 `.env`) |
| 配置 | `deploy/dev/api.env.example` → 生成到 `data/dev/api.env` | `.env.production.example` → 复制为 `.env` |
| Provider | mock(`deploy/provider-routes.mock.json`,零付费) | 自选(Cloud/ComfyUI/Mock) |
| 身份 | 本地 OIDC fixture(8444,自动放行) | 真实 OIDC(IdP) |
| 进程 | 基础设施+worker 跑 docker(`aivideo-dev` 项目);API(8443, TLS)与 OIDC 跑宿主机 | 全部 docker,含 Caddy HTTPS 网关 |
| 画布助手 | 默认禁用;见下文启用方法 | 按 `.env` 配置路由与凭据 |

## 开发环境

```sh
npm run dev:stack        # 幂等:生成证书/env/compose 覆盖 → 起 12 个容器 → 起 OIDC + API → 授权 dev-user
npm run dev:stack:down   # 停止宿主机进程并下线容器,数据卷保留
```

- 入口 `https://localhost:8443/`;登录经本地 OIDC 自动完成(dev-user,已自动加入开发工作区并授予 admin)。
- 证书优先用 mkcert(系统信任,内嵌浏览器可开):`brew install mkcert && sudo mkcert -install` 一次即可;
  未安装 mkcert 时回退 openssl 自签,此时内嵌浏览器会整页拦截(无"继续前往"),请用 Safari/Chrome 打开。
- 运行产物都在 `data/dev/`(已 gitignore):`tls.*`、`api.env`、`*.pid`、`*.log`、`media/`(与 worker 容器共享)。
- 改后端代码:重跑 `npm run dev:stack`(或自行 `--reload`);改前端:`npm run build:web` 后刷新,开发时也可 `npm run dev` 热更新。
- 生成链路走 mock Provider 完整可验;需要真实模型时换 `AIVERO_PRODUCTION_PROVIDER_ROUTES_FILE` 指向自己的路由文件。

### 启用画布助手(可选,费用自理)

1. 复制 `deploy/agent-routes.example.json` 为 `data/dev/agent-routes.json`,填模型名与 baseUrl;
2. `data/dev/api.env` 取消 `AIVERO_PRODUCTION_AGENT_ROUTES_FILE` 注释;
3. 给 agent-worker 挂凭据:`docker compose -p aivideo-dev ... ` 或在 `data/dev/compose.override.yaml` 增加
   `agent-worker.environment.AIVERO_CREDENTIALS_FILE`(凭据 JSON 形如 `{"deepseek":{"apiKey":"…"}}`);
4. 重跑 `npm run dev:stack`。

## 生产环境

按 `.env.production.example` 逐项填写后复制为 `.env`(数据库/队列密码、HTTPS 域名、OIDC、会话密钥、Provider 路由与凭据文件的**绝对路径**),`npm run stack:up` 启动全套(API、worker、Caddy 网关)。凭据文件绝不入库;Agent 路由默认禁用(`deploy/agent-routes.disabled.json`)。
