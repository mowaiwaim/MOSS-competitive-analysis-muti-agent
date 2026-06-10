from __future__ import annotations

import json
import os
import re
import socket
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError

from schema import LLMClaimDraft


DEFAULT_DOUBAO_BASE_URL = "https://ark.cn-beijing.volces.com/api/v3"
DEFAULT_DOUBAO_MODEL_NAME = "Doubao-Seed-2.0-lite"


class LLMProviderError(RuntimeError):
    pass


@dataclass
class LLMResult:
    provider: str
    claims: list[dict[str, Any]]
    input_tokens: int
    output_tokens: int
    used_fallback: bool = False
    fallback_reason: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class LLMJSONResult:
    provider: str
    data: dict[str, Any]
    input_tokens: int
    output_tokens: int
    used_fallback: bool = False
    fallback_reason: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)


def estimate_tokens(text: str) -> int:
    return max(1, len(text or "") // 4)


def build_prompt(task: dict[str, Any], evidence: list[dict[str, Any]]) -> str:
    evidence_lines = []
    allowed_source_ids = []
    for item in evidence[:24]:
        if item.get("source_id"):
            allowed_source_ids.append(item["source_id"])
        evidence_lines.append(
            f"- source_id={item['source_id']} chunk={item.get('chunk_index', 0)} "
            f"competitor={item.get('competitor_name', '')} module={item.get('module', '')} "
            f"type={item.get('source_type', '')} credibility={item.get('credibility', '')} "
            f"raw={item.get('raw_content_status', '')} title={item.get('source_title', '')}: {item.get('excerpt', '')[:520]}"
        )
    competitors = "、".join(task.get("competitors", []))
    focus = "、".join(task.get("focus_areas", []))
    return (
        "你是竞品分析系统的分析 Agent。你的任务是基于给定 evidence 直接生成可进入报告的业务结论，不能补充未给出的事实。"
        "结论要像正式竞品报告，不要写“已采集、采集日期、材料类型、说明、来源清单、待复核、待补证、日志、流程、Schema、来源状态、证据覆盖最高、基于某文件”等过程话术。"
        "不要把文件名、PDF名、source_id 或上传材料的元信息写进 content；只提炼业务含义。"
        "必须覆盖用户关注维度；证据支持时至少输出功能、定价/价格口径、用户评价、用户画像、SWOT 和总览。"
        "价格、套餐、车型/规格、API 单价等时间敏感内容必须只在 evidence 中有明确来源时输出；否则写成证据缺口并标记 needs_review=true。"
        "每条 content 要包含明确判断和一句小理由，不能只复述网页标题。"
        "必须只输出合法 JSON，不要 Markdown，不要解释。输出格式必须是："
        '{"claims":[{"section":"overview","content":"...","confidence":0.72,'
        '"source_ids":["..."],"needs_review":false,"status":"reportable","uncertainty":""}]}。'
        "section 只能从 overview, feature_tree, pricing_model, user_persona, reviews, swot 中选择。"
        "status 只能是 draft, needs_review, confirmed, reportable。"
        f"source_ids 只能从这些值中选择：{', '.join(allowed_source_ids)}。每条 claim 至少绑定一个 source_id。"
        "若 evidence 只有搜索摘要或来源不足，仍可输出谨慎判断，但必须降低 confidence、标记 needs_review，并在 uncertainty 写清缺什么材料。"
        "建议输出 8 到 14 条 claim，并让两个竞品都有可对比结论。"
        f"\n行业：{task.get('industry', '')}\n竞品：{competitors}\n关注维度：{focus}\nEvidence:\n"
        + "\n".join(evidence_lines)
    )


def build_report_prompt(task: dict[str, Any], sections: list[dict[str, Any]], evidence: list[dict[str, Any]]) -> str:
    source_brief = [
        {
            "ref": item.get("source_id", ""),
            "title": item.get("source_title", ""),
            "excerpt": item.get("excerpt", "")[:260],
        }
        for item in evidence[:10]
    ]
    payload = {
        "industry": task.get("industry", ""),
        "competitors": task.get("competitors", []),
        "focus_areas": task.get("focus_areas", []),
        "sections": sections,
        "sources": source_brief,
    }
    return (
        "你是竞品分析报告撰写 Agent。请把结构化结论改写成普通业务用户能读懂的中文报告。"
        "不要输出 source_id、Trace、Schema、证据状态、日志、模板占位符、网页导航乱码、采集日期、材料类型、说明、来源清单、PDF 文件名。"
        "不要写“结论基于...”“当前资料显示...”“待补证”“需补充材料”等过程话术；报告正文要直接给业务判断。"
        "不要编造给定来源以外的功能、价格、销量、评价或市场数据。"
        "功能对比写业务范围、核心能力、产品/车型/服务覆盖；定价写行业适配的价格区间、代表产品/车型/套餐、限制条件或销量/出货线索；"
        "用户评价要按平台差异、正向主题、负向/风险主题和采购含义来写，不要复述材料标题。"
        "SWOT 要按每个竞品分别给出差异化 S/W/O/T，不能输出套话，也不能三家完全一样。"
        "只输出合法 JSON："
        '{"summary":"...","sections":[{"key":"feature_tree","title":"功能对比","body":"...",'
        '"claims":["..."],"table":[["竞品","对比内容"]]}]}。'
        "返回的 sections 必须与输入 sections 的 key 一一对应，不要新增未输入模块。\n"
        f"输入：{json.dumps(payload, ensure_ascii=False)}"
    )


def build_qa_prompt(task: dict[str, Any], claims: list[dict[str, Any]], evidence: list[dict[str, Any]]) -> str:
    payload = {
        "industry": task.get("industry", ""),
        "competitors": task.get("competitors", []),
        "focus_areas": task.get("focus_areas", []),
        "claims": claims[:18],
        "evidence": [
            {
                "ref": item.get("source_id", ""),
                "title": item.get("source_title", ""),
                "excerpt": item.get("excerpt", "")[:220],
            }
            for item in evidence[:10]
        ],
    }
    return (
        "你是竞品分析质检 Agent。只基于输入 claims 和 evidence 检查是否存在无来源事实、逻辑跳跃、"
        "时间敏感结论缺少官方或一手来源、报告口吻像日志、用户评价过度泛化、SWOT 套话、或来源不能支撑结论。"
        "如果发现问题，reason 必须是给人工看的具体中文理由：说明哪一句结论哪里不稳、当前来源为什么不够、应该补什么。"
        "能通过自动补采/重做分析解决的问题，repair_action 输出 auto_collect；只有必须人工确认、上传材料或口述补充时才输出 manual_supplement。"
        "只输出合法 JSON："
        '{"passed":false,"findings":[{"severity":"high","reason":"...","claim_index":0,'
        '"target_agent":"采集 Agent","finding_type":"pricing_missing_official","action_hint":"...",'
        '"missing_material":"官方价格页、规格/型号页或权威价格材料","suggested_queries":["..."],"repair_action":"auto_collect"}],"summary":"..."}。'
        "finding_type 可用 missing_source, unsupported_claim, pricing_missing_official, missing_date, source_ownership_mismatch, overclaim, logic_gap, review_sample_bias, swot_template。"
        "不要输出 API Key 或额外解释。\n"
        f"输入：{json.dumps(payload, ensure_ascii=False)}"
    )


def build_collection_prompt(task: dict[str, Any], sources: list[dict[str, Any]]) -> str:
    payload = {
        "industry": task.get("industry", ""),
        "competitors": task.get("competitors", []),
        "focus_areas": task.get("focus_areas", []),
        "sources": [
            {
                "title": item.get("title", ""),
                "source_type": item.get("source_type", ""),
                "url_or_path": item.get("url_or_path", item.get("url", "")),
                "excerpt": item.get("excerpt", "")[:260],
            }
            for item in sources[:12]
        ],
    }
    return (
        "你是竞品分析采集 Agent。请根据当前已抓取/搜索的公开来源，判断这些来源能覆盖哪些报告模块，"
        "并给出还应该补搜的方向。不要编造事实，不要输出 source_id，不要输出 Markdown。"
        "只输出合法 JSON："
        '{"summary":"...","covered_modules":["功能对比"],"search_gaps":["补充定价页"],"next_queries":["..."]}。\n'
        f"输入：{json.dumps(payload, ensure_ascii=False)}"
    )


def build_collection_query_plan_prompt(task: dict[str, Any]) -> str:
    payload = {
        "industry": task.get("industry", ""),
        "competitors": task.get("competitors", []),
        "focus_areas": task.get("focus_areas", []),
    }
    return (
        "你是竞品分析采集 Agent 的搜索规划器。请为每个竞品生成公开搜索 query，必须覆盖："
        "官网/产品功能、行业适配价格口径、用户评价/口碑、销量/出货/市场新闻、SWOT素材。"
        "价格口径要按行业改写：汽车搜车型价格和配置，光伏搜组件/电池片/硅料价格和规格，煤炭搜煤种/热值/长协/现货价格，AI 才搜 API/会员价格。"
        "如果对象是公司/股票，优先搜索官网、投资者关系、年报/公告、产品、新闻、财报、行业数据；"
        "如果对象是品类/概念，优先搜索定义/标准解释、代表品牌、价格带、消费场景、市场报道、评价内容。"
        "query 必须是 2 到 6 个关键词的短搜索词，不要写成长句。"
        "只输出合法 JSON，不要 Markdown，不要解释，不要输出 API Key。格式："
        '{"summary":"...","queries":[{"competitor":"...","module":"定价","query":"...","aliases":["..."],"related_terms":["..."]}]}。'
        "每个竞品每个模块至少 1 条 query，query 用中文，适合搜索引擎；aliases/related_terms 可用于候选网页相关性判断。\n"
        f"输入：{json.dumps(payload, ensure_ascii=False)}"
    )


def build_report_dimension_plan_prompt(task: dict[str, Any]) -> str:
    payload = {
        "industry": task.get("industry", ""),
        "competitors": task.get("competitors", []),
        "focus_areas": task.get("focus_areas", []),
    }
    return (
        "你是通用竞品分析系统的报告规划 Agent。请先判断行业，再规划本报告应该比较什么维度。"
        "不要沿用 Chat AI 固定模板；只有当对象确实是大模型/API/开发者平台时，才输出 API 成本指数。"
        "汽车应比较车型/配置价格、续航/三电、智驾/座舱、交付/渠道、安全与口碑；"
        "光伏应比较技术路线、组件/电池效率、组件/电池片/硅料价格、产能/出货、客户与供应链；"
        "煤炭应比较煤种/热值/产地价格、产能/销量、长协/现货、运输成本、安全环保和政策风险。"
        "维度名称必须业务化、可评分、可被公开来源验证。"
        "只输出合法 JSON，不要 Markdown，不要解释。格式："
        '{"summary":"...","price_metric_label":"车型/配置价格","price_metric_description":"...",'
        '"show_api_cost":false,"pricing_terms":["指导价","车型","配置"],'
        '"feature_dimensions":[{"name":"产品矩阵","description":"...","keywords":["车型","配置"]}],'
        '"score_dimensions":[{"name":"三电/续航","description":"...","keywords":["续航","电池"]}],'
        '"positioning":{"x_axis":"价格/成本竞争力","y_axis":"产品/品牌竞争力",'
        '"x_dimensions":["车型价格","用车成本"],"y_dimensions":["三电/续航","品牌/渠道"],"interpretation":"..."},'
        '"decision_scenarios":[{"scenario":"家庭通勤","dimensions":["车型价格","三电/续航"],"rule":"..."}]}。'
        "feature_dimensions 建议 5 到 7 个，score_dimensions 建议 6 到 8 个；每个 keywords 3 到 8 个短词。"
        f"\n输入：{json.dumps(payload, ensure_ascii=False)}"
    )


def build_questionnaire_design_prompt(
    task: dict[str, Any], research_objective: str, target_users: str, dimensions: list[str]
) -> str:
    competitors = "、".join(task.get("competitors", []))
    return (
        "你是竞品分析系统的调研设计 Agent。请根据竞品信息、调研目标和覆盖维度，生成一份结构化调研问卷。"
        "问卷必须覆盖以下五个模块：用户背景、使用习惯、满意度、竞品对比、需求痛点。"
        "每个选定维度至少要有 1-2 道针对性题目。题目类型包括 single_choice、multiple_choice、likert（5 分量表）和 open_ended。"
        "选项要有区分度，不能全是一般化表达。每道题要有 question_text 和一个简短的 question_key（英文 snack_case）。"
        "likert 量表题需要给出两端标签（如 1=非常不满意, 5=非常满意）。"
        "预计填写时间控制在 5-15 分钟。只输出合法 JSON，不要 Markdown，不要解释。格式："
        '{"title":"问卷标题","description":"问卷说明",'
        '"sections":[{"section_title":"基本信息","questions":[{"id":"Q1","type":"single_choice",'
        '"question_key":"usage_frequency","question_text":"...","options":["A","B","C"],"required":true}]}],'
        '"estimated_time_minutes":8,"recommended_channels":["线上问卷","面对面访谈"]}。\n'
        f"行业：{task.get('industry', '')}\n竞品：{competitors}\n"
        f"调研目标：{research_objective}\n"
        f"目标用户：{target_users or '未指定，请按行业通用画像设计'}\n"
        f"覆盖维度：{'、'.join(dimensions)}"
    )


def build_survey_analysis_prompt(
    task: dict[str, Any], response_rows: list[dict[str, Any]], survey_structure: dict[str, Any]
) -> str:
    competitors = "、".join(task.get("competitors", []))
    row_count = len(response_rows)
    sample = json.dumps(response_rows[:40], ensure_ascii=False)
    structure = json.dumps(survey_structure, ensure_ascii=False)
    return (
        "你是竞品分析系统的调研分析 Agent。请分析这份用户调研数据，提取可用于竞品报告的发现。"
        "基于数据说话，不要编造数据中不存在的事实。"
        "如果样本量少于 10，必须在 summary 中提示样本量不足的局限。"
        "identify 不同的用户群体（按使用频次、付费意愿、功能偏好、痛点等聚类），每个群体给一个中文标签和特征描述。"
        "statistics 中要给出每个选择题的选项分布百分比，likert 题要给出均值和标准差。"
        "claims_for_report 中的每条结论都要能直接写入竞品报告，section 使用 overview/feature_tree/pricing_model/user_persona/reviews/swot。"
        "不要输出 Markdown 或额外解释，只输出合法 JSON。格式："
        '{"summary":"...","respondent_profile":{"total":N,"segments":[]},'
        '"key_findings":[{"finding":"...","confidence":0.8,"evidence":"数据支撑","related_dimension":"用户画像|用户评价|...","severity":"high|medium|low"}],'
        '"statistics":[{"question_id":"Q1","distribution":{"A":50,"B":30},"mean_likert":null,"std":null}],'
        '"segments":[{"name":"价格敏感型","size":45,"characteristics":"...","pain_points":["..."]}],'
        '"claims_for_report":[{"section":"reviews","content":"...","confidence":0.78,"needs_review":false}]}。\n'
        f"行业：{task.get('industry', '')}\n竞品：{competitors}\n"
        f"样本量：{row_count}\n问卷结构：{structure}\n"
        f"响应数据（前 40 条）：{sample}"
    )


def build_interview_guide_prompt(
    task: dict[str, Any], research_objective: str, target_users: str, interview_count: int
) -> str:
    competitors = "、".join(task.get("competitors", []))
    return (
        "你是竞品分析系统的调研设计 Agent。请生成一份结构化的用户访谈提纲。"
        "访谈分三个阶段：热身（5分钟，建立信任，了解背景）、核心探索（20-30分钟，围绕使用场景、竞品对比、痛点和需求展开）、总结（5-10分钟，确认理解和开放补充）。"
        "每个问题要有 id、text（完整的中文提问句）和 probe（追问方向）。"
        "问题要具体、开放，避免引导性措辞。覆盖维度要明确映射到具体问题编号。"
        "notes_for_interviewer 要给访谈执行建议。只输出合法 JSON，不要 Markdown，不要解释。格式："
        '{"title":"用户访谈提纲","estimated_duration_minutes":45,"target_profile":"目标受访者画像描述",'
        '"phases":[{"phase":"热身","duration_minutes":5,"goals":["建立信任"],'
        '"questions":[{"id":"Q1","text":"...","probe":"..."}]}],'
        '"notes_for_interviewer":"...","dimension_coverage":{"功能对比":["Q3","Q7"],"用户画像":["Q1","Q2"]}}。\n'
        f"行业：{task.get('industry', '')}\n竞品：{competitors}\n"
        f"调研目标：{research_objective}\n"
        f"目标受访者：{target_users or '未指定，请按行业画像推断'}\n预计访谈人数：{interview_count} 人"
    )


def build_interview_extraction_prompt(
    task: dict[str, Any], transcript_text: str, interviewee_profile: dict[str, str]
) -> str:
    competitors = "、".join(task.get("competitors", []))
    return (
        "你是竞品分析系统的访谈分析 Agent。请从用户访谈原文中提取可用于竞品报告的结构化发现。"
        "直接基于受访者原话提取，不要编造访谈中没有的内容。"
        "key_quotes 中的 quote 必须是原文引用（可以轻度脱敏），标注 sentiment 和 theme。"
        "scenarios 描述受访者提到的使用场景，标注频率和痛苦程度。pain_points 标注严重性和证据引用。needs 标注优先级。"
        "claims_for_report 中的每条结论必须绑定到 report section，content 要用业务语言写，不能写“受访者说”。"
        "如果某类发现数据不足，在对应数组留空并在 summary 中说明。"
        "不要输出 Markdown 或额外解释，只输出合法 JSON。格式："
        '{"summary":"本次访谈的核心发现...","interviewee_profile":{"segment":"企业用户","usage_frequency":"daily"},'
        '"key_quotes":[{"quote":"原话引用（可轻度脱敏）","theme":"痛点|期望|评价|习惯","sentiment":"positive|negative|neutral"}],'
        '"scenarios":[{"scenario":"使用场景描述","frequency":"daily|weekly","pain_level":"high|medium|low","workaround":"当前替代方案"}],'
        '"pain_points":[{"point":"痛点描述","severity":"high|medium|low","evidence_quote":"引用原文"}],'
        '"needs":[{"need":"需求描述","priority":"high|medium|low","current_solution":"当前解决方案"}],'
        '"claims_for_report":[{"section":"user_persona","content":"可进入报告的结论","confidence":0.75,"needs_review":true}]}。\n'
        f"行业：{task.get('industry', '')}\n竞品：{competitors}\n"
        f"受访者画像：{json.dumps(interviewee_profile, ensure_ascii=False)}\n"
        f"访谈原文：{transcript_text[:6000]}"
    )


def extract_json_payload(text: str) -> Any:
    stripped = (text or "").strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?", "", stripped, flags=re.I).strip()
        stripped = re.sub(r"```$", "", stripped).strip()
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        match = re.search(r"(\[[\s\S]*\]|\{[\s\S]*\})", stripped)
        if not match:
            raise
        return json.loads(match.group(1))


class LLMProvider:
    def __init__(self) -> None:
        configured = os.environ.get("LLM_PROVIDER", "").strip().lower()
        self.provider = configured or ("doubao" if os.environ.get("DOUBAO_API_KEY") else "mock")
        self.api_key = os.environ.get("DOUBAO_API_KEY", "")
        self.endpoint_id = os.environ.get("DOUBAO_ENDPOINT_ID", "")
        self.model_name = os.environ.get("DOUBAO_MODEL_NAME", DEFAULT_DOUBAO_MODEL_NAME)
        self.model_id = self.endpoint_id or os.environ.get("DOUBAO_MODEL_NAME", "")
        self.base_url = os.environ.get("DOUBAO_BASE_URL", DEFAULT_DOUBAO_BASE_URL).rstrip("/")
        try:
            self.timeout_seconds = max(5, min(60, int(os.environ.get("DOUBAO_TIMEOUT_SECONDS", "20"))))
        except ValueError:
            self.timeout_seconds = 20

    def generate_claims(self, task: dict[str, Any], evidence: list[dict[str, Any]]) -> LLMResult:
        prompt = build_prompt(task, evidence)
        if self.provider == "doubao":
            return self._generate_with_doubao(prompt, evidence)
        return self._generate_with_mock(prompt, evidence)

    def rewrite_report(self, task: dict[str, Any], sections: list[dict[str, Any]], evidence: list[dict[str, Any]]) -> LLMJSONResult:
        prompt = build_report_prompt(task, sections, evidence)
        if self.provider != "doubao":
            return LLMJSONResult(
                provider="mock",
                data={},
                input_tokens=estimate_tokens(prompt),
                output_tokens=1,
                used_fallback=True,
                fallback_reason="未配置豆包，使用确定性报告改写。",
                tool_calls=[{"name": "mock_report_rewrite", "result": "skipped"}],
            )
        payload = self._chat_json(prompt, timeout=self.timeout_seconds)
        if not isinstance(payload.get("sections"), list):
            raise LLMProviderError("doubao report schema validation failed")
        return LLMJSONResult(
            provider="doubao",
            data=payload,
            input_tokens=estimate_tokens(prompt),
            output_tokens=estimate_tokens(json.dumps(payload, ensure_ascii=False)),
            tool_calls=[{"name": "doubao_chat_completions", "result": "report_rewrite"}],
        )

    def review_claims(self, task: dict[str, Any], claims: list[dict[str, Any]], evidence: list[dict[str, Any]]) -> LLMJSONResult:
        prompt = build_qa_prompt(task, claims, evidence)
        if self.provider != "doubao":
            return LLMJSONResult(
                provider="mock",
                data={"passed": True, "findings": [], "summary": "mock qa skipped"},
                input_tokens=estimate_tokens(prompt),
                output_tokens=1,
                used_fallback=True,
                fallback_reason="未配置豆包，使用规则质检。",
                tool_calls=[{"name": "mock_qa_review", "result": "skipped"}],
            )
        payload = self._chat_json(prompt, timeout=self.timeout_seconds)
        if not isinstance(payload.get("passed"), bool) or not isinstance(payload.get("findings", []), list):
            raise LLMProviderError("doubao qa schema validation failed")
        return LLMJSONResult(
            provider="doubao",
            data=payload,
            input_tokens=estimate_tokens(prompt),
            output_tokens=estimate_tokens(json.dumps(payload, ensure_ascii=False)),
            tool_calls=[{"name": "doubao_chat_completions", "result": "qa_review"}],
        )

    def review_collection(self, task: dict[str, Any], sources: list[dict[str, Any]]) -> LLMJSONResult:
        prompt = build_collection_prompt(task, sources)
        if self.provider != "doubao":
            return LLMJSONResult(
                provider="mock",
                data={"summary": "mock collection review skipped", "covered_modules": [], "search_gaps": []},
                input_tokens=estimate_tokens(prompt),
                output_tokens=1,
                used_fallback=True,
                fallback_reason="未配置豆包，使用规则采集判断。",
                tool_calls=[{"name": "mock_collection_review", "result": "skipped"}],
            )
        payload = self._chat_json(prompt, timeout=self.timeout_seconds)
        if not isinstance(payload.get("summary", ""), str):
            raise LLMProviderError("doubao collection schema validation failed")
        return LLMJSONResult(
            provider="doubao",
            data=payload,
            input_tokens=estimate_tokens(prompt),
            output_tokens=estimate_tokens(json.dumps(payload, ensure_ascii=False)),
            tool_calls=[{"name": "doubao_chat_completions", "result": "collection_review"}],
        )

    def plan_collection_queries(self, task: dict[str, Any]) -> LLMJSONResult:
        prompt = build_collection_query_plan_prompt(task)
        if self.provider != "doubao":
            return LLMJSONResult(
                provider="mock",
                data={"summary": "mock query plan skipped", "queries": []},
                input_tokens=estimate_tokens(prompt),
                output_tokens=1,
                used_fallback=True,
                fallback_reason="未配置豆包，使用规则搜索计划。",
                tool_calls=[{"name": "mock_collection_query_plan", "result": "skipped"}],
            )
        payload = self._chat_json(prompt, timeout=self.timeout_seconds)
        if not isinstance(payload.get("queries", []), list):
            raise LLMProviderError("doubao collection query plan schema validation failed")
        return LLMJSONResult(
            provider="doubao",
            data=payload,
            input_tokens=estimate_tokens(prompt),
            output_tokens=estimate_tokens(json.dumps(payload, ensure_ascii=False)),
            tool_calls=[{"name": "doubao_chat_completions", "result": "collection_query_plan"}],
        )

    def plan_report_dimensions(self, task: dict[str, Any]) -> LLMJSONResult:
        prompt = build_report_dimension_plan_prompt(task)
        if self.provider != "doubao":
            return LLMJSONResult(
                provider="mock",
                data={},
                input_tokens=estimate_tokens(prompt),
                output_tokens=1,
                used_fallback=True,
                fallback_reason="未配置豆包，使用规则行业维度规划。",
                tool_calls=[{"name": "mock_report_dimension_plan", "result": "skipped"}],
            )
        payload = self._chat_json(prompt, timeout=self.timeout_seconds)
        if not isinstance(payload.get("score_dimensions", []), list):
            raise LLMProviderError("doubao report dimension plan schema validation failed")
        return LLMJSONResult(
            provider="doubao",
            data=payload,
            input_tokens=estimate_tokens(prompt),
            output_tokens=estimate_tokens(json.dumps(payload, ensure_ascii=False)),
            tool_calls=[{"name": "doubao_chat_completions", "result": "report_dimension_plan"}],
        )

    def classify_industry(self, competitors: list[str], notes: str = "", fallback: str = "待识别行业") -> str:
        if self.provider != "doubao" or not self.api_key or not self.model_id:
            return fallback
        prompt = (
            "你是一个产品行业分类器。根据产品/竞品名称和用户补充说明，返回一个简短中文行业名。"
            "只输出 JSON 对象，不要 Markdown。格式：{\"industry\":\"...\"}。"
            "行业名控制在 4 到 14 个汉字，无法判断时返回“待识别行业”。\n"
            f"产品/竞品：{'、'.join(competitors)}\n补充说明：{notes[:500]}"
        )
        request_body = {
            "model": self.model_id,
            "messages": [
                {"role": "system", "content": "你只输出合法 JSON 对象，不输出额外解释。"},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0,
        }
        request = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(request_body).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=8) as response:
                payload = json.loads(response.read().decode("utf-8", errors="replace"))
            content = payload.get("choices", [{}])[0].get("message", {}).get("content", "")
            parsed = extract_json_payload(content)
        except (json.JSONDecodeError, KeyError, TypeError, urllib.error.URLError, TimeoutError, socket.timeout, OSError):
            return fallback
        industry = ""
        if isinstance(parsed, dict):
            industry = str(parsed.get("industry", "")).strip()
        if not industry or len(industry) > 32:
            return fallback
        return industry

    def design_questionnaire(
        self, task: dict[str, Any], research_objective: str,
        target_users: str = "", dimensions: list[str] | None = None,
    ) -> LLMJSONResult:
        dims = dimensions or task.get("focus_areas", ["功能对比", "定价", "用户评价", "用户画像", "SWOT"])
        prompt = build_questionnaire_design_prompt(task, research_objective, target_users, dims)
        if self.provider != "doubao":
            competitors = [str(item) for item in task.get("competitors", []) if str(item).strip()]
            competitor_label = "、".join(competitors) or "目标竞品"
            dim_options = [str(item) for item in dims[:6]]
            return LLMJSONResult(
                provider="mock",
                data={
                    "title": f"{competitor_label} 用户调研问卷",
                    "description": f"围绕“{research_objective[:120]}”收集用户背景、使用习惯、竞品对比、满意度和需求痛点。",
                    "sections": [
                        {
                            "section_title": "用户背景",
                            "questions": [
                                {
                                    "id": "Q1",
                                    "type": "single_choice",
                                    "question_key": "user_role",
                                    "question_text": "你的角色更接近以下哪一类？",
                                    "options": ["个人用户", "团队成员", "团队负责人", "采购/决策者", "其他"],
                                    "required": True,
                                },
                                {
                                    "id": "Q2",
                                    "type": "single_choice",
                                    "question_key": "usage_frequency",
                                    "question_text": f"你使用 {competitor_label} 或同类产品的频率是？",
                                    "options": ["每天", "每周数次", "每月数次", "偶尔试用", "尚未使用"],
                                    "required": True,
                                },
                            ],
                        },
                        {
                            "section_title": "竞品对比",
                            "questions": [
                                {
                                    "id": "Q3",
                                    "type": "multiple_choice",
                                    "question_key": "used_products",
                                    "question_text": "你实际体验过哪些产品？",
                                    "options": competitors + ["其他同类产品", "尚未实际体验"],
                                    "required": True,
                                },
                                {
                                    "id": "Q4",
                                    "type": "likert",
                                    "question_key": "overall_satisfaction",
                                    "question_text": "你对当前主要使用产品的整体满意度如何？1=非常不满意，5=非常满意。",
                                    "options": ["1", "2", "3", "4", "5"],
                                    "required": True,
                                },
                            ],
                        },
                        {
                            "section_title": "需求痛点",
                            "questions": [
                                {
                                    "id": "Q5",
                                    "type": "multiple_choice",
                                    "question_key": "decision_factors",
                                    "question_text": "你选择或推荐产品时最看重哪些因素？",
                                    "options": dim_options + ["价格/成本", "服务与生态", "数据安全", "学习成本"],
                                    "required": True,
                                },
                                {
                                    "id": "Q6",
                                    "type": "open_ended",
                                    "question_key": "pain_points",
                                    "question_text": "请描述你在使用或选型这些产品时遇到的最大问题。",
                                    "required": False,
                                },
                            ],
                        },
                    ],
                    "estimated_time_minutes": 6,
                    "recommended_channels": ["线上问卷", "用户群", "访谈前筛选"],
                },
                input_tokens=estimate_tokens(prompt),
                output_tokens=estimate_tokens(research_objective) + 80,
                used_fallback=True,
                fallback_reason="未配置豆包，使用本地问卷大纲模板。",
                tool_calls=[{"name": "mock_questionnaire_design", "result": "local outline"}],
            )
        payload = self._chat_json(prompt, timeout=self.timeout_seconds + 10)
        if not isinstance(payload.get("sections"), list):
            raise LLMProviderError("doubao questionnaire design schema validation failed")
        return LLMJSONResult(
            provider="doubao",
            data=payload,
            input_tokens=estimate_tokens(prompt),
            output_tokens=estimate_tokens(json.dumps(payload, ensure_ascii=False)),
            tool_calls=[{"name": "doubao_chat_completions", "result": "questionnaire_design"}],
        )

    def analyze_survey_responses(
        self, task: dict[str, Any], response_rows: list[dict[str, Any]], survey_structure: dict[str, Any],
    ) -> LLMJSONResult:
        prompt = build_survey_analysis_prompt(task, response_rows, survey_structure)
        if self.provider != "doubao":
            total = len(response_rows)
            return LLMJSONResult(
                provider="mock",
                data={
                    "summary": f"未配置豆包，无法自动分析 {total} 份问卷数据。",
                    "respondent_profile": {"total": total, "segments": []},
                    "key_findings": [], "statistics": [], "segments": [], "claims_for_report": [],
                },
                input_tokens=estimate_tokens(prompt),
                output_tokens=1,
                used_fallback=True,
                fallback_reason="未配置豆包，跳过问卷分析。",
                tool_calls=[{"name": "mock_survey_analysis", "result": "skipped"}],
            )
        payload = self._chat_json(prompt, timeout=self.timeout_seconds + 15)
        if not isinstance(payload.get("summary", ""), str):
            raise LLMProviderError("doubao survey analysis schema validation failed")
        return LLMJSONResult(
            provider="doubao",
            data=payload,
            input_tokens=estimate_tokens(prompt),
            output_tokens=estimate_tokens(json.dumps(payload, ensure_ascii=False)),
            tool_calls=[{"name": "doubao_chat_completions", "result": "survey_analysis"}],
        )

    def design_interview_guide(
        self, task: dict[str, Any], research_objective: str,
        target_users: str = "", interview_count: int = 5,
    ) -> LLMJSONResult:
        prompt = build_interview_guide_prompt(task, research_objective, target_users, interview_count)
        if self.provider != "doubao":
            competitors = "、".join(task.get("competitors", [])) or "目标竞品"
            return LLMJSONResult(
                provider="mock",
                data={
                    "title": f"{competitors} 用户访谈提纲",
                    "estimated_duration_minutes": 45,
                    "target_profile": target_users or f"近 3 个月体验过 {competitors} 或同类产品的真实用户",
                    "phases": [
                        {
                            "phase": "热身",
                            "duration_minutes": 5,
                            "goals": ["确认背景", "建立访谈语境"],
                            "questions": [
                                {"id": "Q1", "text": "请先介绍一下你的角色、团队规模和主要工作场景。", "probe": "追问最近一次使用同类产品完成的任务。"},
                                {"id": "Q2", "text": f"你最早是因为什么需求开始关注或使用 {competitors}？", "probe": "追问触发点、替代方案和当时的选择标准。"},
                            ],
                        },
                        {
                            "phase": "核心探索",
                            "duration_minutes": 30,
                            "goals": ["理解使用路径", "比较竞品差异", "定位痛点和付费意愿"],
                            "questions": [
                                {"id": "Q3", "text": "请回忆一次典型使用流程，从开始到完成分别经历了哪些步骤？", "probe": "追问哪里最顺畅、哪里最容易卡住。"},
                                {"id": "Q4", "text": f"如果比较 {competitors}，你认为差异最大的功能或体验是什么？", "probe": "追问具体例子，而不是笼统评价。"},
                                {"id": "Q5", "text": "你对当前产品价格、套餐限制或使用门槛的感受是什么？", "probe": "追问是否影响购买、续费或推荐。"},
                                {"id": "Q6", "text": "过去一次不满意或放弃使用的经历是什么？", "probe": "追问当时造成的业务影响和临时解决办法。"},
                            ],
                        },
                        {
                            "phase": "总结",
                            "duration_minutes": 10,
                            "goals": ["确认判断", "收集开放建议"],
                            "questions": [
                                {"id": "Q7", "text": "如果向同事推荐或不推荐这些产品，你会怎么说？", "probe": "追问推荐对象和前提条件。"},
                                {"id": "Q8", "text": "还有哪些我们没有问到、但会影响你选择的因素？", "probe": "追问合规、安全、协作、迁移成本等隐性因素。"},
                            ],
                        },
                    ],
                    "notes_for_interviewer": f"建议访谈 {interview_count} 人以上；只记录脱敏原话，关键结论必须回链到问题编号和原文证据。",
                    "dimension_coverage": {
                        "用户画像": ["Q1", "Q2"],
                        "功能对比": ["Q3", "Q4"],
                        "定价": ["Q5"],
                        "用户评价": ["Q6", "Q7"],
                        "SWOT": ["Q4", "Q7", "Q8"],
                    },
                },
                input_tokens=estimate_tokens(prompt),
                output_tokens=estimate_tokens(research_objective) + 90,
                used_fallback=True,
                fallback_reason="未配置豆包，使用本地访谈提纲模板。",
                tool_calls=[{"name": "mock_interview_guide", "result": "local outline"}],
            )
        payload = self._chat_json(prompt, timeout=self.timeout_seconds + 10)
        if not isinstance(payload.get("phases"), list):
            raise LLMProviderError("doubao interview guide schema validation failed")
        return LLMJSONResult(
            provider="doubao",
            data=payload,
            input_tokens=estimate_tokens(prompt),
            output_tokens=estimate_tokens(json.dumps(payload, ensure_ascii=False)),
            tool_calls=[{"name": "doubao_chat_completions", "result": "interview_guide"}],
        )

    def extract_interview_insights(
        self, task: dict[str, Any], transcript_text: str, interviewee_profile: dict[str, str] | None = None,
    ) -> LLMJSONResult:
        profile = interviewee_profile or {}
        prompt = build_interview_extraction_prompt(task, transcript_text, profile)
        if self.provider != "doubao":
            return LLMJSONResult(
                provider="mock",
                data={
                    "summary": "未配置豆包，无法自动提取访谈发现。",
                    "interviewee_profile": profile,
                    "key_quotes": [], "scenarios": [], "pain_points": [], "needs": [], "claims_for_report": [],
                },
                input_tokens=estimate_tokens(prompt),
                output_tokens=1,
                used_fallback=True,
                fallback_reason="未配置豆包，跳过访谈提取。",
                tool_calls=[{"name": "mock_interview_extraction", "result": "skipped"}],
            )
        payload = self._chat_json(prompt, timeout=self.timeout_seconds + 20)
        if not isinstance(payload.get("summary", ""), str):
            raise LLMProviderError("doubao interview extraction schema validation failed")
        return LLMJSONResult(
            provider="doubao",
            data=payload,
            input_tokens=estimate_tokens(prompt),
            output_tokens=estimate_tokens(json.dumps(payload, ensure_ascii=False)),
            tool_calls=[{"name": "doubao_chat_completions", "result": "interview_extraction"}],
        )

    def _generate_with_mock(self, prompt: str, evidence: list[dict[str, Any]]) -> LLMResult:
        source_ids = list(dict.fromkeys(item["source_id"] for item in evidence if item.get("source_id")))
        if not source_ids:
            return LLMResult(
                provider="mock",
                claims=[],
                input_tokens=estimate_tokens(prompt),
                output_tokens=1,
                used_fallback=True,
                fallback_reason="没有可注入的 evidence，跳过模型生成。",
                tool_calls=[{"name": "mock_llm_generate_claims", "result": "no evidence"}],
            )
        claims = [
            {
                "section": "overview",
                "content": "模型分析草稿基于已入库证据生成，正式报告会保留来源追溯。",
                "confidence": 0.72,
                "source_ids": source_ids[:2],
                "needs_review": False,
                "status": "reportable",
                "uncertainty": "",
            }
        ]
        return LLMResult(
            provider="mock",
            claims=claims,
            input_tokens=estimate_tokens(prompt),
            output_tokens=estimate_tokens(json.dumps(claims, ensure_ascii=False)),
            tool_calls=[{"name": "mock_llm_generate_claims", "result": "1 claim"}],
        )

    def _chat_json(self, prompt: str, timeout: int) -> dict[str, Any]:
        if not self.api_key:
            raise LLMProviderError("DOUBAO_API_KEY is not configured")
        if not self.model_id:
            raise LLMProviderError("DOUBAO_ENDPOINT_ID is not configured")
        request_body = {
            "model": self.model_id,
            "messages": [
                {"role": "system", "content": "你只输出合法 JSON 对象，不输出 Markdown 或额外解释。"},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.2,
        }
        request = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(request_body).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                payload = json.loads(response.read().decode("utf-8", errors="replace"))
        except urllib.error.HTTPError as exc:
            raise LLMProviderError(f"doubao HTTP {exc.code}") from exc
        except (TimeoutError, socket.timeout) as exc:
            raise LLMProviderError("doubao timeout") from exc
        except urllib.error.URLError as exc:
            raise LLMProviderError(f"doubao network error: {exc.reason}") from exc
        except OSError as exc:
            raise LLMProviderError(f"doubao connection error: {exc.__class__.__name__}") from exc
        content = payload.get("choices", [{}])[0].get("message", {}).get("content", "")
        try:
            parsed = extract_json_payload(content)
        except json.JSONDecodeError as exc:
            raise LLMProviderError("doubao JSON parse failed") from exc
        if not isinstance(parsed, dict):
            raise LLMProviderError("doubao response must be a JSON object")
        return parsed

    def _generate_with_doubao(self, prompt: str, evidence: list[dict[str, Any]]) -> LLMResult:
        if not self.api_key:
            raise LLMProviderError("DOUBAO_API_KEY is not configured")
        if not self.model_id:
            raise LLMProviderError("DOUBAO_ENDPOINT_ID is not configured")

        request_body = {
            "model": self.model_id,
            "messages": [
                {"role": "system", "content": "你只输出合法 JSON 对象，不输出额外解释或 Markdown。"},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.2,
        }
        request = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(request_body).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                payload = json.loads(response.read().decode("utf-8", errors="replace"))
        except urllib.error.HTTPError as exc:
            raise LLMProviderError(f"doubao HTTP {exc.code}") from exc
        except (TimeoutError, socket.timeout) as exc:
            raise LLMProviderError("doubao timeout") from exc
        except urllib.error.URLError as exc:
            raise LLMProviderError(f"doubao network error: {exc.reason}") from exc
        except OSError as exc:
            raise LLMProviderError(f"doubao connection error: {exc.__class__.__name__}") from exc

        content = (
            payload.get("choices", [{}])[0]
            .get("message", {})
            .get("content", "")
        )
        try:
            raw_claims = extract_json_payload(content)
            if isinstance(raw_claims, dict):
                raw_claims = raw_claims.get("claims", [])
            claims = self._validate_claims(raw_claims, evidence)
        except (json.JSONDecodeError, TypeError, ValidationError, ValueError) as exc:
            raise LLMProviderError(f"doubao schema validation failed: {exc.__class__.__name__}") from exc

        usage = payload.get("usage", {}) if isinstance(payload, dict) else {}
        output_text = json.dumps(claims, ensure_ascii=False)
        return LLMResult(
            provider="doubao",
            claims=claims,
            input_tokens=int(usage.get("prompt_tokens") or estimate_tokens(prompt)),
            output_tokens=int(usage.get("completion_tokens") or estimate_tokens(output_text)),
            tool_calls=[{"name": "doubao_chat_completions", "result": f"{len(claims)} claims"}],
        )

    def _validate_claims(self, raw_claims: Any, evidence: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not isinstance(raw_claims, list):
            raise ValueError("claims payload must be a list")
        allowed_sources = {item["source_id"] for item in evidence if item.get("source_id")}
        validated: list[dict[str, Any]] = []
        for item in raw_claims[:18]:
            draft = LLMClaimDraft.model_validate(item)
            source_ids = [source_id for source_id in draft.source_ids if source_id in allowed_sources]
            if not source_ids:
                raise ValueError("claim source_ids do not match injected evidence")
            payload = draft.model_dump()
            payload["source_ids"] = source_ids
            validated.append(payload)
        return validated
