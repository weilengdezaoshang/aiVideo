"""本地开发环境一键编排:证书、API 配置、基础设施容器与宿主机进程。

开发环境(npm run dev:stack):docker 跑 PostgreSQL/RabbitMQ/Redis 与全部 worker(当前代码构建),
宿主机跑 TLS API(8443,服务已构建前端)与本地 OIDC fixture(8444),Provider 固定 mock。
生产环境(npm run stack:up + .env.production.example)不经过本脚本。

用法:
  python3 scripts/dev_stack.py up     # 幂等启动:缺什么补什么,已在跑的跳过
  python3 scripts/dev_stack.py down   # 停止宿主机进程并下线容器(保留数据卷)
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
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
    run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
         "-keyout", str(RUNTIME / "tls.key"), "-out", str(RUNTIME / "tls.pem"),
         "-days", "825", "-subj", "/CN=localhost",
         "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1"])
    print("已生成自签证书 data/dev/tls.pem(仅本机开发用)")


def ensure_api_env() -> None:
    target = RUNTIME / "api.env"
    if target.exists():
        return
    text = (ROOT / "deploy/dev/api.env.example").read_text(encoding="utf-8")
    target.write_text(text.replace("<ROOT>", str(ROOT)), encoding="utf-8")
    print("已生成 data/dev/api.env,可按需编辑")


def ensure_compose_files() -> None:
    (RUNTIME / "compose.env").write_text(COMPOSE_ENV, encoding="utf-8")
    (RUNTIME / "compose.override.yaml").write_text(f"""# 由 scripts/dev_stack.py 生成;修改端口/绑定请改脚本后重跑。
services:
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
            print(f"已停止 {name}(pid {pid})")
        pidfile.unlink(missing_ok=True)


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
    ensure_api_env()
    ensure_compose_files()
    try:
        compose("up", "-d", "--build")
    except subprocess.CalledProcessError:
        # 镜像仓库不可达(如系统代理)时,回退到现有本地镜像;首次使用仍需能拉取基础镜像。
        print("镜像构建失败(可能无法访问镜像仓库),改用现有本地镜像启动")
        compose("up", "-d")
    if not (ROOT / ".web-build" / "workspace" / "app.js").exists():
        print("首次构建前端…")
        run(["npm", "run", "build:web"])
    env = load_env()
    tls = ["--ssl-keyfile", str(RUNTIME / "tls.key"),
           "--ssl-certfile", str(RUNTIME / "tls.pem")]
    spawn("oidc", [str(PYTHON), "-m", "uvicorn", "oidc_fixture:app",
                   "--app-dir", str(ROOT / "deploy/dev"), "--host", "127.0.0.1",
                   "--port", OIDC_PORT, "--log-level", "warning", *tls])
    spawn("api", [str(PYTHON), "-m", "uvicorn", "backend.app:create_app", "--factory",
                  "--host", "127.0.0.1", "--port", API_PORT, "--log-level", "info", *tls], env)
    if wait_healthy():
        print(f"\n开发环境就绪: https://localhost:{API_PORT}/  (OIDC: https://localhost:{OIDC_PORT})")
        print("自签证书首次访问需在浏览器手动放行;登录经本地 OIDC 自动完成。")
        print("改后端代码后重跑 dev:stack 即可;改前端跑 npm run build:web 或用 npm run dev 热更新。")
    else:
        print("API 未在 30 秒内就绪,查看 data/dev/api.log 与 docker 日志排查。")
        sys.exit(1)


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
