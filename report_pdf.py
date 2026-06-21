from __future__ import annotations

import html
import io
import math
import re
from pathlib import Path
from typing import Any, Sequence

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    Flowable,
    KeepTogether,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)


PAGE_W, PAGE_H = A4
MARGIN_X = 18 * mm
MARGIN_TOP = 21 * mm
MARGIN_BOTTOM = 17 * mm
CONTENT_W = PAGE_W - 2 * MARGIN_X
SECTION_NUMERALS = ["一", "二", "三", "四", "五", "六", "七", "八", "九", "十", "十一", "十二", "十三", "十四", "十五", "十六"]

PALETTE = {
    "ink": colors.HexColor("#1F2933"),
    "muted": colors.HexColor("#5D6B7A"),
    "line": colors.HexColor("#D9E2EC"),
    "soft": colors.HexColor("#F5F7FA"),
    "soft2": colors.HexColor("#EDF2F7"),
    "teal": colors.HexColor("#007C7C"),
    "teal_dark": colors.HexColor("#005F60"),
    "blue": colors.HexColor("#2563A7"),
    "amber": colors.HexColor("#D97706"),
    "green": colors.HexColor("#16803C"),
    "red": colors.HexColor("#B42318"),
    "brown": colors.HexColor("#8A5A0A"),
    "slate": colors.HexColor("#334155"),
}


def _register_fonts() -> tuple[str, str]:
    fonts_dir = Path("C:/Windows/Fonts")
    try:
        regular = fonts_dir / "msyh.ttc"
        bold = fonts_dir / "msyhbd.ttc"
        if regular.exists() and bold.exists():
            pdfmetrics.registerFont(TTFont("MSYH", str(regular)))
            pdfmetrics.registerFont(TTFont("MSYH-Bold", str(bold)))
            return "MSYH", "MSYH-Bold"
    except Exception:
        pass
    pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
    return "STSong-Light", "STSong-Light"


FONT_REGULAR, FONT_BOLD = _register_fonts()


def _clean(value: Any, limit: int = 1600) -> str:
    text = str(value or "").replace("竞争情报分析师", "MOSS团队").replace("\x00", " ")
    text = " ".join(text.split())
    if len(text) > limit:
        text = text[: max(0, limit - 1)] + "…"
    return html.escape(text)


def _raw(value: Any, limit: int = 1600) -> str:
    text = str(value or "").replace("竞争情报分析师", "MOSS团队").replace("\x00", " ")
    text = " ".join(text.split())
    if len(text) > limit:
        text = text[: max(0, limit - 1)] + "…"
    return text


def _section_ordinal(index: int) -> str:
    return SECTION_NUMERALS[index - 1] if 1 <= index <= len(SECTION_NUMERALS) else str(index)


def _section_index_from_number(value: Any) -> int:
    try:
        number = int(str(value or "").strip())
    except ValueError:
        return 0
    return number if number > 0 else 0


def _section_index_from_title(title: Any) -> int:
    match = re.match(r"^\s*(?:第)?([一二三四五六七八九十]{1,3}|[0-9]{1,2})[、.．，,\s]+", str(title or ""))
    if not match:
        return 0
    value = match.group(1)
    if value.isdigit():
        return _section_index_from_number(value)
    return SECTION_NUMERALS.index(value) + 1 if value in SECTION_NUMERALS else 0


def _section_index_from_markdown(markdown: Any) -> int:
    match = re.search(r"(?m)^#{3,5}\s+(\d{1,2})\.\d{1,2}\b", str(markdown or ""))
    return _section_index_from_number(match.group(1)) if match else 0


def _report_item_section_index(item: dict[str, Any], rendered_index: int) -> int:
    return rendered_index


def _strip_section_ordinal(title: Any) -> str:
    return re.sub(r"^\s*(?:第?[一二三四五六七八九十]{1,3}|[0-9]{1,2})[、.．，,\s]+", "", str(title or "")).strip()


def _section_title(title: Any, index: int, include_ordinal: bool = True) -> str:
    cleaned = _strip_section_ordinal(title) or f"章节 {index}"
    return f"{_section_ordinal(index)}、{cleaned}" if include_ordinal else cleaned


def _is_conclusion_section(section: dict[str, Any]) -> bool:
    key = str(section.get("key") or section.get("id") or "").lower()
    title = _strip_section_ordinal(section.get("title", ""))
    return "conclusion" in key or bool(re.match(r"^结语(?:\s|$)", title))


def _rendered_report_items(sections: list[dict[str, Any]]) -> list[dict[str, Any]]:
    filtered = [section for section in sections if not _is_conclusion_section(section)]
    return [{"type": "visual", "title": "可视化总览"}] + [{"type": "section", "section": section} for section in filtered]


def _refs(refs: Sequence[str] | None) -> str:
    return " ".join(refs or []) or "未列入正文依据"


def _build_pdf_source_ref_urls(content: dict[str, Any]) -> dict[str, str]:
    result: dict[str, str] = {}
    for source in content.get("source_catalog") or []:
        ref = str(source.get("ref") or "")
        source_id = str(source.get("id") or "")
        url = str(source.get("url_or_path") or "")
        if ref and re.match(r"^https?://", url, flags=re.I):
            result[ref] = url
            match = re.match(r"^S(\d+)$", ref, flags=re.I)
            if match:
                result[match.group(1)] = url
                result[f"[{match.group(1)}]"] = url
        if source_id and re.match(r"^https?://", url, flags=re.I):
            result[source_id] = url
    return result


def _build_pdf_source_ref_labels(content: dict[str, Any]) -> dict[str, str]:
    result: dict[str, str] = {}
    for fallback, source in enumerate(content.get("source_catalog") or [], start=1):
        if not isinstance(source, dict):
            continue
        ref = str(source.get("ref") or "")
        source_id = str(source.get("id") or "")
        label = _source_catalog_label(source, fallback)
        for key in [ref, label, f"S{label}", f"[{label}]", source_id]:
            cleaned = str(key or "").strip().strip("[]")
            if cleaned:
                result[cleaned] = label
                result[f"[{cleaned}]"] = label
    return result


def _source_ref_label(ref: Any) -> str:
    text = str(ref or "").strip().strip("[]")
    match = re.match(r"^S(\d+)$", text, flags=re.I)
    return match.group(1) if match else text


def _is_pdf_internal_source_ref(ref: Any) -> bool:
    return bool(re.match(r"^[A-Za-z0-9]{4,}_(?:search|volc|ga|appark|rss|google|manual|input|url|src|source)[A-Za-z0-9_]*$", str(ref or "").strip().strip("[]"), flags=re.I))


def _pdf_source_ref_markup(
    ref: Any,
    source_ref_urls: dict[str, str] | None,
    source_ref_labels: dict[str, str] | None = None,
    superscript: bool = False,
) -> str:
    raw_ref = str(ref or "").strip()
    raw_key = raw_ref.strip("[]")
    labels = source_ref_labels or {}
    label = labels.get(raw_ref) or labels.get(raw_key) or _source_ref_label(ref)
    if not label:
        return ""
    refs = source_ref_urls or {}
    url = refs.get(raw_ref) or refs.get(raw_key) or refs.get(label) or refs.get(f"S{label}") or refs.get(f"[{label}]")
    if _is_pdf_internal_source_ref(raw_key) and raw_key not in labels and not url:
        return ""
    safe_label = html.escape(label)
    text = f"[{safe_label}]"
    if superscript:
        text = f"<super>{text}</super>"
    if url:
        return f'<a href="{html.escape(url, quote=True)}" color="#2563A7">{text}</a>'
    return text


