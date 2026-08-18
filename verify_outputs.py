#!/usr/bin/env python3
"""Validate generated JSON, OPML, and every RSS document."""

import json
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"


def main() -> int:
    errors = []
    manifest_path = DATA / "resource_pool.json"
    if not manifest_path.exists():
        errors.append("resource_pool.json is missing")
        manifest = {"items": [], "sources": [], "summary": {}}
    else:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    xml_files = sorted(DATA.glob("*.xml")) + sorted((DATA / "feeds").glob("*.xml"))
    for path in xml_files:
        try:
            root = ET.parse(path).getroot()
            if root.tag != "rss":
                errors.append(f"{path}: root is {root.tag}")
        except ET.ParseError as exc:
            errors.append(f"{path}: {exc}")
    try:
        ET.parse(DATA / "resource_pool.opml")
    except (ET.ParseError, OSError) as exc:
        errors.append(f"resource_pool.opml: {exc}")
    items = manifest.get("items", [])
    for index, item in enumerate(items):
        for key in ("id", "title", "link", "platform", "classification", "confidence"):
            if key not in item or item[key] == "":
                errors.append(f"item {index}: empty {key}")
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
    raise SystemExit(main())
