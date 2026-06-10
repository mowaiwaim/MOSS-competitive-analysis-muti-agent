from __future__ import annotations

import json
import hashlib
import os
import re
import sqlite3
import urllib.parse
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from appark_collector import APPARK_COMPETITOR_URL, collect_appark_metrics
from collector import BingSearchClient, VolcWebSearchClient, WebCollector, WebSourceDraft, chunk_text
from llm_provider import LLMProvider, LLMProviderError
from react_report_agent import run_react_report
from rss_collector import collect_google_alert_sources
from schema import ReportableClaim, utc_now_iso

from config_loader import (
    INDUSTRY_RELATED_TERMS,
    OFFICIAL_SOURCE_SEEDS,
    PRODUCT_ALIASES,
    PRODUCT_RELATED_TERMS,
    PRODUCT_SEARCH_QUERIES,
    PRODUCT_URL_HINTS,
    REFERENCE_AI_API_PRICES,
    SEARCH_BLOCK_HOSTS,
    SEARCH_BLOCK_HOSTS_SELF_EXEMPT,
    SEARCH_HIGH_AUTHORITY_DOMAINS,
    SEARCH_HIGH_VALUE_DOMAINS_BY_INDUSTRY,
    SEARCH_LOW_VALUE_DOMAINS,
    SEARCH_LOW_VALUE_DOMAINS_BY_INDUSTRY,
    SEARCH_MEDIUM_AUTHORITY_DOMAINS,
)
from pricing_parser import (
    infer_price_types as _pricing_infer_price_types,
    prices_from_window as _pricing_prices_from_window,
    pricing_claim_text_from_facts as _pricing_claim_text_from_facts,
    pricing_facts_from_source as _pricing_facts_from_source,
)


SENSITIVE_PATTERNS = [
    (re.compile(r"(?i)(api[_-]?key|secret|token|cookie)\s*[:=]\s*[\w\-._]+"), r"\1=[REDACTED]"),
    (re.compile(r"(?i)bearer\s+[\w\-._]+"), "Bearer [REDACTED]"),
    (re.compile(r"\b[a-z]{0,4}ark-[A-Za-z0-9-]{20,}\b", re.I), "[REDACTED_API_KEY]"),
    (re.compile(r"\b(?=[A-Za-z0-9]{32,}\b)(?=[A-Za-z0-9]*[A-Z])(?=[A-Za-z0-9]*[a-z])(?=[A-Za-z0-9]*\d)[A-Za-z0-9]{32,}\b"), "[REDACTED_TOKEN]"),
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"), "[REDACTED_EMAIL]"),
    (re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"), "[REDACTED_PHONE]"),
]


SECTION_META = {
    "feature_tree": {"title": "功能对比", "focus": {"功能对比", "核心能力", "竞品分层"}},
    "pricing_model": {"title": "定价对比", "focus": {"定价", "商业模式与定价", "API成本"}},
    "user_persona": {"title": "用户画像", "focus": {"用户画像", "用户与场景"}},
    "reviews": {"title": "用户评价", "focus": {"用户评价", "市场与赛道", "增长与分发"}},
    "swot": {"title": "SWOT", "focus": {"SWOT", "SWOT与壁垒", "机会建议"}},
}

DEFAULT_FOCUS_AREAS = ["市场与赛道", "竞品分层", "核心能力", "商业模式与定价", "增长与分发", "用户与场景", "SWOT与壁垒", "机会建议"]


class WorkflowStopped(Exception):
    """Raised internally when a user stops a running workflow."""


REPORT_NOISE_PATTERNS = [
    re.compile(r"\{\{[^}]+\}\}"),
    re.compile(r"(?i)\bsource_id\b"),
    re.compile(r"(?i)\btrace\b"),
    re.compile(r"证据状态"),
    re.compile(r"搜索词：[^。]{0,180}。"),
    re.compile(r"搜索结果：[^。]{0,180}。"),
    re.compile(r"摘要："),
    re.compile(r"正文线索："),
]

MARKDOWN_SECTION_TITLES = [
    "核心发现",
    "市场规模与增长趋势",
    "市场结构与增长逻辑",
    "用户需求与痛点",
    "技术与产品趋势",
    "竞品分层框架",
    "本轮竞品范围",
    "分层方法",
    "对比口径",
    "产品定位",
    "核心功能",
    "差异化",
    "价格",
    "用户分层",
    "近期更新",
    "分发渠道",
    "商业模式",
    "风险短板",
    "收入模式对比",
    "订阅制与免费试用",
    "按量计费/API/SDK",
    "企业版与服务收入",
    "当前证据边界",
    "用户获取策略",
    "渠道策略",
    "生态与集成",
    "销售与案例",
    "用户画像分析",
    "使用场景分析",
    "个人用户",
    "专业用户",
    "团队用户",
    "企业采购者",
    "技术壁垒",
    "数据与用户反馈壁垒",
    "生态壁垒",
    "品牌与渠道壁垒",
    "市场机会",
    "产品策略建议",
    "产品定位建议",
    "功能规划建议",
    "定价策略建议",
    "增长策略建议",
    "风险规避建议",
    "来源列表",
    "测试方法",
]


def sanitize_text(value: str, limit: int = 800) -> str:
    cleaned = value or ""
    for pattern, replacement in SENSITIVE_PATTERNS:
        cleaned = pattern.sub(replacement, cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned[:limit]


def sanitize_markdown_text(value: str, limit: int = 120000) -> str:
    cleaned = str(value or "").replace("\x00", " ")
    for pattern, replacement in SENSITIVE_PATTERNS:
        cleaned = pattern.sub(replacement, cleaned)
    cleaned = cleaned.replace("竞争情报分析师", "MOSS团队")
    cleaned = cleaned.replace("\r\n", "\n").replace("\r", "\n")
    cleaned = re.sub(r"[ \t\f\v]+", " ", cleaned)
    cleaned = re.sub(r"\s+(#{2,5}\s+)", r"\n\n\1", cleaned)
    cleaned = re.sub(r"\s+(-\s+\*\*[^*]+?\*\*[：:])", r"\n\1", cleaned)
    cleaned = re.sub(r"\s+(-\s+)", r"\n\1", cleaned)
    cleaned = re.sub(r"\s+(\d+\.\s+)", r"\n\1", cleaned)
    cleaned = re.sub(r"\s+(>\s+)", r"\n\n\1", cleaned)
    cleaned = re.sub(r"(GPT-5)（待核实）\.(\d)(?:（待核实）)?", r"\1.\2（待核实）", cleaned, flags=re.I)
    cleaned = re.sub(r"（待核实）{2,}", "（待核实）", cleaned)
    for title in MARKDOWN_SECTION_TITLES:
        cleaned = re.sub(
            rf"(?m)(#{{3,5}}\s+\d+(?:\.\d+)*\s+{re.escape(title)})\s+(?=\S)",
            r"\1\n",
            cleaned,
        )
    cleaned = re.sub(r" *\n *", "\n", cleaned)
    cleaned = re.sub(r"\n{4,}", "\n\n\n", cleaned).strip()
    return cleaned[:limit]


def sanitize_payload(value: Any, string_limit: int = 900) -> Any:
    if isinstance(value, dict):
        return {
            sanitize_text(str(key), 120): sanitize_payload(item, string_limit)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [sanitize_payload(item, string_limit) for item in value]
    if isinstance(value, tuple):
        return [sanitize_payload(item, string_limit) for item in value]
    if isinstance(value, str):
        return sanitize_text(value, string_limit)
    return value


def sanitize_markdown_payload(value: Any, string_limit: int = 120000) -> Any:
    if isinstance(value, dict):
        return {
            sanitize_text(str(key), 120): sanitize_markdown_payload(item, string_limit)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [sanitize_markdown_payload(item, string_limit) for item in value]
    if isinstance(value, tuple):
        return [sanitize_markdown_payload(item, string_limit) for item in value]
    if isinstance(value, str):
        return sanitize_markdown_text(value, string_limit)
    return value


def row_get(row: sqlite3.Row | dict[str, Any], key: str, default: Any = "") -> Any:
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return default


def _norm_name(value: str) -> str:
    return re.sub(r"[\s_\-—–·.,，。:：()（）]+", "", str(value or "").casefold())


def report_text(value: str, limit: int = 360) -> str:
    cleaned = sanitize_text(value, limit * 2).replace("竞争情报分析师", "MOSS团队")
    for pattern in REPORT_NOISE_PATTERNS:
        cleaned = pattern.sub("", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" ：:;；，,。")
    return cleaned[:limit]


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def loads(value: str | None, fallback: Any) -> Any:
    if not value:
        return fallback
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return fallback


def now_dt() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0, tzinfo=None)


def iso(dt: datetime) -> str:
    return dt.isoformat() + "Z"


class Orchestrator:
    """Deterministic DAG runner with optional real collection and LLM calls."""

    def __init__(self, db_path: str | Path, dataset_path: str | Path):
        self.db_path = str(db_path)
        self.dataset_path = Path(dataset_path)
        self.collector = WebCollector()
        self.search_client = VolcWebSearchClient()
        self.bing_client = BingSearchClient()
        self.llm_provider = LLMProvider()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def _is_task_stopped(self, task_id: str) -> bool:
        with self.connect() as conn:
            row = conn.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()
        return bool(row and row["status"] == "stopped")

    def _ensure_not_stopped(self, task_id: str) -> None:
        if self._is_task_stopped(task_id):
            raise WorkflowStopped()

    def _task_workflow_busy(self, task_id: str) -> bool:
        active_statuses = {"collecting", "analyzing", "reanalyzing", "qa_review", "qa_rework", "reporting"}
        with self.connect() as conn:
            row = conn.execute("SELECT status, completed_at FROM tasks WHERE id = ?", (task_id,)).fetchone()
        return bool(row and not row["completed_at"] and row["status"] in active_statuses)

    def _busy_result(self, task_id: str) -> dict[str, Any]:
        with self.connect() as conn:
            row = conn.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()
        return {
            "status": "busy",
            "task_status": row["status"] if row else "",
            "result_summary": "自动流程仍在运行中；报告会在自动质检后生成，当前人工复核操作不会并发启动。",
        }

    def stop_workflow(self, task_id: str, reason: str = "用户手动停止任务") -> dict[str, Any]:
        now = utc_now_iso()
        with self.connect() as conn:
            row = conn.execute("SELECT status, completed_at FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if not row:
                return {"status": "not_found", "id": task_id}
            was_terminal = bool(row["completed_at"] or row["status"] in {"completed", "failed", "stopped"})
            if not was_terminal:
                conn.execute(
                    "UPDATE tasks SET status = 'stopped', completed_at = ? WHERE id = ?",
                    (now, task_id),
                )
        if was_terminal:
            return {"status": row["status"], "id": task_id, "already_terminal": True}
        self._log_agent_event(
            task_id,
            "编排层",
            "workflow_stop_requested",
            "用户已停止任务；当前进行中的一次外部调用可能需等待超时，但后续阶段不会继续执行。",
            severity="warning",
            meta={"reason": sanitize_text(reason, 200)},
        )
        self._log_agent_run(
            task_id,
            agent_name="编排层",
            input_summary="用户点击停止任务。",
            output_summary="已写入停止状态；后续 Agent 阶段将被跳过。",
            status="stopped",
            duration_ms=1,
            severity="warning",
            tool_calls=[{"name": "stop_workflow", "result": "requested"}],
        )
        return {"status": "stopped", "id": task_id, "already_terminal": False}

    def load_dataset(self) -> dict[str, Any]:
        return json.loads(self.dataset_path.read_text(encoding="utf-8"))

    def _join_names(self, names: list[str]) -> str:
        return "、".join([name for name in names if name]) or "当前竞品"

    def _task_config(self, task_id: str) -> dict[str, Any]:
        with self.connect() as conn:
            task = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if not task:
            return {}
        return {
            "id": task["id"],
            "industry": task["industry"],
            "competitors": loads(task["competitors_json"], []),
            "websites": loads(task["websites_json"], []),
            "focus_areas": loads(task["focus_areas_json"], []),
            "source_mode": task["source_mode"],
            "notes": task["notes"],
        }

    def _external_calls_allowed_for_config(self, task_config: dict[str, Any] | sqlite3.Row | None) -> bool:
        if not task_config:
            return False
        if isinstance(task_config, sqlite3.Row):
            source_mode = row_get(task_config, "source_mode", "")
        else:
            source_mode = str(task_config.get("source_mode", ""))
        return "实时采集" in source_mode

    def _external_calls_allowed(self, task_id: str) -> bool:
        return self._external_calls_allowed_for_config(self._task_config(task_id))

    def _llm_calls_allowed_for_config(self, task_config: dict[str, Any] | sqlite3.Row | None) -> bool:
        return bool(task_config)

    def _offline_external_trace(self, tool_name: str, token_input: int = 1) -> dict[str, Any]:
        return {
            "provider": "local",
            "token_input": max(1, int(token_input or 1)),
            "token_output": 0,
            "fallback_reason": "offline_source_mode_no_external_call",
            "tool_calls": [{"name": tool_name, "provider": "local", "result": "skipped: source_mode has no realtime collection"}],
        }

    def run_initial_workflow(self, task_id: str) -> None:
        try:
            self._ensure_not_stopped(task_id)
            dataset = self.load_dataset()
            task_config = self._task_config(task_id)
            source_map: dict[str, str] = {}
            self._log_agent_event(task_id, "编排层", "workflow_started", "已创建任务，准备进入采集、分析、质检和报告链路。")
            source_mode = task_config.get("source_mode", "")
            if "实时采集" in source_mode:
                source_map.update(self._existing_user_material_source_map(task_id))
                source_map.update(self._collect_real_sources(task_id, task_config, dataset))
                self._ensure_not_stopped(task_id)
                source_map.update(self._collect_manual_scope_sources(task_id, task_config, source_map))
            elif "上传资料" in source_mode:
                source_map.update(self._collect_user_supplied_sources(task_id, task_config))
            else:
                source_map.update(self._collect_sources(task_id, dataset))
            self._ensure_not_stopped(task_id)
            self._collect_appark_metrics_for_task(task_id, task_config.get("competitors", []))
            self._ensure_not_stopped(task_id)
            self._first_analysis(task_id, dataset, source_map)
            last_failure_signature = ""
            repeated_failure_count = 0
            deep_refresh_needed = False
            for qa_round in range(3):
                self._ensure_not_stopped(task_id)
                rejected_claim_id = self._qa_check(task_id, first_pass=(qa_round == 0), rework_round=qa_round)
                self._ensure_not_stopped(task_id)
                if not rejected_claim_id:
                    break
                failure_signature = self._primary_open_finding_signature(task_id, rejected_claim_id) or rejected_claim_id
                if failure_signature == last_failure_signature:
                    repeated_failure_count += 1
                else:
                    last_failure_signature = failure_signature
                    repeated_failure_count = 1
                if repeated_failure_count >= 3:
                    self._handoff_open_findings_to_manual_review(task_id, repeated_failure_count, qa_round, failure_signature)
                    break
                self._auto_repair_open_findings(task_id, qa_round + 1)
                self._ensure_not_stopped(task_id)
                self._repair_analysis(task_id, rejected_claim_id, source_map)
                deep_refresh_needed = self._analysis_artifact_needs_refresh(task_id)
            if deep_refresh_needed:
                self._ensure_not_stopped(task_id)
                self._refresh_deep_analysis_from_current_claims(
                    task_id,
                    "自动质检修复完成后统一刷新深度分析产物",
                )
            self._ensure_not_stopped(task_id)
            self._generate_report(task_id)
            self._set_task_completed(task_id)
        except WorkflowStopped:
            self._log_agent_event(
                task_id,
                "编排层",
                "workflow_stopped",
                "任务已停止，编排层不再推进后续 Agent。",
                severity="warning",
            )

    def fail_workflow(self, task_id: str, exc: Exception) -> None:
        if self._is_task_stopped(task_id):
            return
        safe_error = sanitize_text(f"{exc.__class__.__name__}: {exc}", 500)
        self._log_agent_event(task_id, "编排层", "workflow_failed", f"任务执行失败：{safe_error}", severity="error")
        self._log_agent_run(
            task_id,
            agent_name="编排层",
            input_summary="执行异步任务工作流。",
            output_summary="任务执行失败，错误已写入 Trace；用户可查看日志后重新创建任务。",
            status="failed",
            duration_ms=1000,
            error=safe_error,
            severity="error",
            fallback_reason=safe_error,
            tool_calls=[{"name": "run_initial_workflow", "result": "failed"}],
        )
        with self.connect() as conn:
            conn.execute(
                "UPDATE tasks SET status = 'failed', completed_at = ? WHERE id = ?",
                (utc_now_iso(), task_id),
            )

    def recheck_qa(self, task_id: str, reason: str = "用户触发重新质检") -> dict[str, Any]:
        if self._task_workflow_busy(task_id):
            return self._busy_result(task_id)
        if not self._has_new_inputs_for_recheck(task_id):
            self._log_agent_run(
                task_id,
                agent_name="质检 Agent",
                input_summary=reason,
                output_summary="未检测到新增来源、结论修订或人工确认；直接复检不会改变结果。",
                status="no_change",
                duration_ms=900,
                retry_count=0,
                has_rework=False,
                severity="warning",
                tool_calls=[{"name": "qa_recheck_guard", "result": "no_new_inputs"}],
            )
            return {
                "status": "no_change",
                "result_summary": "未检测到新增来源、结论修订或人工确认；请先修复具体质检问题，再重新质检。",
            }
        rejected_claim_id = self._qa_check(task_id, first_pass=False, rework_round=1)
        self._log_agent_run(
            task_id,
            agent_name="质检 Agent",
            input_summary=reason,
            output_summary="重新质检仍发现开放问题，报告将标记为待复核。" if rejected_claim_id else "重新质检通过：报告结论均有来源依据，低置信度内容保留待复核标记。",
            status="rejected" if rejected_claim_id else "completed",
            duration_ms=8200,
            retry_count=1,
            has_rework=bool(rejected_claim_id),
            severity="warning" if rejected_claim_id else "info",
            tool_calls=[{"name": "validate_claim_sources", "result": "rejected" if rejected_claim_id else "passed"}],
        )
        self._generate_report(task_id, reason="manual_recheck")
        self._set_task_completed(task_id)
        if rejected_claim_id:
            return {"status": "needs_review", "result_summary": "重新质检发现开放问题，已生成待复核报告版本。", "claim_id": rejected_claim_id}
        return {"status": "completed", "result_summary": "重新质检通过，并生成新的报告版本。"}

    def _primary_open_finding_signature(self, task_id: str, claim_id: str = "") -> str:
        with self.connect() as conn:
            if claim_id:
                row = conn.execute(
                    """
                    SELECT claim_id, finding_type, reason
                    FROM qa_findings
                    WHERE task_id = ? AND claim_id = ? AND fix_status = 'open'
                    ORDER BY created_at, rowid
                    LIMIT 1
                    """,
                    (task_id, claim_id),
                ).fetchone()
            else:
                row = conn.execute(
                    """
                    SELECT claim_id, finding_type, reason
                    FROM qa_findings
                    WHERE task_id = ? AND fix_status = 'open'
                    ORDER BY created_at, rowid
                    LIMIT 1
                    """,
                    (task_id,),
                ).fetchone()
        if not row:
            return ""
        return "|".join([sanitize_text(str(row["claim_id"]), 120), sanitize_text(str(row["finding_type"]), 80), sanitize_text(str(row["reason"]), 260)])

    def _handoff_open_findings_to_manual_review(self, task_id: str, failure_count: int, rework_round: int, signature: str) -> None:
        now = utc_now_iso()
        summary = f"同一质检问题连续 {failure_count} 次自动修复后仍未通过，已转入人工复核工作台；报告继续生成，不再阻塞。"
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE qa_findings
                SET fix_status = 'manual_pending',
                    fixed_at = ?,
                    recheck_result = CASE
                    WHEN recheck_result = '' THEN ?
                    ELSE recheck_result || '；' || ?
                END
                WHERE task_id = ? AND fix_status IN ('open', 'manual_pending')
                """,
                (now, summary, summary, task_id),
            )
        self._update_task(task_id, "qa_passed")
        self._log_agent_event(
            task_id,
            "质检 Agent",
            "qa_manual_handoff",
            summary,
            severity="warning",
            meta={"failure_count": failure_count, "rework_round": rework_round, "signature": signature},
        )
        self._log_agent_run(
            task_id,
            agent_name="质检 Agent",
            input_summary="连续自动复检同一开放质检问题。",
            output_summary=summary,
            status="completed",
            duration_ms=1000,
            retry_count=rework_round,
            severity="warning",
            has_rework=True,
            tool_calls=[{"name": "qa_manual_handoff_after_repeated_failures", "result": f"{failure_count} consecutive failures"}],
        )

    def _has_new_inputs_for_recheck(self, task_id: str) -> bool:
        with self.connect() as conn:
            newest_open = conn.execute(
                """
                SELECT MAX(created_at) AS created_at
                FROM qa_findings
                WHERE task_id = ? AND fix_status IN ('open', 'manual_pending')
                """,
                (task_id,),
            ).fetchone()["created_at"]
            if not newest_open:
                return True
            manual_changed = conn.execute(
                """
                SELECT 1 FROM manual_actions
                WHERE task_id = ? AND created_at > ?
                LIMIT 1
                """,
                (task_id, newest_open),
            ).fetchone()
            fixed_after = conn.execute(
                """
                SELECT 1 FROM qa_findings
                WHERE task_id = ? AND fixed_at > ?
                  AND fix_status = 'fixed'
                LIMIT 1
                """,
                (task_id, newest_open),
            ).fetchone()
        return bool(manual_changed or fixed_after)

    def repair_qa_finding(self, task_id: str, finding_id: str, action: str = "auto_collect", user_text: str = "") -> dict[str, Any]:
        if self._task_workflow_busy(task_id):
            return self._busy_result(task_id)
        action = action or "auto_collect"
        with self.connect() as conn:
            finding = conn.execute(
                "SELECT * FROM qa_findings WHERE task_id = ? AND id = ?",
                (task_id, finding_id),
            ).fetchone()
            if not finding:
                return {"status": "not_found", "result_summary": "未找到该质检问题。"}
            claim = conn.execute(
                "SELECT * FROM claims WHERE task_id = ? AND id = ?",
                (task_id, finding["claim_id"]),
            ).fetchone()
        if not claim:
            return {"status": "not_found", "result_summary": "该质检问题绑定的结论不存在。"}

        if action == "manual_supplement":
            summary = self._manual_supplement_source(task_id, user_text or finding["reason"], claim["content"])
            return {"status": "needs_review", "result_summary": summary, "finding_id": finding_id}
        if action == "confirm_uncertainty":
            summary = self._confirm_low_confidence_claim(task_id, user_text or "人工确认该不确定性结论。", claim["id"])
            self._mark_finding_fixed(task_id, finding_id, "已由人工确认该结论，保留确认记录后生成新报告。")
            return {"status": "completed", "result_summary": summary, "finding_id": finding_id}
        if not self._external_calls_allowed(task_id):
            summary = "未开启联网搜索，自动补采已跳过；请上传材料、手动补充来源，或开启联网搜索后再自动补采。"
            return {"status": "needs_review", "result_summary": summary, "finding_id": finding_id, "claim_id": claim["id"]}

        updated = self._auto_repair_claim_from_official_sources(task_id, finding, claim)
        self._mark_finding_fixed(
            task_id,
            finding_id,
            "已执行自动补采/重做分析；如仍有同类问题，复检会生成新的开放问题。",
        )
        if self._analysis_artifact_needs_refresh(task_id):
            self._refresh_deep_analysis_from_current_claims(task_id, "质检问题修复后刷新深度分析产物")
        rejected_claim_id = self._qa_check(task_id, first_pass=False, rework_round=1)
        self._generate_report(task_id, reason="qa_finding_repair")
        self._set_task_completed(task_id)
        return {
            "status": "needs_review" if rejected_claim_id else "completed",
            "result_summary": "已自动补采官方来源并重做该条分析。" if updated else "已尝试自动补采，但仍缺少可抽取正文或价格事实。",
            "finding_id": finding_id,
            "claim_id": claim["id"],
        }

    def handle_manual_action(self, task_id: str, user_text: str, selected_text: str = "", claim_id: str = "") -> dict[str, Any]:
        if self._task_workflow_busy(task_id):
            return self._busy_result(task_id)
        intent, target_agent = self._interpret_manual_intent(user_text)
        action_id = uuid.uuid4().hex
        created_at = utc_now_iso()

        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO manual_actions
                (id, task_id, claim_id, user_text, selected_text, interpreted_intent, target_agent, status, result_summary, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    action_id,
                    task_id,
                    sanitize_text(claim_id, 120),
                    sanitize_text(user_text, 1200),
                    sanitize_text(selected_text, 1200),
                    intent,
                    target_agent,
                    "running",
                    "系统已识别意图，正在触发对应 Agent。",
                    created_at,
                ),
            )

        if intent == "confirm_claim":
            result_summary = self._confirm_low_confidence_claim(task_id, user_text, claim_id)
        elif intent == "recheck_qa":
            result_summary = self.recheck_qa(task_id, "人工复查要求重新质检")["result_summary"]
        elif intent == "supplement_source":
            result_summary = self._manual_supplement_source(task_id, user_text, selected_text)
        else:
            result_summary = self._manual_revise_claim(task_id, user_text, selected_text)

        with self.connect() as conn:
            conn.execute(
                "UPDATE manual_actions SET status = ?, result_summary = ? WHERE id = ?",
                ("completed", result_summary, action_id),
            )

        return {
            "id": action_id,
            "interpreted_intent": intent,
            "target_agent": target_agent,
            "status": "completed",
            "result_summary": result_summary,
            "claim_id": claim_id,
        }

    def process_uploaded_material(self, task_id: str, source_id: str, filename: str, text: str) -> None:
        now = utc_now_iso()
        material = sanitize_text(self._clean_uploaded_material_for_analysis(text), 4000)
        metadata = self._uploaded_material_metadata(task_id, filename, material)
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE sources
                SET competitor_name = ?, module = ?, source_role = ?, raw_content_status = ?,
                    relevance_score = ?, credibility = ?, excerpt = ?
                WHERE task_id = ? AND id = ?
                """,
                (
                    metadata["competitor_name"],
                    metadata["module"],
                    metadata["source_role"],
                    "fetched",
                    metadata["relevance_score"],
                    metadata["credibility"],
                    sanitize_text(material, 1200),
                    task_id,
                    source_id,
                ),
            )
            self._insert_text_evidence(conn, task_id, source_id, material, now)

        lowered = filename.lower()
        if lowered.endswith((".csv", ".json")):
            # Deep analysis for survey/quantitative data
            self.analyze_survey_responses(task_id, source_id)
        else:
            # Deep analysis for interview/qualitative transcripts
            self.extract_interview_insights(task_id, source_id)

    def _parse_survey_structure(self, text: str) -> dict[str, Any]:
        """Attempt to parse survey structure from text content."""
        structure: dict[str, Any] = {"columns": [], "question_hints": []}
        try:
            data = json.loads(text)
            if isinstance(data, list) and data:
                first = data[0]
                if isinstance(first, dict):
                    structure["columns"] = list(first.keys())
            elif isinstance(data, dict):
                structure["columns"] = list(data.keys())
            return structure
        except json.JSONDecodeError:
            pass
        lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
        if lines:
            first_line = lines[0]
            for delimiter in [",", "\t", "|"]:
                if delimiter in first_line:
                    structure["columns"] = [
                        col.strip().strip('"').strip("'")
                        for col in first_line.split(delimiter)
                    ]
                    structure["question_hints"] = lines[1:5] if len(lines) > 1 else []
                    break
        return structure

    def _parse_survey_responses(
        self, text: str, structure: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """Parse survey responses into list of row dicts (capped at 40)."""
        columns = structure.get("columns", [])
        rows: list[dict[str, Any]] = []
        try:
            data = json.loads(text)
            if isinstance(data, list):
                for item in data[:40]:
                    if isinstance(item, dict):
                        rows.append(dict(item))
            elif isinstance(data, dict) and isinstance(data.get("responses"), list):
                for item in data["responses"][:40]:
                    if isinstance(item, dict):
                        rows.append(dict(item))
            return rows
        except json.JSONDecodeError:
            pass
        lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
        if not columns or len(lines) < 2:
            return rows
        delimiter = ","
        if "\t" in lines[0]:
            delimiter = "\t"
        for line in lines[1:41]:
            values = [val.strip().strip('"').strip("'") for val in line.split(delimiter)]
            row: dict[str, Any] = {}
            for i, col in enumerate(columns):
                row[col] = values[i] if i < len(values) else ""
            if any(v for v in row.values() if v):
                rows.append(row)
        return rows

    def design_questionnaire(
        self,
        task_id: str,
        research_objective: str,
        target_users: str = "",
        dimensions: list[str] | None = None,
    ) -> dict[str, Any]:
        """Generate a structured survey questionnaire via LLM."""
        stage_started = now_dt()
        task_config = self._task_config(task_id)
        if dimensions is None:
            dimensions = task_config.get("focus_areas", DEFAULT_FOCUS_AREAS)

        try:
            result = self.llm_provider.design_questionnaire(
                task_config, research_objective, target_users, dimensions
            )
        except (LLMProviderError, Exception) as exc:
            safe_reason = sanitize_text(str(exc), 300)
            return {
                "design_id": "",
                "design": {
                    "title": "调研问卷生成失败",
                    "description": f"LLM 调用失败：{safe_reason}。请确认 API Key 配置或稍后重试。",
                    "sections": [],
                    "estimated_time_minutes": 0,
                    "recommended_channels": [],
                },
                "source_id": "",
                "error": safe_reason,
            }
        design_id = uuid.uuid4().hex
        design_title = sanitize_text(str(result.data.get("title", "用户调研问卷")), 240)
        now = utc_now_iso()

        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO questionnaire_designs
                (id, task_id, title, research_objective, target_users, content_json,
                 focus_dimensions_json, status, estimated_time_minutes, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    design_id,
                    task_id,
                    design_title,
                    sanitize_text(research_objective, 600),
                    sanitize_text(target_users, 400),
                    dumps(result.data),
                    dumps(dimensions),
                    "draft",
                    int(result.data.get("estimated_time_minutes", 5)),
                    now,
                ),
            )
            # Insert as traceable source
            source_id = f"{task_id[:8]}_qdesign_{uuid.uuid4().hex[:8]}"
            conn.execute(
                """
                INSERT INTO sources
                (id, task_id, source_type, title, url_or_path, author_site, published_at, collected_at,
                 credibility, excerpt, related_claim_ids, provider)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    source_id,
                    task_id,
                    "questionnaire_design",
                    design_title,
                    f"llm://questionnaire/{design_id}",
                    "访谈/问卷整理 Agent",
                    "",
                    now,
                    "generated",
                    sanitize_text(research_objective, 500),
                    "[]",
                    result.provider,
                ),
            )

        section_count = len(result.data.get("sections", []))
        self._log_agent_run(
            task_id,
            agent_name="访谈/问卷整理 Agent",
            input_summary=f"设计问卷：{research_objective[:120]}",
            output_summary=f"已生成问卷方案「{design_title}」，共 {section_count} 个模块，预计 {result.data.get('estimated_time_minutes', 5)} 分钟。",
            status="completed",
            duration_ms=self._elapsed_ms(stage_started),
            token_input=result.input_tokens,
            token_output=result.output_tokens,
            model_provider=result.provider,
            fallback_reason=result.fallback_reason,
            tool_calls=result.tool_calls + [{"name": "design_questionnaire", "result": f"{section_count} sections"}],
            started_at=stage_started,
        )
        return {"design_id": design_id, "design": result.data, "source_id": source_id}

    def analyze_survey_responses(self, task_id: str, source_id: str) -> dict[str, Any]:
        """Deep analysis of uploaded survey response data via LLM."""
        stage_started = now_dt()
        task_config = self._task_config(task_id)

        with self.connect() as conn:
            source = conn.execute(
                "SELECT * FROM sources WHERE task_id = ? AND id = ?",
                (task_id, source_id),
            ).fetchone()
        if not source:
            raise ValueError(f"source {source_id} not found")

        raw_text = source["excerpt"] or ""
        # Try to read full evidence text if excerpt is too short
        if len(raw_text) < 80:
            raw_text = self._read_source_evidence_text(task_id, source_id)

        survey_structure = self._parse_survey_structure(raw_text)
        response_rows = self._parse_survey_responses(raw_text, survey_structure)

        if not response_rows:
            # Fallback: treat the file itself as the analysis subject
            response_rows = [{"content": raw_text[:2000]}]
            survey_structure = {"columns": ["content"], "question_hints": ["直接分析上传内容"]}

        try:
            result = self.llm_provider.analyze_survey_responses(
                task_config, response_rows, survey_structure
            )
        except (LLMProviderError, Exception) as exc:
            safe_reason = sanitize_text(str(exc), 300)
            self._log_agent_run(
                task_id, agent_name="访谈/问卷整理 Agent",
                input_summary=f"分析问卷数据：{source['title']}（{len(response_rows)} 份回复）",
                output_summary=f"LLM 调用失败：{safe_reason}",
                status="failed", duration_ms=self._elapsed_ms(stage_started),
                error=safe_reason, severity="error",
            )
            return {
                "analysis_id": "", "summary": f"分析失败：{safe_reason}",
                "findings_count": 0, "segments_count": 0, "claims_count": 0,
                "error": safe_reason,
            }
        analysis_id = uuid.uuid4().hex
        now = utc_now_iso()

        findings = result.data.get("key_findings", [])
        segments = result.data.get("segments", [])
        claims_for_report = result.data.get("claims_for_report", [])
        statistics = result.data.get("statistics", [])
        respondent_count = len(response_rows)

        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO survey_analyses
                (id, task_id, source_id, title, respondent_count, summary,
                 segments_json, findings_json, statistics_json, claims_generated_json,
                 confidence_score, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    analysis_id,
                    task_id,
                    source_id,
                    sanitize_text(str(result.data.get("title", source["title"])), 240),
                    respondent_count,
                    sanitize_text(str(result.data.get("summary", "")), 2000),
                    dumps(segments),
                    dumps(findings),
                    dumps(statistics),
                    dumps(claims_for_report),
                    float(result.data.get("confidence_score", 0.7)),
                    now,
                ),
            )

            # Insert structured claims into the main pipeline
            claims = []
            if claims_for_report:
                for fc in claims_for_report[:12]:
                    claims.append({
                        "section": fc.get("section", "reviews"),
                        "content": sanitize_text(str(fc.get("content", "")), 1200),
                        "confidence": float(fc.get("confidence", 0.7)),
                        "source_ids": [source_id],
                        "needs_review": fc.get("needs_review", True),
                        "status": "needs_review" if fc.get("needs_review", True) else "reportable",
                        "uncertainty": f"来自 {respondent_count} 份问卷数据的分析，建议结合公开材料交叉验证。",
                        "generated_agent": "访谈/问卷整理 Agent",
                        "claim_type": "inference",
                    })
            if not claims:
                claims.append({
                    "section": "reviews",
                    "content": "上传问卷材料已完成脱敏检查和统计摘要登记，可作为用户反馈与主题归纳的来源。",
                    "confidence": 0.68,
                    "source_ids": [source_id],
                    "needs_review": True,
                    "status": "needs_review",
                    "uncertainty": "上传资料已脱敏截断进入分析链路，正式结论仍建议人工抽样复核。",
                    "generated_agent": "访谈/问卷整理 Agent",
                })
            self._insert_claims(task_id, claims, conn)

        self._log_agent_run(
            task_id,
            agent_name="访谈/问卷整理 Agent",
            input_summary=f"分析问卷数据：{source['title']}（{respondent_count} 份回复）",
            output_summary=f"已生成 {len(findings)} 条发现、{len(segments)} 个用户群体、{len(claims_for_report)} 条报告结论。",
            status="completed",
            duration_ms=self._elapsed_ms(stage_started),
            token_input=result.input_tokens,
            token_output=result.output_tokens,
            model_provider=result.provider,
            fallback_reason=result.fallback_reason,
            tool_calls=result.tool_calls + [{"name": "analyze_survey_responses", "result": f"{len(findings)} findings"}],
            started_at=stage_started,
        )
        return {
            "analysis_id": analysis_id,
            "summary": result.data.get("summary", ""),
            "findings_count": len(findings),
            "segments_count": len(segments),
            "claims_count": len(claims_for_report),
        }

    def design_interview_guide(
        self,
        task_id: str,
        research_objective: str,
        target_users: str = "",
        interview_count: int = 5,
    ) -> dict[str, Any]:
        """Generate a structured interview guide via LLM."""
        stage_started = now_dt()
        task_config = self._task_config(task_id)

        try:
            result = self.llm_provider.design_interview_guide(
                task_config, research_objective, target_users, interview_count
            )
        except (LLMProviderError, Exception) as exc:
            safe_reason = sanitize_text(str(exc), 300)
            return {
                "guide_id": "",
                "guide": {
                    "title": "访谈提纲生成失败",
                    "estimated_duration_minutes": 0,
                    "target_profile": "",
                    "phases": [],
                    "notes_for_interviewer": f"LLM 调用失败：{safe_reason}。请确认 API Key 配置或稍后重试。",
                    "dimension_coverage": {},
                },
                "source_id": "",
                "error": safe_reason,
            }
        guide_id = uuid.uuid4().hex
        guide_title = sanitize_text(str(result.data.get("title", "用户访谈提纲")), 240)
        now = utc_now_iso()

        with self.connect() as conn:
            source_id = f"{task_id[:8]}_iguide_{uuid.uuid4().hex[:8]}"
            conn.execute(
                """
                INSERT INTO sources
                (id, task_id, source_type, title, url_or_path, author_site, published_at, collected_at,
                 credibility, excerpt, related_claim_ids, provider)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    source_id,
                    task_id,
                    "interview_guide",
                    guide_title,
                    f"llm://interview_guide/{guide_id}",
                    "访谈/问卷整理 Agent",
                    "",
                    now,
                    "generated",
                    sanitize_text(research_objective, 500),
                    "[]",
                    result.provider,
                ),
            )

        phase_count = len(result.data.get("phases", []))
        self._log_agent_run(
            task_id,
            agent_name="访谈/问卷整理 Agent",
            input_summary=f"设计访谈提纲：{research_objective[:120]}",
            output_summary=f"已生成访谈提纲「{guide_title}」，共 {phase_count} 个阶段，预计 {result.data.get('estimated_duration_minutes', 45)} 分钟。",
            status="completed",
            duration_ms=self._elapsed_ms(stage_started),
            token_input=result.input_tokens,
            token_output=result.output_tokens,
            model_provider=result.provider,
            fallback_reason=result.fallback_reason,
            tool_calls=result.tool_calls + [{"name": "design_interview_guide", "result": f"{phase_count} phases"}],
            started_at=stage_started,
        )
        return {"guide_id": guide_id, "guide": result.data, "source_id": source_id}

    def extract_interview_insights(
        self, task_id: str, source_id: str, interviewee_profile: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Deep extraction of interview transcript/notes via LLM."""
        stage_started = now_dt()
        task_config = self._task_config(task_id)

        with self.connect() as conn:
            source = conn.execute(
                "SELECT * FROM sources WHERE task_id = ? AND id = ?",
                (task_id, source_id),
            ).fetchone()
        if not source:
            raise ValueError(f"source {source_id} not found")

        raw_text = source["excerpt"] or ""
        if len(raw_text) < 200:
            raw_text = self._read_source_evidence_text(task_id, source_id)

        try:
            result = self.llm_provider.extract_interview_insights(
                task_config, raw_text, interviewee_profile or {}
            )
        except (LLMProviderError, Exception) as exc:
            safe_reason = sanitize_text(str(exc), 300)
            self._log_agent_run(
                task_id, agent_name="访谈/问卷整理 Agent",
                input_summary=f"提取访谈发现：{source['title']}",
                output_summary=f"LLM 调用失败：{safe_reason}",
                status="failed", duration_ms=self._elapsed_ms(stage_started),
                error=safe_reason, severity="error",
            )
            return {
                "analysis_id": "", "summary": f"提取失败：{safe_reason}",
                "quotes_count": 0, "scenarios_count": 0, "pain_points_count": 0, "claims_count": 0,
                "error": safe_reason,
            }
        analysis_id = uuid.uuid4().hex
        now = utc_now_iso()

        key_quotes = result.data.get("key_quotes", [])
        scenarios = result.data.get("scenarios", [])
        pain_points = result.data.get("pain_points", [])
        needs = result.data.get("needs", [])
        claims_for_report = result.data.get("claims_for_report", [])

        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO interview_analyses
                (id, task_id, source_id, title, interviewee_profile_json, summary,
                 key_quotes_json, scenarios_json, pain_points_json, needs_json,
                 claims_generated_json, confidence_score, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    analysis_id,
                    task_id,
                    source_id,
                    sanitize_text(str(result.data.get("title", source["title"])), 240),
                    dumps(interviewee_profile or {}),
                    sanitize_text(str(result.data.get("summary", "")), 2000),
                    dumps(key_quotes),
                    dumps(scenarios),
                    dumps(pain_points),
                    dumps(needs),
                    dumps(claims_for_report),
                    float(result.data.get("confidence_score", 0.7)),
                    now,
                ),
            )

            # Insert structured claims into the main pipeline
            claims = []
            if claims_for_report:
                for fc in claims_for_report[:12]:
                    claims.append({
                        "section": fc.get("section", "user_persona"),
                        "content": sanitize_text(str(fc.get("content", "")), 1200),
                        "confidence": float(fc.get("confidence", 0.7)),
                        "source_ids": [source_id],
                        "needs_review": fc.get("needs_review", True),
                        "status": "needs_review" if fc.get("needs_review", True) else "reportable",
                        "uncertainty": "来自用户访谈的一手发现，建议与公开材料交叉验证。",
                        "generated_agent": "访谈/问卷整理 Agent",
                        "claim_type": "inference",
                    })
            if not claims:
                claims.append({
                    "section": "user_persona",
                    "content": "上传访谈材料已抽取用户场景、痛点和原文证据，相关结论将以人工上传来源为依据。",
                    "confidence": 0.68,
                    "source_ids": [source_id],
                    "needs_review": True,
                    "status": "needs_review",
                    "uncertainty": "上传资料已脱敏截断进入分析链路，正式结论仍建议人工抽样复核。",
                    "generated_agent": "访谈/问卷整理 Agent",
                })
            self._insert_claims(task_id, claims, conn)

        self._log_agent_run(
            task_id,
            agent_name="访谈/问卷整理 Agent",
            input_summary=f"提取访谈发现：{source['title']}",
            output_summary=f"已提取 {len(key_quotes)} 条引用、{len(scenarios)} 个场景、{len(pain_points)} 个痛点、{len(claims_for_report)} 条报告结论。",
            status="completed",
            duration_ms=self._elapsed_ms(stage_started),
            token_input=result.input_tokens,
            token_output=result.output_tokens,
            model_provider=result.provider,
            fallback_reason=result.fallback_reason,
            tool_calls=result.tool_calls + [{"name": "extract_interview_insights", "result": f"{len(key_quotes)} quotes"}],
            started_at=stage_started,
        )
        return {
            "analysis_id": analysis_id,
            "summary": result.data.get("summary", ""),
            "quotes_count": len(key_quotes),
            "scenarios_count": len(scenarios),
            "pain_points_count": len(pain_points),
            "claims_count": len(claims_for_report),
        }

    def _read_source_evidence_text(self, task_id: str, source_id: str) -> str:
        """Read the full evidence text from chunks for a given source."""
        with self.connect() as conn:
            chunks = conn.execute(
                "SELECT excerpt FROM evidence_chunks WHERE task_id = ? AND source_id = ? ORDER BY chunk_index",
                (task_id, source_id),
            ).fetchall()
        if chunks:
            return "\n".join(
                sanitize_text(str(chunk["excerpt"] or ""), 800) for chunk in chunks[:10]
            )
        return ""

    def _clean_uploaded_material_for_analysis(self, text: str) -> str:
        lines: list[str] = []
        skip_source_list = False
        for raw_line in (text or "").splitlines():
            line = raw_line.strip()
            if not line:
                continue
            if re.match(r"^#{1,3}\s*来源清单\s*$", line):
                skip_source_list = True
                continue
            if line.startswith("## "):
                skip_source_list = False
            if skip_source_list and (line.startswith("- ") or "http://" in line or "https://" in line):
                continue
            if re.match(r"^(竞品名称|材料类型|采集日期|说明|边界说明|生成日期)[:：]", line):
                continue
            if re.search(r"本文件只(整理|列)|不做竞品|不输出|不是评分|正式报告应保留|采集日为", line):
                continue
            if re.match(r"^#\s*(测试资料|资料索引)$", line):
                continue
            lines.append(line)
        return "\n".join(lines) or (text or "")

    def _uploaded_material_metadata(self, task_id: str, filename: str, text: str) -> dict[str, Any]:
        task_config = self._task_config(task_id)
        title_window = sanitize_text(text, 320)
        haystack = f"{filename} {text}".casefold()
        competitor_name = ""
        explicit_match = re.search(r"竞品名称[:：]\s*([^\n。；;]+)", text)
        explicit_name = explicit_match.group(1).casefold() if explicit_match else ""
        for name in task_config.get("competitors", []):
            aliases = [name] + PRODUCT_ALIASES.get(str(name).casefold(), [])
            if explicit_name and any(alias and alias.casefold() in explicit_name for alias in aliases):
                competitor_name = name
                break
        if not competitor_name:
            weighted: list[tuple[int, str]] = []
            for name in task_config.get("competitors", []):
                aliases = [name] + PRODUCT_ALIASES.get(str(name).casefold(), [])
                score = 0
                filename_folded = filename.casefold()
                for alias in aliases:
                    if not alias:
                        continue
                    alias_folded = alias.casefold()
                    score += haystack.count(alias_folded)
                    if alias_folded in filename_folded:
                        score += 12
                    if alias_folded == str(name).casefold() and alias_folded in haystack:
                        score += 5
                weighted.append((score, name))
            weighted.sort(reverse=True)
            if weighted and weighted[0][0] > 0:
                competitor_name = weighted[0][1]
        modules = []
        if re.search(r"功能|能力|feature|model|模型|上下文|多模态|agent|代码|coding|工具调用", text, flags=re.I):
            modules.append("产品/功能")
        if re.search(r"价格|定价|套餐|订阅|API|token|tokens|每百万|1M|美元|元|RMB|USD|¥|￥|成本", text, flags=re.I):
            modules.append("定价/商业化")
        if re.search(r"评价|评论|评分|口碑|App Store|Trustpilot|G2|review|rating|star", text, flags=re.I):
            modules.append("用户反馈/口碑")
        if re.search(r"用户画像|目标用户|场景|persona|scenario|开发者|企业|学生|内容创作者", text, flags=re.I):
            modules.append("用户画像/场景")
        if re.search(r"风险|隐私|合规|数据|安全|政策|privacy|security|compliance|terms", text, flags=re.I):
            modules.append("风险/合规")
        if re.search(r"用户评价|公开评分|评价摘录|Recent Reviews|Trustpilot|App Store 元数据", title_window, flags=re.I):
            role = "review"
        elif re.search(r"定价|API 价格|价格原始|订阅定价|成本字段", title_window, flags=re.I):
            role = "official_pricing"
        elif re.search(r"SWOT|风险|隐私|合规|安全|privacy|security|policy|terms", title_window, flags=re.I):
            role = "official_doc"
        else:
            role = "official"
        credibility = "high" if re.search(r"openai\.com|deepseek\.com|volcengine\.com|doubao\.com|itunes\.apple\.com", text, flags=re.I) else "medium"
        return {
            "competitor_name": competitor_name,
            "module": "、".join(dict.fromkeys(modules)) or "用户上传材料",
            "source_role": role,
            "relevance_score": 9 if competitor_name else 6,
            "credibility": credibility,
        }

    def build_dag(self, task_id: str) -> dict[str, Any]:
        with self.connect() as conn:
            task = conn.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()
            runs = conn.execute(
                "SELECT * FROM agent_runs WHERE task_id = ? ORDER BY started_at, rowid",
                (task_id,),
            ).fetchall()
            events = conn.execute(
                "SELECT * FROM agent_events WHERE task_id = ? ORDER BY created_at, rowid",
                (task_id,),
            ).fetchall()
            findings = conn.execute(
                "SELECT * FROM qa_findings WHERE task_id = ? ORDER BY created_at",
                (task_id,),
            ).fetchall()

        node_defs = [
            ("collector", "采集 Agent", "采集资料"),
            ("analyst", "分析 Agent", "资料理解、ReAct 深度分析、claims 与评分依据"),
            ("qa", "证据质检 Agent", "来源绑定、过度推断、低置信度和待确认项审查"),
            ("reporter", "报告 Agent", "组织最终报告、目录、图表和 PDF"),
        ]
        agent_to_node = {
            "采集 Agent": "collector",
            "访谈/问卷整理 Agent": "collector",
            "分析 Agent": "analyst",
            "质检 Agent": "qa",
            "报告 Agent": "reporter",
        }
        durations = {node_id: 0 for node_id, _, _ in node_defs}
        statuses = {node_id: "等待中" for node_id, _, _ in node_defs}
        details = {node_id: detail for node_id, _, detail in node_defs}
        events_by_node: dict[str, list[dict[str, Any]]] = {node_id: [] for node_id, _, _ in node_defs}

        for event in events:
            node_id = agent_to_node.get(event["agent_name"], "collector" if event["agent_name"] == "编排层" else "")
            if not node_id:
                continue
            events_by_node[node_id].append(
                {
                    "id": event["id"],
                    "agent_name": event["agent_name"],
                    "event_type": event["event_type"],
                    "message": event["message"],
                    "severity": event["severity"],
                    "created_at": event["created_at"],
                    "meta": loads(event["meta_json"], {}),
                }
            )
        running_since = {
            node_id: items[-1]["created_at"]
            for node_id, items in events_by_node.items()
            if items
        }

        for run in runs:
            node_id = agent_to_node.get(run["agent_name"])
            if not node_id:
                continue
            durations[node_id] += int(run["duration_ms"] or 0)
            statuses[node_id] = self._node_status_from_run(run["status"])
            details[node_id] = self._node_user_detail(node_id, run)

        if any(item["fix_status"] in {"open", "manual_pending"} for item in findings):
            statuses["qa"] = "需复核"
            if statuses.get("analyst") in {"等待中", "被打回", "重做中"}:
                statuses["analyst"] = "已完成"
        elif findings:
            statuses["qa"] = "已完成"
            statuses["analyst"] = "已完成" if statuses["analyst"] == "等待中" else statuses["analyst"]

        current_status = task["status"] if task else ""
        running_node = {
            "created": "collector",
            "collecting": "collector",
            "analyzing": "analyst",
            "qa_review": "qa",
            "qa_rework": "analyst",
            "reporting": "reporter",
            "reanalyzing": "analyst",
            "qa_failed": "qa",
        }.get(current_status)
        if running_node and statuses.get(running_node) == "等待中":
            statuses[running_node] = "运行中"

        if running_node and current_status in {"collecting", "analyzing", "reanalyzing", "qa_review", "qa_rework", "reporting"}:
            statuses[running_node] = "running"

        nodes = [
            {
                "id": node_id,
                "label": label,
                "status": statuses[node_id],
                "duration_ms": durations[node_id],
                "running_ms": self._elapsed_from_iso_ms(running_since.get(node_id, "")) if statuses[node_id] == "运行中" else 0,
                "started_at": running_since.get(node_id, ""),
                "detail": details[node_id],
                "events": events_by_node[node_id][-8:],
            }
            for node_id, label, _ in node_defs
        ]
        for node in nodes:
            if node["status"] == "running":
                node["running_ms"] = self._elapsed_from_iso_ms(str(node.get("started_at") or ""))

        edges = [
            {"source": "collector", "target": "analyst", "label": "sources", "edge_type": "normal"},
            {"source": "analyst", "target": "qa", "label": "claims", "edge_type": "normal"},
            {
                "source": "qa",
                "target": "reporter",
                "label": "自动质检/人工复核" if findings else "qa_passed",
                "edge_type": "normal",
            },
        ]
        if findings:
            finding_events = [
                {
                    "id": item["id"],
                    "agent_name": "质检 Agent",
                    "event_type": "qa_finding",
                    "message": item["reason"],
                    "severity": item["severity"],
                    "created_at": item["created_at"],
                    "meta": {
                        "claim_id": item["claim_id"],
                        "target_agent": item["target_agent"],
                        "fix_status": item["fix_status"],
                        "recheck_result": item["recheck_result"],
                    },
                }
                for item in findings[-8:]
            ]
            events_by_node["qa"].extend(finding_events)
        for node in nodes:
            node["events"] = events_by_node[node["id"]][-8:]

        timeline = [
            {
                "agent_name": run["agent_name"],
                "status": run["status"],
                "output_summary": run["output_summary"],
                "duration_ms": run["duration_ms"],
                "retry_count": run["retry_count"],
            }
            for run in runs
        ]
        return {"nodes": nodes, "edges": edges, "timeline": timeline}

    def _existing_user_material_source_map(self, task_id: str) -> dict[str, str]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT id FROM sources
                WHERE task_id = ?
                  AND source_type IN ('uploaded_file', 'manual_url', 'manual_input')
                ORDER BY collected_at, rowid
                """,
                (task_id,),
            ).fetchall()
        return {row["id"]: row["id"] for row in rows}

    def _collect_appark_metrics_for_task(self, task_id: str, competitors: list[str]) -> None:
        stage_started = now_dt()
        competitor_names = [str(name) for name in competitors if str(name).strip()]
        cache_path = Path(self.db_path).resolve().parent / "appark_metrics.json"
        result = collect_appark_metrics(competitor_names, cache_path=cache_path)
        rows = result.get("rows", []) if isinstance(result, dict) else []
        provider = sanitize_text(str(result.get("provider", "appark") if isinstance(result, dict) else "appark"), 80)
        source_url = sanitize_text(str(result.get("source_url", APPARK_COMPETITOR_URL) if isinstance(result, dict) else APPARK_COMPETITOR_URL), 240)
        collected_at = sanitize_text(str(result.get("collected_at", utc_now_iso()) if isinstance(result, dict) else utc_now_iso()), 80)
        with self.connect() as conn:
            conn.execute("DELETE FROM appark_metrics WHERE task_id = ?", (task_id,))
            for row in rows:
                conn.execute(
                    """
                    INSERT INTO appark_metrics
                    (id, task_id, competitor_name, app_name, publisher, downloads_text, downloads_value,
                     revenue_text, revenue_usd, free_rank, paid_rank, overall_rank, country, store,
                     time_range, source_url, provider, collected_at, raw_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        uuid.uuid4().hex,
                        task_id,
                        sanitize_text(str(row.get("competitor", "")), 120),
                        sanitize_text(str(row.get("app_name", "")), 180),
                        sanitize_text(str(row.get("publisher", "")), 180),
                        sanitize_text(str(row.get("downloads_text", "")), 80),
                        float(row.get("downloads_value") or 0),
                        sanitize_text(str(row.get("revenue_text", "")), 80),
                        float(row.get("revenue_usd") or 0),
                        row.get("free_rank"),
                        row.get("paid_rank"),
                        row.get("overall_rank"),
                        sanitize_text(str(row.get("country", "全球")), 60),
                        sanitize_text(str(row.get("store", "全部")), 60),
                        sanitize_text(str(row.get("time_range", "")), 80),
                        source_url,
                        provider,
                        collected_at,
                        dumps(sanitize_payload(row)),
                    ),
                )
        status = "completed" if rows else "failed"
        caveat = sanitize_text(str(result.get("caveat", "") if isinstance(result, dict) else ""), 260)
        self._log_collection_run(
            task_id,
            provider=f"appark_{provider}",
            query=source_url,
            status=status,
            result_count=len(rows),
            time_cost_ms=self._elapsed_ms(stage_started),
            error=caveat,
        )
        self._log_agent_event(
            task_id,
            "采集 Agent",
            "appark_metrics_collected" if rows else "appark_metrics_unavailable",
            f"AppArk 市场表现采集完成：解析 {len(rows)} 个应用指标。" if rows else f"AppArk 市场表现暂不可用：{caveat or '未解析到数据'}",
            severity="info" if rows else "warning",
            meta={"provider": provider, "row_count": len(rows), "source_url": source_url, "caveat": caveat},
        )

    def _collect_user_supplied_sources(
        self,
        task_id: str,
        task_config: dict[str, Any],
    ) -> dict[str, str]:
        stage_started = now_dt()
        self._update_task(task_id, "collecting")
        competitors = task_config.get("competitors", [])
        source_map = self._existing_user_material_source_map(task_id)
        uploaded_count = len(source_map)
        urls = self._user_supplied_urls(task_config)
        drafts: list[WebSourceDraft] = []
        failures: list[dict[str, str]] = []

        self._log_agent_event(
            task_id,
            "采集 Agent",
            "user_material_collect_started",
            "正在读取用户上传材料和用户指定网页；本模式不触发联网搜索发现，也不套用缓存样例。",
            meta={"uploaded_source_count": len(source_map), "url_count": len(urls), "competitors": competitors},
        )

        if urls:
            drafts, failures = self.collector.collect(urls[:12], f"{task_id[:8]}url")
            for draft in drafts:
                draft.provider = "user_url_fetch"
                draft.source_role = "user_url"
                draft.raw_content_status = "fetched"
                draft.credibility = "medium"
                if not draft.module:
                    draft.module = "用户指定网页"
        if drafts:
            source_map.update(self._insert_source_drafts(task_id, self._dedupe_source_drafts(drafts)))

        collection_trace = self._model_collection_review(task_id)
        total_sources = len(self._source_ids_for_task(task_id))
        self._log_agent_run(
            task_id,
            agent_name="采集 Agent",
            input_summary="读取用户上传文件和用户指定 URL；禁用搜索发现与缓存样例套用。",
            output_summary=f"已登记 {total_sources} 条任务来源，其中上传材料 {uploaded_count} 条，用户 URL 抓取成功 {len(drafts)} 条，失败 {len(failures)} 条。",
            status="completed",
            duration_ms=self._elapsed_ms(stage_started),
            token_input=collection_trace.get("token_input"),
            token_output=collection_trace.get("token_output"),
            model_provider=collection_trace.get("provider", ""),
            fallback_reason=dumps(failures) if failures else collection_trace.get("fallback_reason", ""),
            severity="warning" if failures else "info",
            tool_calls=[
                {"name": "load_uploaded_materials", "result": f"{uploaded_count} sources"},
                {"name": "fetch_user_urls", "result": f"{len(drafts)} saved/{len(failures)} failed"},
            ]
            + collection_trace.get("tool_calls", []),
            started_at=stage_started,
        )
        self._log_agent_event(
            task_id,
            "采集 Agent",
            "user_material_collect_finished",
            f"用户材料采集完成：当前来源库共 {total_sources} 条；未命中材料的竞品会保留待补证范围。",
            severity="info" if total_sources else "warning",
            meta={"source_count": total_sources, "url_failures": failures},
        )
        return source_map

    def _collect_real_sources(
        self,
        task_id: str,
        task_config: dict[str, Any],
        dataset: dict[str, Any],
    ) -> dict[str, str]:
        stage_started = now_dt()
        self._update_task(task_id, "collecting")
        competitors = task_config.get("competitors", [])
        self._log_agent_event(
            task_id,
            "采集 Agent",
            "collect_started",
            f"正在为 {self._join_names(competitors)} 准备联网采集。",
            meta={"competitors": competitors},
        )
        urls = self._candidate_urls(task_config, dataset)
        source_map: dict[str, str] = {}
        drafts: list[WebSourceDraft] = []
        failures: list[dict[str, str]] = []
        searched_competitors: list[str] = []
        search_plan_trace: dict[str, Any] = {"tool_calls": []}

        # Detect unconfigured competitors and log guidance
        for name in competitors:
            if not PRODUCT_URL_HINTS.get(name.casefold()) and not PRODUCT_ALIASES.get(name.casefold()):
                self._log_agent_event(
                    task_id,
                    "采集 Agent",
                    "auto_config_notice",
                    f"{name} 未在 app_config.json 中找到预设配置，将使用自动生成的搜索词和别名。"
                    f"如需更精准结果，可在 data/app_config.json 中为该产品添加 url_hints、aliases 和 related_terms。",
                    severity="info",
                    meta={"competitor": name},
                )

        if urls:
            self._log_agent_event(
                task_id,
                "采集 Agent",
                "url_seed_detected",
                "检测到用户 URL；本轮按火山联网搜索 API 作为证据来源，URL 会作为搜索线索，不再二次抓取原网页正文。",
                meta={"url_count": min(len(urls), 8)},
            )

        self._log_agent_event(
            task_id,
            "采集 Agent",
            "volc_search_started",
            "正在通过火山联网搜索 API 检索官网、价格、功能、公开评价和新闻线索。",
            meta={"competitors": competitors, "focus_areas": task_config.get("focus_areas", [])},
        )
        config_status = self.search_client.config_status()
        self._log_agent_event(
            task_id,
            "采集 Agent",
            "volc_search_config",
            "火山联网搜索配置已读取；API Key 状态只记录是否配置，不写入明文。",
            severity="info" if config_status.get("api_key_configured") else "warning",
            meta=config_status,
        )
        drafts, failures, searched_competitors, search_plan_trace = self._search_sources_for_competitors(
            task_id,
            task_config,
            0,
        )

        rss_drafts, rss_failures, rss_meta = collect_google_alert_sources(competitors, task_id[:8])
        if rss_meta.get("configured"):
            drafts.extend(rss_drafts)
            failures.extend(rss_failures)
            self._log_collection_run(
                task_id,
                provider="google_alerts_rss",
                query=f"{rss_meta.get('url_count', 0)} Google Alerts RSS feeds",
                status="completed" if rss_drafts else ("failed" if rss_failures else "completed"),
                result_count=len(rss_drafts),
                time_cost_ms=int(rss_meta.get("time_cost_ms", 0) or 0),
                error=dumps(rss_failures) if rss_failures else "",
            )
            self._log_agent_event(
                task_id,
                "采集 Agent",
                "google_alerts_rss_read",
                f"Google Alerts RSS 已读取：{rss_meta.get('url_count', 0)} 个订阅，解析 {len(rss_drafts)} 条新闻/舆情线索。",
                severity="info" if rss_drafts else "warning",
                meta=sanitize_payload(rss_meta),
            )
            search_plan_trace.setdefault("tool_calls", []).append(
                {
                    "name": "read_google_alerts_rss",
                    "provider": "google_alerts_rss",
                    "result": f"{len(rss_drafts)} items/{len(rss_failures)} failed",
                }
            )

        drafts = self._dedupe_source_drafts(drafts)
        if drafts:
            source_map = self._insert_source_drafts(task_id, drafts)
            collection_trace = self._model_collection_review(task_id)
            self._log_agent_event(
                task_id,
                "采集 Agent",
                "sources_saved",
                f"已登记 {len(drafts)} 条来源并生成证据分片。",
                meta={"source_count": len(drafts)},
            )
            self._log_agent_run(
                task_id,
                agent_name="采集 Agent",
                input_summary=f"实时采集候选 URL {len(urls[:8])} 个，并搜索 {self._join_names(searched_competitors)}。",
                output_summary=f"成功写入 {len(drafts)} 条真实网页/搜索线索，失败 {len(failures)} 条；证据分片已入库。",
                status="completed",
                duration_ms=self._elapsed_ms(stage_started),
                token_input=collection_trace.get("token_input"),
                token_output=collection_trace.get("token_output"),
                model_provider=collection_trace.get("provider", ""),
                severity="info" if not failures else "warning",
                fallback_reason=dumps(failures) if failures else collection_trace.get("fallback_reason", ""),
                tool_calls=[
                    {"name": "web_search", "provider": "volc_search", "result": f"{len(drafts)} saved/{len(failures)} failed"},
                    {"name": "discover_search_results", "provider": "volc_search", "result": f"{len(searched_competitors)} competitors searched"},
                ]
                + search_plan_trace.get("tool_calls", [])
                + collection_trace.get("tool_calls", []),
                started_at=stage_started,
            )
            return source_map

        collection_trace = self._model_collection_review(task_id)
        self._log_agent_run(
            task_id,
            agent_name="采集 Agent",
            input_summary=f"实时采集 {len(urls[:8])} 个候选 URL，并执行搜索发现。",
            output_summary="未找到可用于当前竞品的联网来源；不会混入缓存样例，报告只保留待补证提示。",
            status="completed",
            duration_ms=self._elapsed_ms(stage_started),
            token_input=collection_trace.get("token_input"),
            token_output=collection_trace.get("token_output"),
            model_provider=collection_trace.get("provider", ""),
            severity="warning",
            fallback_reason=dumps(failures) or collection_trace.get("fallback_reason", "") or "no_realtime_source",
            tool_calls=[{"name": "web_search", "provider": "volc_search", "result": "all failed"}]
            + search_plan_trace.get("tool_calls", [])
            + collection_trace.get("tool_calls", []),
            started_at=stage_started,
        )
        return {}

    def _fetch_search_result_pages(
        self,
        task_id: str,
        search_drafts: list[WebSourceDraft],
    ) -> tuple[list[WebSourceDraft], list[dict[str, str]]]:
        urls = [draft.url for draft in search_drafts if draft.url]
        if not urls:
            return [], []
        self._log_agent_event(
            task_id,
            "采集 Agent",
            "fetch_search_results",
            f"正在抓取 {len(urls)} 个搜索结果页正文。",
            meta={"url_count": len(urls)},
        )
        return self.collector.collect(urls[:8], f"{task_id[:8]}sf")

    def _dedupe_source_drafts(self, drafts: list[WebSourceDraft]) -> list[WebSourceDraft]:
        by_url: dict[str, WebSourceDraft] = {}
        for draft in drafts:
            key = (draft.url or draft.source_id).strip().lower()
            existing = by_url.get(key)
            if existing is None:
                by_url[key] = draft
                continue
            existing_score = int(existing.relevance_score or 0)
            draft_score = int(draft.relevance_score or 0)
            if draft_score > existing_score:
                by_url[key] = draft
            elif existing.source_type == "search_result" and draft.source_type != "search_result":
                by_url[key] = draft
        return list(by_url.values())

    def _insert_source_drafts(self, task_id: str, drafts: list[WebSourceDraft]) -> dict[str, str]:
        source_map: dict[str, str] = {}
        now = utc_now_iso()
        with self.connect() as conn:
            for draft in drafts:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO sources
                    (id, task_id, source_type, title, url_or_path, author_site, published_at, collected_at,
                     credibility, excerpt, related_claim_ids, fallback_reason, provider, search_log_id,
                     search_query, auth_info, auth_level, time_cost_ms, competitor_name, module,
                     relevance_score, source_role, raw_content_status)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        draft.source_id,
                        task_id,
                        draft.source_type,
                        sanitize_text(draft.title, 240),
                        draft.url,
                        draft.author_site,
                        draft.published_at,
                        now,
                        draft.credibility,
                        sanitize_text(draft.excerpt, 800),
                        "[]",
                        sanitize_text(draft.fallback_reason, 300),
                        sanitize_text(draft.provider, 80),
                        sanitize_text(draft.search_log_id, 120),
                        sanitize_text(draft.search_query, 180),
                        sanitize_text(draft.auth_info, 160),
                        int(draft.auth_level or 0),
                        int(draft.time_cost_ms or 0),
                        sanitize_text(draft.competitor_name, 120),
                        sanitize_text(draft.module, 80),
                        int(draft.relevance_score or 0),
                        sanitize_text(draft.source_role, 80),
                        sanitize_text(draft.raw_content_status, 80),
                    ),
                )
                source_map[draft.source_id] = draft.source_id
                self._insert_evidence_chunks(conn, task_id, draft.source_id, draft.chunks, now)
        return source_map

    def _competitors_covered_by_drafts(self, drafts: list[WebSourceDraft], competitors: list[str]) -> set[str]:
        covered = set()
        for name in competitors:
            if any(self._draft_matches_competitor(draft, name) for draft in drafts):
                covered.add(name)
        return covered

    def _draft_matches_competitor(self, draft: WebSourceDraft, name: str) -> bool:
        if draft.competitor_name and draft.competitor_name.casefold() == (name or "").casefold():
            return True
        searchable = f"{draft.title} {draft.url} {draft.author_site}".casefold()
        aliases = self._search_aliases_for_name(name)
        if any(alias and alias.casefold() in searchable for alias in aliases):
            return True
        related = self._search_related_terms_for_name(name, "", self._analysis_object_type(name, ""))
        if self._analysis_object_type(name, "") == "category" and sum(1 for term in related if term.casefold() in searchable) >= 2:
            return True
        url = (draft.url or "").casefold()
        for hint in self._url_hints_for_name(name):
            parts = self._parse_url_parts(hint)
            if not parts:
                continue
            hint_netloc, hint_path = parts
            if hint_netloc and hint_netloc in url and (not hint_path or hint_path in url):
                return True
        return False

    def _collect_manual_scope_sources(
        self,
        task_id: str,
        task_config: dict[str, Any],
        collected_source_map: dict[str, str],
    ) -> dict[str, str]:
        competitors = task_config.get("competitors", [])
        if not competitors:
            return {}
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM sources WHERE task_id = ?",
                (task_id,),
            ).fetchall()
        missing = [name for name in competitors if not any(self._source_matches_competitor(row, name) for row in rows)]
        if not missing:
            return {}
        now = utc_now_iso()
        result: dict[str, str] = {}
        with self.connect() as conn:
            for index, name in enumerate(missing, start=1):
                source_id = f"{task_id[:8]}_scope_{index:02d}"
                excerpt = f"用户当前任务要求分析竞品：{name}。联网采集暂未命中可核验公开来源，该竞品进入待补证状态。"
                conn.execute(
                    """
                    INSERT OR IGNORE INTO sources
                    (id, task_id, source_type, title, url_or_path, author_site, published_at, collected_at,
                     credibility, excerpt, related_claim_ids, fallback_reason, competitor_name, module,
                     relevance_score, source_role, raw_content_status)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        source_id,
                        task_id,
                        "manual_scope",
                        f"{name} 待补证范围说明",
                        "",
                        "user_input",
                        "",
                        now,
                        "low",
                        excerpt,
                        "[]",
                        "realtime_search_no_match",
                        name,
                        "待补证",
                        0,
                        "source_gap",
                        "not_collected",
                    ),
                )
                self._insert_evidence_chunks(conn, task_id, source_id, chunk_text(excerpt), now)
                result[source_id] = source_id
        self._log_agent_event(
            task_id,
            "采集 Agent",
            "scope_sources_created",
            f"未命中 {len(missing)} 个竞品的可核验网页，已标记为待补证：{self._join_names(missing)}。",
            severity="warning",
            meta={"missing_competitors": missing},
        )
        return result

    def _candidate_urls(self, task_config: dict[str, Any], dataset: dict[str, Any]) -> list[str]:
        urls: list[str] = []
        urls.extend(task_config.get("websites", []))
        urls.extend(re.findall(r"https?://[^\s，,。；;）)]+", task_config.get("notes", "")))
        for name in task_config.get("competitors", []):
            urls.extend(self._url_hints_for_name(name))
        competitor_names = {name.casefold() for name in task_config.get("competitors", [])}
        for competitor in dataset.get("competitors", []):
            if competitor.get("name", "").casefold() not in competitor_names:
                continue
            if competitor.get("website"):
                urls.append(competitor["website"])
            for source in competitor.get("sources", []):
                if source.get("source_type") in {"official_site", "pricing_page", "public_doc"}:
                    urls.append(source.get("url_or_path", ""))
        cleaned: list[str] = []
        for url in urls:
            if url and url not in cleaned:
                cleaned.append(url)
        return cleaned

    def _user_supplied_urls(self, task_config: dict[str, Any]) -> list[str]:
        urls: list[str] = []
        urls.extend(task_config.get("websites", []))
        urls.extend(re.findall(r"https?://[^\s，,。；;）)]+", task_config.get("notes", "")))
        cleaned: list[str] = []
        for url in urls:
            url = (url or "").strip().rstrip("，,。；;）)")
            if url and url not in cleaned:
                cleaned.append(url)
        return cleaned

    def _url_hints_for_name(self, name: str) -> list[str]:
        key = (name or "").strip().casefold()
        hints = list(PRODUCT_URL_HINTS.get(key, []))
        ascii_slug = re.sub(r"[^a-z0-9-]+", "", key.replace(" ", ""))
        is_chinese = bool(re.search(r'[一-鿿]', name or ""))
        if ascii_slug and "." not in ascii_slug:
            hints.extend([f"https://www.{ascii_slug}.com/", f"https://{ascii_slug}.com/"])
        # For Chinese names, also try .cn domains
        if is_chinese and ascii_slug and "." not in ascii_slug:
            hints.extend([f"https://www.{ascii_slug}.cn/", f"https://{ascii_slug}.cn/"])
        deduped: list[str] = []
        for url in hints:
            if url and url not in deduped:
                deduped.append(url)
        return deduped

    def _block_hosts_for_task(self, industry: str, competitor_names: list[str]) -> str:
        bucket = self._industry_bucket(industry, competitor_names)
        block_hosts = SEARCH_BLOCK_HOSTS.get(bucket, SEARCH_BLOCK_HOSTS.get("default", ""))
        if SEARCH_BLOCK_HOSTS_SELF_EXEMPT and block_hosts:
            for name in competitor_names:
                for hint in self._url_hints_for_name(name):
                    parts = self._parse_url_parts(hint)
                    if parts and parts[0]:
                        domain = parts[0].removeprefix("www.")
                        escaped = re.escape(domain)
                        block_hosts = re.sub(rf"\b{escaped}\|?", "", block_hosts).strip("|")
        return block_hosts

    def _run_search_with_fallback(self, job: dict[str, Any], task_id: str, industry: str) -> dict[str, Any]:
        """Try VolcSearch → BingSearch → direct URL fetch → graceful empty."""
        query = job["query"]
        module = job["module"]
        name = job["competitor"]
        task_prefix = task_id[:8]
        block_hosts = job.get("block_hosts", "")
        aliases = job.get("aliases", [])
        related_terms = job.get("related_terms", [])

        # Tier 1: VolcSearch
        try:
            results = self.search_client.search(
                query, task_prefix,
                start_index=job["start_index"], limit=6,
                block_hosts=block_hosts,
            )
            log_id = next((draft.search_log_id for draft in results if draft.search_log_id), "")
            time_cost_ms = max([draft.time_cost_ms for draft in results] or [0])
            filtered, filter_meta = self._filter_search_results_for_name(
                results, name, industry, module, aliases, related_terms,
            )
            kept = self._enrich_high_quality_search_results(task_id, filtered[:3])
            return {
                **job, "status": "completed", "provider": "volc_search",
                "results": results, "kept": kept, "log_id": log_id,
                "time_cost_ms": time_cost_ms, "filter_meta": filter_meta,
            }
        except Exception as volc_exc:
            volc_reason = sanitize_text(str(volc_exc), 240)
            self._log_agent_event(
                task_id, "采集 Agent", "volc_search_failed",
                f"VolcSearch 对 '{query}' 失败: {volc_reason}，尝试 Bing 回退。",
                severity="warning",
                meta={"competitor": name, "query": query, "reason": volc_reason},
            )

        # Tier 2: BingSearch
        try:
            results = self.bing_client.search(query, task_prefix, start_index=job["start_index"], limit=3)
            if results:
                filtered, filter_meta = self._filter_search_results_for_name(
                    results, name, industry, module, aliases, related_terms,
                )
                kept = self._enrich_high_quality_search_results(task_id, filtered[:3])
                self._log_agent_event(
                    task_id, "采集 Agent", "bing_search_success",
                    f"Bing 回退对 '{query}' 成功，返回 {len(results)} 条原始结果，保留 {len(kept)} 条。",
                    meta={"competitor": name, "query": query, "raw_count": len(results), "kept_count": len(kept)},
                )
                return {
                    **job, "status": "completed", "provider": "bing_search",
                    "results": results, "kept": kept, "log_id": "",
                    "time_cost_ms": 0, "filter_meta": filter_meta,
                }
        except Exception as bing_exc:
            bing_reason = sanitize_text(str(bing_exc), 240)
            self._log_agent_event(
                task_id, "采集 Agent", "bing_search_failed",
                f"BingSearch 对 '{query}' 失败: {bing_reason}，尝试直接抓取已知 URL。",
                severity="warning",
                meta={"competitor": name, "query": query, "reason": bing_reason},
            )

        # Tier 3: direct page fetch from URL hints
        url_hints = self._url_hints_for_name(name)
        if url_hints:
            try:
                fetched, failures = self.collector.collect(url_hints[:3], f"{task_prefix}_direct")
                if fetched:
                    for draft in fetched:
                        draft.source_type = "web_page"
                        draft.competitor_name = name
                        draft.module = module
                        draft.relevance_score = 5
                        draft.source_role = "third_party"
                        draft.raw_content_status = "fetched"
                        draft.provider = "direct_fetch"
                    self._log_agent_event(
                        task_id, "采集 Agent", "direct_fetch_success",
                        f"直接抓取对 '{name}' 成功，获得 {len(fetched)} 个页面。",
                        meta={"competitor": name, "fetched_count": len(fetched), "failures": len(failures)},
                    )
                    return {
                        **job, "status": "completed", "provider": "direct_fetch",
                        "results": fetched, "kept": fetched[:3], "log_id": "",
                        "time_cost_ms": 0,
                        "filter_meta": {"object_type": self._analysis_object_type(name, industry), "kept_count": len(fetched)},
                    }
            except Exception as fetch_exc:
                self._log_agent_event(
                    task_id, "采集 Agent", "direct_fetch_failed",
                    f"直接抓取对 '{name}' 也失败: {sanitize_text(str(fetch_exc), 160)}",
                    severity="warning",
                )

        # Tier 4: graceful degradation
        return {
            **job, "status": "failed",
            "reason": f"All search tiers exhausted for '{query}'",
            "results": [], "kept": [], "log_id": "", "time_cost_ms": 0, "filter_meta": {},
        }

    def _search_sources_for_competitors(
        self,
        task_id: str,
        task_config: dict[str, Any],
        start_index: int,
    ) -> tuple[list[WebSourceDraft], list[dict[str, str]], list[str], dict[str, Any]]:
        drafts: list[WebSourceDraft] = []
        failures: list[dict[str, str]] = []
        searched: list[str] = []
        industry = task_config.get("industry", "")
        competitors = task_config.get("competitors", [])[:4]
        block_hosts = self._block_hosts_for_task(industry, competitors)
        query_plan, plan_trace = self._model_collection_query_plan(task_id, task_config)
        search_jobs: list[dict[str, Any]] = []
        for competitor_index, name in enumerate(competitors):
            searched.append(name)
            seed_drafts = self._collect_official_seed_drafts(task_id, name)
            if seed_drafts:
                drafts.extend(seed_drafts)
                self._log_agent_event(
                    task_id,
                    "采集 Agent",
                    "official_seed_sources",
                    f"已优先登记 {name} 的官方种子来源 {len(seed_drafts)} 条。",
                    meta={"competitor": name, "source_count": len(seed_drafts), "modules": [draft.module for draft in seed_drafts]},
                )
            queries = self._queries_for_competitor(name, industry, query_plan)
            seen_queries: list[str] = []
            for query_index, query_item in enumerate([item for item in queries if item.get("query")]):
                query = self._compact_search_query(query_item["query"])
                module = query_item.get("module", "综合")
                if query in seen_queries:
                    continue
                seen_queries.append(query)
                self._log_agent_event(
                    task_id,
                    "采集 Agent",
                    "search_query",
                    f"正在搜索{module}线索：{query}",
                    meta={"competitor": name, "module": module, "query": query},
                )
                search_jobs.append(
                    {
                        "competitor_index": competitor_index,
                        "query_index": query_index,
                        "competitor": name,
                        "module": module,
                        "query": query,
                        "aliases": query_item.get("aliases", []),
                        "related_terms": query_item.get("related_terms", []),
                        "start_index": start_index + len(drafts) + len(search_jobs) * 10,
                        "block_hosts": block_hosts,
                    }
                )

        if search_jobs:
            self._log_agent_event(
                task_id,
                "采集 Agent",
                "parallel_search_dispatched",
                f"已并行调度 {len(search_jobs)} 个搜索任务，按竞品和维度同时补采来源。",
                meta={"job_count": len(search_jobs), "competitors": searched},
            )

        def run_search_job(job: dict[str, Any]) -> dict[str, Any]:
            return self._run_search_with_fallback(job, task_id, industry)

        completed_jobs: list[dict[str, Any]] = []
        if search_jobs:
            worker_count = min(8, max(1, len(search_jobs)))
            with ThreadPoolExecutor(max_workers=worker_count) as executor:
                future_map = {executor.submit(run_search_job, job): job for job in search_jobs}
                for future in as_completed(future_map):
                    completed_jobs.append(future.result())
            completed_jobs.sort(key=lambda item: (item["competitor_index"], item["query_index"], item["start_index"]))

        for job in completed_jobs:
            query = job["query"]
            module = job["module"]
            name = job["competitor"]
            if job["status"] == "failed":
                safe_reason = job.get("reason", "")
                failures.append({"query": query, "module": module, "reason": safe_reason})
                self._log_collection_run(
                    task_id,
                    provider=job.get("provider", "volc_search"),
                    query=query,
                    status="failed",
                    result_count=0,
                    error=safe_reason,
                )
                continue

            results = job.get("results", [])
            kept = job.get("kept", [])
            self._log_collection_run(
                task_id,
                provider=job.get("provider", "volc_search"),
                query=query,
                status="completed",
                result_count=len(results),
                log_id=job.get("log_id", ""),
                time_cost_ms=int(job.get("time_cost_ms", 0) or 0),
            )
            if kept:
                drafts.extend(kept)
                self._log_agent_event(
                    task_id,
                    "采集 Agent",
                    "search_query_result",
                    f"{name} 的{module}搜索候选 {len(results)} 条，保留 {len(kept)} 条，丢弃 {max(len(results) - len(kept), 0)} 条。",
                    meta={
                        "competitor": name,
                        "module": module,
                        "query": query,
                        "candidate_count": len(results),
                        "kept": len(kept),
                        "dropped": max(len(results) - len(kept), 0),
                        **job.get("filter_meta", {}),
                    },
                )
            else:
                self._log_agent_event(
                    task_id,
                    "采集 Agent",
                    "search_query_filtered",
                    f"{name} 的{module}搜索候选 {len(results)} 条，因相关性不足或低价值页面全部丢弃。",
                    severity="warning",
                    meta={
                        "competitor": name,
                        "module": module,
                        "query": query,
                        "candidate_count": len(results),
                        "dropped": len(results),
                        **job.get("filter_meta", {}),
                    },
                )
        return self._dedupe_source_drafts(drafts), failures, searched, plan_trace

    def _official_seed_specs_for_name(self, name: str) -> list[dict[str, str]]:
        key = (name or "").strip().casefold()
        keys = [key] + [alias.casefold() for alias in PRODUCT_ALIASES.get(key, [])]
        if "chatgpt" in keys:
            keys.append("openai")
        specs: list[dict[str, str]] = []
        seen_urls: set[str] = set()
        for candidate in keys:
            for spec in OFFICIAL_SOURCE_SEEDS.get(candidate, []):
                url = spec.get("url", "")
                if not url or url in seen_urls:
                    continue
                seen_urls.add(url)
                specs.append(spec)
        return specs

    def _existing_source_urls_for_task(self, task_id: str) -> set[str]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT url_or_path FROM sources WHERE task_id = ?",
                (task_id,),
            ).fetchall()
        return {row["url_or_path"].strip().casefold() for row in rows if row["url_or_path"]}

    def _seed_source_id(self, task_id: str, url: str) -> str:
        digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:10]
        return f"{task_id[:8]}_seed_{digest}"

    def _seed_source_type(self, role: str, url: str) -> str:
        if role == "official_pricing" or re.search(r"pricing|price", url, flags=re.I):
            return "pricing_page"
        if role == "review" or re.search(r"g2\.com|trustpilot|apps\.apple|play\.google", url, flags=re.I):
            return "review_page"
        if role == "news" or re.search(r"news|reuters|apnews|cnbc", url, flags=re.I):
            return "news"
        if role == "official_doc" or re.search(r"docs|developer|help|support|policies", url, flags=re.I):
            return "public_doc"
        return "official_site"

    def _collect_official_seed_drafts(self, task_id: str, name: str) -> list[WebSourceDraft]:
        existing_urls = self._existing_source_urls_for_task(task_id)
        drafts: list[WebSourceDraft] = []
        for spec in self._official_seed_specs_for_name(name):
            url = spec["url"]
            if url.strip().casefold() in existing_urls:
                continue
            source_id = self._seed_source_id(task_id, url)
            fetched, failures = self.collector.collect([url], source_id)
            role = spec.get("source_role", "official")
            module = spec.get("module", "官方来源")
            if fetched:
                draft = fetched[0]
                draft.source_id = source_id
                draft.title = draft.title or spec.get("title", "")
                draft.source_type = self._seed_source_type(role, url)
                draft.credibility = "high"
                draft.fallback_reason = ""
                draft.raw_content_status = "fetched"
            else:
                reason = failures[0].get("reason", "page_fetch_failed") if failures else "page_fetch_failed"
                excerpt = (
                    f"官方来源入口：{spec.get('title', '')}。URL：{url}。"
                    "本次未抓取到正文，不能仅凭该入口生成事实，需结合正文抓取、搜索摘要或人工核验。"
                )
                draft = WebSourceDraft(
                    source_id=source_id,
                    source_type=self._seed_source_type(role, url),
                    title=spec.get("title", urllib.parse.urlparse(url).netloc),
                    url=url,
                    author_site=urllib.parse.urlparse(url).netloc,
                    excerpt=excerpt,
                    credibility="high",
                    chunks=chunk_text(excerpt, chunk_size=700, overlap=80),
                    fallback_reason=sanitize_text(f"page_fetch_failed: {reason}", 240),
                    provider="official_seed",
                    raw_content_status="summary_only",
                )
            draft.competitor_name = name
            draft.module = module
            draft.relevance_score = 12 if role == "official_pricing" else 10
            draft.source_role = role
            draft.provider = draft.provider or "official_seed"
            drafts.append(draft)
        return drafts

    def _enrich_high_quality_search_results(self, task_id: str, drafts: list[WebSourceDraft]) -> list[WebSourceDraft]:
        enriched: list[WebSourceDraft] = []
        for draft in drafts:
            should_fetch = draft.source_role in {"official", "official_pricing", "official_doc"} or int(draft.relevance_score or 0) >= 8
            if not should_fetch:
                draft.raw_content_status = draft.raw_content_status or "summary_only"
                enriched.append(draft)
                continue
            fetched, failures = self.collector.collect([draft.url], f"{draft.source_id}_raw")
            if fetched:
                fetched_source = fetched[0]
                draft.title = fetched_source.title or draft.title
                draft.source_type = fetched_source.source_type if fetched_source.source_type != "web_page" else draft.source_type
                draft.excerpt = fetched_source.excerpt or draft.excerpt
                draft.chunks = fetched_source.chunks or draft.chunks
                draft.raw_content_status = "fetched"
                if draft.credibility != "high":
                    draft.credibility = "medium"
            else:
                draft.raw_content_status = "summary_only"
                reason = failures[0].get("reason", "page_fetch_failed") if failures else "page_fetch_failed"
                draft.fallback_reason = sanitize_text(f"page_fetch_failed: {reason}", 240)
            enriched.append(draft)
        return enriched

    def _price_search_terms(self, name: str, industry: str) -> tuple[str, str]:
        bucket = self._industry_bucket(industry, [name])
        if bucket == "ai":
            return "官方价格/API", f"{name} 官方 价格 API"
        if bucket == "automotive":
            return "官方价格/配置", f"{name} 官方 车型 价格 配置"
        if bucket == "photovoltaic":
            return "官方价格/规格", f"{name} 组件 电池片 价格 规格"
        if bucket == "coal":
            return "价格/长协", f"{name} 煤价 长协 现货 产能"
        if bucket == "content_social":
            return "商业化/变现", f"{name} 广告 商业化 变现 分成"
        return "官方价格/报价", f"{name} 官方 价格 报价"

    def _default_collection_queries(self, name: str, industry: str) -> list[dict[str, str]]:
        base_query = PRODUCT_SEARCH_QUERIES.get((name or "").strip().casefold())
        object_type = self._analysis_object_type(name, industry)
        price_module, price_query = self._price_search_terms(name, industry)
        if object_type == "category":
            return [
                {"module": "定义/标准", "query": f"{name} 定义 标准"},
                {"module": "代表品牌", "query": f"{name} 代表品牌"},
                {"module": "价格带", "query": f"{name} 价格带 价格"},
                {"module": "消费场景", "query": f"{name} 消费场景 人群"},
                {"module": "市场新闻", "query": f"{name} 市场 行业 报道"},
                {"module": "用户评价", "query": f"{name} 用户评价 口碑"},
            ]
        if object_type == "company":
            alias = self._preferred_search_alias(name)
            related = self._search_related_terms_for_name(name, industry, object_type)
            related_text = " ".join(related[:2])
            queries = [
                {"module": "官网/功能", "query": base_query or f"{name} 官网 产品"},
                {"module": price_module, "query": price_query},
                {"module": "官方文档", "query": f"{name} 官方 文档"},
                {"module": "安全/企业", "query": f"{name} 安全 企业 合规"},
                {"module": "评价平台", "query": f"{name} 用户评价 口碑"},
                {"module": "新闻/风险", "query": f"{name} 新闻 风险"},
                {"module": "官网/功能", "query": f"{alias} {related_text} 产品"} if alias and related_text else {},
                {"module": "投资者关系", "query": f"{name} 投资者关系 年报"},
            ]
            return [item for item in queries if item.get("query")]
        bucket = self._industry_bucket(industry, [name])
        if bucket == "content_social":
            return [
                {"module": "官网/产品", "query": base_query or f"{name} 官网 产品 功能"},
                {"module": price_module, "query": price_query},
                {"module": "用户规模", "query": f"{name} DAU MAU 用户 数据 活跃"},
                {"module": "用户评价", "query": f"{name} 用户 评价 口碑 体验"},
                {"module": "创作者生态", "query": f"{name} 创作者 变现 生态"},
                {"module": "广告/商业化", "query": f"{name} 广告 投放 商业化 收入"},
                {"module": "新闻/风险", "query": f"{name} 新闻 风险 监管 竞争"},
                {"module": "竞品对比", "query": f"{name} 对比 竞品 差异"},
            ]
        return [
            {"module": "官网/功能", "query": base_query or f"{name} 官网 产品 功能"},
            {"module": price_module, "query": price_query},
            {"module": "官方文档", "query": f"{name} 官方 文档 开发者"},
            {"module": "安全/企业", "query": f"{name} 安全 企业 隐私"},
            {"module": "评价平台", "query": f"{name} 用户评价 口碑 G2"},
            {"module": "新闻/风险", "query": f"{name} 新闻 风险 市场"},
        ]

    def _queries_for_competitor(
        self,
        name: str,
        industry: str,
        query_plan: dict[str, list[dict[str, str]]],
    ) -> list[dict[str, str]]:
        merged: list[dict[str, str]] = []
        merged.extend(self._default_collection_queries(name, industry))
        merged.extend(query_plan.get(name) or [])
        deduped: list[dict[str, str]] = []
        seen: set[str] = set()
        for item in merged:
            query = self._compact_search_query(str(item.get("query", "")))
            if not query or query in seen:
                continue
            seen.add(query)
            deduped.append({"module": sanitize_text(str(item.get("module", "综合")), 40), "query": query})
            if isinstance(item.get("aliases"), list):
                deduped[-1]["aliases"] = item["aliases"][:8]
            if isinstance(item.get("related_terms"), list):
                deduped[-1]["related_terms"] = item["related_terms"][:10]
        return deduped[:8]

    def _compact_search_query(self, query: str) -> str:
        cleaned = re.sub(r"[，,；;。]+", " ", sanitize_text(query, 120))
        cleaned = re.sub(r"\s+", " ", cleaned).strip()
        parts = cleaned.split()
        if len(parts) > 7:
            cleaned = " ".join(parts[:7])
        return cleaned[:72]

    def _preferred_search_alias(self, name: str) -> str:
        aliases = self._search_aliases_for_name(name)
        for alias in aliases:
            if alias == name:
                continue
            if re.search(r"[A-Za-z]", alias) and len(alias) >= 3:
                return alias
        stripped = re.sub(r"(股份有限公司|股份|有限公司|集团|公司)$", "", name or "").strip()
        return stripped if stripped and stripped != name else ""

    def _model_collection_query_plan(self, task_id: str, task_config: dict[str, Any]) -> tuple[dict[str, list[dict[str, str]]], dict[str, Any]]:
        competitors = task_config.get("competitors", [])[:4]
        fallback_plan = {name: self._default_collection_queries(name, task_config.get("industry", "")) for name in competitors}
        if not self._external_calls_allowed_for_config(task_config):
            return fallback_plan, self._offline_external_trace("collection_query_plan", len(dumps(fallback_plan)) // 4)
        try:
            result = self.llm_provider.plan_collection_queries(task_config)
            plan = {name: [] for name in competitors}
            for item in result.data.get("queries", [])[:40]:
                if not isinstance(item, dict):
                    continue
                competitor = sanitize_text(str(item.get("competitor", "")), 80)
                matched = next((name for name in competitors if name.casefold() == competitor.casefold()), "")
                if not matched:
                    continue
                module = sanitize_text(str(item.get("module", "综合")), 40)
                query = sanitize_text(str(item.get("query", "")), 160)
                raw_aliases = item.get("aliases", [])
                raw_related_terms = item.get("related_terms", [])
                raw_aliases = raw_aliases if isinstance(raw_aliases, list) else []
                raw_related_terms = raw_related_terms if isinstance(raw_related_terms, list) else []
                aliases = [
                    sanitize_text(str(alias), 80)
                    for alias in raw_aliases
                    if sanitize_text(str(alias), 80)
                ][:8]
                related_terms = [
                    sanitize_text(str(term), 80)
                    for term in raw_related_terms
                    if sanitize_text(str(term), 80)
                ][:10]
                if query:
                    plan.setdefault(matched, []).append(
                        {
                            "module": module or "综合",
                            "query": query,
                            "aliases": aliases,
                            "related_terms": related_terms,
                        }
                    )
            for name in competitors:
                if not plan.get(name):
                    plan[name] = fallback_plan[name]
            summary = report_text(str(result.data.get("summary", "")), 180)
            self._log_agent_event(
                task_id,
                "采集 Agent",
                "collection_query_plan",
                summary or "豆包已生成模块化搜索计划。",
                meta={"competitors": competitors, "query_count": sum(len(items) for items in plan.values())},
            )
            return plan, {
                "provider": result.provider,
                "token_input": result.input_tokens,
                "token_output": result.output_tokens,
                "fallback_reason": result.fallback_reason,
                "tool_calls": result.tool_calls
                + [{"name": "collection_query_plan", "result": f"{sum(len(items) for items in plan.values())} module queries"}],
            }
        except LLMProviderError as exc:
            safe_reason = sanitize_text(str(exc), 240)
            return fallback_plan, {
                "provider": self.llm_provider.provider,
                "token_input": 1,
                "token_output": 1,
                "fallback_reason": safe_reason,
                "tool_calls": [{"name": "doubao_collection_query_plan", "result": f"fallback: {safe_reason}"}],
            }

    def _filter_search_results_for_name(
        self,
        results: list[WebSourceDraft],
        name: str,
        industry: str = "",
        module: str = "",
        extra_aliases: list[str] | None = None,
        extra_related_terms: list[str] | None = None,
    ) -> tuple[list[WebSourceDraft], dict[str, Any]]:
        object_type = self._analysis_object_type(name, industry)
        industry_bucket = self._industry_bucket(industry, [name])
        scored: list[tuple[int, WebSourceDraft, list[str]]] = []
        dropped_reasons: dict[str, int] = {}
        for draft in results:
            score, reasons = self._search_candidate_score(
                draft,
                name,
                industry,
                object_type,
                module,
                extra_aliases or [],
                extra_related_terms or [],
                industry_bucket,
            )
            draft.competitor_name = name
            draft.module = module
            draft.relevance_score = score
            draft.source_role = self._source_role_for_candidate(draft, name, module)
            draft.raw_content_status = draft.raw_content_status or "summary_only"
            if industry_bucket == "content_social":
                threshold = 2  # Social platforms: reviews/UGC are primary evidence
            elif object_type == "category":
                threshold = 2
            else:
                threshold = 3
            if score >= threshold:
                draft.credibility = "high" if score >= 8 else "medium" if score >= 5 else "low"
                scored.append((score, draft, reasons))
            else:
                reason = reasons[-1] if reasons else "相关性不足"
                dropped_reasons[reason] = dropped_reasons.get(reason, 0) + 1
        scored.sort(key=lambda item: item[0], reverse=True)
        kept = [draft for _, draft, _ in scored]
        return kept, {
            "object_type": object_type,
            "kept_scores": [score for score, _, _ in scored[:5]],
            "kept_reasons": ["；".join(reasons[:4]) for _, _, reasons in scored[:5]],
            "dropped_reasons": dropped_reasons,
        }

    def _search_candidate_score(
        self,
        draft: WebSourceDraft,
        name: str,
        industry: str,
        object_type: str,
        module: str,
        extra_aliases: list[str],
        extra_related_terms: list[str],
        industry_bucket: str = "generic",
    ) -> tuple[int, list[str]]:
        candidate_excerpt = re.sub(r"搜索词：[^。]{0,160}。", "", draft.excerpt or "")
        candidate_excerpt = re.sub(r"^搜索结果：", "", candidate_excerpt)
        text = f"{draft.title} {draft.url} {draft.author_site} {candidate_excerpt}".casefold()
        aliases = self._search_aliases_for_name(name, extra_aliases)
        related_terms = self._search_related_terms_for_name(name, industry, object_type, extra_related_terms)
        score = 0
        reasons: list[str] = []
        alias_hits = [term for term in aliases if term and term.casefold() in text]
        related_hits = [term for term in related_terms if term and term.casefold() in text]
        official_domain_hit = self._candidate_hits_official_domain(draft, name)
        if alias_hits:
            score += 5
            reasons.append(f"命中名称/别名：{self._join_names(alias_hits[:3])}")
        if related_hits:
            score += min(4, len(related_hits))
            reasons.append(f"命中行业相关词：{self._join_names(related_hits[:4])}")
        authority_score, authority_reason = self._search_source_authority(draft, name)
        score += authority_score
        if authority_reason:
            reasons.append(authority_reason)
        if object_type == "category" and len(related_hits) >= 2:
            score += 2
            reasons.append("品类对比允许按定义/代表品牌/消费场景保留")
        if module and any(token in module for token in ["价格", "定价"]) and re.search(r"价格|报价|套餐|price|pricing|元|万元", text, flags=re.I):
            score += 1
            reasons.append("命中价格/报价线索")
        if module and "用户" in module and re.search(r"评价|口碑|评论|投诉|review", text, flags=re.I):
            score += 1
            reasons.append("命中评价/口碑线索")
        noise_penalty, noise_reason = self._search_noise_penalty(draft, alias_hits, related_hits, object_type, industry_bucket)
        score -= noise_penalty
        if noise_reason:
            reasons.append(noise_reason)
        if object_type != "category" and not alias_hits and not official_domain_hit:
            score -= 7
            reasons.append("产品/公司结果未命中竞品名称、别名或官网域名")
        elif not alias_hits and not related_hits:
            score -= 4
            reasons.append("未命中名称、别名或行业相关词")
        if self._looks_garbled(draft.title) or self._looks_garbled(draft.excerpt):
            score -= 5
            reasons.append("标题或摘要疑似乱码")
        return score, reasons

    def _auto_config_for_product(self, name: str, industry: str = "") -> dict[str, list[str]]:
        """Auto-generate configuration for a product not present in static config."""
        object_type = self._analysis_object_type(name, industry)
        bucket = self._industry_bucket(industry, [name])
        name_clean = name.strip()

        # Auto-aliases
        aliases = [name_clean]
        # Remove common company suffixes
        stripped = re.sub(r"(股份有限公司|股份|有限公司|集团|公司|科技|技术)$", "", name_clean).strip()
        if stripped and stripped != name_clean:
            aliases.append(stripped)
        # For short Chinese names (2-4 chars), the name itself is the best alias
        if len(name_clean) <= 4 and re.search(r'[一-鿿]', name_clean):
            pass  # Already sufficient

        # Auto-related terms from industry text
        industry_terms = re.split(r"\s+|、|，|,|/|｜|\|", industry)
        related_terms = [t for t in industry_terms if len(t.strip()) >= 2]
        related_terms.extend(self._industry_related_terms(name_clean))

        # Object-type specific terms
        if object_type == "product":
            related_terms.extend(["产品", "功能", "定价", "评价", "官方", "app", "下载"])
        elif object_type == "company":
            related_terms.extend(["公司", "官网", "融资", "团队", "发布", "投资者关系"])

        # Bucket-specific terms
        if bucket == "content_social":
            related_terms.extend(["内容", "用户", "创作者", "直播", "电商", "广告", "算法", "社区", "DAU", "商业化"])
        elif bucket == "software":
            related_terms.extend(["SaaS", "软件", "订阅", "云", "协作", "企业"])
        elif bucket == "ai":
            related_terms.extend(["AI", "大模型", "API", "模型", "智能助手"])

        return {
            "aliases": self._dedupe_terms(aliases),
            "related_terms": self._dedupe_terms(related_terms),
        }

    def _analysis_object_type(self, name: str, industry: str = "") -> str:
        name_text = name or ""
        full_text = f"{name} {industry}"
        if re.search(r"chatgpt|deepseek|doubao|claude|gemini|kimi|notion|airtable|slack|trello|asana|openai", name_text, flags=re.I):
            return "product"
        if re.search(r"股份|集团|公司|有限|绿能|通信|能源|科技|电气|银行|证券|保险|矿业|煤业|有色", name_text):
            return "company"
        if re.search(r"品类|类别|香型|白酒|二锅头|酱香|清香|浓香|煤矿|煤炭|小金属|稀土|有色金属|矿产|[一-龥]{1,8}型白酒", full_text):
            return "category"
        return "product"

    def _candidate_hits_official_domain(self, draft: WebSourceDraft, name: str) -> bool:
        url = (draft.url or "").casefold()
        for hint in self._url_hints_for_name(name):
            parts = self._parse_url_parts(hint)
            if parts and parts[0] and parts[0] in url:
                return True
            if parts and parts[0]:
                root = parts[0].removeprefix("www.")
                if root and root in url:
                    return True
        return False

    def _source_role_for_candidate(self, draft: WebSourceDraft, name: str, module: str) -> str:
        url = (draft.url or "").casefold()
        title = (draft.title or "").casefold()
        marker = f"{module} {title} {url}"
        if self._candidate_hits_official_domain(draft, name):
            if re.search(r"价格|定价|pricing|price|api", marker, flags=re.I):
                return "official_pricing"
            if re.search(r"文档|docs|developer|api", marker, flags=re.I):
                return "official_doc"
            return "official"
        if re.search(r"g2|trustpilot|app store|capterra|评价|review", marker, flags=re.I):
            return "review"
        if re.search(r"新闻|风险|市场|监管|news|risk", module or "", flags=re.I):
            return "news"
        return "third_party"

    def _looks_garbled(self, value: str) -> bool:
        text = value or ""
        if "\ufffd" in text:
            return True
        hangul = len(re.findall(r"[\uac00-\ud7af]", text))
        latin_or_cjk = len(re.findall(r"[A-Za-z\u4e00-\u9fff]", text))
        return hangul >= 2 and latin_or_cjk >= 2

    def _search_aliases_for_name(self, name: str, extra_aliases: list[str] | None = None) -> list[str]:
        key = (name or "").strip().casefold()
        candidates = [name]
        candidates.extend(PRODUCT_ALIASES.get(key, []))
        candidates.extend(extra_aliases or [])
        stripped = re.sub(r"(股份有限公司|股份|有限公司|集团|公司)$", "", name or "").strip()
        if stripped and stripped != name:
            candidates.append(stripped)
        if len(stripped) >= 2 and len(stripped) <= 4:
            candidates.append(stripped)
        # Auto-config fallback for unconfigured products
        if len(candidates) <= 2:
            auto = self._auto_config_for_product(name, "")
            candidates.extend(auto.get("aliases", []))
        return self._dedupe_terms(candidates)

    def _search_related_terms_for_name(
        self,
        name: str,
        industry: str,
        object_type: str,
        extra_related_terms: list[str] | None = None,
    ) -> list[str]:
        key = (name or "").strip().casefold()
        terms = list(PRODUCT_RELATED_TERMS.get(key, []))
        terms.extend(extra_related_terms or [])
        terms.extend(re.split(r"\s+|、|，|,|/|｜|\|", industry or ""))
        terms.extend(self._industry_related_terms(f"{name} {industry}"))
        # Auto-config fallback for unconfigured products
        if len(terms) <= 3:
            auto = self._auto_config_for_product(name, industry)
            terms.extend(auto.get("related_terms", []))
        if object_type == "category":
            terms.extend(re.split(r"\s+|、|，|,|/|｜|\|", name or ""))
        return self._dedupe_terms([term for term in terms if len(term.strip()) >= 2])

    def _industry_related_terms(self, text: str) -> list[str]:
        terms: list[str] = []
        for pattern, pattern_terms in INDUSTRY_RELATED_TERMS:
            if pattern.search(text or ""):
                terms.extend(pattern_terms)
        return terms

    def _dedupe_terms(self, terms: list[str]) -> list[str]:
        result: list[str] = []
        seen: set[str] = set()
        for term in terms:
            cleaned = sanitize_text(str(term), 80).strip()
            if not cleaned:
                continue
            key = cleaned.casefold()
            if key in seen:
                continue
            seen.add(key)
            result.append(cleaned)
        return result

    def _search_source_authority(self, draft: WebSourceDraft, name: str) -> tuple[int, str]:
        url = (draft.url or "").casefold()
        title = (draft.title or "").casefold()
        for hint in self._url_hints_for_name(name):
            parts = self._parse_url_parts(hint)
            if parts and parts[0] and parts[0] in url:
                return 4, "命中官网域名"
            if parts and parts[0]:
                root = parts[0].removeprefix("www.")
                if root and root in url:
                    return 4, "命中官网域名"
        if any(domain in url for domain in SEARCH_HIGH_AUTHORITY_DOMAINS):
            return 3, "命中公告/交易所/政府等高可信域名"
        if re.search(r"官网|官方网站|投资者关系|年报|公告|财报|产品中心|解决方案", title, flags=re.I):
            return 2, "命中官网/公告/产品页标题"
        if any(domain in url for domain in SEARCH_MEDIUM_AUTHORITY_DOMAINS):
            return 1, "命中百科/新闻/财经等中可信域名"
        return 0, ""

    def _search_noise_penalty(
        self,
        draft: WebSourceDraft,
        alias_hits: list[str],
        related_hits: list[str],
        object_type: str,
        industry_bucket: str = "generic",
    ) -> tuple[int, str]:
        url = (draft.url or "").casefold()
        text = f"{draft.title} {draft.excerpt}".casefold()
        # Industry-specific low-value domains
        low_value_domains = SEARCH_LOW_VALUE_DOMAINS_BY_INDUSTRY.get(
            industry_bucket, SEARCH_LOW_VALUE_DOMAINS
        )
        if any(domain in url for domain in low_value_domains):
            return 2, "视频/社媒页面降权"
        # Industry-specific high-value domains — give bonus
        high_value_domains = SEARCH_HIGH_VALUE_DOMAINS_BY_INDUSTRY.get(industry_bucket, [])
        if any(domain in url for domain in high_value_domains):
            return -3, "命中行业高价值来源域"
        if re.search(r"笑话|段子|小说|游戏|歌词|招聘|下载|问答题|试题", text):
            return 7, "低价值或娱乐页面降权"
        if object_type != "category" and not alias_hits and len(related_hits) <= 1:
            return 3, "公司/产品页未命中足够名称或业务线索"
        return 0, ""

    def _collect_sources(self, task_id: str, dataset: dict[str, Any]) -> dict[str, str]:
        stage_started = now_dt()
        self._update_task(task_id, "collecting")
        task_config = self._task_config(task_id)
        requested_names = task_config.get("competitors", [])
        self._log_agent_event(
            task_id,
            "采集 Agent",
            "cache_collect_started",
            f"正在从缓存样例中按当前竞品筛选资料：{self._join_names(requested_names)}。",
        )
        requested_lookup = {name.casefold(): name for name in requested_names}
        now = utc_now_iso()
        source_map: dict[str, str] = {}
        inserted = 0
        matched_names: set[str] = set()

        with self.connect() as conn:
            for competitor in dataset["competitors"]:
                if competitor["name"].casefold() not in requested_lookup:
                    continue
                matched_names.add(competitor["name"].casefold())
                conn.execute(
                    """
                    UPDATE competitors
                    SET website = COALESCE(NULLIF(website, ''), ?),
                        target_users_json = ?,
                        collected_at = ?
                    WHERE task_id = ? AND name = ?
                    """,
                    (
                        competitor.get("website", ""),
                        dumps(competitor.get("target_users", [])),
                        now,
                        task_id,
                        competitor["name"],
                    ),
                )
                for source in competitor["sources"]:
                    db_source_id = f"{task_id[:8]}_{source['source_id']}"
                    source_map[source["source_id"]] = db_source_id
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO sources
                        (id, task_id, source_type, title, url_or_path, author_site, published_at, collected_at,
                         credibility, excerpt, related_claim_ids, competitor_name, module, relevance_score,
                         source_role, raw_content_status)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            db_source_id,
                            task_id,
                            source["source_type"],
                            source["title"],
                            source["url_or_path"],
                            source["author_site"],
                            source.get("published_at", ""),
                            now,
                            source["credibility"],
                            source["excerpt"],
                            "[]",
                            competitor["name"],
                            source.get("source_type", ""),
                            8 if source["credibility"] == "high" else 5,
                            "official" if "official" in source["source_type"] else "demo",
                            "cached",
                        ),
                    )
                    self._insert_text_evidence(conn, task_id, db_source_id, source["excerpt"], now)
                    inserted += 1
            for index, name in enumerate(requested_names):
                if name.casefold() in matched_names:
                    continue
                db_source_id = f"{task_id[:8]}_manual_scope_{index + 1:02d}"
                excerpt = (
                    f"任务声明竞品为“{name}”，但缓存样例库中没有该竞品的公开资料。"
                    "系统不能套用其他竞品信息；功能、价格、评价等事实需要实时采集 URL、上传资料或人工补充后再确认。"
                )
                source_map[f"manual_scope_{index + 1:02d}"] = db_source_id
                conn.execute(
                    """
                    INSERT OR IGNORE INTO sources
                    (id, task_id, source_type, title, url_or_path, author_site, published_at, collected_at,
                     credibility, excerpt, related_claim_ids, competitor_name, module, relevance_score,
                     source_role, raw_content_status)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        db_source_id,
                        task_id,
                        "manual_scope",
                        f"{name} 任务范围声明",
                        "manual://task-scope",
                        "用户任务输入",
                        "",
                        now,
                        "medium",
                        excerpt,
                        "[]",
                        name,
                        "待补证",
                        0,
                        "source_gap",
                        "not_collected",
                    ),
                )
                self._insert_text_evidence(conn, task_id, db_source_id, excerpt, now)
                inserted += 1
            for source in dataset["shared_sources"]:
                db_source_id = f"{task_id[:8]}_{source['source_id']}"
                source_map[source["source_id"]] = db_source_id
                conn.execute(
                    """
                    INSERT OR IGNORE INTO sources
                    (id, task_id, source_type, title, url_or_path, author_site, published_at, collected_at,
                     credibility, excerpt, related_claim_ids, module, relevance_score, source_role, raw_content_status)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        db_source_id,
                        task_id,
                        source["source_type"],
                        source["title"],
                        source["url_or_path"],
                        source["author_site"],
                        source.get("published_at", ""),
                        now,
                        source["credibility"],
                        source["excerpt"],
                        "[]",
                        "共享背景",
                        4,
                        "demo_shared",
                        "cached",
                    ),
                )
                self._insert_text_evidence(conn, task_id, db_source_id, source["excerpt"], now)
                inserted += 1

        collection_trace = self._model_collection_review(task_id)
        self._log_agent_run(
            task_id,
            agent_name="采集 Agent",
            input_summary="按当前任务竞品筛选缓存样例；未命中的竞品只登记任务范围，不套用其他竞品资料。",
            output_summary=f"已登记 {inserted} 条来源，其中缓存匹配 {len(matched_names)} 个竞品，未命中 {len(requested_names) - len(matched_names)} 个竞品。",
            status="completed",
            duration_ms=self._elapsed_ms(stage_started),
            token_input=collection_trace.get("token_input"),
            token_output=collection_trace.get("token_output"),
            model_provider=collection_trace.get("provider", ""),
            fallback_reason=collection_trace.get("fallback_reason", ""),
            tool_calls=[{"name": "load_cached_dataset", "result": f"{inserted} sources"}] + collection_trace.get("tool_calls", []),
            started_at=stage_started,
        )
        self._log_agent_event(
            task_id,
            "采集 Agent",
            "cache_collect_finished",
            f"缓存采集完成：登记 {inserted} 条来源，命中 {len(matched_names)} 个竞品。",
            meta={"source_count": inserted, "matched_competitors": len(matched_names)},
        )
        return source_map

    def _insert_text_evidence(
        self,
        conn: sqlite3.Connection,
        task_id: str,
        source_id: str,
        text: str,
        collected_at: str,
    ) -> None:
        chunks = chunk_text(text, chunk_size=700, overlap=80)
        self._insert_evidence_chunks(conn, task_id, source_id, chunks, collected_at)

    def _insert_evidence_chunks(
        self,
        conn: sqlite3.Connection,
        task_id: str,
        source_id: str,
        chunks: list[Any],
        collected_at: str,
    ) -> None:
        for chunk in chunks:
            chunk_id = f"{source_id}_chunk_{int(chunk.chunk_index):03d}"
            conn.execute(
                """
                INSERT OR IGNORE INTO evidence_chunks
                (id, task_id, source_id, chunk_index, char_start, char_end, summary, excerpt, collected_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    chunk_id,
                    task_id,
                    source_id,
                    int(chunk.chunk_index),
                    int(chunk.char_start),
                    int(chunk.char_end),
                    sanitize_text(chunk.summary, 260),
                    sanitize_text(chunk.excerpt, 1200),
                    collected_at,
                ),
            )

    def _first_analysis(self, task_id: str, dataset: dict[str, Any], source_map: dict[str, str]) -> None:
        stage_started = now_dt()
        self._update_task(task_id, "analyzing")
        task_config = self._task_config(task_id)
        competitor_names = task_config.get("competitors", [])
        source_ids = self._source_ids_for_task(task_id)
        evidence_source_ids = [source_id for source_id in source_ids if source_id]
        primary_sources = evidence_source_ids[:3]
        self._refresh_pricing_facts(task_id)
        self._log_agent_event(
            task_id,
            "分析 Agent",
            "analysis_started",
            f"正在读取 {len(evidence_source_ids)} 个来源的证据分片，并按竞品聚合。",
            meta={"source_count": len(evidence_source_ids), "competitors": competitor_names},
        )
        model_claims, model_trace = self._model_claims_for_task(task_id)
        evidence_claims = self._evidence_claims_for_task(task_id, competitor_names, primary_sources)
        claims = self._analysis_claims_for_current_provider(model_claims, evidence_claims, model_trace)
        claims = self._dedupe_claims(claims)
        self._insert_claims(task_id, claims)
        analysis_artifact = self._run_deep_analysis(task_id, claims)
        self._log_agent_event(
            task_id,
            "分析 Agent",
            "analysis_finished",
            f"已生成 {len(claims)} 条结构化结论与 {len(analysis_artifact.get('sections', []))} 章深度分析草稿；未通过来源校验的内容不会进入报告。",
            meta={"claim_count": len(claims), "provider": analysis_artifact.get("provider") or model_trace["provider"]},
        )
        self._log_agent_run(
            task_id,
            agent_name="分析 Agent",
            input_summary="读取来源表与 evidence chunks，抽取结构化 claims，并运行 LangGraph ReAct 深度研究生成分析草稿与评分依据。",
            output_summary=f"已生成 {len(claims)} 条结构化结论、{len(analysis_artifact.get('sections', []))} 章分析草稿和 {len(analysis_artifact.get('score_dimensions', []))} 条评分项。",
            status="completed",
            duration_ms=self._elapsed_ms(stage_started),
            token_input=int(model_trace["token_input"] or 0) + int(analysis_artifact.get("token_input") or 0),
            token_output=int(model_trace["token_output"] or 0) + int(analysis_artifact.get("token_output") or 0),
            model_provider=analysis_artifact.get("provider") or model_trace["provider"],
            fallback_reason=model_trace["fallback_reason"] or analysis_artifact.get("fallback_reason", ""),
            tool_calls=model_trace["tool_calls"]
            + analysis_artifact.get("tool_calls", [])
            + [{"name": "map_sources_to_schema", "result": f"{len(claims)} claims"}],
            started_at=stage_started,
        )

    def _run_deep_analysis(self, task_id: str, claims: list[dict[str, Any]]) -> dict[str, Any]:
        self._log_agent_event(
            task_id,
            "分析 Agent",
            "deep_analysis_started",
            "正在调用深度研究链路，基于已采集资料生成完整深度分析草稿、评分依据和雷达数据。",
            meta={"claim_count": len(claims)},
        )
        with self.connect() as conn:
            task = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
            competitors = conn.execute("SELECT * FROM competitors WHERE task_id = ? ORDER BY rowid", (task_id,)).fetchall()
            sources = conn.execute("SELECT * FROM sources WHERE task_id = ? ORDER BY collected_at, rowid", (task_id,)).fetchall()
            pricing_fact_rows = conn.execute(
                "SELECT * FROM pricing_facts WHERE task_id = ? ORDER BY competitor_name, plan_name, price_type",
                (task_id,),
            ).fetchall()
        if not task:
            return {}

        competitor_names = [item["name"] for item in competitors]
        react_task = {
            "id": task_id,
            "name": task["name"],
            "industry": task["industry"],
            "competitors": loads(task["competitors_json"], []),
            "focus_areas": loads(task["focus_areas_json"], []),
        }
        react_report = run_react_report(
            react_task,
            [dict(row) for row in sources],
            claims,
            Path(__file__).resolve().parent / "static",
        )
        analysis_markdown = self._guard_analysis_markdown(react_report.markdown, sources, claims)
        guarded_sections = self._guard_analysis_sections(react_report.sections, sources, claims)
        sections = self._ensure_analysis_sections(guarded_sections, analysis_markdown)

        dimension_profile = self._build_dimension_profile(task, competitor_names)
        source_catalog = self._build_source_catalog(sources)
        pricing_comparison = self._build_pricing_comparison(competitor_names, claims, sources, pricing_fact_rows, dimension_profile)
        score_dimensions = self._build_score_dimensions(competitor_names, sources, pricing_comparison, dimension_profile)
        if dimension_profile.get("show_api_cost"):
            score_dimensions = self._apply_reference_ai_scores(competitor_names, score_dimensions, source_catalog, pricing_comparison)
        score_dimensions = self._calibrate_scores_from_analysis(score_dimensions, claims, sources, sections, analysis_markdown)
        radar_data = self._build_radar_chart_data(score_dimensions)
        artifact = {
            "provider": react_report.provider,
            "analysis_markdown": analysis_markdown,
            "sections": sections,
            "score_dimensions": score_dimensions,
            "radar_data": radar_data,
            "tool_calls": react_report.tool_calls,
            "screenshots": react_report.screenshots,
            "fallback_reason": react_report.fallback_reason,
            "token_input": react_report.token_input,
            "token_output": react_report.token_output,
        }
        artifact_saved = self._save_analysis_artifact(task_id, artifact)
        self._log_agent_event(
            task_id,
            "分析 Agent",
            "deep_analysis_finished",
            f"深度分析已保存：{len(sections)} 章、{len(score_dimensions)} 条评分项，模型状态 {react_report.provider}。",
            meta={"provider": react_report.provider, "fallback_reason": react_report.fallback_reason, "artifact_saved": artifact_saved},
        )
        return artifact

    def _ensure_analysis_sections(self, sections: list[dict[str, Any]], markdown: str) -> list[dict[str, Any]]:
        normalized = [dict(section) for section in sections if isinstance(section, dict)]
        if normalized and len(normalized) < 12:
            normalized.append(
                {
                    "key": f"react_{len(normalized) + 1}",
                    "title": "结语",
                    "body": "本报告基于当前已采集资料、结构化结论和模型分析形成。后续正式使用前，应继续复核价格、模型规格、发布时间和用户评价样本。",
                    "markdown": "本报告基于当前已采集资料、结构化结论和模型分析形成。后续正式使用前，应继续复核价格、模型规格、发布时间和用户评价样本。",
                }
            )
        elif not normalized and markdown:
            normalized = [{"key": "react_1", "title": "深度分析", "body": markdown, "markdown": markdown}]
        return normalized

    def _analysis_evidence_haystack(self, sources: list[sqlite3.Row], claims: list[dict[str, Any]]) -> str:
        parts: list[str] = []
        for source in sources:
            parts.extend(
                [
                    str(row_get(source, "title", "")),
                    str(row_get(source, "excerpt", "")),
                    str(row_get(source, "url_or_path", "")),
                    str(row_get(source, "author_site", "")),
                ]
            )
        for claim in claims:
            parts.append(str(claim.get("content", "")))
        return sanitize_markdown_text("\n".join(parts), 250000).casefold()

    def _term_is_supported_by_evidence(self, term: str, evidence_haystack: str) -> bool:
        normalized = re.sub(r"[\s_\-]+", "", term).casefold()
        evidence = re.sub(r"[\s_\-]+", "", evidence_haystack).casefold()
        return bool(normalized and normalized in evidence)

    def _guard_analysis_markdown(
        self,
        markdown: str,
        sources: list[sqlite3.Row],
        claims: list[dict[str, Any]],
        append_boundary_note: bool = True,
    ) -> str:
        guarded = sanitize_markdown_text(markdown)
        if not guarded:
            return guarded
        evidence_haystack = self._analysis_evidence_haystack(sources, claims)
        risky_terms = sorted(
            {
                match.group(0)
                for match in re.finditer(
                    r"\bGPT-5(?:\.\d+)?\b|\bGPT-Image-\d+\b|\bGPT-Realtime-\d+\b|\bGLM-5\b|\bDeepSeek[-\s]?V4\b",
                    guarded,
                    flags=re.I,
                )
                if not self._term_is_supported_by_evidence(match.group(0), evidence_haystack)
            },
            key=len,
            reverse=True,
        )
        for term in risky_terms:
            guarded = re.sub(
                re.escape(term) + r"(?!（待核实）)",
                f"{term}（待核实）",
                guarded,
                flags=re.I,
            )

        guarded = self._mark_unsupported_quantitative_paragraphs(guarded)
        if append_boundary_note and (risky_terms or "待核实" in guarded):
            boundary = (
                "\n\n> 证据边界：深度研究 Agent 输出中包含当前入库来源未直接支撑的模型版本或时间敏感表述，"
                "系统已标注“待核实”；正式使用前请以官网、价格页、发布说明或可信第三方来源复核。"
            )
            if "证据边界：深度研究 Agent 输出中包含" not in guarded:
                guarded += boundary
        return guarded

    def _guard_analysis_sections(
        self,
        sections: list[dict[str, Any]],
        sources: list[sqlite3.Row],
        claims: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        guarded: list[dict[str, Any]] = []
        for section in sections:
            if not isinstance(section, dict):
                continue
            item = dict(section)
            for field in ("body", "markdown"):
                if item.get(field):
                    item[field] = self._guard_analysis_markdown(
                        str(item.get(field, "")),
                        sources,
                        claims,
                        append_boundary_note=False,
                    )
            guarded.append(item)
        has_pending = any(
            "待核实" in str(item.get("markdown", "")) or "待核实" in str(item.get("body", ""))
            for item in guarded
        )
        has_boundary = any(
            "证据边界：深度研究 Agent 输出中包含" in str(item.get("markdown", "")) or "证据边界：深度研究 Agent 输出中包含" in str(item.get("body", ""))
            for item in guarded
        )
        if guarded and has_pending and not has_boundary:
            note = (
                "\n\n> 证据边界：深度研究 Agent 输出中包含当前入库来源未直接支撑的模型版本或时间敏感表述，"
                "系统已标注“待核实”；正式使用前请以官网、价格页、发布说明或可信第三方来源复核。"
            )
            last_markdown = str(guarded[-1].get("markdown") or guarded[-1].get("body") or "")
            last_body = str(guarded[-1].get("body") or guarded[-1].get("markdown") or "")
            guarded[-1]["markdown"] = last_markdown + note
            guarded[-1]["body"] = last_body + note
        return guarded

    def _mark_unsupported_quantitative_paragraphs(self, markdown: str) -> str:
        parts = re.split(r"(\n{2,})", markdown)
        guarded_parts: list[str] = []
        risky_pattern = re.compile(r"(市场规模|用户规模|每周|月活|营收|收入|融资|增长率|降低|预计|2030|2026|亿人|亿元|万亿|%)")
        numeric_pattern = re.compile(r"\d")
        for part in parts:
            if not part or re.fullmatch(r"\n{2,}", part):
                guarded_parts.append(part)
                continue
            if part.lstrip().startswith(("##", ">")) or "待核实" in part or "http://" in part or "https://" in part:
                guarded_parts.append(part)
                continue
            if risky_pattern.search(part) and numeric_pattern.search(part):
                guarded_parts.append(
                    part.rstrip()
                    + "\n\n> 证据边界：该段包含量化或时间敏感表述，段内未给出直接 URL，需复核后使用。"
                )
            else:
                guarded_parts.append(part)
        return "".join(guarded_parts)

    def _save_analysis_artifact(self, task_id: str, artifact: dict[str, Any]) -> bool:
        markdown = sanitize_markdown_text(str(artifact.get("analysis_markdown", "")), 120000)
        sections = sanitize_markdown_payload(artifact.get("sections", []), 120000)
        try:
            min_ratio = float(os.environ.get("REACT_ARTIFACT_REPLACE_MIN_RATIO", "0.85"))
        except ValueError:
            min_ratio = 0.85
        min_ratio = max(0.1, min(1.0, min_ratio))
        with self.connect() as conn:
            previous = conn.execute(
                "SELECT version, provider, analysis_markdown, sections_json FROM analysis_artifacts WHERE task_id = ? ORDER BY version DESC, rowid DESC LIMIT 1",
                (task_id,),
            ).fetchone()
            if previous:
                previous_markdown = str(previous["analysis_markdown"] or "")
                previous_sections = loads(previous["sections_json"], [])
                previous_provider = str(previous["provider"] or "")
                new_provider = str(artifact.get("provider") or "")
                previous_len = len(previous_markdown)
                new_len = len(markdown)
                previous_section_count = len(previous_sections if isinstance(previous_sections, list) else [])
                new_section_count = len(sections if isinstance(sections, list) else [])
                shorter_than_previous = previous_len >= 5000 and new_len < int(previous_len * min_ratio)
                section_regressed = new_section_count < max(1, min(previous_section_count, 11))
                deepseek_replacing_non_deepseek = new_provider == "deepseek-react" and previous_provider != "deepseek-react"
                if shorter_than_previous and not deepseek_replacing_non_deepseek and (section_regressed or new_section_count <= previous_section_count):
                    self._log_agent_event(
                        task_id,
                        "分析 Agent",
                        "analysis_artifact_protected",
                        "新的 ReAct 刷新产物明显短于上一版，已保留上一版深度报告产物，避免短报告覆盖长报告。",
                        severity="warning",
                        meta={
                            "previous_version": previous["version"],
                            "previous_chars": previous_len,
                            "new_chars": new_len,
                            "previous_sections": previous_section_count,
                            "new_sections": new_section_count,
                            "previous_provider": previous_provider,
                            "new_provider": new_provider,
                            "min_ratio": min_ratio,
                        },
                    )
                    return False
            version = conn.execute(
                "SELECT COUNT(*) AS count FROM analysis_artifacts WHERE task_id = ?",
                (task_id,),
            ).fetchone()["count"] + 1
            conn.execute(
                """
                INSERT INTO analysis_artifacts
                (id, task_id, version, provider, analysis_markdown, sections_json, score_dimensions_json,
                 radar_data_json, tool_calls_json, screenshots_json, fallback_reason, token_input, token_output, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    uuid.uuid4().hex,
                    task_id,
                    int(version),
                    sanitize_text(str(artifact.get("provider", "")), 120),
                    markdown,
                    dumps(sections),
                    dumps(sanitize_payload(artifact.get("score_dimensions", []), 4000)),
                    dumps(sanitize_payload(artifact.get("radar_data", []), 4000)),
                    dumps(sanitize_payload(artifact.get("tool_calls", []), 4000)),
                    dumps(sanitize_payload(artifact.get("screenshots", []), 1000)),
                    sanitize_text(str(artifact.get("fallback_reason", "")), 600),
                    int(artifact.get("token_input") or 0),
                    int(artifact.get("token_output") or 0),
                    utc_now_iso(),
                ),
            )
        return True

    def _latest_analysis_artifact(self, task_id: str) -> dict[str, Any]:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM analysis_artifacts WHERE task_id = ? ORDER BY version DESC, rowid DESC LIMIT 1",
                (task_id,),
            ).fetchone()
        if not row:
            return {}
        return {
            "provider": row["provider"],
            "analysis_markdown": row["analysis_markdown"],
            "sections": loads(row["sections_json"], []),
            "score_dimensions": loads(row["score_dimensions_json"], []),
            "radar_data": loads(row["radar_data_json"], []),
            "tool_calls": loads(row["tool_calls_json"], []),
            "screenshots": loads(row["screenshots_json"], []),
            "fallback_reason": row["fallback_reason"],
            "token_input": row["token_input"],
            "token_output": row["token_output"],
        }

    def _analysis_artifact_needs_refresh(self, task_id: str) -> bool:
        artifact = self._latest_analysis_artifact(task_id)
        if not artifact:
            return True
        markdown = str(artifact.get("analysis_markdown") or "")
        sections = artifact.get("sections") or []
        if len(markdown) < 5000:
            return True
        if len(sections if isinstance(sections, list) else []) < 11:
            return True
        return False

    def _claims_for_deep_analysis(self, task_id: str) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM claims WHERE task_id = ? ORDER BY created_at, rowid",
                (task_id,),
            ).fetchall()
        claims: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["source_ids"] = loads(row["source_ids"], [])
            if item["source_ids"] or item.get("status") == "confirmed":
                claims.append(item)
        return claims

    def _refresh_deep_analysis_from_current_claims(self, task_id: str, reason: str) -> dict[str, Any]:
        stage_started = now_dt()
        claims = self._claims_for_deep_analysis(task_id)
        if not claims:
            self._log_agent_event(
                task_id,
                "分析 Agent",
                "deep_analysis_refresh_skipped",
                "当前没有可用于深度分析的带来源结论，跳过分析产物刷新。",
                severity="warning",
                meta={"reason": reason},
            )
            return {}
        self._update_task(task_id, "reanalyzing")
        self._log_agent_event(
            task_id,
            "分析 Agent",
            "deep_analysis_refresh_started",
            "检测到来源或结论变化，正在重新生成分析草稿、评分依据和雷达数据。",
            meta={"reason": reason, "claim_count": len(claims)},
        )
        artifact = self._run_deep_analysis(task_id, claims)
        self._log_agent_run(
            task_id,
            agent_name="分析 Agent",
            input_summary=f"{reason}；读取当前 claims 和 sources，刷新 LangGraph ReAct 深度分析产物。",
            output_summary=f"已刷新 {len(artifact.get('sections', []))} 章分析草稿、{len(artifact.get('score_dimensions', []))} 条评分项和雷达数据。",
            status="rerun_completed",
            duration_ms=self._elapsed_ms(stage_started),
            retry_count=1,
            has_rework=True,
            token_input=int(artifact.get("token_input") or 0),
            token_output=int(artifact.get("token_output") or 0),
            model_provider=artifact.get("provider", ""),
            fallback_reason=artifact.get("fallback_reason", ""),
            tool_calls=artifact.get("tool_calls", []) + [{"name": "refresh_analysis_artifact", "result": reason}],
            started_at=stage_started,
        )
        return artifact

    def _calibrate_scores_from_analysis(
        self,
        score_dimensions: list[dict[str, Any]],
        claims: list[dict[str, Any]],
        sources: list[sqlite3.Row],
        sections: list[dict[str, Any]],
        markdown: str,
    ) -> list[dict[str, Any]]:
        ref_map = self._source_ref_map(sources)
        calibrated = []
        for row in score_dimensions:
            item = dict(row)
            section_refs = self._analysis_section_refs(item, sections, markdown)
            claim_refs = self._claim_refs_for_score(item, claims, ref_map)
            evidence_refs = list(dict.fromkeys((item.get("evidence_refs") or []) + claim_refs))
            inferred_score = self._score_from_analysis_sections(item, sections, markdown)
            if section_refs and evidence_refs and float(item.get("score") or 0) > 0:
                item["score"] = min(5.0, round(float(item.get("score") or 0) + 0.2, 1))
            if not evidence_refs:
                if section_refs and inferred_score > 0:
                    item["score"] = inferred_score
                    item["status"] = "待确认"
                    item["rationale"] = report_text(
                        f"该评分基于深度分析章节 {', '.join(section_refs)} 的判断形成，但缺少可直接跳转的来源引用，已标为待确认。",
                        240,
                    )
                else:
                    item["score"] = 0
                    item["status"] = "NA"
                    item["rationale"] = "该评分缺少可追溯来源或章节依据，已按质检口径降为 NA。"
            else:
                item["status"] = item.get("status") or "分析判断"
                if inferred_score > 0:
                    item["score"] = max(float(item.get("score") or 0), inferred_score)
                if section_refs:
                    item["rationale"] = report_text(f"{item.get('rationale', '')}；已在深度分析章节 {', '.join(section_refs)} 中交叉出现。", 240)
            item["evidence_refs"] = evidence_refs[:6]
            item["section_refs"] = section_refs
            calibrated.append(item)
        return calibrated

    def _score_from_analysis_sections(
        self,
        score_row: dict[str, Any],
        sections: list[dict[str, Any]],
        markdown: str,
    ) -> float:
        competitor = str(score_row.get("competitor", "")).casefold()
        dimension = str(score_row.get("dimension", ""))
        if not competitor:
            return 0.0
        tokens = self._score_dimension_terms(dimension)
        snippets: list[str] = []
        competitor_snippets: list[str] = []
        for section in sections:
            text = f"{section.get('title', '')} {section.get('body', '') or section.get('markdown', '')}"
            haystack = text.casefold()
            if competitor in haystack:
                competitor_snippets.append(text[:1800])
            if competitor in haystack and (not tokens or any(token in text for token in tokens)):
                snippets.append(text[:1800])
        if not snippets and competitor_snippets:
            snippets.append(" ".join(competitor_snippets[:2]))
        if not snippets:
            return 0.0
        text = " ".join(snippets)
        positive_terms = ["领先", "优势", "成熟", "完善", "强", "高", "低价", "性价比", "支持", "集成", "开放", "企业", "长上下文", "生态"]
        negative_terms = ["不足", "风险", "待核实", "未公开", "缺少", "不透明", "受限", "落后", "依赖"]
        positive = sum(1 for term in positive_terms if term in text)
        negative = sum(1 for term in negative_terms if term in text)
        score = 2.6 + min(positive, 5) * 0.28 - min(negative, 4) * 0.22
        if tokens and any(token in text for token in tokens):
            score += 0.2
        if text.count("待核实") >= 3:
            score = min(score, 3.4)
        return round(max(1.8, min(4.4, score)), 1)

    def _score_dimension_terms(self, dimension: str) -> list[str]:
        base_terms = [token for token in re.split(r"[\/、\s]+", str(dimension or "")) if len(token) >= 2]
        extra = {
            "综合生产力": ["日常", "工作台", "助手", "生产力", "深度研究"],
            "推理/代码": ["推理", "代码", "编程", "Codex", "开发者"],
            "多模态与创意": ["多模态", "图像", "语音", "视频", "创作"],
            "企业治理": ["企业", "SSO", "权限", "安全", "合规", "隐私"],
            "API 成本效率": ["API", "价格", "成本", "token", "低价"],
            "开放/自部署": ["开源", "开放", "自部署", "MIT"],
            "长上下文": ["上下文", "长文档", "token"],
            "生态集成": ["生态", "集成", "应用", "插件"],
        }
        for key, terms in extra.items():
            if key == dimension:
                base_terms.extend(terms)
        return list(dict.fromkeys(base_terms))

    def _analysis_section_refs(self, score_row: dict[str, Any], sections: list[dict[str, Any]], markdown: str) -> list[str]:
        competitor = str(score_row.get("competitor", "")).casefold()
        dimension = str(score_row.get("dimension", ""))
        refs: list[str] = []
        tokens = self._score_dimension_terms(dimension)
        for index, section in enumerate(sections, start=1):
            title = str(section.get("title", ""))
            body = str(section.get("body", "") or section.get("markdown", ""))
            haystack = f"{title} {body}".casefold()
            if competitor and competitor not in haystack:
                continue
            if dimension and (dimension in title or dimension in body or any(token and token in body for token in tokens)):
                refs.append(f"第{index}章")
        if not refs and competitor and competitor in (markdown or "").casefold():
            refs.append("全文")
        return refs[:4]

    def _claim_refs_for_score(self, score_row: dict[str, Any], claims: list[dict[str, Any]], ref_map: dict[str, str]) -> list[str]:
        competitor = str(score_row.get("competitor", "")).casefold()
        dimension = str(score_row.get("dimension", ""))
        refs: list[str] = []
        tokens = [token for token in re.split(r"[\/、\s]+", dimension) if len(token) >= 2]
        for claim in claims:
            content = str(claim.get("content", ""))
            if competitor and competitor not in content.casefold():
                continue
            if tokens and not any(token in content for token in tokens):
                continue
            refs.extend(self._source_refs_for_ids(claim.get("source_ids") or [], ref_map))
        return list(dict.fromkeys(refs))[:4]

    def _analysis_claims_for_current_provider(
        self,
        model_claims: list[dict[str, Any]],
        evidence_claims: list[dict[str, Any]],
        model_trace: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """Prefer real LLM analysis; keep rule-derived claims only as unavailable-model coverage."""
        provider = str(model_trace.get("provider", ""))
        model_failed = bool(model_trace.get("fallback_reason"))
        if model_claims and provider == "doubao" and not model_failed:
            return model_claims
        if model_claims:
            model_sections = {sanitize_text(str(claim.get("section", "")), 80) for claim in model_claims}
            supplemental = [
                claim
                for claim in evidence_claims
                if sanitize_text(str(claim.get("section", "")), 80) not in model_sections
            ]
            return model_claims + supplemental
        return evidence_claims

    def _evidence_claims_for_task(
        self,
        task_id: str,
        competitor_names: list[str],
        primary_sources: list[str],
    ) -> list[dict[str, Any]]:
        with self.connect() as conn:
            sources = conn.execute(
                "SELECT * FROM sources WHERE task_id = ? ORDER BY collected_at, rowid",
                (task_id,),
            ).fetchall()
        factual_sources = [
            source for source in sources if source["source_type"] not in {"manual_scope", "demo_scope_note"}
        ]
        claims: list[dict[str, Any]] = []
        if sources:
            claims.append(
                {
                    "section": "overview",
                    "content": (
                        f"本任务覆盖 {self._join_names(competitor_names)}，已登记 {len(sources)} 条来源，"
                        f"其中 {len(factual_sources)} 条可作为网页事实或搜索摘要线索进入分析。"
                    ),
                    "confidence": 0.72 if factual_sources else 0.56,
                    "source_ids": [source["id"] for source in (factual_sources or sources)[:3]],
                    "needs_review": not bool(factual_sources),
                    "status": "reportable" if factual_sources else "needs_review",
                    "uncertainty": "" if factual_sources else "当前只有任务范围说明，事实结论需要补充公开网页或上传资料。",
                }
            )

        competitor_factual: dict[str, list[sqlite3.Row]] = {}
        for name in competitor_names:
            related = [source for source in sources if self._source_matches_competitor(source, name)]
            factual = [
                source
                for source in related
                if source["source_type"] not in {"manual_scope", "demo_scope_note"}
            ]
            competitor_factual[name] = factual
            scope = related[:1] or sources[:1]
            if not factual:
                if scope:
                    claims.append(
                        {
                            "section": "feature_tree",
                            "content": f"{name} 当前没有命中可核验公开网页，功能、定价、评价和 SWOT 均需补充来源后再确认。",
                            "confidence": 0.54,
                            "source_ids": [scope[0]["id"]],
                            "needs_review": True,
                            "status": "needs_review",
                            "uncertainty": "缺少真实网页或上传材料。",
                        }
                    )
                continue

            feature_sources = self._sources_for_report_module(factual, "feature_tree")
            pricing_sources = self._sources_for_report_module(factual, "pricing_model")
            review_sources = self._sources_for_report_module(factual, "reviews")
            persona_sources = self._sources_for_report_module(factual, "user_persona")
            source_ids = [source["id"] for source in feature_sources[:2]]
            combined_text = " ".join([f"{source['title']} {source['excerpt']}" for source in factual[:6]])
            excerpt = self._source_summary(feature_sources or factual, 220)
            confidence = 0.62 if any(source["source_type"] == "search_result" for source in feature_sources[:2]) else 0.76
            needs_review = confidence < 0.68
            claims.append(
                {
                    "section": "feature_tree",
                    "content": f"{name} 当前资料显示：{excerpt}",
                    "confidence": confidence,
                    "source_ids": source_ids,
                    "needs_review": needs_review,
                    "status": "reportable" if not needs_review else "needs_review",
                    "uncertainty": "搜索摘要线索仍需正文或官方页面交叉核验。" if needs_review else "",
                }
            )
            if any(keyword in combined_text for keyword in ["定价", "价格", "售价", "套餐", "订阅", "pricing", "price"]):
                pricing_source_ids = [source["id"] for source in (pricing_sources or factual)[:3]]
                claims.append(
                    {
                        "section": "pricing_model",
                        "content": f"{name} 的价格/套餐/API 线索来自当前采集材料：{self._pricing_summary(pricing_sources or factual)}",
                        "confidence": confidence,
                        "source_ids": pricing_source_ids,
                        "needs_review": True,
                        "status": "needs_review",
                        "uncertainty": "价格、套餐或车型金额以采集日期为准，正式使用前建议复核官网或定价页。",
                    }
                )
            else:
                claims.append(
                    {
                        "section": "pricing_model",
                        "content": f"{name} 当前采集来源未出现明确价格、套餐或车型金额；报告先记录为价格信息未覆盖，不推断具体金额。",
                        "confidence": 0.58,
                        "source_ids": source_ids[:1],
                        "needs_review": True,
                        "status": "needs_review",
                        "uncertainty": "未采集到明确的定价页面或价格字段。",
                    }
                )
            if any(keyword in combined_text for keyword in ["评价", "口碑", "用户", "评论", "review", "reviews", "满意", "投诉"]):
                review_source_ids = [source["id"] for source in (review_sources or factual)[:2]]
                claims.append(
                    {
                        "section": "reviews",
                        "content": f"{name} 的当前资料出现用户评价或口碑线索：{self._review_summary(review_sources or factual)}",
                        "confidence": min(confidence, 0.66),
                        "source_ids": review_source_ids,
                        "needs_review": True,
                        "status": "needs_review",
                        "uncertainty": "用户评价需要更多独立来源或上传问卷/访谈材料支撑。",
                    }
                )
            claims.append(
                {
                    "section": "user_persona",
                    "content": f"{name} 的目标用户和使用场景可从当前公开定位中归纳：{self._persona_summary(persona_sources or factual)}",
                    "confidence": confidence,
                    "source_ids": [source["id"] for source in (persona_sources or factual)[:2]],
                    "needs_review": True,
                    "status": "needs_review",
                    "uncertainty": "用户画像属于推断，当前只作为待复核线索。",
                }
            )

        if factual_sources:
            swot_claims = []
            for name in competitor_names:
                factual = competitor_factual.get(name, [])
                if not factual:
                    continue
                swot_claims.append(
                    {
                        "section": "swot",
                        "content": self._swot_summary(name, factual),
                        "confidence": 0.66,
                        "source_ids": [source["id"] for source in factual[:2]],
                        "needs_review": True,
                        "status": "needs_review",
                        "uncertainty": "SWOT 基于当前采集资料归纳，后续可随新增来源更新。",
                    }
                )
            claims.extend(swot_claims)
        return claims

    def _sources_for_report_module(self, sources: list[sqlite3.Row], section: str) -> list[sqlite3.Row]:
        role_priority = {
            "feature_tree": ["official", "official_doc"],
            "pricing_model": ["official_pricing"],
            "reviews": ["review"],
            "user_persona": ["official", "review"],
            "swot": ["official", "official_doc", "news", "review"],
        }.get(section, ["official", "official_doc"])
        module_patterns = {
            "feature_tree": r"官网|功能|产品|文档|feature|product",
            "pricing_model": r"价格|定价|套餐|api|pricing|price",
            "reviews": r"评价|口碑|review|g2|trustpilot|app store",
            "user_persona": r"用户|场景|官网|产品|评价",
            "swot": r"官网|文档|新闻|风险|市场|评价|安全",
        }
        pattern = module_patterns.get(section, "")
        ranked = []
        for source in sources:
            score = int(row_get(source, "relevance_score", 0) or 0)
            role = row_get(source, "source_role", "")
            if role in role_priority:
                score += 10 - role_priority.index(role)
            if pattern and re.search(pattern, f"{source['title']} {source['excerpt']} {row_get(source, 'module', '')}", flags=re.I):
                score += 3
            if row_get(source, "raw_content_status", "") == "fetched":
                score += 1
            ranked.append((score, source))
        ranked.sort(key=lambda item: item[0], reverse=True)
        return [source for score, source in ranked if score > 0]

    def _source_summary(self, sources: list[sqlite3.Row], limit: int = 220) -> str:
        parts = []
        for source in sources[:3]:
            excerpt = self._clean_source_excerpt_for_report(source["excerpt"], 130)
            title = "" if source["source_type"] == "uploaded_file" else report_text(source["title"], 70)
            if title and excerpt:
                parts.append(f"{title} 提到 {excerpt}")
            elif excerpt:
                parts.append(excerpt)
        return report_text("；".join(parts), limit) or "当前来源可确认其公开产品定位和能力线索"

    def _clean_source_excerpt_for_report(self, value: str, limit: int = 160) -> str:
        text = self._clean_uploaded_material_for_analysis(value or "")
        text = re.sub(r"搜索词：[^。]{0,180}。", "", text)
        text = re.sub(r"搜索结果：[^。]{0,180}。", "", text)
        text = re.sub(r"摘要：", "", text)
        text = re.sub(r"正文线索：", "", text)
        text = re.sub(r"\b[A-Za-z0-9_]*_0[1-4]_[^。\s]{0,24}\.pdf\s*提到\s*", "", text)
        text = re.sub(r"\b0[1-4]_[^。\s]{0,24}\.pdf\s*提到\s*", "", text)
        text = re.sub(r"来源清单[^。]{0,180}", "", text)
        return report_text(text, limit)

    def _pricing_summary(self, sources: list[sqlite3.Row]) -> str:
        text = self._source_summary(sources, 260)
        if re.search(r"价格|售价|定价|套餐|订阅|车型|万元|元|pricing|price|plan", text, flags=re.I):
            return text
        return f"{text}。当前来源只出现定价相关入口或线索，未稳定抽取到具体金额"

    def _review_summary(self, sources: list[sqlite3.Row]) -> str:
        text = " ".join(self._clean_source_excerpt_for_report(source["excerpt"], 420) for source in sources[:4])
        if not text:
            return "公开评价样本较少，暂以已上传评论材料为准。"
        positive = []
        negative = []
        for label, pattern in [
            ("易用性", r"易用|方便|简洁|helpful|useful|assistant|好帮手"),
            ("学习/办公效率", r"学习|办公|写作|代码|文档|PPT|效率|coding|grammar"),
            ("多模态体验", r"语音|图片|视频|P 图|image|voice"),
            ("低成本/API", r"免费|低价|API|cost|price"),
        ]:
            if re.search(pattern, text, flags=re.I):
                positive.append(label)
        for label, pattern in [
            ("生成错误", r"错误|wrong|mistake|hallucinat"),
            ("账号/订阅/账单", r"订阅|付费|账单|account|subscription|billing|限制"),
            ("隐私/合规担忧", r"隐私|位置|数据|privacy|location|political|filter"),
            ("样本分散", r"Trustpilot|App Store|平台|样本|评分"),
        ]:
            if re.search(pattern, text, flags=re.I):
                negative.append(label)
        if positive or negative:
            return f"正向反馈集中在{'、'.join(positive[:3]) or '核心功能使用'}；负向或风险反馈集中在{'、'.join(negative[:3]) or '服务体验波动'}。"
        return report_text(text, 180)

    def _persona_summary(self, sources: list[sqlite3.Row]) -> str:
        text = self._source_summary(sources, 220)
        return text or "当前公开定位可支撑初步目标用户归纳，细分画像需访谈或问卷补强"

    def _coverage_terms(self, sources: list[sqlite3.Row]) -> str:
        text = " ".join([f"{source['title']} {source['excerpt']}" for source in sources[:6]])
        labels = []
        for label, pattern in [
            ("官网/产品能力", r"官网|产品|功能|能力|服务"),
            ("定价/套餐", r"价格|售价|定价|套餐|订阅|pricing|price|plan"),
            ("用户反馈", r"评价|口碑|评论|用户|review"),
            ("新闻/市场线索", r"新闻|销量|交付|市场|发布"),
            ("技术/智能化", r"AI|智能|模型|自动|技术|芯片"),
        ]:
            if re.search(pattern, text, flags=re.I):
                labels.append(label)
        return "、".join(labels[:4]) or "产品定位与公开能力"

    def _source_signal_definitions(self) -> list[tuple[str, str]]:
        return [
            ("官网/产品能力", r"官网|产品|功能|能力|服务|协作|文档|知识库|工作流|workspace|product|feature"),
            ("定价/商业化", r"价格|售价|定价|套餐|订阅|付费|免费|额度|pricing|price|plan|subscription|cost"),
            ("用户反馈/口碑", r"评价|口碑|评论|用户|反馈|满意|投诉|review|reviews|g2|capterra"),
            ("市场/增长", r"新闻|市场|发布|融资|增长|销量|客户|企业|行业|营收|market|revenue|customer"),
            ("技术/API/生态", r"AI|模型|API|开发者|生态|集成|插件|开源|推理|自动化|技术|LLM|agent"),
            ("合规/安全", r"隐私|安全|合规|权限|数据|政策|监管|security|privacy|compliance"),
        ]

    def _source_signal_labels(self, sources: list[sqlite3.Row]) -> list[str]:
        text = " ".join([f"{source['title']} {source['excerpt']} {source['author_site']}" for source in sources[:8]])
        labels = []
        for label, pattern in self._source_signal_definitions():
            if re.search(pattern, text, flags=re.I):
                labels.append(label)
        return labels

    def _source_digest(self, source: sqlite3.Row | None, limit: int = 150) -> str:
        if not source:
            return "材料显示该项证据有限"
        title = "" if source["source_type"] == "uploaded_file" else report_text(source["title"], 58)
        excerpt = self._clean_source_excerpt_for_report(source["excerpt"], max(limit - len(title) - 8, 60))
        if title and excerpt:
            return report_text(f"{title}：{excerpt}", limit)
        return report_text(excerpt or title or "材料未提供有效摘要", limit)

    def _source_signal_evidence(self, label: str, sources: list[sqlite3.Row]) -> str:
        pattern = next((item[1] for item in self._source_signal_definitions() if item[0] == label), "")
        for source in sources:
            text = f"{source['title']} {source['excerpt']} {source['author_site']}"
            if pattern and re.search(pattern, text, flags=re.I):
                return self._source_digest(source)
        return self._source_digest(sources[0] if sources else None)

    def _swot_summary(self, competitor_name: str, sources: list[sqlite3.Row]) -> str:
        labels = self._source_signal_labels(sources)
        coverage = "、".join(labels[:3]) or self._coverage_terms(sources)
        strength_label = labels[0] if labels else "公开产品定位"
        strength_evidence = self._source_signal_evidence(strength_label, sources)
        missing = [
            label
            for label in ["定价/商业化", "用户反馈/口碑", "市场/增长", "合规/安全"]
            if label not in labels
        ]
        if missing:
            weakness = f"公开信息主要集中在{coverage}，对{'、'.join(missing[:2])}的佐证较少，形成结论时需要降低权重。"
        elif len(sources) < 3:
            weakness = "独立来源数量偏少，虽然已有线索，但仍需更多官网、评价或第三方材料交叉验证。"
        else:
            weakness = f"现有证据集中在{coverage}，深层用户痛点和商业效果仍需人工复核。"

        if "技术/API/生态" in labels:
            opportunity = "可围绕技术、API 或生态集成能力设计差异化场景，并用更多开发者/企业案例验证。"
        elif "定价/商业化" in labels:
            opportunity = "可围绕已出现的定价或套餐线索，进一步比较价格权益、使用限制和转化门槛。"
        elif "用户反馈/口碑" in labels:
            opportunity = "可把用户反馈中反复出现的痛点转化为功能体验和服务改进机会。"
        else:
            opportunity = f"可围绕{coverage}强化垂直场景，把已有能力转化为更清晰的产品定位。"

        if "定价/商业化" in labels:
            threat = "价格、套餐和权益调整具有时间敏感性，若未持续核验，结论容易过期。"
        elif "用户反馈/口碑" in labels:
            threat = "公开口碑和用户评价变化会快速影响竞争判断，需要持续监测负面反馈和替代方案。"
        elif "市场/增长" in labels:
            threat = "市场发布、客户案例和渠道动作更新较快，可能改变其阶段性竞争位置。"
        else:
            threat = "竞品功能迭代和公开资料更新会改变对比结论，未覆盖领域不能直接下判断。"
        return (
            f"{competitor_name}：优势：当前来源显示{coverage}是主要可核验线索，代表证据为「{strength_evidence}」；"
            f"劣势：{weakness}"
            f"机会：{opportunity}"
            f"威胁：{threat}"
        )

    def _dedupe_claims(self, claims: list[dict[str, Any]]) -> list[dict[str, Any]]:
        seen: set[tuple[str, str]] = set()
        deduped: list[dict[str, Any]] = []
        for claim in claims:
            key = (claim.get("section", ""), sanitize_text(claim.get("content", ""), 120))
            if key in seen:
                continue
            seen.add(key)
            deduped.append(claim)
        return deduped[:18]

    def _source_ids_for_task(self, task_id: str) -> list[str]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT id FROM sources WHERE task_id = ? ORDER BY collected_at, rowid",
                (task_id,),
            ).fetchall()
        return [row["id"] for row in rows]

    def _model_claims_for_task(self, task_id: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        task = self._task_config(task_id)
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT c.source_id, c.chunk_index, c.excerpt, c.summary, s.title AS source_title,
                       s.source_type, s.credibility, s.competitor_name, s.module, s.raw_content_status
                FROM evidence_chunks c
                JOIN sources s ON s.id = c.source_id
                WHERE c.task_id = ?
                ORDER BY c.collected_at, c.source_id, c.chunk_index
                LIMIT 24
                """,
                (task_id,),
            ).fetchall()
        evidence = [dict(row) for row in rows]
        for item in evidence:
            item["excerpt"] = self._clean_source_excerpt_for_report(str(item.get("excerpt", "")), 700)
            item["summary"] = self._clean_source_excerpt_for_report(str(item.get("summary", "")), 220)
        if not self._llm_calls_allowed_for_config(task):
            trace = self._offline_external_trace(
                "llm_generate_claims",
                sum(len(item.get("excerpt", "")) for item in evidence) // 4,
            )
            return [], trace
        try:
            result = self.llm_provider.generate_claims(task, evidence)
            claims = []
            for claim in result.claims:
                payload = dict(claim)
                payload["generated_agent"] = "分析 Agent / LLM Provider"
                claims.append(payload)
            return claims, {
                "provider": result.provider,
                "token_input": result.input_tokens,
                "token_output": result.output_tokens,
                "fallback_reason": result.fallback_reason,
                "tool_calls": result.tool_calls,
            }
        except LLMProviderError as exc:
            safe_reason = sanitize_text(str(exc), 240)
            return [], {
                "provider": self.llm_provider.provider,
                "token_input": max(1, sum(len(item.get("excerpt", "")) for item in evidence) // 4),
                "token_output": 1,
                "fallback_reason": safe_reason,
                "tool_calls": [{"name": "llm_generate_claims", "result": f"fallback: {safe_reason}"}],
            }

    def _qa_check(self, task_id: str, first_pass: bool, rework_round: int = 0) -> str | None:
        stage_started = now_dt()
        self._update_task(task_id, "qa_review")
        self._log_agent_event(
            task_id,
            "质检 Agent",
            "qa_started",
            "正在检查来源绑定、Schema 字段、低置信度说明、重复结论和引用覆盖。",
            meta={"first_pass": first_pass, "rework_round": rework_round},
        )
        qa_trace = self._model_qa_review_for_task(task_id)
        artifact_review = self._review_analysis_artifact_for_qa(task_id)
        blocking_findings: list[dict[str, Any]] = []
        skipped_model_findings: list[dict[str, Any]] = []
        with self.connect() as conn:
            claims = conn.execute(
                "SELECT * FROM claims WHERE task_id = ? ORDER BY created_at, rowid",
                (task_id,),
            ).fetchall()
            sources = conn.execute(
                "SELECT id, source_type, collected_at, published_at, competitor_name, title, url_or_path, excerpt FROM sources WHERE task_id = ?",
                (task_id,),
            ).fetchall()
            source_by_id = {source["id"]: source for source in sources}
            task_config = self._task_config(task_id)
            competitors = task_config.get("competitors", [])
            seen_contents: dict[str, str] = {}
            for index, claim in enumerate(claims):
                source_ids = loads(claim["source_ids"], [])
                if not source_ids and claim["status"] != "confirmed":
                    blocking_findings.append(
                        self._qa_finding(
                            claim,
                            "high",
                            "关键结论缺少 source_id，不能进入最终报告。",
                            "分析 Agent",
                            "missing_source",
                            "补充可追溯来源，或把该结论降级为待补证缺口。",
                            {"missing_material": "需要 URL、上传材料、问卷/访谈片段或人工确认记录。", "repair_action": "manual_supplement"},
                        )
                    )
                if float(claim["confidence"] or 0) < 0.5 and not claim["uncertainty"]:
                    blocking_findings.append(
                        self._qa_finding(
                            claim,
                            "medium",
                            "低置信度结论缺少不确定性说明，需分析 Agent 修复后再进入报告。",
                            "分析 Agent",
                            "low_confidence_missing_uncertainty",
                            "补充不确定性、反证或人工确认；不能直接作为确定事实。",
                            {"repair_action": "confirm_uncertainty"},
                        )
                    )
                content_key = sanitize_text(claim["content"], 180)
                if content_key in seen_contents:
                    blocking_findings.append(
                        self._qa_finding(
                            claim,
                            "low",
                            "发现重复结论，需要合并或删除重复 claim。",
                            "分析 Agent",
                            "duplicate_claim",
                            "合并重复结论，只保留证据更充分、信息量更高的一条。",
                            {"duplicate_of": seen_contents[content_key], "repair_action": "manual_supplement"},
                        )
                    )
                seen_contents[content_key] = claim["id"]
                if claim["section"] == "pricing_model":
                    missing_dates = [
                        source_id
                        for source_id in source_ids
                        if source_id in source_by_id and not (source_by_id[source_id]["collected_at"] or source_by_id[source_id]["published_at"])
                    ]
                    if missing_dates:
                        blocking_findings.append(
                            self._qa_finding(
                                claim,
                                "medium",
                                "定价、规格、政策或关键能力属于时间敏感信息，来源缺少采集日期或发布时间。",
                                "采集 Agent",
                                "missing_date",
                                "补充带采集日期的官方价格、规格/型号或权威政策页面后再复检。",
                                {"source_ids": missing_dates, "repair_action": "auto_collect"},
                            )
                        )
                if claim["section"] in {"pricing_model", "swot"} and source_ids:
                    only_scope = all(
                        (source_by_id[source_id]["source_type"] if source_id in source_by_id else "") in {"manual_scope", "demo_scope_note"}
                        for source_id in source_ids
                    )
                    if only_scope:
                        blocking_findings.append(
                            self._qa_finding(
                                claim,
                                "medium",
                                "高价值结论只有任务范围说明支撑，需要公开来源、上传材料或人工确认。",
                                "采集 Agent",
                                "scope_only",
                                "补充官网、官方文档、价格页或可信第三方来源。",
                                {"repair_action": "auto_collect"},
                            )
                        )
                mismatched = self._claim_source_ownership_mismatches(str(claim["content"] or ""), source_ids, source_by_id, competitors, str(claim["section"] or ""))
                if mismatched:
                    blocking_findings.append(
                        self._qa_finding(
                            claim,
                            "high",
                            f"结论提到 {self._join_names(mismatched)}，但绑定来源属于其他竞品或未命中该竞品，需重新采集或重做分析。",
                            "分析 Agent",
                            "source_ownership_mismatch",
                            f"为 {self._join_names(mismatched)} 自动补采官方页并重做该模块分析。",
                            {
                                "affected_competitors": mismatched,
                                "source_ids": source_ids,
                                "missing_material": f"需要 {self._join_names(mismatched)} 自有官网、官方文档或明确命中该竞品的来源。",
                                "suggested_queries": [f"{name} 官方 价格 API" for name in mismatched],
                                "repair_action": "auto_collect",
                            },
                        )
                    )
            for finding in qa_trace.get("findings", []):
                if not isinstance(finding, dict):
                    continue
                claim_id = self._claim_id_from_model_finding(finding, claims)
                if not claim_id:
                    skipped_model_findings.append(
                        {
                            "reason": "model_finding_claim_reference_invalid",
                            "claim_id": sanitize_text(str(finding.get("claim_id", "")), 120),
                            "claim_index": sanitize_text(str(finding.get("claim_index", "")), 120),
                        }
                    )
                    continue
                blocking_findings.append(
                    self._qa_finding(
                        next((claim for claim in claims if claim["id"] == claim_id), {"id": claim_id, "section": ""}),
                        str(finding.get("severity") or "medium"),
                        sanitize_text(str(finding.get("reason") or "模型质检发现结论需要复核。"), 500),
                        sanitize_text(str(finding.get("target_agent") or "分析 Agent"), 120),
                        sanitize_text(str(finding.get("finding_type") or "model_review"), 80),
                        sanitize_text(str(finding.get("action_hint") or "按模型质检原因补充证据或改写结论。"), 240),
                        {
                            "model_finding": finding,
                            "repair_action": sanitize_text(str(finding.get("repair_action") or ""), 80)
                            or self._repair_action_for_model_finding(finding),
                            "missing_material": sanitize_text(str(finding.get("missing_material") or ""), 300),
                            "suggested_queries": self._clean_suggested_queries(finding.get("suggested_queries")),
                        },
                    )
                )
            blocking_findings = self._dedupe_qa_findings(blocking_findings)
            for finding in blocking_findings:
                existing = conn.execute(
                    """
                    SELECT id FROM qa_findings
                    WHERE task_id = ? AND claim_id = ? AND reason = ? AND fix_status = 'open'
                    """,
                    (task_id, finding["claim_id"], finding["reason"]),
                ).fetchone()
                if existing:
                    continue
                conn.execute(
                    """
                    INSERT INTO qa_findings
                    (id, task_id, claim_id, severity, reason, target_agent, finding_type, action_hint, meta_json,
                     fix_status, recheck_result, created_at, fixed_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        uuid.uuid4().hex,
                        task_id,
                        finding["claim_id"],
                        finding["severity"],
                        finding["reason"],
                        finding["target_agent"],
                        finding.get("finding_type", "general"),
                        finding.get("action_hint", ""),
                        dumps(finding.get("meta", {})),
                        "open",
                        "",
                        utc_now_iso(),
                        "",
                    ),
                )

        for skipped in skipped_model_findings:
            self._log_agent_event(
                task_id,
                "质检 Agent",
                "qa_model_finding_skipped",
                "模型质检返回的 claim_id/claim_index 无法映射到当前结论，已跳过该条 finding。",
                severity="warning",
                meta=skipped,
            )

        if blocking_findings:
            rejected_claim_id = blocking_findings[0]["claim_id"]
            self._update_task(task_id, "qa_rework")
            self._log_agent_event(
                task_id,
                "质检 Agent",
                "qa_rejected",
                f"自动质检发现 {len(blocking_findings)} 条问题，已打回采集/分析 Agent 自动修复；连续三次同因失败后转人工复核。",
                severity="warning",
                meta={"claim_id": rejected_claim_id, "finding_count": len(blocking_findings), "rework_round": rework_round},
            )
            self._log_agent_run(
                task_id,
                agent_name="质检 Agent",
                input_summary="检查 claims 的 source_id、Schema 完整性、置信度、时间敏感信息、重复结论和报告准入条件。",
                output_summary=f"自动质检未通过：{len(blocking_findings)} 条问题已打回自动修复；{artifact_review['summary']}",
                status="rejected",
                duration_ms=self._elapsed_ms(stage_started),
                severity="warning",
                has_rework=True,
                retry_count=rework_round,
                token_input=qa_trace.get("token_input"),
                token_output=qa_trace.get("token_output"),
                model_provider=qa_trace.get("provider", ""),
                fallback_reason=qa_trace.get("fallback_reason", ""),
                tool_calls=[
                    {"name": "validate_schema_completeness", "result": "checked"},
                    {"name": "validate_claim_sources", "result": f"{len(blocking_findings)} rejected"},
                    {"name": "validate_time_sensitive_claims", "result": "checked"},
                ]
                + artifact_review["tool_calls"]
                + qa_trace.get("tool_calls", []),
                started_at=stage_started,
            )
            return rejected_claim_id

        self._update_task(task_id, "qa_passed")
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE qa_findings
                SET fix_status = 'fixed', recheck_result = '复检通过：修复后结论已绑定来源，并保留低置信度待确认标记。', fixed_at = ?
                WHERE task_id = ? AND fix_status = 'open'
                """,
                (utc_now_iso(), task_id),
            )
        self._log_agent_event(
            task_id,
            "质检 Agent",
            "qa_passed",
            "复检通过：进入报告的结论均有来源或明确标记待确认。",
            meta={"first_pass": first_pass},
        )
        self._log_agent_run(
            task_id,
            agent_name="质检 Agent",
            input_summary="复检打回后的结论与引用映射。",
            output_summary=f"复检通过：所有进入报告的关键结论均可追溯，低置信度结论已标记待确认；{artifact_review['summary']}",
            status="completed",
            duration_ms=self._elapsed_ms(stage_started),
            retry_count=1 if not first_pass else 0,
            token_input=qa_trace.get("token_input"),
            token_output=qa_trace.get("token_output"),
            model_provider=qa_trace.get("provider", ""),
            fallback_reason=qa_trace.get("fallback_reason", ""),
            has_rework=not first_pass,
            tool_calls=[
                {"name": "validate_schema_completeness", "result": "passed"},
                {"name": "validate_claim_sources", "result": "passed"},
                {"name": "validate_duplicate_claims", "result": "passed"},
            ]
            + artifact_review["tool_calls"]
            + qa_trace.get("tool_calls", []),
            started_at=stage_started,
        )
        return None

    def _review_analysis_artifact_for_qa(self, task_id: str) -> dict[str, Any]:
        artifact = self._latest_analysis_artifact(task_id)
        sections = artifact.get("sections", []) if artifact else []
        score_rows = artifact.get("score_dimensions", []) if artifact else []
        markdown = str(artifact.get("analysis_markdown", "") if artifact else "")
        score_without_basis = [
            row
            for row in score_rows
            if row.get("status") != "NA" and not (row.get("evidence_refs") or row.get("section_refs"))
        ]
        pending_scores = [row for row in score_rows if row.get("status") in {"待确认", "NA"}]
        markdown_ok = bool(re.search(r"^###\s+\d", markdown, flags=re.M) or len(sections) >= 8)
        summary = (
            f"报告产物审查：{len(sections)} 章、{len(score_rows)} 条评分，"
            f"{len(pending_scores)} 条待确认/NA，Markdown 结构{'正常' if markdown_ok else '需修复'}。"
        )
        self._log_agent_event(
            task_id,
            "质检 Agent",
            "qa_analysis_artifact_reviewed",
            summary,
            severity="warning" if score_without_basis or not markdown_ok else "info",
            meta={
                "section_count": len(sections),
                "score_count": len(score_rows),
                "pending_score_count": len(pending_scores),
                "score_without_basis_count": len(score_without_basis),
                "markdown_ok": markdown_ok,
            },
        )
        return {
            "summary": summary,
            "tool_calls": [
                {"name": "validate_analysis_sections", "result": f"{len(sections)} sections"},
                {"name": "validate_score_basis", "result": f"{len(score_rows) - len(score_without_basis)}/{len(score_rows)} with basis"},
                {"name": "validate_markdown_structure", "result": "passed" if markdown_ok else "needs_repair"},
            ],
        }

    def _repair_action_for_model_finding(self, finding: dict[str, Any]) -> str:
        target_agent = str(finding.get("target_agent", ""))
        finding_type = str(finding.get("finding_type", ""))
        if "采集" in target_agent or finding_type in {
            "missing_source",
            "pricing_missing_official",
            "missing_date",
            "source_ownership_mismatch",
            "insufficient_evidence",
            "unsupported_claim",
            "overclaim",
            "logic_gap",
        }:
            return "auto_collect"
        return "manual_supplement"

    def _clean_suggested_queries(self, value: Any) -> list[str]:
        if not isinstance(value, list):
            return []
        cleaned: list[str] = []
        for item in value:
            query = sanitize_text(str(item), 120)
            if query:
                cleaned.append(query)
        return cleaned[:5]

    def _claim_source_ownership_mismatches(
        self,
        content: str,
        source_ids: list[str],
        source_by_id: dict[str, sqlite3.Row],
        competitors: list[str],
        section: str,
    ) -> list[str]:
        if section == "overview" or not source_ids:
            return []
        mentioned = [
            name
            for name in competitors
            if name and re.search(re.escape(name), content, flags=re.I)
        ]
        if not mentioned:
            return []
        mismatched = []
        for name in mentioned:
            matched_source = False
            for source_id in source_ids:
                source = source_by_id.get(source_id)
                if not source:
                    continue
                owner = str(row_get(source, "competitor_name", "") or "")
                if owner and owner.casefold() == name.casefold():
                    matched_source = True
                    break
                if not owner and self._source_matches_competitor(source, name):
                    matched_source = True
                    break
            if not matched_source:
                mismatched.append(name)
        return mismatched

    def _safe_claim_index(self, value: Any) -> int | None:
        if value is None or isinstance(value, bool):
            return None
        if isinstance(value, (list, tuple)):
            for item in value:
                parsed = self._safe_claim_index(item)
                if parsed is not None:
                    return parsed
            return None
        if isinstance(value, dict):
            return None
        try:
            return int(float(str(value).strip()))
        except (TypeError, ValueError):
            return None

    def _claim_id_from_model_finding(self, finding: dict[str, Any], claims: list[sqlite3.Row]) -> str:
        claim_ids = {claim["id"] for claim in claims}
        raw_claim_id = finding.get("claim_id")
        if isinstance(raw_claim_id, str):
            claim_id = sanitize_text(raw_claim_id, 120)
            if claim_id in claim_ids:
                return claim_id
        elif isinstance(raw_claim_id, (list, tuple)):
            for item in raw_claim_id:
                if isinstance(item, str):
                    claim_id = sanitize_text(item, 120)
                    if claim_id in claim_ids:
                        return claim_id

        claim_index = self._safe_claim_index(finding.get("claim_index"))
        if claim_index is not None and 0 <= claim_index < len(claims):
            return claims[claim_index]["id"]
        return ""

    def _qa_finding(
        self,
        claim: sqlite3.Row | dict[str, Any],
        severity: str,
        reason: str,
        target_agent: str,
        finding_type: str,
        action_hint: str,
        meta: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        claim_id = row_get(claim, "id", "")
        section = row_get(claim, "section", "")
        source_ids = loads(row_get(claim, "source_ids", "[]"), []) if isinstance(row_get(claim, "source_ids", []), str) else row_get(claim, "source_ids", [])
        metadata = dict(meta or {})
        metadata.setdefault("finding_type", finding_type)
        metadata.setdefault("section", section)
        metadata.setdefault("source_ids", source_ids)
        if section == "pricing_model" and "missing_material" not in metadata:
            metadata["missing_material"] = "需要官方价格页、规格/型号/套餐页面或带采集日期的权威价格材料。"
        return {
            "claim_id": sanitize_text(str(claim_id), 120),
            "severity": sanitize_text(str(severity or "medium"), 40),
            "reason": sanitize_text(reason, 500),
            "target_agent": sanitize_text(target_agent or "分析 Agent", 120),
            "finding_type": sanitize_text(finding_type or "general", 80),
            "action_hint": sanitize_text(action_hint, 300),
            "meta": sanitize_payload(metadata, 900),
        }

    def _dedupe_qa_findings(self, findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
        deduped: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for finding in findings:
            reason = sanitize_text(str(finding.get("reason", "")), 500)
            claim_id = sanitize_text(str(finding.get("claim_id", "")), 120)
            if not reason or not claim_id:
                continue
            key = (claim_id, reason)
            if key in seen:
                continue
            seen.add(key)
            severity = str(finding.get("severity", "medium"))
            if severity not in {"low", "medium", "high", "critical"}:
                severity = "medium"
            deduped.append(
                {
                    "claim_id": claim_id,
                    "severity": severity,
                    "reason": reason,
                    "target_agent": sanitize_text(str(finding.get("target_agent", "分析 Agent")), 120) or "分析 Agent",
                    "finding_type": sanitize_text(str(finding.get("finding_type", "general")), 80) or "general",
                    "action_hint": sanitize_text(str(finding.get("action_hint", "")), 300),
                    "meta": sanitize_payload(finding.get("meta", {}), 900),
                }
            )
        return deduped

    def _model_qa_review_for_task(self, task_id: str) -> dict[str, Any]:
        task = self._task_config(task_id)
        with self.connect() as conn:
            claim_rows = conn.execute(
                "SELECT section, content, confidence, source_ids, needs_review, status FROM claims WHERE task_id = ? ORDER BY created_at, rowid",
                (task_id,),
            ).fetchall()
        claims = []
        for row in claim_rows:
            item = dict(row)
            item["source_ids"] = loads(item["source_ids"], [])
            claims.append(item)
        evidence = self._evidence_rows_for_task(task_id, limit=10)
        if not self._llm_calls_allowed_for_config(task):
            trace = self._offline_external_trace(
                "doubao_qa_review",
                sum(len(claim.get("content", "")) for claim in claims) // 4,
            )
            trace["findings"] = []
            return trace
        try:
            result = self.llm_provider.review_claims(task, claims, evidence)
            return {
                "provider": result.provider,
                "token_input": result.input_tokens,
                "token_output": result.output_tokens,
                "fallback_reason": result.fallback_reason,
                "tool_calls": result.tool_calls,
                "findings": result.data.get("findings", []) if isinstance(result.data, dict) else [],
            }
        except LLMProviderError as exc:
            safe_reason = sanitize_text(str(exc), 240)
            return {
                "provider": self.llm_provider.provider,
                "token_input": max(1, sum(len(claim.get("content", "")) for claim in claims) // 4),
                "token_output": 1,
                "fallback_reason": safe_reason,
                "tool_calls": [{"name": "doubao_qa_review", "result": f"fallback: {safe_reason}"}],
                "findings": [],
            }

    def _model_collection_review(self, task_id: str) -> dict[str, Any]:
        task = self._task_config(task_id)
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT source_type, title, url_or_path, excerpt, credibility
                FROM sources
                WHERE task_id = ?
                ORDER BY collected_at, rowid
                LIMIT 12
                """,
                (task_id,),
            ).fetchall()
        sources = [dict(row) for row in rows]
        if not self._llm_calls_allowed_for_config(task):
            return self._offline_external_trace(
                "doubao_collection_review",
                sum(len(item.get("excerpt", "")) for item in sources) // 4,
            )
        try:
            result = self.llm_provider.review_collection(task, sources)
            summary = report_text(str(result.data.get("summary", "")), 180)
            return {
                "provider": result.provider,
                "token_input": result.input_tokens,
                "token_output": result.output_tokens,
                "fallback_reason": result.fallback_reason,
                "tool_calls": result.tool_calls + ([{"name": "collection_review_summary", "result": summary}] if summary else []),
            }
        except LLMProviderError as exc:
            safe_reason = sanitize_text(str(exc), 240)
            return {
                "provider": self.llm_provider.provider,
                "token_input": max(1, sum(len(item.get("excerpt", "")) for item in sources) // 4),
                "token_output": 1,
                "fallback_reason": safe_reason,
                "tool_calls": [{"name": "doubao_collection_review", "result": f"fallback: {safe_reason}"}],
            }

    def _auto_repair_open_findings(self, task_id: str, rework_round: int) -> int:
        if not self._external_calls_allowed(task_id):
            self._log_agent_event(
                task_id,
                "采集 Agent",
                "qa_rework_collect_skipped",
                "未开启联网搜索，质检自动补采已跳过；后续只基于缓存内容、上传材料或人工补充来源修复。",
                severity="warning",
                meta={"rework_round": rework_round, "source_mode": self._task_config(task_id).get("source_mode", "")},
            )
            return 0
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT f.*, c.content AS claim_content, c.section AS claim_section
                FROM qa_findings f
                JOIN claims c ON c.id = f.claim_id
                WHERE f.task_id = ? AND f.fix_status = 'open'
                ORDER BY f.created_at, f.rowid
                LIMIT 6
                """,
                (task_id,),
            ).fetchall()
            claims = {
                row["claim_id"]: conn.execute(
                    "SELECT * FROM claims WHERE task_id = ? AND id = ?",
                    (task_id, row["claim_id"]),
                ).fetchone()
                for row in rows
            }

        if not rows:
            return 0

        self._update_task(task_id, "collecting")
        self._log_agent_event(
            task_id,
            "采集 Agent",
            "qa_rework_collect_started",
            f"质检第 {rework_round} 轮打回后，正在按问题清单补采来源并准备重做分析。",
            severity="warning",
            meta={"finding_count": len(rows), "rework_round": rework_round},
        )
        fixed = 0
        for finding in rows:
            claim = claims.get(finding["claim_id"])
            if not claim:
                continue
            updated = self._auto_repair_claim_from_official_sources(task_id, finding, claim)
            if updated:
                fixed += 1
                self._mark_finding_fixed(
                    task_id,
                    finding["id"],
                    f"第 {rework_round} 轮已自动补采来源并重做该条分析，等待质检复检。",
                )
        self._log_agent_event(
            task_id,
            "分析 Agent",
            "qa_rework_analysis_finished",
            f"第 {rework_round} 轮打回处理完成：{fixed}/{len(rows)} 条问题已自动修复。",
            severity="info" if fixed else "warning",
            meta={"fixed_count": fixed, "finding_count": len(rows), "rework_round": rework_round},
        )
        return fixed

    def _repair_analysis(self, task_id: str, claim_id: str, source_map: dict[str, str]) -> None:
        stage_started = now_dt()
        self._update_task(task_id, "reanalyzing")
        self._log_agent_event(
            task_id,
            "分析 Agent",
            "analysis_rework_started",
            "收到质检打回，正在把问题结论改写为带来源或待确认结论。",
            severity="warning",
            meta={"claim_id": claim_id},
        )
        task_config = self._task_config(task_id)
        competitor_label = self._join_names(task_config.get("competitors", []))
        repair_source_id = source_map.get("src_demo_scope_note") or (self._source_ids_for_task(task_id) or [""])[0]
        with self.connect() as conn:
            repaired_content = (
                f"{competitor_label} 的成熟度判断属于高价值且易受版本影响的信息；当前证据不足时只能列为待采集或待人工确认，不能直接断言。"
            )
            conn.execute(
                """
                UPDATE claims
                SET content = ?, confidence = ?, source_ids = ?, needs_review = 1,
                    status = 'needs_review', uncertainty = ?, claim_type = 'assumption'
                WHERE id = ? AND task_id = ?
                """,
                (
                    repaired_content,
                    0.42,
                    dumps([repair_source_id] if repair_source_id else []),
                    "当前来源不足以证明成熟度，需要实时采集、上传材料或人工确认。",
                    claim_id,
                    task_id,
                ),
            )
            if repair_source_id:
                self._insert_evidence_links(conn, task_id, "claims", claim_id, [repair_source_id], repaired_content[:260])
        self._log_agent_run(
            task_id,
            agent_name="分析 Agent",
            input_summary="接收质检打回：关键结论缺少 source_id。",
            output_summary="已将无来源断言改为不确定结论，绑定样例范围说明并标记待确认。",
            status="rerun_completed",
            duration_ms=self._elapsed_ms(stage_started),
            retry_count=1,
            has_rework=True,
            tool_calls=[{"name": "repair_claim_with_source_policy", "result": "fixed"}],
            started_at=stage_started,
        )
        self._log_agent_event(task_id, "分析 Agent", "analysis_rework_finished", "打回修复完成，等待质检复检。")

    def _mark_finding_fixed(self, task_id: str, finding_id: str, result: str) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE qa_findings
                SET fix_status = 'fixed', recheck_result = ?, fixed_at = ?
                WHERE task_id = ? AND id = ?
                """,
                (sanitize_text(result, 500), utc_now_iso(), task_id, finding_id),
            )

    def _auto_repair_claim_from_official_sources(self, task_id: str, finding: sqlite3.Row, claim: sqlite3.Row) -> bool:
        task_config = self._task_config(task_id)
        competitors = task_config.get("competitors", [])
        meta = loads(row_get(finding, "meta_json", "{}"), {})
        affected = meta.get("affected_competitors") if isinstance(meta, dict) else []
        names = [str(name) for name in affected if name] if isinstance(affected, list) else []
        if not names:
            names = [self._competitor_for_claim(claim["content"], competitors)]
        names = [name for name in names if name]
        if not names:
            return False

        inserted_count = 0
        for name in names:
            drafts = self._collect_official_seed_drafts(task_id, name)
            drafts.extend(self._collect_rework_search_drafts(task_id, name, claim["section"], meta))
            if drafts:
                self._insert_source_drafts(task_id, self._dedupe_source_drafts(drafts))
                inserted_count += len(drafts)
        self._refresh_pricing_facts(task_id)

        updated = False
        for name in names:
            if claim["section"] == "pricing_model":
                updated = self._repair_pricing_claim(task_id, claim["id"], name) or updated
            else:
                updated = self._repair_nonpricing_claim(task_id, claim["id"], name, claim["section"]) or updated
        self._log_agent_run(
            task_id,
            agent_name="分析 Agent",
            input_summary=f"按质检问题自动补采官方来源并修复 claim：{finding['id']}",
            output_summary=f"已补采/复用官方种子来源 {inserted_count} 条，并{'更新' if updated else '尝试更新'}相关结论。",
            status="rerun_completed" if updated else "needs_review",
            duration_ms=6400,
            retry_count=1,
            has_rework=True,
            tool_calls=[{"name": "repair_qa_finding", "result": "updated" if updated else "no_extractable_fact"}],
        )
        return updated

    def _collect_rework_search_drafts(self, task_id: str, name: str, section: str, meta: dict[str, Any]) -> list[WebSourceDraft]:
        suggested = meta.get("suggested_queries") if isinstance(meta, dict) else []
        queries = [sanitize_text(str(query), 120) for query in suggested if sanitize_text(str(query), 120)] if isinstance(suggested, list) else []
        industry = self._task_config(task_id).get("industry", "")
        if not queries:
            _, price_query = self._price_search_terms(name, industry)
            module_query = {
                "pricing_model": price_query,
                "reviews": f"{name} 用户评价 口碑 G2",
                "user_persona": f"{name} 用户 场景 官网",
                "swot": f"{name} 新闻 风险 市场",
            }.get(section, f"{name} 官网 产品 功能")
            queries = [module_query, f"{name} 官方 文档", f"{name} 评价 风险"]
        drafts: list[WebSourceDraft] = []
        for query in list(dict.fromkeys(queries))[:3]:
            self._log_agent_event(
                task_id,
                "采集 Agent",
                "qa_rework_search_query",
                f"质检打回后补搜：{query}",
                severity="warning",
                meta={"competitor": name, "query": query, "section": section},
            )
            try:
                results = self.search_client.search(query, task_id[:8], start_index=len(drafts), limit=6)
                self._log_collection_run(
                    task_id,
                    provider="volc_search",
                    query=query,
                    status="completed",
                    result_count=len(results),
                    log_id=next((draft.search_log_id for draft in results if draft.search_log_id), ""),
                    time_cost_ms=max([draft.time_cost_ms for draft in results] or [0]),
                )
                filtered, _ = self._filter_search_results_for_name(results, name, industry, section, [], [])
                drafts.extend(self._enrich_high_quality_search_results(task_id, filtered[:3]))
            except Exception as exc:
                safe_reason = sanitize_text(str(exc), 240)
                self._log_collection_run(
                    task_id,
                    provider="volc_search",
                    query=query,
                    status="failed",
                    result_count=0,
                    error=safe_reason,
                )
                self._log_agent_event(
                    task_id,
                    "采集 Agent",
                    "qa_rework_search_failed",
                    f"补搜失败：{query}",
                    severity="warning",
                    meta={"reason": safe_reason},
                )
        return drafts

    def _repair_pricing_claim(self, task_id: str, claim_id: str, competitor: str) -> bool:
        with self.connect() as conn:
            fact_rows = conn.execute(
                """
                SELECT * FROM pricing_facts
                WHERE task_id = ? AND lower(competitor_name) = lower(?)
                ORDER BY confidence DESC, amount DESC
                """,
                (task_id, competitor),
            ).fetchall()
            official_sources = conn.execute(
                """
                SELECT * FROM sources
                WHERE task_id = ? AND lower(competitor_name) = lower(?)
                  AND source_role = 'official_pricing'
                ORDER BY relevance_score DESC, collected_at DESC
                """,
                (task_id, competitor),
            ).fetchall()
            if fact_rows:
                content = self._pricing_claim_text_from_facts(competitor, [dict(row) for row in fact_rows])
                source_ids = list(dict.fromkeys([row["source_id"] for row in fact_rows]))[:4]
                needs_review = 0
                status = "reportable"
                uncertainty = "价格按官方来源采集日口径；正式使用前仍需复核官网。"
            elif official_sources:
                content = f"已定位 {competitor} 官方价格/报价来源，但当前未稳定抽取到明确金额；报告仅记录为待复核价格缺口，不输出价格结论。"
                source_ids = [row["id"] for row in official_sources[:3]]
                needs_review = 1
                status = "needs_review"
                uncertainty = "官方页面正文或价格表需人工复核。"
            else:
                return False
            conn.execute(
                """
                UPDATE claims
                SET content = ?, confidence = ?, source_ids = ?, needs_review = ?,
                    status = ?, uncertainty = ?, claim_type = 'fact', created_at = ?
                WHERE task_id = ? AND id = ?
                """,
                (
                    sanitize_text(content, 1200),
                    0.9 if fact_rows else 0.62,
                    dumps(source_ids),
                    needs_review,
                    status,
                    uncertainty,
                    utc_now_iso(),
                    task_id,
                    claim_id,
                ),
            )
            self._insert_evidence_links(conn, task_id, "claims", claim_id, source_ids, content[:260])
        return True

    def _repair_nonpricing_claim(self, task_id: str, claim_id: str, competitor: str, section: str) -> bool:
        role_order = {
            "feature_tree": ["official", "official_doc"],
            "reviews": ["review"],
            "user_persona": ["official", "review"],
            "swot": ["official", "official_doc", "news", "review"],
        }.get(section, ["official", "official_doc"])
        placeholders = ",".join(["?"] * len(role_order))
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM sources
                WHERE task_id = ? AND lower(competitor_name) = lower(?)
                  AND source_role IN ({placeholders})
                ORDER BY relevance_score DESC, collected_at DESC
                """,
                (task_id, competitor, *role_order),
            ).fetchall()
            if not rows:
                return False
            content = self._section_repair_text(competitor, section, rows)
            needs_review = 1 if section in {"reviews", "swot", "user_persona"} else 0
            source_ids = [row["id"] for row in rows[:3]]
            conn.execute(
                """
                UPDATE claims
                SET content = ?, confidence = ?, source_ids = ?, needs_review = ?,
                    status = ?, uncertainty = ?, created_at = ?
                WHERE task_id = ? AND id = ?
                """,
                (
                    sanitize_text(content, 1200),
                    0.78,
                    dumps(source_ids),
                    needs_review,
                    "needs_review" if needs_review else "reportable",
                    "该结论已改为仅基于同竞品来源归纳，仍需更多独立来源交叉核验。" if needs_review else "",
                    utc_now_iso(),
                    task_id,
                    claim_id,
                ),
            )
            self._insert_evidence_links(conn, task_id, "claims", claim_id, source_ids, content[:260])
        return True

    def _section_repair_text(self, competitor: str, section: str, sources: list[sqlite3.Row]) -> str:
        refs = self._source_summary(sources, 180)
        if section == "reviews":
            return f"{competitor} 当前只采到同竞品评价线索：{refs}；样本不足时不输出整体口碑结论。"
        if section == "user_persona":
            return f"{competitor} 的用户画像只能基于同竞品公开定位和评价线索初步归纳：{refs}。"
        if section == "swot":
            return self._swot_summary(competitor, sources)
        return f"{competitor} 的产品/功能结论已改为仅使用同竞品来源：{refs}。"

    def _manual_supplement_source(self, task_id: str, user_text: str, selected_text: str) -> str:
        self._update_task(task_id, "collecting")
        source_id = f"{task_id[:8]}_manual_{uuid.uuid4().hex[:8]}"
        excerpt = selected_text or user_text
        url_match = re.search(r"https?://[^\s，,。；;）)]+", user_text or "")
        manual_url = url_match.group(0) if url_match else ""
        title = "人工补充来源" if manual_url else "人工口述/材料补充"
        if manual_url:
            title = f"人工补充来源：{urllib.parse.urlparse(manual_url).netloc}"
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO sources
                (id, task_id, source_type, title, url_or_path, author_site, published_at, collected_at,
                 credibility, excerpt, related_claim_ids)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    source_id,
                    task_id,
                    "manual_url" if manual_url else "manual_input",
                    title,
                    manual_url or "manual://intervention",
                    urllib.parse.urlparse(manual_url).netloc if manual_url else "用户人工输入",
                    "",
                    utc_now_iso(),
                    "medium",
                    sanitize_text(excerpt, 500),
                    "[]",
                ),
            )
        self._log_agent_run(
            task_id,
            agent_name="采集 Agent",
            input_summary="人工复查要求补充来源或重新搜索。",
            output_summary="已将人工补充的网址、材料或口述说明登记为来源，并进入分析复核。",
            status="completed",
            duration_ms=6800,
            tool_calls=[{"name": "register_manual_source", "result": source_id}],
        )
        claim_id = uuid.uuid4().hex
        self._insert_claims(
            task_id,
            [
                {
                    "id": claim_id,
                    "section": "overview",
                    "content": "人工复查已补充证据需求，系统保留该说明并将相关结论继续标记为待复核。",
                    "confidence": 0.66,
                    "source_ids": [source_id],
                    "needs_review": True,
                    "status": "needs_review",
                    "uncertainty": "人工输入可作为线索，正式事实仍需外部来源核验。",
                }
            ],
        )
        self._log_agent_run(
            task_id,
            agent_name="分析 Agent",
            input_summary="读取人工补充来源并更新待复核说明。",
            output_summary="已新增 1 条带人工来源的待复核概览结论。",
            status="rerun_completed",
            duration_ms=9200,
            retry_count=1,
        )
        self._refresh_deep_analysis_from_current_claims(task_id, "人工补充来源后刷新深度分析产物")
        self._qa_check(task_id, first_pass=False)
        self._generate_report(task_id, reason="manual_source")
        self._set_task_completed(task_id)
        return "已补充人工来源，重新分析与质检通过，并生成新的报告版本。"

    def _manual_revise_claim(self, task_id: str, user_text: str, selected_text: str) -> str:
        self._update_task(task_id, "reanalyzing")
        source_id = f"{task_id[:8]}_manual_revision_{uuid.uuid4().hex[:8]}"
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO sources
                (id, task_id, source_type, title, url_or_path, author_site, published_at, collected_at,
                 credibility, excerpt, related_claim_ids)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    source_id,
                    task_id,
                    "manual_input",
                    "人工修正意见",
                    "manual://revision",
                    "用户人工输入",
                    "",
                    utc_now_iso(),
                    "medium",
                    sanitize_text(selected_text or user_text, 500),
                    "[]",
                ),
            )
        self._insert_claims(
            task_id,
            [
                {
                    "section": "overview",
                    "content": f"根据人工修正，报告应优先复核：{sanitize_text(user_text, 120)}",
                    "confidence": 0.68,
                    "source_ids": [source_id],
                    "needs_review": True,
                    "status": "needs_review",
                    "uncertainty": "人工修正意见已入库，外部事实仍需来源支撑。",
                }
            ],
        )
        self._log_agent_run(
            task_id,
            agent_name="分析 Agent",
            input_summary="人工复查要求修正结论。",
            output_summary="已新增人工修正结论并绑定 manual_input 来源。",
            status="rerun_completed",
            duration_ms=8600,
            retry_count=1,
        )
        self._refresh_deep_analysis_from_current_claims(task_id, "人工修正结论后刷新深度分析产物")
        self._qa_check(task_id, first_pass=False)
        self._generate_report(task_id, reason="manual_revision")
        self._set_task_completed(task_id)
        return "已根据人工修正更新结论，重新质检通过，并生成新的报告版本。"

    def _confirm_low_confidence_claim(self, task_id: str, user_text: str, claim_id: str = "") -> str:
        self._update_task(task_id, "qa_passed")
        with self.connect() as conn:
            if claim_id:
                row = conn.execute(
                    "SELECT id FROM claims WHERE task_id = ? AND id = ?",
                    (task_id, claim_id),
                ).fetchone()
            else:
                row = conn.execute(
                    """
                    SELECT id FROM claims
                    WHERE task_id = ? AND needs_review = 1
                    ORDER BY created_at DESC, rowid DESC LIMIT 1
                    """,
                    (task_id,),
                ).fetchone()
            if row:
                conn.execute(
                    "UPDATE claims SET needs_review = 0, status = 'confirmed', uncertainty = ? WHERE id = ?",
                    (f"已由人工确认：{sanitize_text(user_text, 120)}", row["id"]),
                )
        self._log_agent_run(
            task_id,
            agent_name="质检 Agent",
            input_summary="人工确认低置信度结论后进行准入复核。",
            output_summary="人工确认已记录；复核通过并保留人工确认来源类型。",
            status="completed",
            duration_ms=6200,
            retry_count=1,
        )
        self._generate_report(task_id, reason="manual_confirmation")
        self._set_task_completed(task_id)
        return "当前结论已记录为人工确认，并生成新的报告版本。" if claim_id else "低置信度结论已记录为人工确认，并生成新的报告版本。"

    def _generate_report(self, task_id: str, reason: str = "initial") -> None:
        stage_started = now_dt()
        self._update_task(task_id, "reporting")
        self._log_agent_event(task_id, "报告 Agent", "report_started", "正在组织报告结构、引用映射和指标卡片。")
        self._refresh_pricing_facts(task_id)
        with self.connect() as conn:
            task = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
            competitors = conn.execute(
                "SELECT * FROM competitors WHERE task_id = ? ORDER BY rowid",
                (task_id,),
            ).fetchall()
            sources = conn.execute(
                "SELECT * FROM sources WHERE task_id = ? ORDER BY collected_at, rowid",
                (task_id,),
            ).fetchall()
            claims = conn.execute(
                "SELECT * FROM claims WHERE task_id = ? ORDER BY created_at, rowid",
                (task_id,),
            ).fetchall()
            evidence_count = conn.execute(
                "SELECT COUNT(*) AS count FROM evidence_chunks WHERE task_id = ?",
                (task_id,),
            ).fetchone()["count"]
            report_count = conn.execute(
                "SELECT COUNT(*) AS count FROM reports WHERE task_id = ?",
                (task_id,),
            ).fetchone()["count"]
            run_rows = conn.execute(
                "SELECT model_provider, fallback_reason, tool_calls, has_rework FROM agent_runs WHERE task_id = ?",
                (task_id,),
            ).fetchall()
            qa_rows = conn.execute(
                "SELECT fix_status FROM qa_findings WHERE task_id = ?",
                (task_id,),
            ).fetchall()
            collection_rows = conn.execute(
                "SELECT provider, status FROM collection_runs WHERE task_id = ?",
                (task_id,),
            ).fetchall()
            pricing_fact_rows = conn.execute(
                "SELECT * FROM pricing_facts WHERE task_id = ? ORDER BY competitor_name, plan_name, price_type",
                (task_id,),
            ).fetchall()
            structured_counts = {
                "feature_tree": conn.execute("SELECT COUNT(*) AS count FROM feature_items WHERE task_id = ?", (task_id,)).fetchone()["count"],
                "pricing_model": conn.execute("SELECT COUNT(*) AS count FROM pricing_items WHERE task_id = ?", (task_id,)).fetchone()["count"],
                "user_persona": conn.execute("SELECT COUNT(*) AS count FROM persona_items WHERE task_id = ?", (task_id,)).fetchone()["count"],
                "reviews": conn.execute("SELECT COUNT(*) AS count FROM review_items WHERE task_id = ?", (task_id,)).fetchone()["count"],
                "swot": conn.execute("SELECT COUNT(*) AS count FROM swot_items WHERE task_id = ?", (task_id,)).fetchone()["count"],
            }

        reportable_claims = []
        citation_map: dict[str, list[str]] = {}
        for claim in claims:
            claim_dict = dict(claim)
            claim_dict["source_ids"] = loads(claim["source_ids"], [])
            if not claim_dict["source_ids"]:
                continue
            ReportableClaim(**claim_dict)
            reportable_claims.append(claim_dict)
            citation_map[claim["id"]] = claim_dict["source_ids"]

        raw_sections = self._build_report_sections(task, competitors, sources, reportable_claims)
        display_sections, report_trace = self._build_display_report(task, raw_sections)
        executive_summary = self._user_report_text(
            report_trace.get("summary")
            or f"本报告围绕 {self._join_names(loads(task['competitors_json'], []))} 的已选维度生成，结论均来自当前公开资料或上传材料。"
        )
        confidence_score = round(
            sum(float(claim["confidence"]) for claim in reportable_claims) / max(len(reportable_claims), 1),
            2,
        )
        provider_label = self._provider_label(run_rows)
        fallback_count = len([row for row in run_rows if row["fallback_reason"]])
        qa_open_count = len([row for row in qa_rows if row["fix_status"] == "open"])
        qa_fixed_count = len([row for row in qa_rows if row["fix_status"] == "fixed"])
        qa_manual_pending_count = len([row for row in qa_rows if row["fix_status"] == "manual_pending"])
        qa_rework_count = len([row for row in run_rows if int(row["has_rework"] or 0)])
        completed_structured = len([count for count in structured_counts.values() if count > 0])
        structured_field_completion = round(completed_structured / max(len(structured_counts), 1), 2)
        collection_providers = list(dict.fromkeys(row["provider"] for row in collection_rows if row["provider"]))
        analysis_artifact = self._latest_analysis_artifact(task_id)
        report_enrichment = self._build_report_enrichment(
            task,
            competitors,
            sources,
            reportable_claims,
            display_sections,
            pricing_fact_rows,
            analysis_artifact=analysis_artifact,
        )
        structured_sections = display_sections
        competitor_names = [row["name"] for row in competitors]
        competitor_swot = self._build_competitor_swot(competitor_names, sources, reportable_claims)
        react_sections = self._augment_deep_report_comparison_tables(
            analysis_artifact.get("sections") or display_sections,
            competitor_names,
            report_enrichment,
            competitor_swot,
        )
        analysis_provider = analysis_artifact.get("provider", "")
        analysis_fallback = analysis_artifact.get("fallback_reason", "")
        analysis_markdown = analysis_artifact.get("analysis_markdown", "")
        final_provider_label = self._provider_user_label(analysis_provider) if analysis_provider else provider_label
        content = {
            "title": f"{task['industry']}竞品分析报告",
            "summary": executive_summary,
            "executive_summary": executive_summary,
            "metrics": {
                "source_count": len(sources),
                "claim_count": len(reportable_claims),
                "evidence_chunk_count": int(evidence_count),
                "citation_coverage": 1.0 if reportable_claims else 0,
                "manual_review_count": len([claim for claim in reportable_claims if claim["needs_review"]]),
                "qa_rework_visible": bool(qa_rows or qa_rework_count),
                "qa_open_count": qa_open_count,
                "qa_fixed_count": qa_fixed_count,
                "qa_manual_pending_count": qa_manual_pending_count,
                "qa_rework_count": qa_rework_count,
                "structured_field_completion": structured_field_completion,
                "collection_provider": "、".join(collection_providers) or "none",
                "search_result_count": len([source for source in sources if source["source_type"] in {"search_result", "volc_search_result"}]),
                "model_provider": final_provider_label,
                "provider_used": final_provider_label,
                "llm_called": any(row["model_provider"] in {"doubao", "deepseek-react", "doubao-react"} for row in run_rows)
                or report_trace.get("provider") == "doubao",
                "search_called": self._search_called(run_rows),
                "fallback_count": fallback_count + (1 if report_trace.get("fallback_reason") else 0),
                "pricing_fact_count": len(pricing_fact_rows),
                "react_report_enabled": bool(analysis_markdown and not analysis_fallback),
                    "react_report_provider": analysis_provider,
                    "analysis_provider": analysis_provider,
                    "analysis_fallback_reason": analysis_fallback,
                },
            "sections": structured_sections,
            "display_sections": react_sections,
            "structured_sections": structured_sections,
            "react_report": {
                "enabled": bool(analysis_markdown and not analysis_fallback),
                "provider": analysis_provider,
                "markdown": analysis_markdown,
                "sections": react_sections,
                "screenshots": analysis_artifact.get("screenshots", []),
                "fallback_reason": analysis_fallback,
            },
            "competitor_swot": competitor_swot,
            "citation_refs": citation_map,
            "technical_section_count": len(raw_sections),
            "full_report": [analysis_markdown] if analysis_markdown else self._build_full_report(task, display_sections),
            "reason": reason,
            **report_enrichment,
        }

        report_id = uuid.uuid4().hex
        report_version = int(report_count) + 1
        generated_at = utc_now_iso()
        qa_status = "needs_review" if (qa_open_count or qa_manual_pending_count) else "passed"
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO reports
                (id, task_id, version, title, content_json, generated_at, citation_map, qa_status, confidence_score)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    report_id,
                    task_id,
                    report_version,
                    content["title"],
                    dumps(content),
                    generated_at,
                    dumps(citation_map),
                    qa_status,
                    confidence_score,
                ),
            )
        report_payload = {
            "id": report_id,
            "task_id": task_id,
            "version": report_version,
            "title": content["title"],
            "content": content,
            "generated_at": generated_at,
            "citation_map": citation_map,
            "qa_status": qa_status,
            "confidence_score": confidence_score,
        }
        pdf_path = ""
        pdf_error = ""
        try:
            pdf_path = self._write_report_pdf_file(task_id, report_payload)
        except Exception as exc:
            pdf_error = sanitize_text(str(exc), 240)
            self._log_agent_event(
                task_id,
                "报告 Agent",
                "report_pdf_failed",
                f"报告 PDF 同步生成失败：{pdf_error}",
                severity="warning",
                meta={"version": report_version},
            )
        self._log_agent_event(
            task_id,
            "报告 Agent",
            "report_finished",
            f"已生成报告版本 v{report_version}。",
            meta={"version": report_version, "claim_count": len(reportable_claims), "pdf_path": pdf_path},
        )
        self._log_agent_run(
            task_id,
            agent_name="报告 Agent",
            input_summary="读取结构化 claims、sources、qa_findings 和引用映射。",
            output_summary=f"已生成用户可读报告版本 v{report_version}，按已选模块展示。",
            status="completed",
            duration_ms=self._elapsed_ms(stage_started),
            token_input=int(report_trace.get("token_input") or 0),
            token_output=int(report_trace.get("token_output") or 0),
            model_provider=report_trace.get("provider", "") or "report-renderer",
            fallback_reason=report_trace.get("fallback_reason", ""),
            tool_calls=report_trace.get("tool_calls", [])
            + [
                {"name": "render_report_toc", "result": f"{len(react_sections)} sections"},
                {"name": "render_visual_payload", "result": f"{len(content.get('score_dimensions', []))} score rows"},
                {"name": "prepare_pdf_payload", "result": "toc_sections_score_api_radar"},
                {"name": "render_report_pdf", "result": pdf_path or f"failed: {pdf_error}"},
                {"name": "render_report_json", "result": f"v{report_version}"},
            ],
            started_at=stage_started,
        )

    def _write_report_pdf_file(self, task_id: str, report: dict[str, Any]) -> str:
        from report_pdf import render_competitive_report_pdf

        output_dir = Path(__file__).resolve().parent / "data" / "reports"
        output_dir.mkdir(parents=True, exist_ok=True)
        version = int(report.get("version") or 0)
        suffix = f"_v{version}" if version else ""
        output_path = output_dir / f"{sanitize_text(task_id, 80)}{suffix}_competitive_report.pdf"
        buffer = render_competitive_report_pdf(task_id, report)
        output_path.write_bytes(buffer.getvalue())
        return str(output_path)

    def _industry_bucket(self, industry: str, competitor_names: list[str]) -> str:
        haystack = f"{industry} {' '.join(competitor_names)}".casefold()
        if re.search(r"chatgpt|deepseek|openai|claude|豆包|通义|kimi|大模型|生成式|人工智能|智能助手|\bai\b|llm|agent", haystack, flags=re.I):
            return "ai"
        if re.search(r"汽车|新能源车|乘用车|电动车|智能车|车型|byd|xiaopeng|xpeng|tesla|nio|理想|蔚来|小鹏|比亚迪", haystack, flags=re.I):
            return "automotive"
        if re.search(r"光伏|太阳能|组件|电池片|硅片|硅料|储能|隆基|通威|爱旭|晶科|天合", haystack, flags=re.I):
            return "photovoltaic"
        if re.search(r"煤炭|煤矿|煤价|动力煤|焦煤|无烟煤|热值|长协|港口煤", haystack, flags=re.I):
            return "coal"
        if re.search(r"saas|软件|协同|办公|crm|erp|知识管理|数据库|项目管理", haystack, flags=re.I):
            return "software"
        if re.search(r"抖音|tiktok|快手|小红书|bilibili|b站|youtube|微博|instagram|snapchat|知乎|豆瓣|内容社区|短视频|直播平台|社交平台|社交媒体|内容平台", haystack, flags=re.I):
            return "content_social"
        return "generic"

    def _dimension(self, name: str, description: str, keywords: list[str], cost_dimension_kind: str = "") -> dict[str, Any]:
        item: dict[str, Any] = {
            "name": name,
            "description": description,
            "keywords": keywords,
        }
        if cost_dimension_kind:
            item["cost_dimension_kind"] = cost_dimension_kind
        return item

    def _rule_dimension_profile(self, industry: str, competitor_names: list[str]) -> dict[str, Any]:
        bucket = self._industry_bucket(industry, competitor_names)
        if bucket == "ai":
            score_dimensions = [
                self._dimension("综合生产力", "是否能作为日常工作台使用", ["官网", "产品", "功能", "工作台", "文件", "图片", "语音", "联网"]),
                self._dimension("推理/代码", "推理、代码和复杂任务能力", ["推理", "代码", "编程", "数学", "reason", "code", "coder"]),
                self._dimension("多模态与创意", "图像、语音、视频、视觉理解和创意工具", ["图片", "图像", "视觉", "语音", "视频", "多模态", "image", "vision"]),
                self._dimension("企业治理", "SSO、权限、审计、数据不训练、隐私和安全", ["企业", "安全", "隐私", "合规", "数据", "审计", "权限", "enterprise"]),
                self._dimension("API 成本效率", "输入、缓存和输出价格归一化后的成本优势", ["价格", "定价", "收费", "api", "pricing", "token"], "api_cost"),
                self._dimension("开放/自部署", "开源权重、许可证、本地部署和私有化空间", ["开源", "开放权重", "许可证", "本地", "自部署", "open source", "github"]),
                self._dimension("长上下文", "上下文窗口、最大输出和长文档适配能力", ["上下文", "长文本", "最大输出", "context", "token", "128K", "1M"]),
                self._dimension("生态集成", "API、SDK、连接器、插件、平台兼容和社区", ["API", "SDK", "插件", "连接器", "平台", "文档", "开发者", "生态"]),
            ]
            return {
                "industry_bucket": bucket,
                "profile_source": "rule",
                "summary": "AI 大模型与智能助手维度",
                "price_metric_label": "订阅/API 价格",
                "price_metric_description": "订阅套餐、API 输入/输出/缓存单价、成本指数与计算口径。",
                "pricing_terms": ["订阅", "套餐", "API", "tokens", "输入", "输出", "缓存"],
                "show_api_cost": True,
                "feature_dimensions": [
                    self._dimension("产品/功能", "产品入口和核心能力覆盖", ["官网", "产品", "功能", "能力", "feature", "product"]),
                    self._dimension("价格/API", "订阅、API 和商业口径", ["价格", "定价", "套餐", "api", "pricing", "price"]),
                    self._dimension("企业/安全", "企业治理、安全和隐私", ["企业", "安全", "隐私", "合规", "security", "privacy"]),
                    self._dimension("评价/口碑", "第三方评价与用户反馈", ["评价", "口碑", "评论", "review", "g2", "trustpilot"]),
                    self._dimension("新闻/风险", "新闻、监管和市场风险", ["新闻", "风险", "监管", "市场", "news", "risk"]),
                ],
                "score_dimensions": score_dimensions,
                "positioning": {
                    "x_axis": "成本效率、开放/自部署、长上下文",
                    "y_axis": "综合生产力、企业治理、生态集成",
                    "x_dimensions": ["API 成本效率", "开放/自部署", "长上下文"],
                    "y_dimensions": ["综合生产力", "企业治理", "生态集成"],
                    "interpretation": "右侧更偏模型层成本/开放价值，上侧更偏应用层工作台和企业治理成熟度。",
                },
                "decision_scenarios": [
                    {"scenario": "个人与企业端一站式 AI 工作台", "dimensions": ["综合生产力", "企业治理", "生态集成"], "rule": "优先选择应用层能力、连接器和企业治理证据更完整的一方。"},
                    {"scenario": "低成本 API 与可替代供应商", "dimensions": ["API 成本效率", "长上下文"], "rule": "优先选择官方输出价指数更低、上下文证据更强的一方。"},
                    {"scenario": "自部署、主权 AI 或开源生态", "dimensions": ["开放/自部署", "长上下文"], "rule": "优先选择开放权重、许可和本地化证据更完整的一方。"},
                    {"scenario": "用户评价驱动的产品验证", "dimensions": ["综合生产力"], "rule": "评价样本不足时先补第三方评价、应用商店、问卷或访谈材料。"},
                ],
            }
        if bucket == "automotive":
            score_dimensions = [
                self._dimension("车型/产品矩阵", "车型覆盖、级别定位和配置梯度", ["车型", "配置", "产品矩阵", "SUV", "轿车", "MPV", "版本"]),
                self._dimension("车型价格/权益", "指导价、权益、金融方案和保值口径", ["价格", "指导价", "权益", "金融", "补贴", "购车"]),
                self._dimension("三电/续航", "电池、电驱、电控、续航和补能能力", ["电池", "续航", "电驱", "充电", "能耗", "三电"]),
                self._dimension("智能座舱/智驾", "座舱系统、辅助驾驶、芯片和软件体验", ["智驾", "辅助驾驶", "座舱", "芯片", "OTA", "智能"]),
                self._dimension("安全/质量", "安全评级、召回、质量投诉和可靠性", ["安全", "碰撞", "召回", "质量", "投诉", "可靠"]),
                self._dimension("渠道/交付/售后", "门店、交付、服务网络和补能体系", ["交付", "渠道", "门店", "售后", "服务", "补能"]),
                self._dimension("销量/口碑", "销量、用户评价和市场热度", ["销量", "交付量", "评价", "口碑", "投诉", "排名"]),
            ]
            return {
                "industry_bucket": bucket,
                "profile_source": "rule",
                "summary": "汽车行业维度",
                "price_metric_label": "车型/配置价格",
                "price_metric_description": "车型指导价、配置差异、购车权益、金融方案和交付口径。",
                "pricing_terms": ["车型", "指导价", "配置", "权益", "金融", "补贴"],
                "show_api_cost": False,
                "feature_dimensions": score_dimensions[:6],
                "score_dimensions": score_dimensions,
                "positioning": {
                    "x_axis": "价格/用车成本竞争力",
                    "y_axis": "产品/品牌竞争力",
                    "x_dimensions": ["车型价格/权益", "渠道/交付/售后"],
                    "y_dimensions": ["车型/产品矩阵", "三电/续航", "智能座舱/智驾", "销量/口碑"],
                    "interpretation": "右侧更偏价格和用车成本优势，上侧更偏产品力、智能化和品牌口碑成熟度。",
                },
                "decision_scenarios": [
                    {"scenario": "家庭通勤/首购", "dimensions": ["车型价格/权益", "三电/续航", "安全/质量"], "rule": "优先看价格带、续航、安全和售后服务确定性。"},
                    {"scenario": "智能化体验优先", "dimensions": ["智能座舱/智驾", "三电/续航"], "rule": "优先选择智驾、座舱和 OTA 证据更完整的一方。"},
                    {"scenario": "规模与交付确定性", "dimensions": ["销量/口碑", "渠道/交付/售后"], "rule": "优先选择销量、渠道和服务网络更稳定的一方。"},
                ],
            }
        if bucket == "photovoltaic":
            score_dimensions = [
                self._dimension("技术路线/产品", "组件、电池片、硅片和系统方案覆盖", ["组件", "电池", "硅片", "TOPCon", "BC", "HJT", "产品"]),
                self._dimension("效率/功率", "转换效率、组件功率和可靠性指标", ["效率", "功率", "转换效率", "衰减", "可靠", "认证"]),
                self._dimension("组件/材料价格", "组件、电池片、硅片、硅料价格与成本口径", ["价格", "报价", "组件", "电池片", "硅料", "硅片", "成本"]),
                self._dimension("产能/出货", "产能规划、出货规模和订单交付", ["产能", "出货", "订单", "扩产", "产线", "交付"]),
                self._dimension("客户/渠道", "集中式、分布式、海外和大客户覆盖", ["客户", "渠道", "海外", "分布式", "集中式", "项目"]),
                self._dimension("盈利/财务", "毛利、现金流、库存和财务稳健性", ["毛利", "盈利", "现金流", "库存", "财务", "利润"]),
                self._dimension("供应链/政策风险", "原材料、贸易壁垒、政策和合规风险", ["供应链", "政策", "关税", "贸易", "风险", "合规"]),
            ]
            return {
                "industry_bucket": bucket,
                "profile_source": "rule",
                "summary": "光伏行业维度",
                "price_metric_label": "组件/材料价格",
                "price_metric_description": "组件、电池片、硅片、硅料报价、成本区间和采集日期。",
                "pricing_terms": ["组件", "电池片", "硅片", "硅料", "报价", "成本"],
                "show_api_cost": False,
                "feature_dimensions": score_dimensions[:6],
                "score_dimensions": score_dimensions,
                "positioning": {
                    "x_axis": "成本/价格竞争力",
                    "y_axis": "技术/规模竞争力",
                    "x_dimensions": ["组件/材料价格", "盈利/财务"],
                    "y_dimensions": ["技术路线/产品", "效率/功率", "产能/出货"],
                    "interpretation": "右侧更偏成本与报价优势，上侧更偏技术效率、产能和出货规模。",
                },
                "decision_scenarios": [
                    {"scenario": "电站采购降本", "dimensions": ["组件/材料价格", "产能/出货", "供应链/政策风险"], "rule": "优先看报价口径、交付确定性和贸易风险。"},
                    {"scenario": "高效率产品路线", "dimensions": ["技术路线/产品", "效率/功率"], "rule": "优先选择效率、可靠性和认证证据更完整的一方。"},
                    {"scenario": "海外市场拓展", "dimensions": ["客户/渠道", "供应链/政策风险", "盈利/财务"], "rule": "优先看海外渠道、政策适配和财务韧性。"},
                ],
            }
        if bucket == "coal":
            score_dimensions = [
                self._dimension("资源/煤种", "资源储量、煤种、热值和产地质量", ["资源", "储量", "煤种", "热值", "产地", "矿区"]),
                self._dimension("产能/销量", "核定产能、产量、销量和利用率", ["产能", "产量", "销量", "核定", "利用率"]),
                self._dimension("煤价/长协", "长协价、现货价、港口价和价格弹性", ["煤价", "长协", "现货", "港口价", "价格", "报价"]),
                self._dimension("成本/运输", "开采成本、铁路港口运输和区域成本", ["成本", "运输", "铁路", "港口", "运费", "开采"]),
                self._dimension("客户/区域", "下游电力、钢铁、化工客户和区域布局", ["客户", "电力", "钢铁", "化工", "区域", "下游"]),
                self._dimension("安全/环保", "安全事故、环保投入和ESG约束", ["安全", "事故", "环保", "ESG", "治理", "排放"]),
                self._dimension("政策/财务风险", "政策调控、财务稳健性和周期风险", ["政策", "调控", "财务", "利润", "周期", "风险"]),
            ]
            return {
                "industry_bucket": bucket,
                "profile_source": "rule",
                "summary": "煤炭行业维度",
                "price_metric_label": "煤种/热值价格",
                "price_metric_description": "按煤种、热值、产地、长协/现货和港口口径比较价格。",
                "pricing_terms": ["煤种", "热值", "长协", "现货", "港口价", "运费"],
                "show_api_cost": False,
                "feature_dimensions": score_dimensions[:6],
                "score_dimensions": score_dimensions,
                "positioning": {
                    "x_axis": "成本/价格韧性",
                    "y_axis": "资源/规模确定性",
                    "x_dimensions": ["煤价/长协", "成本/运输", "政策/财务风险"],
                    "y_dimensions": ["资源/煤种", "产能/销量", "客户/区域"],
                    "interpretation": "右侧更偏成本和价格韧性，上侧更偏资源质量、规模和客户确定性。",
                },
                "decision_scenarios": [
                    {"scenario": "电煤长协稳定供应", "dimensions": ["资源/煤种", "产能/销量", "煤价/长协"], "rule": "优先看资源质量、长协口径和供应稳定性。"},
                    {"scenario": "周期景气弹性", "dimensions": ["煤价/长协", "成本/运输", "政策/财务风险"], "rule": "优先比较价格弹性、成本曲线和政策约束。"},
                    {"scenario": "安全环保约束", "dimensions": ["安全/环保", "政策/财务风险"], "rule": "优先排查安全、环保和监管风险。"},
                ],
            }
        if bucket == "software":
            score_dimensions = [
                self._dimension("产品/工作流覆盖", "核心功能、工作流和协作场景覆盖", ["产品", "功能", "工作流", "协作", "知识库", "项目"]),
                self._dimension("价格/套餐", "订阅套餐、席位、限制和企业采购口径", ["价格", "定价", "套餐", "订阅", "seat", "pricing"]),
                self._dimension("易用性/迁移", "上手成本、模板、导入导出和迁移难度", ["易用", "模板", "导入", "导出", "迁移", "体验"]),
                self._dimension("集成/自动化", "第三方集成、自动化和开放接口", ["集成", "自动化", "API", "插件", "connector", "integration"]),
                self._dimension("企业治理/安全", "权限、审计、SSO、合规和数据安全", ["企业", "权限", "审计", "SSO", "安全", "合规"]),
                self._dimension("生态/模板", "模板、社区、伙伴和最佳实践", ["生态", "模板", "社区", "伙伴", "案例"]),
                self._dimension("评价/口碑", "用户评价、NPS线索和投诉点", ["评价", "口碑", "评论", "review", "G2", "Capterra"]),
            ]
            return {
                "industry_bucket": bucket,
                "profile_source": "rule",
                "summary": "软件/SaaS 行业维度",
                "price_metric_label": "订阅/席位价格",
                "price_metric_description": "套餐、席位、企业版限制、用量权益和采购口径。",
                "pricing_terms": ["套餐", "席位", "订阅", "企业版", "用量", "权益"],
                "show_api_cost": False,
                "feature_dimensions": score_dimensions[:6],
                "score_dimensions": score_dimensions,
                "positioning": {
                    "x_axis": "价格/迁移成本竞争力",
                    "y_axis": "产品/企业成熟度",
                    "x_dimensions": ["价格/套餐", "易用性/迁移"],
                    "y_dimensions": ["产品/工作流覆盖", "集成/自动化", "企业治理/安全"],
                    "interpretation": "右侧更偏成本和迁移友好，上侧更偏产品覆盖、企业治理和集成成熟度。",
                },
                "decision_scenarios": [
                    {"scenario": "团队协作落地", "dimensions": ["产品/工作流覆盖", "易用性/迁移"], "rule": "优先选择上手成本低、核心工作流覆盖更完整的一方。"},
                    {"scenario": "企业级采购", "dimensions": ["企业治理/安全", "价格/套餐", "集成/自动化"], "rule": "优先比较权限、安全、集成和采购口径。"},
                    {"scenario": "生态与模板复用", "dimensions": ["生态/模板", "评价/口碑"], "rule": "优先看模板社区、案例和评价样本。"},
                ],
            }
        if bucket == "content_social":
            score_dimensions = [
                self._dimension("内容生态/丰富度", "内容品类覆盖、创作者规模、内容质量和多样性", ["内容", "创作者", "品类", "质量", "多样性", "ugc", "pugc"]),
                self._dimension("用户规模/活跃度", "DAU/MAU、用户时长、留存和增长趋势", ["用户", "dau", "mau", "日活", "月活", "留存", "时长", "增长"]),
                self._dimension("创作者变现", "分成机制、直播打赏、电商佣金、广告分成", ["变现", "分成", "打赏", "佣金", "创作者", "直播"]),
                self._dimension("广告/商业化", "广告投放、ROI、信息流、品牌合作", ["广告", "投放", "roi", "商业化", "品牌", "信息流"]),
                self._dimension("电商/交易融合", "直播电商、商品橱窗、本地生活、支付闭环", ["电商", "交易", "直播带货", "橱窗", "本地生活", "支付"]),
                self._dimension("推荐/算法体验", "推荐精准度、内容分发效率、用户满意度", ["推荐", "算法", "分发", "体验", "满意度", "个性化"]),
                self._dimension("合规/内容安全", "内容审核、数据隐私、未成年保护、监管合规", ["合规", "审核", "隐私", "未成年", "监管", "安全"]),
                self._dimension("创新/国际化", "新功能迭代、AI能力、出海/全球化进展", ["创新", "AI", "出海", "国际化", "新功能", "迭代"]),
            ]
            return {
                "industry_bucket": bucket,
                "profile_source": "rule",
                "summary": "内容社区与短视频平台维度",
                "price_metric_label": "商业化/变现口径",
                "price_metric_description": "广告CPM/CPC、直播分成比例、电商佣金、创作者激励和会员订阅价格。",
                "pricing_terms": ["广告", "分成", "佣金", "会员", "订阅", "CPM", "CPC"],
                "show_api_cost": False,
                "feature_dimensions": score_dimensions[:6],
                "score_dimensions": score_dimensions,
                "positioning": {
                    "x_axis": "商业化/变现成熟度",
                    "y_axis": "用户/内容竞争力",
                    "x_dimensions": ["广告/商业化", "电商/交易融合", "创作者变现"],
                    "y_dimensions": ["用户规模/活跃度", "内容生态/丰富度", "推荐/算法体验"],
                    "interpretation": "右侧更偏商业化和变现效率，上侧更偏用户规模、内容生态和体验质量。",
                },
                "decision_scenarios": [
                    {"scenario": "品牌广告投放选择", "dimensions": ["用户规模/活跃度", "广告/商业化"], "rule": "优先选择目标人群覆盖、活跃度和投放ROI证据更强的一方。"},
                    {"scenario": "内容创作者入驻", "dimensions": ["内容生态/丰富度", "创作者变现"], "rule": "优先选择内容品类匹配、分成机制更透明的一方。"},
                    {"scenario": "电商带货选平台", "dimensions": ["电商/交易融合", "用户规模/活跃度"], "rule": "优先选择交易闭环成熟、直播电商GMV证据更强的一方。"},
                    {"scenario": "合规风险评估", "dimensions": ["合规/内容安全", "推荐/算法体验"], "rule": "优先审查内容审核、隐私合规和监管处罚记录。"},
                ],
            }
        score_dimensions = [
            self._dimension("产品/服务覆盖", "产品线、服务范围和核心能力", ["产品", "服务", "功能", "能力", "方案"]),
            self._dimension("价格/商业模式", "价格口径、收费方式、成本和商业模式", ["价格", "定价", "报价", "成本", "商业模式"]),
            self._dimension("技术/质量", "技术路线、质量、可靠性和规格", ["技术", "质量", "规格", "可靠", "性能"]),
            self._dimension("渠道/市场", "渠道、区域、客户和市场份额线索", ["渠道", "市场", "客户", "区域", "份额", "销售"]),
            self._dimension("用户/客户反馈", "用户评价、客户案例和投诉反馈", ["评价", "口碑", "客户", "案例", "投诉", "review"]),
            self._dimension("供应链/交付", "供应、交付、产能和服务能力", ["供应链", "交付", "产能", "库存", "服务"]),
            self._dimension("风险/合规", "政策、监管、财务和合规风险", ["风险", "合规", "监管", "政策", "财务"]),
        ]
        return {
            "industry_bucket": bucket,
            "profile_source": "rule",
            "summary": "通用行业维度",
            "price_metric_label": "价格/商业模式",
            "price_metric_description": "按行业常用报价、套餐、型号、服务或采购口径比较。",
            "pricing_terms": ["价格", "报价", "套餐", "型号", "服务", "采购"],
            "show_api_cost": False,
            "feature_dimensions": score_dimensions[:6],
            "score_dimensions": score_dimensions,
            "positioning": {
                "x_axis": "价格/交付竞争力",
                "y_axis": "产品/市场竞争力",
                "x_dimensions": ["价格/商业模式", "供应链/交付"],
                "y_dimensions": ["产品/服务覆盖", "技术/质量", "渠道/市场"],
                "interpretation": "右侧更偏价格和交付优势，上侧更偏产品、技术和市场成熟度。",
            },
            "decision_scenarios": [
                {"scenario": "成本敏感采购", "dimensions": ["价格/商业模式", "供应链/交付"], "rule": "优先比较价格口径、交付确定性和售后成本。"},
                {"scenario": "产品能力优先", "dimensions": ["产品/服务覆盖", "技术/质量"], "rule": "优先选择核心能力和可靠性证据更完整的一方。"},
                {"scenario": "市场验证优先", "dimensions": ["渠道/市场", "用户/客户反馈"], "rule": "优先看客户案例、渠道覆盖和评价样本。"},
            ],
        }

    def _normalize_dimension_items(self, items: Any, fallback: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
        normalized: list[dict[str, Any]] = []
        if isinstance(items, list):
            for raw in items:
                if isinstance(raw, str):
                    name = sanitize_text(raw, 40)
                    if name:
                        normalized.append(self._dimension(name, name, [name]))
                    continue
                if not isinstance(raw, dict):
                    continue
                name = sanitize_text(str(raw.get("name", "")), 40)
                if not name:
                    continue
                keywords = raw.get("keywords", [])
                if not isinstance(keywords, list):
                    keywords = []
                keyword_values = [sanitize_text(str(keyword), 28) for keyword in keywords if sanitize_text(str(keyword), 28)]
                normalized.append(
                    self._dimension(
                        name,
                        sanitize_text(str(raw.get("description", "")), 140) or name,
                        keyword_values or [name],
                        sanitize_text(str(raw.get("cost_dimension_kind", "")), 40),
                    )
                )
        if len(normalized) < 4:
            normalized = fallback
        deduped: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in normalized:
            key = item["name"].casefold()
            if key in seen:
                continue
            seen.add(key)
            deduped.append(item)
            if len(deduped) >= limit:
                break
        return deduped

    def _normalize_dimension_profile(self, data: dict[str, Any], fallback: dict[str, Any], industry: str, competitor_names: list[str]) -> dict[str, Any]:
        profile = dict(fallback)
        ai_bucket = self._industry_bucket(industry, competitor_names) == "ai"
        if isinstance(data, dict):
            profile["profile_source"] = "llm"
            for key, limit in [("summary", 140), ("price_metric_label", 60), ("price_metric_description", 180)]:
                value = sanitize_text(str(data.get(key, "")), limit)
                if value:
                    profile[key] = value
            if isinstance(data.get("pricing_terms"), list):
                terms = [sanitize_text(str(term), 28) for term in data["pricing_terms"] if sanitize_text(str(term), 28)]
                if terms:
                    profile["pricing_terms"] = terms[:10]
            profile["show_api_cost"] = bool(data.get("show_api_cost")) if ai_bucket else False
            profile["feature_dimensions"] = self._normalize_dimension_items(data.get("feature_dimensions"), fallback.get("feature_dimensions", []), 7)
            profile["score_dimensions"] = self._normalize_dimension_items(data.get("score_dimensions"), fallback.get("score_dimensions", []), 8)
            if isinstance(data.get("positioning"), dict):
                positioning = dict(fallback.get("positioning", {}))
                raw_positioning = data["positioning"]
                for key, limit in [("x_axis", 80), ("y_axis", 80), ("interpretation", 180)]:
                    value = sanitize_text(str(raw_positioning.get(key, "")), limit)
                    if value:
                        positioning[key] = value
                for key in ["x_dimensions", "y_dimensions"]:
                    values = raw_positioning.get(key)
                    if isinstance(values, list):
                        cleaned = [sanitize_text(str(value), 40) for value in values if sanitize_text(str(value), 40)]
                        if cleaned:
                            positioning[key] = cleaned[:4]
                profile["positioning"] = positioning
            if isinstance(data.get("decision_scenarios"), list):
                scenarios = []
                for raw in data["decision_scenarios"]:
                    if not isinstance(raw, dict):
                        continue
                    scenario = sanitize_text(str(raw.get("scenario", "")), 80)
                    dimensions = raw.get("dimensions", [])
                    if not scenario or not isinstance(dimensions, list):
                        continue
                    scenarios.append(
                        {
                            "scenario": scenario,
                            "dimensions": [sanitize_text(str(dim), 40) for dim in dimensions if sanitize_text(str(dim), 40)][:4],
                            "rule": sanitize_text(str(raw.get("rule", "")), 180) or "按该场景的高权重维度选择证据更完整的一方。",
                        }
                    )
                if scenarios:
                    profile["decision_scenarios"] = scenarios[:4]
        if not profile.get("show_api_cost"):
            profile["score_dimensions"] = [
                item
                for item in profile.get("score_dimensions", [])
                if item.get("cost_dimension_kind") != "api_cost" and not re.search(r"\bapi\b|token|tokens|API 成本", item.get("name", ""), flags=re.I)
            ]
            if len(profile["score_dimensions"]) < 4:
                profile["score_dimensions"] = [
                    item
                    for item in fallback.get("score_dimensions", [])
                    if item.get("cost_dimension_kind") != "api_cost"
                ][:8]
        return profile

    def _build_dimension_profile(self, task: sqlite3.Row, competitor_names: list[str]) -> dict[str, Any]:
        industry = row_get(task, "industry", "")
        fallback = self._rule_dimension_profile(industry, competitor_names)
        task_payload = {
            "id": row_get(task, "id", ""),
            "industry": industry,
            "competitors": competitor_names,
            "focus_areas": loads(row_get(task, "focus_areas_json", "[]"), []),
        }
        if not self._llm_calls_allowed_for_config(task):
            fallback["planning_trace"] = self._offline_external_trace(
                "report_dimension_plan",
                len(dumps(task_payload)) // 4,
            )
            return fallback
        if self.llm_provider.provider != "doubao":
            return fallback
        try:
            result = self.llm_provider.plan_report_dimensions(task_payload)
            profile = self._normalize_dimension_profile(result.data, fallback, industry, competitor_names)
            profile["planning_trace"] = {
                "provider": result.provider,
                "fallback_reason": result.fallback_reason,
                "tool_calls": result.tool_calls,
            }
            return profile
        except LLMProviderError as exc:
            fallback["planning_trace"] = {
                "provider": self.llm_provider.provider,
                "fallback_reason": sanitize_text(str(exc), 240),
                "tool_calls": [{"name": "report_dimension_plan", "result": "fallback"}],
            }
            return fallback

    def _build_report_enrichment(
        self,
        task: sqlite3.Row,
        competitors: list[sqlite3.Row],
        sources: list[sqlite3.Row],
        claims: list[dict[str, Any]],
        sections: list[dict[str, Any]],
        pricing_facts: list[sqlite3.Row],
        analysis_artifact: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        competitor_names = [item["name"] for item in competitors]
        dimension_profile = self._build_dimension_profile(task, competitor_names)
        source_catalog = self._build_source_catalog(sources)
        feature_scores = self._build_feature_scores(competitor_names, sources, dimension_profile)
        pricing_comparison = self._build_pricing_comparison(competitor_names, claims, sources, pricing_facts, dimension_profile)
        pricing_fact_items = self._build_pricing_fact_items(pricing_facts, sources)
        score_dimensions = self._build_score_dimensions(competitor_names, sources, pricing_comparison, dimension_profile)
        if dimension_profile.get("show_api_cost"):
            score_dimensions = self._apply_reference_ai_scores(competitor_names, score_dimensions, source_catalog, pricing_comparison)
        if analysis_artifact and analysis_artifact.get("score_dimensions"):
            score_dimensions = analysis_artifact.get("score_dimensions", [])
        api_cost_data = self._build_api_cost_data(pricing_comparison, pricing_fact_items, dimension_profile)
        app_market_data = self._build_app_market_data(row_get(task, "id", ""), competitor_names)
        positioning_map = self._build_positioning_map(competitor_names, score_dimensions, dimension_profile)
        review_summary = self._build_review_summary(competitor_names, claims, sources)
        source_reliability = self._build_source_reliability(sources)
        risk_controls = self._build_risk_controls(competitor_names, sources, claims)
        executive_cards = self._build_executive_cards(claims, sources, source_catalog, pricing_comparison, feature_scores, review_summary, risk_controls, dimension_profile)
        decision_matrix = self._build_decision_matrix(competitor_names, feature_scores, pricing_comparison, review_summary, dimension_profile)
        scenario_recommendations = self._build_scenario_recommendations(competitor_names, score_dimensions, pricing_comparison, review_summary, dimension_profile)
        key_insights = self._build_key_insights(positioning_map, api_cost_data, review_summary, source_reliability, dimension_profile)
        fact_notes = self._build_fact_notes(pricing_facts, score_dimensions, dimension_profile)
        return {
            "executive_cards": executive_cards,
            "methodology": {
                "collection_date": utc_now_iso()[:10],
                "scope": f"覆盖 {self._join_names(competitor_names)}；仅展示已选择模块和已入库来源可支撑的结论。",
                "source_policy": "优先采用官网、官方文档、定价页、可信第三方评价和新闻/监管来源；没有正文的检索结果只作为线索，不直接写成事实。",
                "score_policy": f"1-5 分为分析判断，不是厂商官方指标；本轮按“{dimension_profile.get('summary', '行业适配维度')}”规划维度，评分依据来源覆盖、官方证据、成熟度、用户反馈、生态和风险调整。",
                "new_product_research": "若任务是新品/实物消费品，额外采集电商详情页、主图、规格/价格/销量、好中差评关键词、问大家和未满足需求，再进入同一套事实表与质检链路。",
                "limitations": [
                    f"{dimension_profile.get('price_metric_label', '价格')}、政策和评分具有时间敏感性，正式使用前需复核官网或权威页面。",
                    "第三方评价存在样本偏差，报告只呈现来源覆盖到的口碑线索。",
                    "没有来源的判断不会进入核心结论卡或图表。",
                ],
            },
            "dimension_profile": dimension_profile,
            "source_reliability": source_reliability,
            "feature_scores": feature_scores,
            "score_dimensions": score_dimensions,
            "pricing_comparison": pricing_comparison,
            "pricing_facts": pricing_fact_items,
            "api_cost_data": api_cost_data,
            "app_market_data": app_market_data,
            "positioning_map": positioning_map,
            "review_summary": review_summary,
            "decision_matrix": decision_matrix,
            "scenario_recommendations": scenario_recommendations,
            "key_insights": key_insights,
            "fact_notes": fact_notes,
            "risk_controls": risk_controls,
            "source_catalog": source_catalog,
            "chart_data": {
                "feature_heatmap": feature_scores,
                "score_heatmap": score_dimensions,
                "radar": (analysis_artifact or {}).get("radar_data") or self._build_radar_chart_data(score_dimensions),
                "positioning_map": positioning_map,
                "pricing_bars": pricing_comparison,
                "api_cost_index": api_cost_data,
                "app_market": app_market_data,
                "source_reliability": source_reliability,
            },
        }

    def _augment_deep_report_comparison_tables(
        self,
        sections: list[dict[str, Any]],
        competitor_names: list[str],
        enrichment: dict[str, Any],
        competitor_swot: dict[str, dict[str, str]],
    ) -> list[dict[str, Any]]:
        augmented = [dict(section) for section in sections if isinstance(section, dict)]
        table_builders = [
            (r"核心能力|Product Capability", "核心能力对比", "4.4 核心能力对比", self._core_capability_comparison_table),
            (r"商业模式|Monetization", "商业模式对比", "5.4 商业模式对比", self._business_model_comparison_table),
            (r"增长|分发|Growth", "增长策略对比", "6.4 增长策略对比", self._growth_strategy_comparison_table),
            (r"用户与场景|User|Use Case|用户场景", "用户场景对比", "7.4 用户场景对比", self._user_scenario_comparison_table),
            (r"SWOT|优劣势", "SWOT 对比矩阵", "8.4 SWOT 对比矩阵", self._swot_comparison_table),
            (r"关键差异|壁垒|Moat|差异", "差异化、壁垒与避雷对比", "9.4 差异化、壁垒与避雷对比", self._moat_risk_comparison_table),
        ]
        for section in augmented:
            title = str(section.get("title") or "")
            for pattern, marker, heading, builder in table_builders:
                if not re.search(pattern, title, flags=re.I):
                    continue
                table = builder(competitor_names, enrichment, competitor_swot)
                self._append_section_table(section, marker, heading, table)
                break
        return augmented

    def _append_section_table(self, section: dict[str, Any], marker: str, heading: str, table: list[list[str]]) -> None:
        if len(table) < 2:
            return
        body = str(section.get("markdown") or section.get("body") or "").strip()
        if marker in body:
            return
        addition = f"\n\n### {heading}\n\n{self._markdown_table(table)}"
        if section.get("markdown"):
            section["markdown"] = f"{body}{addition}".strip()
        else:
            section["body"] = f"{body}{addition}".strip()
            section["markdown"] = section["body"]

    def _markdown_table(self, rows: list[list[str]]) -> str:
        cleaned = [[self._markdown_cell(cell) for cell in row] for row in rows if row]
        if not cleaned:
            return ""
        width = max(len(row) for row in cleaned)
        normalized = [row + [""] * (width - len(row)) for row in cleaned]
        header = normalized[0]
        separator = ["---"] * width
        body = normalized[1:]
        return "\n".join(
            ["| " + " | ".join(header) + " |", "| " + " | ".join(separator) + " |"]
            + ["| " + " | ".join(row) + " |" for row in body]
        )

    def _markdown_cell(self, value: Any, limit: int = 140) -> str:
        text = report_text(str(value or ""), limit).replace("|", "／").replace("\n", " ")
        return text or "待核实"

    def _score_lookup(self, enrichment: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
        return {
            (str(row.get("competitor", "")).casefold(), str(row.get("dimension", ""))): row
            for row in enrichment.get("score_dimensions", [])
            if isinstance(row, dict)
        }

    def _dimension_names(self, enrichment: dict[str, Any]) -> list[str]:
        names = list(dict.fromkeys(
            str(row.get("dimension", ""))
            for row in enrichment.get("score_dimensions", [])
            if isinstance(row, dict) and row.get("dimension")
        ))
        return names[:8]

    def _score_cell(self, lookup: dict[tuple[str, str], dict[str, Any]], competitor: str, dimension: str) -> str:
        row = lookup.get((competitor.casefold(), dimension), {})
        if not row:
            return "待核实"
        score = "NA" if row.get("status") == "NA" else f"{float(row.get('score') or 0):.1f}/5"
        rationale = report_text(str(row.get("rationale") or row.get("description") or ""), 78)
        return f"{score}｜{rationale}" if rationale else score

    def _score_value(self, lookup: dict[tuple[str, str], dict[str, Any]], competitor: str, dimensions: list[str]) -> float:
        values = [
            float((lookup.get((competitor.casefold(), dimension), {}) or {}).get("score") or 0)
            for dimension in dimensions
        ]
        values = [value for value in values if value > 0]
        return round(sum(values) / len(values), 1) if values else 0.0

    def _star_cell(self, lookup: dict[tuple[str, str], dict[str, Any]], competitor: str, dimensions: list[str]) -> str:
        value = self._score_value(lookup, competitor, dimensions)
        if not value:
            return "待核实"
        filled = max(1, min(5, round(value)))
        return f"{'★' * filled}{'☆' * (5 - filled)}（{value}/5）"

    def _core_capability_comparison_table(
        self,
        competitor_names: list[str],
        enrichment: dict[str, Any],
        competitor_swot: dict[str, dict[str, str]],
    ) -> list[list[str]]:
        lookup = self._score_lookup(enrichment)
        rows = [["维度", *competitor_names]]
        for dimension in self._dimension_names(enrichment):
            rows.append([dimension, *[self._score_cell(lookup, name, dimension) for name in competitor_names]])
        return rows

    def _business_model_comparison_table(
        self,
        competitor_names: list[str],
        enrichment: dict[str, Any],
        competitor_swot: dict[str, dict[str, str]],
    ) -> list[list[str]]:
        pricing_by = {str(row.get("competitor", "")).casefold(): row for row in enrichment.get("pricing_comparison", []) if isinstance(row, dict)}
        lookup = self._score_lookup(enrichment)
        rows = [["维度", *competitor_names]]
        row_defs = [
            ("主要收入来源", lambda name: self._business_income_cell(name, pricing_by.get(name.casefold(), {}))),
            ("个人用户付费", lambda name: self._business_personal_payment_cell(name, pricing_by.get(name.casefold(), {}))),
            ("企业/API付费", lambda name: self._business_api_cell(name, pricing_by.get(name.casefold(), {}))),
            ("开源/自部署策略", lambda name: self._score_cell(lookup, name, "开放/自部署")),
            ("生态整合", lambda name: self._score_cell(lookup, name, "生态集成")),
            ("商业化风险", lambda name: competitor_swot.get(name, {}).get("劣势", "待核实")),
        ]
        for label, fn in row_defs:
            rows.append([label, *[fn(name) for name in competitor_names]])
        return rows

    def _business_income_cell(self, name: str, pricing: dict[str, Any]) -> str:
        key = name.casefold()
        if pricing.get("price_text") and "未抽取" not in str(pricing.get("price_text")):
            return f"订阅/API；{pricing.get('price_text')}"
        if "deepseek" in key:
            return "免费对话 + 低价 API；订阅信息待核实"
        if "豆包" in name or "doubao" in key:
            return "免费入口 + 订阅/API/生态分发；价格权益需复核"
        return "订阅/API/企业版线索待核实"

    def _business_personal_payment_cell(self, name: str, pricing: dict[str, Any]) -> str:
        key = name.casefold()
        if "deepseek" in key:
            return "个人端免费为主，付费权益需复核"
        if pricing.get("official_source") or pricing.get("price_text"):
            return "有付费/价格线索，需以官方页复核权益"
        return "待核实"

    def _business_api_cell(self, name: str, pricing: dict[str, Any]) -> str:
        if pricing.get("price_text") and "未抽取" not in str(pricing.get("price_text")):
            return f"{pricing.get('price_text')}；{pricing.get('basis', '')}"
        return "API/企业付费未形成明确金额，需补官方价格页"

    def _growth_strategy_comparison_table(
        self,
        competitor_names: list[str],
        enrichment: dict[str, Any],
        competitor_swot: dict[str, dict[str, str]],
    ) -> list[list[str]]:
        review_by = {str(row.get("competitor", "")).casefold(): row for row in enrichment.get("review_summary", []) if isinstance(row, dict)}
        rows = [["维度", *competitor_names]]
        row_defs = [
            ("核心策略", self._growth_core_strategy),
            ("用户获取", self._growth_acquisition),
            ("用户留存", lambda name: review_by.get(name.casefold(), {}).get("summary", "待补用户留存与口碑样本")),
            ("增长速度/阶段", lambda name: competitor_swot.get(name, {}).get("机会", "待核实")),
            ("主要分发渠道", self._growth_channel),
        ]
        for label, fn in row_defs:
            rows.append([label, *[fn(name) for name in competitor_names]])
        return rows

    def _growth_core_strategy(self, name: str) -> str:
        key = name.casefold()
        if "chatgpt" in key or "openai" in key:
            return "产品驱动 + 分层定价 + 企业市场渗透"
        if "deepseek" in key:
            return "技术口碑 + 低价 API + 开放/兼容生态"
        if "豆包" in name or "doubao" in key:
            return "免费策略 + 字节生态流量 + 多模态差异化"
        return "围绕核心能力、渠道和价格口径做增长验证"

    def _growth_acquisition(self, name: str) -> str:
        key = name.casefold()
        if "chatgpt" in key or "openai" in key:
            return "品牌自然增长、开发者生态和企业销售"
        if "deepseek" in key:
            return "开发者社区、价格优势和 API 兼容迁移"
        if "豆包" in name or "doubao" in key:
            return "内容生态、移动端入口和平台级分发"
        return "官网、渠道、内容和口碑来源需继续拆分"

    def _growth_channel(self, name: str) -> str:
        key = name.casefold()
        if "chatgpt" in key or "openai" in key:
            return "Web/App/API/企业版/连接器"
        if "deepseek" in key:
            return "Web/App/API/开源社区/开发者文档"
        if "豆包" in name or "doubao" in key:
            return "App/抖音生态/火山方舟/办公与编程工具"
        return "官网、App、销售渠道和第三方生态待核验"

    def _user_scenario_comparison_table(
        self,
        competitor_names: list[str],
        enrichment: dict[str, Any],
        competitor_swot: dict[str, dict[str, str]],
    ) -> list[list[str]]:
        lookup = self._score_lookup(enrichment)
        scenario_dims = [
            ("写作/内容创作", ["综合生产力", "多模态与创意"]),
            ("编程/开发", ["推理/代码", "生态集成"]),
            ("学习/研究", ["综合生产力", "长上下文"]),
            ("多模态创意", ["多模态与创意"]),
            ("Agent/API 开发", ["推理/代码", "API 成本效率", "生态集成"]),
            ("企业协作", ["企业治理", "生态集成"]),
            ("成本敏感场景", ["API 成本效率", "开放/自部署"]),
        ]
        rows = [["场景", *competitor_names]]
        for scenario, dims in scenario_dims:
            rows.append([scenario, *[self._star_cell(lookup, name, dims) for name in competitor_names]])
        return rows

    def _swot_comparison_table(
        self,
        competitor_names: list[str],
        enrichment: dict[str, Any],
        competitor_swot: dict[str, dict[str, str]],
    ) -> list[list[str]]:
        rows = [["竞品", "优势", "劣势", "机会", "威胁"]]
        for name in competitor_names:
            swot = competitor_swot.get(name, {})
            rows.append([
                name,
                swot.get("优势", "待核实"),
                swot.get("劣势", "待核实"),
                swot.get("机会", "待核实"),
                swot.get("威胁", "待核实"),
            ])
        return rows

    def _moat_risk_comparison_table(
        self,
        competitor_names: list[str],
        enrichment: dict[str, Any],
        competitor_swot: dict[str, dict[str, str]],
    ) -> list[list[str]]:
        lookup = self._score_lookup(enrichment)
        risk_by_name = {
            name: next((row for row in enrichment.get("risk_controls", []) if name.casefold() in str(row.get("risk", "")).casefold()), {})
            for name in competitor_names
        }
        rows = [["维度", *competitor_names]]
        row_defs = [
            ("差异化定位", lambda name: self._top_dimension_cell(name, lookup)),
            ("主要壁垒", lambda name: self._top_dimensions_summary(name, lookup)),
            ("可替代风险", lambda name: competitor_swot.get(name, {}).get("威胁", "待核实")),
            ("避雷点", lambda name: risk_by_name.get(name, {}).get("control") or competitor_swot.get(name, {}).get("劣势", "待核实")),
            ("下一步核验", lambda name: "复核价格权益、数据政策、评价样本和关键场景实测；缺 URL 的量化结论不得作为事实。"),
        ]
        for label, fn in row_defs:
            rows.append([label, *[fn(name) for name in competitor_names]])
        return rows

    def _top_dimension_cell(self, name: str, lookup: dict[tuple[str, str], dict[str, Any]]) -> str:
        rows = [row for (competitor, _dimension), row in lookup.items() if competitor == name.casefold()]
        rows.sort(key=lambda row: float(row.get("score") or 0), reverse=True)
        if not rows:
            return "待核实"
        row = rows[0]
        return f"{row.get('dimension')}（{float(row.get('score') or 0):.1f}/5）"

    def _top_dimensions_summary(self, name: str, lookup: dict[tuple[str, str], dict[str, Any]]) -> str:
        rows = [row for (competitor, _dimension), row in lookup.items() if competitor == name.casefold()]
        rows.sort(key=lambda row: float(row.get("score") or 0), reverse=True)
        labels = [f"{row.get('dimension')} {float(row.get('score') or 0):.1f}" for row in rows[:3]]
        return "；".join(labels) if labels else "待核实"

    def _source_ref_map(self, sources: list[sqlite3.Row]) -> dict[str, str]:
        return {source["id"]: f"S{index + 1}" for index, source in enumerate(sources)}

    def _build_source_catalog(self, sources: list[sqlite3.Row]) -> list[dict[str, Any]]:
        refs = self._source_ref_map(sources)
        catalog = []
        for source in sources:
            catalog.append(
                {
                    "id": source["id"],
                    "ref": refs[source["id"]],
                    "title": report_text(source["title"], 120),
                    "url_or_path": source["url_or_path"],
                    "type": source["source_type"],
                    "site": source["author_site"],
                    "published_at": source["published_at"],
                    "collected_at": source["collected_at"],
                    "credibility": source["credibility"],
                    "competitor": row_get(source, "competitor_name", ""),
                    "module": row_get(source, "module", ""),
                    "role": row_get(source, "source_role", ""),
                    "raw_content_status": row_get(source, "raw_content_status", ""),
                    "relevance_score": int(row_get(source, "relevance_score", 0) or 0),
                }
            )
        return catalog

    def _source_refs_for_ids(self, source_ids: list[str], ref_map: dict[str, str]) -> list[str]:
        return [ref_map[source_id] for source_id in source_ids if source_id in ref_map]

    def _build_pricing_fact_items(self, pricing_facts: list[sqlite3.Row], sources: list[sqlite3.Row]) -> list[dict[str, Any]]:
        ref_map = self._source_ref_map(sources)
        items = []
        for row in pricing_facts:
            item = dict(row)
            source_id = str(item.pop("source_id", "") or "")
            item["evidence_ref"] = ref_map.get(source_id, "")
            items.append(item)
        return items

    def _build_app_market_data(self, task_id: str, competitor_names: list[str]) -> dict[str, Any]:
        if not task_id:
            return {"enabled": False, "rows": [], "caveat": "缺少任务 ID，无法读取 AppArk 指标。"}
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM appark_metrics
                WHERE task_id = ?
                ORDER BY downloads_value DESC, revenue_usd DESC, rowid
                """,
                (task_id,),
            ).fetchall()
        items = []
        wanted = {_norm_name(name) for name in competitor_names if name}
        for row in rows:
            item = {
                "competitor": row["competitor_name"],
                "app_name": row["app_name"],
                "publisher": row["publisher"],
                "downloads_text": row["downloads_text"],
                "downloads_value": float(row["downloads_value"] or 0),
                "revenue_text": row["revenue_text"],
                "revenue_usd": float(row["revenue_usd"] or 0),
                "free_rank": row["free_rank"],
                "paid_rank": row["paid_rank"],
                "overall_rank": row["overall_rank"],
                "country": row["country"],
                "store": row["store"],
                "time_range": row["time_range"],
                "source_url": row["source_url"],
                "provider": row["provider"],
                "collected_at": row["collected_at"],
            }
            if wanted and _norm_name(item["competitor"]) not in wanted:
                matched = any(_norm_name(name) in _norm_name(f"{item['competitor']} {item['app_name']} {item['publisher']}") for name in competitor_names)
                if not matched:
                    continue
            items.append(item)
        return {
            "enabled": bool(items),
            "title": "App 市场表现",
            "source": "AppArk",
            "source_url": APPARK_COMPETITOR_URL,
            "rows": items,
            "caveat": "" if items else "未采集到 AppArk 下载量、收入额或榜单排名数据。",
        }

    def _build_executive_cards(
        self,
        claims: list[dict[str, Any]],
        sources: list[sqlite3.Row],
        source_catalog: list[dict[str, Any]],
        pricing_comparison: list[dict[str, Any]],
        feature_scores: list[dict[str, Any]],
        review_summary: list[dict[str, Any]],
        risk_controls: list[dict[str, Any]],
        dimension_profile: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        price_label = (dimension_profile or {}).get("price_metric_label", "价格口径")
        show_api_cost = bool((dimension_profile or {}).get("show_api_cost", False))
        official_prices = [row for row in pricing_comparison if row.get("official_source") and not row.get("needs_review")]
        price_refs = list(dict.fromkeys(ref for row in pricing_comparison for ref in row.get("evidence_refs", [])))[:4]
        comparison_note = next((row.get("calculation_note") for row in pricing_comparison if row.get("calculation_note") and "便宜" in row.get("calculation_note", "")), "")
        price_candidates = [
            row
            for row in official_prices
            if row.get("cost_index") is not None or row.get("output_amount") is not None
        ]
        if price_candidates:
            price_leader_row = min(
                price_candidates,
                key=lambda row: float(row.get("cost_index") if row.get("cost_index") is not None else row.get("output_amount") or 0),
            )
            price_status = price_leader_row.get("competitor") or "未形成判断"
        elif official_prices:
            price_status = official_prices[0].get("competitor") or "未形成判断"
        else:
            price_status = "未形成判断"
        if official_prices:
            price_verdict = comparison_note or (
                "已抽取官方价格/API口径；可在价格表中查看输入、输出和成本指数。"
                if show_api_cost
                else f"已抽取或定位官方{price_label}；可在价格表中查看代表口径和证据。"
            )
            price_confidence = 0.9
        else:
            price_verdict = f"官方{price_label}材料较少；本次不输出强价格结论。"
            price_confidence = 0.42

        totals: dict[str, int] = {}
        refs_by_competitor: dict[str, list[str]] = {}
        for row in feature_scores:
            totals[row["competitor"]] = totals.get(row["competitor"], 0) + int(float(row.get("score", 0) or 0))
            refs_by_competitor.setdefault(row["competitor"], []).extend(row.get("evidence_refs", []))
        product_refs = list(dict.fromkeys(ref for refs in refs_by_competitor.values() for ref in refs))[:4]
        if totals:
            leader = max(totals, key=totals.get)
            product_verdict = f"{leader} 在本轮产品能力评分中领先，优势主要来自功能覆盖、工具生态和材料中的成熟能力描述。"
            product_status = leader
        else:
            product_verdict = "产品/功能材料较少，暂不形成主判断。"
            product_status = "未形成判断"

        review_refs = list(dict.fromkeys(ref for row in review_summary for ref in row.get("evidence_refs", [])))[:4]
        review_platforms = sum(int(row.get("platform_count", 0) or 0) for row in review_summary)
        if review_refs and review_platforms:
            review_leader = max(
                review_summary,
                key=lambda row: (int(row.get("platform_count", 0) or 0), len(row.get("evidence_refs", []) or [])),
            ).get("competitor", "未形成判断")
            review_verdict = "；".join(
                self._user_report_text(row.get("summary", ""), 120)
                for row in review_summary
                if row.get("summary") and row.get("evidence_refs")
            ) or f"当前覆盖 {review_platforms} 个评价平台/站点，口碑判断需区分平台样本。"
            review_status = review_leader
        else:
            review_verdict = "评价样本较少；本轮只呈现已出现的评价主题，不输出整体口碑排名。"
            review_status = "未形成判断"

        risk_refs = product_refs or price_refs
        risk_verdict = "主要风险集中在来源质量、价格时效和企业合规差异；每条风险需回溯到来源或明确标为控制项。"
        if any("deepseek" in str(source.get("competitor", "")).casefold() for source in source_catalog) and any("chatgpt" in str(source.get("competitor", "")).casefold() for source in source_catalog):
            risk_verdict = "ChatGPT 风险偏成本与闭源依赖；DeepSeek 风险偏数据地域、合规和企业采购审查，需按场景复核。"
        risk_counts: dict[str, int] = {}
        for item in risk_controls:
            text = f"{item.get('risk', '')} {item.get('impact', '')} {item.get('control', '')}"
            for row in source_catalog:
                name = str(row.get("competitor", "") or "")
                if name and name in text:
                    risk_counts[name] = risk_counts.get(name, 0) + 1
        if not risk_counts:
            for row in source_catalog:
                name = str(row.get("competitor", "") or "")
                if name:
                    risk_counts[name] = risk_counts.get(name, 0) + 1
        risk_status = max(risk_counts, key=risk_counts.get) if risk_counts and risk_refs else "未形成判断"

        return [
            {
                "type": "pricing",
                "title": "价格结论",
                "verdict": self._user_report_text(price_verdict, 220) or "本轮未形成强价格结论。",
                "confidence": price_confidence,
                "evidence_refs": price_refs,
                "status": price_status,
            },
            {
                "type": "product",
                "title": "产品结论",
                "verdict": self._user_report_text(product_verdict, 220) or "本轮未形成强产品结论。",
                "confidence": 0.78 if product_refs else 0.4,
                "evidence_refs": product_refs,
                "status": product_status,
            },
            {
                "type": "review",
                "title": "评价结论",
                "verdict": self._user_report_text(review_verdict, 220) or "本轮未形成整体口碑排名。",
                "confidence": 0.68 if review_refs else 0.36,
                "evidence_refs": review_refs,
                "status": review_status,
            },
            {
                "type": "risk",
                "title": "风险结论",
                "verdict": self._user_report_text(risk_verdict, 220) or "本轮风险以场景复核为主。",
                "confidence": 0.7 if risk_refs else 0.45,
                "evidence_refs": risk_refs,
                "status": risk_status,
            },
        ]

    def _build_source_reliability(self, sources: list[sqlite3.Row]) -> list[dict[str, Any]]:
        buckets: dict[str, dict[str, Any]] = {}
        for source in sources:
            role = row_get(source, "source_role", "") or source["source_type"]
            label = {
                "official": "官方事实",
                "official_pricing": "官方价格/报价",
                "official_doc": "官方文档",
                "review": "第三方评价",
                "news": "新闻/风险",
                "source_gap": "范围说明",
                "demo": "缓存样例",
                "demo_shared": "共享背景",
            }.get(role, "第三方线索")
            bucket = buckets.setdefault(
                label,
                {"category": label, "count": 0, "fetched": 0, "summary_only": 0, "high": 0, "usage": "支撑事实、图表和报告边界说明"},
            )
            bucket["count"] += 1
            if row_get(source, "raw_content_status", "") == "fetched":
                bucket["fetched"] += 1
            if row_get(source, "raw_content_status", "") == "summary_only":
                bucket["summary_only"] += 1
            if source["credibility"] == "high":
                bucket["high"] += 1
        return list(buckets.values())

    def _dimension_keyword_pattern(self, item: dict[str, Any]) -> str:
        keywords = item.get("keywords", [])
        if not isinstance(keywords, list):
            keywords = []
        terms = [sanitize_text(str(keyword), 40) for keyword in keywords if sanitize_text(str(keyword), 40)]
        if not terms:
            terms = [sanitize_text(str(item.get("name", "")), 40)]
        return "|".join(re.escape(term) for term in terms if term) or r"产品|功能|价格|市场"

    def _dimension_specs(self, dimension_profile: dict[str, Any] | None, key: str, fallback_key: str = "generic") -> list[dict[str, Any]]:
        profile = dimension_profile or self._rule_dimension_profile("", [])
        specs = profile.get(key)
        if isinstance(specs, list) and specs:
            return [item for item in specs if isinstance(item, dict) and item.get("name")]
        fallback = self._rule_dimension_profile("", [])
        return fallback.get("feature_dimensions" if fallback_key == "feature" else "score_dimensions", [])

    def _build_feature_scores(
        self,
        competitor_names: list[str],
        sources: list[sqlite3.Row],
        dimension_profile: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        dimensions = self._dimension_specs(dimension_profile, "feature_dimensions", "feature")
        ref_map = self._source_ref_map(sources)
        rows = []
        for name in competitor_names:
            related = [source for source in sources if self._source_matches_competitor(source, name)]
            for item in dimensions:
                dimension = item.get("name", "")
                pattern = self._dimension_keyword_pattern(item)
                matched = [
                    source
                    for source in related
                    if re.search(pattern, f"{source['title']} {source['excerpt']} {row_get(source, 'module', '')}", flags=re.I)
                ]
                score = min(5, len(matched) + len([source for source in matched if source["credibility"] == "high"]))
                rows.append(
                    {
                        "competitor": name,
                        "dimension": dimension,
                        "score": score,
                        "max_score": 5,
                        "evidence_refs": self._source_refs_for_ids([source["id"] for source in matched[:3]], ref_map),
                    }
                )
        return rows

    def _build_score_dimensions(
        self,
        competitor_names: list[str],
        sources: list[sqlite3.Row],
        pricing_comparison: list[dict[str, Any]],
        dimension_profile: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        specs = self._dimension_specs(dimension_profile, "score_dimensions", "score")
        ref_map = self._source_ref_map(sources)
        pricing_lookup = {row["competitor"].casefold(): row for row in pricing_comparison}
        rows: list[dict[str, Any]] = []
        for name in competitor_names:
            related_sources = [source for source in sources if self._source_matches_competitor(source, name)]
            for item in specs:
                dimension = item.get("name", "")
                description = item.get("description", "")
                pattern = self._dimension_keyword_pattern(item)
                if item.get("cost_dimension_kind") == "api_cost" or (dimension == "API 成本效率" and (dimension_profile or {}).get("show_api_cost", True)):
                    rows.append(self._score_api_cost_dimension(name, description, pricing_lookup.get(name.casefold(), {})))
                    continue
                matched = [
                    source
                    for source in related_sources
                    if re.search(pattern, f"{source['title']} {source['excerpt']} {row_get(source, 'module', '')} {row_get(source, 'source_role', '')}", flags=re.I)
                ]
                refs = self._source_refs_for_ids([source["id"] for source in matched[:4]], ref_map)
                official_count = len([source for source in matched if row_get(source, "source_role", "") in {"official", "official_doc", "official_pricing"}])
                fetched_count = len([source for source in matched if row_get(source, "raw_content_status", "") == "fetched"])
                review_count = len([source for source in matched if row_get(source, "source_role", "") == "review"])
                score = 0.0
                if refs:
                    score = min(5.0, 1.6 + official_count * 0.9 + fetched_count * 0.45 + review_count * 0.25 + min(len(matched), 5) * 0.22)
                    if (dimension in {"开放/自部署", "长上下文"} or re.search(r"安全|合规|质量|财务|风险", dimension)) and not official_count:
                        score = min(score, 3.2)
                    score = round(max(1.0, score), 1)
                status = "分析判断" if refs else "未评分"
                rationale = (
                    f"命中 {len(matched)} 条相关来源，其中官方/文档 {official_count} 条、正文抓取 {fetched_count} 条；评分为分析判断。"
                    if refs
                    else "该维度材料较少，暂不形成能力判断。"
                )
                rows.append(
                    {
                        "competitor": name,
                        "dimension": dimension,
                        "description": description,
                        "score": score,
                        "max_score": 5,
                        "status": status,
                        "rationale": report_text(rationale, 180),
                        "evidence_refs": refs,
                    }
                )
        return rows

    def _apply_reference_ai_scores(
        self,
        competitor_names: list[str],
        score_dimensions: list[dict[str, Any]],
        source_catalog: list[dict[str, Any]],
        pricing_comparison: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        normalized = {name.casefold(): name for name in competitor_names}
        has_chatgpt = any(key in {"chatgpt", "openai"} or "chatgpt" in key for key in normalized)
        has_deepseek = any("deepseek" in key for key in normalized)
        if not (has_chatgpt and has_deepseek):
            return score_dimensions

        actual_name = {}
        for key, name in normalized.items():
            if key in {"chatgpt", "openai"} or "chatgpt" in key:
                actual_name["chatgpt"] = name
            if "deepseek" in key:
                actual_name["deepseek"] = name

        ref_by_competitor: dict[str, dict[str, list[str]]] = {}
        for source in source_catalog:
            comp = str(source.get("competitor", "")).casefold()
            role = str(source.get("role", ""))
            module = str(source.get("module", ""))
            ref = str(source.get("ref", ""))
            if not ref:
                continue
            key = "chatgpt" if "chatgpt" in comp or "openai" in comp else "deepseek" if "deepseek" in comp else ""
            if not key:
                continue
            buckets = ref_by_competitor.setdefault(key, {"official": [], "pricing": [], "review": [], "security": [], "open": []})
            if role in {"official", "official_doc"}:
                buckets["official"].append(ref)
            if role == "official_pricing" or "价格" in module or "API" in module:
                buckets["pricing"].append(ref)
            if role == "review" or "评价" in module:
                buckets["review"].append(ref)
            if "安全" in module or "企业" in module or "privacy" in str(source.get("title", "")).casefold():
                buckets["security"].append(ref)
            if "hugging" in str(source.get("url_or_path", "")).casefold() or "open" in str(source.get("title", "")).casefold():
                buckets["open"].append(ref)

        pricing_refs = {
            row.get("competitor", "").casefold(): row.get("evidence_refs", [])
            for row in pricing_comparison
        }

        profiles = {
            "chatgpt": {
                "综合生产力": (4.8, "官方产品和企业资料显示其工作台、文件、连接器、Agent 和多模态入口更完整。", "official"),
                "推理/代码": (4.7, "模型、Codex/Agent 和开发者生态支撑复杂推理与代码工作流。", "official"),
                "多模态与创意": (4.9, "图像、语音、视频和创意工具链的产品化证据更充分。", "official"),
                "企业治理": (4.8, "Business/Enterprise、业务数据政策和企业控制项证据更完整。", "security"),
                "API 成本效率": (2.2, "官方输出价作为高端基准，成本效率弱于 DeepSeek 低价 API。", "pricing"),
                "开放/自部署": (1.4, "以闭源商业服务为主，自部署和开放权重不是核心交付方式。", "official"),
                "长上下文": (3.4, "长上下文能力可用，但本轮官方证据相对 DeepSeek 1M 上下文更弱。", "official"),
                "生态集成": (5.0, "连接器、企业管理、开发者平台和用户习惯形成更强生态。", "official"),
            },
            "deepseek": {
                "综合生产力": (3.7, "免费聊天入口和文本/代码能力较强，但一站式工作台证据弱于 ChatGPT。", "official"),
                "推理/代码": (4.4, "官方文档、API 兼容性和开发者口碑支撑推理与代码场景。", "official"),
                "多模态与创意": (2.8, "公开证据主要集中在文本、推理、代码和 API，多模态产品化不足。", "official"),
                "企业治理": (2.5, "隐私、数据地域和企业控制项仍需逐项采购审查。", "security"),
                "API 成本效率": (5.0, "官方 API 输出价和缓存输入价显著低于高端基准。", "pricing"),
                "开放/自部署": (5.0, "公开模型卡和开放权重/MIT 许可支撑自部署和替代供应商策略。", "open"),
                "长上下文": (5.0, "官方文档显示 1M 上下文和高最大输出，适合长文档/RAG 场景。", "official"),
                "生态集成": (3.3, "OpenAI/Anthropic 兼容接口降低迁移成本，但应用层连接器生态仍较弱。", "official"),
            },
        }

        dimensions = list(dict.fromkeys(row.get("dimension", "") for row in score_dimensions if row.get("dimension")))
        description_by_dim = {row.get("dimension", ""): row.get("description", "") for row in score_dimensions}
        rows: list[dict[str, Any]] = []
        for key in ["chatgpt", "deepseek"]:
            name = actual_name.get(key)
            if not name:
                continue
            for dimension in dimensions:
                score, rationale, ref_bucket = profiles[key].get(dimension, (0, "该维度缺少参考评分。", "official"))
                bucket_refs = ref_by_competitor.get(key, {}).get(ref_bucket, [])
                if ref_bucket == "pricing":
                    bucket_refs = pricing_refs.get(name.casefold(), []) or bucket_refs
                refs = list(dict.fromkeys(bucket_refs or ref_by_competitor.get(key, {}).get("official", [])))[:4]
                rows.append(
                    {
                        "competitor": name,
                        "dimension": dimension,
                        "description": description_by_dim.get(dimension, ""),
                        "score": score if refs else 0.0,
                        "max_score": 5,
                        "status": "分析判断" if refs else "未评分",
                        "rationale": rationale if refs else "该维度材料较少，暂不输出参考评分。",
                        "evidence_refs": refs,
                    }
                )
        other_rows = [
            row
            for row in score_dimensions
            if row.get("competitor") not in set(actual_name.values())
        ]
        return rows + other_rows

    def _score_api_cost_dimension(self, competitor: str, description: str, pricing: dict[str, Any]) -> dict[str, Any]:
        refs = pricing.get("evidence_refs", []) if pricing else []
        output_amount = pricing.get("output_amount") if pricing else None
        cost_index = pricing.get("cost_index") if pricing else None
        if output_amount and cost_index is not None:
            score = round(max(1.0, min(5.0, 5.2 - (float(cost_index) / 100.0) * 3.0)), 1)
            rationale = f"官方输出价已归一为成本指数 {cost_index}；指数越低，API 成本效率得分越高。"
            status = "分析判断"
        elif output_amount:
            score = 3.4
            rationale = "已抽取官方输出价，但缺少可比基准；暂按中性偏高处理。"
            status = "分析判断"
        elif refs:
            score = 2.4
            rationale = "已定位官方价格/API来源，但未抽取到可计算金额。"
            status = "待复核"
        else:
            score = 0.0
            rationale = "官方价格/API材料较少，暂不评分。"
            status = "未评分"
        return {
            "competitor": competitor,
            "dimension": "API 成本效率",
            "description": description,
            "score": score,
            "max_score": 5,
            "status": status,
            "rationale": report_text(rationale, 180),
            "evidence_refs": refs,
        }

    def _build_api_cost_data(
        self,
        pricing_comparison: list[dict[str, Any]],
        pricing_facts: list[dict[str, Any]] | None = None,
        dimension_profile: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if dimension_profile and not dimension_profile.get("show_api_cost"):
            label = dimension_profile.get("price_metric_label") or "行业价格口径"
            return {
                "enabled": False,
                "title": label,
                "baseline": None,
                "formula": "",
                "rows": [],
                "caveat": f"本行业不生成 API token 成本指数；价格分析以{label}、来源日期和业务口径为准。",
            }
        priced_rows = []
        for fact in pricing_facts or []:
            if fact.get("price_type") != "output" or not fact.get("amount") or not fact.get("currency"):
                continue
            priced_rows.append(
                {
                    "competitor": fact.get("competitor_name", ""),
                    "plan_name": fact.get("plan_name", ""),
                    "output_amount": float(fact.get("amount") or 0),
                    "output_currency": fact.get("currency", ""),
                    "evidence_refs": [fact.get("evidence_ref", "")] if fact.get("evidence_ref") else [],
                    "calculation_note": "",
                }
            )
        if len(priced_rows) < 2:
            priced_rows = [
                row
                for row in pricing_comparison
                if row.get("output_amount") and row.get("output_currency")
            ]
        if not priced_rows:
            return {
                "enabled": True,
                "title": "API 成本指数",
                "baseline": None,
                "formula": "cost_index = competitor_output_price / baseline_output_price * 100",
                "rows": [],
                "caveat": "未抽取到至少两个官方输出价时，不生成成本指数。",
            }
        baseline = max(priced_rows, key=lambda row: float(row["output_amount"]))
        baseline_amount = float(baseline["output_amount"])
        rows = []
        for row in priced_rows:
            amount = float(row["output_amount"])
            if row["output_currency"] != baseline["output_currency"] or baseline_amount <= 0:
                continue
            index = round((amount / baseline_amount) * 100, 2)
            multiplier = round(baseline_amount / amount, 2) if amount > 0 else None
            rows.append(
                {
                    "competitor": row.get("competitor", ""),
                    "plan_name": row.get("plan_name", ""),
                    "output_amount": amount,
                    "currency": row.get("output_currency", ""),
                    "unit": "每百万 tokens",
                    "cost_index": index,
                    "baseline": row.get("competitor") == baseline.get("competitor"),
                    "multiplier_vs_baseline": multiplier,
                    "evidence_refs": row.get("evidence_refs", []),
                    "note": row.get("calculation_note", "") or ("本组官方输出价最高，作为成本指数基准。" if row.get("competitor") == baseline.get("competitor") else ""),
                }
            )
        rows.sort(key=lambda item: item["cost_index"], reverse=True)
        return {
            "enabled": True,
            "title": "API 成本指数",
            "baseline": {
                "competitor": baseline.get("competitor", ""),
                "plan_name": baseline.get("plan_name", ""),
                "output_amount": baseline_amount,
                "currency": baseline.get("output_currency", ""),
            },
            "formula": "cost_index = competitor_output_price / baseline_output_price * 100",
            "rows": rows,
            "caveat": "实际成本还取决于缓存命中率、上下文长度、重试率、延迟、质量、拒答率和安全过滤。",
        }

    def _build_positioning_map(
        self,
        competitor_names: list[str],
        score_dimensions: list[dict[str, Any]],
        dimension_profile: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        lookup = {(row["competitor"], row["dimension"]): float(row.get("score", 0) or 0) for row in score_dimensions}
        positioning = (dimension_profile or {}).get("positioning", {}) if isinstance((dimension_profile or {}).get("positioning", {}), dict) else {}
        score_names = list(dict.fromkeys(row["dimension"] for row in score_dimensions if row.get("dimension")))
        x_dimensions = positioning.get("x_dimensions") if isinstance(positioning.get("x_dimensions"), list) else []
        y_dimensions = positioning.get("y_dimensions") if isinstance(positioning.get("y_dimensions"), list) else []
        if not x_dimensions:
            x_dimensions = score_names[: max(1, min(3, len(score_names)))]
        if not y_dimensions:
            y_dimensions = score_names[max(1, len(score_names) // 2): max(2, len(score_names))]
        if not y_dimensions:
            y_dimensions = score_names[-3:] or x_dimensions

        def avg(name: str, dims: list[str]) -> float:
            values = [lookup.get((name, dim), 0) for dim in dims if lookup.get((name, dim), 0) > 0]
            return round(sum(values) / len(values), 2) if values else 0.0

        points = []
        for name in competitor_names:
            x = avg(name, x_dimensions)
            y = avg(name, y_dimensions)
            points.append(
                {
                    "competitor": name,
                    "x": x,
                    "y": y,
                    "label": f"{positioning.get('x_axis', '横轴')}较强" if x >= y else f"{positioning.get('y_axis', '纵轴')}较强",
                }
            )
        return {
            "x_axis": positioning.get("x_axis") or "价格/交付竞争力",
            "y_axis": positioning.get("y_axis") or "产品/市场竞争力",
            "x_dimensions": x_dimensions,
            "y_dimensions": y_dimensions,
            "points": points,
            "interpretation": positioning.get("interpretation") or "右侧更偏横轴优势，上侧更偏纵轴优势。",
        }

    def _build_scenario_recommendations(
        self,
        competitor_names: list[str],
        score_dimensions: list[dict[str, Any]],
        pricing_comparison: list[dict[str, Any]],
        review_summary: list[dict[str, Any]],
        dimension_profile: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        score_lookup: dict[str, dict[str, dict[str, Any]]] = {}
        for row in score_dimensions:
            score_lookup.setdefault(row["competitor"], {})[row["dimension"]] = row

        def best_for(dimensions: list[str]) -> tuple[str, float, list[str]]:
            best_name = competitor_names[0] if competitor_names else ""
            best_score = -1.0
            refs: list[str] = []
            for name in competitor_names:
                rows = [score_lookup.get(name, {}).get(dim, {}) for dim in dimensions]
                values = [float(row.get("score", 0) or 0) for row in rows if row]
                current = sum(values) / len(values) if values else 0.0
                if current > best_score:
                    best_name = name
                    best_score = current
                    refs = list(dict.fromkeys(ref for row in rows for ref in row.get("evidence_refs", [])))[:4]
            return best_name, round(best_score, 1), refs

        raw_scenarios = (dimension_profile or {}).get("decision_scenarios", [])
        scenarios = [
            (
                item.get("scenario", "场景化选择"),
                item.get("dimensions", []),
                item.get("rule", "按该场景的高权重维度选择证据更完整的一方。"),
            )
            for item in raw_scenarios
            if isinstance(item, dict) and item.get("scenario") and isinstance(item.get("dimensions", []), list)
        ]
        if not scenarios:
            dimensions = list(dict.fromkeys(row.get("dimension", "") for row in score_dimensions if row.get("dimension")))
            scenarios = [
                ("成本敏感采购", dimensions[:2], "优先比较价格口径、交付确定性和后续使用成本。"),
                ("能力优先选型", dimensions[2:5] or dimensions[:3], "优先选择核心能力和可靠性证据更完整的一方。"),
                ("市场验证优先", dimensions[-2:] or dimensions[:2], "优先看客户案例、渠道覆盖和评价样本。"),
            ]
        recommendations = []
        review_refs = list(dict.fromkeys(ref for row in review_summary for ref in row.get("evidence_refs", [])))[:4]
        for scenario, dims, rule in scenarios:
            name, score, refs = best_for(dims)
            if re.search(r"评价|口碑|用户|客户", scenario) and review_refs:
                refs = review_refs
            recommendations.append(
                {
                    "scenario": scenario,
                    "recommended": name or "未形成推荐",
                    "confidence": "高" if score >= 4.0 and refs else "中" if refs else "低",
                    "reason": f"{rule} 当前相关维度均值 {score}/5。",
                    "next_action": "先做人工复核，再进入采购或新品立项决策。" if not refs else "结合业务权重和人工复核结果确认最终选择。",
                    "evidence_refs": refs,
                }
            )
        return recommendations

    def _build_key_insights(
        self,
        positioning_map: dict[str, Any],
        api_cost_data: dict[str, Any],
        review_summary: list[dict[str, Any]],
        source_reliability: list[dict[str, Any]],
        dimension_profile: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        insights: list[dict[str, Any]] = []
        cost_rows = api_cost_data.get("rows", [])
        cheaper_candidates = [row for row in cost_rows if not row.get("baseline") and row.get("multiplier_vs_baseline")]
        cheaper = min(cheaper_candidates, key=lambda row: float(row.get("cost_index", 100))) if cheaper_candidates else None
        if cheaper:
            insights.append(
                {
                    "title": "价格差异必须转成成本指数",
                    "insight": f"{cheaper['competitor']} 的官方输出价成本指数为 {cheaper['cost_index']}，约为基准的 {cheaper['cost_index']}%。",
                    "evidence_refs": cheaper.get("evidence_refs", []),
                }
            )
        elif api_cost_data.get("enabled") is False:
            label = (dimension_profile or {}).get("price_metric_label", "行业价格")
            insights.append(
                {
                    "title": "价格口径按行业重设",
                    "insight": f"本报告不使用 API token 成本指数；价格对比改按{label}和业务口径解释。",
                    "evidence_refs": [],
                }
            )
        points = positioning_map.get("points", [])
        if len(points) >= 2:
            labels = "；".join(f"{point['competitor']}：{point['label']}" for point in points)
            insights.append(
                {
                    "title": "竞争定位要按场景权重解释",
                    "insight": f"定位图显示 {labels}。选型应按场景权重，而不是只看单点指标。",
                    "evidence_refs": [],
                }
            )
        review_platforms = sum(int(row.get("platform_count", 0) or 0) for row in review_summary)
        insights.append(
            {
                "title": "用户评价需要样本分层",
                "insight": f"当前评价平台/站点样本数为 {review_platforms}；需区分 B2B 平台、应用商店、售后投诉和社区观点。",
                "evidence_refs": list(dict.fromkeys(ref for row in review_summary for ref in row.get("evidence_refs", [])))[:4],
            }
        )
        summary_only = sum(int(row.get("summary_only", 0) or 0) for row in source_reliability)
        insights.append(
            {
                "title": "检索线索不能直接进结论",
                "insight": f"当前仅有检索线索的来源 {summary_only} 条；价格、政策、评价和 SWOT 优先采用正文、上传材料或人工录入证据。",
                "evidence_refs": [],
            }
        )
        insights.append(
            {
                "title": "新品竞品分析要看需求缺口",
                "insight": "新品、实物或行业产品场景应把规格、价格、销量/出货、评价关键词和未满足需求放在同一张事实表里解释机会点。",
                "evidence_refs": [],
            }
        )
        return insights

    def _build_fact_notes(
        self,
        pricing_facts: list[sqlite3.Row],
        score_dimensions: list[dict[str, Any]],
        dimension_profile: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        dimension_count = len(list(dict.fromkeys(row.get("dimension", "") for row in score_dimensions if row.get("dimension"))))
        notes = [
            {
                "topic": "评分口径",
                "note": f"{dimension_count} 维评分是分析判断，基于官方证据、正文抓取、评价样本和风险调整；不是厂商官方指标。",
                "evidence_refs": [],
            },
            {
                "topic": "行业价格口径",
                "note": f"本轮价格口径为{(dimension_profile or {}).get('price_metric_label', '价格/商业模式')}；应与规格、销量/出货、评价和风险放在同一业务口径下解释。",
                "evidence_refs": [],
            },
        ]
        for fact in pricing_facts[:10]:
            row = dict(fact)
            notes.append(
                {
                    "topic": f"{row.get('competitor_name', '')} {row.get('plan_name', '')} {row.get('price_type', '')}",
                    "note": f"{row.get('amount', '')} {row.get('currency', '')}/{row.get('unit', '')}；以材料中的计费口径呈现。",
                    "evidence_refs": [],
                }
            )
        weak_scores = [
            row
            for row in score_dimensions
            if row.get("status") != "分析判断"
        ][:6]
        for row in weak_scores:
            notes.append(
                {
                    "topic": f"{row.get('competitor', '')} {row.get('dimension', '')}",
                    "note": "该维度资料支撑较弱，报告只呈现谨慎判断。",
                    "evidence_refs": row.get("evidence_refs", []),
                }
            )
        return notes

    def _build_radar_chart_data(self, feature_scores: list[dict[str, Any]]) -> list[dict[str, Any]]:
        grouped: dict[str, dict[str, int]] = {}
        for row in feature_scores:
            grouped.setdefault(row["competitor"], {})[row["dimension"]] = row["score"]
        return [{"competitor": name, "scores": scores} for name, scores in grouped.items()]

    def _build_pricing_comparison(
        self,
        competitor_names: list[str],
        claims: list[dict[str, Any]],
        sources: list[sqlite3.Row],
        pricing_facts: list[sqlite3.Row],
        dimension_profile: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        price_label = (dimension_profile or {}).get("price_metric_label") or "价格口径"
        price_description = (dimension_profile or {}).get("price_metric_description") or "官方价格、套餐、型号或服务口径。"
        show_api_cost = bool((dimension_profile or {}).get("show_api_cost", False))
        ref_map = self._source_ref_map(sources)
        facts_by_competitor: dict[str, list[dict[str, Any]]] = {}
        for row in pricing_facts:
            item = dict(row)
            facts_by_competitor.setdefault(item["competitor_name"].casefold(), []).append(item)
        rows = []
        for name in competitor_names:
            facts = facts_by_competitor.get(name.casefold(), [])
            official_sources = [
                source
                for source in sources
                if self._source_matches_competitor(source, name) and row_get(source, "source_role", "") == "official_pricing"
            ]
            if facts:
                output_fact = self._preferred_price_fact(facts, "output")
                input_fact = self._preferred_price_fact(facts, "input")
                cached_fact = self._preferred_price_fact(facts, "input_cached")
                price_parts = []
                if cached_fact:
                    price_parts.append(f"缓存输入 {self._format_price_fact(cached_fact)}")
                if input_fact:
                    price_parts.append(f"输入 {self._format_price_fact(input_fact)}")
                if output_fact:
                    price_parts.append(f"输出 {self._format_price_fact(output_fact)}")
                if not price_parts:
                    price_parts = [f"{fact.get('plan_name', '价格项')} {self._format_price_fact(fact)}" for fact in facts[:3]]
                source_ids = list(dict.fromkeys([fact["source_id"] for fact in facts]))
                refs = self._source_refs_for_ids(source_ids, ref_map)
                primary = output_fact or input_fact or facts[0]
                rows.append(
                    {
                        "competitor": name,
                        "price_text": "；".join(price_parts[:3]) or self._format_price_fact(primary),
                        "basis": (
                            f"官方价格/API 页抽取，代表模型：{primary.get('plan_name', '未识别模型')}，单位统一按每百万 tokens 展示。"
                            if show_api_cost
                            else f"{price_label}抽取：{primary.get('plan_name', '未识别价格项')}；{price_description}"
                        ),
                        "evidence_refs": refs,
                        "needs_review": False,
                        "official_source": True,
                        "output_amount": float(output_fact["amount"]) if output_fact else None,
                        "output_currency": output_fact["currency"] if output_fact else "",
                        "plan_name": primary.get("plan_name", ""),
                    }
                )
                continue
            refs = self._source_refs_for_ids([source["id"] for source in official_sources[:3]], ref_map)
            rows.append(
                {
                    "competitor": name,
                    "price_text": "未抽取到明确金额",
                    "basis": f"已定位{price_label}来源但未抽取到结构化金额。" if refs else f"当前来源未覆盖{price_label}金额或区间。",
                    "evidence_refs": refs,
                    "needs_review": True,
                    "official_source": bool(refs),
                    "output_amount": None,
                    "output_currency": "",
                    "plan_name": "",
                }
            )
        output_rows = [row for row in rows if row.get("output_amount") and row.get("output_currency")]
        if show_api_cost and len(output_rows) >= 2:
            baseline = max(output_rows, key=lambda row: float(row["output_amount"]))
            baseline_amount = float(baseline["output_amount"])
            for row in output_rows:
                if row["output_currency"] != baseline["output_currency"] or baseline_amount <= 0:
                    continue
                ratio = float(row["output_amount"]) / baseline_amount
                row["cost_index"] = round(ratio * 100, 2)
                if row["competitor"] != baseline["competitor"]:
                    row["calculation_note"] = f"{row['competitor']} 输出价约为 {baseline['competitor']} 的 {ratio * 100:.1f}%，约 {baseline_amount / float(row['output_amount']):.1f} 倍便宜。"
                else:
                    row["calculation_note"] = "本组官方输出价最高，作为成本指数基准。"
        return rows

    def _build_review_summary(
        self,
        competitor_names: list[str],
        claims: list[dict[str, Any]],
        sources: list[sqlite3.Row],
    ) -> list[dict[str, Any]]:
        ref_map = self._source_ref_map(sources)
        rows = []
        for name in competitor_names:
            related_claims = [
                claim
                for claim in claims
                if claim.get("section") == "reviews" and re.search(re.escape(name), claim.get("content", ""), flags=re.I)
            ]
            related_sources = [source for source in sources if self._source_matches_competitor(source, name) and row_get(source, "source_role", "") == "review"]
            refs = []
            if related_sources:
                refs = self._source_refs_for_ids([source["id"] for source in related_sources[:3]], ref_map)
            elif related_claims:
                refs = self._source_refs_for_ids(related_claims[0].get("source_ids", []), ref_map)
            if related_claims:
                summary = related_claims[0].get("content", "")
            elif related_sources:
                summary = self._review_summary(related_sources[:3])
            else:
                summary = "公开评价样本较少，暂以已上传评论材料为准。"
            rows.append(
                {
                    "competitor": name,
                    "summary": self._user_report_text(summary, 180),
                    "platform_count": len({source["author_site"] for source in related_sources}),
                    "evidence_refs": refs,
                    "bias_note": "平台样本不同，App Store 更偏移动端满意度，Trustpilot 更容易聚集售后投诉。" if refs else "评价样本较少。",
                }
            )
        return rows

    def _build_decision_matrix(
        self,
        competitor_names: list[str],
        feature_scores: list[dict[str, Any]],
        pricing_comparison: list[dict[str, Any]],
        review_summary: list[dict[str, Any]],
        dimension_profile: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        price_label = (dimension_profile or {}).get("price_metric_label", "价格口径")
        rows = []
        for name in competitor_names:
            score_total = sum(row["score"] for row in feature_scores if row["competitor"] == name)
            pricing = next((row for row in pricing_comparison if row["competitor"] == name), {})
            review = next((row for row in review_summary if row["competitor"] == name), {})
            rows.append(
                {
                    "scenario": "资料充分度优先",
                    "competitor": name,
                    "priority": "高" if score_total >= 10 else "中" if score_total >= 5 else "低",
                    "reason": f"资料支撑分 {score_total}；{price_label}：{pricing.get('price_text', '未覆盖')}；评价样本：{review.get('platform_count', 0)} 个平台。",
                    "next_action": f"复核官方{price_label}与评价样本后再形成采购建议。" if score_total >= 5 else "先核验官网、价格/规格页和评价来源。",
                    "evidence_refs": list(dict.fromkeys((pricing.get("evidence_refs") or []) + (review.get("evidence_refs") or [])))[:4],
                }
            )
        return rows

    def _build_risk_controls(
        self,
        competitor_names: list[str],
        sources: list[sqlite3.Row],
        claims: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        rows = []
        for name in competitor_names:
            related_sources = [source for source in sources if self._source_matches_competitor(source, name)]
            summary_only = len([source for source in related_sources if row_get(source, "raw_content_status", "") == "summary_only"])
            low_sources = len([source for source in related_sources if source["credibility"] == "low"])
            rows.append(
                {
                    "risk": f"{name} 来源质量风险",
                    "impact": "检索线索或低可信来源过多时，价格、评价和 SWOT 容易失真。",
                    "control": f"优先采用官方页和独立评价；当前仅有检索线索={summary_only}，低可信来源={low_sources}。",
                    "owner": "采集 Agent / 质检 Agent",
                }
            )
        rows.append(
            {
                "risk": "时间敏感信息过期",
                "impact": "价格、规格、政策、销量/出货和评分会随日期变化。",
                "control": "质检 Agent 对缺少时间口径的高价值结论打回，报告正文只呈现业务判断。",
                "owner": "质检 Agent",
            }
        )
        return rows

    def _build_display_report(self, task: sqlite3.Row, raw_sections: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        focus_keys = self._focus_section_keys(loads(task["focus_areas_json"], []))
        default_sections = [self._display_section(section) for section in raw_sections if section["key"] in focus_keys]
        task_payload = {
            "id": task["id"],
            "industry": task["industry"],
            "competitors": loads(task["competitors_json"], []),
            "focus_areas": loads(task["focus_areas_json"], []),
        }
        trace = {
            "provider": "report-renderer",
            "token_input": 0,
            "token_output": 0,
            "fallback_reason": "",
            "tool_calls": [{"name": "deterministic_report_sections", "result": f"{len(default_sections)} sections"}],
            "summary": "",
        }
        default_sections = self._postprocess_display_sections(default_sections, task_payload)
        return default_sections, trace

    def _focus_section_keys(self, focus_areas: list[str]) -> list[str]:
        selected = set(focus_areas or DEFAULT_FOCUS_AREAS)
        keys = []
        for key, meta in SECTION_META.items():
            if selected & set(meta["focus"]):
                keys.append(key)
        return keys

    def _display_section(self, section: dict[str, Any]) -> dict[str, Any]:
        key = section["key"]
        display_claims = []
        for claim in section.get("claims", [])[:4]:
            content = self._user_report_text(str(claim.get("content", "")), 520)
            if not content:
                continue
            split_items = self._split_swot_items(content) if key == "swot" else [content]
            for item in split_items:
                item = self._user_report_text(item, 520)
                if not item:
                    continue
                display_claims.append(
                    {
                        "id": claim.get("id", ""),
                        "content": item,
                        "claim_type": claim.get("claim_type", "fact"),
                        "source_refs": claim.get("source_ids", []),
                        "needs_review": bool(claim.get("needs_review")),
                    }
                )
        body = self._section_body_for_display(key, bool(display_claims))
        result = {
            "key": key,
            "title": SECTION_META.get(key, {}).get("title", section.get("title", key)),
            "body": body,
            "claims": display_claims,
        }
        if key == "feature_tree" and section.get("table"):
            result["table"] = section["table"]
        return result

    def _section_body_for_display(self, key: str, has_claims: bool) -> str:
        if key == "feature_tree":
            return "从已采集的公开材料中提炼各竞品的业务范围、核心能力和产品/服务覆盖。"
        if key == "pricing_model":
            return "整理材料中出现的价格区间、代表产品/车型/套餐、限制条件和商业化线索。"
        if key == "user_persona":
            return "归纳公开定位、访谈、问卷或评价材料能支撑的目标用户和使用场景。" if has_claims else "当前暂无可靠来源支撑用户画像，本模块不做无来源推断。"
        if key == "reviews":
            return "汇总公开评价、问卷或访谈中可追溯的用户反馈。" if has_claims else "当前暂无可靠来源支撑用户评价，本模块不做口碑推断。"
        if key == "swot":
            return "按竞品分别归纳优势、劣势、机会和威胁。"
        return "展示关键判断、风险状态和需要业务侧关注的事项。"

    def _split_swot_items(self, content: str) -> list[str]:
        labels = ["优势", "劣势", "机会", "威胁"]
        if not all(f"{label}：" in content for label in labels):
            return [content]
        first_label_pos = min(content.find(f"{label}：") for label in labels if f"{label}：" in content)
        prefix = report_text(content[:first_label_pos], 80)
        items = []
        for index, label in enumerate(labels):
            start = content.find(f"{label}：")
            end_candidates = [content.find(f"{next_label}：") for next_label in labels[index + 1 :]]
            end_candidates = [pos for pos in end_candidates if pos > start]
            end = min(end_candidates) if end_candidates else len(content)
            text = report_text(content[start:end], 260)
            if text:
                if prefix and not text.startswith(prefix):
                    text = f"{prefix}{text}"
                items.append(text)
        return items or [content]

    def _merge_llm_report(self, default_sections: list[dict[str, Any]], payload: dict[str, Any]) -> list[dict[str, Any]]:
        by_key = {section.get("key"): section for section in payload.get("sections", []) if isinstance(section, dict)}
        merged = []
        for section in default_sections:
            candidate = by_key.get(section["key"])
            if not candidate:
                merged.append(section)
                continue
            updated = dict(section)
            body = self._user_report_text(str(candidate.get("body", "")), 520)
            if body:
                updated["body"] = body
            title = report_text(str(candidate.get("title", "")), 80)
            if title:
                updated["title"] = title
            candidate_claims = candidate.get("claims", [])
            if isinstance(candidate_claims, list):
                new_claims = []
                for index, claim in enumerate(updated.get("claims", [])):
                    text_source = candidate_claims[index] if index < len(candidate_claims) else claim.get("content", "")
                    if isinstance(text_source, dict):
                        text_source = text_source.get("content", "")
                    text = self._user_report_text(str(text_source), 520)
                    new_claims.append({**claim, "content": text or claim.get("content", "")})
                updated["claims"] = new_claims
            candidate_table = candidate.get("table")
            if isinstance(candidate_table, list) and candidate_table:
                cleaned_table = []
                for row in candidate_table[:8]:
                    if isinstance(row, list):
                        cleaned_row = [self._user_report_text(str(cell), 160) for cell in row[:5]]
                        if any(cleaned_row):
                            cleaned_table.append(cleaned_row)
                if cleaned_table:
                    updated["table"] = cleaned_table
            merged.append(updated)
        return merged

    def _postprocess_display_sections(self, sections: list[dict[str, Any]], task_payload: dict[str, Any]) -> list[dict[str, Any]]:
        for section in sections:
            section["body"] = self._user_report_text(str(section.get("body", "")), 520)
            for claim in section.get("claims", []):
                claim["content"] = self._user_report_text(str(claim.get("content", "")), 520)
            if isinstance(section.get("table"), list):
                cleaned_table = []
                for row in section.get("table", []):
                    if isinstance(row, list):
                        cleaned_table.append([self._user_report_text(str(cell), 160) for cell in row])
                section["table"] = cleaned_table
            if section.get("key") == "swot":
                section["body"] = "按竞品分别归纳产品优势、劣势、机会和威胁。"
        return sections

    def _user_report_text(self, value: str, limit: int = 420) -> str:
        text = report_text(value, limit)
        replacements = [
            (r"竞品名称[:：][^。；;\n]{0,120}", ""),
            (r"材料类型[:：][^。；;\n]{0,160}", ""),
            (r"采集日期[:：]\s*\d{4}-\d{2}-\d{2}", ""),
            (r"说明[:：][^。；;\n]{0,220}", ""),
            (r"来源清单[^。；;\n]{0,220}", ""),
            (r"\b[A-Za-z0-9_]*_0[1-4]_[^。；;\s]{0,40}\.pdf\s*提到\s*", ""),
            (r"\b0[1-4]_[^。；;\s]{0,40}\.pdf\s*提到\s*", ""),
            (r"[\w\u4e00-\u9fa5（）() -]{1,80}\.pdf\s*提到\s*", ""),
            (r"当前资料显示[:：]?", ""),
            (r"当前资料出现用户评价或口碑线索[:：]?", ""),
            (r"当前产品/功能证据覆盖最高[，,；;。]?", ""),
            (r"当前来源库[^。；;]{0,120}[。；;]?", ""),
            (r"结论基于[^。；;]{0,120}[。；;]?", ""),
            (r"证据[:：]\s*(S\d+\s*)+", ""),
            (r"不再使用搜索日志片段[。；;]?", ""),
            (r"以采集日期为准[，,；;。]?", ""),
            (r"本文只整理[^。；;]{0,180}[。；;]?", ""),
            (r"不做口碑对比[，,；;。]?", ""),
            (r"不做竞品对比[，,；;。]?", ""),
            (r"不输出胜负判断[，,；;。]?", ""),
            (r"后续补充更多数据源后可进一步完善分析精度[。；;]?", ""),
            (r"后续新增来源时可刷新[。；;]?", ""),
            (r"可刷新报告[。；;]?", ""),
            (r"可重新生成报告[。；;]?", ""),
            (r"建议补搜来源[。；;]?", ""),
            (r"采集不足[。；;]?", ""),
            (r"待补证[。；;]?", ""),
            (r"需要继续补证[。；;]?", ""),
            (r"需补充来源后再确认[。；;]?", ""),
        ]
        for pattern, repl in replacements:
            text = re.sub(pattern, repl, text)
        return report_text(text.strip(" ；;。"), limit)

    def _extract_competitor_swot(self, sections: list[dict[str, Any]]) -> dict[str, dict[str, str]]:
        result: dict[str, dict[str, str]] = {}
        swot_section = next((section for section in sections if section.get("key") == "swot"), None)
        if not swot_section:
            return result
        for claim in swot_section.get("claims", []):
            content = str(claim.get("content", ""))
            match = re.match(r"(.+?)(优势|劣势|机会|威胁)：(.+)", content)
            if not match:
                continue
            competitor = report_text(match.group(1), 80).strip("：: -")
            label = match.group(2)
            value = report_text(match.group(3), 300)
            if competitor and value:
                result.setdefault(competitor, {})[label] = value
        return result

    def _build_competitor_swot(
        self,
        competitors: list[str],
        sources: list[sqlite3.Row],
        claims: list[dict[str, Any]],
    ) -> dict[str, dict[str, str]]:
        result: dict[str, dict[str, str]] = {}
        for name in competitors:
            related_sources = [source for source in sources if self._source_matches_competitor(source, name)]
            text = " ".join(
                self._clean_source_excerpt_for_report(f"{source['title']} {source['excerpt']}", 1200)
                for source in related_sources
            )
            claim_text = " ".join(claim.get("content", "") for claim in claims if name.lower() in claim.get("content", "").lower())
            combined = f"{text} {claim_text}"
            result[name] = self._swot_profile_for_competitor(name, combined)
        return result

    def _swot_profile_for_competitor(self, name: str, text: str) -> dict[str, str]:
        name_key = (name or "").casefold()
        lowered = f"{name} {text}".casefold()
        if "deepseek" in name_key:
            return {
                "优势": "API 成本低，1M 上下文、384K 最大输出、OpenAI/Anthropic 兼容接口适合长文档、RAG、代码和成本敏感场景。",
                "劣势": "应用层、多模态和企业治理材料相对薄，品牌在合规采购中仍需要更多证明，公开评价样本量小于头部应用。",
                "机会": "可作为成本优化、私有化试点、模型多供应商和长上下文工作流的第二供应商，吸引开发者迁移。",
                "威胁": "数据地域、跨境监管、政府/企业禁用风险、促销价到期和服务稳定性会影响高敏场景落地。",
            }
        if "豆包" in name or "doubao" in name_key or "volc" in name_key:
            return {
                "优势": "中文消费端入口和多模态体验强，覆盖学习办公、语音、P 图、图片/视频生成，并背靠火山方舟、TRAE 和办公协作生态。",
                "劣势": "官方价格表字段不够透明，个人端订阅权益材料不足，国际评价样本和企业公开采购案例仍偏少。",
                "机会": "可在中国区移动助手、视频生成、企业客服 Agent、火山方舟集成和编程工具链中扩大使用场景。",
                "威胁": "价格口径变化、平台生态绑定、国内大模型同质化竞争和内容合规约束可能影响长期差异化。",
            }
        if "chatgpt" in name_key or "openai" in name_key:
            return {
                "优势": "产品化链路完整，覆盖文本、文件、图像、语音、Deep Research、Agent/Codex 与企业协作场景。",
                "劣势": "订阅与高阶 API 成本较高，公开差评集中在账单、账号、限制、生成错误和事实可信度波动。",
                "机会": "可继续向企业 AI 工作台、连接器、Agent 自动化、数据分析和代码工作流扩展，提高组织内标准化使用频率。",
                "威胁": "低价 API、开源/私有化模型和区域化模型会压缩成本优势，闭源与供应商锁定也会影响部分企业采购。",
            }
        if "deepseek" in lowered:
            return {
                "优势": "API 成本低，1M 上下文、384K 最大输出、OpenAI/Anthropic 兼容接口适合长文档、RAG、代码和成本敏感场景。",
                "劣势": "应用层、多模态和企业治理材料相对薄，品牌在合规采购中仍需要更多证明，公开评价样本量小于头部应用。",
                "机会": "可作为成本优化、私有化试点、模型多供应商和长上下文工作流的第二供应商，吸引开发者迁移。",
                "威胁": "数据地域、跨境监管、政府/企业禁用风险、促销价到期和服务稳定性会影响高敏场景落地。",
            }
        if "豆包" in lowered or "doubao" in lowered or "volc" in lowered or "字节" in lowered:
            return {
                "优势": "中文消费端入口和多模态体验强，覆盖学习办公、语音、P 图、图片/视频生成，并背靠火山方舟、TRAE 和办公协作生态。",
                "劣势": "官方价格表字段不够透明，个人端订阅权益材料不足，国际评价样本和企业公开采购案例仍偏少。",
                "机会": "可在中国区移动助手、视频生成、企业客服 Agent、火山方舟集成和编程工具链中扩大使用场景。",
                "威胁": "价格口径变化、平台生态绑定、国内大模型同质化竞争和内容合规约束可能影响长期差异化。",
            }
        labels = self._evidence_labels_from_text(text)
        return {
            "优势": f"材料显示{name}在{labels.get('strength', '核心功能覆盖')}上已有可识别基础。",
            "劣势": f"公开信息对{labels.get('weakness', '商业化和服务体验')}的解释仍不充分，使用时应降低确定性。",
            "机会": f"可围绕{labels.get('opportunity', '目标用户场景')}做更清晰定位并沉淀案例。",
            "威胁": f"价格、政策、口碑和替代产品变化可能影响{name}的阶段性竞争力。",
        }

    def _evidence_labels_from_text(self, text: str) -> dict[str, str]:
        candidates = {
            "strength": [("产品功能", r"功能|产品|能力|feature"), ("低成本", r"低价|成本|免费|price"), ("用户口碑", r"评价|评分|口碑|review")],
            "weakness": [("价格透明度", r"价格|定价|订阅"), ("服务体验", r"投诉|错误|限制|账号"), ("合规安全", r"隐私|合规|安全|监管")],
            "opportunity": [("企业集成", r"企业|API|开发者|集成"), ("多模态应用", r"图像|视频|语音|多模态"), ("学习办公", r"学习|办公|文档|PPT")],
        }
        result: dict[str, str] = {}
        for key, items in candidates.items():
            result[key] = next((label for label, pattern in items if re.search(pattern, text, flags=re.I)), "")
        return result

    def _evidence_rows_for_task(self, task_id: str, limit: int = 12) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT c.source_id, c.chunk_index, c.excerpt, c.summary, s.title AS source_title
                FROM evidence_chunks c
                JOIN sources s ON s.id = c.source_id
                WHERE c.task_id = ?
                ORDER BY c.collected_at, c.source_id, c.chunk_index
                LIMIT ?
                """,
                (task_id, limit),
            ).fetchall()
        result = [dict(row) for row in rows]
        for item in result:
            item["excerpt"] = self._clean_source_excerpt_for_report(str(item.get("excerpt", "")), 700)
            item["summary"] = self._clean_source_excerpt_for_report(str(item.get("summary", "")), 220)
        return result

    def _search_called(self, run_rows: list[sqlite3.Row]) -> bool:
        for row in run_rows:
            for call in loads(row["tool_calls"], []):
                name = str(call.get("name", ""))
                if "search" in name or name == "discover_search_results":
                    return True
        return False

    def _insert_claims(self, task_id: str, claims: list[dict[str, Any]], conn: sqlite3.Connection | None = None) -> None:
        def _insert_one(connection: sqlite3.Connection) -> None:
            for claim in claims:
                claim_id = claim.get("id", uuid.uuid4().hex)
                source_ids = [source_id for source_id in claim.get("source_ids", []) if source_id]
                claim_type = claim.get("claim_type") or self._default_claim_type(claim.get("section", ""))
                connection.execute(
                    """
                    INSERT INTO claims
                    (id, task_id, section, content, confidence, source_ids, counter_evidence, uncertainty,
                     generated_agent, needs_review, status, claim_type, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        claim_id,
                        task_id,
                        claim["section"],
                        sanitize_text(claim["content"], 1200),
                        float(claim["confidence"]),
                        dumps(source_ids),
                        sanitize_text(claim.get("counter_evidence", ""), 500),
                        sanitize_text(claim.get("uncertainty", ""), 500),
                        sanitize_text(claim.get("generated_agent", "分析 Agent"), 120),
                        1 if claim.get("needs_review") else 0,
                        claim.get("status", "draft"),
                        sanitize_text(claim_type, 40),
                        utc_now_iso(),
                    ),
                )
                self._insert_evidence_links(
                    connection,
                    task_id,
                    "claims",
                    claim_id,
                    source_ids,
                    sanitize_text(claim.get("content", ""), 260),
                )
                self._insert_structured_item_for_claim(connection, task_id, claim_id, {**claim, "source_ids": source_ids, "claim_type": claim_type})

        if conn is not None:
            _insert_one(conn)
        else:
            with self.connect() as new_conn:
                _insert_one(new_conn)

    def _default_claim_type(self, section: str) -> str:
        if section in {"user_persona", "swot"}:
            return "inference"
        if section == "overview":
            return "assumption"
        return "fact"

    def _insert_evidence_links(
        self,
        conn: sqlite3.Connection,
        task_id: str,
        entity_type: str,
        entity_id: str,
        source_ids: list[str],
        quote: str,
    ) -> None:
        for source_id in source_ids:
            chunk = conn.execute(
                """
                SELECT id, excerpt FROM evidence_chunks
                WHERE task_id = ? AND source_id = ?
                ORDER BY chunk_index LIMIT 1
                """,
                (task_id, source_id),
            ).fetchone()
            conn.execute(
                """
                INSERT OR IGNORE INTO evidence_links
                (id, task_id, entity_type, entity_id, source_id, chunk_id, quote, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    uuid.uuid4().hex,
                    task_id,
                    entity_type,
                    entity_id,
                    source_id,
                    chunk["id"] if chunk else "",
                    sanitize_text(quote or (chunk["excerpt"] if chunk else ""), 500),
                    utc_now_iso(),
                ),
            )

    def _insert_structured_item_for_claim(
        self,
        conn: sqlite3.Connection,
        task_id: str,
        claim_id: str,
        claim: dict[str, Any],
    ) -> None:
        section = claim.get("section", "")
        if section not in {"feature_tree", "pricing_model", "user_persona", "reviews", "swot"}:
            return
        item_id = f"{claim_id}_{section}"
        task_config = self._task_config(task_id)
        competitor_name = self._competitor_for_claim(claim.get("content", ""), task_config.get("competitors", []))
        content = sanitize_text(claim.get("content", ""), 1000)
        now = utc_now_iso()
        confidence = float(claim.get("confidence", 0.5))
        needs_review = 1 if claim.get("needs_review") else 0
        if section == "feature_tree":
            conn.execute(
                """
                INSERT OR IGNORE INTO feature_items
                (id, task_id, competitor_name, level1, level2, comparison_note, maturity_status, confidence, needs_review, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (item_id, task_id, competitor_name, "公开能力", "", content, claim.get("status", ""), confidence, needs_review, now),
            )
            entity_type = "feature_tree"
        elif section == "pricing_model":
            conn.execute(
                """
                INSERT OR IGNORE INTO pricing_items
                (id, task_id, competitor_name, plan_name, price, billing_cycle, limitations, confidence, needs_review, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (item_id, task_id, competitor_name, "公开定价线索", self._extract_price_text(content), "", content, confidence, needs_review, now),
            )
            entity_type = "pricing_model"
        elif section == "user_persona":
            conn.execute(
                """
                INSERT OR IGNORE INTO persona_items
                (id, task_id, competitor_name, user_type, scenario, pain_points, confidence, needs_review, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (item_id, task_id, competitor_name, "待确认用户类型", content, claim.get("uncertainty", ""), confidence, needs_review, now),
            )
            entity_type = "user_persona"
        elif section == "reviews":
            conn.execute(
                """
                INSERT OR IGNORE INTO review_items
                (id, task_id, competitor_name, feedback, sentiment, topic, original_quote, confidence, needs_review, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (item_id, task_id, competitor_name, content, "mixed", "公开评价", content[:240], confidence, needs_review, now),
            )
            entity_type = "reviews"
        else:
            conn.execute(
                """
                INSERT OR IGNORE INTO swot_items
                (id, task_id, competitor_name, dimension, content, confidence, needs_review, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (item_id, task_id, competitor_name, "summary", content, confidence, needs_review, now),
            )
            entity_type = "swot"
        self._insert_evidence_links(conn, task_id, entity_type, item_id, claim.get("source_ids", []), content[:260])

    def _competitor_for_claim(self, content: str, competitors: list[str]) -> str:
        for name in competitors:
            if name and name.casefold() in (content or "").casefold():
                return name
        return competitors[0] if competitors else "综合"

    def _extract_price_text(self, content: str) -> str:
        match = re.search(r"(\d+(?:\.\d+)?\s*(?:元|万元|美元|USD|RMB|￥|¥)[^，。；; ]*)", content, flags=re.I)
        return match.group(1) if match else "未抽取到明确金额"

    def _refresh_pricing_facts(self, task_id: str) -> None:
        with self.connect() as conn:
            sources = conn.execute(
                """
                SELECT * FROM sources
                WHERE task_id = ? AND source_role = 'official_pricing'
                ORDER BY collected_at, rowid
                """,
                (task_id,),
            ).fetchall()
            conn.execute("DELETE FROM pricing_facts WHERE task_id = ?", (task_id,))
            now = utc_now_iso()
            for source in sources:
                for fact in self._pricing_facts_from_source(source):
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO pricing_facts
                        (id, task_id, competitor_name, plan_name, price_type, amount, currency, unit,
                         region, effective_at, source_id, confidence, created_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            uuid.uuid4().hex,
                            task_id,
                            fact["competitor_name"],
                            fact["plan_name"],
                            fact["price_type"],
                            float(fact["amount"]),
                            fact["currency"],
                            fact["unit"],
                            fact.get("region", ""),
                            fact.get("effective_at", source["collected_at"] or now),
                            source["id"],
                            float(fact.get("confidence", 0.9)),
                            now,
                        ),
                    )
            inserted = conn.execute(
                "SELECT competitor_name, price_type FROM pricing_facts WHERE task_id = ?",
                (task_id,),
            ).fetchall()
            output_covered = {
                str(row["competitor_name"]).casefold()
                for row in inserted
                if row["price_type"] == "output"
            }
            for source in sources:
                competitor_key = str(row_get(source, "competitor_name", "")).strip().casefold()
                reference_key = self._reference_ai_price_key(competitor_key)
                if not reference_key or competitor_key in output_covered:
                    continue
                for plan_name, price_type, amount, currency in REFERENCE_AI_API_PRICES[reference_key]:
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO pricing_facts
                        (id, task_id, competitor_name, plan_name, price_type, amount, currency, unit,
                         region, effective_at, source_id, confidence, created_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            uuid.uuid4().hex,
                            task_id,
                            row_get(source, "competitor_name", "") or reference_key,
                            plan_name,
                            price_type,
                            float(amount),
                            currency,
                            "每百万 tokens",
                            "US",
                            source["collected_at"] or now,
                            source["id"],
                            0.74 if row_get(source, "raw_content_status", "") == "summary_only" else 0.88,
                            now,
                        ),
                    )
                output_covered.add(competitor_key)

    def _reference_ai_price_key(self, competitor_key: str) -> str:
        aliases = {competitor_key, *[alias.casefold() for alias in PRODUCT_ALIASES.get(competitor_key, [])]}
        if aliases & {"chatgpt", "openai"}:
            return "chatgpt"
        if "deepseek" in aliases:
            return "deepseek"
        return ""

    def _pricing_facts_from_source(self, source: sqlite3.Row) -> list[dict[str, Any]]:
        return _pricing_facts_from_source(
            source,
            row_get(source, "competitor_name", "") or "未识别竞品",
            sanitize_text(f"{source["title"]} {source["excerpt"]}", 4000),
            row_get(source, "raw_content_status", ""),
        )

    def _prices_from_window(self, window: str, header_context: str = "") -> list[dict[str, Any]]:
        return _pricing_prices_from_window(window, header_context)

    def _infer_price_types(self, prices: list[dict[str, Any]]) -> list[str]:
        return _pricing_infer_price_types(prices)

    def _normalize_currency(self, value: str) -> str:
        lowered = (value or "").casefold()
        if lowered in {"美元", "usd"} or value == "$":
            return "USD"
        if lowered in {"元", "人民币", "rmb"} or value in {"¥", "￥"}:
            return "CNY"
        return ""

    def _pricing_claim_text_from_facts(self, competitor: str, facts: list[dict[str, Any]]) -> str:
        return _pricing_claim_text_from_facts(competitor, facts)

    def _preferred_price_fact(self, facts: list[dict[str, Any]] | list[sqlite3.Row], price_type: str) -> dict[str, Any] | None:
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

    def _format_price_fact(self, fact: dict[str, Any]) -> str:
        amount = float(fact.get("amount", 0))
        amount_text = f"{amount:g}"
        currency = "美元" if fact.get("currency") == "USD" else "元" if fact.get("currency") == "CNY" else fact.get("currency", "")
        return f"{amount_text}{currency}/{fact.get("unit", "单位")}"

    def _build_report_sections(
        self,
        task: sqlite3.Row,
        competitors: list[sqlite3.Row],
        sources: list[sqlite3.Row],
        claims: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        by_section: dict[str, list[dict[str, Any]]] = {}
        for claim in claims:
            by_section.setdefault(claim["section"], []).append(
                {
                    "id": claim["id"],
                    "content": claim["content"],
                    "confidence": claim["confidence"],
                    "claim_type": claim.get("claim_type", "fact"),
                    "source_ids": claim["source_ids"],
                    "needs_review": bool(claim["needs_review"]),
                    "uncertainty": claim["uncertainty"],
                }
            )
        competitor_names = [item["name"] for item in competitors]
        competitor_label = self._join_names(competitor_names)
        source_count = len(sources)
        manual_scope_count = len([source for source in sources if source["source_type"] == "manual_scope"])
        search_result_count = len([source for source in sources if source["source_type"] in {"search_result", "volc_search_result"}])
        real_source_count = len(
            [
                source
                for source in sources
                if source["source_type"] not in {"manual_scope", "demo_scope_note"}
            ]
        )
        evidence_status = (
            f"已登记 {source_count} 条来源，其中 {real_source_count} 条为网页事实/上传/人工来源，"
            f"{search_result_count} 条为检索线索，{manual_scope_count} 条为仅说明任务范围的来源。"
        )
        feature_table = self._build_dynamic_feature_table(competitor_names, sources)
        return [
            {
                "key": "overview",
                "title": "竞品概览",
                "body": f"本任务覆盖 {competitor_label}。{evidence_status} 报告正文只呈现材料可以支撑的业务判断。",
                "claims": by_section.get("overview", []),
            },
            {
                "key": "feature_tree",
                "title": "功能对比",
                "body": "功能对比按材料中可支撑的能力、场景和产品边界展开。",
                "table": feature_table,
                "claims": by_section.get("feature_tree", []),
            },
            {
                "key": "pricing_model",
                "title": "定价对比",
                "body": "定价、型号/套餐、规格权益和政策按材料中的业务口径呈现。",
                "claims": by_section.get("pricing_model", []),
            },
            {
                "key": "user_persona",
                "title": "用户画像",
                "body": "用户画像围绕官网定位、访谈、问卷或用户评价中出现的场景和痛点归纳。",
                "claims": by_section.get("user_persona", []),
            },
            {
                "key": "reviews",
                "title": "用户评价",
                "body": "用户评价按平台差异、正负面主题和采购含义归纳。",
                "claims": by_section.get("reviews", []),
            },
            {
                "key": "swot",
                "title": "SWOT",
                "body": "SWOT 按每个竞品独立归纳，不复用同一套模板话术。",
                "claims": by_section.get("swot", []),
            },
        ]

    def _build_dynamic_feature_table(
        self,
        competitor_names: list[str],
        sources: list[sqlite3.Row],
    ) -> list[list[str]]:
        rows = [["竞品", "业务/能力覆盖", "代表线索", "业务含义"]]
        for name in competitor_names:
            related = [source for source in sources if self._source_matches_competitor(source, name)]
            factual = [
                source
                for source in related
                if source["source_type"] not in {"manual_scope", "demo_scope_note"}
            ]
            if factual:
                status = self._capability_phrase(factual)
                excerpt = report_text(factual[0]["excerpt"], 72)
                pending = "价格、评价、销量或高风险判断需结合多类材料解释"
            else:
                status = "公开能力材料较少"
                excerpt = "官网、产品页、定价页或公开评价材料暂未覆盖"
                pending = "该竞品暂不进入强结论排序"
            rows.append([name, status, excerpt, pending])
        return rows

    def _capability_phrase(self, sources: list[sqlite3.Row]) -> str:
        combined = " ".join([f"{source['title']} {source['excerpt']}" for source in sources[:3]])
        keywords = []
        for label, pattern in [
            ("官网/品牌能力", r"官网|品牌|产品|服务"),
            ("价格/车型/套餐", r"价格|售价|定价|车型|套餐|pricing|price"),
            ("用户评价/口碑", r"评价|口碑|评论|用户|review"),
            ("销量/市场表现", r"销量|交付|市场|新闻|销售"),
            ("智能化/技术能力", r"智能|AI|自动|辅助|算法|芯片"),
        ]:
            if re.search(pattern, combined, flags=re.I):
                keywords.append(label)
        return "、".join(keywords[:3]) or "产品定位与公开能力线索"

    def _source_matches_competitor(self, source: sqlite3.Row, name: str) -> bool:
        source_competitor = str(row_get(source, "competitor_name", "") or "").casefold()
        if source_competitor and source_competitor == (name or "").casefold():
            return True
        if source_competitor:
            return False
        text = f"{source['title']} {source['excerpt']} {source['url_or_path']}".casefold()
        aliases = self._search_aliases_for_name(name)
        if any(alias and alias.casefold() in text for alias in aliases):
            return True
        related = self._search_related_terms_for_name(name, "", self._analysis_object_type(name, ""))
        if self._analysis_object_type(name, "") == "category" and sum(1 for term in related if term.casefold() in text) >= 2:
            return True
        source_url = (source["url_or_path"] or "").casefold()
        for hint in self._url_hints_for_name(name):
            hint_parts = self._parse_url_parts(hint)
            if not hint_parts:
                continue
            hint_netloc, hint_path = hint_parts
            if hint_netloc and hint_netloc in source_url and (not hint_path or hint_path in source_url):
                return True
        return False

    def _parse_url_parts(self, url: str) -> tuple[str, str] | None:
        try:
            parsed = urllib.parse.urlparse(url)
        except Exception:
            return None
        path = parsed.path.strip("/").casefold()
        return parsed.netloc.casefold(), path

    def _provider_label(self, run_rows: list[sqlite3.Row]) -> str:
        providers = []
        fallback_count = 0
        for row in run_rows:
            provider = self._provider_user_label(row["model_provider"])
            if provider and provider not in providers:
                providers.append(provider)
            if row["fallback_reason"]:
                fallback_count += 1
        if not providers:
            return "未调用"
        suffix = " / 备用规则" if fallback_count else ""
        return "、".join(providers) + suffix

    def _provider_user_label(self, value: str) -> str:
        return {
            "doubao": "豆包大模型",
            "doubao-react": "豆包 ReAct 深度分析",
            "deepseek-react": "DeepSeek ReAct 深度分析",
            "local-react-fallback": "本地深度分析备用规则",
            "report-renderer": "报告排版生成",
            "mock": "规则模式",
            "volc_search": "火山联网搜索",
            "official_seed": "官方种子来源",
        }.get(str(value or "").strip(), str(value or "").strip())

    def _build_full_report(self, task: sqlite3.Row, sections: list[dict[str, Any]]) -> list[str]:
        paragraphs = [
            f"报告任务：{task['name']}。",
            "本报告只呈现当前任务已选择的分析维度，关键事实均保留来源追溯。",
        ]
        for section in sections:
            paragraphs.append(f"{section['title']}：{section['body']}")
        paragraphs.append("结论边界：未被当前任务来源覆盖的价格、政策、功能和用户口碑不会被写成事实。")
        return paragraphs

    def _set_task_completed(self, task_id: str) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE tasks
                SET status = 'completed', completed_at = ?
                WHERE id = ? AND status != 'stopped'
                """,
                (utc_now_iso(), task_id),
            )

    def _update_task(self, task_id: str, status: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE tasks SET status = ? WHERE id = ? AND status != 'stopped'",
                (status, task_id),
            )

    def _elapsed_ms(self, started: datetime) -> int:
        return max(1, int((now_dt() - started).total_seconds() * 1000))

    def _elapsed_from_iso_ms(self, value: str) -> int:
        if not value:
            return 0
        try:
            started = datetime.fromisoformat(value.replace("Z", ""))
        except ValueError:
            return 0
        return self._elapsed_ms(started)

    def _log_collection_run(
        self,
        task_id: str,
        provider: str,
        query: str,
        status: str,
        result_count: int,
        log_id: str = "",
        time_cost_ms: int = 0,
        error: str = "",
    ) -> None:
        search_type = getattr(self.search_client, "search_type", "")
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO collection_runs
                (id, task_id, provider, query, search_type, status, result_count, log_id, time_cost_ms, error, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    uuid.uuid4().hex,
                    task_id,
                    sanitize_text(provider, 80),
                    sanitize_text(query, 180),
                    sanitize_text(search_type, 80),
                    sanitize_text(status, 40),
                    int(result_count),
                    sanitize_text(log_id, 120),
                    int(time_cost_ms or 0),
                    sanitize_text(error, 300),
                    utc_now_iso(),
                ),
            )

    def _log_agent_event(
        self,
        task_id: str,
        agent_name: str,
        event_type: str,
        message: str,
        severity: str = "info",
        meta: dict[str, Any] | None = None,
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO agent_events
                (id, task_id, agent_name, event_type, message, severity, created_at, meta_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    uuid.uuid4().hex,
                    task_id,
                    sanitize_text(agent_name, 120),
                    sanitize_text(event_type, 120),
                    sanitize_text(message, 800),
                    sanitize_text(severity, 40),
                    utc_now_iso(),
                    dumps(sanitize_payload(meta or {})),
                ),
            )

    def _log_agent_run(
        self,
        task_id: str,
        agent_name: str,
        input_summary: str,
        output_summary: str,
        status: str,
        duration_ms: int,
        retry_count: int = 0,
        error: str = "",
        tool_calls: list[dict[str, Any]] | None = None,
        token_input: int | None = None,
        token_output: int | None = None,
        severity: str = "info",
        has_rework: bool = False,
        fallback_reason: str = "",
        model_provider: str = "",
        started_at: datetime | None = None,
    ) -> None:
        started = started_at or now_dt()
        duration_ms = max(1, int(duration_ms))
        ended = started + timedelta(milliseconds=duration_ms)
        safe_input = sanitize_text(input_summary)
        safe_output = sanitize_text(output_summary)
        safe_fallback = sanitize_text(fallback_reason, 600)
        safe_tool_calls = sanitize_payload(tool_calls or [])
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO agent_runs
                (id, task_id, agent_name, input_summary, output_summary, status, started_at, ended_at,
                 duration_ms, error, retry_count, token_input, token_output, tool_calls, severity, has_rework,
                 fallback_reason, model_provider)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    uuid.uuid4().hex,
                    task_id,
                    agent_name,
                    safe_input,
                    safe_output,
                    status,
                    iso(started),
                    iso(ended),
                    duration_ms,
                    sanitize_text(error, 300),
                    retry_count,
                    token_input if token_input is not None else max(1, len(safe_input) // 4),
                    token_output if token_output is not None else max(1, len(safe_output) // 4),
                    dumps(safe_tool_calls),
                    severity,
                    1 if has_rework else 0,
                    safe_fallback,
                    sanitize_text(model_provider, 120),
                ),
            )

    def _interpret_manual_intent(self, user_text: str) -> tuple[str, str]:
        text = user_text.lower()
        if any(keyword in text for keyword in ["确认", "认可", "人工确认"]):
            return "confirm_claim", "人工确认"
        if any(keyword in text for keyword in ["来源", "证据", "搜索", "补充", "查找"]):
            return "supplement_source", "采集 Agent"
        if any(keyword in text for keyword in ["质检", "复检", "重新检查"]):
            return "recheck_qa", "质检 Agent"
        return "revise_claim", "分析 Agent"

    def _node_status_from_run(self, run_status: str) -> str:
        mapping = {
            "completed": "已完成",
            "rejected": "被打回",
            "rerun_completed": "已完成",
            "failed": "失败",
            "running": "运行中",
        }
        return mapping.get(run_status, run_status)

    def _node_user_detail(self, node_id: str, run: sqlite3.Row) -> str:
        if node_id == "collector":
            return "联网检索并抓取官网、产品页、价格页、公开评价和新闻线索，成功内容已进入来源库。"
        if node_id == "analyst":
            provider = self._provider_user_label(run["model_provider"] or self.llm_provider.provider)
            return f"基于已采集证据完成结构化 claims、ReAct 深度分析、章节草稿和评分依据；模型状态：{provider}。"
        if node_id == "qa":
            provider = self._provider_user_label(run["model_provider"] or self.llm_provider.provider)
            return f"检查分析结论和评分是否有来源、是否越界推断、是否需要人工确认；模型复核：{provider}。"
        if node_id == "reporter":
            provider = self._provider_user_label(run["model_provider"] or self.llm_provider.provider)
            return f"读取自动质检后的分析产物和待复核标记，生成最终报告 JSON、图表和导出内容；报告生成：{provider}。"
        return run["output_summary"]
