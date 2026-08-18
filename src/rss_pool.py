#!/usr/bin/env python3
"""Build a provenance-aware Reuters/Bloomberg RSS resource pool.

The module deliberately separates a wire-service article from a publisher's
own article that merely cites a wire report.  Publisher-owned endpoints and
Yahoo Japan's publisher-specific media pages are treated as structured
evidence.  On other platforms an exact author/source credit or a retained
copyright/byline is required for ``wire_syndication``; phrases such as
"Bloomberg News reports" are classified as ``wire_attribution``.
"""

from __future__ import annotations

import concurrent.futures
import datetime as dt
import email.utils
import hashlib
import html as html_lib
import json
import os
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Iterable


USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36 "
    "ReutersBloombergRSSPool/1.0"
)
HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/rss+xml,"
              "application/atom+xml,application/xml;q=0.9,*/*;q=0.7",
    "Accept-Language": "en-US,en;q=0.9,ja;q=0.8,zh-CN;q=0.7",
}
CONTENT_NS = "http://purl.org/rss/1.0/modules/content/"
DC_NS = "http://purl.org/dc/elements/1.1/"
WIRE_NS = "urn:reuters-bloomberg-rss-pool:v1"
ET.register_namespace("content", CONTENT_NS)
ET.register_namespace("dc", DC_NS)
ET.register_namespace("wire", WIRE_NS)

WIRE_NAMES = {
    "reuters": "Reuters",
    "thomsonreuters": "Reuters",
    "reutersnews": "Reuters",
    "reutersstaff": "Reuters",
    "ロイター": "Reuters",
    "ロイター通信": "Reuters",
    "bloomberg": "Bloomberg",
    "bloombergnews": "Bloomberg",
    "ブルームバーグ": "Bloomberg",
}

WEAK_PATTERNS = {
    "Reuters": [
        r"\baccording\s+to\s+(?:a\s+report\s+(?:from|by)\s+)?Reuters\b",
        r"\bReuters\s+(?:reported|reports|says|said|wrote)\b",
        r"\bas\s+(?:first\s+)?reported\s+by\s+Reuters\b",
        r"\bciting\s+Reuters\b",
        r"ロイター(?:通信)?(?:が|の)(?:報じ|伝え|報道|取材)",
        r"ロイター(?:通信)?によると",
    ],
    "Bloomberg": [
        r"\baccording\s+to\s+(?:a\s+report\s+(?:from|by)\s+)?Bloomberg(?:\s+News)?\b",
        r"\bBloomberg(?:\s+News)?\s+(?:reported|reports|says|said|wrote)\b",
        r"\bas\s+(?:first\s+)?reported\s+by\s+Bloomberg(?:\s+News)?\b",
        r"\bciting\s+Bloomberg(?:\s+News)?\b",
        r"ブルームバーグ(?:が|の)(?:報じ|伝え|報道|取材)",
        r"ブルームバーグによると",
    ],
}

