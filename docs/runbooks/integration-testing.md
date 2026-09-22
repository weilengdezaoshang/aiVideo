# 集成测试运行手册(tests/integration)

## 覆盖内容

| 文件 | 依赖 | 验证内容 |
| --- | --- | --- |
| test_persistence.py | PostgreSQL | 迁移/漂移/事务原子性/幂等冲突/乐观并发/分页 |
| test_celery_contract.py | PostgreSQL + fork | ADR-0003 执行契约(循环/客户端/fork/协作取消/硬超时) |
| test_delivery_loop.py | PostgreSQL + RabbitMQ | §五 投递闭环(确认发布/无路由/退避/多投递器/死信/幂等) |
| test_lifecycle.py | PostgreSQL | §七/§八 分阶段管线/到期调度/模型名额并发/受理原子性 |
| test_deadletter.py | PostgreSQL | §九 死信列表/重放/终态拒绝/审计 |
| test_job_cache.py | Redis | §十 缓存命中/singleflight 回源/版本竞争/乱序拒绝/降级限流/跨空间拒绝 |

基础设施不可用时相应用例**显式 skip**(输出 skipped 数量),不冒充通过。

## 容器编排(本机开发)

```bash
docker network create aivideo-test
docker run -d --name aivideo-test-pg -e POSTGRES_USER=aivideo \
  -e POSTGRES_PASSWORD=aivideo-test -e POSTGRES_DB=aivideo_test \
  --network aivideo-test postgres:16-alpine
docker run -d --name aivideo-test-rabbit -e RABBITMQ_DEFAULT_USER=aivideo \
  -e RABBITMQ_DEFAULT_PASS=aivideo-test \
  --network aivideo-test rabbitmq:3.13-alpine
docker run -d --name aivideo-test-redis --network aivideo-test \
  redis:7-alpine --maxmemory 64mb --maxmemory-policy allkeys-lru
# runner:python:3.11-slim,挂载仓库,安装依赖(见下)后 sleep infinity
```

> 注意:`docker network connect` 加入网络的容器,若之后被重建,需要重新 connect。

## 执行

宿主直连(端口映射可用时):

```bash
.venv/bin/python -m pytest tests/integration -q
# 默认地址:PG 127.0.0.1:55433 / RabbitMQ 127.0.0.1:5673
```

容器化执行(宿主端口转发不可用时,当前 colima 环境即如此):

```bash
docker exec \
  -e AIVERO_TEST_DB_URL="postgresql+psycopg://aivideo:aivideo-test@aivideo-test-pg:5432/aivideo_test" \
  -e AIVERO_TEST_PG_URL="postgresql://aivideo:aivideo-test@aivideo-test-pg:5432/aivideo_test" \
  -e AIVERO_TEST_BROKER_HOST="aivideo-test-rabbit" \
  -e AIVERO_TEST_BROKER_PORT=5672 \
  -w /work aivideo-test-runner \
  sh -c "PYTHONPATH=apps/api python -m pytest tests/integration -q"
```

runner 依赖安装(离线方式,宿主下载后拷入):

```bash
.venv/bin/python -m pip download -d /tmp/aivideo-wheels --only-binary=:all: \
  --platform manylinux_2_28_aarch64 --python-version 311 \
  pytest "sqlalchemy==2.0.52" "alembic==1.20.0" "psycopg[binary]==3.3.5" \
  "pydantic-settings==2.15.0" "celery==5.6.3" "httpx==0.28.1" "greenlet==3.2.4"
docker cp /tmp/aivideo-wheels aivideo-test-runner:/tmp/wheels
docker exec aivideo-test-runner pip install -q --no-index \
  --find-links=/tmp/wheels <同上包列表>
```

## 环境变量

| 变量 | 用途 | 默认 |
| --- | --- | --- |
| AIVERO_TEST_DB_URL | SQLAlchemy 测试库 URL | 127.0.0.1:55433/aivideo_test |
| AIVERO_TEST_PG_URL | psycopg 直连 URL(可用性探测) | 同上 |
| AIVERO_TEST_BROKER_HOST/PORT | RabbitMQ 探测地址 | 127.0.0.1:5673 |
| AIVERO_TEST_BROKER_URL | kombu 完整 URL | 由 host/port 组合 |
| AIVERO_TEST_REDIS_URL / _HOST / _PORT | Redis 连接与探测 | 127.0.0.1:56379 / 由 host/port 组合 |

## 已知环境限制(2026-09-13 记录)

- 本机 colima 对**新建容器**的宿主端口转发失效(PG 旧容器 55433 仍可用);
  端口转发恢复后可直接宿主执行,无需容器化 runner。
- RabbitMQ 容器重建后需重新 `docker network connect aivideo-test`。
- colima 的重启会影响所有运行中容器(studycommit-minio 等),不要为恢复转发
  擅自重启 colima;先用容器化执行方式。

## 运行指标端点

`GET /api/metrics/runtime` —— 进程 counter/gauge + 数据库采集
(Outbox 待发送数量/最老年龄、任务 queued/running/unknown)。
采集失败自动降级为进程指标;高基数标签(jobId 等)在注册表层被拒绝。

## 开发编排

`compose.dev.yaml`:api/worker/beat + postgres:16-alpine + rabbitmq:3.13-alpine
+ redis:7-alpine(版本固定,健康检查,依赖就绪条件)。
环境变量统一前缀 `AIVERO_*`(DB_URL/BROKER_URL/REDIS_URL)。

```bash
docker compose -f compose.dev.yaml up -d        # 启动开发栈
docker compose -f compose.dev.yaml ps            # 健康状态
curl -s localhost:7801/api/health/ready | jq .   # readiness(库 down=503,broker down=degraded)
curl -s localhost:7801/api/metrics/runtime | jq .gauges
```

周期任务(celery beat,单一调度入口):Outbox 补投递 2s、到期查询扫描 5s、
滞留 publishing 回收 60s。
