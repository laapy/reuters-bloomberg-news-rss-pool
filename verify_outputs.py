#!/usr/bin/env python3
"""Validate generated JSON, OPML, and every RSS document."""

import argparse
import json
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"


def main(data_dir: Path = DATA) -> int:
    errors = []
    manifest_path = data_dir / "resource_pool.json"
    if not manifest_path.exists():
        errors.append("resource_pool.json is missing")
        manifest = {"items": [], "sources": [], "summary": {}}
    else:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    required_feeds = {
        "deduplicated.xml", "reuters.xml", "bloomberg.xml", "fulltext.xml",
    }
    for filename in sorted(required_feeds):
        if not (data_dir / filename).exists():
            errors.append(f"{filename} is missing")
    xml_files = sorted(data_dir.glob("*.xml")) + sorted((data_dir / "feeds").glob("*.xml"))
    for path in xml_files:
        try:
            root = ET.parse(path).getroot()
            if root.tag != "rss":
                errors.append(f"{path}: root is {root.tag}")
        except ET.ParseError as exc:
            errors.append(f"{path}: {exc}")
    try:
        ET.parse(data_dir / "resource_pool.opml")
    except (ET.ParseError, OSError) as exc:
        errors.append(f"resource_pool.opml: {exc}")
    items = manifest.get("items", [])
    if manifest.get("schema_version") != 2:
        errors.append(f"schema_version is {manifest.get('schema_version')}, expected 2")
    for index, item in enumerate(items):
        for key in (
            "id", "title", "link", "platform", "found_at", "relation",
            "content_level", "classification", "confidence",
        ):
            if key not in item or item[key] == "":
                errors.append(f"item {index}: empty {key}")
        if item.get("relation") not in {"original", "repost", "mention", "unknown"}:
            errors.append(f"item {index}: unknown relation")
        if item.get("content_level") not in {"full", "summary", "link_only"}:
            errors.append(f"item {index}: unknown content_level")
        if item.get("classification") not in {
            "wire_original", "wire_syndication", "wire_attribution", "discovery_candidate"
        }:
            errors.append(f"item {index}: unknown classification")
    output = {
        "rss_files": len(xml_files), "items": len(items),
        "sources": len(manifest.get("sources", [])), "errors": errors,
    }
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 1 if errors else 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", type=Path, default=DATA,
                        help="generated data directory to validate")
    args = parser.parse_args()
    raise SystemExit(main(args.data))
