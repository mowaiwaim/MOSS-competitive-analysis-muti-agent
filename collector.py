from __future__ import annotations

import json
import os
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
import base64
from dataclasses import dataclass, field
from html import unescape
from html.parser import HTMLParser
from pathlib import Path


USER_AGENT = "CompetitiveAnalysisAgent/2.0 (+local-demo; respectful-crawler)"
FETCH_TIMEOUT_SECONDS = 8
REQUEST_DELAY_SECONDS = 0.35
MAX_TEXT_CHARS = 24000


@dataclass
class EvidenceChunkDraft:
    chunk_index: int
    char_start: int
    char_end: int
    excerpt: str
    summary: str


@dataclass
class WebSourceDraft:
    source_id: str
    source_type: str
    title: str
    url: str
    author_site: str
    excerpt: str
    credibility: str
    chunks: list[EvidenceChunkDraft] = field(default_factory=list)
    robots_status: str = ""
    fallback_reason: str = ""
    published_at: str = ""
    provider: str = "web_fetch"
    search_log_id: str = ""
    search_query: str = ""
    auth_info: str = ""
    auth_level: int = 0
    rank: int = 0
    time_cost_ms: int = 0
    competitor_name: str = ""
    module: str = ""
    relevance_score: int = 0
    source_role: str = ""
    raw_content_status: str = "summary_only"


class PageTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.title = ""
        self._in_title = False
        self._skip_depth = 0
        self._parts: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in {"script", "style", "noscript", "svg"}:
            self._skip_depth += 1
        if tag == "title":
            self._in_title = True

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "noscript", "svg"} and self._skip_depth:
            self._skip_depth -= 1
        if tag == "title":
            self._in_title = False
        if tag in {"p", "li", "section", "article", "h1", "h2", "h3", "tr"}:
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        text = re.sub(r"\s+", " ", data).strip()
        if not text:
            return
        if self._in_title and not self.title:
            self.title = text[:180]
        if self._skip_depth:
            return
        self._parts.append(text)

    @property
    def text(self) -> str:
        return clean_text(" ".join(self._parts))


def clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def normalize_url(value: str) -> str:
    candidate = (value or "").strip()
    if not candidate:
        raise ValueError("empty URL")
    parsed = urllib.parse.urlparse(candidate)
    if not parsed.scheme:
        candidate = "https://" + candidate
        parsed = urllib.parse.urlparse(candidate)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("only http/https URLs are supported")
    return urllib.parse.urlunparse(parsed._replace(fragment=""))


def classify_source_type(url: str) -> str:
    lowered = url.lower()
    if any(marker in lowered for marker in ["/pricing", "price", "plans", "pricing."]):
        return "pricing_page"
    if any(marker in lowered for marker in ["/docs", "developer", "help", "support"]):
        return "public_doc"
    return "web_page"


def check_robots(url: str) -> tuple[bool, str]:
    parsed = urllib.parse.urlparse(url)
    robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
    parser = urllib.robotparser.RobotFileParser()
    parser.set_url(robots_url)
    try:
        parser.read()
    except Exception:
        return True, "robots_unavailable_allowed"
    try:
        allowed = parser.can_fetch(USER_AGENT, url)
    except Exception:
        return True, "robots_parse_failed_allowed"
    return allowed, "robots_allowed" if allowed else "robots_disallowed"


def chunk_text(text: str, chunk_size: int = 900, overlap: int = 120) -> list[EvidenceChunkDraft]:
    cleaned = clean_text(text)[:MAX_TEXT_CHARS]
    chunks: list[EvidenceChunkDraft] = []
    if not cleaned:
        return chunks
    start = 0
    index = 0
    while start < len(cleaned):
        end = min(len(cleaned), start + chunk_size)
        excerpt = cleaned[start:end].strip()
        if excerpt:
            chunks.append(
                EvidenceChunkDraft(
                    chunk_index=index,
                    char_start=start,
                    char_end=end,
                    excerpt=excerpt,
                    summary=excerpt[:180],
                )
            )
            index += 1
        if end >= len(cleaned):
            break
        start = max(end - overlap, start + 1)
    return chunks


