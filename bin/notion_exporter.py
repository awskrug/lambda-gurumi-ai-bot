import os

from python_notion_exporter import NotionExporter, ExportType, ViewExportType


NOTION_TOKEN = os.getenv("NOTION_TOKEN")
NOTION_FILE_TOKEN = os.getenv("NOTION_FILE_TOKEN")

NOTION_PAGE_ID = os.getenv("NOTION_PAGE_ID", "0c7c08203a9b4435a4ca07b6454151d7")
NOTION_PAGE_NAME = os.getenv("NOTION_PAGE_NAME", "demo")


if __name__ == "__main__":
    exporter = NotionExporter(
        token_v2=NOTION_TOKEN,
        file_token=NOTION_FILE_TOKEN,
        pages={NOTION_PAGE_NAME: NOTION_PAGE_ID},
        export_directory="build",
        flatten_export_file_tree=True,
        export_type=ExportType.MARKDOWN,
        current_view_export_type=ViewExportType.CURRENT_VIEW,
        include_files=False,
        recursive=True,
        export_name=NOTION_PAGE_NAME,
    )
    exporter.process()
