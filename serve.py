#!/usr/bin/env python3
"""Persistent HTTP service for the Reuters/Bloomberg RSS resource pool."""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import mimetypes
import re
import sys
import threading
import time
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from src.rss_pool import (  # noqa: E402
    PRIMARY_RELATIONS,
    RELATION_RANK,
    PoolBuilder,
    apply_article_dimensions,
    article_content_level,
    article_relation,
    build_rss,
    classify_wire_item,
    deduplicate_items,
    fetch_url,
    iso_now,
    parse_rss,
)


def _normalized(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip().casefold()


def _query_terms(query: str) -> list[str]:
    quoted = re.findall(r'"([^"]+)"', query)
    remainder = re.sub(r'"[^"]+"', " ", query)
    terms = quoted + re.findall(r"[\w\-\.\u3040-\u30ff\u3400-\u9fff]+", remainder,
                                flags=re.UNICODE)
    return [_normalized(term) for term in terms if _normalized(term)]


def search_items(items: list[dict[str, Any]], query: str, limit: int = 50,
                 mode: str = "verified", relation: str = "",
                 publisher: str = "", found_at: str = "",
                 content_level: str = "", platform: str = "",
                 full_text_only: bool = False,
                 classification: str = "") -> list[dict[str, Any]]:
    """Search a resource-pool snapshot using AND terms and weighted fields."""
    terms = _query_terms(query)
    wanted_relations = {part.strip() for part in relation.split(",") if part.strip()}
    wanted_classes = {part.strip() for part in classification.split(",") if part.strip()}
    publisher_norm = _normalized(publisher)
    found_at_norm = _normalized(found_at)
    content_level_norm = _normalized(content_level)
    platform_norm = _normalized(platform)
    scored: list[tuple[float, str, dict[str, Any]]] = []
    for item in items:
        apply_article_dimensions(item)
        item_class = item.get("classification", "")
        item_relation = article_relation(item)
        item_content_level = article_content_level(item)
        if mode != "all" and item_relation not in PRIMARY_RELATIONS:
            continue
        if wanted_relations and item_relation not in wanted_relations:
            continue
        if wanted_classes and item_class not in wanted_classes:
            continue
        if publisher_norm and publisher_norm not in _normalized(item.get("publisher", "")):
            continue
        if found_at_norm and found_at_norm not in _normalized(
                item.get("found_at") or item.get("source_id", "")):
            continue
        if content_level_norm and item_content_level != content_level_norm:
            continue
        if platform_norm and platform_norm not in _normalized(item.get("platform", "")):
            continue
        if full_text_only and item_content_level != "full":
            continue
        title = _normalized(item.get("title", ""))
        summary = _normalized(item.get("summary", ""))
        body = _normalized(item.get("body", ""))
        metadata = _normalized(" ".join(str(item.get(key, "")) for key in (
            "publisher", "platform", "author", "source_id", "found_at",
            "relation", "content_level", "classification"
        )))
        haystack = " ".join((title, summary, body, metadata))
        if terms and not all(term in haystack for term in terms):
            continue
        score = 0.0
        for term in terms:
            score += 10.0 if term in title else 0.0
            score += 4.0 if term in summary else 0.0
            score += 1.0 if term in body else 0.0
            score += 2.0 if term in metadata else 0.0
        score += float(RELATION_RANK.get(item_relation, 0))
        scored.append((score, item.get("published", ""), item))
    scored.sort(key=lambda row: (row[0], row[1]), reverse=True)
    return [row[2] for row in scored[:max(1, min(int(limit), 200))]]


def _google_url(query: str, language: str = "en") -> str:
    if language == "ja":
        params = {"hl": "ja", "gl": "JP", "ceid": "JP:ja"}
    else:
        params = {"hl": "en-US", "gl": "US", "ceid": "US:en"}
    params["q"] = query
    return "https://news.google.com/rss/search?" + urllib.parse.urlencode(params)


def _bing_url(query: str) -> str:
    return "https://www.bing.com/news/search?" + urllib.parse.urlencode({
        "q": query, "format": "rss", "mkt": "en-US",
    })


def _remote_search_one(spec: dict[str, Any]) -> list[dict[str, Any]]:
    payload, _, _ = fetch_url(spec["url"], timeout=25, retries=1)
    rows = parse_rss(payload, spec)
    for row in rows:
        host = urllib.parse.urlparse(row.get("link", "")).netloc.casefold()
        if host == "reuters.com" or host.endswith(".reuters.com"):
            row["owned_by"] = "Reuters"
            row["source_kind"] = "rss"
        elif host == "bloomberg.com" or host.endswith(".bloomberg.com"):
            row["owned_by"] = "Bloomberg"
            row["source_kind"] = "rss"
        row.update(classify_wire_item(row))
        apply_article_dimensions(row)
    return rows


def live_search(query: str, limit: int = 50) -> list[dict[str, Any]]:
    """Run request-time Google/Bing RSS searches and return normalized items."""
    cleaned = re.sub(r"[\r\n\t]+", " ", query).strip()[:240]
    if not cleaned:
        return []
    specs = [
        {
            "id": "live_google_en", "platform": "Google News live",
            "kind": "google_discovery", "language": "en", "max_items": 60,
            "publisher_hint": "", "url": _google_url(
                f'{cleaned} (Reuters OR Bloomberg) when:7d'),
        },
        {
            "id": "live_google_ja", "platform": "Google News 日本 live",
            "kind": "google_discovery", "language": "ja", "max_items": 40,
            "publisher_hint": "", "url": _google_url(
                f'{cleaned} (Reuters OR Bloomberg OR ロイター OR ブルームバーグ) when:7d',
                "ja"),
        },
        {
            "id": "live_bing_official", "platform": "Bing News live",
            "kind": "bing_discovery", "language": "en", "max_items": 60,
            "publisher_hint": "", "url": _bing_url(
                f'{cleaned} (site:reuters.com OR site:bloomberg.com)'),
        },
    ]
    rows: list[dict[str, Any]] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
        futures = [executor.submit(_remote_search_one, spec) for spec in specs]
        for future in concurrent.futures.as_completed(futures):
            try:
                rows.extend(future.result())
            except Exception:
                continue
    result = deduplicate_items(rows)
    return result[:max(1, min(int(limit), 200))]


class ServiceState:
    def __init__(self, root: Path, workers: int, update_interval: int,
                 scheduler_enabled: bool):
        self.root = root
        self.workers = workers
        self.update_interval = max(60, update_interval)
        self.scheduler_enabled = scheduler_enabled
        self._manifest: dict[str, Any] = {}
        self._manifest_mtime = -1.0
        self._manifest_lock = threading.Lock()
        self._update_lock = threading.Lock()
        self._stop = threading.Event()
        self._scheduler_thread: threading.Thread | None = None
        self.update_status = {
            "running": False, "last_started": "", "last_finished": "",
            "last_error": "",
        }
        self.live_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}
        self.live_cache_lock = threading.Lock()

    def manifest(self) -> dict[str, Any]:
        path = self.root / "data" / "resource_pool.json"
        mtime = path.stat().st_mtime
        with self._manifest_lock:
            if not self._manifest or mtime != self._manifest_mtime:
                self._manifest = json.loads(path.read_text(encoding="utf-8"))
                for item in self._manifest.get("items", []):
                    apply_article_dimensions(item)
                self._manifest_mtime = mtime
            return self._manifest

    def run_update(self) -> bool:
        if not self._update_lock.acquire(blocking=False):
            return False
        self.update_status.update(running=True, last_started=iso_now(), last_error="")
        try:
            builder = PoolBuilder(
                config_path=self.root / "sources.json",
                output_dir=self.root / "data", workers=self.workers, enrich=True,
            )
            builder.run()
            with self._manifest_lock:
                self._manifest = {}
                self._manifest_mtime = -1.0
            self.update_status["last_finished"] = iso_now()
            return True
        except Exception as exc:
            self.update_status["last_error"] = f"{type(exc).__name__}: {exc}"
            self.update_status["last_finished"] = iso_now()
            return False
        finally:
            self.update_status["running"] = False
            self._update_lock.release()

    def start_scheduler(self, update_on_start: bool = False) -> None:
        if not self.scheduler_enabled:
            return

        def loop() -> None:
            if update_on_start:
                self.run_update()
            while not self._stop.wait(self.update_interval):
                self.run_update()

        self._scheduler_thread = threading.Thread(
            target=loop, name="rss-pool-updater", daemon=True)
        self._scheduler_thread.start()

    def stop(self) -> None:
        self._stop.set()

    def live(self, query: str, limit: int) -> list[dict[str, Any]]:
        key = f"{_normalized(query)}|{limit}"
        with self.live_cache_lock:
            cached = self.live_cache.get(key)
            if cached and time.time() - cached[0] < 60:
                return cached[1]
        rows = live_search(query, limit)
        with self.live_cache_lock:
            self.live_cache[key] = (time.time(), rows)
            if len(self.live_cache) > 100:
                oldest = sorted(self.live_cache, key=lambda k: self.live_cache[k][0])[:20]
                for old_key in oldest:
                    self.live_cache.pop(old_key, None)
        return rows


