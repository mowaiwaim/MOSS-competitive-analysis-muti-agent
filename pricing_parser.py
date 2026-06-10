from __future__ import annotations

import re
import sqlite3
from typing import Any


def _normalize_currency(value: str) -> str:
    lowered = (value or "").casefold()
    if lowered in {"美元", "usd"} or value == "$":
        return "USD"
    if lowered in {"元", "人民币", "rmb"} or value in {"¥", "￥"}:
        return "CNY"
    return ""


def _format_price_fact(fact: dict[str, Any]) -> str:
    amount = float(fact.get("amount", 0))
    amount_text = f"{amount:g}"
    currency = "美元" if fact.get("currency") == "USD" else "元" if fact.get("currency") == "CNY" else fact.get("currency", "")
    return f"{amount_text}{currency}/{fact.get('unit', '单位')}"


def _preferred_price_fact(facts: list[dict[str, Any]] | list[sqlite3.Row], price_type: str) -> dict[str, Any] | None:
    rows = [dict(row) for row in facts]
    typed = [row for row in rows if row.get("price_type") == price_type]
    if not typed:
        return None
    preferred_patterns = [r"v4[-_]?pro", r"gpt[-_]?5\.5", r"gpt[-_]?5", r"deepseek[-_]?chat", r"deepseek[-_]?reasoner"]
    for pattern in preferred_patterns:
        candidate = next((row for row in typed if re.search(pattern, row.get("plan_name", ""), flags=re.I)), None)
        if candidate:
            return candidate
    return max(typed, key=lambda row: float(row.get("confidence", 0)))


def prices_from_window(window: str, header_context: str = "") -> list[dict[str, Any]]:
    prices: list[dict[str, Any]] = []
    combined_context = f"{header_context} {window}"
    window_has_price_context = bool(
        re.search(
            r"token|tokens|百万|百萬|1M|/M|每百万|每百萬|输入|輸入|输出|輸出|缓存|快取|price|pricing|input|output",
            combined_context,
            flags=re.I,
        )
    )
    if re.search(r"收費計算機|收费计算机|圖像|图像|設定寬度|设置宽度|512×512", window) and not re.search(
        r"(收費|收费|价格|pricing)\s*(输入|輸入|input)",
        window,
        flags=re.I,
    ):
        return prices
    pattern = re.compile(r"(\d+(?:\.\d+)?)\s*(美元|元|人民币|RMB|USD|¥|￥)", re.I)
    for match in pattern.finditer(window):
        before = window[max(0, match.start() - 90) : match.start()]
        after = window[match.end() : min(len(window), match.end() + 56)]
        context = f"{header_context[-160:]} {before} {after}"
        if not window_has_price_context and not re.search(
            r"token|tokens|百万|百萬|1M|/M|每百万|每百萬|输入|輸入|输出|輸出|缓存|快取|price|pricing|input|output",
            context,
            flags=re.I,
        ):
            continue
        currency = _normalize_currency(match.group(2))
        if not currency:
            continue
        unit = "每百万 tokens" if re.search(r"百万|百萬|1M|/M|million|tokens?", f"{combined_context} {context}", flags=re.I) else "公开价格单位"
        label_prefix = before[-72:]
        hint_type = ""
        if re.search(r"(缓存命中|快取輸入|快取输入|cache hit|cached)[^0-9]{0,36}$", label_prefix, flags=re.I):
            hint_type = "input_cached"
        elif re.search(r"(缓存未命中|未命中|cache miss|uncached)[^0-9]{0,36}$", label_prefix, flags=re.I):
            hint_type = "input"
        elif re.search(r"(输出|輸出|output)[^0-9]{0,36}$", label_prefix, flags=re.I):
            hint_type = "output"
        elif re.search(r"(输入|輸入|input)[^0-9]{0,36}$", label_prefix, flags=re.I):
            hint_type = "input"
        prices.append(
            {
                "amount": float(match.group(1)),
                "currency": currency,
                "unit": unit,
                "context": context,
                "window_context": combined_context,
                "hint_type": hint_type,
            }
        )
    return prices[:6]


