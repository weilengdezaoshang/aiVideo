"""本地开发环境一键编排:证书、API 配置、基础设施容器与宿主机进程。

开发环境(npm run dev:stack):docker 跑 PostgreSQL/RabbitMQ/Redis 与全部 worker(当前代码构建),
宿主机跑 TLS API(8443,服务已构建前端)与本地 OIDC fixture(8444),Provider 固定 mock。
生产环境(npm run stack:up + .env.production.example)不经过本脚本。

用法:
  python3 scripts/dev_stack.py up     # 幂等启动:构建当前代码并刷新 API/OIDC，保留开发数据
  python3 scripts/dev_stack.py down   # 停止宿主机进程并下线容器(保留数据卷)
"""
from __future__ import annotations

import os
import json
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RUNTIME = ROOT / "data" / "dev"
PYTHON = ROOT / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
PROJECT = "aivideo-dev"
DB_PORT, REDIS_PORT, MQ_PORT = "55437", "6387", "5677"
API_PORT, OIDC_PORT = "8443", "8444"
COMPOSE_ENV = """AIVERO_DB_PASSWORD=aivideo_dev
AIVERO_MQ_PASSWORD=aivideo_dev
"""


def run(command: list[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(command, cwd=ROOT, check=True, **kwargs)


def ensure_certificates() -> None:
    if (RUNTIME / "tls.pem").exists():
        return
    # 优先 mkcert:证书被系统信任,内嵌浏览器(无"继续前往"按钮)也能打开。
    mkcert = shutil.which("mkcert")
    if mkcert and Path.home().joinpath("Library/Application Support/mkcert/rootCA.pem").exists():
        run([mkcert, "-cert-file", str(RUNTIME / "tls.pem"), "-key-file", str(RUNTIME / "tls.key"),
             "localhost", "127.0.0.1"])
        print("已用 mkcert 生成受信证书 data/dev/tls.pem")
        return
    run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
         "-keyout", str(RUNTIME / "tls.key"), "-out", str(RUNTIME / "tls.pem"),
         "-days", "825", "-subj", "/CN=localhost",
         "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1"])
    print("已生成自签证书 data/dev/tls.pem(仅本机开发用)")
    if not mkcert:
        print("提示:安装并信任 mkcert(brew install mkcert && sudo mkcert -install)后,"
              "删除 data/dev/tls.* 重跑可生成受信证书,内嵌浏览器即可打开。")


def ensure_host_catalog() -> None:
    """A usable development Host with no paid route and no production catalog edits."""
    target = RUNTIME / "host-catalog.json"
    if target.exists():
        config = json.loads(target.read_text(encoding="utf-8"))
    else:
        config = json.loads((ROOT / "deploy/host-catalog.development.json").read_text(encoding="utf-8"))
        config["enabled"] = True
        for capability in config["capabilities"]:
            capability["enabled"] = bool(capability.get("binding"))
    if config.get("environment") != "development" or any(
        cap.get("enabled") and cap.get("binding", {}).get("engine", "provider") != "frames"
        and cap.get("binding", {}).get("providerSnapshot", {}).get("provider") != "mock"
        for cap in config.get("capabilities", [])
    ):
        sys.exit("dev:stack 仅允许 development Mock Host；真实模型请使用显式部署环境")
    if not target.exists():
        target.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def ensure_api_env() -> None:
    target = RUNTIME / "api.env"
    if target.exists():
        text = target.read_text(encoding="utf-8")
    else:
        text = (ROOT / "deploy/dev/api.env.example").read_text(encoding="utf-8").replace("<ROOT>", str(ROOT))
    key = "AIVERO_PRODUCTION_HOST_CATALOG_FILE"
    previous = str(ROOT / "deploy/host-catalog.development.json")
    lines = text.splitlines()
    configured = next((line.split("=", 1)[1].strip() for line in lines if line.startswith(key + "=")), None)
    if configured not in (None, previous, str(RUNTIME / "host-catalog.json")):
        sys.exit("dev:stack Host 指向自定义目录；请先确认它是独立 Mock 配置，未覆盖已有配置")
    lines = [line for line in lines if not line.startswith(key + "=")]
    lines.append(f"{key}={RUNTIME / 'host-catalog.json'}")
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")


def ensure_compose_files() -> None:
    (RUNTIME / "compose.env").write_text(COMPOSE_ENV, encoding="utf-8")
    (RUNTIME / "compose.override.yaml").write_text(f"""# 由 scripts/dev_stack.py 生成;修改端口/绑定请改脚本后重跑。
services:
  agent-worker:
    environment:
      AIVERO_RUNTIME_ENVIRONMENT: development
      AIVERO_PRODUCTION_HOST_CATALOG_FILE: /run/config/host-catalog.json
    volumes:
      - {RUNTIME / "host-catalog.json"}:/run/config/host-catalog.json:ro
  image-worker:
    environment:
      AIVERO_RUNTIME_ENVIRONMENT: development
  video-worker:
    environment:
      AIVERO_RUNTIME_ENVIRONMENT: development
  postgres:
    ports: ["127.0.0.1:{DB_PORT}:5432"]
  rabbitmq:
    ports: ["127.0.0.1:{MQ_PORT}:5672"]
  redis:
    ports: ["127.0.0.1:{REDIS_PORT}:6379"]
volumes:
  media:
    driver: local
    driver_opts:
      type: none
      o: bind
      device: {RUNTIME / "media"}
""", encoding="utf-8")


def compose(*arguments: str) -> subprocess.CompletedProcess:
    return run(["docker", "compose", "-p", PROJECT,
                "--env-file", str(RUNTIME / "compose.env"),
                "-f", "compose.execution.yaml",
                "-f", str(RUNTIME / "compose.override.yaml"), *arguments])