def _source_refs_flow(
    refs: Sequence[str] | None,
    source_ref_urls: dict[str, str],
    source_ref_labels: dict[str, str] | None = None,
) -> Paragraph:
    parts: list[str] = []
    for ref in refs or []:
        markup = _pdf_source_ref_markup(ref, source_ref_urls, source_ref_labels)
        if markup:
            parts.append(markup)
    return P_html(" ".join(parts) or "未列入正文依据", "TableCell")


def _source_catalog_label(source: dict[str, Any], fallback: int) -> str:
    ref = str(source.get("ref") or "")
    match = re.match(r"^S(\d+)$", ref, flags=re.I)
    return match.group(1) if match else ref or str(fallback)


def _source_catalog_flows(content: dict[str, Any], index: int) -> list[Flowable]:
    rows = [row for row in content.get("source_catalog") or [] if isinstance(row, dict)]
    if not rows:
        return []
    flows: list[Flowable] = [
        Spacer(1, 7 * mm),
        P(_section_title("参考文献（来源链接）", index, True), "H1"),
    ]
    for fallback, row in enumerate(rows[:120], start=1):
        url = str(row.get("url_or_path") or "")
        title = _clean(row.get("title") or url or row.get("ref") or "未命名来源", 500)
        label = html.escape(_source_catalog_label(row, fallback))
        meta = " · ".join(
            str(value)
            for value in [
                row.get("site"),
                row.get("competitor") or "综合",
                _source_type_label(row.get("type")),
                row.get("published_at") or row.get("collected_at"),
            ]
            if value
        )
        safe_meta = _clean(meta, 500)
        if re.match(r"^https?://", url, flags=re.I):
            safe_url = html.escape(url, quote=True)
            flows.append(
                P_html(
                    f'<a href="{safe_url}" color="#2563A7">[{label}]</a>&nbsp;'
                    f'<a href="{safe_url}" color="#1F2933">{title}</a><br/>'
                    f'<font color="#5D6B7A">{safe_meta}</font>',
                    "Body",
                )
            )
        else:
            flows.append(P_html(f"<b>[{label}]</b>&nbsp;{title}<br/><font color=\"#5D6B7A\">{safe_meta}</font>", "Body"))
    return flows


def _raw_status_label(value: Any) -> str:
    return {
        "fetched": "已抓取正文",
        "summary_only": "检索线索",
        "cached": "缓存样例",
        "not_collected": "未采到正文",
    }.get(str(value or ""), str(value or "未标记"))


def _source_type_label(value: Any) -> str:
    return {
        "official_site": "官网",
        "pricing_page": "定价页",
        "public_doc": "公开文档",
        "review_page": "评价平台",
        "news": "新闻/风险",
        "search_result": "搜索结果",
        "volc_search_result": "搜索结果",
        "manual_scope": "任务范围说明",
        "manual_input": "人工补充",
        "manual_url": "人工补充网址",
        "demo_scope_note": "缓存范围说明",
    }.get(str(value or ""), str(value or "未分类"))


def _styles() -> dict[str, ParagraphStyle]:
    base = getSampleStyleSheet()
    return {
        "Title": ParagraphStyle(
            "Title",
            parent=base["Title"],
            fontName=FONT_BOLD,
            fontSize=24,
            leading=33,
            alignment=TA_LEFT,
            textColor=PALETTE["ink"],
            wordWrap="CJK",
            spaceAfter=7,
        ),
        "Subtitle": ParagraphStyle(
            "Subtitle",
            parent=base["Normal"],
            fontName=FONT_REGULAR,
            fontSize=10.5,
            leading=15,
            textColor=PALETTE["muted"],
            wordWrap="CJK",
        ),
        "H1": ParagraphStyle(
            "H1",
            parent=base["Heading1"],
            fontName=FONT_BOLD,
            fontSize=18,
            leading=25,
            textColor=PALETTE["teal_dark"],
            spaceBefore=4,
            spaceAfter=6,
            keepWithNext=True,
            wordWrap="CJK",
        ),
        "H2": ParagraphStyle(
            "H2",
            parent=base["Heading2"],
            fontName=FONT_BOLD,
            fontSize=14.2,
            leading=20,
            textColor=PALETTE["slate"],
            spaceBefore=6,
            spaceAfter=4,
            keepWithNext=True,
            wordWrap="CJK",
        ),
        "Body": ParagraphStyle(
            "Body",
            parent=base["Normal"],
            fontName=FONT_REGULAR,
            fontSize=9.6,
            leading=15.2,
            textColor=PALETTE["ink"],
            wordWrap="CJK",
            spaceAfter=4,
        ),
        "Small": ParagraphStyle(
            "Small",
            parent=base["Normal"],
            fontName=FONT_REGULAR,
            fontSize=9.0,
            leading=13.5,
            textColor=PALETTE["muted"],
            wordWrap="CJK",
            spaceAfter=2.5,
        ),
        "Caption": ParagraphStyle(
            "Caption",
            parent=base["Normal"],
            fontName=FONT_REGULAR,
            fontSize=8,
            leading=11.8,
            textColor=PALETTE["muted"],
            alignment=TA_CENTER,
            wordWrap="CJK",
            spaceBefore=3,
            spaceAfter=5,
        ),
        "TableHead": ParagraphStyle(
            "TableHead",
            parent=base["Normal"],
            fontName=FONT_BOLD,
            fontSize=9.2,
            leading=13.8,
            textColor=colors.white,
            alignment=TA_CENTER,
            wordWrap="CJK",
        ),
        "TableCell": ParagraphStyle(
            "TableCell",
            parent=base["Normal"],
            fontName=FONT_REGULAR,
            fontSize=8.8,
            leading=13.5,
            textColor=PALETTE["ink"],
            wordWrap="CJK",
        ),
        "CardTitle": ParagraphStyle(
            "CardTitle",
            parent=base["Normal"],
            fontName=FONT_BOLD,
            fontSize=10,
            leading=14,
            textColor=PALETTE["teal_dark"],
            wordWrap="CJK",
        ),
        "CardMetric": ParagraphStyle(
            "CardMetric",
            parent=base["Normal"],
            fontName=FONT_BOLD,
            fontSize=15,
            leading=19,
            textColor=PALETTE["ink"],
            alignment=TA_CENTER,
            wordWrap="CJK",
        ),
        "CardNote": ParagraphStyle(
            "CardNote",
            parent=base["Normal"],
            fontName=FONT_REGULAR,
            fontSize=7.4,
            leading=10.8,
            textColor=PALETTE["muted"],
            alignment=TA_LEFT,
            wordWrap="CJK",
        ),
    }


STYLES = _styles()


def P(text: Any, style: str = "Body") -> Paragraph:
    return Paragraph(_clean(text), STYLES[style])


def P_html(text: str, style: str = "Body") -> Paragraph:
    return Paragraph(text, STYLES[style])


def _normalize_reference_url(value: Any) -> str:
    raw = str(value or "").strip().rstrip(".,;，。；、")
    if not raw:
        return ""
    try:
        from urllib.parse import urlparse, urlunparse

        parsed = urlparse(raw)
        path = parsed.path.rstrip("/")
        normalized = urlunparse((parsed.scheme.lower(), parsed.netloc.lower(), path, "", parsed.query, ""))
        return normalized.lower()
    except Exception:
        return raw.rstrip("/").lower()


def _build_pdf_url_refs(content: dict[str, Any]) -> dict[str, str]:
    refs: dict[str, str] = {}
    for source in content.get("source_catalog") or []:
        url = source.get("url_or_path") or ""
        ref = str(source.get("ref") or "")
        match = re.match(r"^S(\d+)$", ref, flags=re.I)
        label = match.group(1) if match else ref
        normalized = _normalize_reference_url(url)
        if normalized and label:
            refs[normalized] = label
    return refs


def _split_url_token(token: str) -> tuple[str, str]:
    match = re.match(r"^(.*?)([.,;:!?，。；、]+)?$", token or "")
    if not match:
        return token, ""
    return match.group(1), match.group(2) or ""