def infer_price_types(prices: list[dict[str, Any]]) -> list[str]:
    types = []
    for price in prices:
        context = str(price.get("context", ""))
        if price.get("hint_type"):
            types.append(str(price["hint_type"]))
        elif re.search(r"缓存命中|快取|cache hit|cached", context, flags=re.I):
            types.append("input_cached")
        elif re.search(r"缓存未命中|cache miss|uncached|输入|輸入|input", context, flags=re.I):
            types.append("input")
        elif re.search(r"输出|輸出|output", context, flags=re.I):
            types.append("output")
        else:
            types.append("")
    if len(prices) >= 3 and any(re.search(r"缓存|cache", str(price.get("context", "")), flags=re.I) for price in prices):
        merged_context = " ".join(str(price.get("window_context") or price.get("context", "")) for price in prices)
        deepseek_order = bool(re.search(r"(缓存命中|cache hit).{0,90}(缓存未命中|cache miss).{0,90}(输出|輸出|output)", merged_context, flags=re.I))
        openai_order = bool(re.search(r"(输入|輸入|input).{0,45}(快取|缓存命中|cached)", merged_context, flags=re.I))
        fallback = ["input_cached", "input", "output"] if deepseek_order or not openai_order else ["input", "input_cached", "output"]
        for index in range(min(len(types), len(fallback))):
            if not prices[index].get("hint_type"):
                types[index] = fallback[index]
        if "output" not in types:
            types[min(len(types), 3) - 1] = "output"
    missing = [index for index, value in enumerate(types) if not value]
    if missing:
        fallback = ["input_cached", "input", "output"] if len(prices) >= 3 else ["input", "output"]
        for offset, index in enumerate(missing):
            types[index] = fallback[min(offset, len(fallback) - 1)]
    return types


def pricing_facts_from_source(
    source: sqlite3.Row,
    competitor: str,
    title_excerpt: str,
    raw_content_status: str = "",
) -> list[dict[str, Any]]:
    text = title_excerpt
    text = re.sub(r"US\$\s*(\d+(?:\.\d+)?)", r"\1美元", text, flags=re.I)
    text = re.sub(r"\$\s*(\d+(?:\.\d+)?)", r"\1美元", text)
    text = re.sub(r"USD\s*(\d+(?:\.\d+)?)", r"\1美元", text, flags=re.I)
    model_pattern = re.compile(
        r"(?:\b(?:gpt|o\d|deepseek|doubao|seed)[A-Za-z0-9_.\-‑‐]*\b|豆包[A-Za-z0-9_.\-‑‐一-龥]*)",
        re.I,
    )
    matches = list(model_pattern.finditer(text))
    if not matches and re.search(r"\d+(?:\.\d+)?\s*(?:美元|元|人民币|RMB|USD|¥|￥)", text, flags=re.I):
        matches = [re.match(r".*", text)]
    facts: list[dict[str, Any]] = []
    for index, match in enumerate(matches):
        start = match.start() if hasattr(match, "start") else 0
        end = matches[index + 1].start() if index + 1 < len(matches) and hasattr(matches[index + 1], "start") else min(len(text), start + 420)
        header_context = text[max(0, start - 180) : start]
        window = text[start:end]
        plan_name = match.group(0) if hasattr(match, "group") else ""
        if hasattr(match, "end") and re.match(r"\s+mini\b", text[match.end() : match.end() + 12], flags=re.I):
            plan_name = f"{plan_name} mini"
        price_items = prices_from_window(window, header_context)
        inferred_types = infer_price_types(price_items)
        for price, price_type in zip(price_items, inferred_types):
            facts.append(
                {
                    "competitor_name": competitor,
                    "plan_name": plan_name,
                    "price_type": price_type,
                    "amount": price["amount"],
                    "currency": price["currency"],
                    "unit": price["unit"],
                    "region": "US" if price["currency"] == "USD" else "CN",
                    "confidence": 0.92 if raw_content_status == "fetched" else 0.82,
                }
            )
    deduped: list[dict[str, Any]] = []
    seen: set[tuple[str, str, float, str]] = set()
    for fact in facts:
        key = (fact["plan_name"].casefold(), fact["price_type"], round(float(fact["amount"]), 6), fact["currency"])
        if key in seen:
            continue
        seen.add(key)
        deduped.append(fact)
    return deduped


def pricing_claim_text_from_facts(competitor: str, facts: list[dict[str, Any]]) -> str:
    output_fact = _preferred_price_fact(facts, "output")
    input_fact = _preferred_price_fact(facts, "input")
    cached_fact = _preferred_price_fact(facts, "input_cached")
    parts = []
    if cached_fact:
        parts.append(f"{cached_fact['plan_name']} 输入缓存命中 {_format_price_fact(cached_fact)}")
    if input_fact:
        parts.append(f"{input_fact['plan_name']} 输入 {_format_price_fact(input_fact)}")
    if output_fact:
        parts.append(f"{output_fact['plan_name']} 输出 {_format_price_fact(output_fact)}")
    if not parts:
        parts = [f"{fact['plan_name']} {fact['price_type']} {_format_price_fact(fact)}" for fact in facts[:3]]
    return f"{competitor} 官方价格口径：{'；'.join(parts[:4])}。金额按采集日官方来源记录，报告不使用第三方转述替代官方价格。"