def load_env() -> dict[str, str]:
    values = dict(os.environ)
    for line in (RUNTIME / "api.env").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip()
    values["AIVERO_RUNTIME_ENVIRONMENT"] = "development"
    values["AIVERO_PRODUCTION_RUNTIME_ENVIRONMENT"] = "development"
    values["PYTHONPATH"] = str(ROOT / "apps/api") + os.pathsep + str(ROOT)
    return values


def alive(pid: str | None) -> bool:
    if not pid or not pid.isdigit():
        return False
    try:
        os.kill(int(pid), 0)
        return True
    except OSError:
        return False


def spawn(name: str, command: list[str], env: dict[str, str] | None = None) -> None:
    pidfile = RUNTIME / f"{name}.pid"
    if alive(pidfile.read_text().strip() if pidfile.exists() else None):
        print(f"{name} 已在运行(pid {pidfile.read_text().strip()}),跳过")
        return
    log = (RUNTIME / f"{name}.log").open("w")
    process = subprocess.Popen(command, cwd=ROOT, env=env or os.environ,
                               stdout=log, stderr=log, start_new_session=True)
    pidfile.write_text(str(process.pid))
    print(f"{name} 已启动(pid {process.pid},日志 data/dev/{name}.log)")


def stop_children() -> None:
    for name in ("api", "oidc"):
        pidfile = RUNTIME / f"{name}.pid"
        pid = pidfile.read_text().strip() if pidfile.exists() else ""
        if alive(pid):
            os.kill(int(pid), 15)
            deadline = time.monotonic() + 10
            while alive(pid) and time.monotonic() < deadline:
                time.sleep(0.1)
            if alive(pid):
                sys.exit(f"{name} 尚未退出，请等待当前请求结束后重试；未强制终止")
            print(f"已停止 {name}(pid {pid})")
        pidfile.unlink(missing_ok=True)


def ensure_workspace_member() -> None:
    """把本地 OIDC 的 dev-user 加入固定开发工作区(幂等);应用层不做登录旁路。"""
    subprocess.run(
        [str(PYTHON), "-m", "backend.tools.production_admin", "grant-member",
         "--workspace", str(uuid.uuid5(uuid.NAMESPACE_URL, "https://localhost:8443/#dev")),
         "--issuer", f"https://localhost:{OIDC_PORT}", "--subject", "dev-user",
         "--role", "admin", "--actor", "dev_stack"],
        cwd=ROOT, env=load_env(), check=True, capture_output=True)
    run([str(PYTHON), "-c", """
import asyncio, uuid
from backend.infrastructure.database import create_async_database_engine, create_async_session_factory
from backend.agent.media.credits import grant
async def main():
    engine = create_async_database_engine()
    try:
        async with create_async_session_factory(engine).begin() as db:
            await grant(db, uuid.uuid5(uuid.NAMESPACE_URL, 'https://localhost:8443/#dev'),
                'dev-stack-mock-initial-v1', 1000, 'dev_stack_mock')
    finally:
        await engine.dispose()
asyncio.run(main())
"""], env=load_env(), capture_output=True)
    print("dev-user 已加入开发工作区(admin)，Mock 测试积分已初始化(幂等)")


def wait_healthy() -> bool:
    import urllib.request
    import ssl
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    deadline = time.time() + 30
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"https://localhost:{API_PORT}/api/health",
                                        timeout=2, context=context) as response:
                if response.status == 200:
                    return True
        except OSError:
            pass
        time.sleep(0.5)
    return False


def up() -> None:
    RUNTIME.mkdir(parents=True, exist_ok=True)
    (RUNTIME / "media").mkdir(exist_ok=True)
    if not PYTHON.exists():
        sys.exit("缺少 .venv,请先: python3 -m venv .venv && .venv/bin/python -m pip install -r requirements-dev.txt")
    ensure_certificates()
    ensure_host_catalog()
    ensure_api_env()
    ensure_compose_files()
    # A failed build must not silently start a stale image with another schema.
    compose("build", "migrate")
    compose("up", "-d", "--no-build")
    print("构建当前主干前端…")
    run(["npm", "run", "build:web"])
    stop_children()
    env = load_env()
    tls = ["--ssl-keyfile", str(RUNTIME / "tls.key"),
           "--ssl-certfile", str(RUNTIME / "tls.pem")]
    spawn("oidc", [str(PYTHON), "-m", "uvicorn", "oidc_fixture:app",
                   "--app-dir", str(ROOT / "deploy/dev"), "--host", "127.0.0.1",
                   "--port", OIDC_PORT, "--log-level", "warning", *tls])
    spawn("api", [str(PYTHON), "-m", "uvicorn", "backend.app:create_app", "--factory",
                  "--host", "127.0.0.1", "--port", API_PORT, "--log-level", "info", *tls], env)
    if not wait_healthy():
        print("API 未在 30 秒内就绪,查看 data/dev/api.log 与 docker 日志排查。")
        sys.exit(1)
    ensure_workspace_member()
    print(f"\n开发环境就绪: https://localhost:{API_PORT}/  (OIDC: https://localhost:{OIDC_PORT})")


def down() -> None:
    stop_children()
    compose("down")
    print("开发环境已停止(数据卷保留,重跑 up 数据不丢)。")


if __name__ == "__main__":
    action = sys.argv[1] if len(sys.argv) > 1 else ""
    if action == "up":
        up()
    elif action == "down":
        down()
    else:
        sys.exit(__doc__)
