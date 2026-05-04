"""JSON-RPC error codes (mirrors the daemon's reserved set).

Methods raise these so the host can surface them consistently to MCP
clients (FastMCP turns the exception into a tool-error result).
"""

from __future__ import annotations


class JsonRpcError(Exception):
    code: int = -32603

    def __init__(self, message: str, data: object | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.data = data

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
