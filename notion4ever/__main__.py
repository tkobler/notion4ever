from notion4ever import notion2json
from notion4ever import structuring
from notion4ever import site_generation

import logging
import json
from pathlib import Path
import re
import shutil
import argparse
import os
import sys

from notion_client import Client

# Helper function to handle boolean arguments
def str_to_bool(value):
    if isinstance(value, bool):
        return value
    if value.lower() in {'true', 't', 'yes', 'y', '1'}:
        return True
    elif value.lower() in {'false', 'f', 'no', 'n', '0'}:
        return False
    else:
        raise argparse.ArgumentTypeError(f"Boolean value expected, got {value}")

_PAGE_ID_RE = re.compile(
    r"(?<![0-9a-fA-F])"
    r"[0-9a-fA-F]{8}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{12}"
    r"(?![0-9a-fA-F])")

def parse_page_ids(values) -> list:
    """Turns the -p values into a list of unique page IDs.

    Each value may contain several IDs separated by commas or whitespace, and
    each ID may be a bare ID or a full Notion URL (the ID is extracted from it;
    query strings such as ?v=... are ignored).
    """
    if values is None:
        return []
    if isinstance(values, str):
        values = [values]
    page_ids = []
    for value in values:
        for token in re.split(r"[,\s]+", value.strip()):
            if not token:
                continue
            path = re.split(r"[?#]", token, maxsplit=1)[0]
            matches = _PAGE_ID_RE.findall(path)
            page_id = matches[-1].replace("-", "").lower() if matches else token
            if page_id not in page_ids:
                page_ids.append(page_id)
    return page_ids

def download_raw_notion(page_id: str, notion: Client, filename: str) -> dict:
    """Stage 1. Downloads (or reads the cached) raw notion content."""
    raw_notion = {}
    if Path(filename).exists():
        logging.info(f"🤖 Reading existing raw notion content from {filename}.")
        with open(filename, "r") as f:
            raw_notion = json.load(f)
    else:
        logging.info("🤖 Started raw notion content parsing.")
        try:
            notion2json.notion_page_parser(page_id,
                                    notion=notion,
                                    filename=filename,
                                    notion_json=raw_notion)
        except BaseException:
            # Don't leave a partial dump that would be reused on the next run.
            Path(filename).unlink(missing_ok=True)
            raise
        logging.info(f"🤖 Downloaded raw notion content. Saved at {filename}")
    return raw_notion

def build_site(raw_notion: dict, config: dict, filename_structured: str):
    """Stages 2 and 3. Structures the raw content and generates the site."""
    # Stage 2. Structuring data
    logging.info(f"🤖 Started structuring notion data")
    structured_notion = structuring.structurize_notion_content(raw_notion,
                                                            config)
    with open(filename_structured, "w+", encoding="utf-8") as f:
        json.dump(structured_notion, f, ensure_ascii=False, indent=4)

    logging.info(f"🤖 Finished structuring notion data")

    if Path(filename_structured).exists():
        logging.info("🤖 Reading existing raw notion content.")
        with open(filename_structured, "r") as f:
            structured_notion = json.load(f)

    # Stage 3. Generating site from template and data
    if config["build_locally"]:
        structured_notion['base_url'] = \
            str(Path(config["output_dir"]).resolve())
    else:
        structured_notion['base_url'] = config["site_url"]

    logging.info(("🤖 Started generating site "
                 f"{'locally' if config['build_locally'] else ''} "
                 f"to {config['output_dir']}"))

    site_generation.generate_site(structured_notion, config)

    logging.info("🤖 Finished generating site.")

def root_page_title(raw_notion: dict) -> str:
    """Title of the root page (the first page downloaded)."""
    headers = structuring.parse_headers(raw_notion)
    return headers[list(raw_notion.keys())[0]]["title"]