class PoolHandler(BaseHTTPRequestHandler):
    server_version = "ReutersBloombergPool/1.0"

    @property
    def state(self) -> ServiceState:
        return self.server.state  # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stderr.write("%s - - [%s] %s\n" % (
            self.client_address[0], self.log_date_time_string(), fmt % args))

    def _send(self, status: int, payload: bytes, content_type: str,
              extra_headers: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store" if "json" in content_type else "max-age=60")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("X-Content-Type-Options", "nosniff")
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(payload)

    def _json(self, value: Any, status: int = 200) -> None:
        self._send(status, json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _params(self) -> tuple[str, dict[str, list[str]]]:
        parsed = urllib.parse.urlparse(self.path)
        return parsed.path, urllib.parse.parse_qs(parsed.query)

    @staticmethod
    def _one(params: dict[str, list[str]], name: str, default: str = "") -> str:
        return (params.get(name) or [default])[0]

    def _search(self, params: dict[str, list[str]]) -> list[dict[str, Any]]:
        query = self._one(params, "q")[:240]
        try:
            limit = int(self._one(params, "limit", "50"))
        except ValueError:
            limit = 50
        manifest = self.state.manifest()
        return search_items(
            manifest.get("items", []), query=query, limit=limit,
            mode=self._one(params, "mode", "verified"),
            relation=self._one(params, "relation"),
            classification=self._one(params, "classification"),
            publisher=self._one(params, "publisher"),
            found_at=self._one(params, "found_at"),
            content_level=self._one(params, "content_level"),
            platform=self._one(params, "platform"),
            full_text_only=self._one(params, "full_text") in ("1", "true", "yes"),
        )

    def do_GET(self) -> None:  # noqa: N802
        path, params = self._params()
        if path in ("/health", "/api/health"):
            manifest = self.state.manifest()
            self._json({
                "status": "ok", "service_time": iso_now(),
                "scheduler_enabled": self.state.scheduler_enabled,
                "update_interval_seconds": self.state.update_interval,
                "update": self.state.update_status,
                "pool": manifest.get("summary", {}),
            })
            return
        if path == "/api/sources":
            manifest = self.state.manifest()
            self._json({"sources": manifest.get("sources", []),
                        "count": len(manifest.get("sources", []))})
            return
        if path == "/api/search":
            rows = self._search(params)
            self._json({
                "query": self._one(params, "q"), "returned": len(rows),
                "mode": self._one(params, "mode", "verified"), "items": rows,
            })
            return
        if path == "/rss/search.xml":
            rows = self._search(params)
            query = self._one(params, "q")
            payload = build_rss(
                rows, f"Reuters/Bloomberg search: {query or 'latest'}",
                self.path, "Dynamic search over the latest local resource-pool snapshot.")
            self._send(200, payload, "application/rss+xml; charset=utf-8")
            return
        if path in ("/api/live-search", "/rss/live-search.xml"):
            query = self._one(params, "q")[:240]
            if not query.strip():
                self._json({"error": "q is required"}, HTTPStatus.BAD_REQUEST)
                return
            try:
                limit = int(self._one(params, "limit", "50"))
            except ValueError:
                limit = 50
            rows = self.state.live(query, limit)
            if path.startswith("/rss/"):
                payload = build_rss(
                    rows, f"Reuters/Bloomberg live search: {query}", self.path,
                    "Request-time Google/Bing news search with source classification.")
                self._send(200, payload, "application/rss+xml; charset=utf-8")
            else:
                self._json({"query": query, "returned": len(rows),
                            "generated_at": iso_now(), "items": rows})
            return
        self._serve_static(path)

    def do_POST(self) -> None:  # noqa: N802
        path, _ = self._params()
        if path == "/api/shutdown":
            if self.client_address[0] not in ("127.0.0.1", "::1"):
                self._json({"error": "local request required"}, HTTPStatus.FORBIDDEN)
                return
            self._json({"accepted": True, "stopping_at": iso_now()}, HTTPStatus.ACCEPTED)
            threading.Thread(target=self.server.shutdown, name="http-shutdown",
                             daemon=True).start()
            return
        if path != "/api/update":
            self._json({"error": "route not found"}, HTTPStatus.NOT_FOUND)
            return
        if self.client_address[0] not in ("127.0.0.1", "::1"):
            self._json({"error": "local request required"}, HTTPStatus.FORBIDDEN)
            return
        if self.state.update_status["running"]:
            self._json({"accepted": False, "reason": "update already running"},
                       HTTPStatus.CONFLICT)
            return
        threading.Thread(target=self.state.run_update, name="manual-rss-update",
                         daemon=True).start()
        self._json({"accepted": True, "started_at": iso_now()}, HTTPStatus.ACCEPTED)

    def _serve_static(self, request_path: str) -> None:
        relative = "index.html" if request_path == "/" else request_path.lstrip("/")
        candidate = (self.state.root / relative).resolve()
        root = self.state.root.resolve()
        try:
            candidate.relative_to(root)
        except ValueError:
            self._json({"error": "invalid path"}, HTTPStatus.BAD_REQUEST)
            return
        if not candidate.is_file() or (relative != "index.html" and not relative.startswith("data/")):
            self._json({"error": "route not found"}, HTTPStatus.NOT_FOUND)
            return
        content_type = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
        if candidate.suffix == ".xml":
            content_type = "application/rss+xml"
        elif candidate.suffix == ".opml":
            content_type = "text/x-opml"
        self._send(200, candidate.read_bytes(), content_type +
                   ("; charset=utf-8" if content_type.startswith(("text/", "application/json",
                                                                  "application/rss")) else ""))


class PoolHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], state: ServiceState):
        super().__init__(address, PoolHandler)
        self.state = state


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--workers", type=int, default=12)
    parser.add_argument("--update-interval", type=int, default=1800)
    parser.add_argument("--no-scheduler", action="store_true")
    parser.add_argument("--update-on-start", action="store_true")
    args = parser.parse_args()
    state = ServiceState(
        ROOT, workers=max(1, args.workers), update_interval=args.update_interval,
        scheduler_enabled=not args.no_scheduler,
    )
    state.manifest()
    state.start_scheduler(update_on_start=args.update_on_start)
    server = PoolHTTPServer((args.host, args.port), state)
    print(f"HTTP service: http://{args.host}:{args.port}/", flush=True)
    print(f"Local search: http://{args.host}:{args.port}/api/search?q=RBI", flush=True)
    print(f"Live search: http://{args.host}:{args.port}/api/live-search?q=RBI", flush=True)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        state.stop()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
