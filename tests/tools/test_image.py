"""Tests for src.tools.image."""
from __future__ import annotations

from unittest.mock import MagicMock

from tests.tools._helpers import _ctx
from src.tools.image import generate_image


# --------------------------------------------------------------------------- #
# generate_image
# --------------------------------------------------------------------------- #


def test_generate_image_returns_permalink():
    llm = MagicMock()
    llm.generate_image.return_value = b"imgbytes"
    client = MagicMock()
    client.files_upload_v2.return_value = {"file": {"permalink": "https://slack/abc", "title": "t"}}
    ctx = _ctx(slack_client=client, llm=llm)
    out = generate_image(ctx, prompt="cat")
    assert out == {"permalink": "https://slack/abc", "title": "t"}
    llm.generate_image.assert_called_once_with("cat")
    upload_kwargs = client.files_upload_v2.call_args.kwargs
    assert upload_kwargs["title"] == ctx.settings.image_model
