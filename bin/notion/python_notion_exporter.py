import concurrent
import json
import logging
import multiprocessing
import os
import shutil
import time
import requests

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from tqdm import tqdm


class ExportType:
    """Represent the different types of export formats."""

    MARKDOWN = "markdown"
    HTML = "html"
    PDF = "pdf"


class ViewExportType:
    """Represent the different view types for export."""

    CURRENT_VIEW = "currentView"
    ALL = "all"


class NotionExporter:
    """Class to handle exporting Notion content."""

    def __init__(
        self,
        token_v2: str,
        file_token: str,
        pages: dict,
        export_directory: str = None,
        flatten_export_file_tree: bool = True,
        export_type: ExportType = ExportType.MARKDOWN,
        current_view_export_type: ViewExportType = ViewExportType.CURRENT_VIEW,
        include_files: bool = False,
        recursive: bool = True,
        workers: int = multiprocessing.cpu_count(),
        export_name: str = None,
    ):
        """
        Initializes the NotionExporter class.

        Args:
            token_v2 (str): The user's Notion V2 token.
            file_token (str): The user's file token for Notion.
            pages (dict): Dictionary of pages to be exported.
            export_directory (str, optional): Directory where exports will be saved. Defaults to the current directory.
            flatten_export_file_tree (bool, optional): If True, flattens the export file tree. Defaults to True.
            export_type (ExportType, optional): Type of export (e.g., MARKDOWN, HTML, PDF). Defaults to MARKDOWN.
            current_view_export_type (ViewExportType, optional): Type of view export (e.g., CURRENT_VIEW, ALL). Defaults to CURRENT_VIEW.
            include_files (bool, optional): If True, includes files in the export. Defaults to False.
            recursive (bool, optional): If True, exports will be recursive. Defaults to True.
            workers (int, optional): Number of worker threads for exporting. Defaults to the number of CPUs available.
            export_name (str, optional): Name of the export. Defaults to the current date and time.
        """

        self.export_name = (
            f"export-{datetime.now().strftime('%Y-%m-%d-%H-%M-%S')}"
            if not export_name
            else export_name
        )
        self.token_v2 = token_v2
        self.file_token = file_token
        self.include_files = include_files
        self.recursive = recursive
        self.pages = pages
        self.current_view_export_type = current_view_export_type
        self.flatten_export_file_tree = flatten_export_file_tree
        self.export_type = export_type
        self.export_directory = f"{export_directory}/" if export_directory else ""
        self.download_headers = {
            "content-type": "application/json",
            "cookie": f"file_token={self.file_token};",
        }
        self.query_headers = {
            "content-type": "application/json",
            "cookie": f"token_v2={self.token_v2};",
        }
        self.workers = workers
        os.makedirs(f"{self.export_directory}{self.export_name}", exist_ok=True)

    def _to_uuid_format(self, input_string: str) -> str:
        """
        Converts a string to UUID format.

        Args:
            input_string (str): The input string.

        Returns:
            str: The string in UUID format.
        """
        if (
            "-" == input_string[8]
            and "-" == input_string[13]
            and "-" == input_string[18]
            and "-" == input_string[23]
        ):
            return input_string
        return f"{input_string[:8]}-{input_string[8:12]}-{input_string[12:16]}-{input_string[16:20]}-{input_string[20:]}"

    def _get_format_options(
        self, export_type: ExportType, include_files: bool = False
    ) -> dict:
        """
        Retrieves format options based on the export type and whether to include files.

        Args:
            export_type (ExportType): Type of export (e.g., MARKDOWN, HTML, PDF).
            include_files (bool, optional): If True, includes files in the export. Defaults to False.

        Returns:
            dict: A dictionary containing format options.
        """
        format_options = {}
        if export_type == ExportType.PDF:
            format_options["pdfFormat"] = "Letter"

        if not include_files:
            format_options["includeContents"] = "no_files"

        return format_options

    def _export(self, page_id: str) -> str:
        """
        Initiates the export of a Notion page.

        Args:
            page_id (str): The ID of the Notion page.

        Returns:
            str: The task ID of the initiated export.
        """
        url = "https://www.notion.so/api/v3/enqueueTask"
        page_id = self._to_uuid_format(input_string=page_id)
        export_options = {
            "exportType": self.export_type,
            "locale": "en",
            "timeZone": "Europe/London",
            "collectionViewExportType": self.current_view_export_type,
            "flattenExportFiletree": self.flatten_export_file_tree,
        }

        # Update the exportOptions with format-specific options
        export_options.update(
            self._get_format_options(
                export_type=self.export_type, include_files=self.include_files
            )
        )

        payload = json.dumps(
            {
                "task": {
                    "eventName": "exportBlock",
                    "request": {
                        "block": {
                            "id": page_id,
                        },
                        "recursive": self.recursive,
                        "exportOptions": export_options,
                    },
                }
            }
        )

        response = requests.request(
            "POST", url, headers=self.query_headers, data=payload
        ).json()
        return response["taskId"]

    def _get_status(self, task_id: str) -> dict:
        """
        Fetches the status of an export task.

        Args:
            task_id (str): The ID of the export task.

        Returns:
            dict: A dictionary containing details about the task status.
        """
        url = "https://www.notion.so/api/v3/getTasks"

        payload = json.dumps({"taskIds": [task_id]})

        response = requests.request(
            "POST", url, headers=self.query_headers, data=payload
        ).json()

        if not response["results"]:
            # print(response)
            return {"state": "failure", "error": "No results found."}

        return response["results"][0]

    def _download(self, url: str):
        """
        Downloads an exported file from a given URL.

        Args:
            url (str): The URL of the exported file.
        """
        response = requests.request("GET", url, headers=self.download_headers)
        file_name = url.split("/")[-1][100:]
        with open(
            f"{self.export_directory}{self.export_name}/{file_name}",
            "wb",
        ) as f:
            f.write(response.content)

    def _process_page(self, page_details: tuple) -> dict:
        """
        Processes an individual Notion page for export.

        Args:
            page_details (tuple): Tuple containing the name and ID of the Notion page.

        Returns:
            dict: Details about the export status and any errors.
        """
        name, id = page_details
        task_id = self._export(id)

        status, state, error, pages_exported = self._wait_for_export_completion(
            task_id=task_id
        )
        if state == "failure":
            logging.error(f"Export failed for {name} with error: {error}")
            return {"state": state, "name": name, "error": error}

        export_url = status.get("status", {}).get("exportURL")
        if export_url:
            self._download(export_url)
        else:
            logging.warning(f"Failed to get exportURL for {name}")

        return {
            "state": state,
            "name": name,
            "exportURL": export_url,
            "pagesExported": pages_exported,
        }

    def _wait_for_export_completion(self, task_id: str) -> tuple[dict, str, str, int]:
        """
        Waits until a given export task completes or fails.

        Args:
            task_id (str): The ID of the export task.

        Returns:
            tuple: A tuple containing the status, state, error, and number of pages exported.
        """
        while True:
            status = self._get_status(task_id)

            if not status:
                time.sleep(5)
                continue
            state = status.get("state")
            error = status.get("error")
            if state == "failure" or status.get("status", {}).get("exportURL"):
                return (
                    status,
                    state,
                    error,
                    status.get("status", {}).get("pagesExported"),
                )
            time.sleep(5)

    def _unpack(self):
        """
        Unpacks and saves exported content from zip archives.
        """
        directory_path = f"{self.export_directory}{self.export_name}"
        for file in os.listdir(directory_path):
            if file.endswith(".zip"):
                full_file_path = os.path.join(directory_path, file)
                shutil.unpack_archive(full_file_path, directory_path, "zip")
                os.remove(full_file_path)

    def process(self):
        """
        Processes and exports all provided Notion pages.
        """
        logging.info(f"Exporting {len(self.pages)} pages...")

        with ThreadPoolExecutor(max_workers=self.workers) as executor:
            with tqdm(total=len(self.pages), dynamic_ncols=True) as pbar:
                futures = {
                    executor.submit(self._process_page, item): item
                    for item in self.pages.items()
                }
                for future in concurrent.futures.as_completed(futures):
                    result = future.result()
                    if result["state"] == "failure":
                        continue
                    name = result["name"]
                    pagesExported = result["pagesExported"]

                    pbar.set_postfix_str(
                        f"Exporting {name}... {pagesExported} pages already exported"
                    )
                    pbar.update(1)

        self._unpack()
