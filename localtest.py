"""Local CLI test script — runs SlackMentionAgent without a real Slack connection.

Streams LLM output token-by-token and prints intermediate agent steps
(tool calls, their results, compose phase) to stderr so they stay out of
the streamed answer on stdout.

Usage:
    python localtest.py "질문 내용"
    python localtest.py                # interactive (stdin, Ctrl+D to submit)
    python localtest.py --no-stream    # wait for the full answer, then print
    python localtest.py --quiet-steps  # hide intermediate step indicators

Environment:
    Copy .env.example to .env.local and fill in values before running.
    Minimum required: OPENAI_API_KEY (for OpenAI provider).
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any


logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")


LOCAL_UPLOAD_DIR = Path("./.uploads")


class _StubSlackClient:
    """Returns empty but structurally correct responses for every Slack call.

    `files_upload_v2` writes the received bytes to ./.uploads/ so you can
    actually open generated images instead of discarding them.
    """

    # `read_attached_images` / `edit_image` read the bot token off the
    # WebClient (per-app token in production); a non-empty stub keeps the
    # auth header builder from short-circuiting on falsy values.
    token = "xoxb-stub"

    def conversations_replies(self, **_):
        return {"messages": []}

    def files_upload_v2(self, *, file=None, filename="generated.bin", **_):
        if file is None:
            return {"file": {"permalink": "", "title": filename}}
        LOCAL_UPLOAD_DIR.mkdir(exist_ok=True)
        ts = int(time.time() * 1000)
        path = LOCAL_UPLOAD_DIR / f"{ts}-{filename}"
        path.write_bytes(file)
        resolved = path.resolve()
        return {"file": {"permalink": resolved.as_uri(), "title": filename}}

    def users_info(self, **_):
        return {"user": {"profile": {"display_name": "local-user"}}}


def _build_slack_client(token: str):
    if token and not token.startswith("xoxb-your"):
        try:
            from slack_sdk import WebClient

            return WebClient(token=token)
        except Exception:
            pass
    return _StubSlackClient()


def _detect_image_mime(data: bytes) -> str:
    """Detect image MIME from magic bytes — file extensions lie (e.g. JPEG saved as .png)."""
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"GIF8"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return "application/octet-stream"


def _install_local_image_fetcher(url_to_path: dict[str, Path]) -> None:
    """Patch HTTP helpers so the image tools can 'download' local files via
    fake files.slack.com URLs without hitting the real network.

    Two interception points are needed:
      1. `urllib.request.urlopen` — for the document fetch path.
      2. `src.tools.{slack,image}._http_get` — the no-redirect helper the
         image tools route through (introduced for the SSRF/Authorization
         leak fix). It uses `build_opener` internally and bypasses any
         `urlopen` monkeypatch.

    Real Slack URLs untouched — anything not in the mapping passes through
    to the original handlers, so production Slack behaviour is unaffected
    in environments where SLACK_BOT_TOKEN is real.
    """
    real_urlopen = urllib.request.urlopen

    class _LocalResponse:
        def __init__(self, body: bytes, mime: str):
            self._body = body
            self.headers = {"Content-Type": mime}

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def read(self, n: int = -1) -> bytes:
            if n == -1 or n >= len(self._body):
                return self._body
            return self._body[:n]

    def _patched(req, *args, **kwargs):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        path = url_to_path.get(url)
        if path is None:
            return real_urlopen(req, *args, **kwargs)
        body = path.read_bytes()
        return _LocalResponse(body, _detect_image_mime(body[:32]))

    urllib.request.urlopen = _patched

    def _patched_http_get(req, timeout: int = 15):
        return _patched(req, timeout=timeout)

    from src.tools import image as _image_tools
    from src.tools import slack as _slack_tools

    _image_tools._http_get = _patched_http_get
    _slack_tools._http_get = _patched_http_get


def _build_attachment_event(attach_paths: list[str]) -> dict[str, Any]:
    """Turn `--attach` paths into a fake Slack mention event the image tools
    can consume. Returns {} when no attachments were given."""
    if not attach_paths:
        return {}
    files: list[dict[str, Any]] = []
    url_to_path: dict[str, Path] = {}
    for raw in attach_paths:
        path = Path(raw).expanduser().resolve()
        if not path.is_file():
            print(f"[오류] 첨부 파일을 찾을 수 없습니다: {raw}", file=sys.stderr)
            sys.exit(1)
        head = path.read_bytes()[:32]
        mime = _detect_image_mime(head)
        # The host has to be one of the SLACK_FILE_HOSTS values so the SSRF
        # guard in src/tools/image.py accepts it; a millis-prefixed name
        # keeps every test attachment URL unique even within one run.
        url = f"https://files.slack.com/local/{int(time.time() * 1000)}-{path.name}"
        url_to_path[url] = path
        files.append(
            {
                "mimetype": mime,
                "url_private_download": url,
                "name": path.name,
            }
        )
    _install_local_image_fetcher(url_to_path)
    return {"files": files}


def _make_on_step(quiet: bool):
    if quiet:
        return None

    def on_step(step: int, phase: str, detail: dict[str, Any]) -> None:
        if phase == "tool_use":
            tools = ", ".join(detail.get("tools") or [])
            print(f"\n[step {step}] ▶ 도구 호출: {tools}", file=sys.stderr, flush=True)
        elif phase == "tool_result":
            mark = "✓" if detail.get("ok") else "✗"
            tool = detail.get("tool") or ""
            if detail.get("ok"):
                print(f"[step {step}] {mark} {tool}", file=sys.stderr, flush=True)
            else:
                err = detail.get("error") or ""
                print(f"[step {step}] {mark} {tool}: {err}", file=sys.stderr, flush=True)
        elif phase == "compose":
            hint = " (max_steps 도달)" if detail.get("max_steps_hit") else ""
            print(f"\n[step {step}] ▶ 답변 작성 중...{hint}", file=sys.stderr, flush=True)

    return on_step


def main() -> None:
    parser = argparse.ArgumentParser(description="Local agent test runner.")
    parser.add_argument("question", nargs="*", help="Question text. If omitted, read from stdin.")
    parser.add_argument("--no-stream", action="store_true", help="Disable streaming output; print the full answer at the end.")
    parser.add_argument("--quiet-steps", action="store_true", help="Suppress intermediate step logs on stderr.")
    parser.add_argument(
        "--attach",
        action="append",
        default=[],
        metavar="PATH",
        help="로컬 이미지를 mention 첨부로 시뮬레이션 (반복 사용 가능). edit_image 같은 첨부 기반 도구 테스트용.",
    )
    args = parser.parse_args()

    stream_mode = not args.no_stream

    from src.agent import SlackMentionAgent
    from src.config import Settings
    from src.llms import get_llm
    from src.tools import ToolContext, default_registry

    settings = Settings.from_env()

    if not settings.slack_bot_token or settings.slack_bot_token.startswith("xoxb-your"):
        print("[경고] SLACK_BOT_TOKEN 미설정 — Slack 관련 도구는 빈 결과를 반환합니다.\n", file=sys.stderr)
    if settings.llm_provider == "openai":
        import os

        if not os.getenv("OPENAI_API_KEY"):
            print("[오류] OPENAI_API_KEY가 설정되지 않았습니다. .env.local 을 확인하세요.", file=sys.stderr)
            sys.exit(1)
    elif settings.llm_provider == "xai":
        if not settings.xai_api_key:
            print("[오류] XAI_API_KEY가 설정되지 않았습니다. .env.local 을 확인하세요.", file=sys.stderr)
            sys.exit(1)

    llm = get_llm(
        provider=settings.llm_provider,
        model=settings.llm_model,
        image_provider=settings.image_provider,
        image_model=settings.image_model,
        region=settings.aws_region,
        api_keys={"xai": settings.xai_api_key},
    )

    slack_client = _build_slack_client(settings.slack_bot_token)
    event = _build_attachment_event(args.attach)
    if args.attach:
        names = ", ".join(f["name"] for f in event["files"])
        print(f"[첨부] {names}", file=sys.stderr)
    context = ToolContext(
        slack_client=slack_client,
        channel="local",
        thread_ts="0",
        event=event,
        settings=settings,
        llm=llm,
    )

    if args.question:
        user_message = " ".join(args.question).strip()
    else:
        print("질문을 입력하세요 (Ctrl+D 로 종료):", file=sys.stderr)
        try:
            user_message = sys.stdin.read().strip()
        except (EOFError, KeyboardInterrupt):
            print(file=sys.stderr)
            sys.exit(0)

    if not user_message:
        print("[오류] 질문이 비어 있습니다.", file=sys.stderr)
        sys.exit(1)

    print(f"\n▶ 질문: {user_message}", file=sys.stderr)

    on_stream = None
    if stream_mode:
        def on_stream(delta: str) -> None:  # noqa: RUF013
            sys.stdout.write(delta)
            sys.stdout.flush()

    on_step = _make_on_step(args.quiet_steps)

    agent = SlackMentionAgent(
        llm=llm,
        context=context,
        registry=default_registry,
        max_steps=settings.agent_max_steps,
        response_language=settings.response_language,
        system_message=settings.system_message,
        on_stream=on_stream,
        on_step=on_step,
        max_output_tokens=settings.max_output_tokens,
    )

    try:
        result = agent.run(user_message)
    except Exception as exc:  # noqa: BLE001
        print(f"\n[오류] {exc}", file=sys.stderr)
        sys.exit(1)

    if stream_mode:
        # Streaming already wrote the answer to stdout; just end the line.
        sys.stdout.write("\n")
        sys.stdout.flush()
    else:
        print("\n" + "─" * 60, file=sys.stderr)
        sys.stdout.write(result.text + "\n")
        sys.stdout.flush()
        print("─" * 60, file=sys.stderr)

    if result.image_url:
        print(f"[이미지] {result.image_url}", file=sys.stderr)

    print(
        f"\nsteps={result.steps} tool_calls={result.tool_calls_count} "
        f"tokens_in={result.token_usage.get('input', 0)} tokens_out={result.token_usage.get('output', 0)}",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
