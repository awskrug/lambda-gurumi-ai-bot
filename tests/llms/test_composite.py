"""Tests for src.llms.composite."""
from __future__ import annotations

from unittest.mock import MagicMock

from src.llms.composite import _CompositeProvider


def test_composite_provider_routes_image_to_image_llm():
    text = MagicMock()
    image = MagicMock()
    image.generate_image.return_value = b"img"
    composite = _CompositeProvider(text=text, image=image)
    composite.chat(system="s", messages=[])
    text.chat.assert_called_once()
    composite.generate_image("x")
    image.generate_image.assert_called_once_with("x")


def test_composite_provider_routes_edit_to_image_llm():
    """edit_image must follow the same routing as generate_image — when the
    operator pairs (e.g.) Bedrock text + xAI image, edits go to xAI."""
    text = MagicMock()
    image = MagicMock()
    image.edit_image.return_value = b"edited"
    composite = _CompositeProvider(text=text, image=image)

    out = composite.edit_image("style", [(b"a", "image/png")])

    assert out == b"edited"
    image.edit_image.assert_called_once_with("style", [(b"a", "image/png")])
    text.edit_image.assert_not_called()
