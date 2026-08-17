"""UpstageProvider — Solar chat at https://api.upstage.ai/v1."""
from __future__ import annotations

from src.llms.openai_wire import _OpenAICompatProvider


class UpstageProvider(_OpenAICompatProvider):
    """Upstage (Solar) — OpenAI-wire compatible, different base URL.

    Models (text only):
      solar-pro2, solar-pro, solar-mini, ...

    All current Solar chat models accept `max_tokens` + `temperature` the
    classic way, so the inherited `_token_params` default is correct — no
    `max_completion_tokens` split.

    Upstage has no image generation/edit endpoint. Because IMAGE_PROVIDER
    falls back to LLM_PROVIDER, `LLM_PROVIDER=upstage` without an explicit
    IMAGE_PROVIDER would otherwise route `generate_image` to the OpenAI SDK
    against api.upstage.ai and fail with an opaque error. Raise a clear
    message instead so `ToolExecutor` surfaces it to the LLM as recoverable.
    """

    BASE_URL = "https://api.upstage.ai/v1"
    API_KEY_ENV_VAR = "UPSTAGE_API_KEY"

    def generate_image(self, prompt: str) -> bytes:
        raise NotImplementedError(
            "generate_image is not supported by Upstage. "
            "Switch IMAGE_PROVIDER to 'openai', 'xai', or 'bedrock'."
        )

    def edit_image(self, prompt: str, images: list[tuple[bytes, str]]) -> bytes:
        raise NotImplementedError(
            "edit_image is not supported by Upstage. "
            "Switch IMAGE_PROVIDER to 'openai' or 'xai' for editing."
        )
