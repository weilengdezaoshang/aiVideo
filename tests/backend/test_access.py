"""工作区访问控制(补充要求 §十七.5):可替换接口 + 开发实现 + 显式非生产声明。"""

import uuid

import pytest

from backend.services.access import require_workspace_access, set_access_policy


def test_default_dev_policy_allows_any_identity_but_is_marked_non_production():
    """默认开发策略放行任意身份;实现显式声明不可用作生产认证(§十七.5)。"""
    from backend.services.access import DevAllowPolicy

    assert DevAllowPolicy.production_ready is False
    require_workspace_access(identity="dev", workspace_id=uuid.uuid4())  # 不抛


def test_policy_can_be_replaced_and_denies_cross_workspace():
    """策略可替换(注入测试/生产实现);跨工作空间访问被拒绝(§十七.6)。"""

    class WorkspaceScopedPolicy:
        production_ready = True

        def __init__(self):
            self.allowed = {uuid.uuid4()}

        def check(self, identity, workspace_id):
            from backend.errors import ForbiddenError

            if workspace_id not in self.allowed:
                raise ForbiddenError("无权访问该工作区", details={"workspaceId": str(workspace_id)})

    policy = WorkspaceScopedPolicy()
    set_access_policy(policy)
    try:
        require_workspace_access("user-1", next(iter(policy.allowed)))  # 放行
        with pytest.raises(Exception) as info:
            require_workspace_access("user-1", uuid.uuid4())
        assert getattr(info.value, "code", None).value == "FORBIDDEN"
    finally:
        set_access_policy(None)  # 恢复默认
