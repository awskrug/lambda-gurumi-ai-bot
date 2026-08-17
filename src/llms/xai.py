"""XAIProvider — Grok chat + grok-imagine at https://api.x.ai/v1."""
from __future__ import annotations

import base64
import json
import os
import urllib.error
import urllib.request
from typing import Any

from src.llms.openai_wire import _OpenAICompatProvider


class XAIProvider(_OpenAICompatProvider):
    """xAI (Grok) — OpenAI-wire compatible, different base URL and image params.

    Models:
      text:  grok-4-1-fast-reasoning, grok-4.20-0309-reasoning, ...
      image: grok-imagine-image, grok-imagine-image-pro

    Differences from OpenAI that matter here:
      - `images.generate` rejects `size` (uses `aspect_ratio`/`resolution`).
        We omit `size` and request `response_format=b64_json` so we can
        decode bytes locally, matching the rest of the pipeline.
      - `images.edit` of the OpenAI SDK is NOT supported by xAI (per docs):
        their `/v1/images/edits` is JSON, not multipart, and uses an
        `image: {url, type}` field. We bypass the SDK and POST raw JSON.
      - All current grok chat models accept `max_tokens` + `temperature`
        the classic way — no `max_completion_tokens` split.
    """

    BASE_URL = "https://api.x.ai/v1"
    API_KEY_ENV_VAR = "XAI_API_KEY"

    def _image_generate_kwargs(self, prompt: str) -> dict[str, Any]:
        return {
            "model": self.image_model,
            "prompt": prompt,
            "n": 1,
            "response_format": "b64_json",
        }

    def edit_image(self, prompt: str, images: list[tuple[bytes, str]]) -> bytes:
        if not images:
            raise ValueError("edit_image requires at least one input image")
        api_key = self._api_key or os.environ.get(self.API_KEY_ENV_VAR, "")
        if not api_key:
            raise ValueError(f"{self.API_KEY_ENV_VAR} not configured")

        # Each input image becomes a `{url: data:..., type: image_url}` block.
        # Per xAI docs the field is named `image` and supports up to 5 images;
        # the documented single-image shape is one object, not an array, so
        # we send the bare object when there's one image and an array when
        # there are several. If xAI rejects the multi form, the executor
        # surfaces it to the LLM as a recoverable tool error.
        blocks = [
            {
                "url": f"data:{mime};base64,{base64.b64encode(data).decode('utf-8')}",
                "type": "image_url",
            }
            for data, mime in images
        ]
        body = {
            "model": self.image_model,
            "prompt": prompt,
            "image": blocks[0] if len(blocks) == 1 else blocks,
            "n": 1,
            "response_format": "b64_json",
        }
        req = urllib.request.Request(
            f"{self.BASE_URL}/images/edits",
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=240) as resp:  # noqa: S310 (xAI host pinned by BASE_URL)
                payload = json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
            raise ValueError(f"xAI image edit failed: HTTP {exc.code} {detail[:300]}") from exc

        data = payload.get("data") or []
        if not data or not data[0].get("b64_json"):
            raise ValueError("xAI image edit returned no image bytes")
        return base64.b64decode(data[0]["b64_json"])