def _pdf_citation_label(url: str, url_refs: dict[str, str] | None) -> str:
    refs = url_refs if url_refs is not None else {}
    normalized = _normalize_reference_url(url)
    if not normalized:
        return "ref"
    if normalized in refs:
        return refs[normalized]
    numeric_refs = [int(value) for value in refs.values() if str(value).isdigit()]
    label = str((max(numeric_refs) if numeric_refs else 0) + 1)
    refs[normalized] = label
    return label


PDF_CITATION_MATCH_GROUPS = [
    {"aliases": ["chatgpt", "openai", "gpt"], "source_terms": ["chatgpt", "openai", "gpt"]},
    {"aliases": ["deepseek", "深度求索"], "source_terms": ["deepseek", "深度求索"]},
    {"aliases": ["豆包", "doubao", "字节", "bytedance", "volcengine", "火山"], "source_terms": ["豆包", "doubao", "字节", "bytedance", "volcengine", "火山"]},
]

PDF_CITATION_TOPIC_GROUPS = [
    {"text_terms": ["价格", "定价", "订阅", "付费", "免费", "成本", "api", "token", "套餐"], "source_terms": ["pricing", "price", "api", "billing", "套餐", "价格", "定价"]},
    {"text_terms": ["企业", "团队", "合规", "安全", "隐私", "数据", "管理员"], "source_terms": ["enterprise", "business", "security", "compliance", "privacy", "安全", "合规"]},
    {"text_terms": ["app", "下载", "收入", "榜单", "应用商店", "移动端"], "source_terms": ["app", "app store", "google play", "appark", "下载", "收入"]},
    {"text_terms": ["模型", "推理", "agent", "多模态", "代码", "codex", "能力", "文档"], "source_terms": ["docs", "documentation", "model", "agent", "codex", "文档"]},
    {"text_terms": ["用户", "评价", "口碑", "社区", "评论", "g2", "reddit"], "source_terms": ["review", "g2", "reddit", "评价", "评论"]},
]


def _includes_any_text(haystack: Any, terms: Sequence[str]) -> bool:
    text = str(haystack or "").casefold()
    return any(str(term).casefold() in text for term in terms)


def _source_search_text(source: dict[str, Any]) -> str:
    return " ".join(
        str(source.get(key) or "")
        for key in ("ref", "title", "url_or_path", "type", "site", "competitor", "module", "role")
    ).casefold()


def _pdf_citation_score(source: dict[str, Any], text: str) -> float:
    source_text = _source_search_text(source)
    score = 0.0
    for group in PDF_CITATION_MATCH_GROUPS:
        if _includes_any_text(text, group["aliases"]) and _includes_any_text(source_text, group["source_terms"]):
            score += 30
    for group in PDF_CITATION_TOPIC_GROUPS:
        if _includes_any_text(text, group["text_terms"]) and _includes_any_text(source_text, group["source_terms"]):
            score += 10
    if re.search(r"official|pricing|docs|product|enterprise|security|help|openai|deepseek|volcengine|doubao", source_text, flags=re.I):
        score += 3
    if str(source.get("credibility") or "").casefold() == "high":
        score += 2
    try:
        score += min(5, float(source.get("relevance_score") or 0) / 4)
    except (TypeError, ValueError):
        pass
    return score


def _best_pdf_citation_source(source_catalog: Sequence[dict[str, Any]], text: str, source_terms: Sequence[str] | None = None) -> dict[str, Any] | None:
    candidates = []
    for source in source_catalog:
        url = str(source.get("url_or_path") or "")
        if not re.match(r"^https?://", url, flags=re.I):
            continue
        if source_terms and not _includes_any_text(_source_search_text(source), source_terms):
            continue
        candidates.append((_pdf_citation_score(source, text), str(source.get("ref") or ""), source))
    candidates.sort(key=lambda item: (-item[0], item[1]))
    return candidates[0][2] if candidates else None


def _pdf_citation_sources_for_text(
    source_catalog: Sequence[dict[str, Any]] | None,
    text: Any,
    used_urls: set[str] | None = None,
    limit: int = 3,
) -> list[dict[str, Any]]:
    catalog = [source for source in (source_catalog or []) if isinstance(source, dict)]
    cleaned = re.sub(r"https?://\S+", " ", str(text or "")).strip()
    if len(cleaned) < 10 or not catalog:
        return []
    used = set(used_urls or set())
    selected: list[dict[str, Any]] = []
    seen: set[str] = set()
    for group in PDF_CITATION_MATCH_GROUPS:
        if not _includes_any_text(cleaned, group["aliases"]):
            continue
        source = _best_pdf_citation_source(catalog, cleaned, group["source_terms"])
        key = str(source.get("ref") or source.get("url_or_path") or "") if source else ""
        normalized = _normalize_reference_url(source.get("url_or_path")) if source else ""
        if source and key and key not in seen and normalized not in used:
            selected.append(source)
            seen.add(key)
    if not selected:
        source = _best_pdf_citation_source(catalog, cleaned)
        key = str(source.get("ref") or source.get("url_or_path") or "") if source else ""
        normalized = _normalize_reference_url(source.get("url_or_path")) if source else ""
        if source and key and normalized not in used:
            selected.append(source)
            seen.add(key)
    if len(selected) < limit:
        scored = sorted(
            ((_pdf_citation_score(source, cleaned), str(source.get("ref") or ""), source) for source in catalog),
            key=lambda item: (-item[0], item[1]),
        )
        for score, _ref, source in scored:
            key = str(source.get("ref") or source.get("url_or_path") or "")
            normalized = _normalize_reference_url(source.get("url_or_path"))
            if len(selected) >= limit:
                break
            if score > 0 and key and key not in seen and normalized not in used:
                selected.append(source)
                seen.add(key)
    return selected[:limit]


def _trailing_pdf_citations(
    text: Any,
    source_catalog: Sequence[dict[str, Any]] | None,
    url_refs: dict[str, str] | None,
    used_urls: set[str] | None = None,
) -> str:
    parts: list[str] = []
    used = set(used_urls or set())
    for source in _pdf_citation_sources_for_text(source_catalog, text, used_urls=used):
        url = str(source.get("url_or_path") or "")
        normalized = _normalize_reference_url(url)
        if not normalized or normalized in used:
            continue
        label = _pdf_citation_label(url, url_refs)
        parts.append(f'<a href="{html.escape(url, quote=True)}" color="#2563A7"><super>[{html.escape(label)}]</super></a>')
        used.add(normalized)
    return ("&nbsp;" + "&nbsp;".join(parts)) if parts else ""


def _clean_markdown_link_label(value: str) -> str:
    text = re.sub(r"\*\*([^*]+)\*\*", r"\1", str(value or ""))
    text = re.sub(r"(?:资料来源|参考来源|来源|出处)[：:]?\s*$", "", text)
    return text.strip()


def _is_evidence_style_link_label(value: str) -> bool:
    raw = str(value or "")
    cleaned = _clean_markdown_link_label(raw)
    return "**" in raw or bool(re.search(r"(?:资料来源|参考来源|来源|出处)", raw)) or len(cleaned) > 36


def _is_pdf_source_ref_token(value: Any) -> bool:
    token = str(value or "").strip().strip("[]")
    return bool(re.match(r"^(?:S?\d+|[A-Za-z0-9]{4,}_(?:search|volc|ga|appark|rss|google|manual|input|url|src|source)[A-Za-z0-9_]*)$", token, flags=re.I))


def _clean_pdf_source_ref_token(value: Any) -> str:
    token = str(value or "").strip().strip("[]")
    token = re.sub(r"^(?:资料来源|参考来源|来源|出处)\s*[:：]?\s*", "", token)
    token = token.strip().strip("[]").strip()
    token = re.sub(r"[，,。；;、.]+$", "", token).strip()
    return token.strip("[]")


