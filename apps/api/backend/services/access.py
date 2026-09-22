"""工作区访问控制(补充要求 §十七):可替换策略接口。

- 当前无身份提供方凭据,默认策略仅为开发放行实现,
  显式声明 production_ready=False,不得作为生产认证(§十七.5);
- 策略可替换:接入真实身份体系时实现 check() 并 set_access_policy();
- 文档、任务、资产、SSE 等入口统一经 require_workspace_access 校验。
"""

import threading



class DevAllowPolicy:
    """开发环境放行策略;明确非生产(§十七.5)。"""

    production_ready = False

    def check(self, identity, workspace_id) -> None:
        return None


_policy: DevAllowPolicy | None = None
_policy_lock = threading.Lock()


def set_access_policy(policy) -> None:
    """替换访问策略;None 恢复默认开发策略(测试与部署装配用)。"""
    global _policy
    with _policy_lock:
        _policy = policy


def require_workspace_access(identity, workspace_id) -> None:
    """校验 identity 对 workspace 的访问权;拒绝抛 ForbiddenError(403/FORBIDDEN)。"""
    with _policy_lock:
        policy = _policy or DevAllowPolicy()
    policy.check(identity, workspace_id)
