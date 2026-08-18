import json
import sys
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.rss_pool import (  # noqa: E402
    build_rss,
    classify_wire_item,
    extract_json_object,
    generic_article_data,
    parse_rss,
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


if __name__ == "__main__":
    unittest.main()
