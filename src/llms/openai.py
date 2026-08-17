"""OpenAIProvider — default OpenAI endpoint with vision and gpt-image-1."""
from __future__ import annotations

from typing import Any

from src.llms.openai_wire import _OpenAICompatProvider, _is_new_gen_openai


class OpenAIProvider(_OpenAICompatProvider):
    BASE_URL = None  # default OpenAI endpoint
    API_KEY_ENV_VAR = "OPENAI_API_KEY"

    def _token_params(self, max_tokens: int) -> dict[str, Any]:
        # Newer OpenAI reasoning models only accept max_completion_tokens and
        # reject `temperature`. Legacy chat models still use max_tokens.
        if _is_new_gen_openai(self.model):
            return {"max_completion_tokens": max_tokens}
        return {"max_tokens": max_tokens, "temperature": 0.2}
