import json
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.rss_pool import (  # noqa: E402
    PoolBuilder,
    apply_article_dimensions,
    build_rss,
    classify_wire_item,
    extract_json_object,
    generic_article_data,
    parse_bitget_article,
    parse_rss,
    parse_wordpress_posts,
    sitemap_locations,
    unwrap_bing_link,
    yahoo_article_data,
)


class ClassificationTests(unittest.TestCase):
    def base(self, **values):
        item = {
            "source_kind": "rss_filter", "owned_by": "", "title": "Headline",
            "summary": "", "body": "", "raw_description": "", "copyright": "",
            "author": "", "creator": "", "byline": "", "media_name": "",
        }
        item.update(values)
        return item

    def test_owned_endpoint(self):
        result = classify_wire_item(self.base(owned_by="Reuters", source_kind="rss"))
        self.assertEqual(result["classification"], "wire_original")
        self.assertEqual(result["publisher"], "Reuters")

    def test_yahoo_media_is_structured_syndication(self):
        result = classify_wire_item(self.base(
            owned_by="Bloomberg", source_kind="yahoo_news_media"))
        self.assertEqual(result["classification"], "wire_syndication")

    def test_exact_author_is_syndication(self):
        result = classify_wire_item(self.base(author="Reuters"))
        self.assertEqual(result["classification"], "wire_syndication")

    def test_bloomberg_reports_is_attribution(self):
        result = classify_wire_item(self.base(
            author="Example Desk", body="Bloomberg News reports that the talks resumed."))
        self.assertEqual(result["classification"], "wire_attribution")
        self.assertEqual(result["publisher"], "Bloomberg")

    def test_reuters_copyright_is_syndication(self):
        result = classify_wire_item(self.base(
            body="Reporting by A Person; Editing by B Person. © 2026 Reuters"))
        self.assertEqual(result["classification"], "wire_syndication")

    def test_discovery_only_stays_candidate(self):
        result = classify_wire_item(self.base(source_kind="bing_discovery"))
        self.assertEqual(result["classification"], "discovery_candidate")

    def test_brand_name_alone_is_not_wire_proof(self):
        result = classify_wire_item(self.base(
            title="BNN Bloomberg market update", author="BNN Bloomberg"))
        self.assertEqual(result["classification"], "unrelated")

    def test_compact_article_dimensions(self):
        item = {
            "source_id": "yahoo_news_jp_reuters", "platform": "Yahoo Japan",
            "classification": "wire_syndication", "summary": "Short summary",
            "body": "Full article paragraph " * 20,
        }
        apply_article_dimensions(item)
        self.assertEqual(item["found_at"], "yahoo_news_jp_reuters")
        self.assertEqual(item["relation"], "repost")
        self.assertEqual(item["content_level"], "full")


