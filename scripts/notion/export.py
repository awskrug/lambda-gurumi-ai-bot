import os
import pathlib

from notion_exporter import NotionExporter


NOTION_TOKEN = os.getenv("NOTION_TOKEN")

NOTION_PAGE_NAME = os.getenv("NOTION_PAGE_NAME", "demo")
NOTION_PAGE_ID = os.getenv("NOTION_PAGE_ID", "7aace0412a82431996f61a29225a95ec")


if __name__ == "__main__":
    if not NOTION_TOKEN:
        raise ValueError("NOTION_TOKEN environment variable is required")

    exporter = NotionExporter(
        notion_token=NOTION_TOKEN,
        export_child_pages=True,
    )
    exported_pages = exporter.export_pages(
        page_ids=[NOTION_PAGE_ID],
    )

    output_dir = pathlib.Path("build") / NOTION_PAGE_NAME
    output_dir.mkdir(parents=True, exist_ok=True)

    for page_id, content in exported_pages.items():
        file_path = output_dir / f"{page_id}.md"
        file_path.write_text(content, encoding="utf-8")
        print(f"Exported: {file_path}")

    print(f"Exported {len(exported_pages)} pages to {output_dir}")
