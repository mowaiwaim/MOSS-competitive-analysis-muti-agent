from __future__ import annotations

import hashlib
import os
import re
import site
import sys
import textwrap
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any, Literal
from urllib.parse import urlparse


def _enable_user_site_packages() -> None:
    try:
        user_site = site.getusersitepackages()
    except Exception:
        return
    if user_site and os.path.isdir(user_site) and user_site not in sys.path:
        sys.path.insert(0, user_site)


_enable_user_site_packages()

import requests

try:
    from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
    from langchain_openai import ChatOpenAI
    from langgraph.graph import END, StateGraph
    from langgraph.graph.message import add_messages
    from langgraph.prebuilt import ToolNode
    from typing_extensions import TypedDict
    _LANGGRAPH_IMPORT_ERROR: Exception | None = None
except Exception as exc:
    AIMessage = BaseMessage = HumanMessage = SystemMessage = None  # type: ignore[assignment]
    ChatOpenAI = StateGraph = ToolNode = None  # type: ignore[assignment]
    END = None  # type: ignore[assignment]
    add_messages = None  # type: ignore[assignment]
    TypedDict = None  # type: ignore[assignment]
    _LANGGRAPH_IMPORT_ERROR = exc

if _LANGGRAPH_IMPORT_ERROR is None:
    class AgentState(TypedDict):
        messages: Annotated[list[BaseMessage], add_messages]
else:
    AgentState = None  # type: ignore[assignment]


REACT_REPORT_H2 = [
    "一、报告概述（Executive Summary）",
    "二、市场与赛道分析（Market Context）",
    "三、竞品选择与分层（Competitive Landscape）",
    "四、核心能力拆解（Product Capability Analysis）",
    "五、商业模式分析（Monetization）",
    "六、增长与分发策略（Growth Strategy）",
    "七、用户与场景分析（User & Use Case）",
    "八、优劣势对比（SWOT / 对比矩阵）",
    "九、关键差异与壁垒（Moat Analysis）",
    "十、机会点与策略建议（Opportunities）",
    "十一、数据附录（Appendix）",
]


SYSTEM_PROMPT = """你是一名专业的竞争情报（Competitive Intelligence）分析师，擅长市场竞争分析和产品定位策略。

铁律：
1. 在所有竞品全部搜集完毕之前，你只能调用工具，不得输出解释性过渡文字。
2. 最终报告必须以 "# 竞品调研报告：" 或 "# 竞争情报报告：" 开头。
3. 每个关键结论必须附来源 URL；信息不足必须标注“待核实”或“未公开”。
4. 最终报告总字数必须尽量 >= 5000 字；竞品超过 6 个时应更长。
5. 不要编造价格、用户口碑、销量、融资、功能发布时间等时间敏感事实。
6. 不得写入未在来源或工具结果中出现的模型版本、未来产品名、市场规模、用户规模、价格数字或增长率；没有直接来源时只能写“待核实”，不能用常识补全。
7. Markdown 必须保留真实换行：二级/三级/四级标题、列表、引用、表格各自独立成行，不能压成一段。

工作流程：
阶段一：逐个搜集所有竞品信息。官网优先，交叉验证。优先搜索官网、pricing/plans、features/product、docs/help、blog/news、changelog/release notes、评测媒体、应用商店、社区、新闻稿、案例库。
阶段二：一次性输出 11 章深度报告，严格使用以下二级标题。报告风格必须像咨询/PMM 竞品研究长文，不要像系统日志、模块卡片或简短摘要：

## 一、报告概述（Executive Summary）
## 二、市场与赛道分析（Market Context）
## 三、竞品选择与分层（Competitive Landscape）
## 四、核心能力拆解（Product Capability Analysis）
## 五、商业模式分析（Monetization）
## 六、增长与分发策略（Growth Strategy）
## 七、用户与场景分析（User & Use Case）
## 八、优劣势对比（SWOT / 对比矩阵）
## 九、关键差异与壁垒（Moat Analysis）
## 十、机会点与策略建议（Opportunities）
## 十一、数据附录（Appendix）

额外约束：
- 第四章中，每个竞品使用三级或四级标题单列小节。
- 第二章、第三章、第五章、第六章、第七章、第八章、第九章、第十章必须使用 2.1、3.1、5.1 这类三级标题组织论证。
- 第四章每个竞品尽量包含 8 个字段：定位、核心功能、价格、用户分层、近期更新、分发渠道、商业模式、风险短板。每个字段都要写成 "- **字段名**：..."。
- 对公开信息不足的竞品，也要列出同样字段，但明确写“待核实/未公开”，不要省略。
- 第三章末尾必须给“竞争态势矩阵”；第四章末尾必须给“核心能力对比”；第五章末尾必须给“商业模式对比”；第六章末尾必须给“增长策略对比”；第七章末尾必须给“用户场景对比”；第八章末尾必须给“SWOT 对比矩阵”；第九章末尾必须给“差异化、壁垒与避雷对比”。这些都必须是 Markdown 表格，列为“维度/场景/竞品 + 各竞品”，行要覆盖核心比较维度。
- 对比表不要直接展示 URL；正文事实可保留来源 URL，系统会在渲染层转换为参考文献式超链接。
- 所有量化表述、模型规格、发布时间、用户规模和价格必须在同段或同条 bullet 里给出 URL；否则改写为证据缺口。
- 若截图工具返回 Markdown 图片行，可以原样写入第四章或附录。
- 附录必须包含来源列表与测试方法。
"""


@dataclass
class ReactReportResult:
    enabled: bool
    provider: str
    markdown: str
    sections: list[dict[str, Any]]
    execution_mode: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    screenshots: list[str] = field(default_factory=list)
    fallback_reason: str = ""
    token_input: int = 0
    token_output: int = 0


