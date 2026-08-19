import json
import sys
import threading
import unittest
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from serve import PoolHTTPServer, ServiceState, search_items  # noqa: E402


class SearchTests(unittest.TestCase):
    def setUp(self):
        self.items = [
            {
                "title": "India central bank supports rupee", "summary": "RBI action",
                "body": "", "publisher": "Reuters", "platform": "CNA",
                "author": "Reuters", "source_id": "cna", "published": "2026-08-18T01:00:00Z",
                "classification": "wire_syndication", "relation": "repost",
                "found_at": "cna", "content_level": "summary", "has_full_text": False,
            },
            {
                "title": "India market commentary", "summary": "Bloomberg reported the move",
                "body": "full text", "publisher": "Bloomberg", "platform": "Example",
                "author": "Desk", "source_id": "example", "published": "2026-08-18T02:00:00Z",
                "classification": "discovery_candidate", "relation": "unknown",
                "found_at": "example", "content_level": "full", "has_full_text": True,
            },
        ]

    def test_verified_is_default(self):
        rows = search_items(self.items, "India", mode="verified")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["publisher"], "Reuters")

    def test_all_includes_candidates(self):
        rows = search_items(self.items, "India", mode="all")
        self.assertEqual(len(rows), 2)

    def test_field_filters(self):
        rows = search_items(self.items, "India", mode="all", publisher="Bloomberg",
                            full_text_only=True)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["platform"], "Example")

    def test_compact_filters(self):
        rows = search_items(self.items, "India", mode="all", relation="repost",
                            found_at="cna", content_level="summary")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["publisher"], "Reuters")


class HTTPIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.state = ServiceState(ROOT, workers=2, update_interval=1800,
                                 scheduler_enabled=False)
        manifest_path = ROOT / "data" / "resource_pool.json"
        cls.state._manifest = {
            "schema_version": 2,
            "summary": {"sources_total": 39, "items_total": 1},
            "sources": [],
            "items": [{
                "id": "http-fixture", "title": "India fixture headline",
                "link": "https://example.test/india", "summary": "RBI fixture",
                "body": "", "publisher": "Reuters", "platform": "Fixture",
                "author": "Reuters", "source_id": "fixture",
                "found_at": "fixture", "published": "2026-08-18T01:00:00Z",
                "classification": "wire_syndication", "relation": "repost",
                "content_level": "summary", "has_full_text": False,
            }],
        }
        cls.state._manifest_mtime = manifest_path.stat().st_mtime
        cls.server = PoolHTTPServer(("127.0.0.1", 0), cls.state)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.state.stop()
        cls.thread.join(timeout=3)

    def get(self, path):
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}", timeout=10) as response:
            return response.status, response.headers.get("Content-Type", ""), response.read()

    def test_health(self):
        status, content_type, payload = self.get("/api/health")
        data = json.loads(payload)
        self.assertEqual(status, 200)
        self.assertIn("application/json", content_type)
        self.assertEqual(data["status"], "ok")
        self.assertEqual(data["pool"]["sources_total"], 39)

    def test_local_search_json(self):
        status, _, payload = self.get("/api/search?q=India&mode=all&limit=5")
        data = json.loads(payload)
        self.assertEqual(status, 200)
        self.assertLessEqual(data["returned"], 5)
        self.assertTrue(data["items"])

    def test_dynamic_search_rss(self):
        status, content_type, payload = self.get("/rss/search.xml?q=India&mode=all&limit=5")
        self.assertEqual(status, 200)
        self.assertIn("application/rss+xml", content_type)
        self.assertEqual(ET.fromstring(payload).tag, "rss")

    def test_static_feed(self):
        status, content_type, payload = self.get("/data/deduplicated.xml")
        self.assertEqual(status, 200)
        self.assertIn("application/rss+xml", content_type)
        self.assertEqual(ET.fromstring(payload).tag, "rss")


if __name__ == "__main__":
    unittest.main()