def _has_pdf_source_cue(value: Any) -> bool:
    return bool(re.search(r"(?:资料来源|参考来源|来源|出处)\s*[:：]?", str(value or "")))


def _split_pdf_source_ref_tokens(value: Any) -> list[str]:
    cleaned_value = re.sub(r"^(?:资料来源|参考来源|来源|出处)\s*[:：]?\s*", "", str(value or "").strip())
    tokens = [
        _clean_pdf_source_ref_token(part)
        for part in re.split(r"\s*(?:[、,，/;；]|\band\b|和|及|-|–|—)\s*", cleaned_value)
        if part.strip()
    ]
    return [token for token in tokens if _is_pdf_source_ref_token(token)]


def _md_inline(
    text: Any,
    limit: int = 1800,
    url_refs: dict[str, str] | None = None,
    source_catalog: Sequence[dict[str, Any]] | None = None,
    append_citations: bool = False,
) -> str:
    raw_text = str(text or "")
    original_text = raw_text
    markers: dict[str, str] = {}
    used_urls: set[str] = set()
    source_ref_urls = _build_pdf_source_ref_urls({"source_catalog": source_catalog or []})
    source_ref_labels = _build_pdf_source_ref_labels({"source_catalog": source_catalog or []})

    def make_marker(url_token: str) -> str:
        url, suffix = _split_url_token(url_token)
        suffix = suffix.replace("、", "")
        normalized = _normalize_reference_url(url)
        if normalized:
            used_urls.add(normalized)
        label = _pdf_citation_label(url, url_refs)
        marker = f"__PDF_CIT_{len(markers)}__"
        safe_url = html.escape(url, quote=True)
        markers[marker] = f'<a href="{safe_url}" color="#2563A7"><super>[{html.escape(label)}]</super></a>{html.escape(suffix)}'
        return marker

    def make_source_ref_marker(ref_token: str) -> str:
        marker = f"__PDF_CIT_{len(markers)}__"
        raw_key = str(ref_token or "").strip().strip("[]")
        url = source_ref_urls.get(str(ref_token)) or source_ref_urls.get(raw_key) or source_ref_urls.get(_source_ref_label(ref_token))
        if url:
            normalized = _normalize_reference_url(url)
            if normalized:
                used_urls.add(normalized)
        markup = _pdf_source_ref_markup(ref_token, source_ref_urls, source_ref_labels, superscript=True)
        markers[marker] = markup or ("" if _is_pdf_internal_source_ref(raw_key) else html.escape(str(ref_token or "")))
        return marker

    def replace_source_ref_bracket(match: re.Match[str]) -> str:
        tokens = _split_pdf_source_ref_tokens(match.group(1))
        if not tokens:
            if _has_pdf_source_cue(match.group(1)):
                return ""
            return match.group(0)
        return "".join(make_source_ref_marker(token) for token in tokens)

    def replace_explicit_source_ref(match: re.Match[str]) -> str:
        tokens = _split_pdf_source_ref_tokens(match.group(1))
        return "".join(make_source_ref_marker(token) for token in tokens)

    raw_text = re.sub(
        r"\[([^\]]+)\]\((https?://[^)]+)\)",
        lambda match: f"{_clean_markdown_link_label(match.group(1)) if _is_evidence_style_link_label(match.group(1)) else match.group(1)} {make_marker(match.group(2))}",
        raw_text,
    )
    raw_text = re.sub(
        r"(?:(?:资料来源|参考来源|来源|出处)[:：]\s*)?(https?://[^\s)\]）}，。；、]+)",
        lambda match: make_marker(match.group(1)),
        raw_text,
    )
    raw_text = re.sub(r"\[([^\]\n]{1,260})\]", replace_source_ref_bracket, raw_text)
    source_ref_token = r"(?:[A-Za-z0-9]{4,}_(?:search|volc|ga|appark|rss|google|manual|input|url|src|source)[A-Za-z0-9_]*|S?\d+)"
    raw_text = re.sub(
        rf"(?:资料来源|参考来源|来源|出处)[:：]?\s*({source_ref_token}(?:\s*(?:[、,，/;；]|和|及|-|–|—)\s*{source_ref_token})*)(?:[，,。；;、.]*)",
        replace_explicit_source_ref,
        raw_text,
        flags=re.I,
    )
    raw_text = re.sub(r"(__PDF_CIT_\d+)__、+", r"\1__ ", raw_text)
    escaped = _clean(raw_text, limit)
    escaped = re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", escaped)
    escaped = re.sub(r"^([^<：:\n]{2,30})([：:])", r"<b>\1\2</b>", escaped)
    for marker, markup in markers.items():
        escaped = escaped.replace(marker, markup)
    if append_citations:
        escaped += _trailing_pdf_citations(original_text, source_catalog, url_refs, used_urls)
    return escaped


def P_md(
    text: Any,
    style: str = "Body",
    url_refs: dict[str, str] | None = None,
    source_catalog: Sequence[dict[str, Any]] | None = None,
    append_citations: bool = False,
) -> Paragraph:
    return Paragraph(
        _md_inline(text, url_refs=url_refs, source_catalog=source_catalog, append_citations=append_citations),
        STYLES[style],
    )


def _pdf_reference_source_flow(title: str, url_token: str, url_refs: dict[str, str] | None = None) -> Paragraph:
    url, _suffix = _split_url_token(url_token)
    label = _pdf_citation_label(url, url_refs)
    safe_url = html.escape(url, quote=True)
    safe_title = _clean(_clean_markdown_link_label(re.sub(r"[：:]\s*$", "", title)), 500)
    return P_html(f'<a href="{safe_url}" color="#2563A7">[{html.escape(label)}]</a>&nbsp;'
                  f'<a href="{safe_url}" color="#1F2933">{safe_title}</a>', "Body")


class ScoreStrip(Flowable):
    def __init__(self, rows: list[dict[str, Any]], width: float = CONTENT_W, height: float = 172):
        super().__init__()
        self.rows = rows
        self.width = width
        self.height = height

    def wrap(self, availWidth, availHeight):
        self.width = min(self.width, availWidth)
        return self.width, self.height

    def draw(self):
        c = self.canv
        if not self.rows:
            return
        competitors = list(dict.fromkeys(row.get("competitor", "") for row in self.rows if row.get("competitor")))
        dimensions = list(dict.fromkeys(row.get("dimension", "") for row in self.rows if row.get("dimension")))[:8]
        lookup = {(row.get("competitor"), row.get("dimension")): row for row in self.rows}
        left = 88
        top = self.height - 24
        row_h = max(11, (self.height - 34) / max(1, len(dimensions)))
        col_w = (self.width - left - 8) / max(1, len(competitors))
        c.setFont(FONT_BOLD, 9.8)
        c.setFillColor(PALETTE["ink"])
        c.drawString(0, self.height - 9, f"{len(dimensions)}维评分热力条（1-5，分析判断）")
        c.setFont(FONT_REGULAR, 7.4)
        c.setFillColor(PALETTE["muted"])
        for idx, name in enumerate(competitors):
            c.drawString(left + idx * col_w, self.height - 22, name[:18])
        colors_by = [PALETTE["teal"], PALETTE["amber"], PALETTE["blue"], PALETTE["green"]]
        for r, dim in enumerate(dimensions):
            y = top - (r + 1) * row_h
            c.setFont(FONT_REGULAR, 7.2)
            c.setFillColor(PALETTE["ink"])
            c.drawString(0, y + 3, dim[:16])
            for idx, name in enumerate(competitors):
                item = lookup.get((name, dim), {})
                value = max(0.0, min(5.0, float(item.get("score") or 0)))
                x = left + idx * col_w
                bar_w = col_w - 10
                c.setFillColor(PALETTE["soft2"])
                c.roundRect(x, y, bar_w, 8, 2, fill=1, stroke=0)
                c.setFillColor(colors_by[idx % len(colors_by)])
                if value:
                    c.roundRect(x, y, max(2, bar_w * value / 5), 8, 2, fill=1, stroke=0)
                c.setFillColor(PALETTE["ink"])
                c.setFont(FONT_BOLD, 6.8)
                c.drawRightString(x + bar_w, y + 1.6, f"{value:.1f}" if value else "NA")


