from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from backend.core.asset_history import asset_head, compare, version_projection


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
async def test_history_acl_rejects_other_user_and_workspace_without_revealing_asset():
    workspace = uuid4()
    asset = SimpleNamespace(workspace_id=workspace, ownership="personal", user_id="owner")
    tx = SimpleNamespace(get=AsyncMock(return_value=asset))
    with pytest.raises(LookupError, match="asset_history_not_found"):
        await asset_head(tx, asset_id=uuid4(), workspace_id=workspace, user_id="other")
    asset.ownership = "team"
    assert await asset_head(tx, asset_id=uuid4(), workspace_id=workspace, user_id="other") is asset
    with pytest.raises(LookupError):
        await asset_head(tx, asset_id=uuid4(), workspace_id=uuid4(), user_id="owner")


@pytest.mark.anyio
async def test_removed_or_blocked_version_never_returns_content_url():
    workspace = uuid4()
    stored = SimpleNamespace(id=uuid4(), workspace_id=workspace, details={"hostBlocked": True})
    row = SimpleNamespace(id=uuid4(), stored_asset_id=stored.id, workspace_id=workspace,
        status="normal", version=1, kind="image", created_at=datetime.now(timezone.utc),
        run_id=None, task_id=None, previous_version_id=None)
    tx = SimpleNamespace(get=AsyncMock(return_value=stored))
    assert (await version_projection(tx, row))["src"] is None
    stored.details = {}
    assert (await version_projection(tx, row))["src"] == f"/api/assets/{stored.id}/content"
    row.status = "removed"
    assert (await version_projection(tx, row))["src"] is None


@pytest.mark.anyio
async def test_compare_cannot_read_a_version_from_another_asset():
    workspace, asset_id = uuid4(), uuid4()
    asset = SimpleNamespace(workspace_id=workspace, ownership="personal", user_id="owner")
    foreign = SimpleNamespace(asset_id=uuid4(), workspace_id=workspace)
    tx = SimpleNamespace(get=AsyncMock(side_effect=[asset, foreign]))
    with pytest.raises(LookupError, match="asset_version_not_found"):
        await compare(tx, asset_id=asset_id, left=uuid4(), right=uuid4(),
                      workspace_id=workspace, user_id="owner")
