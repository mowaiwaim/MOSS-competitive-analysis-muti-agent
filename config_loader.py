from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


CONFIG_DIR = Path(__file__).resolve().parent / "data"
CONFIG_PATH = CONFIG_DIR / "app_config.json"


def _load_config() -> dict[str, Any]:
    raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    return raw


_cfg = _load_config()

PRODUCT_URL_HINTS: dict[str, list[str]] = _cfg["product_url_hints"]
PRODUCT_ALIASES: dict[str, list[str]] = _cfg["product_aliases"]
PRODUCT_RELATED_TERMS: dict[str, list[str]] = _cfg["product_related_terms"]

INDUSTRY_RELATED_TERMS: list[tuple[re.Pattern, list[str]]] = [
    (re.compile(entry["pattern"]), entry["terms"])
    for entry in _cfg["industry_related_terms"]
]

SEARCH_HIGH_AUTHORITY_DOMAINS: list[str] = _cfg["search_high_authority_domains"]
SEARCH_MEDIUM_AUTHORITY_DOMAINS: list[str] = _cfg["search_medium_authority_domains"]
SEARCH_LOW_VALUE_DOMAINS: list[str] = _cfg["search_low_value_domains"]
PRODUCT_SEARCH_QUERIES: dict[str, str] = _cfg["product_search_queries"]
OFFICIAL_SOURCE_SEEDS: dict[str, list[dict[str, str]]] = _cfg["official_source_seeds"]

SEARCH_BLOCK_HOSTS: dict[str, str] = _cfg.get("search_block_hosts", {})
SEARCH_BLOCK_HOSTS_SELF_EXEMPT: bool = _cfg.get("search_block_hosts_self_exempt", True)
SEARCH_LOW_VALUE_DOMAINS_BY_INDUSTRY: dict[str, list[str]] = _cfg.get("search_low_value_domains_by_industry", {})
SEARCH_HIGH_VALUE_DOMAINS_BY_INDUSTRY: dict[str, list[str]] = _cfg.get("search_high_value_domains_by_industry", {})

# Convert dict-based prices to tuples for backwards compatibility
REFERENCE_AI_API_PRICES: dict[str, list[tuple[str, str, float, str]]] = {
    name: [
        (entry["plan_name"], entry["price_type"], entry["amount"], entry["currency"])
        for entry in entries
    ]
    for name, entries in _cfg["reference_ai_api_prices"].items()
}