class RadarChart(Flowable):
    def __init__(self, rows: list[dict[str, Any]], width: float = 245, height: float = 238):
        super().__init__()
        self.rows = rows
        self.width = width
        self.height = height

    def wrap(self, availWidth, availHeight):
        self.width = min(self.width, availWidth)
        return self.width, self.height

    def draw(self):
        c = self.canv
        if not self.rows:
            return
        competitors = list(dict.fromkeys(row.get("competitor", "") for row in self.rows if row.get("competitor")))
        dimensions = list(dict.fromkeys(row.get("dimension", "") for row in self.rows if row.get("dimension")))[:8]
        lookup = {(row.get("competitor"), row.get("dimension")): float(row.get("score") or 0) for row in self.rows}
        cx = self.width / 2
        cy = self.height / 2 - 4
        radius = min(self.width, self.height) * 0.30
        c.setFont(FONT_BOLD, 9.8)
        c.setFillColor(PALETTE["ink"])
        c.drawCentredString(cx, self.height - 10, "能力雷达图")
        for level in range(1, 6):
            pts = []
            r = radius * level / 5
            for idx in range(len(dimensions)):
                a = -math.pi / 2 + 2 * math.pi * idx / len(dimensions)
                pts.append((cx + r * math.cos(a), cy + r * math.sin(a)))
            c.setStrokeColor(PALETTE["line"])
            c.setLineWidth(0.4)
            for idx in range(len(pts)):
                c.line(pts[idx][0], pts[idx][1], pts[(idx + 1) % len(pts)][0], pts[(idx + 1) % len(pts)][1])
        for idx, dim in enumerate(dimensions):
            a = -math.pi / 2 + 2 * math.pi * idx / len(dimensions)
            c.setStrokeColor(PALETTE["line"])
            c.line(cx, cy, cx + radius * math.cos(a), cy + radius * math.sin(a))
            x = cx + (radius + 17) * math.cos(a)
            y = cy + (radius + 17) * math.sin(a)
            c.setFont(FONT_REGULAR, 6.6)
            c.setFillColor(PALETTE["muted"])
            if math.cos(a) > 0.35:
                c.drawString(x - 2, y - 2, dim[:8])
            elif math.cos(a) < -0.35:
                c.drawRightString(x + 2, y - 2, dim[:8])
            else:
                c.drawCentredString(x, y - 2, dim[:8])
        series_colors = [PALETTE["teal"], PALETTE["amber"], PALETTE["blue"], PALETTE["green"]]
        for sidx, name in enumerate(competitors[:4]):
            pts = []
            for idx, dim in enumerate(dimensions):
                a = -math.pi / 2 + 2 * math.pi * idx / len(dimensions)
                r = radius * max(0, min(5, lookup.get((name, dim), 0))) / 5
                pts.append((cx + r * math.cos(a), cy + r * math.sin(a)))
            c.setStrokeColor(series_colors[sidx % len(series_colors)])
            c.setLineWidth(1.7)
            for idx in range(len(pts)):
                c.line(pts[idx][0], pts[idx][1], pts[(idx + 1) % len(pts)][0], pts[(idx + 1) % len(pts)][1])
            c.setFillColor(series_colors[sidx % len(series_colors)])
            for x, y in pts:
                c.circle(x, y, 2, fill=1, stroke=0)
        x = 18
        c.setFont(FONT_REGULAR, 7.4)
        for sidx, name in enumerate(competitors[:4]):
            c.setFillColor(series_colors[sidx % len(series_colors)])
            c.roundRect(x, 6, 8, 8, 1, fill=1, stroke=0)
            c.setFillColor(PALETTE["muted"])
            c.drawString(x + 11, 7, name[:18])
            x += 20 + pdfmetrics.stringWidth(name[:18], FONT_REGULAR, 7.4)


class PositioningMap(Flowable):
    def __init__(self, data: dict[str, Any], width: float = 245, height: float = 205):
        super().__init__()
        self.data = data
        self.width = width
        self.height = height

    def wrap(self, availWidth, availHeight):
        self.width = min(self.width, availWidth)
        return self.width, self.height

    def draw(self):
        c = self.canv
        plot_x = 34
        plot_y = 27
        plot_w = self.width - 54
        plot_h = self.height - 58
        c.setFont(FONT_BOLD, 8.4)
        c.setFillColor(PALETTE["ink"])
        c.drawCentredString(self.width / 2, self.height - 10, "竞争定位图")
        c.setFillColor(PALETTE["soft"])
        c.roundRect(plot_x, plot_y, plot_w, plot_h, 5, fill=1, stroke=0)
        c.setStrokeColor(PALETTE["line"])
        c.line(plot_x + plot_w / 2, plot_y, plot_x + plot_w / 2, plot_y + plot_h)
        c.line(plot_x, plot_y + plot_h / 2, plot_x + plot_w, plot_y + plot_h / 2)
        c.setFont(FONT_REGULAR, 5.5)
        c.setFillColor(PALETTE["muted"])
        c.drawCentredString(plot_x + plot_w / 2, 7, _raw(self.data.get("x_axis", "成本/开放价值"), 24))
        c.saveState()
        c.translate(9, plot_y + plot_h / 2)
        c.rotate(90)
        c.drawCentredString(0, 0, _raw(self.data.get("y_axis", "应用层/治理成熟度"), 24))
        c.restoreState()
        point_colors = [PALETTE["teal"], PALETTE["amber"], PALETTE["blue"], PALETTE["green"]]
        for idx, point in enumerate(self.data.get("points", [])[:4]):
            x_value = max(0, min(5, float(point.get("x") or 0)))
            y_value = max(0, min(5, float(point.get("y") or 0)))
            px = plot_x + x_value / 5 * plot_w
            py = plot_y + y_value / 5 * plot_h
            c.setFillColor(point_colors[idx % len(point_colors)])
            c.circle(px, py, 7, fill=1, stroke=0)
            c.setFillColor(colors.white)
            c.setFont(FONT_BOLD, 6)
            c.drawCentredString(px, py - 2, _raw(point.get("competitor", "?"), 1))
            c.setFillColor(PALETTE["ink"])
            c.setFont(FONT_BOLD, 6)
            c.drawString(px + 10, py + 1.5, _raw(point.get("competitor", ""), 18))
            c.setFont(FONT_REGULAR, 5.2)
            c.setFillColor(PALETTE["muted"])
            c.drawString(px + 10, py - 6, _raw(point.get("label", ""), 22))