class WebCollector:
    def collect(self, urls: list[str], task_prefix: str) -> tuple[list[WebSourceDraft], list[dict[str, str]]]:
        results: list[WebSourceDraft] = []
        failures: list[dict[str, str]] = []
        seen: set[str] = set()
        for raw_url in urls:
            try:
                url = normalize_url(raw_url)
            except ValueError as exc:
                failures.append({"url": raw_url, "reason": str(exc)})
                continue
            if url in seen:
                continue
            seen.add(url)
            try:
                source = self.collect_one(url, task_prefix, len(results))
                results.append(source)
                time.sleep(REQUEST_DELAY_SECONDS)
            except Exception as exc:
                failures.append({"url": url, "reason": clean_text(str(exc))[:240]})
        return results, failures

    def collect_one(self, url: str, task_prefix: str, index: int) -> WebSourceDraft:
        allowed, robots_status = check_robots(url)
        if not allowed:
            raise PermissionError("robots.txt disallows this URL")

        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "text/html,application/xhtml+xml,text/plain;q=0.9,*/*;q=0.5",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=FETCH_TIMEOUT_SECONDS) as response:
                raw = response.read(1024 * 1024)
                content_type = response.headers.get("content-type", "")
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"HTTP {exc.code}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"network error: {exc.reason}") from exc

        text = raw.decode("utf-8", errors="replace")
        title = ""
        if "html" in content_type.lower() or "<html" in text[:500].lower():
            parser = PageTextParser()
            parser.feed(text)
            title = parser.title
            text = parser.text
        else:
            text = clean_text(text)

        if len(text) < 80:
            raise RuntimeError("collected page has too little readable text")

        parsed = urllib.parse.urlparse(url)
        chunks = chunk_text(text)
        return WebSourceDraft(
            source_id=f"{task_prefix}_web_{index + 1:02d}",
            source_type=classify_source_type(url),
            title=title or parsed.netloc,
            url=url,
            author_site=parsed.netloc,
            excerpt=text[:520],
            credibility="medium",
            chunks=chunks,
            robots_status=robots_status,
        )