class ParserTests(unittest.TestCase):
    def test_extract_preloaded_state_with_braces_in_string(self):
        html = '<script>window.__PRELOADED_STATE__ = {"a":"x } y", "b":{"c":2}};</script>'
        self.assertEqual(extract_json_object(html, "window.__PRELOADED_STATE__")["b"]["c"], 2)

    def test_rss_content_and_creator(self):
        payload = b'''<?xml version="1.0"?><rss version="2.0"
          xmlns:content="http://purl.org/rss/1.0/modules/content/"
          xmlns:dc="http://purl.org/dc/elements/1.1/"><channel><item>
          <title>Test</title><link>https://example.test/a</link>
          <description>Short abstract</description><dc:creator>Reuters</dc:creator>
          <content:encoded><![CDATA[<p>Long article body here.</p>]]></content:encoded>
          <pubDate>Tue, 18 Aug 2026 01:00:00 GMT</pubDate></item></channel></rss>'''
        rows = parse_rss(payload, {"id": "x", "platform": "X", "kind": "rss_filter",
                                   "url": "https://x", "max_items": 5})
        self.assertEqual(rows[0]["author"], "Reuters")
        self.assertEqual(rows[0]["body"], "Long article body here.")
        self.assertEqual(rows[0]["published"], "2026-08-18T01:00:00Z")

    def test_bing_redirect_unwrap(self):
        url = "https://www.bing.com/news/apiclick.aspx?url=https%3A%2F%2Fexample.com%2Fa&x=1"
        self.assertEqual(unwrap_bing_link(url), "https://example.com/a")

    def test_jsonld_article(self):
        page = '''<html><head><script type="application/ld+json">
        {"@context":"https://schema.org","@type":"NewsArticle",
        "headline":"H","description":"S","articleBody":"Full body text",
        "author":{"name":"Reuters"},"datePublished":"2026-08-18T01:02:03Z"}
        </script></head></html>'''
        result = generic_article_data(page)
        self.assertEqual(result["author"], "Reuters")
        self.assertEqual(result["body"], "Full body text")

    def test_cgsp_wordpress_post_keeps_wire_author_and_body(self):
        posts = [{
            "link": "https://chinaglobalsouth.com/a/", "date_gmt": "2026-08-18T01:00:00",
            "title": {"rendered": "Reuters wire story"},
            "excerpt": {"rendered": "<p>Short summary</p>"},
            "content": {"rendered": "<p>Complete article body from the public API.</p>"},
            "_embedded": {"author": [{"name": "Reuters"}]},
        }]
        spec = {"id": "cgsp_wire", "platform": "CGSP", "kind": "wordpress_rest",
                "url": "https://chinaglobalsouth.com/wp-json/", "max_items": 10}
        rows = parse_wordpress_posts(json.dumps(posts), spec)
        self.assertEqual(rows[0]["author"], "Reuters")
        self.assertIn("Complete article body", rows[0]["body"])
        self.assertEqual(classify_wire_item(rows[0])["classification"], "wire_syndication")

    def test_cgsp_locked_post_is_summary_only(self):
        posts = [{
            "link": "https://chinaglobalsouth.com/b/", "title": {"rendered": "Locked"},
            "excerpt": {"rendered": "Public abstract"},
            "content": {"rendered": "Lead. Subscribe or log in to read the rest of this content."},
            "_embedded": {"author": [{"name": "Bloomberg"}]},
        }]
        spec = {"id": "cgsp_wire", "platform": "CGSP", "kind": "wordpress_rest",
                "url": "https://chinaglobalsouth.com/wp-json/"}
        row = parse_wordpress_posts(json.dumps(posts), spec)[0]
        self.assertEqual(row["body"], "")
        self.assertEqual(row["summary"], "Public abstract")

    def test_bitget_sitemap_and_article_parser(self):
        sitemap = '''<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
          <url><loc>https://www.bitget.com/news/detail/12560605538350</loc></url>
        </urlset>'''
        self.assertEqual(sitemap_locations(sitemap)[0],
                         "https://www.bitget.com/news/detail/12560605538350")
        page = '''<script type="application/ld+json">{
          "@type":"NewsArticle","headline":"Bitget Reuters story",
          "description":"Abstract","articleBody":"A sufficiently complete public article body.",
          "author":{"name":"Reuters"},"datePublished":"2026-08-18T01:00:00Z"
        }</script><script>{"detailProps":{"title":"Bitget Reuters story",
          "contentText":"A sufficiently complete public article body.",
          "sourceName":"Reuters","originPublishTime":"1787014800000"},
          "trendingNewsProps":[]}</script>'''
        spec = {"id": "bitget_news", "platform": "Bitget News",
                "kind": "bitget_sitemap", "url": "https://www.bitget.com/sitemap.xml"}
        row = parse_bitget_article(page, sitemap_locations(sitemap)[0], spec)
        self.assertEqual(row["author"], "Reuters")
        self.assertEqual(classify_wire_item(row)["classification"], "wire_syndication")

    def test_yahoo_finance_body(self):
        state = {
            "mainNewsArticleDetail": {
                "headline": "H", "summary": "Summary", "media": {"name": "Reuters"},
                "paragraphs": [{"headline": None, "body": "A sufficiently long article paragraph " * 4}],
            }
        }
        page = "window.__PRELOADED_STATE__ = " + json.dumps(state) + ";"
        result = yahoo_article_data(page)
        self.assertIn("sufficiently long", result["body"])
        self.assertEqual(result["author"], "Reuters")
        self.assertEqual(result["summary"], "Summary")

    def test_generated_rss_is_valid(self):
        row = {
            "id": "id1", "title": "A & B", "link": "https://example.test/a?x=1&y=2",
            "published": "2026-08-18T01:00:00Z", "summary": "S", "body": "P1\n\nP2",
            "author": "Reuters", "platform": "X", "source_id": "x",
            "publisher": "Reuters", "classification": "wire_syndication",
            "confidence": 0.99, "summary_source": "RSS", "body_source": "page",
            "canonical_url": "https://example.test/a", "evidence": ["author"],
        }
        parsed = ET.fromstring(build_rss([row], "T", "https://example.test", "D"))
        self.assertEqual(parsed.tag, "rss")
        self.assertEqual(parsed.findtext("./channel/item/title"), "A & B")
        namespace = {"wire": "urn:reuters-bloomberg-rss-pool:v1"}
        self.assertEqual(parsed.findtext("./channel/item/wire:relation", namespaces=namespace),
                         "repost")
        self.assertEqual(parsed.findtext("./channel/item/wire:contentLevel", namespaces=namespace),
                         "summary")


