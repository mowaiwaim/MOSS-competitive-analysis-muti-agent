from __future__ import annotations

import json
import os
import re
import time
import urllib.request
from pathlib import Path
from typing import Any

APPARK_COMPETITOR_URL = "https://appark.ai/cn/dashboards/competitor"


def parse_metric_number(value: str) -> float:
    text = str(value or "").replace(",", "").strip()
    if not text or text == "-":
        return 0.0
    match = re.search(r"(-?\d+(?:\.\d+)?)", text)
    if not match:
        return 0.0
    number = float(match.group(1))
    if "亿" in text:
        number *= 100000000
    elif "万" in text:
        number *= 10000
    elif re.search(r"\bK\b", text, flags=re.I):
        number *= 1000
    elif re.search(r"\bM\b", text, flags=re.I):
        number *= 1000000
    elif re.search(r"\bB\b", text, flags=re.I):
        number *= 1000000000
    return number


def parse_rank(value: str) -> int | None:
    text = str(value or "").strip()
    if not text or text == "-":
        return None
    match = re.search(r"\d+", text)
    return int(match.group(0)) if match else None


def _norm(value: str) -> str:
    return re.sub(r"[\s_\-—–·.,，。:：()（）]+", "", str(value or "").casefold())


def _competitor_for_app(app_name: str, publisher: str, competitor_names: list[str]) -> str:
    haystack = _norm(f"{app_name} {publisher}")
    aliases = {
        "chatgpt": ["chatgpt", "openai"],
        "openai": ["chatgpt", "openai"],
        "豆包": ["豆包", "doubao", "抖音"],
        "doubao": ["豆包", "doubao", "抖音"],
        "deepseek": ["deepseek", "深度求索"],
        "deepseekr1": ["deepseek", "深度求索"],
    }
    for competitor in competitor_names:
        keys = [competitor, *aliases.get(_norm(competitor), [])]
        if any(_norm(key) and _norm(key) in haystack for key in keys):
            return competitor
    return app_name


def parse_appark_text(text: str, competitor_names: list[str] | None = None) -> dict[str, Any]:
    competitor_names = competitor_names or []
    lines = [line.strip() for line in str(text or "").splitlines() if line.strip()]
    if not lines:
        return {"enabled": False, "rows": [], "caveat": "AppArk 页面文本为空。"}

    try:
        start = lines.index("应用", lines.index("操作") if "操作" in lines else 0)
    except ValueError:
        try:
            start = lines.index("应用")
        except ValueError:
            return {"enabled": False, "rows": [], "caveat": "未找到 AppArk 指标表头。"}

    headers = ["应用", "下载量", "收入额", "免费榜排名", "付费榜排名", "排行榜排名"]
    cursor = start
    for header in headers:
        if cursor < len(lines) and lines[cursor] == header:
            cursor += 1
    tail_stop = len(lines)
    for marker in ["中文", "核心功能", "实时排行榜", "资源"]:
        if marker in lines[cursor:]:
            tail_stop = min(tail_stop, cursor + lines[cursor:].index(marker))
    tokens = lines[cursor:tail_stop]

    rows: list[dict[str, Any]] = []
    index = 0
    while index + 6 < len(tokens):
        app_name, publisher, downloads, revenue, free_rank, paid_rank, overall_rank = tokens[index:index + 7]
        if not re.search(r"\d", downloads) or not re.search(r"\d", revenue):
            index += 1
            continue
        rows.append(
            {
                "competitor": _competitor_for_app(app_name, publisher, competitor_names),
                "app_name": app_name,
                "publisher": publisher,
                "downloads_text": downloads,
                "downloads_value": parse_metric_number(downloads),
                "revenue_text": revenue,
                "revenue_usd": parse_metric_number(revenue),
                "free_rank": parse_rank(free_rank),
                "paid_rank": parse_rank(paid_rank),
                "overall_rank": parse_rank(overall_rank),
                "source": "AppArk",
            }
        )
        index += 7

    return {
        "enabled": bool(rows),
        "source": "AppArk",
        "source_url": APPARK_COMPETITOR_URL,
        "collected_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "rows": rows,
        "caveat": "" if rows else "未从 AppArk 页面解析到竞品指标。",
    }


def _read_appark_text_from_cdp() -> tuple[str, str]:
    cdp_url = os.environ.get("APPARK_CHROME_CDP_URL", "http://127.0.0.1:9222").rstrip("/")
    with urllib.request.urlopen(f"{cdp_url}/json", timeout=2) as response:
        tabs = json.loads(response.read().decode("utf-8", errors="replace"))
    target = next((tab for tab in tabs if "appark.ai" in str(tab.get("url", ""))), None)
    if not target:
        return "", "Chrome CDP 未找到 AppArk 标签页。"
    ws_url = target.get("webSocketDebuggerUrl", "")
    if not ws_url:
        return "", "AppArk 标签页未暴露 DevTools WebSocket。"
    try:
        import websocket  # type: ignore
    except Exception as exc:
        return "", f"websocket-client 不可用：{exc}"
    ws = websocket.create_connection(ws_url, timeout=3)
    try:
        ws.send(json.dumps({"id": 1, "method": "Runtime.evaluate", "params": {"expression": "document.body ? document.body.innerText : ''", "returnByValue": True}}))
        while True:
            payload = json.loads(ws.recv())
            if payload.get("id") == 1:
                return str(payload.get("result", {}).get("result", {}).get("value", "") or ""), ""
    finally:
        ws.close()


def collect_appark_metrics(competitor_names: list[str], cache_path: str | Path | None = None) -> dict[str, Any]:
    errors: list[str] = []
    if os.environ.get("APPARK_BROWSER_COLLECT_ENABLED", "1").strip().lower() not in {"0", "false", "no", "off"}:
        try:
            text, error = _read_appark_text_from_cdp()
            if text:
                result = parse_appark_text(text, competitor_names)
                result["provider"] = "chrome_cdp"
                return result
            if error:
                errors.append(error)
        except Exception as exc:
            errors.append(f"Chrome CDP 读取失败：{exc}")

    path = Path(cache_path or os.environ.get("APPARK_CACHE_PATH", "data/appark_metrics.json"))
    if path.exists():
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(payload, dict) and isinstance(payload.get("rows"), list):
                payload.setdefault("enabled", bool(payload.get("rows")))
                payload.setdefault("provider", "local_cache")
                payload.setdefault("source", "AppArk")
                payload.setdefault("source_url", APPARK_COMPETITOR_URL)
                return payload
            if isinstance(payload, str):
                result = parse_appark_text(payload, competitor_names)
                result["provider"] = "local_cache_text"
                return result
        except Exception as exc:
            errors.append(f"AppArk 缓存读取失败：{exc}")

    return {
        "enabled": False,
        "provider": "none",
        "source": "AppArk",
        "source_url": APPARK_COMPETITOR_URL,
        "rows": [],
        "caveat": "；".join(errors) or "未配置可读取的 AppArk Chrome/CDP 或本地缓存。",
    }
