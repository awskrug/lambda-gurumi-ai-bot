"""Agent loop using native LLM function calling.

No more JSON-in-prompt tool orchestration: we pass tool specs directly to
the LLM and let the provider emit structured tool_calls. The loop ends
when the LLM stops requesting tools (stop_reason == "end_turn") or we hit
`max_steps`. Duplicate tool calls (same name+args) within the loop are
short-circuited to prevent runaway loops.
"""
from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Callable

from src.llms import LLMProvider, ToolCall
from src.logging_utils import log_event
from src.tools import ToolContext, ToolExecutor, ToolRegistry

logger = logging.getLogger(__name__)


@dataclass
class AgentResult:
    text: str
    image_url: str | None = None
    steps: int = 0
    tool_calls_count: int = 0
    token_usage: dict[str, int] = field(default_factory=dict)


class SlackMentionAgent:
    def __init__(
        self,
        llm: LLMProvider,
        context: ToolContext,
        registry: ToolRegistry,
        max_steps: int,
        tool_executor: ToolExecutor | None = None,
        response_language: str = "ko",
        system_message: str | None = None,
        persona_message: str | None = None,
        history: list[dict[str, Any]] | None = None,
        user_memory: list[dict[str, Any]] | None = None,
        on_stream: Callable[[str], None] | None = None,
        on_step: Callable[[int, str, dict[str, Any]], None] | None = None,
        max_output_tokens: int = 4096,
    ):
        self.llm = llm
        self.context = context
        self.registry = registry
        self.max_steps = max_steps
        self.executor = tool_executor or ToolExecutor(context, registry)
        # Only close the executor we created ourselves — an injected one is
        # owned by the caller (e.g. a test or a shared long-lived harness).
        self._owns_executor = tool_executor is None
        self.response_language = response_language
        self.system_message = system_message
        self.persona_message = persona_message
        self.history = history or []
        # user_memory is `[{key, value, ts}, ...]` from MemoryStore.get.
        # Surfaced in the system prompt so the LLM has it on every turn
        # without spending a tool_call to fetch.
        self.user_memory = user_memory or []
        self.on_stream = on_stream
        # on_step(step_num, phase, detail) — phases: "tool_use", "tool_result", "compose"
        self.on_step = on_step
        self.max_output_tokens = max_output_tokens

    def run(self, user_message: str) -> AgentResult:
        try:
            return self._run(user_message)
        finally:
            if self._owns_executor:
                # Release the ThreadPoolExecutor so Lambda warm containers
                # don't accumulate idle workers across requests.
                self.executor.close()

    def _run(self, user_message: str) -> AgentResult:
        system = self._build_system_prompt()
        messages: list[dict[str, Any]] = [*self.history, {"role": "user", "content": user_message}]
        seen_calls: set[str] = set()
        image_url: str | None = None
        total_usage = {"input": 0, "output": 0}
        tool_calls_total = 0
        steps = 0

        for step in range(self.max_steps):
            steps = step + 1
            # Pass on_stream as on_delta so the provider can stream content
            # tokens live while still returning accumulated tool_calls. When
            # the model starts a tool_call the provider stops forwarding
            # content to avoid leaking the pre-tool preamble into the reply.
            result = self.llm.chat(
                system,
                messages,
                tools=self.registry.specs(),
                max_tokens=self.max_output_tokens,
                on_delta=self.on_stream,
            )
            total_usage["input"] += result.token_usage.get("input", 0)
            total_usage["output"] += result.token_usage.get("output", 0)
            log_event(logger, "llm.hop", step=steps, stop=result.stop_reason, tool_calls=len(result.tool_calls))

            if not result.tool_calls:
                self._notify_step(steps, "compose", {})
                # If on_stream was provided, the content was already streamed
                # during this hop — don't pay for a second LLM call to re-do it.
                return AgentResult(
                    text=(result.content or "").strip(),
                    image_url=image_url,
                    steps=steps,
                    tool_calls_count=tool_calls_total,
                    token_usage=total_usage,
                )

            tool_names = [c.name for c in result.tool_calls]
            self._notify_step(steps, "tool_use", {"tools": tool_names})

            messages.append(
                {
                    "role": "assistant",
                    "content": result.content or "",
                    "tool_calls": [
                        {"id": c.id, "name": c.name, "arguments": c.arguments} for c in result.tool_calls
                    ],
                }
            )

            for call in result.tool_calls:
                tool_calls_total += 1
                signature = self._call_signature(call)
                if signature in seen_calls:
                    tool_result = {"ok": False, "error": "duplicate call skipped"}
                else:
                    seen_calls.add(signature)
                    tool_result = self.executor.execute(call)
                log_event(
                    logger,
                    "tool.result",
                    step=steps,
                    tool=call.name,
                    ok=tool_result.get("ok", False),
                    duration_ms=tool_result.get("duration_ms", 0),
                )

                self._notify_step(
                    steps,
                    "tool_result",
                    {"tool": call.name, "ok": bool(tool_result.get("ok")), "error": tool_result.get("error")},
                )

                if call.name in ("generate_image", "edit_image") and tool_result.get("ok"):
                    permalink = (tool_result.get("result") or {}).get("permalink")
                    if permalink:
                        image_url = permalink

                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.id,
                        "content": json.dumps(tool_result, ensure_ascii=False),
                    }
                )

        # max_steps reached — force one final compose without tools.
        self._notify_step(steps, "compose", {"max_steps_hit": True})
        final_text, final_usage = self._compose_without_tools(system, messages)
        total_usage["input"] += final_usage.get("input", 0)
        total_usage["output"] += final_usage.get("output", 0)
        return AgentResult(
            text=final_text,
            image_url=image_url,
            steps=steps,
            tool_calls_count=tool_calls_total,
            token_usage=total_usage,
        )

    # ------------------------------------------------------------------ #

    def _build_system_prompt(self) -> str:
        """Assemble the system prompt from three layers:

        1. Task rules (owned by code, always present) — how to plan, call
           tools, render Slack replies, look up attachments.
        2. Operator policy (`SYSTEM_MESSAGE`, optional) — extra organization
           or channel-specific policy appended on top of task rules.
        3. Persona (`PERSONA_MESSAGE`, optional) — answer style / tone.

        The language directive is re-emphasized at the very end so the
        model does not drift even if persona is in a different language.
        """
        # Layer 1 — task rules. Fixed in code so operators cannot accidentally
        # delete the loop's planning / parallel-call / tool guidance by
        # overriding SYSTEM_MESSAGE.
        task_rules = (
            "You are an assistant for Slack mention requests. Plan work, "
            "call tools when needed, and provide concise helpful answers. "
            "When multiple independent tools are required, emit their "
            "tool_calls in parallel within a single turn instead of running "
            "them one-by-one. If a tool returns `ok:false`, tell the user "
            "briefly what failed (one short line) and, when it makes sense, "
            "suggest an alternative — do not retry blindly with the same "
            "arguments and do not fabricate a result.\n"
            "When you decide to use tools in a turn, emit ONLY the "
            "tool_calls — do not also output any text in that same turn. "
            "Streaming a 'let me check…' preamble before tool_calls leaks "
            "to the user and renders as duplicate output once the real "
            "answer arrives. Save user-facing text for the turn AFTER "
            "tool results return."
        )
        # Slack rendering rules. The streaming fallback path posts via plain
        # chat.postMessage / chat.update with a `text` field, which Slack
        # renders as mrkdwn — NOT GitHub markdown. Guide the model so replies
        # don't surface raw `**bold**` or `[text](url)` strings.
        slack_rules = (
            "When you call `generate_image` or `edit_image`, the resulting "
            "image is already uploaded inline into the Slack thread. Do NOT "
            "repeat the image URL or permalink in your text reply — just "
            "describe or caption the image briefly. The user sees the picture "
            "attached directly; a URL line is duplicate noise.\n"
            "Pick the right tool: `generate_image` for fresh prompts with no "
            "source image; `edit_image` whenever the user wants to transform, "
            "restyle, or modify an existing image (theirs or one earlier in "
            "the thread). For edits to a thread image, call "
            "`fetch_thread_history` first to obtain `url_private_download`, "
            "then pass it to `edit_image(prompt=..., urls=[...])`.\n"
            "Slack renders mrkdwn, not GitHub markdown. Use `*bold*` with "
            "single asterisks, `_italic_`, `` `code` ``, and "
            "`<https://url|label>` for links. Do NOT use `**bold**` or "
            "`[label](url)` — those appear as raw text in Slack."
        )
        attachment_rules = (
            "If the user asks about an image or document in the current "
            "thread, call `read_attached_images` / `read_attached_document` "
            "first — they target files attached to the triggering message. "
            "If the result is an empty list, the attachment lives on an "
            "earlier message: call `fetch_thread_history`, take the "
            "`url_private_download` values from that message's `files`, "
            "then call `read_attached_images(urls=[...])` or "
            "`read_attached_document(urls=[...])` with them. When the user's "
            "reference to a file is ambiguous (e.g. '이 사진', '아까 그 "
            "파일'), call `read_attached_images` (or "
            "`read_attached_document`) speculatively first — it returns an "
            "empty list cheaply when no file is attached to the current "
            "message, and that signals you to fall back to "
            "`fetch_thread_history`. Never guess or fabricate file URLs."
        )
        sections = [task_rules, slack_rules, attachment_rules]

        # Layer 2 — operator policy (optional, append).
        if self.system_message:
            sections.append(f"Additional policy:\n{self.system_message}")

        # Layer 3 — persona / answer style (optional, append).
        if self.persona_message:
            sections.append(f"Response style:\n{self.persona_message}")

        # Layer 4 — user memory (optional). Auto-injected so the LLM
        # never spends a `recall` round-trip; that's why no recall tool
        # is registered. New writes via `remember`/`forget` take effect
        # on the NEXT turn (memory is loaded once at agent
        # construction in handlers.message).
        if self.user_memory:
            lines = [f"- {e['key']}: {e['value']}" for e in self.user_memory]
            sections.append(
                "User memory (saved across sessions, scoped to this user):\n"
                + "\n".join(lines)
            )

        sections.append(f"Respond in language: {self.response_language}.")
        return "\n\n".join(sections)

    @staticmethod
    def _call_signature(call: ToolCall) -> str:
        args_blob = json.dumps(call.arguments or {}, sort_keys=True, ensure_ascii=False)
        return f"{call.name}:{hashlib.sha1(args_blob.encode()).hexdigest()[:12]}"

    def _compose_without_tools(
        self, system: str, messages: list[dict[str, Any]]
    ) -> tuple[str, dict[str, int]]:
        """Force a final answer when max_steps is reached — no tools permitted.

        Returns (text, token_usage) so the caller can fold this hop's
        cost into `agent.done` accounting. Routes through `chat` (with
        `on_delta` when streaming) so a single code path delivers both
        the streaming UX and the usage numbers.
        """
        directive = (
            "Provide the final answer now. Do not request any more tools; summarize based on prior observations."
        )
        messages = [*messages, {"role": "user", "content": directive}]
        result = self.llm.chat(
            system,
            messages,
            tools=None,
            max_tokens=self.max_output_tokens,
            on_delta=self.on_stream,
        )
        return (result.content or "").strip(), result.token_usage or {}

    def _notify_step(self, step: int, phase: str, detail: dict[str, Any]) -> None:
        if not self.on_step:
            return
        try:
            self.on_step(step, phase, detail)
        except Exception as exc:  # noqa: BLE001
            logger.debug("on_step callback failed: %s", exc)