class BarChart(Flowable):
    def __init__(self, rows: list[dict[str, Any]], title: str, width: float = CONTENT_W, height: float = 140):
        super().__init__()
        self.rows = rows
        self.title = title
        self.width = width
        self.height = height

    def wrap(self, availWidth, availHeight):
        self.width = min(self.width, availWidth)
        return self.width, self.height

    def draw(self):
        c = self.canv
        if not self.rows:
            return
        left = 104
        chart_w = self.width - left - 22
        row_h = (self.height - 22) / len(self.rows)
        max_value = max([float(row.get("cost_index") or row.get("output_amount") or 0) for row in self.rows] + [1])
        c.setFont(FONT_BOLD, 9.8)
        c.setFillColor(PALETTE["ink"])
        c.drawString(0, self.height - 9, self.title)
        for idx, row in enumerate(self.rows):
            y = self.height - 24 - (idx + 1) * row_h + 5
            value = float(row.get("cost_index") or row.get("output_amount") or 0)
            c.setFont(FONT_REGULAR, 7.4)
            c.setFillColor(PALETTE["ink"])
            c.drawString(0, y + 2.5, _raw(f"{row.get('competitor','')} {row.get('plan_name','')}", 26))
            c.setFillColor(PALETTE["soft2"])
            c.roundRect(left, y, chart_w, 9, 2, fill=1, stroke=0)
            color = PALETTE["red"] if row.get("baseline") else PALETTE["green"] if value < 10 else PALETTE["blue"]
            c.setFillColor(color)
            c.roundRect(left, y, max(2, chart_w * value / max_value), 9, 2, fill=1, stroke=0)
            label = f"{row.get('cost_index', '')}"
            if row.get("output_amount"):
                label = f"{label} | {row.get('output_amount')} {row.get('currency','')}"
            c.setFont(FONT_BOLD, 7.2)
            c.setFillColor(PALETTE["ink"])
            c.drawRightString(self.width - 2, y + 2, _raw(label, 24))


class MetricBarChart(Flowable):
    def __init__(
        self,
        rows: list[dict[str, Any]],
        title: str,
        value_key: str,
        label_key: str,
        width: float = CONTENT_W,
        height: float = 128,
    ):
        super().__init__()
        self.rows = rows
        self.title = title
        self.value_key = value_key
        self.label_key = label_key
        self.width = width
        self.height = height

    def wrap(self, availWidth, availHeight):
        self.width = min(self.width, availWidth)
        return self.width, self.height

    def draw(self):
        c = self.canv
        if not self.rows:
            return
        left = 98
        chart_w = self.width - left - 24
        row_h = (self.height - 22) / max(1, len(self.rows))
        max_value = max([float(row.get(self.value_key) or 0) for row in self.rows] + [1])
        c.setFont(FONT_BOLD, 9.8)
        c.setFillColor(PALETTE["ink"])
        c.drawString(0, self.height - 9, self.title)
        for idx, row in enumerate(self.rows):
            y = self.height - 24 - (idx + 1) * row_h + 5
            value = float(row.get(self.value_key) or 0)
            c.setFont(FONT_REGULAR, 7.4)
            c.setFillColor(PALETTE["ink"])
            c.drawString(0, y + 2.5, _raw(row.get("competitor") or row.get("app_name", ""), 24))
            c.setFillColor(PALETTE["soft2"])
            c.roundRect(left, y, chart_w, 9, 2, fill=1, stroke=0)
            c.setFillColor(PALETTE["teal"] if self.value_key == "downloads_value" else PALETTE["green"])
            c.roundRect(left, y, max(2, chart_w * value / max_value), 9, 2, fill=1, stroke=0)
            c.setFont(FONT_BOLD, 7.2)
            c.setFillColor(PALETTE["ink"])
            c.drawRightString(self.width - 2, y + 2, _raw(row.get(self.label_key, "NA"), 24))


def _table(
    rows: list[list[Any]],
    widths: list[float],
    header_rows: int = 1,
    small: bool = True,
    url_refs: dict[str, str] | None = None,
    source_catalog: Sequence[dict[str, Any]] | None = None,
) -> Table:
    converted = []
    for r, row in enumerate(rows):
        converted_row = []
        for cell in row:
            if isinstance(cell, Flowable):
                converted_row.append(cell)
            else:
                converted_row.append(
                    P_md(
                        cell,
                        "TableHead" if r < header_rows else "TableCell",
                        url_refs=url_refs,
                        source_catalog=source_catalog,
                    )
                )
        converted.append(converted_row)
    result = Table(converted, colWidths=widths, repeatRows=header_rows, hAlign="LEFT", splitByRow=1)
    result.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, header_rows - 1), PALETTE["teal_dark"]),
                ("TEXTCOLOR", (0, 0), (-1, header_rows - 1), colors.white),
                ("GRID", (0, 0), (-1, -1), 0.35, PALETTE["line"]),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("ROWBACKGROUNDS", (0, header_rows), (-1, -1), [colors.white, PALETTE["soft"]]),
                ("LEFTPADDING", (0, 0), (-1, -1), 5),
                ("RIGHTPADDING", (0, 0), (-1, -1), 5),
                ("TOPPADDING", (0, 0), (-1, -1), 6),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ]
        )
    )
    return result


def _md_table_row(line: str) -> list[str] | None:
    if "|" not in line:
        return None
    cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
    return cells if len(cells) >= 2 else None


def _md_table_separator(cells: list[str]) -> bool:
    return all(re.fullmatch(r":?-{3,}:?", cell or "") for cell in cells)


def _flush_md_paragraph(
    flows: list[Flowable],
    paragraph: list[str],
    url_refs: dict[str, str] | None = None,
    source_catalog: Sequence[dict[str, Any]] | None = None,
) -> None:
    text = " ".join(item.strip() for item in paragraph if item.strip()).strip()
    paragraph.clear()
    if text:
        flows.append(P_md(text, url_refs=url_refs, source_catalog=source_catalog, append_citations=True))


def _flush_md_table(
    flows: list[Flowable],
    table_rows: list[list[str]],
    url_refs: dict[str, str] | None = None,
    source_catalog: Sequence[dict[str, Any]] | None = None,
) -> None:
    if not table_rows:
        return
    col_count = max(len(row) for row in table_rows)
    normalized = [row + [""] * (col_count - len(row)) for row in table_rows]
    widths = [CONTENT_W / col_count] * col_count
    flows.append(_table(normalized[:18], widths, url_refs=url_refs, source_catalog=source_catalog))
    table_rows.clear()


def _flush_md_bullet_table(
    flows: list[Flowable],
    bullet_rows: list[str],
    url_refs: dict[str, str] | None = None,
    source_catalog: Sequence[dict[str, Any]] | None = None,
) -> None:
    if not bullet_rows:
        return
    converted = [[P_md(item, "TableCell", url_refs=url_refs, source_catalog=source_catalog, append_citations=True)] for item in bullet_rows]
    table = Table(converted, colWidths=[CONTENT_W], hAlign="LEFT", splitByRow=1)
    table.setStyle(
        TableStyle(
            [
                ("GRID", (0, 0), (-1, -1), 0.35, PALETTE["line"]),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("ROWBACKGROUNDS", (0, 0), (-1, -1), [PALETTE["soft2"], colors.white]),
                ("LEFTPADDING", (0, 0), (-1, -1), 7),
                ("RIGHTPADDING", (0, 0), (-1, -1), 7),
                ("TOPPADDING", (0, 0), (-1, -1), 7),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
            ]
        )
    )
    flows.append(table)
    bullet_rows.clear()


