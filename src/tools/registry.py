"""Tool registry + executor. Tool functions live in sibling submodules
(slack.py, search.py, web.py, image.py, time.py) and register themselves
via the @tool decorator on import."""
from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
from dataclasses import dataclass, field
from typing import Any, Callable

from src.config import Settings
from src.llms import LLMProvider, ToolCall

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Registry
# --------------------------------------------------------------------------- #


@dataclass
class ToolDef:
    name: str
    description: str
    parameters: dict[str, Any]  # JSON Schema
    fn: Callable[..., Any]
    timeout: float | None = None  # None -> use executor default


@dataclass
class ToolRegistry:
    _tools: dict[str, ToolDef] = field(default_factory=dict)

    def register(self, td: ToolDef) -> None:
        self._tools[td.name] = td

    def names(self) -> list[str]:
        return list(self._tools.keys())

    def get(self, name: str) -> ToolDef | None:
        return self._tools.get(name)

    def specs(self) -> list[dict[str, Any]]:
        return [
            {"name": t.name, "description": t.description, "parameters": t.parameters}
            for t in self._tools.values()
        ]


def tool(
    registry: ToolRegistry,
    name: str,
    description: str,
    parameters: dict[str, Any],
    timeout: float | None = None,
):
    def decorator(fn: Callable[..., Any]):
        registry.register(
            ToolDef(name=name, description=description, parameters=parameters, fn=fn, timeout=timeout)
        )
        return fn

    return decorator


# --------------------------------------------------------------------------- #
# Context
# --------------------------------------------------------------------------- #


@dataclass
class ToolContext:
    slack_client: Any
    channel: str
    thread_ts: str
    event: dict[str, Any]
    settings: Settings
    llm: LLMProvider
    # Slack user_id of the person who triggered the request. Used by
    # memory tools (`remember`/`forget`) to scope writes per-user.
    # Empty string when the event has no user (e.g. some bot-authored
    # events) — memory tools refuse to operate without it.
    user_id: str = ""


# --------------------------------------------------------------------------- #
# Executor
# --------------------------------------------------------------------------- #


class ToolExecutor:
    def __init__(self, context: ToolContext, registry: ToolRegistry, timeout: float = 20.0):
        self.context = context
        self.registry = registry
        self.timeout = timeout
        self._pool = ThreadPoolExecutor(max_workers=2)
        self._closed = False

    def execute(self, call: ToolCall) -> dict[str, Any]:
        td = self.registry.get(call.name)
        started = time.monotonic()
        if td is None:
            return {"ok": False, "error": f"unknown tool: {call.name}"}
        effective_timeout = td.timeout if td.timeout is not None else self.timeout
        try:
            future = self._pool.submit(td.fn, self.context, **(call.arguments or {}))
            result = future.result(timeout=effective_timeout)
            return {"ok": True, "result": result, "duration_ms": int((time.monotonic() - started) * 1000)}
        except FuturesTimeout:
            logger.warning("tool %s timed out after %.1fs", call.name, effective_timeout)
            return {"ok": False, "error": f"tool '{call.name}' timed out after {effective_timeout}s"}
        except Exception as exc:  # noqa: BLE001
            # Broad catch on purpose: provider SDKs raise their own APIError
            # hierarchies (openai.APIError, anthropic.APIError, httpx.HTTPError)
            # that were missing from the previous allowlist — and when they
            # escaped the executor the whole agent loop aborted with a generic
            # error instead of handing the failure back to the LLM to recover.
            # The agent already treats {"ok": False, ...} as a recoverable tool
            # result, so swallowing here is correct.
            logger.exception("tool %s failed", call.name)
            return {"ok": False, "error": f"{exc.__class__.__name__}: {exc}"}

    def close(self) -> None:
        """Release the worker pool.

        Called by the owning agent at end-of-request. Safe to call twice.
        Must be invoked in Lambda warm-start environments — otherwise every
        request spawns a fresh ThreadPoolExecutor whose non-daemon workers
        stay in the process-wide registry until interpreter exit.
        """
        if self._closed:
            return
        self._closed = True
        # wait=False so a timed-out tool's worker doesn't pin the Lambda
        # invocation. The stray thread will be cleaned up on GC.
        self._pool.shutdown(wait=False)


# --------------------------------------------------------------------------- #
# Built-in tools
# --------------------------------------------------------------------------- #

default_registry = ToolRegistry()