class VolcWebSearchClient:
    """Client for Volcengine web search API; evidence is built from API results."""

    def __init__(self, env_path: str | Path | None = None) -> None:
        configured_env_path = os.environ.get("VOLC_SEARCH_ENV_PATH", "")
        self.env_path = Path(env_path or configured_env_path) if (env_path or configured_env_path) else Path(__file__).resolve().parent / ".env.local"
        self.api_key = ""
        self.base_url = "https://open.feedcoopapi.com"
        self.search_type = "web_summary"
        self.default_count = 6
        self.time_range = "OneYear"
        self.refresh_settings()

    def _local_env_values(self) -> dict[str, str]:
        if not self.env_path.exists():
            return {}
        values: dict[str, str] = {}
        for raw_line in self.env_path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip('"').strip("'")
        return values

    def _setting(self, key: str, default: str, file_values: dict[str, str]) -> str:
        env_value = os.environ.get(key, "").strip()
        if env_value:
            return env_value
        file_value = file_values.get(key, "").strip()
        return file_value or default

    def refresh_settings(self) -> None:
        file_values = self._local_env_values()
        self.api_key = self._setting("VOLC_SEARCH_API_KEY", "", file_values)
        self.base_url = self._setting("VOLC_SEARCH_BASE_URL", "https://open.feedcoopapi.com", file_values).rstrip("/")
        self.search_type = self._setting("VOLC_SEARCH_TYPE", "web_summary", file_values)
        try:
            self.default_count = int(self._setting("VOLC_SEARCH_COUNT", "6", file_values) or "6")
        except ValueError:
            self.default_count = 6
        self.time_range = self._setting("VOLC_SEARCH_TIME_RANGE", "OneYear", file_values)

    def config_status(self) -> dict[str, object]:
        self.refresh_settings()
        return {
            "provider": "volc_search",
            "api_key_configured": bool(self.api_key),
            "base_url": self.base_url,
            "search_type": self.search_type,
            "count": self.default_count,
            "time_range": self.time_range,
            "env_path": str(self.env_path),
        }

    def _safe_int(self, value: object, default: int = 0) -> int:
        def parse(item: object) -> int | None:
            if item is None or isinstance(item, bool):
                return None
            if isinstance(item, (list, tuple)):
                for nested in item:
                    parsed_nested = parse(nested)
                    if parsed_nested is not None:
                        return parsed_nested
                return None
            if isinstance(item, dict):
                return None
            try:
                return int(float(str(item).strip()))
            except (TypeError, ValueError):
                return None

        parsed = parse(value)
        return default if parsed is None else parsed

    def _parse_response_payload(self, raw: bytes) -> dict[str, object]:
        text = raw.decode("utf-8", errors="replace").strip()
        if not text:
            raise RuntimeError("volc search response error: empty body")
        if text.startswith("data:") or "\ndata:" in text:
            parsed_events: list[dict[str, object]] = []
            for line in text.splitlines():
                line = line.strip()
                if not line.startswith("data:"):
                    continue
                data = line.removeprefix("data:").strip()
                if not data or data == "[DONE]":
                    continue
                try:
                    event_payload = json.loads(data)
                except json.JSONDecodeError:
                    continue
                if isinstance(event_payload, dict):
                    parsed_events.append(event_payload)
                    result = event_payload.get("Result") or event_payload.get("result") or {}
                    if isinstance(result, dict) and result.get("WebResults"):
                        return event_payload
            if parsed_events:
                return parsed_events[-1]
            raise RuntimeError("volc search response error: invalid SSE payload")
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            prefix = clean_text(text[:160])
            raise RuntimeError(f"volc search response error: invalid JSON body: {prefix}") from exc
        if not isinstance(payload, dict):
            raise RuntimeError("volc search response error: JSON root must be an object")
        return payload

    def search(self, query: str, task_prefix: str, start_index: int = 0, limit: int = 6,
              block_hosts: str | None = None) -> list[WebSourceDraft]:
        self.refresh_settings()
        if not self.api_key:
            raise RuntimeError("VOLC_SEARCH_API_KEY is not configured")
        count = max(1, min(self._safe_int(limit, self.default_count), self.default_count, 10))
        effective_block = block_hosts if block_hosts is not None else "bilibili.com|douyin.com|kuaishou.com|weibo.com"
        filter_payload: dict[str, object] = {
            "NeedContent": True,
            "NeedUrl": True,
        }
        if effective_block.strip():
            filter_payload["BlockHosts"] = effective_block.strip()
        request_body = {
            "Query": query,
            "SearchType": self.search_type,
            "Count": count,
            "NeedSummary": True,
            "TimeRange": self.time_range,
            "Filter": filter_payload,
        }
        request = urllib.request.Request(
            f"{self.base_url}/search_api/web_search",
            data=json.dumps(request_body, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "User-Agent": USER_AGENT,
            },
            method="POST",
        )
        try:
            started = time.perf_counter()
            with urllib.request.urlopen(request, timeout=FETCH_TIMEOUT_SECONDS) as response:
                payload = self._parse_response_payload(response.read())
            elapsed_ms = int((time.perf_counter() - started) * 1000)
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"volc search HTTP {exc.code}") from exc
        except (TimeoutError, socket.timeout) as exc:
            raise RuntimeError("volc search timeout") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"volc search network error: {exc.reason}") from exc
        except OSError as exc:
            raise RuntimeError(f"volc search response error: {exc.__class__.__name__}") from exc

        # Check nested error in ResponseMetadata (actual API error format)
        response_meta = payload.get("ResponseMetadata", {})
        if isinstance(response_meta, dict):
            meta_error = response_meta.get("Error", {})
            if isinstance(meta_error, dict) and meta_error:
                code_str = str(meta_error.get("Code", "") or meta_error.get("CodeN", ""))
                message = str(meta_error.get("Message", "") or meta_error.get("message", "") or code_str)
                raise RuntimeError(f"volc search API error: {message} (code={code_str})")

        # Check top-level error code (alternative format)
        code = payload.get("Code", payload.get("code", 0))
        if code not in {0, "0", "", None}:
            message = payload.get("Message") or payload.get("message") or "unknown error"
            raise RuntimeError(f"volc search API error: {message}")

        result = payload.get("Result") or payload.get("result")
        if result is None:
            raise RuntimeError("volc search returned null Result (possible auth or quota error)")
        if not isinstance(result, dict):
            raise RuntimeError("volc search Result must be an object")
        rows = (
            result.get("WebResults")
            or result.get("web_results")
            or result.get("webResults")
            or []
        )
        if not isinstance(rows, list):
            raise RuntimeError("volc search WebResults must be a list")
        log_id = str(result.get("LogId") or result.get("log_id") or payload.get("LogId") or "")
        time_cost = self._safe_int(result.get("TimeCost", result.get("time_cost", elapsed_ms)), elapsed_ms)

        drafts: list[WebSourceDraft] = []
        for row in rows[:count]:
            if not isinstance(row, dict):
                continue
            title = clean_text(str(row.get("Title") or row.get("title") or row.get("Name") or ""))
            url = clean_text(str(row.get("Url") or row.get("URL") or row.get("url") or row.get("Link") or ""))
            if not title or not url:
                continue
            summary = clean_text(str(row.get("Summary") or row.get("summary") or row.get("Snippet") or row.get("snippet") or ""))
            content = clean_text(str(row.get("Content") or row.get("content") or ""))
            parsed = urllib.parse.urlparse(url)
            author_site = clean_text(
                str(row.get("SiteName") or row.get("site_name") or row.get("Source") or row.get("source") or parsed.netloc)
            )
            auth_info = clean_text(str(row.get("AuthInfoDes") or row.get("auth_info_des") or row.get("AuthInfo") or ""))
            auth_level = self._safe_int(row.get("AuthInfoLevel", row.get("auth_info_level", 0)), 0)
            published_at = clean_text(str(row.get("PublishTime") or row.get("publish_time") or row.get("PublishedAt") or ""))
            evidence_text = clean_text(
                f"搜索词：{query}。搜索结果：{title}。摘要：{summary or content[:260]}。正文线索：{content[:1200]}"
            )
            credibility = "high" if auth_level >= 3 else "medium" if auth_level >= 1 else "low"
            drafts.append(
                WebSourceDraft(
                    source_id=f"{task_prefix}_volc_{start_index + len(drafts) + 1:02d}",
                    source_type="volc_search_result",
                    title=title[:180],
                    url=url,
                    author_site=author_site or parsed.netloc or "volc_search",
                    excerpt=evidence_text[:900],
                    credibility=credibility,
                    chunks=chunk_text(evidence_text, chunk_size=700, overlap=80),
                    published_at=published_at,
                    provider="volc_search",
                    search_log_id=log_id,
                    search_query=query,
                    auth_info=auth_info,
                    auth_level=auth_level,
                    rank=start_index + len(drafts) + 1,
                    time_cost_ms=time_cost,
                )
            )
        return drafts


