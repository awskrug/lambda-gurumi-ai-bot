"""get_llm — build an LLM provider from explicit parameters."""
from __future__ import annotations

from src.llms.base import LLMProvider
from src.llms.bedrock import BedrockProvider
from src.llms.composite import _CompositeProvider
from src.llms.openai import OpenAIProvider
from src.llms.upstage import UpstageProvider
from src.llms.xai import XAIProvider


def get_llm(
    provider: str,
    model: str,
    image_provider: str,
    image_model: str,
    region: str = "us-east-1",
    api_keys: dict[str, str | None] | None = None,
) -> LLMProvider:
    """Build an LLM client for the requested provider(s).

    `api_keys` carries per-provider keys that need explicit wiring (xAI today;
    OpenAI reads OPENAI_API_KEY from env directly, Bedrock uses the AWS SDK
    credential chain).
    """
    api_keys = api_keys or {}

    def build(p: str) -> LLMProvider:
        if p == "bedrock":
            return BedrockProvider(model=model, image_model=image_model, region=region)
        if p == "xai":
            return XAIProvider(model=model, image_model=image_model, api_key=api_keys.get("xai"))
        if p == "upstage":
            return UpstageProvider(model=model, image_model=image_model, api_key=api_keys.get("upstage"))
        return OpenAIProvider(model=model, image_model=image_model)

    text = build(provider)
    if image_provider == provider:
        return text
    return _CompositeProvider(text=text, image=build(image_provider))