def markdown_to_pdf_flows(
    markdown: Any,
    url_refs: dict[str, str] | None = None,
    source_catalog: Sequence[dict[str, Any]] | None = None,
) -> list[Flowable]:
    flows: list[Flowable] = []
    paragraph: list[str] = []
    table_rows: list[list[str]] = []
    bullet_rows: list[str] = []
    for raw_line in str(markdown or "").replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = raw_line.strip()
        if re.fullmatch(r"[-*_]{3,}", line):
            continue
        if not line:
            _flush_md_paragraph(flows, paragraph, url_refs=url_refs, source_catalog=source_catalog)
            _flush_md_bullet_table(flows, bullet_rows, url_refs=url_refs, source_catalog=source_catalog)
            _flush_md_table(flows, table_rows, url_refs=url_refs, source_catalog=source_catalog)
            continue
        table_cells = _md_table_row(line)
        if table_cells:
            _flush_md_paragraph(flows, paragraph, url_refs=url_refs, source_catalog=source_catalog)
            _flush_md_bullet_table(flows, bullet_rows, url_refs=url_refs, source_catalog=source_catalog)
            if _md_table_separator(table_cells):
                continue
            table_rows.append(table_cells)
            continue
        _flush_md_table(flows, table_rows, url_refs=url_refs, source_catalog=source_catalog)
        heading = re.match(r"^(#{3,5})\s+(.+)$", line)
        if heading:
            _flush_md_paragraph(flows, paragraph, url_refs=url_refs, source_catalog=source_catalog)
            _flush_md_bullet_table(flows, bullet_rows, url_refs=url_refs, source_catalog=source_catalog)
            style = "H2" if len(heading.group(1)) <= 3 else "Small"
            flows.append(P_md(heading.group(2), style, url_refs=url_refs, source_catalog=source_catalog))
            continue
        bullet = re.match(r"^[-*]\s+(.+)$", line)
        if bullet:
            _flush_md_paragraph(flows, paragraph, url_refs=url_refs, source_catalog=source_catalog)
            bullet_rows.append(bullet.group(1))
            continue
        reference_source = re.match(r"^\d+\.\s+(.+?)[：:]\s*(https?://\S+)$", line)
        if reference_source:
            _flush_md_paragraph(flows, paragraph, url_refs=url_refs, source_catalog=source_catalog)
            _flush_md_bullet_table(flows, bullet_rows, url_refs=url_refs, source_catalog=source_catalog)
            flows.append(_pdf_reference_source_flow(reference_source.group(1), reference_source.group(2), url_refs=url_refs))
            continue
        ordered = re.match(r"^(\d+)\.\s+(.+)$", line)
        if ordered:
            _flush_md_paragraph(flows, paragraph, url_refs=url_refs, source_catalog=source_catalog)
            _flush_md_bullet_table(flows, bullet_rows, url_refs=url_refs, source_catalog=source_catalog)
            flows.append(P_html(
                f"<b>{ordered.group(1)}.</b>&nbsp;"
                f"{_md_inline(ordered.group(2), 1200, url_refs=url_refs, source_catalog=source_catalog, append_citations=True)}",
                "Body",
            ))
            continue
        quote = re.match(r"^>\s+(.+)$", line)
        if quote:
            _flush_md_paragraph(flows, paragraph, url_refs=url_refs, source_catalog=source_catalog)
            _flush_md_bullet_table(flows, bullet_rows, url_refs=url_refs, source_catalog=source_catalog)
            flows.append(P_html(f"<font color='#5D6B7A'>{_md_inline(quote.group(1), 1000, url_refs=url_refs, source_catalog=source_catalog)}</font>", "Small"))
            continue
        _flush_md_bullet_table(flows, bullet_rows, url_refs=url_refs, source_catalog=source_catalog)
        paragraph.append(line)
    _flush_md_paragraph(flows, paragraph, url_refs=url_refs, source_catalog=source_catalog)
    _flush_md_bullet_table(flows, bullet_rows, url_refs=url_refs, source_catalog=source_catalog)
    _flush_md_table(flows, table_rows, url_refs=url_refs, source_catalog=source_catalog)
    return flows or [P("暂无正文。")]


def _sync_section_markdown_numbering(markdown: Any, rendered_index: int) -> str:
    lines: list[str] = []
    for raw_line in str(markdown or "").replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = raw_line.strip()
        if re.match(r"^#{1,2}\s+", line):
            continue

        heading = re.match(r"^(#{3,5})\s+(\d{1,2})((?:\.\d{1,2})+)(\s+.+)$", line)
        if heading:
            line = f"{heading.group(1)} {rendered_index}{heading.group(3)}{heading.group(4)}"

        lines.append(line if line else "")
    return "\n".join(lines).strip()


def _executive_cards(cards: list[dict[str, Any]]) -> Table:
    palette = [PALETTE["green"], PALETTE["teal"], PALETTE["amber"], PALETTE["red"]]
    cells = []
    for idx, card in enumerate(cards[:4]):
        cells.append(
            [
                P_html(f"<font color='{palette[idx].hexval()}'>■</font> {_clean(card.get('title','核心结论'), 80)}", "CardTitle"),
                P(card.get("status") or card.get("type") or "核心判断", "CardMetric"),
                P(f"{card.get('verdict','')} 证据：{_refs(card.get('evidence_refs'))}", "CardNote"),
            ]
        )
    while len(cells) < 4:
        cells.append([P("核心结论", "CardTitle"), P("NA", "CardMetric"), P("本轮未形成该类判断。", "CardNote")])
    result = Table([cells[:2], cells[2:]], colWidths=[CONTENT_W / 2 - 4, CONTENT_W / 2 - 4], hAlign="LEFT")
    result.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), PALETTE["soft"]),
                ("BOX", (0, 0), (-1, -1), 0.4, PALETTE["line"]),
                ("INNERGRID", (0, 0), (-1, -1), 5, colors.white),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("LEFTPADDING", (0, 0), (-1, -1), 9),
                ("RIGHTPADDING", (0, 0), (-1, -1), 9),
                ("TOPPADDING", (0, 0), (-1, -1), 8),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
            ]
        )
    )
    return result


def _swot_tables(content: dict[str, Any]) -> list[Flowable]:
    swot = content.get("competitor_swot") or {}
    flows: list[Flowable] = []
    color_map = {"优势": PALETTE["teal_dark"], "劣势": PALETTE["brown"], "机会": PALETTE["blue"], "威胁": PALETTE["red"]}
    for name, item in swot.items():
        cells = []
        for label in ["优势", "劣势", "机会", "威胁"]:
            cells.append([P_html(f"<b><font color='white'>{label}</font></b>", "TableCell"), P_html(f"<font color='white'>{_clean(item.get(label,'未形成判断'), 220)}</font>", "TableCell")])
        table = Table([[P(name, "H2")], [_table(cells, [23 * mm, CONTENT_W - 23 * mm], header_rows=0)]], colWidths=[CONTENT_W], hAlign="LEFT")
        table.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 1), (-1, 1), colors.white),
                    ("BOX", (0, 1), (-1, 1), 0, colors.white),
                    ("LEFTPADDING", (0, 0), (-1, -1), 0),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                ]
            )
        )
        inner = table._cellvalues[1][0]
        for idx, label in enumerate(["优势", "劣势", "机会", "威胁"]):
            inner.setStyle(TableStyle([("BACKGROUND", (0, idx), (-1, idx), color_map[label]), ("GRID", (0, idx), (-1, idx), 3, colors.white)]))
        flows.append(table)
        flows.append(Spacer(1, 6))
    return flows


def _draw_page(canvas, doc):
    canvas.saveState()
    canvas.setFillColor(PALETTE["soft"])
    canvas.rect(0, PAGE_H - 10 * mm, PAGE_W, 10 * mm, fill=1, stroke=0)
    canvas.setFillColor(PALETTE["teal_dark"])
    canvas.rect(0, PAGE_H - 10 * mm, PAGE_W, 1.1 * mm, fill=1, stroke=0)
    canvas.setFont(FONT_REGULAR, 7)
    canvas.setFillColor(PALETTE["muted"])
    canvas.drawString(MARGIN_X, PAGE_H - 6.7 * mm, "AI 驱动的竞品分析 Agent 协作系统")
    canvas.drawRightString(PAGE_W - MARGIN_X, PAGE_H - 6.7 * mm, "竞品分析报告")
    canvas.setStrokeColor(PALETTE["line"])
    canvas.line(MARGIN_X, 11.5 * mm, PAGE_W - MARGIN_X, 11.5 * mm)
    canvas.setFillColor(PALETTE["muted"])
    canvas.drawString(MARGIN_X, 7.2 * mm, "评分为分析判断，不是厂商官方指标；价格和权益请以正式采购页面为准。")
    canvas.drawRightString(PAGE_W - MARGIN_X, 7.2 * mm, f"{doc.page}")
    canvas.restoreState()


