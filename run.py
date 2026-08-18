#!/usr/bin/env python3
"""Build the standalone Reuters/Bloomberg resource pool."""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from src.rss_pool import PoolBuilder  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=str(ROOT / "sources.json"))
    parser.add_argument("--output", default=str(ROOT / "data"))
    parser.add_argument("--workers", type=int, default=10)
    parser.add_argument("--quick", action="store_true",
                        help="skip article-page enrichment")
    parser.add_argument("--source", action="append", default=[],
                        help="run selected source id; repeatable")
    args = parser.parse_args()
    builder = PoolBuilder(
        config_path=Path(args.config), output_dir=Path(args.output),
        workers=max(1, args.workers), enrich=not args.quick,
        selected_sources=set(args.source),
    )
    result = builder.run()
    print(json.dumps(result["summary"], ensure_ascii=False, indent=2))
    return 0 if result["summary"]["sources_ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