class BingSearchClient:
    def search(self, query: str, task_prefix: str, start_index: int = 0, limit: int = 3) -> list[WebSourceDraft]:
        params = urllib.parse.urlencode(
            {
                "q": query,
                "mkt": "zh-CN",
                "setlang": "zh-Hans",
                "cc": "CN",
            }
        )
        search_url = f"https://www.bing.com/search?{params}"
        request = urllib.request.Request(
            search_url,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.6",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=FETCH_TIMEOUT_SECONDS) as response:
                html = response.read(512 * 1024).decode("utf-8", errors="replace")
        except (urllib.error.HTTPError, urllib.error.URLError) as exc:
            raise RuntimeError(f"search error: {exc}") from exc

        results: list[WebSourceDraft] = []
        blocks = re.findall(r'<li class="b_algo"[\s\S]*?</li>', html, flags=re.I)
        for block in blocks:
            match = re.search(r'<h2[^>]*>\s*<a[^>]+href="([^"]+)"[^>]*>([\s\S]*?)</a>', block, flags=re.I)
            if not match:
                continue
            href = unwrap_bing_redirect(unescape(match.group(1)))
            title = clean_text(strip_tags(match.group(2)))
            snippet_match = re.search(r'<p[^>]*>([\s\S]*?)</p>', block, flags=re.I)
            snippet = clean_text(strip_tags(snippet_match.group(1) if snippet_match else ""))
            if not href or not title:
                continue
            excerpt = f"搜索词：{query}。搜索结果：{title}。摘要：{snippet or '搜索结果未提供摘要。'}"
            chunks = chunk_text(excerpt, chunk_size=700, overlap=80)
            results.append(
                WebSourceDraft(
                    source_id=f"{task_prefix}_search_{start_index + len(results) + 1:02d}",
                    source_type="search_result",
                    title=title[:180],
                    url=href,
                    author_site=urllib.parse.urlparse(href).netloc or "Bing search",
                    excerpt=excerpt,
                    credibility="low",
                    chunks=chunks,
                )
            )
            if len(results) >= limit:
                break
        return results


def strip_tags(value: str) -> str:
    return re.sub(r"<[^>]+>", " ", unescape(value or ""))


def unwrap_bing_redirect(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    if "bing.com" not in parsed.netloc or not parsed.query:
        return url
    target_values = urllib.parse.parse_qs(parsed.query).get("u", [])
    if not target_values:
        return url
    encoded = target_values[0]
    if encoded.startswith("a1"):
        encoded = encoded[2:]
    padding = "=" * (-len(encoded) % 4)
    try:
        decoded = base64.urlsafe_b64decode((encoded + padding).encode("ascii")).decode("utf-8", errors="replace")
    except Exception:
        return url
    return decoded if decoded.startswith(("http://", "https://")) else url
