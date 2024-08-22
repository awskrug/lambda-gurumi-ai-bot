import os

from python_notion_exporter import NotionExporter, ExportType, ViewExportType


NOTION_TOKEN_V2 = os.getenv("NOTION_TOKEN_V2")
NOTION_FILE_TOKEN = os.getenv("NOTION_FILE_TOKEN")

NOTION_PAGE_NAME = os.getenv("NOTION_PAGE_NAME", "demo")
NOTION_PAGE_ID = os.getenv("NOTION_PAGE_ID", "7aace0412a82431996f61a29225a95ec")


if __name__ == "__main__":
    exporter = NotionExporter(
        token_v2=NOTION_TOKEN_V2,
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
