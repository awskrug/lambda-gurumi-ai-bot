"""Image generation tool — calls the configured LLM provider and uploads
to the current Slack thread."""
from __future__ import annotations

from src.tools.registry import ToolContext, default_registry, tool


@tool(
    default_registry,
    name="generate_image",
    description="Generate an image from a prompt and upload it to the Slack thread. Returns the permalink.",
    parameters={
        "type": "object",
        "properties": {"prompt": {"type": "string"}},
        "required": ["prompt"],
    },
    timeout=240.0,  # gpt-image-2 / titan / stability can take 60–180s; Lambda caps at 300s, leaves ~60s for compose + upload
)
def generate_image(ctx: ToolContext, prompt: str) -> dict[str, str]:
    image_bytes = ctx.llm.generate_image(prompt)
    model_name = ctx.settings.image_model
    upload = ctx.slack_client.files_upload_v2(
        channel=ctx.channel,
        thread_ts=ctx.thread_ts,
        title=model_name,
        filename="generated.png",
        file=image_bytes,
    )
    file_info = upload.get("file", {})
    return {"permalink": file_info.get("permalink", ""), "title": file_info.get("title", "generated.png")}