def export_several_pages(page_ids: list, config: dict, notion: Client):
    """Exports each page as its own site in <output_dir>/<root page title>/."""
    base_output_dir = Path(config["output_dir"])
    base_site_url = config["site_url"]
    used_folders = set()
    failed = []

    for i, page_id in enumerate(page_ids, start=1):
        logging.info(f"🤖 [{i}/{len(page_ids)}] Exporting page {page_id}")
        try:
            filename = f"./notion_content_{page_id}.json"
            filename_structured = f"./notion_structured_{page_id}.json"
            raw_notion = download_raw_notion(page_id, notion, filename)

            folder = structuring.clean_url_string(root_page_title(raw_notion))
            candidate, n = folder, 2
            while candidate in used_folders:
                candidate, n = f"{folder}_{n}", n + 1
            folder = candidate
            used_folders.add(folder)

            page_config = dict(config)
            page_config["output_dir"] = str(base_output_dir / folder)
            if base_site_url:
                page_config["site_url"] = \
                    f"{base_site_url.rstrip('/')}/{folder}/"

            # Only this page's own folder is removed, never the whole output.
            if config["remove_before"] and Path(page_config["output_dir"]).exists():
                shutil.rmtree(page_config["output_dir"])
                logging.debug(f"🤖 Removed old site files in {page_config['output_dir']}")

            build_site(raw_notion, page_config, filename_structured)
            logging.info(f"🤖 [{i}/{len(page_ids)}] Exported to {page_config['output_dir']}")
        except Exception:
            logging.exception(f"🤖 [{i}/{len(page_ids)}] Failed to export page {page_id}")
            failed.append(page_id)

    done = len(page_ids) - len(failed)
    logging.info(f"🤖 Exported {done}/{len(page_ids)} pages to {base_output_dir}.")
    if failed:
        logging.error(f"🤖 Failed pages: {', '.join(failed)}")
        sys.exit(1)

def main():
    parser = argparse.ArgumentParser(description=(
        "Notion4ever: Export all your Notion content to markdown and HTML,"
        "and serve it as a static site."
    ))
    parser.add_argument('--notion_token', '-n',
        type=str, help="Set your Notion API token.",
        default=os.environ.get("NOTION_TOKEN"))
    parser.add_argument('--notion_page_id', '-p',
        type=str, nargs='+',
        help=("Page ID(s) or URL(s) of the target page(s), separated by spaces "
              "or commas. With several pages, each one is exported to "
              "<output_dir>/<page title>/."),
        default=os.environ.get("NOTION_PAGE_ID"))
    parser.add_argument('--output_dir', '-od',
        type=str, default="./_site",
        help="Directory to save the generated site.")
    parser.add_argument('--templates_dir', '-td',
        type=str, default="./_templates",
        help="Directory containing site templates.")
    parser.add_argument('--sass_dir', '-sd',
        type=str, default="./_sass",
        help="Directory for SASS files.")
    parser.add_argument('--build_locally', '-bl',
        type=str_to_bool, default=False,
        help="Build the site locally. (true/false)")
    parser.add_argument('--download_files', '-df',
        type=str_to_bool, default=True,
        help="Download files. (true/false)")
    parser.add_argument('--site_url', '-su',
        type=str, default=os.environ.get("SITE_URL"),
        help="Base URL of the site.")
    parser.add_argument('--remove_before', '-rb',
        type=str_to_bool, default=False,
        help="Remove existing files before generating the site. (true/false)")
    parser.add_argument('--include_footer', '-if',
        type=str_to_bool, default=os.environ.get("INCLUDE_FOOTER"),
        help="Include a footer in the site. (true/false)")
    parser.add_argument('--logging_level', '-ll',
        type=str, default="INFO", choices=["INFO", "DEBUG"],
        help="Logging level.")
    parser.add_argument('--include_search', '-is',
        type=str_to_bool, default=os.environ.get("INCLUDE_SEARCH"),
        help="Include a search feature in the site. (true/false)")

    config = vars(parser.parse_args())

    if config["logging_level"] == "DEBUG":
        llevel = logging.DEBUG
    else:
        llevel = logging.INFO

    logging.basicConfig(format="%(asctime)s %(levelname)s: %(message)s",
                    level=llevel)

    page_ids = parse_page_ids(config["notion_page_id"])
    if not page_ids:
        parser.error("no page ID given: use -p or set NOTION_PAGE_ID.")

    if len(page_ids) == 1:
        config["notion_page_id"] = page_ids[0]
        if config["remove_before"]:
            if Path(config["output_dir"]).exists():
                shutil.rmtree(config["output_dir"])
                logging.debug("🤖 Removed old site files")

    notion = Client(auth=config["notion_token"])
    logging.info("🤖 Notion authentification completed successfully.")

    if len(page_ids) > 1:
        export_several_pages(page_ids, config, notion)
        return

    # Single page: same behaviour and file names as before.
    raw_notion = download_raw_notion(config["notion_page_id"], notion,
                                     "./notion_content.json")
    build_site(raw_notion, config, "./notion_structured.json")

if __name__ == "__main__":
    main()