class BuilderTests(unittest.TestCase):
    @staticmethod
    def config(path: Path) -> None:
        path.write_text(json.dumps({"sources": [{
            "id": "fixture_reuters", "platform": "Fixture Reuters", "kind": "rss",
            "url": "https://example.test/rss", "owned_by": "Reuters", "max_items": 5,
        }]}), encoding="utf-8")

    @staticmethod
    def row() -> dict:
        return {
            "id": "fixture-1", "title": "Fixture headline",
            "link": "https://example.test/a", "canonical_url": "https://example.test/a",
            "published": "2026-08-18T01:00:00Z", "summary": "Fixture summary",
            "body": "Fixture full text paragraph " * 20, "author": "Reuters",
            "creator": "", "byline": "", "media_name": "", "copyright": "",
            "raw_description": "", "summary_source": "fixture", "body_source": "fixture",
            "source_id": "fixture_reuters", "platform": "Fixture Reuters",
            "source_kind": "rss", "owned_by": "Reuters",
        }

    def test_build_writes_four_primary_feeds_and_compact_fields(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = root / "sources.json"
            output = root / "data"
            self.config(config)
            builder = PoolBuilder(config, output, workers=1, enrich=False)
            builder.collect_source = lambda spec: ([self.row()], {
                "source_id": spec["id"], "platform": spec["platform"],
                "kind": spec["kind"], "url": spec["url"], "status": "ok",
                "fetched": 1, "accepted": 0, "elapsed_seconds": 0.01,
                "checked_at": "2026-08-18T01:00:00Z", "error": "",
            })
            manifest = builder.run()
            self.assertEqual(manifest["schema_version"], 2)
            self.assertEqual(manifest["items"][0]["relation"], "original")
            self.assertEqual(manifest["items"][0]["content_level"], "full")
            for filename in ("deduplicated.xml", "reuters.xml", "bloomberg.xml",
                             "fulltext.xml"):
                self.assertTrue((output / filename).is_file(), filename)
            self.assertFalse((output / "wire_original.xml").exists())
            self.assertEqual(json.loads((output / "last_attempt.json").read_text(
                encoding="utf-8"))["status"], "published")

    def test_failed_refresh_preserves_previous_manifest(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = root / "sources.json"
            output = root / "data"
            output.mkdir()
            self.config(config)
            previous = '{"sentinel":"last-good"}'
            (output / "resource_pool.json").write_text(previous, encoding="utf-8")
            builder = PoolBuilder(config, output, workers=1, enrich=False)
            builder.collect_source = lambda spec: ([], {
                "source_id": spec["id"], "platform": spec["platform"],
                "kind": spec["kind"], "url": spec["url"], "status": "error",
                "fetched": 0, "accepted": 0, "elapsed_seconds": 0.01,
                "checked_at": "2026-08-18T01:00:00Z", "error": "fixture failure",
            })
            with self.assertRaisesRegex(RuntimeError, "last good snapshot preserved"):
                builder.run()
            self.assertEqual((output / "resource_pool.json").read_text(encoding="utf-8"),
                             previous)
            self.assertEqual(json.loads((output / "last_attempt.json").read_text(
                encoding="utf-8"))["status"], "rejected")


if __name__ == "__main__":
    unittest.main()
