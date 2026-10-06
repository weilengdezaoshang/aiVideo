"""Canvas Agent tools. Handlers live in canvas_read; ``execute_envelope`` is the gateway entry."""
from backend.agent.tools.canvas_read import READ_TOOLS, project_snapshot
from backend.agent.tools.control import CONTROL_NAMES, CONTROL_TOOLS
from backend.agent.tools.errors import ToolError
from backend.agent.tools.registry import ToolGateway

GATEWAY = ToolGateway([*READ_TOOLS, *CONTROL_TOOLS])
tool_schemas = GATEWAY.schemas
execute_envelope = GATEWAY.execute

__all__ = ["CONTROL_NAMES", "GATEWAY", "ToolError", "execute_envelope", "project_snapshot", "tool_schemas"]