def estimate_tokens(text: str) -> int:
    return max(1, len(text or "") // 4)


def compact_text(value: str, limit: int = 800) -> str:
    return re.sub(r"\s+", " ", value or "").strip()[:limit]


ZHIPU_SAFETY_PATTERNS: tuple[tuple[str, str], ...] = (
    (
        "politics_conflict",
        r"习近平|中共|共产党|政治局|台湾|香港|新疆|西藏|人权|制裁|战争|军事|导弹|核武|恐怖|袭击|暴乱|示威|抗议|间谍|国家安全|地缘政治",
    ),
    (
        "crime_violence",
        r"色情|成人|裸露|性侵|未成年|自杀|毒品|赌博|诈骗|洗钱|黑产|仇恨|歧视|暴力|枪击|爆炸|伤亡|杀害|死亡",
    ),
    (
        "cyber_privacy",
        r"破解|越狱|绕过|木马|恶意软件|黑客攻击|勒索软件|泄露|数据泄漏|隐私泄露|API\s*KEY|access\s*token|secret\s*key",
    ),
)


def _zhipu_safety_filter_enabled() -> bool:
    value = os.environ.get("ZHIPU_SAFETY_FILTER_ENABLED", "1").strip().lower()
    return value not in {"0", "false", "no", "off"}


def _zhipu_drop_safety_hit_sources() -> bool:
    value = os.environ.get("ZHIPU_DROP_SAFETY_HIT_SOURCES", "1").strip().lower()
    return value not in {"0", "false", "no", "off"}


def _zhipu_excluded_source_ids() -> set[str]:
    raw = os.environ.get("ZHIPU_EXCLUDED_SOURCE_IDS", "")
    return {item.strip() for item in re.split(r"[\n,;]+", raw) if item.strip()}


def _sanitize_zhipu_text(text: Any) -> tuple[str, list[str]]:
    sanitized = str(text or "")
    tags: list[str] = []
    if not sanitized:
        return sanitized, tags
    for tag, pattern in ZHIPU_SAFETY_PATTERNS:
        if re.search(pattern, sanitized, flags=re.I):
            tags.append(tag)
            sanitized = re.sub(pattern, f"[已过滤:{tag}]", sanitized, flags=re.I)
    return sanitized, tags


def _sanitize_zhipu_sources(sources: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if not _zhipu_safety_filter_enabled():
        return sources, {"enabled": False, "filtered_source_count": 0, "categories": [], "source_ids": []}
    sanitized_sources: list[dict[str, Any]] = []
    source_ids: list[str] = []
    dropped_source_ids: list[str] = []
    categories: set[str] = set()
    hit_count = 0
    excluded_ids = _zhipu_excluded_source_ids()
    drop_hit_sources = _zhipu_drop_safety_hit_sources()
    for source in sources:
        item = dict(source)
        source_id = str(item.get("id") or item.get("source_id") or "")[:120]
        if source_id and source_id in excluded_ids:
            dropped_source_ids.append(source_id)
            continue
        item_tags: set[str] = set()
        for field in ("title", "source_title", "excerpt"):
            cleaned, tags = _sanitize_zhipu_text(item.get(field, ""))
            if tags:
                item[field] = cleaned
                item_tags.update(tags)
        if item_tags:
            hit_count += len(item_tags)
            categories.update(item_tags)
            source_ids.append(source_id[:80])
            if drop_hit_sources:
                dropped_source_ids.append(source_id)
                continue
        sanitized_sources.append(item)
    return sanitized_sources, {
        "enabled": True,
        "filtered_source_count": len([item for item in source_ids if item]),
        "dropped_source_count": len([item for item in dropped_source_ids if item]),
        "hit_count": hit_count,
        "categories": sorted(categories),
        "source_ids": [item for item in source_ids if item][:24],
        "dropped_source_ids": [item for item in dropped_source_ids if item][:36],
    }


def _sanitize_zhipu_claims(claims: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if not _zhipu_safety_filter_enabled():
        return claims, {"enabled": False, "filtered_claim_count": 0, "categories": []}
    sanitized_claims: list[dict[str, Any]] = []
    categories: set[str] = set()
    hit_count = 0
    for claim in claims:
        item = dict(claim)
        item_tags: set[str] = set()
        for field in ("section", "content", "counter_evidence", "uncertainty"):
            cleaned, tags = _sanitize_zhipu_text(item.get(field, ""))
            if tags:
                item[field] = cleaned
                item_tags.update(tags)
        if item_tags:
            hit_count += 1
            categories.update(item_tags)
        sanitized_claims.append(item)
    return sanitized_claims, {
        "enabled": True,
        "filtered_claim_count": hit_count,
        "categories": sorted(categories),
    }


def _failover_diagnostic_calls(provider_name: str, tool_calls: list[dict[str, Any]], error: str) -> list[dict[str, Any]]:
    diagnostics = [
        dict(call)
        for call in tool_calls
        if str(call.get("name", "")).startswith("zhipu_")
    ]
    if diagnostics or "1301" in error or "contentFilter" in error or "timeout" in error.lower():
        diagnostics.append({
            "name": "react_provider_failover_diagnostics",
            "provider": provider_name,
            "result": "captured",
            "error": error,
        })
    return diagnostics


def safe_error(exc: BaseException) -> str:
    text = f"{exc.__class__.__name__}: {exc}"
    for pattern in [
        r"sk-[A-Za-z0-9_\-]{12,}",
        r"Bearer\s+[A-Za-z0-9._\-]+",
        r"(api[_-]?key|secret|token)\s*[:=]\s*[^,\s]+",
    ]:
        text = re.sub(pattern, "[REDACTED]", text, flags=re.I)
    return compact_text(text, 300)


def _is_enabled_by_env() -> bool:
    value = os.environ.get("REACT_REPORT_ENABLED", "1").strip().lower()
    return value not in {"0", "false", "no", "off"}


def _strict_provider_from_env() -> str:
    preferred = os.environ.get("REACT_AGENT_PROVIDER", "").strip().lower()
    if preferred in {"deepseek", "deepseek-react", "deepseek-v4-pro", "deepseek-v4-flash"}:
        return "deepseek-react"
    if preferred in {"zhipu", "zhipu-react", "glm", "glm-react"}:
        return "zhipu-react"
    if preferred in {"doubao", "doubao-react"}:
        return "doubao-react"
    return ""


_LAST_PROVIDER_STATUS: dict[str, str] = {
    "last_success_provider": "",
    "last_failover_reason": "",
}


def _read_int_env(name: str, default: int, minimum: int = 60, maximum: int = 1800) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except ValueError:
        value = default
    return max(minimum, min(maximum, value))


def _provider_timeout_seconds(provider_name: str) -> int:
    fallback = _read_int_env("REACT_AGENT_MAX_SECONDS", 600)
    if provider_name == "deepseek-react":
        return _read_int_env("DEEPSEEK_REACT_MAX_SECONDS", 900, maximum=2400)
    if provider_name == "zhipu-react":
        return _read_int_env("ZHIPU_REACT_MAX_SECONDS", 600, maximum=1800)
    if provider_name == "doubao-react":
        return _read_int_env("DOUBAO_REACT_MAX_SECONDS", 450, maximum=1800)
    return fallback


def _provider_public(provider: dict[str, Any]) -> dict[str, Any]:
    return {
        "provider": provider.get("provider", ""),
        "api_key_configured": bool(provider.get("api_key")),
        "base_url": provider.get("base_url", ""),
        "model": provider.get("model", ""),
        "timeout_seconds": int(provider.get("timeout_seconds") or 0),
    }


def _provider_extra_body(provider_name: str) -> dict[str, Any] | None:
    if provider_name != "deepseek-react":
        return None
    thinking_type = os.environ.get("DEEPSEEK_THINKING_TYPE", "enabled").strip().lower()
    if not thinking_type or thinking_type in {"0", "false", "no", "off"}:
        return None
    return {"thinking": {"type": thinking_type}}


def _provider_reasoning_effort(provider_name: str) -> str | None:
    if provider_name != "deepseek-react":
        return None
    value = os.environ.get("DEEPSEEK_REASONING_EFFORT", "").strip().lower()
    return value or None


def _configured_providers() -> list[dict[str, Any]]:
    preferred = os.environ.get("REACT_AGENT_PROVIDER", "").strip().lower()
    strict_provider = _strict_provider_from_env()
    deepseek_key = os.environ.get("DEEPSEEK_API_KEY", "")
    zhipu_key = os.environ.get("ZHIPU_API_KEY", "")
    zhipu_model = os.environ.get("ZHIPU_MODEL", "")
    doubao_key = os.environ.get("DOUBAO_API_KEY", "")
    doubao_model = os.environ.get("DOUBAO_ENDPOINT_ID", "") or os.environ.get("DOUBAO_MODEL_NAME", "")

    deepseek_provider = {
        "provider": "deepseek-react",
        "api_key": deepseek_key,
        "base_url": os.environ.get("DEEPSEEK_API_BASE_URL", "https://api.deepseek.com").rstrip("/"),
        "model": os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-pro"),
        "timeout_seconds": _provider_timeout_seconds("deepseek-react"),
        "extra_body": _provider_extra_body("deepseek-react"),
        "reasoning_effort": _provider_reasoning_effort("deepseek-react"),
    } if deepseek_key else None
    zhipu_provider = {
        "provider": "zhipu-react",
        "api_key": zhipu_key,
        "base_url": os.environ.get("ZHIPU_BASE_URL", "https://open.bigmodel.cn/api/paas/v4").rstrip("/"),
        "model": zhipu_model,
        "timeout_seconds": _provider_timeout_seconds("zhipu-react"),
    } if zhipu_key and zhipu_model else None
    doubao_provider = {
        "provider": "doubao-react",
        "api_key": doubao_key,
        "base_url": os.environ.get("DOUBAO_BASE_URL", "https://ark.cn-beijing.volces.com/api/v3").rstrip("/"),
        "model": doubao_model,
        "timeout_seconds": _provider_timeout_seconds("doubao-react"),
    } if doubao_key and doubao_model else None

    if strict_provider == "deepseek-react":
        preferred_order = [deepseek_provider]
    elif strict_provider == "zhipu-react":
        preferred_order = [zhipu_provider]
    elif strict_provider == "doubao-react":
        preferred_order = [doubao_provider]
    else:
        preferred_order = [deepseek_provider, zhipu_provider, doubao_provider]
    providers: list[dict[str, Any]] = []
    for provider in preferred_order:
        if provider and provider["provider"] not in {item["provider"] for item in providers}:
            providers.append(provider)
    return providers


def _pick_provider() -> dict[str, Any]:
    providers = _configured_providers()
    if providers:
        return providers[0]
    return {"provider": "local-react-fallback", "api_key": "", "base_url": "", "model": ""}


def react_provider_status() -> dict[str, Any]:
    provider = _pick_provider()
    providers = _configured_providers()
    return {
        "provider": provider.get("provider", ""),
        "configured_provider": provider.get("provider", ""),
        "api_key_configured": bool(provider.get("api_key")),
        "base_url": provider.get("base_url", ""),
        "model": provider.get("model", ""),
        "enabled": _is_enabled_by_env(),
        "preferred_provider": os.environ.get("REACT_AGENT_PROVIDER", "").strip().lower(),
        "strict_provider": _strict_provider_from_env(),
        "preferred_order": [_provider_public(item) for item in providers],
        "react_timeout_seconds": {
            "default": _read_int_env("REACT_AGENT_MAX_SECONDS", 600),
            "deepseek-react": _provider_timeout_seconds("deepseek-react"),
            "zhipu-react": _provider_timeout_seconds("zhipu-react"),
            "doubao-react": _provider_timeout_seconds("doubao-react"),
        },
        "last_success_provider": _LAST_PROVIDER_STATUS.get("last_success_provider", ""),
        "last_failover_reason": _LAST_PROVIDER_STATUS.get("last_failover_reason", ""),
    }


def _source_lines(sources: list[dict[str, Any]], limit: int = 36) -> list[str]:
    lines = []
    for index, source in enumerate(sources[:limit], start=1):
        url = source.get("url_or_path") or source.get("url") or ""
        title = compact_text(str(source.get("title", "") or source.get("source_title", "")), 120)
        excerpt = compact_text(str(source.get("excerpt", "")), 220)
        competitor = compact_text(str(source.get("competitor_name", "")), 80)
        lines.append(
            f"{index}. [{source.get('id', '')}] {competitor} {title} {url} 摘要：{excerpt}"
        )
    return lines


def _claim_lines(claims: list[dict[str, Any]], limit: int = 36) -> list[str]:
    lines = []
    for index, claim in enumerate(claims[:limit], start=1):
        source_ids = claim.get("source_ids") or []
        lines.append(
            f"{index}. {claim.get('section', '')}｜{compact_text(str(claim.get('content', '')), 260)}"
            f"｜confidence={claim.get('confidence', '')}｜sources={source_ids}"
        )
    return lines


def build_user_task(task: dict[str, Any], sources: list[dict[str, Any]], claims: list[dict[str, Any]]) -> str:
    competitors = [str(item) for item in task.get("competitors", []) if str(item).strip()]
    focus = [str(item) for item in task.get("focus_areas", []) if str(item).strip()]
    source_block = "\n".join(_source_lines(sources)) or "暂无已入库来源。"
    claim_block = "\n".join(_claim_lines(claims)) or "暂无已质检结论。"
    return (
        "请基于系统已入库证据，并在必要时继续联网搜索，生成正式竞品调研报告。\n\n"
        f"任务名称：{task.get('name', '')}\n"
        f"行业/市场：{task.get('industry', '')}\n"
        f"竞品：{'、'.join(competitors)}\n"
        f"关注维度：{'、'.join(focus) or '功能、价格、用户、增长、SWOT'}\n\n"
        "已入库来源（可优先使用，报告中的来源 URL 应尽量来自这里或工具搜索结果）：\n"
        f"{source_block}\n\n"
        "已通过系统 1 多 Agent 链路形成的结构化结论：\n"
        f"{claim_block}\n\n"
        "请按系统提示先补足公开信息，再输出完整 11 章 Markdown 报告。"
        "最终正文应以长文形式直接呈现，不要输出 JSON，不要输出执行过程。"
        "请参考这样的密度：报告概述给 4-6 条核心发现；市场章节拆趋势、需求、技术；"
        "竞品选择章节做分层；核心能力章节逐个竞品拆 8 个字段；商业模式、增长、用户、SWOT、壁垒、机会都要有三级标题和可执行判断。"
        "核心能力、商业模式、增长策略、用户场景、SWOT、差异化/壁垒章节末尾都要追加 Markdown 对比表，表格只放参考文献编号或普通文本，不直接铺网址。"
    )


def _zhipu_direct_safe_mode_enabled() -> bool:
    value = os.environ.get("ZHIPU_DIRECT_SAFE_MODE", "1").strip().lower()
    return value not in {"0", "false", "no", "off"}


def _deepseek_direct_thinking_mode_enabled() -> bool:
    value = os.environ.get("DEEPSEEK_DIRECT_THINKING_MODE", "1").strip().lower()
    return value not in {"0", "false", "no", "off"}


def _zhipu_direct_system_prompt() -> str:
    headings = "\n".join(f"## {title}" for title in REACT_REPORT_H2)
    return (
        "你是竞争情报研究员。请只基于用户提供的已过滤资料和结论，写一份正式中文竞品深度报告。\n"
        "不要调用外部资料，不要讨论已排除来源，不要输出执行过程，不要输出 JSON。\n"
        "所有不确定信息写为“待核实”；价格、用户规模、发布日期、排名和政策类信息必须保留来源 URL 或写为待核实。\n"
        "正文必须包含以下 11 个二级标题，标题文字必须逐字保持一致：\n"
        f"{headings}\n"
        "每章至少包含 2 个三级标题；第四章按每个竞品分别拆定位、核心功能、价格、用户分层、近期更新、分发渠道、商业模式、风险短板。"
        "第四至第九章末尾必须追加 Markdown 对比表：核心能力对比、商业模式对比、增长策略对比、用户场景对比、SWOT 对比矩阵、差异化/壁垒/避雷对比。"
    )


def _run_zhipu_direct_safe_report(
    provider: dict[str, Any],
    user_task: str,
    competitors: list[str],
    max_seconds: int,
    tool_calls: list[dict[str, Any]],
) -> tuple[str, list[dict[str, Any]]]:
    safe_user_task, safety_hits = _sanitize_zhipu_text(user_task)
    if safety_hits:
        tool_calls.append({
            "name": "zhipu_direct_prompt_safety_filter",
            "provider": provider["provider"],
            "result": f"{len(set(safety_hits))} categories filtered",
            "categories": sorted(set(safety_hits)),
        })
    llm = ChatOpenAI(
        model=provider["model"],
        api_key=provider["api_key"],
        base_url=provider["base_url"],
        temperature=float(os.environ.get("REACT_AGENT_TEMPERATURE", "0.3")),
        max_tokens=int(os.environ.get("REACT_AGENT_MAX_TOKENS", "8192")),
        timeout=max(30, max_seconds),
        extra_body=provider.get("extra_body"),
    )
    response = llm.invoke(
        [
            SystemMessage(content=_zhipu_direct_system_prompt()),
            HumanMessage(content=safe_user_task),
        ]
    )
    content = str(getattr(response, "content", "") or "")
    tool_calls.append({
        "name": "zhipu_direct_safe_report",
        "provider": provider["provider"],
        "timeout_seconds": max_seconds,
        "result": f"{len(content)} chars",
    })
    return content, tool_calls


def _run_deepseek_direct_thinking_report(
    provider: dict[str, Any],
    user_task: str,
    max_seconds: int,
    tool_calls: list[dict[str, Any]],
) -> tuple[str, list[dict[str, Any]]]:
    llm = ChatOpenAI(
        model=provider["model"],
        api_key=provider["api_key"],
        base_url=provider["base_url"],
        temperature=float(os.environ.get("REACT_AGENT_TEMPERATURE", "0.3")),
        max_tokens=int(os.environ.get("REACT_AGENT_MAX_TOKENS", "16384")),
        timeout=max(30, max_seconds),
        extra_body=provider.get("extra_body"),
        reasoning_effort=provider.get("reasoning_effort"),
    )
    response = llm.invoke(
        [
            SystemMessage(content=_zhipu_direct_system_prompt()),
            HumanMessage(content=user_task),
        ]
    )
    content = str(getattr(response, "content", "") or "")
    reasoning_content = str((getattr(response, "additional_kwargs", {}) or {}).get("reasoning_content") or "")
    tool_calls.append({
        "name": "deepseek_direct_thinking_report",
        "provider": provider["provider"],
        "timeout_seconds": max_seconds,
        "thinking": (provider.get("extra_body") or {}).get("thinking", {}),
        "result": f"{len(content)} chars",
        "reasoning_chars": len(reasoning_content),
    })
    return content, tool_calls


def _ensure_formal_report_title(markdown: str, task: dict[str, Any]) -> str:
    text = str(markdown or "").strip()
    if text.startswith("# 竞争情报报告：") or text.startswith("# 竞品调研报告："):
        return text
    industry = str(task.get("industry") or task.get("name") or "竞品分析").strip()
    text = re.sub(r"^#\s+.+?(?:\n+|$)", "", text, count=1).strip()
    return f"# 竞品调研报告：{industry}\n\n{text}".strip()


def _coerce_zhipu_report_structure(markdown: str, task: dict[str, Any]) -> str:
    text = _ensure_formal_report_title(markdown, task)
    exact_count = sum(1 for title in REACT_REPORT_H2 if f"## {title}" in text)
    if exact_count >= 8:
        for title in REACT_REPORT_H2:
            if f"## {title}" not in text:
                text += f"\n\n## {title}\n\n待核实：智谱已返回正文，但该章节标题缺失，需后续补充结构化内容。"
        return text

    lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        lines.append(stripped)
    body = "\n\n".join(lines).strip() or "待核实：智谱返回内容为空，需要重新分析。"
    paragraphs = [item.strip() for item in re.split(r"\n{2,}", body) if item.strip()]
    chunks = [""] * len(REACT_REPORT_H2)
    current = 0
    target_len = max(450, len(body) // max(1, len(REACT_REPORT_H2)))
    for paragraph in paragraphs:
        if current < len(chunks) - 1 and len(chunks[current]) >= target_len:
            current += 1
        chunks[current] = f"{chunks[current]}\n\n{paragraph}".strip()
    for index, chunk in enumerate(chunks):
        if not chunk:
            chunks[index] = "待核实：智谱已返回报告正文，但该章节内容不足，需要后续补充。"

    title_line = text.splitlines()[0] if text.startswith("#") else _ensure_formal_report_title("", task).splitlines()[0]
    sections = [title_line]
    for title, chunk in zip(REACT_REPORT_H2, chunks):
        sections.append(f"## {title}\n\n{chunk}")
    return "\n\n".join(sections).strip()


def _ensure_min_source_urls(markdown: str, user_task: str, competitors: list[str]) -> str:
    min_url_count = max(4, min(12, len(competitors) * 3))
    if len(re.findall(r"https?://[^\s)]+", markdown)) >= min_url_count:
        return markdown
    urls = list(dict.fromkeys(re.findall(r"https?://[^\s)]+", user_task)))[:min_url_count]
    if not urls:
        return markdown
    appendix = "\n".join(f"- 来源：{url}" for url in urls)
    return f"{markdown}\n\n### 附录来源补充\n\n{appendix}"


def _parse_markdown_sections(markdown: str) -> list[dict[str, Any]]:
    sections: list[dict[str, Any]] = []
    matches = list(re.finditer(r"^##\s+(.+?)\s*$", markdown or "", flags=re.MULTILINE))
    for index, match in enumerate(matches):
        title = match.group(1).strip()
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(markdown)
        body = markdown[start:end].strip()
        sections.append({"key": f"react_{index + 1}", "title": title, "body": body, "markdown": body})
    return sections


def _report_is_complete(markdown: str, competitors: list[str]) -> bool:
    stripped = (markdown or "").lstrip()
    if not (stripped.startswith("# 竞争情报报告：") or stripped.startswith("# 竞品调研报告：")):
        return False
    for title in REACT_REPORT_H2:
        if f"## {title}" not in markdown:
            return False
    if len(re.findall(r"https?://[^\s)]+", markdown)) < max(4, min(12, len(competitors) * 3)):
        return False
    return len(markdown) >= 3500


def _report_completion_reason(markdown: str, competitors: list[str]) -> str:
    stripped = (markdown or "").lstrip()
    if not (stripped.startswith("# 竞争情报报告：") or stripped.startswith("# 竞品调研报告：")):
        return "缺少正式报告标题"
    missing = [title for title in REACT_REPORT_H2 if f"## {title}" not in markdown]
    if missing:
        return f"章节不完整：缺少 {len(missing)} 章（首个缺失：{missing[0]}）"
    url_count = len(re.findall(r"https?://[^\s)]+", markdown))
    min_url_count = max(4, min(12, len(competitors) * 3))
    if url_count < min_url_count:
        return f"来源 URL 不足：{url_count}/{min_url_count}"
    if len(markdown) < 3500:
        return f"正文过短：{len(markdown)}/3500 字符"
    return ""


def _fallback_report(task: dict[str, Any], sources: list[dict[str, Any]], claims: list[dict[str, Any]], reason: str) -> ReactReportResult:
    competitors = [str(item) for item in task.get("competitors", []) if str(item).strip()] or ["目标竞品"]
    industry = task.get("industry", "目标市场")
    source_lines = _source_lines(sources, 40)
    focus_areas = [str(item) for item in task.get("focus_areas", []) if str(item).strip()]

    def source_url(source: dict[str, Any]) -> str:
        url = str(source.get("url_or_path") or source.get("url") or "").strip()
        return url if url.startswith(("http://", "https://")) else ""

    def source_title(source: dict[str, Any]) -> str:
        return compact_text(str(source.get("title", "") or source.get("author_site", "") or source.get("id", "来源")), 80)

    def sources_for_name(name: str) -> list[dict[str, Any]]:
        lowered = name.lower()
        matched = [
            source for source in sources
            if lowered in str(source.get("competitor_name", "")).lower()
            or lowered in str(source.get("title", "")).lower()
            or lowered in str(source.get("excerpt", "")).lower()
        ]
        return matched or sources[:3]

    def cite(name: str) -> str:
        for source in sources_for_name(name):
            url = source_url(source)
            if url:
                return f"来源：{url}"
        return "来源：系统已入库材料，详见报告末尾来源目录"

    claims_by_competitor: dict[str, list[str]] = {name: [] for name in competitors}
    for claim in claims:
        text = compact_text(str(claim.get("content", "")), 320)
        if not text:
            continue
        assigned = False
        for name in competitors:
            if name.lower() in text.lower():
                claims_by_competitor.setdefault(name, []).append(text)
                assigned = True
                break
        if not assigned:
            claims_by_competitor.setdefault(competitors[0], []).append(text)

    def claim_hint(name: str, index: int, fallback: str) -> str:
        items = claims_by_competitor.get(name, [])
        return items[index] if index < len(items) else fallback

    def competitor_block(name: str, index: int) -> str:
        name_sources = sources_for_name(name)
        source_hint = cite(name)
        source_count = len(name_sources)
        return f"""### 4.{index} {name}
- **定位**：{claim_hint(name, 0, f"{name} 的定位需要结合官网、产品页和公开评价继续校准；本轮已获得 {source_count} 条相关来源。")}（{source_hint}）
- **核心功能**：{claim_hint(name, 1, "以已入库材料看，核心功能应优先从官网功能页、帮助文档和价格页确认；未覆盖功能不得写成确定事实。")}
- **差异化**：{claim_hint(name, 2, "差异化判断应落到具体场景、工作流、性能、生态或价格门槛，而不是只写品牌强弱。")}
- **价格**：若未抓到官方价格页或套餐页，应标注为“未公开/待核实”；若系统价格解析已有记录，应以后端 pricing facts 为准。
- **用户分层**：建议至少区分轻量个人用户、专业用户、团队用户和企业/采购决策者，避免把所有用户需求合并为单一画像。
- **近期更新**：近期功能、模型、套餐、渠道和政策变化必须来自官网新闻、发布说明、应用商店或可信媒体；当前未命中时保留为待核实。
- **分发渠道**：优先核验官网、Web 产品入口、App Store、Google Play、插件/集成市场、社区和内容渠道。
- **商业模式**：可从免费试用、订阅、按量计费、企业版、API/SDK、广告或增值服务几个角度判断，缺少证据时不要写成事实。
- **风险短板**：主要关注功能同质化、价格不透明、数据隐私、平台依赖、迁移成本和用户留存压力；具体风险需要继续用评论和案例交叉验证。"""

    source_appendix = "\n".join(f"- {line}" for line in source_lines[:24]) or "- 暂无可列示来源。"
    competitor_sections = "\n\n".join(competitor_block(name, index) for index, name in enumerate(competitors, start=1))
    competitor_names = "、".join(competitors)
    focus_text = "、".join(focus_areas) or "功能、定价、用户评价、用户画像、SWOT"
    markdown = f"""# 竞品调研报告：{industry}

## 一、报告概述（Executive Summary）
本报告围绕 {competitor_names} 在 {industry} 赛道中的产品定位、核心能力、商业化路径、用户场景、增长方式和潜在壁垒展开。当前报告优先使用系统 1 已入库的来源、证据分片和通过质检的结构化结论，并以系统 2 的 11 章深度报告结构组织正文。由于本轮未实际调用 ReAct 模型或工具，降级原因是：{reason}。

核心发现：
- **竞争判断要从“功能列表”推进到“工作流位置”**：仅比较功能是否存在不足以支撑产品决策，更重要的是各竞品在用户真实任务中的入口、连续使用频率、协作方式和替代成本。
- **价格与商业化是高风险信息**：套餐、API 单价、折扣和权益经常变化，必须以官方价格页、销售材料或可信第三方页面为依据；未抓到明确来源时只能标注“待核实”。
- **用户口碑需要交叉验证**：单一官网或营销页面只能说明厂商叙事，用户评价、社区反馈、应用商店评论和案例库才能支撑体验判断。
- **系统 1 的可视化仍然有价值**：热力图、定位图、SWOT、决策矩阵和来源目录适合做管理层快速浏览；本章正文则承担系统 2 风格的深度解释。
- **下一步应补强动态证据**：建议配置 DeepSeek Key，让 DeepSeek direct thinking 优先生成高质量长报告；需要搜索、抓页或截图补证时，再由内层 StateGraph ReAct 工具循环执行。

## 二、市场与赛道分析（Market Context）
### 2.1 市场结构与增长逻辑
{industry} 的竞争结构通常不是单纯的“谁功能更多”，而是围绕用户任务链条形成多个入口：轻量试用入口、专业生产入口、团队协作入口、企业采购入口和开发者/API 入口。对 {competitor_names} 的分析应把这些入口拆开，否则容易把面向不同人群的产品混在同一张表里比较。

### 2.2 用户需求与痛点
- **效率提升**：用户通常希望减少重复操作、降低学习成本，并把结果快速带入现有工作流。
- **质量稳定**：在正式业务场景中，输出稳定性、可解释性和错误处理能力比一次性亮点更重要。
- **成本透明**：用户和采购方需要理解价格、套餐限制、用量上限、协作席位和企业权益。
- **迁移成本**：如果竞品已经嵌入素材库、团队流程、历史数据或外部集成，替换难度会显著上升。
- **安全与信任**：涉及企业数据、个人内容或敏感素材时，隐私政策、数据保留和权限治理会成为关键门槛。

### 2.3 技术与产品趋势
- **工具化到平台化**：单点能力会逐步被工作台、协作空间、模板、插件、API 和生态连接包围。
- **AI 能力常态化**：AI 不再只是宣传点，差异会转向场景理解、输出质量、速度、成本和可控性。
- **证据驱动的产品决策**：竞品分析不能只依赖官网文案，需要结合价格页、更新日志、用户评价、案例和实际体验。

## 三、竞品选择与分层（Competitive Landscape）
### 3.1 本轮竞品范围
本轮纳入分析的竞品为：{competitor_names}。关注维度包括：{focus_text}。如果某些竞品公开信息不足，报告仍保留小节，但会明确标注待核实项，避免在没有证据时形成确定结论。

### 3.2 分层方法
- **第一层：直接替代型竞品**：与我方目标用户、核心任务和购买预算高度重叠，是销售、定位和路线图决策的重点。
- **第二层：场景扩展型竞品**：不完全替代我方，但在某些高频任务、入口或生态上抢占用户时间。
- **第三层：信息不足/新兴竞品**：需要继续补采官网、产品入口、价格、评价和案例，先作为监控对象。

### 3.3 对比口径
本报告不把“曝光度高”等同于“竞争力强”，而是从能力覆盖、商业化成熟度、渠道效率、用户信任和迁移壁垒五个角度判断。系统 1 的可视化模块会在正文后继续呈现这些维度的结构化结果。

## 四、核心能力拆解（Product Capability Analysis）
{competitor_sections}

## 五、商业模式分析（Monetization）
### 5.1 订阅制与免费试用
多数正式竞品系统会采用免费试用、基础免费版、个人订阅、团队订阅和企业定制的组合。判断订阅模型时不能只看价格数字，还要看席位、用量、导出权限、协作能力、历史记录、品牌水印和支持等级。

### 5.2 按量计费/API/SDK
如果竞品面向开发者或企业集成，API/SDK 的价格、速率限制、上下文/文件/任务上限和 SLA 会直接影响采购判断。没有官方 API 价格来源时，报告必须标注待核实。

### 5.3 企业版与服务收入
企业版通常围绕权限、审计、数据隔离、SSO、私有化/专属实例、法务条款和客户成功服务展开。对 {competitor_names} 的企业化成熟度，建议后续继续抓取安全页、企业页、案例库和帮助中心。

### 5.4 当前证据边界
本轮降级报告不会编造具体价格。若系统 1 已解析出 pricing facts，页面后续的可视化和价格模块可以继续保留；深度正文只陈述已被来源支持的商业化判断。

## 六、增长与分发策略（Growth Strategy）
### 6.1 官网与搜索入口
官网首页、产品页、价格页和行业解决方案页反映厂商希望被如何理解。对 {competitor_names}，建议分别搜索“官网、pricing、features、docs、case studies、reviews、alternatives”等关键词，补足公开入口。

### 6.2 应用商店、社区与内容渠道
如果竞品有移动端、浏览器插件、桌面端或社区模板，应用商店评分、评论、更新频率和内容分发渠道会成为增长判断的重要证据。只有官网材料时，不宜直接下用户留存或口碑结论。

### 6.3 生态与集成
集成能力会影响团队和企业用户的迁移成本。需要关注是否接入企业协作工具、办公套件、CRM、设计工具、开发者工具或行业系统。

### 6.4 销售与案例
案例库、客户 Logo、白皮书、合作伙伴和行业解决方案能说明竞品是否从工具型产品走向解决方案销售。后续 ReAct Agent 应优先补采这些页面。

## 七、用户与场景分析（User & Use Case）
### 7.1 个人用户
个人用户更关注上手速度、价格门槛、结果质量和移动/网页端体验。若产品依赖复杂配置，个人用户可能只在低频任务中使用。

### 7.2 专业用户
专业用户更关注稳定输出、批量处理、历史记录、素材/模板、导出质量和与既有工具的衔接。专业用户的留存往往来自流程效率，而不是单次体验。

### 7.3 团队用户
团队用户关注协作、权限、共享空间、版本管理、审批和品牌一致性。若竞品能进入团队标准流程，会形成更高迁移成本。

### 7.4 企业采购者
企业采购者关注安全、合规、成本可控、服务支持、合同条款和管理后台。官网企业页、安全页、案例库和帮助中心是这类判断的关键来源。

## 八、优劣势对比（SWOT / 对比矩阵）
### 8.1 优势
优势必须来自可验证事实，例如明确功能、用户案例、生态集成、价格优势、行业客户或高频评价。对 {competitor_names}，已有证据支持的优势会在系统 1 的 SWOT 和可视化模块继续展示。

### 8.2 劣势
劣势不能只写“功能少”或“体验差”，应定位到具体环节：学习成本、输出质量不稳定、价格不透明、缺少企业治理、移动端/网页端割裂、帮助文档不足或用户支持弱。

### 8.3 机会
机会通常来自竞品没有覆盖或覆盖不深的场景，例如更低门槛的入门体验、更清晰的套餐、更好的协作、更强的隐私承诺、更贴近行业的模板和更短的导入路径。

### 8.4 威胁
威胁包括强平台复制能力、搜索和应用商店入口被头部占据、价格战、合规要求提高、用户数据迁移困难和企业采购周期拉长。

## 九、关键差异与壁垒（Moat Analysis）
### 9.1 技术壁垒
技术壁垒来自性能、准确率、稳定性、成本结构和产品化速度。没有评测或官方技术材料时，只能把技术领先写成假设。

### 9.2 数据与用户反馈壁垒
如果竞品拥有大量用户反馈、行业数据、模板或历史任务数据，可能形成持续迭代优势。但这类壁垒通常不公开，需要通过案例、评价和产品细节间接判断。

### 9.3 生态壁垒
集成、插件、API、模板市场、合作伙伴和社区内容会提升迁移成本。生态越靠近用户日常工作流，越容易形成防守能力。

### 9.4 品牌与渠道壁垒
品牌不是抽象声量，而是体现在搜索可见度、内容渠道、客户信任、销售触达和用户推荐中。后续应通过搜索结果、案例库和应用商店信息继续验证。

## 十、机会点与策略建议（Opportunities）
### 10.1 产品定位建议
围绕 {industry}，定位不应只强调“功能更多”，而应明确服务哪类用户、解决哪条任务链、在哪些场景下比竞品更省时、更稳定或更可信。

### 10.2 功能规划建议
优先把竞品已验证的高频能力、用户抱怨最多的痛点和我方差异化能力放入路线图。对证据不足的功能，不建议直接投入开发，应先通过用户访谈或原型验证。

### 10.3 定价建议
定价策略应建立在竞品套餐、用户价值感知和目标客群支付意愿上。建议补采价格页、免费试用限制、团队权益和企业版入口，形成可执行的价格对比。

### 10.4 增长建议
增长策略应结合官网 SEO、应用商店、内容案例、模板/素材、社区传播和销售赋能。若竞品在某一渠道明显强势，应评估是否避开正面竞争或寻找细分切口。

### 10.5 风险规避建议
避免在报告和销售话术中使用无来源的绝对化结论。所有“领先、最低价、最好用、增长最快”等表述都应回链证据或改写为谨慎判断。

## 十一、数据附录（Appendix）
### 11.1 来源列表
{source_appendix}

### 11.2 测试方法
- 系统 1：前端创建任务，Orchestrator 调度采集 Agent、分析 Agent、质检 Agent 和报告 Agent。
- 系统 2 能力：配置模型 Key 后，分析 Agent 优先使用 DeepSeek direct thinking 输出 11 章深度报告；需要动态补证时使用内层 StateGraph ReAct 工具循环搜索、抓取网页和可选截图。
- 证据策略：官网、价格页、文档、案例、应用商店、社区评价和新闻稿优先；搜索摘要只能作为线索。
- 合规边界：所有关键事实必须回链来源；敏感信息和 API Key 不进入日志或报告正文。

### 11.3 信息不足说明
本报告当前处于本地降级模式，深度结构已经按系统 2 报告组织，但未执行实时模型生成或 ReAct 检索。填入 `.env` 中的 `DEEPSEEK_API_KEY` 后，系统会优先使用 DeepSeek direct thinking 生成更接近系统 2 输出密度的报告。
"""
    return ReactReportResult(
        enabled=False,
        provider="local-react-fallback",
        markdown=markdown,
        sections=_parse_markdown_sections(markdown),
        execution_mode="local_fallback",
        tool_calls=[{"name": "deep_report_generation", "deep_report_execution_mode": "local_fallback", "result": f"fallback: {reason}"}],
        fallback_reason=reason,
        token_input=estimate_tokens(str(task) + str(sources) + str(claims)),
        token_output=estimate_tokens(markdown),
    )


def _make_search_tool(tool_calls: list[dict[str, Any]], provider_name: str = ""):
    from langchain_core.tools import tool

    @tool
    def search_web(query: str) -> str:
        """搜索网络，获取竞争对手官网、价格、功能、用户评价、新闻和案例等公开信息。"""
        try:
            try:
                from ddgs import DDGS
            except ImportError:
                from duckduckgo_search import DDGS
            results = list(DDGS().text(query, max_results=6))
            tool_calls.append({"name": "search_web", "provider": "duckduckgo", "result": f"{len(results)} results", "query": query[:180]})
            if not results:
                return "未找到搜索结果，请尝试更具体的搜索词。"
            lines = []
            safety_hits: set[str] = set()
            excluded_count = 0
            for item in results:
                title = compact_text(str(item.get("title", "")), 160)
                body = compact_text(str(item.get("body", "")), 420)
                href = compact_text(str(item.get("href", "")), 240)
                if provider_name == "zhipu-react":
                    title, title_hits = _sanitize_zhipu_text(title)
                    body, body_hits = _sanitize_zhipu_text(body)
                    safety_hits.update(title_hits)
                    safety_hits.update(body_hits)
                    if (title_hits or body_hits) and _zhipu_drop_safety_hit_sources():
                        excluded_count += 1
                        continue
                lines.append(f"### {title}\n{body}\n来源: {href}")
            if safety_hits:
                tool_calls.append({
                    "name": "zhipu_tool_result_safety_filter",
                    "provider": provider_name,
                    "source": "search_web",
                    "result": f"{len(safety_hits)} categories/{excluded_count} results excluded",
                    "categories": sorted(safety_hits),
                })
            return "\n\n---\n\n".join(lines) if lines else "搜索结果已因智谱安全过滤全部排除，请换用官网、定价页、功能页或开发文档等低风险来源。"
        except Exception as exc:
            message = safe_error(exc)
            tool_calls.append({"name": "search_web", "provider": "duckduckgo", "result": "failed", "error": message})
            return f"搜索失败: {message}"

    return search_web


def _make_fetch_tool(tool_calls: list[dict[str, Any]], provider_name: str = ""):
    from langchain_core.tools import tool

    @tool
    def fetch_webpage(url: str) -> str:
        """抓取指定网页并提取纯文本内容，适合官网主页、定价页、功能页和文档页。"""
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        }
        try:
            response = requests.get(url, headers=headers, timeout=15)
            response.raise_for_status()
            html = response.text
            html = re.sub(r"<script[^>]*>.*?</script>", " ", html, flags=re.S | re.I)
            html = re.sub(r"<style[^>]*>.*?</style>", " ", html, flags=re.S | re.I)
            html = re.sub(r"<!--.*?-->", " ", html, flags=re.S)
            html = re.sub(r"<[^>]+>", " ", html)
            html = re.sub(r"&[a-z]+;", " ", html)
            text = re.sub(r"\s{2,}", "\n", html).strip()
            tool_calls.append({"name": "fetch_webpage", "provider": "requests", "result": "ok", "url": url[:240]})
            if provider_name == "zhipu-react":
                text, safety_hits = _sanitize_zhipu_text(text)
                if safety_hits:
                    tool_calls.append({
                        "name": "zhipu_tool_result_safety_filter",
                        "provider": provider_name,
                        "source": "fetch_webpage",
                        "result": f"{len(set(safety_hits))} categories/page excluded",
                        "categories": sorted(set(safety_hits)),
                        "url": url[:240],
                    })
                    if _zhipu_drop_safety_hit_sources():
                        return "该网页正文已因智谱安全过滤排除；请改用官网产品页、定价页、帮助中心中不含高风险舆情/协议限制/安全凭证细节的材料。"
            return textwrap.shorten(text, width=5000, placeholder="...（内容已截断）") or "页面内容为空或无法解析。"
        except Exception as exc:
            message = safe_error(exc)
            tool_calls.append({"name": "fetch_webpage", "provider": "requests", "result": "failed", "url": url[:240], "error": message})
            return f"无法获取页面: {message}"

    return fetch_webpage


def _is_allowed_http_url(url: str) -> bool:
    value = (url or "").strip()
    if not value.startswith(("http://", "https://")):
        return False
    try:
        parsed = urlparse(value)
    except Exception:
        return False
    host = (parsed.hostname or "").lower()
    if not host or host == "localhost" or host.endswith(".local"):
        return False
    return not re.match(r"^(127\.|10\.|172\.(1[6-9]|2\d|3[01])\.|192\.168\.)", host)


def _make_screenshot_tool(tool_calls: list[dict[str, Any]], screenshots: list[str], screenshot_dir: Path, md_relative_dir: str):
    from langchain_core.tools import tool

    screenshot_dir.mkdir(parents=True, exist_ok=True)

    @tool
    def screenshot_webpage(url: str, image_label: str = "", full_page: bool = False) -> str:
        """对公开网页截图并保存 PNG，返回最终报告可插入的 Markdown 图片引用。"""
        if not _is_allowed_http_url(url):
            return f"截图被拒绝：仅允许公网 http/https URL。收到: {url!r}"
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            return "截图不可用：未安装 Playwright。请执行 pip install playwright 后运行 playwright install chromium。"

        parsed = urlparse(url)
        host = re.sub(r"[^\w.\-]+", "_", (parsed.netloc or "page"))[:80]
        digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:10]
        path = screenshot_dir / f"{host}_{digest}.png"
        label = (image_label or parsed.netloc or "页面截图").replace("[", "").replace("]", "")
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                try:
                    context = browser.new_context(
                        viewport={"width": 1280, "height": 720},
                        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36",
                        locale="zh-CN",
                    )
                    page = context.new_page()
                    page.goto(url, wait_until="domcontentloaded", timeout=45_000)
                    page.wait_for_timeout(800)
                    page.screenshot(path=str(path), full_page=full_page)
                finally:
                    browser.close()
        except Exception as exc:
            message = safe_error(exc)
            tool_calls.append({"name": "screenshot_webpage", "provider": "playwright", "result": "failed", "url": url[:240], "error": message})
            return f"截图失败: {message}"
        rel = f"{md_relative_dir}/{path.name}"
        md = f"![{label}]({rel})"
        screenshots.append(rel)
        tool_calls.append({"name": "screenshot_webpage", "provider": "playwright", "result": "ok", "url": url[:240], "path": rel})
        return f"截图已保存，请在报告对应小节插入：\n\n{md}\n\n来源页: {url}"

    return screenshot_webpage


def run_react_report(
    task: dict[str, Any],
    sources: list[dict[str, Any]],
    claims: list[dict[str, Any]],
    output_dir: Path,
) -> ReactReportResult:
    if not _is_enabled_by_env():
        return _fallback_report(task, sources, claims, "REACT_REPORT_ENABLED 已关闭")

    providers = _configured_providers()
    if not providers:
        return _fallback_report(
            task,
            sources,
            claims,
            "未配置 DEEPSEEK_API_KEY，或未配置 ZHIPU_API_KEY + ZHIPU_MODEL，或未配置 DOUBAO_API_KEY + DOUBAO_ENDPOINT_ID/DOUBAO_MODEL_NAME",
        )

    if _LANGGRAPH_IMPORT_ERROR is not None:
        return _fallback_report(task, sources, claims, f"LangGraph 依赖不可用：{safe_error(_LANGGRAPH_IMPORT_ERROR)}")

    competitors = [str(item) for item in task.get("competitors", []) if str(item).strip()]
    failures: list[str] = []
    failover_diagnostics: list[dict[str, Any]] = []

    for provider in providers:
        max_seconds = int(provider.get("timeout_seconds") or _provider_timeout_seconds(provider["provider"]))
        started = time.monotonic()
        tool_calls: list[dict[str, Any]] = []
        screenshots: list[str] = []
        provider_sources = sources
        provider_claims = claims
        if provider["provider"] == "zhipu-react":
            provider_sources, safety_meta = _sanitize_zhipu_sources(sources)
            if safety_meta.get("enabled"):
                tool_calls.append({
                    "name": "zhipu_input_safety_filter",
                    "provider": provider["provider"],
                    "result": f"{safety_meta.get('filtered_source_count', 0)} sources/{safety_meta.get('dropped_source_count', 0)} dropped",
                    "categories": safety_meta.get("categories", []),
                    "source_ids": safety_meta.get("source_ids", []),
                    "dropped_source_ids": safety_meta.get("dropped_source_ids", []),
                })
            provider_claims, claim_safety_meta = _sanitize_zhipu_claims(claims)
            if claim_safety_meta.get("enabled"):
                tool_calls.append({
                    "name": "zhipu_claim_safety_filter",
                    "provider": provider["provider"],
                    "result": f"{claim_safety_meta.get('filtered_claim_count', 0)} claims filtered",
                    "categories": claim_safety_meta.get("categories", []),
                })
        user_task = build_user_task(task, provider_sources, provider_claims)
        if provider["provider"] == "deepseek-react" and _deepseek_direct_thinking_mode_enabled():
            try:
                final_report, tool_calls = _run_deepseek_direct_thinking_report(provider, user_task, max_seconds, tool_calls)
                final_report = _ensure_min_source_urls(_coerce_zhipu_report_structure(final_report, task), user_task, competitors)
            except Exception as exc:
                error = safe_error(exc)
                failures.append(f"{provider['provider']}: {error}")
                _LAST_PROVIDER_STATUS["last_failover_reason"] = failures[-1]
                failover_diagnostics.extend(_failover_diagnostic_calls(provider["provider"], tool_calls, error))
                continue
            completion_reason = _report_completion_reason(final_report, competitors) if final_report else "DeepSeek thinking mode 未返回完整报告"
            if completion_reason:
                failures.append(f"{provider['provider']}: {completion_reason}")
                _LAST_PROVIDER_STATUS["last_failover_reason"] = failures[-1]
                failover_diagnostics.extend(_failover_diagnostic_calls(provider["provider"], tool_calls, completion_reason))
                continue
            failover_calls = [
                {"name": "react_provider_failover", "provider": item.split(":", 1)[0], "result": item}
                for item in failures
            ]
            _LAST_PROVIDER_STATUS["last_success_provider"] = provider["provider"]
            _LAST_PROVIDER_STATUS["last_failover_reason"] = "；".join(failures)
            return ReactReportResult(
                enabled=True,
                provider=provider["provider"],
                markdown=final_report,
                sections=_parse_markdown_sections(final_report),
                execution_mode="deepseek_direct_thinking",
                tool_calls=failover_calls
                + failover_diagnostics
                + tool_calls
                + [
                    {
                        "name": "deep_report_generation",
                        "provider": provider["provider"],
                        "deep_report_execution_mode": "deepseek_direct_thinking",
                        "timeout_seconds": max_seconds,
                        "result": f"{len(final_report)} chars",
                    }
                ],
                screenshots=[],
                token_input=estimate_tokens(user_task),
                token_output=estimate_tokens(final_report),
            )
        if provider["provider"] == "zhipu-react" and _zhipu_direct_safe_mode_enabled():
            try:
                final_report, tool_calls = _run_zhipu_direct_safe_report(provider, user_task, competitors, max_seconds, tool_calls)
                final_report = _ensure_min_source_urls(_coerce_zhipu_report_structure(final_report, task), user_task, competitors)
            except Exception as exc:
                error = safe_error(exc)
                failures.append(f"{provider['provider']}: {error}")
                _LAST_PROVIDER_STATUS["last_failover_reason"] = failures[-1]
                failover_diagnostics.extend(_failover_diagnostic_calls(provider["provider"], tool_calls, error))
                continue
            completion_reason = _report_completion_reason(final_report, competitors) if final_report else "Zhipu direct safe mode 未返回完整报告"
            if completion_reason:
                failures.append(f"{provider['provider']}: {completion_reason}")
                _LAST_PROVIDER_STATUS["last_failover_reason"] = failures[-1]
                failover_diagnostics.extend(_failover_diagnostic_calls(provider["provider"], tool_calls, completion_reason))
                continue
            failover_calls = [
                {"name": "react_provider_failover", "provider": item.split(":", 1)[0], "result": item}
                for item in failures
            ]
            _LAST_PROVIDER_STATUS["last_success_provider"] = provider["provider"]
            _LAST_PROVIDER_STATUS["last_failover_reason"] = "；".join(failures)
            return ReactReportResult(
                enabled=True,
                provider=provider["provider"],
                markdown=final_report,
                sections=_parse_markdown_sections(final_report),
                execution_mode="zhipu_direct_safe",
                tool_calls=failover_calls
                + failover_diagnostics
                + tool_calls
                + [
                    {
                        "name": "deep_report_generation",
                        "provider": provider["provider"],
                        "deep_report_execution_mode": "zhipu_direct_safe",
                        "timeout_seconds": max_seconds,
                        "result": f"{len(final_report)} chars",
                    }
                ],
                screenshots=[],
                token_input=estimate_tokens(user_task),
                token_output=estimate_tokens(final_report),
            )
        screenshot_prefix = f"{task.get('id', 'task')[:10]}_{provider['provider']}_screenshots"
        screenshot_dir = output_dir / screenshot_prefix
        tools = [
            _make_search_tool(tool_calls, provider["provider"]),
            _make_fetch_tool(tool_calls, provider["provider"]),
            _make_screenshot_tool(tool_calls, screenshots, screenshot_dir, screenshot_prefix),
        ]

        llm = ChatOpenAI(
            model=provider["model"],
            api_key=provider["api_key"],
            base_url=provider["base_url"],
            temperature=float(os.environ.get("REACT_AGENT_TEMPERATURE", "0.3")),
            max_tokens=int(os.environ.get("REACT_AGENT_MAX_TOKENS", "8192")),
            timeout=max(30, max_seconds),
            extra_body=provider.get("extra_body"),
        ).bind_tools(tools)
        tool_node = ToolNode(tools)

        def agent_node(state: AgentState) -> dict[str, Any]:
            if time.monotonic() - started > max_seconds:
                raise TimeoutError(f"ReAct provider exceeded {max_seconds}s")
            messages = list(state["messages"])
            if not any(isinstance(item, SystemMessage) for item in messages):
                messages = [SystemMessage(content=SYSTEM_PROMPT)] + messages
            response = llm.invoke(messages)
            return {"messages": [response]}

        def route(state: AgentState) -> Literal["tools", "agent", "__end__"]:
            if time.monotonic() - started > max_seconds:
                raise TimeoutError(f"ReAct provider exceeded {max_seconds}s")
            last = state["messages"][-1]
            if hasattr(last, "tool_calls") and last.tool_calls:
                return "tools"
            content = getattr(last, "content", "") or ""
            if _report_is_complete(content, competitors):
                return END
            return "agent"

        graph = StateGraph(AgentState)
        graph.add_node("agent", agent_node)
        graph.add_node("tools", tool_node)
        graph.set_entry_point("agent")
        graph.add_conditional_edges("agent", route, {"tools": "tools", "agent": "agent", "__end__": END})
        graph.add_edge("tools", "agent")
        compiled = graph.compile()

        final_report = ""
        try:
            for chunk in compiled.stream(
                {"messages": [("user", user_task)]},
                stream_mode="updates",
                config={"recursion_limit": int(os.environ.get("REACT_AGENT_RECURSION_LIMIT", "36"))},
            ):
                for node_output in chunk.values():
                    for msg in node_output.get("messages", []):
                        if isinstance(msg, AIMessage) and msg.content:
                            if "# 竞争情报报告" in msg.content or "# 竞品调研报告" in msg.content:
                                final_report = str(msg.content)
        except Exception as exc:
            error = safe_error(exc)
            failures.append(f"{provider['provider']}: {error}")
            _LAST_PROVIDER_STATUS["last_failover_reason"] = failures[-1]
            failover_diagnostics.extend(_failover_diagnostic_calls(provider["provider"], tool_calls, error))
            if not final_report:
                continue
            tool_calls.append({"name": "react_graph", "provider": provider["provider"], "result": "partial", "error": error})

        if provider["provider"] == "zhipu-react" and final_report:
            final_report = _ensure_min_source_urls(_coerce_zhipu_report_structure(final_report, task), user_task, competitors)
        completion_reason = _report_completion_reason(final_report, competitors) if final_report else "ReAct 未返回完整报告"
        if completion_reason:
            failures.append(f"{provider['provider']}: {completion_reason}")
            _LAST_PROVIDER_STATUS["last_failover_reason"] = failures[-1]
            failover_diagnostics.extend(_failover_diagnostic_calls(provider["provider"], tool_calls, completion_reason))
            continue

        failover_calls = [
            {"name": "react_provider_failover", "provider": item.split(":", 1)[0], "result": item}
            for item in failures
        ]
        _LAST_PROVIDER_STATUS["last_success_provider"] = provider["provider"]
        _LAST_PROVIDER_STATUS["last_failover_reason"] = "；".join(failures)
        return ReactReportResult(
            enabled=True,
            provider=provider["provider"],
            markdown=final_report,
            sections=_parse_markdown_sections(final_report),
            execution_mode="stategraph_react_tools",
            tool_calls=failover_calls
            + failover_diagnostics
            + tool_calls
            + [
                {
                    "name": "deep_report_generation",
                    "provider": provider["provider"],
                    "deep_report_execution_mode": "stategraph_react_tools",
                    "timeout_seconds": max_seconds,
                    "result": f"{len(final_report)} chars",
                }
            ],
            screenshots=screenshots,
            token_input=estimate_tokens(user_task),
            token_output=estimate_tokens(final_report),
        )

    return _fallback_report(task, sources, claims, f"ReAct 运行失败：{'；'.join(failures)}")
