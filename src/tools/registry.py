"""Tool registry + executor. Tool functions live in sibling submodules
(slack.py, search.py, web.py, image.py, time.py) and register themselves
via the @tool decorator on import."""
from __future__ import annotations

import logging
import time
from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError as FuturesTimeout
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
        # max_workers=4 matches the agent's typical fan-out for a single turn
        # (LLMs emit 2–4 parallel tool_calls when the system prompt encourages
        # it — e.g. fetch_thread_history + fetch_user_profile +
        # read_attached_images). Inner tool pools (read_attached_images,
        # read_attached_document) also use 4 as their cap, so this stays
        # consistent across the codebase.
        self._pool = ThreadPoolExecutor(max_workers=4)
        self._closed = False

    def execute(self, call: ToolCall) -> dict[str, Any]:
        # Single-call path delegates to execute_many so timeout + exception
        # handling lives in one place.
        return self.execute_many([call])[0]

    def execute_many(self, calls: list[ToolCall]) -> list[dict[str, Any]]:
        """Run a batch of tool calls concurrently. Returns results in the same
        order as `calls`.

        All known tools are submitted to the worker pool up front, so they
        execute in parallel up to ``max_workers``. Unknown tools short-circuit
        without consuming a worker slot.

        Per-call timeout is enforced from each call's submit time, not from
        when we begin waiting on it — otherwise a slow earlier call could
        silently extend a later call's allotted budget.
        """
        if not calls:
            return []
        prepared: list[tuple[Future | None, ToolDef | None, float, ToolCall]] = []
        for call in calls:
            td = self.registry.get(call.name)
            started = time.monotonic()
            if td is None:
                prepared.append((None, None, started, call))
                continue
            future = self._pool.submit(td.fn, self.context, **(call.arguments or {}))
            prepared.append((future, td, started, call))

        results: list[dict[str, Any]] = []
        for future, td, started, call in prepared:
            if td is None or future is None:
                results.append({"ok": False, "error": f"unknown tool: {call.name}"})
                continue
            effective_timeout = td.timeout if td.timeout is not None else self.timeout
            remaining = max(0.0, started + effective_timeout - time.monotonic())
            try:
                result = future.result(timeout=remaining)
                results.append(
                    {
                        "ok": True,
                        "result": result,
                        "duration_ms": int((time.monotonic() - started) * 1000),
                    }
                )
            except FuturesTimeout:
                logger.warning("tool %s timed out after %.1fs", call.name, effective_timeout)
                results.append(
                    {"ok": False, "error": f"tool '{call.name}' timed out after {effective_timeout}s"}
                )
            except Exception as exc:  # noqa: BLE001
                # Broad catch on purpose: provider SDKs raise their own APIError
                # hierarchies (openai.APIError, anthropic.APIError, httpx.HTTPError)
                # that the agent must treat as recoverable. Returning
                # {"ok": False, ...} hands the failure back to the LLM, which
                # can retry, fall back, or surface a friendly message. A
                # narrower except list lets those propagate and aborts the loop.
                logger.exception("tool %s failed", call.name)
                results.append({"ok": False, "error": f"{exc.__class__.__name__}: {exc}"})
        return results

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