STRONG_PATTERNS = {
    "Reuters": [
        r"\bSource\s*:\s*Reuters\b",
        r"\b(?:Copyright|©)\s*(?:\d{4}\s*)?(?:Thomson\s+)?Reuters\b",
        r"\bReporting\s+by\b.{0,300}\bEditing\s+by\b",
        r"^\s*(?:By\s+[^\n]{1,120},?\s+)?Reuters\s*[-—:]",
        r"^\s*\([^\n)]{0,80}\)\s*[-—]\s*Reuters\b",
        r"(?:提供|配信|出典)\s*[：:]\s*ロイター(?:通信)?",
        r"^\s*[（(]ロイター[）)]",
    ],
    "Bloomberg": [
        r"\bSource\s*:\s*Bloomberg(?:\s+News)?\b",
        r"\b(?:Copyright|©)\s*(?:\d{4}\s*)?Bloomberg\b",
        r"^\s*(?:By\s+[^\n]{1,120},?\s+)?Bloomberg(?:\s+News)?\s*[-—:]",
        r"(?:提供|配信|出典)\s*[：:]\s*ブルームバーグ",
        r"^\s*[（(]ブルームバーグ[）)]",
    ],
}


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def iso_now() -> str:
    return utc_now().isoformat(timespec="seconds").replace("+00:00", "Z")


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def clean_text(value: Any) -> str:
    if value is None:
        return ""
    text = str(value)
    text = re.sub(r"(?is)<(?:script|style)[^>]*>.*?</(?:script|style)>", " ", text)
    text = re.sub(r"(?i)<br\s*/?>|</p\s*>|</div\s*>|</li\s*>", "\n", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = html_lib.unescape(text)
    text = text.replace("\u200b", "").replace("\u200c", "").replace("\u2060", "")
    text = re.sub(r"[\t\r\f\v ]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def normalize_credit(value: str) -> str:
    value = clean_text(value).casefold()
    value = re.sub(r"\b(?:news|newswire|staff|agency)\b", "", value)
    return re.sub(r"[^a-z0-9\u3040-\u30ff\u3400-\u9fff]+", "", value)


def exact_wire_credit(values: Iterable[str]) -> tuple[str, str] | tuple[None, None]:
    for value in values:
        normalized = normalize_credit(value)
        publisher = WIRE_NAMES.get(normalized)
        if publisher:
            return publisher, f"structured credit: {clean_text(value)}"
    return None, None


def classify_wire_item(item: dict[str, Any]) -> dict[str, Any]:
    """Return classification, publisher, confidence, and evidence.

    Classification values:
      * wire_original: Reuters/Bloomberg-owned endpoint
      * wire_syndication: an exact third-party wire credit or retained byline
      * wire_attribution: another publisher cites/reports the wire's reporting
      * discovery_candidate: search-index discovery awaiting stronger evidence
      * unrelated: no wire evidence
    """
    kind = item.get("source_kind", "")
    owned_by = clean_text(item.get("owned_by", ""))
    if owned_by in ("Reuters", "Bloomberg"):
        if kind in ("yahoo_news_media", "yahoo_finance_media"):
            return {
                "classification": "wire_syndication",
                "publisher": owned_by,
                "confidence": 1.0,
                "evidence": [f"publisher-specific Yahoo media endpoint: {owned_by}"],
            }
        return {
            "classification": "wire_original",
            "publisher": owned_by,
            "confidence": 1.0,
            "evidence": [f"publisher-owned endpoint: {owned_by}"],
        }

    structured = [
        item.get("author", ""), item.get("creator", ""),
        item.get("byline", ""), item.get("media_name", ""),
    ]
    publisher, evidence = exact_wire_credit(structured)
    if publisher:
        return {
            "classification": "wire_syndication", "publisher": publisher,
            "confidence": 0.99, "evidence": [evidence],
        }

    evidence_fields = [clean_text(item.get(k, "")) for k in (
        "title", "summary", "body", "raw_description", "copyright"
    ) if item.get(k)]
    for publisher_name, patterns in STRONG_PATTERNS.items():
        for pattern in patterns:
            for evidence_text in evidence_fields:
                match = re.search(pattern, evidence_text, re.I | re.S)
                if match:
                    return {
                        "classification": "wire_syndication",
                        "publisher": publisher_name,
                        "confidence": 0.94,
                        "evidence": [f"retained wire byline/source: {clean_text(match.group(0))[:180]}"],
                    }

    weak_hits: list[tuple[str, str]] = []
    for publisher_name, patterns in WEAK_PATTERNS.items():
        for pattern in patterns:
            matched = False
            for evidence_text in evidence_fields:
                match = re.search(pattern, evidence_text, re.I | re.S)
                if match:
                    weak_hits.append((publisher_name, clean_text(match.group(0))[:180]))
                    matched = True
                    break
            if matched:
                break
    if weak_hits:
        publisher_name, phrase = weak_hits[0]
        return {
            "classification": "wire_attribution", "publisher": publisher_name,
            "confidence": 0.86,
            "evidence": [f"reporting reference, not an exact wire credit: {phrase}"],
        }

    if kind in ("bing_discovery", "google_discovery"):
        hint = clean_text(item.get("publisher_hint", ""))
        return {
            "classification": "discovery_candidate",
            "publisher": hint if hint in ("Reuters", "Bloomberg") else "",
            "confidence": 0.35,
            "evidence": [f"{kind.replace('_', ' ')} query match only"],
        }
    return {
        "classification": "unrelated", "publisher": "",
        "confidence": 0.0, "evidence": ["no Reuters/Bloomberg credit found"],
    }


def extract_json_object(text: str, marker: str) -> dict[str, Any]:
    index = text.find(marker)
    if index < 0:
        raise ValueError(f"marker missing: {marker}")
    start = text.find("{", index)
    if start < 0:
        raise ValueError(f"object start missing: {marker}")
    depth = 0
    in_string = False
    escape = False
    for pos in range(start, len(text)):
        char = text[pos]
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
        else:
            if char == '"':
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    return json.loads(text[start:pos + 1])
    raise ValueError(f"unbalanced object: {marker}")


def parse_datetime(value: str) -> str:
    value = clean_text(value)
    if not value:
        return ""
    try:
        parsed = email.utils.parsedate_to_datetime(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        return parsed.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")
    except (TypeError, ValueError, OverflowError):
        pass
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        return parsed.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")
    except ValueError:
        return value


def rfc822(value: str) -> str:
    value = parse_datetime(value)
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        parsed = utc_now()
    return email.utils.format_datetime(parsed.astimezone(dt.timezone.utc))


def stable_id(*values: str) -> str:
    text = "\x1f".join(clean_text(v).casefold() for v in values if v)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:24]


def fetch_url(url: str, timeout: int = 30, retries: int = 2) -> tuple[bytes, str, str]:
    last_error: Exception | None = None
    for attempt in range(retries + 1):
        try:
            request = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(
                    request, timeout=timeout, context=ssl.create_default_context()) as response:
                return response.read(), response.geturl(), response.headers.get("Content-Type", "")
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_error = exc
            if attempt < retries:
                time.sleep(0.5 * (attempt + 1))
    raise RuntimeError(f"fetch failed: {url}: {last_error!r}")


def rss_child(node: ET.Element, *names: str) -> str:
    wanted = {name.casefold() for name in names}
    for child in node:
        if local_name(child.tag) not in wanted:
            continue
        if local_name(child.tag) == "link" and child.attrib.get("href"):
            return child.attrib["href"].strip()
        value = "".join(child.itertext()).strip()
        if value:
            return value
    return ""


def unwrap_bing_link(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    if "bing.com" in parsed.netloc.casefold():
        target = urllib.parse.parse_qs(parsed.query).get("url", [""])[0]
        if target.startswith(("http://", "https://")):
            return target
    return url


def parse_rss(payload: bytes, spec: dict[str, Any]) -> list[dict[str, Any]]:
    root = ET.fromstring(payload)
    rows: list[dict[str, Any]] = []
    maximum = int(spec.get("max_items", 100))
    for node in root.iter():
        if local_name(node.tag) not in ("item", "entry"):
            continue
        title = clean_text(rss_child(node, "title"))
        link = rss_child(node, "link").strip()
        if not title or not link:
            continue
        link = unwrap_bing_link(link)
        description_raw = rss_child(node, "description", "summary")
        encoded_raw = rss_child(node, "encoded", "content")
        author = clean_text(rss_child(node, "creator", "author"))
        source = clean_text(rss_child(node, "source"))
        body = clean_text(encoded_raw)
        summary = clean_text(description_raw)
        if summary == body or len(summary) > 1200:
            summary = summary[:1200].rsplit(" ", 1)[0]
        published = parse_datetime(rss_child(
            node, "pubDate", "published", "updated", "modified", "date"))
        row = {
            "id": stable_id(link, title), "title": title, "link": link,
            "canonical_url": link, "platform": spec["platform"],
            "source_id": spec["id"], "source_kind": spec["kind"],
            "source_url": spec.get("url", ""), "owned_by": spec.get("owned_by", ""),
            "publisher_hint": spec.get("publisher_hint", ""),
            "language": spec.get("language", ""), "published": published,
            "author": author, "creator": author, "rss_source": source,
            "media_name": "", "summary": summary, "summary_source": "RSS description",
            "body": body, "body_source": "RSS content:encoded" if body else "",
            "raw_description": clean_text(description_raw), "copyright": "",
            "discovery_method": spec["kind"],
        }
        rows.append(row)
        if len(rows) >= maximum:
            break
    return rows


def article_time_from_yahoo(entry: dict[str, Any]) -> str:
    thumb = entry.get("thumbUrl", "") or ""
    match = re.search(r"/amd-img/(\d{4})(\d{2})(\d{2})-", thumb)
    hour = minute = 0
    time_match = re.match(r"(\d{1,2}):(\d{2})", entry.get("timeString", "") or
                          entry.get("createTime", "") or "")
    if time_match:
        hour, minute = map(int, time_match.groups())
    if match:
        year, month, day = map(int, match.groups())
        try:
            value = dt.datetime(year, month, day, hour, minute,
                                tzinfo=dt.timezone(dt.timedelta(hours=9)))
            return value.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")
        except ValueError:
            pass
    now = dt.datetime.now(dt.timezone(dt.timedelta(hours=9)))
    date_match = re.match(r"(\d{1,2})/(\d{1,2})", entry.get("dateString", "") or "")
    if date_match:
        month, day = map(int, date_match.groups())
        try:
            value = dt.datetime(now.year, month, day, hour, minute, tzinfo=now.tzinfo)
            if value > now + dt.timedelta(days=2):
                value = value.replace(year=now.year - 1)
            return value.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")
        except ValueError:
            pass
    return now.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def collect_yahoo_news_media(spec: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    pages = int(spec.get("pages", 2))
    for page in range(1, pages + 1):
        url = spec["url"] if page == 1 else f"{spec['url']}?page={page}"
        payload, _, _ = fetch_url(url)
        state = extract_json_object(payload.decode("utf-8", "replace"),
                                    "window.__PRELOADED_STATE__")
        entries = state.get("mediaArticleList", {}).get("list", [])
        if not entries:
            break
        for entry in entries:
            if entry.get("isPay"):
                continue
            title = clean_text(entry.get("headline", ""))
            link = clean_text(entry.get("newsLink", ""))
            if not title or not link:
                continue
            media_name = clean_text(entry.get("mediaName", ""))
            rows.append({
                "id": stable_id(link, title), "title": title, "link": link,
                "canonical_url": link, "platform": spec["platform"],
                "source_id": spec["id"], "source_kind": spec["kind"],
                "source_url": spec["url"], "owned_by": spec["owned_by"],
                "publisher_hint": spec["owned_by"], "language": spec.get("language", "ja"),
                "published": article_time_from_yahoo(entry), "author": media_name,
                "creator": media_name, "media_name": media_name, "rss_source": "",
                "summary": "", "summary_source": "", "body": "", "body_source": "",
                "raw_description": "", "copyright": "",
                "discovery_method": "Yahoo __PRELOADED_STATE__ mediaArticleList",
            })
            if len(rows) >= int(spec.get("max_items", 50)):
                return rows
        if len(entries) < 25:
            break
    return rows


def collect_yahoo_finance_media(spec: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    pages = int(spec.get("pages", 2))
    for page in range(1, pages + 1):
        url = spec["url"] if page == 1 else f"{spec['url']}?page={page}"
        payload, _, _ = fetch_url(url)
        state = extract_json_object(payload.decode("utf-8", "replace"),
                                    "window.__PRELOADED_STATE__")
        news = state.get("mainNewsMediaResult", {}).get("news", {})
        entries = news.get("articles", [])
        if not entries:
            break
        for entry in entries:
            if entry.get("isPaidArticle"):
                continue
            title = clean_text(entry.get("headline", ""))
            link = clean_text(entry.get("link", ""))
            if not title or not link:
                continue
            media_name = clean_text(entry.get("mediaName", ""))
            yahoo_entry = {
                "timeString": entry.get("createTime", ""),
                "dateString": entry.get("createDate", ""),
                "thumbUrl": (entry.get("thumbnail") or {}).get("url", ""),
            }
            rows.append({
                "id": stable_id(link, title), "title": title, "link": link,
                "canonical_url": link, "platform": spec["platform"],
                "source_id": spec["id"], "source_kind": spec["kind"],
                "source_url": spec["url"], "owned_by": spec["owned_by"],
                "publisher_hint": spec["owned_by"], "language": spec.get("language", "ja"),
                "published": article_time_from_yahoo(yahoo_entry), "author": media_name,
                "creator": media_name, "media_name": media_name, "rss_source": "",
                "summary": clean_text(entry.get("summary", "")),
                "summary_source": "Yahoo embedded state" if entry.get("summary") else "",
                "body": "", "body_source": "", "raw_description": "", "copyright": "",
                "discovery_method": "Yahoo Finance __PRELOADED_STATE__ mainNewsMediaResult",
            })
            if len(rows) >= int(spec.get("max_items", 40)):
                return rows
    return rows


def discovery_url(spec: dict[str, Any]) -> str:
    if spec["kind"] == "bing_discovery":
        terms = spec.get("query_terms", '("Reuters" OR "Bloomberg")')
        query = f"site:{spec['domain']} {terms}"
        return "https://www.bing.com/news/search?" + urllib.parse.urlencode({
            "q": query, "format": "rss", "mkt": "en-US",
        })
    language = spec.get("language", "en")
    if language.startswith("ja"):
        locale = {"hl": "ja", "gl": "JP", "ceid": "JP:ja"}
    elif language.startswith("zh"):
        locale = {"hl": "zh-CN", "gl": "CN", "ceid": "CN:zh-Hans"}
    else:
        locale = {"hl": "en-US", "gl": "US", "ceid": "US:en"}
    locale["q"] = spec["query"]
    return "https://news.google.com/rss/search?" + urllib.parse.urlencode(locale)


def google_site_discovery_url(spec: dict[str, Any]) -> str:
    language = spec.get("language", "en")
    if language.startswith("ja"):
        locale = {"hl": "ja", "gl": "JP", "ceid": "JP:ja"}
    elif language.startswith("zh"):
        locale = {"hl": "zh-TW", "gl": "TW", "ceid": "TW:zh-Hant"}
    elif language.startswith("es"):
        locale = {"hl": "es-419", "gl": "US", "ceid": "US:es-419"}
    else:
        locale = {"hl": "en-US", "gl": "US", "ceid": "US:en"}
    terms = spec.get("query_terms", "(Reuters OR Bloomberg)")
    locale["q"] = f"site:{spec['domain']} {terms} when:30d"
    return "https://news.google.com/rss/search?" + urllib.parse.urlencode(locale)


def jsonld_article(html_text: str) -> dict[str, Any]:
    scripts = re.findall(
        r'(?is)<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html_text,
    )
    candidates: list[dict[str, Any]] = []

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            item_type = value.get("@type", "")
            types = item_type if isinstance(item_type, list) else [item_type]
            if any(str(x).casefold() in (
                    "newsarticle", "article", "reportagenewsarticle") for x in types):
                candidates.append(value)
            graph = value.get("@graph")
            if graph:
                visit(graph)
        elif isinstance(value, list):
            for entry in value:
                visit(entry)

    for script in scripts:
        try:
            visit(json.loads(html_lib.unescape(script).strip()))
        except (json.JSONDecodeError, TypeError):
            continue
    if not candidates:
        return {}
    return max(candidates, key=lambda x: len(str(x.get("articleBody", ""))) +
               len(str(x.get("description", ""))))


def author_names(value: Any) -> str:
    if isinstance(value, str):
        return clean_text(value)
    if isinstance(value, dict):
        return clean_text(value.get("name", ""))
    if isinstance(value, list):
        return ", ".join(filter(None, (author_names(item) for item in value)))
    return ""


def meta_content(html_text: str, *names: str) -> str:
    for name in names:
        escaped = re.escape(name)
        patterns = [
            rf'(?is)<meta[^>]+(?:name|property)=["\']{escaped}["\'][^>]+content=["\'](.*?)["\']',
            rf'(?is)<meta[^>]+content=["\'](.*?)["\'][^>]+(?:name|property)=["\']{escaped}["\']',
        ]
        for pattern in patterns:
            match = re.search(pattern, html_text)
            if match:
                return clean_text(match.group(1))
    return ""


def yahoo_article_data(html_text: str) -> dict[str, str]:
    state = extract_json_object(html_text, "window.__PRELOADED_STATE__")
    detail = state.get("articleDetail", {})
    if not detail:
        detail = state.get("mainNewsArticleDetail", {})
    if not isinstance(detail, dict) or not detail:
        return {}
    pieces: list[str] = []

    def find_text_details(value: Any) -> None:
        if isinstance(value, dict):
            if isinstance(value.get("body"), str) and len(value["body"].strip()) > 30:
                pieces.append(clean_text(value["body"]))
                return
            if isinstance(value.get("text"), str) and len(value["text"].strip()) > 30:
                pieces.append(clean_text(value["text"]))
                return
            for child in value.values():
                find_text_details(child)
        elif isinstance(value, list):
            for child in value:
                find_text_details(child)

    find_text_details(detail.get("paragraphs", []))
    unique: list[str] = []
    for piece in pieces:
        if piece and not any(piece == old or piece in old for old in unique):
            unique.append(piece)
    media = detail.get("media", {}) if isinstance(detail.get("media"), dict) else {}
    result = {
        "title": clean_text(detail.get("headline", "")),
        "body": "\n\n".join(unique),
        "author": clean_text(media.get("mediaName", "") or media.get("name", "") or
                             detail.get("mediaName", "") or detail.get("author", "")),
        "copyright": clean_text(media.get("copyright", "") or detail.get("copyright", "")),
        "published": parse_datetime(detail.get("createDateTime", "")),
        "summary": clean_text(detail.get("summary", "")),
    }
    return result


def generic_article_data(html_text: str) -> dict[str, str]:
    article = jsonld_article(html_text)
    body = clean_text(article.get("articleBody", "")) if article else ""
    if len(body) < 200:
        match = re.search(r"(?is)<article\b[^>]*>(.*?)</article>", html_text)
        if match:
            article_text = clean_text(match.group(1))
            if len(article_text) > len(body):
                body = article_text
    summary = clean_text(article.get("description", "")) if article else ""
    if not summary:
        summary = meta_content(html_text, "description", "og:description", "twitter:description")
    return {
        "title": clean_text(article.get("headline", "") or article.get("name", ""))
                 if article else meta_content(html_text, "og:title", "twitter:title"),
        "body": body,
        "author": author_names(article.get("author", "")) if article else "",
        "copyright": clean_text(article.get("copyrightHolder", "") or
                                article.get("copyrightNotice", "")) if article else "",
        "published": parse_datetime(article.get("datePublished", "")) if article else "",
        "summary": summary,
    }


def enrich_item(item: dict[str, Any]) -> dict[str, Any]:
    result = dict(item)
    url = unwrap_bing_link(result["link"])
    if "news.google.com/rss/articles/" in url:
        result["enrichment_note"] = "Google News jump URL retained"
        return result
    try:
        payload, final_url, content_type = fetch_url(url, timeout=25, retries=1)
        result["resolved_url"] = final_url
        if "html" not in content_type.casefold() and not payload.lstrip().startswith(b"<"):
            result["enrichment_note"] = f"non-HTML content: {content_type}"
            return result
        html_text = payload.decode("utf-8", "replace")
        if "yahoo.co.jp/" in final_url:
            try:
                extracted = yahoo_article_data(html_text)
            except (ValueError, json.JSONDecodeError):
                extracted = generic_article_data(html_text)
        else:
            extracted = generic_article_data(html_text)
        if extracted.get("body") and len(extracted["body"]) > len(result.get("body", "")):
            result["body"] = extracted["body"]
            result["body_source"] = "article page structured data"
        if extracted.get("summary") and not result.get("summary"):
            result["summary"] = extracted["summary"]
            result["summary_source"] = "article page metadata"
        if extracted.get("author"):
            result["author"] = extracted["author"]
        if extracted.get("copyright"):
            result["copyright"] = extracted["copyright"]
        if extracted.get("published"):
            result["published"] = extracted["published"]
        if not result.get("summary") and result.get("body"):
            summary = result["body"][:420]
            result["summary"] = summary.rsplit(" ", 1)[0] if " " in summary else summary
            result["summary_source"] = "extractive lead"
        result["enrichment_note"] = "article page parsed"
    except Exception as exc:  # source health records the item-level outcome
        result["enrichment_note"] = f"article fetch error: {type(exc).__name__}: {exc}"
    return result


def build_rss(items: list[dict[str, Any]], title: str, link: str,
              description: str) -> bytes:
    root = ET.Element("rss", {"version": "2.0"})
    channel = ET.SubElement(root, "channel")
    ET.SubElement(channel, "title").text = title
    ET.SubElement(channel, "link").text = link
    ET.SubElement(channel, "description").text = description
    ET.SubElement(channel, "language").text = "mul"
    ET.SubElement(channel, "lastBuildDate").text = email.utils.format_datetime(utc_now())
    ET.SubElement(channel, "ttl").text = "30"
    for row in items:
        node = ET.SubElement(channel, "item")
        ET.SubElement(node, "title").text = row["title"]
        ET.SubElement(node, "link").text = row["link"]
        guid = ET.SubElement(node, "guid", {"isPermaLink": "false"})
        guid.text = row["id"]
        ET.SubElement(node, "pubDate").text = rfc822(row.get("published", ""))
        ET.SubElement(node, "description").text = row.get("summary", "")
        if row.get("author"):
            ET.SubElement(node, f"{{{DC_NS}}}creator").text = row["author"]
        if row.get("body"):
            paragraphs = [p.strip() for p in row["body"].split("\n\n") if p.strip()]
            encoded = "".join(f"<p>{html_lib.escape(p)}</p>" for p in paragraphs)
            ET.SubElement(node, f"{{{CONTENT_NS}}}encoded").text = encoded
        for name, key in (
            ("platform", "platform"), ("sourceId", "source_id"),
            ("publisher", "publisher"), ("classification", "classification"),
            ("confidence", "confidence"), ("summarySource", "summary_source"),
            ("bodySource", "body_source"), ("canonicalUrl", "canonical_url"),
        ):
            value = row.get(key, "")
            if value != "":
                ET.SubElement(node, f"{{{WIRE_NS}}}{name}").text = str(value)
        for evidence in row.get("evidence", []):
            ET.SubElement(node, f"{{{WIRE_NS}}}evidence").text = evidence
    ET.indent(root, space="  ")
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def atomic_write(path: Path, data: bytes | str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    if isinstance(data, str):
        temporary.write_text(data, encoding="utf-8")
    else:
        temporary.write_bytes(data)
    os.replace(temporary, path)


class PoolBuilder:
    def __init__(self, config_path: Path, output_dir: Path, workers: int = 10,
                 enrich: bool = True, selected_sources: set[str] | None = None):
        self.config_path = config_path
        self.output_dir = output_dir
        self.workers = workers
        self.enrich = enrich
        self.selected_sources = selected_sources or set()
        config = json.loads(config_path.read_text(encoding="utf-8"))
        self.sources: list[dict[str, Any]] = config["sources"]
        if self.selected_sources:
            self.sources = [s for s in self.sources if s["id"] in self.selected_sources]

    def collect_source(self, spec: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        started = time.monotonic()
        health = {
            "source_id": spec["id"], "platform": spec["platform"],
            "kind": spec["kind"], "url": spec.get("url", ""),
            "status": "ok", "fetched": 0, "accepted": 0, "elapsed_seconds": 0.0,
            "checked_at": iso_now(), "error": "",
        }
        try:
            if spec["kind"] == "yahoo_news_media":
                rows = collect_yahoo_news_media(spec)
            elif spec["kind"] == "yahoo_finance_media":
                rows = collect_yahoo_finance_media(spec)
            else:
                url = spec.get("url") or discovery_url(spec)
                health["url"] = url
                payload, final_url, content_type = fetch_url(url)
                rows = parse_rss(payload, {**spec, "url": url})
                health["final_url"] = final_url
                health["content_type"] = content_type
                if not rows and spec["kind"] == "bing_discovery":
                    fallback_url = google_site_discovery_url(spec)
                    payload, final_url, content_type = fetch_url(fallback_url)
                    rows = parse_rss(payload, {**spec, "url": fallback_url})
                    health["fallback_url"] = fallback_url
                    health["fallback_final_url"] = final_url
                    health["fallback_content_type"] = content_type
            health["fetched"] = len(rows)
        except Exception as exc:
            rows = []
            health["status"] = "error"
            health["error"] = f"{type(exc).__name__}: {exc}"
        health["elapsed_seconds"] = round(time.monotonic() - started, 3)
        return rows, health

    def _enrich(self, rows_by_source: dict[str, list[dict[str, Any]]]) -> None:
        if not self.enrich:
            return
        jobs: list[tuple[str, int, dict[str, Any]]] = []
        specs = {spec["id"]: spec for spec in self.sources}
        for source_id, rows in rows_by_source.items():
            limit = int(specs[source_id].get("enrich_limit", 0))
            if limit <= 0:
                continue
            prelim = [(classify_wire_item(row)["classification"] != "unrelated", i, row)
                      for i, row in enumerate(rows)]
            prelim.sort(key=lambda x: (not x[0], x[1]))
            for _, index, row in prelim[:limit]:
                jobs.append((source_id, index, row))
        with concurrent.futures.ThreadPoolExecutor(max_workers=self.workers) as executor:
            future_map = {executor.submit(enrich_item, row): (source_id, index)
                          for source_id, index, row in jobs}
            for future in concurrent.futures.as_completed(future_map):
                source_id, index = future_map[future]
                try:
                    rows_by_source[source_id][index] = future.result()
                except Exception as exc:
                    rows_by_source[source_id][index]["enrichment_note"] = (
                        f"worker error: {type(exc).__name__}: {exc}")

    def run(self) -> dict[str, Any]:
        started = time.monotonic()
        rows_by_source: dict[str, list[dict[str, Any]]] = {}
        health_by_source: dict[str, dict[str, Any]] = {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=self.workers) as executor:
            future_map = {executor.submit(self.collect_source, spec): spec
                          for spec in self.sources}
            for future in concurrent.futures.as_completed(future_map):
                spec = future_map[future]
                rows, health = future.result()
                rows_by_source[spec["id"]] = rows
                health_by_source[spec["id"]] = health

        self._enrich(rows_by_source)
        all_items: list[dict[str, Any]] = []
        per_source: dict[str, list[dict[str, Any]]] = {}
        for spec in self.sources:
            accepted: list[dict[str, Any]] = []
            for row in rows_by_source.get(spec["id"], []):
                row.update(classify_wire_item(row))
                if row["classification"] == "unrelated":
                    continue
                row["has_full_text"] = len(row.get("body", "")) >= 200
                accepted.append(row)
                all_items.append(row)
            per_source[spec["id"]] = accepted
            health_by_source[spec["id"]]["accepted"] = len(accepted)
            health_by_source[spec["id"]]["class_counts"] = {
                name: sum(1 for row in accepted if row["classification"] == name)
                for name in ("wire_original", "wire_syndication", "wire_attribution",
                             "discovery_candidate")
            }

        def sort_key(row: dict[str, Any]) -> str:
            return row.get("published", "")

        all_items.sort(key=sort_key, reverse=True)
        base_link = "./"
        feed_dir = self.output_dir / "feeds"
        for spec in self.sources:
            rows = sorted(per_source[spec["id"]], key=sort_key, reverse=True)
            atomic_write(feed_dir / f"{spec['id']}.xml", build_rss(
                rows, f"{spec['platform']} — Reuters/Bloomberg pool",
                spec.get("url") or health_by_source[spec["id"]].get("url", ""),
                "Provenance-aware feed; classification fields distinguish exact wire copies from citations.",
            ))

        groups = {
            "all.xml": all_items,
            "verified_all.xml": [r for r in all_items if r["classification"] != "discovery_candidate"],
            "wire_original.xml": [r for r in all_items if r["classification"] == "wire_original"],
            "wire_syndication.xml": [r for r in all_items if r["classification"] == "wire_syndication"],
            "wire_attribution.xml": [r for r in all_items if r["classification"] == "wire_attribution"],
            "discovery_candidates.xml": [r for r in all_items if r["classification"] == "discovery_candidate"],
            "fulltext.xml": [r for r in all_items if r.get("has_full_text")],
        }
        for filename, rows in groups.items():
            atomic_write(self.output_dir / filename, build_rss(
                rows, f"Reuters/Bloomberg resource pool — {filename[:-4]}",
                base_link, "Generated resource-pool feed with provenance and confidence metadata.",
            ))

        deduplicated: dict[str, dict[str, Any]] = {}
        rank = {"wire_original": 4, "wire_syndication": 3,
                "wire_attribution": 2, "discovery_candidate": 1}
        for row in all_items:
            key = re.sub(r"\W+", "", row["title"].casefold())[:180] or row["id"]
            old = deduplicated.get(key)
            score = (rank[row["classification"]], len(row.get("body", "")))
            old_score = ((rank[old["classification"]], len(old.get("body", "")))
                         if old else (-1, -1))
            if score > old_score:
                deduplicated[key] = row
        dedup_rows = sorted(deduplicated.values(), key=sort_key, reverse=True)
        atomic_write(self.output_dir / "deduplicated.xml", build_rss(
            dedup_rows, "Reuters/Bloomberg resource pool — deduplicated",
            base_link, "Title-deduplicated cross-platform feed.",
        ))

        health = [health_by_source[spec["id"]] for spec in self.sources]
        summary = {
            "generated_at": iso_now(), "sources_total": len(self.sources),
            "sources_ok": sum(1 for row in health if row["status"] == "ok"),
            "sources_error": sum(1 for row in health if row["status"] == "error"),
            "items_total": len(all_items), "items_deduplicated": len(dedup_rows),
            "wire_original": len(groups["wire_original.xml"]),
            "wire_syndication": len(groups["wire_syndication.xml"]),
            "wire_attribution": len(groups["wire_attribution.xml"]),
            "discovery_candidates": len(groups["discovery_candidates.xml"]),
            "full_text": len(groups["fulltext.xml"]),
            "elapsed_seconds": round(time.monotonic() - started, 3),
        }
        manifest = {
            "schema_version": 1, "summary": summary, "sources": health,
            "items": all_items,
        }
        atomic_write(self.output_dir / "resource_pool.json",
                     json.dumps(manifest, ensure_ascii=False, indent=2))
        atomic_write(self.output_dir / "health.json",
                     json.dumps({"summary": summary, "sources": health},
                                ensure_ascii=False, indent=2))
        atomic_write(self.output_dir / "source_registry.json",
                     json.dumps(self.sources, ensure_ascii=False, indent=2))
        self._write_opml(health)
        return manifest

    def _write_opml(self, health: list[dict[str, Any]]) -> None:
        root = ET.Element("opml", {"version": "2.0"})
        head = ET.SubElement(root, "head")
        ET.SubElement(head, "title").text = "Reuters/Bloomberg RSS resource pool"
        body = ET.SubElement(root, "body")
        generated = ET.SubElement(body, "outline", {"text": "Generated feeds"})
        for spec in self.sources:
            ET.SubElement(generated, "outline", {
                "type": "rss", "text": spec["platform"], "title": spec["id"],
                "xmlUrl": f"data/feeds/{spec['id']}.xml",
                "htmlUrl": spec.get("url", health[[x["source_id"] for x in health].index(spec["id"])].get("url", "")),
            })
        ET.indent(root, space="  ")
        atomic_write(self.output_dir / "resource_pool.opml",
                     ET.tostring(root, encoding="utf-8", xml_declaration=True))
