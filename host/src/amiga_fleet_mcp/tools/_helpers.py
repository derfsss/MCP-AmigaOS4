"""Shared helpers for tool registration."""

from __future__ import annotations

import functools
from collections.abc import Awaitable, Callable
from typing import Any, ParamSpec, TypeVar

from ..archive import Archive, Timer
from ..errors import InternalError, JsonRpcError

P = ParamSpec("P")
R = TypeVar("R")


def archived(
    tool_name: str,
    archive: Archive,
    *,
    target_arg: str = "target",
) -> Callable[[Callable[P, Awaitable[R]]], Callable[P, Awaitable[R]]]:
    """Decorator that records each tool call (params + result/error +
    wall-clock duration) into the run archive."""

    def deco(fn: Callable[P, Awaitable[R]]) -> Callable[P, Awaitable[R]]:
        @functools.wraps(fn)
        async def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            target = kwargs.get(target_arg)
            params = {k: v for k, v in kwargs.items() if k != target_arg}
            with Timer() as t:
                try:
                    result = await fn(*args, **kwargs)
                except JsonRpcError as e:
                    archive.log_call(
                        tool_name,
                        target if isinstance(target, str) else None,
                        params,
                        error=e.to_dict(),
                        duration_s=t.elapsed,
                    )
                    raise
                except Exception as e:
                    archive.log_call(
                        tool_name,
                        target if isinstance(target, str) else None,
                        params,
                        error={"code": -32603, "message": str(e)},
                        duration_s=t.elapsed,
                    )
                    raise InternalError(str(e)) from e
            archive.log_call(
                tool_name,
                target if isinstance(target, str) else None,
                params,
                result=_serialise_result(result),
                duration_s=t.elapsed,
            )
            return result

        return wrapper

    return deco


def _serialise_result(r: Any) -> Any:
    if hasattr(r, "model_dump"):
        return r.model_dump()
    return r
