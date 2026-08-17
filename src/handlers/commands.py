"""Worker handler for Slack slash commands (`/img-gpt`, `/img-xai`).

Reached from `router._process_worker` when the worker payload carries
`kind=command`. The receiver path (Bolt's `@app.command(...)`) ack()s
immediately and enqueues this worker — so by the time we run, the Slack
3-second ack window is already closed and we have the full Lambda
timeout (300s) to generate + upload.

Flow per invocation:

  1. Validate `command` + non-empty `text`; respond ephemerally on bad
     input so it does not surface as a public bot message.
  2. Two-stage dedup on `trigger_id` (same contract as the message
     handler — `reserve` blocks parallel runs, `mark_done` absorbs
     Lambda async retries past the in-flight TTL).
  3. Build a command-specific LLM via `get_llm(image_provider=..., image_model=...)`
     so the slash command bypasses the deployment-wide IMAGE_MODEL /
     IMAGE_PROVIDER defaults.
  4. Generate the image and `files_upload_v2` it to the channel with the
     prompt as `initial_comment`.
  5. Any provider/upload failure → ephemeral error via `response_url`,
     skip `mark_done` (so a retry can attempt again).

Cross-module state is reached via the `runtime` module object so tests
can monkeypatch `src.runtime`'s accessors. The `get_llm` import is at
module level so tests can `monkeypatch.setattr(_commands, "get_llm", ...)`.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
import uuid

from src import runtime
from src.llms import get_llm
from src.logging_utils import log_event, set_request_id


# `/command` → (image_provider, settings-attr-name carrying the model id).
# Using attribute names instead of resolving here keeps the mapping
# table inert until invocation time, so tests that swap settings on the
# runtime module pick up the right value without re-importing.
_COMMAND_TO_IMAGE: dict[str, tuple[str, str]] = {
    "/img-gpt": ("openai", "image_model_gpt"),
    "/img-xai": ("xai", "image_model_xai"),
}


def _process_command(payload: dict, client, api_app_id: str = "") -> None:  # noqa: ANN001
    set_request_id(str(uuid.uuid4()))
    command = (payload.get("command") or "").strip()
    text = (payload.get("text") or "").strip()
    channel = payload.get("channel_id") or ""
    user = payload.get("user_id") or ""
    trigger_id = payload.get("trigger_id") or ""
    response_url = payload.get("response_url") or ""

    spec = _COMMAND_TO_IMAGE.get(command)
    if spec is None:
        log_event(runtime.logger, "command.unknown", command=command, api_app_id=api_app_id)
        _respond_error(response_url, f"지원하지 않는 명령입니다: `{command}`")
        return
    if not text:
        _respond_error(response_url, f"사용법: `{command} <prompt>`")
        return
    if not channel:
        log_event(runtime.logger, "command.no_channel", command=command, api_app_id=api_app_id)
        return

    # trigger_id is unique per slash-command invocation, including across
    # the user pressing the command twice in a row. Slack does NOT retry
    # slash commands the way it retries events, but Lambda async self-retry
    # still applies — so the two-stage dedup mirrors the message path.
    dedup = runtime._get_dedup()
    full_key = f"dedup:cmd:{trigger_id}" if trigger_id else f"dedup:cmd:{channel}:{user}:{text[:64]}"
    dedup_available = True
    try:
        if dedup.is_done(full_key):
            log_event(runtime.logger, "dedup.skip", key=full_key, reason="already_done")
            return
        if not dedup.reserve(full_key, user=user or "system"):
            log_event(runtime.logger, "dedup.skip", key=full_key, reason="in_flight")
            return
    except Exception as exc:  # noqa: BLE001
        runtime.logger.warning("dedup unavailable, proceeding without it: %s", exc)
        dedup_available = False

    settings = runtime.settings
    image_provider, model_attr = spec
    image_model = getattr(settings, model_attr)

    log_event(
        runtime.logger,
        "command.start",
        command=command,
        user=user,
        channel=channel,
        api_app_id=api_app_id,
        image_provider=image_provider,
        image_model=image_model,
    )

    try:
        # The text provider stays on the deployment default because we
        # never call chat() here — OpenAICompatProvider is lazy and only
        # builds the underlying client on first use.
        llm = get_llm(
            provider=settings.llm_provider,
            model=settings.llm_model,
            image_provider=image_provider,
            image_model=image_model,
            region=settings.aws_region,
            api_keys={"xai": settings.xai_api_key},
        )
        image_bytes = llm.generate_image(text)
        client.files_upload_v2(
            channel=channel,
            title=image_model,
            filename="generated.png",
            file=image_bytes,
            initial_comment=f"`{command}` {text}",
        )
    except Exception as exc:  # noqa: BLE001
        log_event(
            runtime.logger,
            "command.failure",
            command=command,
            error_class=exc.__class__.__name__,
            api_app_id=api_app_id,
        )
        runtime.logger.debug("command failure traceback", exc_info=True)
        _respond_error(response_url, f"이미지 생성 실패: {exc}")
        return

    if dedup_available:
        try:
            dedup.mark_done(full_key, user=user or "system")
        except Exception as exc:  # noqa: BLE001
            runtime.logger.debug("dedup.mark_done failed: %s", exc)

    log_event(
        runtime.logger,
        "command.done",
        command=command,
        user=user,
        channel=channel,
        api_app_id=api_app_id,
    )


def _respond_error(response_url: str, text: str) -> None:
    """POST an ephemeral error reply via the slash-command `response_url`.

    The response_url is valid for 30 minutes / 5 uses, and is the only
    surface where we can deliver feedback that's visible *only* to the
    invoking user. Falls back to a structured log if posting fails — we
    do not want a delivery error to mask the underlying command error.
    """
    if not response_url:
        return
    body = json.dumps({"response_type": "ephemeral", "text": text}).encode("utf-8")
    req = urllib.request.Request(
        response_url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310 (Slack-issued URL)
            resp.read()
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as exc:
        runtime.logger.warning("response_url post failed: %s", exc)
