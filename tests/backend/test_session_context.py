from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from backend.core.session_context import readable_asset, reference_context


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
async def test_reference_ambiguity_never_guesses_a_target_or_queries_foreign_assets():
    tx = SimpleNamespace(get=AsyncMock(), scalar=AsyncMock())
    session = SimpleNamespace(id=uuid4())
    request, result = await reference_context(tx, session, {"message": "左边那个还是右边那个"})
    assert result["status"] == "ambiguous"
    assert "selected_assets" not in request
    tx.get.assert_not_called()


@pytest.mark.anyio
async def test_selected_upload_is_resolved_without_an_artifact_index():
    ident, workspace = uuid4(), uuid4()
    asset = SimpleNamespace(id=ident, workspace_id=workspace, details={})
    tx = SimpleNamespace(get=AsyncMock(return_value=asset), scalar=AsyncMock(side_effect=[None, uuid4()]))
    session = SimpleNamespace(id=uuid4(), workspace_id=workspace, user_id="owner", canvas_id=uuid4())
    request, result = await reference_context(tx, session,
        {"message": "编辑选中的图片", "selected_assets": [str(ident)]})
    assert request["selected_assets"] == [str(ident)]
    assert result["status"] == "resolved"
    assert result["asset_id"] == str(ident)


@pytest.mark.anyio
async def test_private_asset_in_same_workspace_is_not_readable_by_another_user():
    ident, workspace = uuid4(), uuid4()
    asset = SimpleNamespace(id=ident, workspace_id=workspace, details={})
    version = SimpleNamespace(asset_id=uuid4(), workspace_id=workspace, status="normal")
    head = SimpleNamespace(workspace_id=workspace, ownership="personal", user_id="someone-else")
    tx = SimpleNamespace(get=AsyncMock(side_effect=[asset, head]), scalar=AsyncMock(return_value=version))
    session = SimpleNamespace(workspace_id=workspace, user_id="owner")
    with pytest.raises(ValueError, match="asset_unavailable"):
        await readable_asset(tx, session, str(ident))
