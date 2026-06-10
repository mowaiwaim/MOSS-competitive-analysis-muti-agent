from __future__ import annotations

import os
import re
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from typing import Any

from collector import WebSourceDraft, chunk_text, clean_text


RSS_TIMEOUT_SECONDS = float(os.environ.get("GOOGLE_ALERTS_RSS_TIMEOUT_SECONDS", "5") or 5)
RSS_MAX_ITEMS = int(os.environ.get("GOOGLE_ALERTS_RSS_MAX_ITEMS", "30") or 30)


def google_alerts_rss_urls() -> list[str]:
    values: list[str] = []
    for env_name in ("GOOGLE_ALERTS_RSS_URLS", "GOOGLE_ALERTS_RSS_URL"):
        raw = os.environ.get(env_name, "")
        if raw:
            values.extend(re.split(r"[\n,;]+", raw))
    for key, value in os.environ.items():
        if key.startswith("GOOGLE_ALERTS_RSS_") and key not in {"GOOGLE_ALERTS_RSS_URL", "GOOGLE_ALERTS_RSS_URLS"}:
            values.extend(re.split(r"[\n,;]+", value or ""))
    seen: set[str] = set()
    urls: list[str] = []
    for value in values:
        url = value.strip().strip('"').strip("'")
        if not url or not url.startswith(("http://", "https://")):
            continue
        if url in seen:
            continue
        seen.add(url)
        urls.append(url)
    return urls


def google_alerts_status() -> dict[str, object]:
    urls = google_alerts_rss_urls()
    return {
        "configured": bool(urls),
        "url_count": len(urls),
    }


def _text(node: ET.Element | None) -> str:
    return clean_text("".join(node.itertext())) if node is not None else ""


def _first_text(item: ET.Element, names: list[str]) -> str:
    for name in names:
        node = item.find(name)
        if node is not None:
            value = _text(node)
            if value:
                return value
    return ""


def _first_link(item: ET.Element) -> str:
    link = _first_text(item, ["link", "{http://www.w3.org/2005/Atom}link"])
    if link:
        return link
    for node in item.findall("{http://www.w3.org/2005/Atom}link"):
        href = node.attrib.get("href", "")
        if href:
            return href
    return ""


def _author_site(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    return parsed.netloc or "google-alerts"


def _published_at(item: ET.Element) -> str:
    return _first_text(
        item,
        [
            "pubDate",
            "published",
            "updated",
            "{http://www.w3.org/2005/Atom}published",
            "{http://www.w3.org/2005/Atom}updated",
        ],
    )


def _match_competitor(title: str, summary: str, competitor_names: list[str]) -> str:
    haystack = f"{title} {summary}".casefold()
    for name in competitor_names:
        if str(name).casefold() in haystack:
            return str(name)
    return ""


def _rss_items(xml_text: str) -> list[ET.Element]:
    root = ET.fromstring(xml_text)
    items = root.findall(".//item")
    if items:
        return items
    return root.findall(".//{http://www.w3.org/2005/Atom}entry")


def collect_google_alert_sources(
    competitor_names: list[str],
    task_prefix: str,
    max_items_per_feed: int | None = None,
) -> tuple[list[WebSourceDraft], list[dict[str, str]], dict[str, Any]]:
    urls = google_alerts_rss_urls()
    max_items = max(1, int(max_items_per_feed or RSS_MAX_ITEMS))
    drafts: list[WebSourceDraft] = []
    failures: list[dict[str, str]] = []
    started = time.perf_counter()

    for feed_index, feed_url in enumerate(urls):
        try:
            request = urllib.request.Request(
                feed_url,
                headers={
                    "User-Agent": "CompetitiveAnalysisAgent/2.0 GoogleAlertsRSSReader",
                    "Accept": "application/rss+xml,application/atom+xml,application/xml,text/xml;q=0.9,*/*;q=0.5",
                },
            )
            with urllib.request.urlopen(request, timeout=RSS_TIMEOUT_SECONDS) as response:
                xml_text = response.read(1024 * 1024).decode("utf-8", errors="replace")
            for item_index, item in enumerate(_rss_items(xml_text)[:max_items]):
                title = _first_text(item, ["title", "{http://www.w3.org/2005/Atom}title"])
                link = _first_link(item) or feed_url
                summary = _first_text(
                    item,
                    [
                        "description",
                        "summary",
                        "content",
                        "{http://www.w3.org/2005/Atom}summary",
                        "{http://www.w3.org/2005/Atom}content",
                    ],
                )
                excerpt = clean_text(f"{title}。{summary}")[:900]
                if not title and not excerpt:
                    continue
                source_id = f"{task_prefix}_ga_{feed_index + 1:02d}_{item_index + 1:02d}"
                drafts.append(
                    WebSourceDraft(
                        source_id=source_id,
                        source_type="google_alert_rss",
                        title=title or _author_site(link),
                        url=link,
                        author_site=_author_site(link),
                        excerpt=excerpt or title,
                        credibility="medium",
                        chunks=chunk_text(excerpt or title, chunk_size=700, overlap=80),
                        published_at=_published_at(item),
                        provider="google_alerts_rss",
                        search_query="Google Alerts RSS",
                        competitor_name=_match_competitor(title, summary, competitor_names),
                        module="news_signal",
                        relevance_score=68,
                        source_role="news_alert",
                        raw_content_status="summary_only",
                    )
                )
        except Exception as exc:
            failures.append({"url": feed_url, "reason": clean_text(str(exc))[:240]})

    return (
        drafts,
        failures,
        {
            "configured": bool(urls),
            "url_count": len(urls),
            "item_count": len(drafts),
            "failure_count": len(failures),
            "time_cost_ms": int((time.perf_counter() - started) * 1000),
        },
    )
