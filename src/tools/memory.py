"""User-scoped persistent memory tools.

`remember(key, value)` upserts an entry. `forget(key)` removes one.
Saved entries are auto-injected into the agent's system prompt on the
NEXT turn (handlers/message.py loads memory before constructing the
agent), so the LLM doesn't need a `recall` tool — the values are
already in context.
"""
from __future__ import annotations

import logging

from src import runtime
from src.tools.registry import ToolContext, default_registry, tool

logger = logging.getLogger(__name__)


@tool(
    default_registry,
    name="remember",
    description=(
        "Save a fact about the user across sessions. Use a short snake_case "
        "key (e.g. 'company', 'team', 'pref_response_style'). Existing key "
        "with the same name is overwritten. Saved entries are auto-injected "
        "into your context on future turns — there is NO `recall` tool "
        "because memory is always already visible. Only call this when the "
        "user asks you to remember something or shares stable info "
        "(role, project, preference) that will help future replies. Do NOT "
        "save transient one-shot info or anything sensitive (passwords, "
        "tokens, PII). Value max 1000 chars."
    ),
    parameters={
        "type": "object",
        "properties": {
            "key": {
                "type": "string",
                "description": "Short snake_case identifier, max 64 chars.",
            },
            "value": {
                "type": "string",
                "description": "Free text to remember, max 1000 chars.",
            },
        },
        "required": ["key", "value"],
    },
    timeout=10.0,
)
def remember(ctx: ToolContext, key: str, value: str) -> dict[str, str]:
    if not ctx.user_id:
        # Memory is per-user; without a user_id we have no key.
        raise ValueError(
            "no user context available — memory cannot be saved for "
            "events without a user_id."
        )
    runtime._get_memory().put(ctx.user_id, key, value)
    return {"key": key, "saved": "ok"}


@tool(
    default_registry,
    name="forget",
    description=(
        "Remove a previously saved memory by key. Use this when the user "
        "explicitly asks you to forget something. Returns ok=false if the "
        "key wasn't saved."
    ),
    parameters={
        "type": "object",
        "properties": {
            "key": {
                "type": "string",
                "description": "The key to remove.",
            }
        },
        "required": ["key"],
    },
    timeout=10.0,
)
def forget(ctx: ToolContext, key: str) -> dict[str, str | bool]:
    if not ctx.user_id:
        raise ValueError(
            "no user context available — memory cannot be modified for "
            "events without a user_id."
        )
    removed = runtime._get_memory().delete(ctx.user_id, key)
    if not removed:
        return {"key": key, "removed": False, "note": "no such memory"}
    return {"key": key, "removed": True}
