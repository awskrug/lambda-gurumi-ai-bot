"""Tests for src.llms.factory (get_llm)."""
from __future__ import annotations

from src.llms import get_llm
from src.llms.composite import _CompositeProvider
from src.llms.openai import OpenAIProvider
from src.llms.upstage import UpstageProvider
from src.llms.xai import XAIProvider


def test_get_llm_builds_upstage_provider():
    provider = get_llm(
        provider="upstage",
        model="solar-pro2",
        image_provider="upstage",
        image_model="",
        region="us-east-1",
        api_keys={"upstage": "up-secret"},
    )
    assert isinstance(provider, UpstageProvider)
    assert provider._api_key == "up-secret"


def test_get_llm_composite_upstage_text_openai_image():
    """Upstage has no image support, so a typical setup pairs it with a
    different image provider through _CompositeProvider."""
    provider = get_llm(
        provider="upstage",
        model="solar-pro2",
        image_provider="openai",
        image_model="gpt-image-1",
        region="us-east-1",
        api_keys={"upstage": "up-key"},
    )
    assert isinstance(provider, _CompositeProvider)
    assert isinstance(provider.text, UpstageProvider)
    assert isinstance(provider.image, OpenAIProvider)


def test_get_llm_builds_xai_provider():
    provider = get_llm(
        provider="xai",
        model="grok-4-1-fast-reasoning",
        image_provider="xai",
        image_model="grok-imagine-image",
        region="us-east-1",
        api_keys={"xai": "xai-secret"},
    )
    assert isinstance(provider, XAIProvider)
    assert provider._api_key == "xai-secret"


def test_get_llm_composite_xai_text_openai_image():
    """Mixed-provider setups still work through _CompositeProvider."""
    provider = get_llm(
        provider="xai",
        model="grok-4-1-fast-reasoning",
        image_provider="openai",
        image_model="gpt-image-1",
        region="us-east-1",
        api_keys={"xai": "xai-key"},
    )
    assert isinstance(provider, _CompositeProvider)
    assert isinstance(provider.text, XAIProvider)
    assert isinstance(provider.image, OpenAIProvider)