def _draw_first_page(canvas, doc):
    canvas.saveState()
    canvas.setFillColor(colors.HexColor("#F7FAFC"))
    canvas.rect(0, 0, PAGE_W, PAGE_H, fill=1, stroke=0)
    canvas.setFillColor(PALETTE["teal_dark"])
    canvas.rect(0, 0, 12 * mm, PAGE_H, fill=1, stroke=0)
    canvas.setFillColor(PALETTE["amber"])
    canvas.rect(12 * mm, PAGE_H - 45 * mm, 3 * mm, 35 * mm, fill=1, stroke=0)
    canvas.setStrokeColor(PALETTE["line"])
    canvas.line(MARGIN_X, 19 * mm, PAGE_W - MARGIN_X, 19 * mm)
    canvas.setFont(FONT_REGULAR, 7)
    canvas.setFillColor(PALETTE["muted"])
    canvas.drawString(MARGIN_X, 13.8 * mm, "资料来源：官方文档、公开评价平台、新闻/风险来源和用户上传材料")
    canvas.restoreState()


def render_competitive_report_pdf(task_id: str, report: dict[str, Any]) -> io.BytesIO:
    content = report.get("content") or {}
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=MARGIN_X,
        rightMargin=MARGIN_X,
        topMargin=MARGIN_TOP,
        bottomMargin=MARGIN_BOTTOM,
        title=_raw(content.get("title", "竞品分析报告"), 120),
    )
    story: list[Flowable] = []

    sections = content.get("display_sections") or content.get("sections") or []
    report_items = _rendered_report_items([dict(section) for section in sections if isinstance(section, dict)])[:12]
    source_catalog = content.get("source_catalog") or []
    toc_items = report_items + ([{"type": "sources", "title": "参考文献（来源链接）"}] if source_catalog else [])
    score_rows = content.get("score_dimensions") or content.get("feature_scores") or []
    api_cost = content.get("api_cost_data") or {}
    app_market = content.get("app_market_data") or {}
    url_refs = _build_pdf_url_refs(content)
    source_ref_urls = _build_pdf_source_ref_urls(content)
    source_ref_labels = _build_pdf_source_ref_labels(content)

    story.extend(
        [
            Spacer(1, 18 * mm),
            P(content.get("title", "竞品分析报告"), "Title"),
            P(f"生成时间：{report.get('generated_at','')}    可信度：{round(float(report.get('confidence_score', 0)) * 100)}%", "Subtitle"),
            Spacer(1, 9 * mm),
            P("目录", "H1"),
        ]
    )
    if toc_items:
        toc_rows = [["章节"]] + [
            [
                _section_title(
                    item.get("title") if item.get("type") in {"visual", "sources"} else item.get("section", {}).get("title", f"章节 {index}"),
                    _report_item_section_index(item, index),
                    True,
                )
            ]
            for index, item in enumerate(toc_items, start=1)
        ]
        story.append(_table(toc_rows, [CONTENT_W]))
    else:
        story.append(P("暂无可渲染的报告章节。"))

    def append_visual_chapter(index: int) -> None:
        story.append(Spacer(1, 7 * mm))
        story.append(P(_section_title("可视化总览", index, True), "H1"))
        story.append(P(f"{index}.1 评分矩阵", "H2"))
        if score_rows:
            story.append(ScoreStrip(score_rows))
        else:
            story.append(P("暂无评分数据。"))

        story.append(Spacer(1, 7 * mm))
        story.append(P(f"{index}.2 API 成本柱状图", "H2"))
        api_rows = api_cost.get("rows") or []
        if api_rows:
            story.append(BarChart(api_rows, api_cost.get("title") or "输出成本指数（基准最高价 = 100，越低越好）"))
            if api_cost.get("formula"):
                story.append(P(api_cost.get("formula", ""), "Small"))
            if api_cost.get("caveat"):
                story.append(P(api_cost.get("caveat", ""), "Small"))
            story.append(
                _table(
                    [["竞品", "套餐/模型", "输出价格", "成本指数", "说明", "证据"]]
                    + [
                        [
                            row.get("competitor", ""),
                            row.get("plan_name", "") or row.get("model", ""),
                            f"{row.get('output_amount', '')} {row.get('currency', '')}",
                            row.get("cost_index", ""),
                            row.get("note", "") or row.get("basis", ""),
                            _source_refs_flow(row.get("evidence_refs"), source_ref_urls, source_ref_labels),
                        ]
                        for row in api_rows[:16]
                    ],
                    [23 * mm, 31 * mm, 25 * mm, 21 * mm, CONTENT_W - 125 * mm, 21 * mm],
                )
            )
        else:
            story.append(P(api_cost.get("caveat") or "未抽取到可计算的 API/token 官方价格，暂不绘制成本图。"))

        story.append(Spacer(1, 7 * mm))
        story.append(P(f"{index}.3 能力雷达图", "H2"))
        if score_rows:
            story.append(RadarChart(score_rows, width=CONTENT_W, height=268))
            story.append(P("雷达图直接由评分矩阵派生，确保能力图和评分表使用同一组评分依据。", "Caption"))
        else:
            story.append(P("暂无可渲染的评分维度，完成分析后会从评分表自动生成雷达图。"))

        story.append(Spacer(1, 7 * mm))
        story.append(P(f"{index}.4 App 市场表现", "H2"))
        app_rows = app_market.get("rows") or []
        if app_rows:
            story.append(P("数据来自 AppArk 竞品对比页；下载量和收入额使用柱状图，榜单排名越小表示位置越靠前。", "Small"))
            story.append(MetricBarChart(app_rows, "下载量对比", "downloads_value", "downloads_text", height=128))
            story.append(Spacer(1, 6))
            story.append(MetricBarChart(app_rows, "收入额对比", "revenue_usd", "revenue_text", height=128))
            story.append(Spacer(1, 6))
            story.append(
                _table(
                    [["应用", "下载量", "收入额", "免费榜", "付费榜", "总榜"]]
                    + [
                        [
                            row.get("competitor") or row.get("app_name", ""),
                            row.get("downloads_text", ""),
                            row.get("revenue_text", ""),
                            "-" if row.get("free_rank") in {None, ""} else row.get("free_rank"),
                            "-" if row.get("paid_rank") in {None, ""} else row.get("paid_rank"),
                            "-" if row.get("overall_rank") in {None, ""} else row.get("overall_rank"),
                        ]
                        for row in app_rows[:12]
                    ],
                    [33 * mm, 28 * mm, 30 * mm, 21 * mm, 21 * mm, CONTENT_W - 133 * mm],
                )
            )
        else:
            story.append(P(app_market.get("caveat") or "暂无 AppArk 下载量、收入额和榜单排名数据。"))

    for index, item in enumerate(report_items, start=1):
        section_index = _report_item_section_index(item, index)
        if item.get("type") == "visual":
            append_visual_chapter(section_index)
            continue
        section = item.get("section") or {}
        story.append(Spacer(1, 7 * mm))
        title = section.get("title") or f"章节 {section_index}"
        body = section.get("markdown") or section.get("body") or ""
        story.append(P(_section_title(title, section_index, True), "H1"))
        story.extend(markdown_to_pdf_flows(
            _sync_section_markdown_numbering(body, section_index),
            url_refs=url_refs,
            source_catalog=source_catalog,
        ))
    if source_catalog:
        story.extend(_source_catalog_flows(content, len(report_items) + 1))

    doc.build(story, onFirstPage=_draw_first_page, onLaterPages=_draw_page)
    buffer.seek(0)
    return buffer
