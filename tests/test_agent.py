from unittest.mock import MagicMock

from src.agent import SlackMentionAgent
from src.llms import LLMResult, ToolCall
from src.tools import ToolContext, ToolRegistry, tool


def _ctx():
    return ToolContext(
        slack_client=MagicMock(),
        channel="C1",
        thread_ts="ts1",
        event={},
        settings=MagicMock(),
        llm=MagicMock(),
    )


class ScriptedLLM:
    def __init__(self, results: list[LLMResult]):
        self._results = list(results)
        self.calls: list[dict] = []

    def chat(self, system, messages, tools=None, max_tokens=1024, on_delta=None):
        self.calls.append({"messages": list(messages), "tools": tools, "on_delta": on_delta})
        if not self._results:
            result = LLMResult(content="(empty)", tool_calls=[], stop_reason="end_turn")
        else:
            result = self._results.pop(0)
        if on_delta is not None and result.content and not result.tool_calls:
            on_delta(result.content)
        return result

    def stream_chat(self, system, messages, on_delta, max_tokens=1024):
        result = self.chat(system, messages)
        if on_delta and result.content:
            on_delta(result.content)
        return result.content

    def describe_image(self, b, m):
        return "desc"

    def generate_image(self, p):
        return b"img"


def _registry_with_search():
    reg = ToolRegistry()

    @tool(reg, name="search_web", description="", parameters={"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]})
    def _search(ctx, query):
        return [{"title": "AWS", "url": "https://aws.amazon.com"}]

    return reg


def test_agent_terminates_when_no_tool_calls():
    reg = _registry_with_search()
    llm = ScriptedLLM([LLMResult(content="final", tool_calls=[], stop_reason="end_turn")])
    agent = SlackMentionAgent(llm=llm, context=_ctx(), registry=reg, max_steps=3)
    result = agent.run("question")
    assert result.text == "final"
    assert result.steps == 1
    assert result.tool_calls_count == 0


def test_agent_runs_tool_then_returns_text():
    reg = _registry_with_search()
    llm = ScriptedLLM(
        [
            LLMResult(
                content="",
                tool_calls=[ToolCall(id="c1", name="search_web", arguments={"query": "aws"})],
                stop_reason="tool_use",
            ),
            LLMResult(content="결과 기반 답변", tool_calls=[], stop_reason="end_turn"),
        ]
    )
    agent = SlackMentionAgent(llm=llm, context=_ctx(), registry=reg, max_steps=3)
    result = agent.run("질문")
    assert result.text == "결과 기반 답변"
    assert result.tool_calls_count == 1
    assert result.steps == 2


def test_agent_duplicate_call_is_skipped():
    reg = _registry_with_search()
    called = {"count": 0}

    reg = ToolRegistry()

    @tool(reg, name="search_web", description="", parameters={"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]})
    def _search(ctx, query):
        called["count"] += 1
        return [{"title": "X"}]

    # LLM calls the same tool twice with identical args, then finishes.
    llm = ScriptedLLM(
        [
            LLMResult(
                content="",
                tool_calls=[ToolCall(id="c1", name="search_web", arguments={"query": "same"})],
                stop_reason="tool_use",
            ),
            LLMResult(
                content="",
                tool_calls=[ToolCall(id="c2", name="search_web", arguments={"query": "same"})],
                stop_reason="tool_use",
            ),
            LLMResult(content="done", tool_calls=[], stop_reason="end_turn"),
        ]
    )
    agent = SlackMentionAgent(llm=llm, context=_ctx(), registry=reg, max_steps=5)
    agent.run("q")
    assert called["count"] == 1  # second call suppressed


def test_agent_captures_image_url():
    reg = ToolRegistry()

    @tool(reg, name="generate_image", description="", parameters={"type": "object", "properties": {"prompt": {"type": "string"}}, "required": ["prompt"]})
    def _gen(ctx, prompt):
        return {"permalink": "https://slack/x"}

    llm = ScriptedLLM(
        [
            LLMResult(
                content="",
                tool_calls=[ToolCall(id="c1", name="generate_image", arguments={"prompt": "cat"})],
                stop_reason="tool_use",
            ),
            LLMResult(content="here is your image", tool_calls=[], stop_reason="end_turn"),
        ]
    )
    agent = SlackMentionAgent(llm=llm, context=_ctx(), registry=reg, max_steps=3)
    result = agent.run("그려줘")
    assert result.image_url == "https://slack/x"


def test_agent_captures_image_url_from_edit_image():
    """edit_image must be tracked the same way as generate_image — both leave
    a file inline in the thread, so the image_url field on AgentResult should
    point at it regardless of which tool produced it."""
    reg = ToolRegistry()

    @tool(reg, name="edit_image", description="", parameters={"type": "object", "properties": {"prompt": {"type": "string"}}, "required": ["prompt"]})
    def _edit(ctx, prompt):
        return {"permalink": "https://slack/edited"}

    llm = ScriptedLLM(
        [
            LLMResult(
                content="",
                tool_calls=[ToolCall(id="c1", name="edit_image", arguments={"prompt": "sketch"})],
                stop_reason="tool_use",
            ),
            LLMResult(content="여기 편집했어요", tool_calls=[], stop_reason="end_turn"),
        ]
    )
    agent = SlackMentionAgent(llm=llm, context=_ctx(), registry=reg, max_steps=3)
    result = agent.run("스케치 스타일로")
    assert result.image_url == "https://slack/edited"


def test_agent_forces_final_compose_at_max_steps():
    reg = _registry_with_search()
    # Every step returns more tool calls — never ends.
    def infinite():
        while True:
            yield LLMResult(
                content="",
                tool_calls=[ToolCall(id="x", name="search_web", arguments={"query": "q"})],
                stop_reason="tool_use",
            )

    class EndlessLLM:
        def __init__(self):
            self._gen = infinite()
            self.end_called = False

        def chat(self, *a, **k):
            return next(self._gen)

        def stream_chat(self, system, messages, on_delta, max_tokens=1024):
            self.end_called = True
            if on_delta:
                on_delta("forced")
            return "forced"

        def describe_image(self, *a, **k):
            return ""

        def generate_image(self, *a, **k):
            return b""

    llm = EndlessLLM()
    agent = SlackMentionAgent(llm=llm, context=_ctx(), registry=reg, max_steps=2, on_stream=lambda d: None)
    result = agent.run("q")
    assert result.text == "forced"
    assert llm.end_called is True
    assert result.steps == 2


def test_agent_on_step_fires_for_tool_use_and_compose():
    reg = _registry_with_search()
    events: list[tuple[int, str, dict]] = []
    llm = ScriptedLLM(
        [
            LLMResult(
                content="",
                tool_calls=[ToolCall(id="c1", name="search_web", arguments={"query": "x"})],
                stop_reason="tool_use",
            ),
            LLMResult(content="done", tool_calls=[], stop_reason="end_turn"),
        ]
    )
    agent = SlackMentionAgent(
        llm=llm,
        context=_ctx(),
        registry=reg,
        max_steps=3,
        on_step=lambda step, phase, detail: events.append((step, phase, detail)),
    )
    agent.run("q")
    phases = [p for _, p, _ in events]
    assert "tool_use" in phases
    assert "tool_result" in phases
    assert "compose" in phases
    # compose should fire on the second hop (step=2) without max_steps_hit flag
    compose_events = [e for e in events if e[1] == "compose"]
    assert compose_events[0][2].get("max_steps_hit") is not True


def test_agent_streams_final_answer_when_on_stream_set():
    """When on_stream is set, chat(on_delta=...) streams content during the
    terminal hop. The agent must not then re-call the LLM to re-stream."""
    reg = _registry_with_search()
    delta_buffer: list[str] = []

    class StreamingLLM(ScriptedLLM):
        def chat(self, system, messages, tools=None, max_tokens=1024, on_delta=None):
            self.calls.append({"on_delta": on_delta})
            # emit three deltas through on_delta, no tool_calls
            if on_delta is not None:
                for chunk in ["재미있", "는 답변", "입니다"]:
                    on_delta(chunk)
            return LLMResult(content="재미있는 답변입니다", tool_calls=[], stop_reason="end_turn")

    llm = StreamingLLM([])
    agent = SlackMentionAgent(
        llm=llm,
        context=_ctx(),
        registry=reg,
        max_steps=3,
        on_stream=delta_buffer.append,
    )
    result = agent.run("q")
    assert delta_buffer == ["재미있", "는 답변", "입니다"]
    assert result.text == "재미있는 답변입니다"
    # Exactly one chat call — no compose re-call.
    assert len(llm.calls) == 1


def test_agent_closes_owned_executor_after_run():
    """SlackMentionAgent must release the ThreadPoolExecutor it created so
    Lambda warm containers don't accumulate idle workers across requests."""
    reg = _registry_with_search()
    llm = ScriptedLLM([LLMResult(content="done", tool_calls=[], stop_reason="end_turn")])
    agent = SlackMentionAgent(llm=llm, context=_ctx(), registry=reg, max_steps=3)
    agent.run("q")
    assert agent.executor._closed is True


def test_agent_closes_owned_executor_on_exception():
    """Even when the LLM raises, the owned executor must still be closed."""
    reg = _registry_with_search()

    class BoomLLM:
        def chat(self, *a, **k):
            raise RuntimeError("boom")

        def stream_chat(self, *a, **k):  # pragma: no cover
            raise RuntimeError("boom")

        def describe_image(self, *a, **k):  # pragma: no cover
            return ""

        def generate_image(self, *a, **k):  # pragma: no cover
            return b""

    agent = SlackMentionAgent(llm=BoomLLM(), context=_ctx(), registry=reg, max_steps=3)
    import pytest

    with pytest.raises(RuntimeError):
        agent.run("q")
    assert agent.executor._closed is True


def test_agent_does_not_close_injected_executor():
    """An externally-supplied ToolExecutor is owned by the caller; the agent
    must not shut it down."""
    from src.tools.registry import ToolExecutor

    reg = _registry_with_search()
    ext_exec = ToolExecutor(_ctx(), reg)
    llm = ScriptedLLM([LLMResult(content="done", tool_calls=[], stop_reason="end_turn")])
    agent = SlackMentionAgent(
        llm=llm, context=_ctx(), registry=reg, max_steps=3, tool_executor=ext_exec
    )
    agent.run("q")
    assert ext_exec._closed is False
    ext_exec.close()


def test_system_prompt_keeps_task_rules_even_when_system_message_set():
    """SYSTEM_MESSAGE must NOT replace the task rules — it is appended as
    additional policy so the agent's planning / tool / attachment guidance
    always stays in the prompt."""
    reg = _registry_with_search()
    agent = SlackMentionAgent(
        llm=MagicMock(),
        context=_ctx(),
        registry=reg,
        max_steps=3,
        system_message="Do not expose secrets.",
        persona_message="자연스러운 한국어로 핵심부터 답한다.",
    )
    prompt = agent._build_system_prompt()
    # Layer 1: task + slack + attachment rules are always present.
    assert "Plan work, call tools when needed" in prompt
    assert "in parallel within a single turn" in prompt
    assert "generate_image" in prompt
    assert "fetch_thread_history" in prompt
    # Layer 2 / 3 labels present and carry the operator-supplied text.
    assert "Additional policy:" in prompt
    assert "Do not expose secrets." in prompt
    assert "Response style:" in prompt
    assert "자연스러운 한국어로 핵심부터 답한다." in prompt
    # Language re-emphasis remains last.
    assert prompt.rstrip().endswith("Respond in language: ko.")


def test_system_prompt_includes_tier_s_guidance():
    """The three Tier-S guidances must stay in the prompt:
    (H1) Slack mrkdwn rendering, (H2) speculative attachment lookup on
    ambiguous references, (H3) tool-failure response policy."""
    reg = _registry_with_search()
    agent = SlackMentionAgent(
        llm=MagicMock(), context=_ctx(), registry=reg, max_steps=3
    )
    prompt = agent._build_system_prompt()
    # H1: mrkdwn vs GitHub markdown guidance.
    assert "mrkdwn" in prompt
    assert "`*bold*`" in prompt
    assert "`**bold**`" in prompt  # the anti-pattern is called out
    assert "<https://url|label>" in prompt
    # H2: speculative attachment call on ambiguous reference.
    assert "speculatively" in prompt
    assert "이 사진" in prompt
    # H3: tool-failure policy.
    assert "ok:false" in prompt
    assert "do not retry blindly" in prompt
    assert "do not fabricate" in prompt


def test_system_prompt_omits_empty_optional_layers():
    """When SYSTEM_MESSAGE / PERSONA_MESSAGE are unset, their labeled
    blocks must not appear in the prompt."""
    reg = _registry_with_search()
    agent = SlackMentionAgent(
        llm=MagicMock(), context=_ctx(), registry=reg, max_steps=3
    )
    prompt = agent._build_system_prompt()
    assert "Additional policy:" not in prompt
    assert "Response style:" not in prompt
    assert "User memory" not in prompt  # no memory layer when empty
    assert "Plan work" in prompt


def test_system_prompt_renders_user_memory_section():
    """user_memory entries must appear under a clearly labeled section so
    the LLM treats them as durable user context, not transient history."""
    reg = _registry_with_search()
    agent = SlackMentionAgent(
        llm=MagicMock(),
        context=_ctx(),
        registry=reg,
        max_steps=3,
        user_memory=[
            {"key": "company", "value": "Daangn", "ts": 1700000000},
            {"key": "team", "value": "AI Platform", "ts": 1700000100},
        ],
    )
    prompt = agent._build_system_prompt()
    assert "User memory" in prompt
    assert "- company: Daangn" in prompt
    assert "- team: AI Platform" in prompt


def test_agent_aggregates_token_usage():
    reg = _registry_with_search()
    llm = ScriptedLLM(
        [
            LLMResult(
                content="",
                tool_calls=[ToolCall(id="c1", name="search_web", arguments={"query": "x"})],
                stop_reason="tool_use",
                token_usage={"input": 10, "output": 20},
            ),
            LLMResult(
                content="done",
                tool_calls=[],
                stop_reason="end_turn",
                token_usage={"input": 5, "output": 7},
            ),
        ]
    )
    agent = SlackMentionAgent(llm=llm, context=_ctx(), registry=reg, max_steps=3)
    result = agent.run("q")
    assert result.token_usage == {"input": 15, "output": 27}
