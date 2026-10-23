"""JSON-RPC error codes (mirrors the daemon's reserved set).

Methods raise these so the host can surface them consistently to MCP
clients. They are mcp ToolErrors (not MCPError), so MCPServer turns
them into a tool-error result (isError) carrying their text, rather
than a protocol-level JSON-RPC error.
"""

from __future__ import annotations

import json

from mcp.server.mcpserver.exceptions import ToolError


class JsonRpcError(ToolError):
    """Base for every error a tool raises on purpose.

    It subclasses mcp's ToolError because MCPServer only shows the model
    the text of a ToolError: any other exception is reported as a bare
    "Error executing tool <name>", which would hide that a command is
    still running, or which path was not found. The model sees only
    str(), so str() carries `data` too (a job_id, may_have_run, ...).
    """

    code: int = -32603

    def __init__(self, message: str, data: object | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.data = data

    def __str__(self) -> str:
        if self.data is None:
            return self.message
        return f"{self.message} [data: {json.dumps(self.data, default=str)}]"

    def to_dict(self) -> dict[str, object]:
        d: dict[str, object] = {"code": self.code, "message": self.message}
        if self.data is not None:
            d["data"] = self.data
        return d


class ParseError(JsonRpcError):
    code = -32700


class InvalidRequest(JsonRpcError):
    code = -32600


class MethodNotFound(JsonRpcError):
    code = -32601


class InvalidParams(JsonRpcError):
    code = -32602


class InternalError(JsonRpcError):
    code = -32603


class TargetError(JsonRpcError):
    """-32001: target operation failed (FS not found, perms, ...)."""

    code = -32001


class Cancelled(JsonRpcError):
    code = -32002


class NotCapable(JsonRpcError):
    """-32003: target/channel doesn't support this method."""

    code = -32003


class AuthRequired(JsonRpcError):
    code = -32004


class Busy(JsonRpcError):
    code = -32005
