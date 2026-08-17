"""CompositeProvider — wrap separate text and image providers."""
from __future__ import annotations

from dataclasses import dataclass

from src.llms.base import LLMProvider


@dataclass
class _CompositeProvider:
    """Delegates generate_image to a different provider than chat/vision."""

    text: LLMProvider
    image: LLMProvider

    def chat(self, system, messages, tools=None, max_tokens=1024, on_delta=None):
        return self.text.chat(system, messages, tools=tools, max_tokens=max_tokens, on_delta=on_delta)

    def stream_chat(self, system, messages, on_delta, max_tokens=1024):
        return self.text.stream_chat(system, messages, on_delta, max_tokens=max_tokens)

    def describe_image(self, image_bytes, mime_type):
        return self.text.describe_image(image_bytes, mime_type)

    def generate_image(self, prompt):
        return self.image.generate_image(prompt)

    def edit_image(self, prompt, images):
        return self.image.edit_image(prompt, images)
