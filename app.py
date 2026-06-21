from __future__ import annotations

import json
import os
import sqlite3
import uuid
import csv
import io
import re
import site
import sys
import time
import zipfile
import threading
from pathlib import Path
from typing import Any


def _enable_user_site_packages() -> None:
    try:
        user_site = site.getusersitepackages()
    except Exception:
        return
    if user_site and os.path.isdir(user_site) and user_site not in sys.path:
        sys.path.insert(0, user_site)


_enable_user_site_packages()

from flask import Flask, abort, jsonify, render_template, request, send_file
from pydantic import ValidationError
from werkzeug.exceptions import HTTPException
from werkzeug.utils import secure_filename

from collector import VolcWebSearchClient
from feishu_publisher import FeishuPublishError, FeishuQuestionnairePublisher
from orchestrator import Orchestrator, dumps, loads, sanitize_payload, sanitize_text
from llm_provider import LLMProvider
from react_report_agent import react_provider_status
from rss_collector import google_alerts_status
from schema import TaskCreate, utc_now_iso


BASE_DIR = Path(__file__).resolve().parent
APP_VERSION = "20260620-manual-submit-2"
DEFAULT_DB = BASE_DIR / "data" / "app.db"
DATASET_PATH = BASE_DIR / "data" / "demo_dataset.json"
UPLOAD_DIR = BASE_DIR / "data" / "uploads"
ALLOWED_UPLOAD_EXTENSIONS = {".txt", ".md", ".csv", ".json", ".pdf"}
ENV_PATH = BASE_DIR / ".env"
LOCAL_ENV_PATH = BASE_DIR / ".env.local"
SYSTEM2_ENV_PATH = Path(os.environ.get("COMPETITORSMART_ENV_PATH") or (Path.home() / "Documents" / "New project" / "competitorsmart" / ".env"))


INDUSTRY_KEYWORDS = [
    (
        {
            "比亚迪",
            "byd",
            "小鹏",
            "小鹏汽车",
            "xpeng",
            "理想",
            "理想汽车",
            "li auto",
            "蔚来",
            "nio",
            "特斯拉",
            "tesla",
            "问界",
            "aito",
            "极氪",
            "zeekr",
            "汽车",
            "新能源车",
            "新能源汽车",
            "智能汽车",
        },
        "新能源汽车与智能汽车",
    ),
    ({"chatgpt", "openai", "豆包", "doubao", "claude", "deepseek", "kimi", "通义", "文心", "gemini"}, "AI 大模型与智能助手"),
    ({"飞书", "notion", "airtable", "slack", "trello", "asana"}, "协同办公与知识管理"),
    ({"淘宝", "京东", "拼多多", "amazon", "shopify"}, "电商与零售平台"),
    ({"抖音", "快手", "小红书", "bilibili", "youtube", "tiktok"}, "内容社区与短视频平台"),
]


def load_local_env(path: Path) -> None:
    """Load local demo secrets without overriding real environment variables."""
    if not path.exists():
        return
    allowed_names = {
        "LLM_PROVIDER",
        "DOUBAO_API_KEY",
        "DOUBAO_ENDPOINT_ID",
        "DOUBAO_MODEL_NAME",
        "DOUBAO_BASE_URL",
        "DOUBAO_TIMEOUT_SECONDS",
        "DEEPSEEK_API_BASE_URL",
        "DEEPSEEK_API_KEY",
        "DEEPSEEK_MODEL",
        "DEEPSEEK_THINKING_TYPE",
        "DEEPSEEK_DIRECT_THINKING_MODE",
        "REACT_REPORT_ENABLED",
        "REACT_AGENT_PROVIDER",
        "REACT_AGENT_TEMPERATURE",
        "REACT_AGENT_MAX_TOKENS",
        "REACT_AGENT_RECURSION_LIMIT",
        "REACT_AGENT_MAX_SECONDS",
        "DEEPSEEK_REACT_MAX_SECONDS",
        "DOUBAO_REACT_MAX_SECONDS",
        "REACT_ARTIFACT_REPLACE_MIN_RATIO",
        "VOLC_SEARCH_API_KEY",
        "VOLC_SEARCH_BASE_URL",
        "VOLC_SEARCH_TYPE",
        "VOLC_SEARCH_COUNT",
        "VOLC_SEARCH_TIME_RANGE",
        "GOOGLE_ALERTS_RSS_URL",
        "GOOGLE_ALERTS_RSS_URLS",
        "GOOGLE_ALERTS_RSS_TIMEOUT_SECONDS",
        "GOOGLE_ALERTS_RSS_MAX_ITEMS",
        "APPARK_BROWSER_COLLECT_ENABLED",
        "APPARK_CHROME_CDP_URL",
        "APPARK_CACHE_PATH",
        "FLASK_SECRET_KEY",
        "API_TOKEN",
        "FEISHU_CLI_PATH",
        "LARK_CLI_PATH",
        "FEISHU_DEFAULT_FOLDER_TOKEN",
        "FEISHU_IDENTITY",
    }
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key not in allowed_names or not value or os.environ.get(key):
            continue
        os.environ[key] = value


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    industry TEXT NOT NULL,
    competitors_json TEXT NOT NULL,
    websites_json TEXT NOT NULL,
    focus_areas_json TEXT NOT NULL,
    source_mode TEXT NOT NULL,
    notes TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,
    archived INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    completed_at TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS competitors (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    name TEXT NOT NULL,
    website TEXT NOT NULL DEFAULT '',
    industry TEXT NOT NULL,
    target_users_json TEXT NOT NULL DEFAULT '[]',
    collected_at TEXT NOT NULL,
    FOREIGN KEY(task_id) REFERENCES tasks(id)
);

CREATE TABLE IF NOT EXISTS sources (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    source_type TEXT NOT NULL,
    title TEXT NOT NULL,
    url_or_path TEXT NOT NULL,
    author_site TEXT NOT NULL,
    published_at TEXT NOT NULL DEFAULT '',
    collected_at TEXT NOT NULL,
    credibility TEXT NOT NULL,
    excerpt TEXT NOT NULL,
    related_claim_ids TEXT NOT NULL DEFAULT '[]',
    fallback_reason TEXT NOT NULL DEFAULT '',
    provider TEXT NOT NULL DEFAULT '',
    search_log_id TEXT NOT NULL DEFAULT '',
    search_query TEXT NOT NULL DEFAULT '',
    auth_info TEXT NOT NULL DEFAULT '',
    auth_level INTEGER NOT NULL DEFAULT 0,
    time_cost_ms INTEGER NOT NULL DEFAULT 0,
    competitor_name TEXT NOT NULL DEFAULT '',
    module TEXT NOT NULL DEFAULT '',
    relevance_score INTEGER NOT NULL DEFAULT 0,
    source_role TEXT NOT NULL DEFAULT '',
    raw_content_status TEXT NOT NULL DEFAULT 'summary_only',
    FOREIGN KEY(task_id) REFERENCES tasks(id)
);

CREATE TABLE IF NOT EXISTS evidence_chunks (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    source_id TEXT NOT NULL,
    chunk_index INTEGER NOT NULL,
    char_start INTEGER NOT NULL,
    char_end INTEGER NOT NULL,
    summary TEXT NOT NULL,
    excerpt TEXT NOT NULL,
    collected_at TEXT NOT NULL,
    FOREIGN KEY(task_id) REFERENCES tasks(id),
    FOREIGN KEY(source_id) REFERENCES sources(id)
);

CREATE TABLE IF NOT EXISTS claims (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    section TEXT NOT NULL,
    content TEXT NOT NULL,
    confidence REAL NOT NULL,
    source_ids TEXT NOT NULL DEFAULT '[]',
    counter_evidence TEXT NOT NULL DEFAULT '',
    uncertainty TEXT NOT NULL DEFAULT '',
    generated_agent TEXT NOT NULL,
    needs_review INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL,
    claim_type TEXT NOT NULL DEFAULT 'fact',
    created_at TEXT NOT NULL,
    FOREIGN KEY(task_id) REFERENCES tasks(id)
);

CREATE TABLE IF NOT EXISTS agent_runs (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    agent_name TEXT NOT NULL,
    input_summary TEXT NOT NULL,
    output_summary TEXT NOT NULL,
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    ended_at TEXT NOT NULL,
    duration_ms INTEGER NOT NULL,
    error TEXT NOT NULL DEFAULT '',
    retry_count INTEGER NOT NULL DEFAULT 0,
    token_input INTEGER NOT NULL DEFAULT 0,
    token_output INTEGER NOT NULL DEFAULT 0,
    tool_calls TEXT NOT NULL DEFAULT '[]',
    severity TEXT NOT NULL DEFAULT 'info',
    has_rework INTEGER NOT NULL DEFAULT 0,
    fallback_reason TEXT NOT NULL DEFAULT '',
    model_provider TEXT NOT NULL DEFAULT '',
    FOREIGN KEY(task_id) REFERENCES tasks(id)
);

CREATE TABLE IF NOT EXISTS agent_events (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    agent_name TEXT NOT NULL,
    event_type TEXT NOT NULL,
    message TEXT NOT NULL,
    severity TEXT NOT NULL DEFAULT 'info',
    created_at TEXT NOT NULL,
    meta_json TEXT NOT NULL DEFAULT '{}',
    FOREIGN KEY(task_id) REFERENCES tasks(id)
);

CREATE TABLE IF NOT EXISTS qa_findings (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    claim_id TEXT NOT NULL,
    severity TEXT NOT NULL,
    reason TEXT NOT NULL,
    target_agent TEXT NOT NULL,
    finding_type TEXT NOT NULL DEFAULT 'general',
    action_hint TEXT NOT NULL DEFAULT '',
    meta_json TEXT NOT NULL DEFAULT '{}',
    fix_status TEXT NOT NULL,
    recheck_result TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    fixed_at TEXT NOT NULL DEFAULT '',
    FOREIGN KEY(task_id) REFERENCES tasks(id)
);

CREATE TABLE IF NOT EXISTS reports (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    title TEXT NOT NULL,
    content_json TEXT NOT NULL,
    generated_at TEXT NOT NULL,
    citation_map TEXT NOT NULL,
    qa_status TEXT NOT NULL,
    confidence_score REAL NOT NULL,
    FOREIGN KEY(task_id) REFERENCES tasks(id)
);

CREATE TABLE IF NOT EXISTS analysis_artifacts (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    provider TEXT NOT NULL DEFAULT '',
    analysis_markdown TEXT NOT NULL DEFAULT '',
    sections_json TEXT NOT NULL DEFAULT '[]',
    score_dimensions_json TEXT NOT NULL DEFAULT '[]',
    radar_data_json TEXT NOT NULL DEFAULT '[]',
    tool_calls_json TEXT NOT NULL DEFAULT '[]',
    screenshots_json TEXT NOT NULL DEFAULT '[]',
    fallback_reason TEXT NOT NULL DEFAULT '',
    token_input INTEGER NOT NULL DEFAULT 0,
    token_output INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    FOREIGN KEY(task_id) REFERENCES tasks(id)
);

CREATE TABLE IF NOT EXISTS manual_actions (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    claim_id TEXT NOT NULL DEFAULT '',
    user_text TEXT NOT NULL,
    selected_text TEXT NOT NULL DEFAULT '',
    interpreted_intent TEXT NOT NULL,
    target_agent TEXT NOT NULL,
    status TEXT NOT NULL,
    result_summary TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(task_id) REFERENCES tasks(id)
);

CREATE TABLE IF NOT EXISTS collection_runs (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    provider TEXT NOT NULL,
    query TEXT NOT NULL,
    search_type TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,
    result_count INTEGER NOT NULL DEFAULT 0,
    log_id TEXT NOT NULL DEFAULT '',
    time_cost_ms INTEGER NOT NULL DEFAULT 0,
    error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    FOREIGN KEY(task_id) REFERENCES tasks(id)
);

CREATE TABLE IF NOT EXISTS feature_items (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    competitor_name TEXT NOT NULL,
    level1 TEXT NOT NULL,
    level2 TEXT NOT NULL DEFAULT '',
    comparison_note TEXT NOT NULL,
    maturity_status TEXT NOT NULL DEFAULT '',
    confidence REAL NOT NULL,
    needs_review INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    FOREIGN KEY(task_id) REFERENCES tasks(id)
);

CREATE TABLE IF NOT EXISTS pricing_items (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    competitor_name TEXT NOT NULL,
    plan_name TEXT NOT NULL,
    price TEXT NOT NULL DEFAULT '',
    billing_cycle TEXT NOT NULL DEFAULT '',
    limitations TEXT NOT NULL DEFAULT '',
    confidence REAL NOT NULL,
    needs_review INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    FOREIGN KEY(task_id) REFERENCES tasks(id)
);

CREATE TABLE IF NOT EXISTS pricing_facts (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    competitor_name TEXT NOT NULL,
    plan_name TEXT NOT NULL,
    price_type TEXT NOT NULL,
    amount REAL NOT NULL,
    currency TEXT NOT NULL,
    unit TEXT NOT NULL,
    region TEXT NOT NULL DEFAULT '',
    effective_at TEXT NOT NULL DEFAULT '',
    source_id TEXT NOT NULL,
    confidence REAL NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(task_id) REFERENCES tasks(id)
);

CREATE TABLE IF NOT EXISTS appark_metrics (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    competitor_name TEXT NOT NULL,
    app_name TEXT NOT NULL,
    publisher TEXT NOT NULL DEFAULT '',
    downloads_text TEXT NOT NULL DEFAULT '',
    downloads_value REAL NOT NULL DEFAULT 0,
    revenue_text TEXT NOT NULL DEFAULT '',
    revenue_usd REAL NOT NULL DEFAULT 0,
    free_rank INTEGER,
    paid_rank INTEGER,
    overall_rank INTEGER,
    country TEXT NOT NULL DEFAULT '',
    store TEXT NOT NULL DEFAULT '',
    time_range TEXT NOT NULL DEFAULT '',
    source_url TEXT NOT NULL DEFAULT '',
    provider TEXT NOT NULL DEFAULT 'appark',
    collected_at TEXT NOT NULL,
    raw_json TEXT NOT NULL DEFAULT '{}',
    FOREIGN KEY(task_id) REFERENCES tasks(id)
);

CREATE TABLE IF NOT EXISTS persona_items (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    competitor_name TEXT NOT NULL,
    user_type TEXT NOT NULL,
    scenario TEXT NOT NULL DEFAULT '',
    pain_points TEXT NOT NULL DEFAULT '',
    confidence REAL NOT NULL,
    needs_review INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    FOREIGN KEY(task_id) REFERENCES tasks(id)
);

CREATE TABLE IF NOT EXISTS review_items (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    competitor_name TEXT NOT NULL,
    feedback TEXT NOT NULL,
    sentiment TEXT NOT NULL DEFAULT '',
    topic TEXT NOT NULL DEFAULT '',
    original_quote TEXT NOT NULL DEFAULT '',
    confidence REAL NOT NULL,
    needs_review INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    FOREIGN KEY(task_id) REFERENCES tasks(id)
);

CREATE TABLE IF NOT EXISTS swot_items (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    competitor_name TEXT NOT NULL,
    dimension TEXT NOT NULL,
    content TEXT NOT NULL,
    confidence REAL NOT NULL,
    needs_review INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    FOREIGN KEY(task_id) REFERENCES tasks(id)
);

CREATE TABLE IF NOT EXISTS evidence_links (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    source_id TEXT NOT NULL,
    chunk_id TEXT NOT NULL DEFAULT '',
    quote TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    FOREIGN KEY(task_id) REFERENCES tasks(id),
    FOREIGN KEY(source_id) REFERENCES sources(id)
);

CREATE TABLE IF NOT EXISTS questionnaire_designs (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    title TEXT NOT NULL,
    research_objective TEXT NOT NULL,
    target_users TEXT NOT NULL DEFAULT '',
    content_json TEXT NOT NULL,
    focus_dimensions_json TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'draft',
    estimated_time_minutes INTEGER NOT NULL DEFAULT 5,
    created_at TEXT NOT NULL,
    FOREIGN KEY(task_id) REFERENCES tasks(id)
);

CREATE TABLE IF NOT EXISTS questionnaire_responses (
    id TEXT PRIMARY KEY,
    questionnaire_design_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    answers_json TEXT NOT NULL DEFAULT '{}',
    respondent_label TEXT NOT NULL DEFAULT '',
    submitted_at TEXT NOT NULL,
    user_agent TEXT NOT NULL DEFAULT '',
    FOREIGN KEY(questionnaire_design_id) REFERENCES questionnaire_designs(id),
    FOREIGN KEY(task_id) REFERENCES tasks(id)
);

CREATE TABLE IF NOT EXISTS questionnaire_publish_targets (
    id TEXT PRIMARY KEY,
    questionnaire_design_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    provider TEXT NOT NULL,
    external_ids_json TEXT NOT NULL DEFAULT '{}',
    share_url TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending',
    error_message TEXT NOT NULL DEFAULT '',
    published_at TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    FOREIGN KEY(questionnaire_design_id) REFERENCES questionnaire_designs(id),
    FOREIGN KEY(task_id) REFERENCES tasks(id)
);

CREATE TABLE IF NOT EXISTS survey_analyses (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    source_id TEXT NOT NULL,
    questionnaire_design_id TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL,
    respondent_count INTEGER NOT NULL DEFAULT 0,
    summary TEXT NOT NULL,
    segments_json TEXT NOT NULL DEFAULT '[]',
    findings_json TEXT NOT NULL DEFAULT '[]',
    statistics_json TEXT NOT NULL DEFAULT '[]',
    claims_generated_json TEXT NOT NULL DEFAULT '[]',
    confidence_score REAL NOT NULL DEFAULT 0.7,
    created_at TEXT NOT NULL,
    FOREIGN KEY(task_id) REFERENCES tasks(id),
    FOREIGN KEY(source_id) REFERENCES sources(id)
);

CREATE TABLE IF NOT EXISTS interview_analyses (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    source_id TEXT NOT NULL,
    interview_guide_id TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL,
    interviewee_profile_json TEXT NOT NULL DEFAULT '{}',
    summary TEXT NOT NULL,
    key_quotes_json TEXT NOT NULL DEFAULT '[]',
    scenarios_json TEXT NOT NULL DEFAULT '[]',
    pain_points_json TEXT NOT NULL DEFAULT '[]',
    needs_json TEXT NOT NULL DEFAULT '[]',
    claims_generated_json TEXT NOT NULL DEFAULT '[]',
    confidence_score REAL NOT NULL DEFAULT 0.7,
    created_at TEXT NOT NULL,
    FOREIGN KEY(task_id) REFERENCES tasks(id),
    FOREIGN KEY(source_id) REFERENCES sources(id)
);
"""


def create_app(test_config: dict[str, Any] | None = None) -> Flask:
    if not (test_config and test_config.get("TESTING")):
        load_local_env(ENV_PATH)
        load_local_env(LOCAL_ENV_PATH)
        load_local_env(SYSTEM2_ENV_PATH)
    api_token = os.environ.get("API_TOKEN", "")
    app = Flask(__name__, template_folder="templates", static_folder="static")
    app.config.update(
        DATABASE=str(DEFAULT_DB),
        MAX_CONTENT_LENGTH=25 * 1024 * 1024,
        JSON_AS_ASCII=False,
        SECRET_KEY=os.environ.get("FLASK_SECRET_KEY", uuid.uuid4().hex),
        WORKFLOW_ASYNC=True,
        TEMPLATES_AUTO_RELOAD=True,
        API_TOKEN=api_token,
    )
    if test_config:
        app.config.update(test_config)

    def _require_api_token():
        token = (app.config.get("API_TOKEN") or "").strip()
        if not token:
            return
        submitted = (request.headers.get("X-API-Token") or "").strip()
        if submitted != token:
            abort(403, "valid API token required for this operation")

    db_path = Path(app.config["DATABASE"])
    db_path.parent.mkdir(parents=True, exist_ok=True)
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    init_db(db_path)
    recover_interrupted_tasks(db_path)

    orchestrator = Orchestrator(db_path, DATASET_PATH)

    @app.after_request
    def add_security_headers(response):
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-App-Version"] = APP_VERSION
        if request.path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-cache, max-age=0"
        response.headers["X-Frame-Options"] = "SAMEORIGIN"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "script-src 'self'; "
            "style-src 'self'; "
            "img-src 'self' data:; "
            "connect-src 'self'; "
            "object-src 'none'; "
            "base-uri 'none'; "
            "form-action 'self'"
        )
        return response

    @app.errorhandler(ValidationError)
    def handle_validation_error(exc: ValidationError):
        return jsonify({"error": "validation_error", "message": sanitize_text(str(exc), 800)}), 400

    @app.errorhandler(HTTPException)
    def handle_http_error(exc: HTTPException):
        return jsonify({"error": exc.name, "message": sanitize_text(exc.description, 500)}), exc.code

    @app.get("/")
    def index():
        app.jinja_env.cache.clear()
        return render_template("index.html").replace("__APP_VERSION__", APP_VERSION)

    @app.get("/questionnaires/<design_id>")
    def questionnaire_page(design_id: str):
        with connect_db(app.config["DATABASE"]) as conn:
            row = conn.execute(
                """
                SELECT q.*, t.name AS task_name, t.industry AS task_industry, t.competitors_json
                FROM questionnaire_designs q
                JOIN tasks t ON t.id = q.task_id
                WHERE q.id = ?
                """,
                (design_id,),
            ).fetchone()
        if not row:
            abort(404, "questionnaire not found")
        content = loads(row["content_json"], {})
        task = {
            "name": row["task_name"],
            "industry": row["task_industry"],
            "competitors": loads(row["competitors_json"], []),
        }
        return render_template(
            "questionnaire.html",
            app_version=APP_VERSION,
            design_id=design_id,
            design=content,
            task=task,
        )

    @app.post("/questionnaires/<design_id>/responses")
    def submit_questionnaire_response(design_id: str):
        with connect_db(app.config["DATABASE"]) as conn:
            row = conn.execute(
                "SELECT id, task_id, title, content_json FROM questionnaire_designs WHERE id = ?",
                (design_id,),
            ).fetchone()
            if not row:
                abort(404, "questionnaire not found")
            if request.is_json:
                payload = request.get_json(silent=True) or {}
                answers = payload.get("answers", {})
                respondent_label = sanitize_text(str(payload.get("respondent_label", "")), 120)
            else:
                answers = {}
                for key in request.form.keys():
                    values = request.form.getlist(key)
                    answers[key] = values if len(values) > 1 else values[0]
                respondent_label = sanitize_text(str(request.form.get("respondent_label", "")), 120)
            if not isinstance(answers, dict):
                abort(400, "answers must be an object")
            safe_answers = sanitize_payload(answers)
            response_id = uuid.uuid4().hex
            conn.execute(
                """
                INSERT INTO questionnaire_responses
                (id, questionnaire_design_id, task_id, answers_json, respondent_label, submitted_at, user_agent)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    response_id,
                    design_id,
                    row["task_id"],
                    dumps(safe_answers),
                    respondent_label,
                    utc_now_iso(),
                    sanitize_text(request.headers.get("User-Agent", ""), 180),
                ),
            )
        if request.is_json:
            return jsonify({"status": "received", "response_id": response_id}), 201
        return render_template(
            "questionnaire_thanks.html",
            app_version=APP_VERSION,
            title=row["title"],
            response_id=response_id,
        )

    @app.get("/api/health")
    def health():
        return jsonify({"status": "ok", "time": utc_now_iso()})

    @app.get("/api/provider-status")
    def provider_status():
        search_status = VolcWebSearchClient().config_status()
        llm = LLMProvider()
        return jsonify(
            {
                "volc_search": {
                    "provider": search_status.get("provider"),
                    "api_key_configured": bool(search_status.get("api_key_configured")),
                    "base_url": search_status.get("base_url"),
                    "search_type": search_status.get("search_type"),
                    "count": search_status.get("count"),
                    "time_range": search_status.get("time_range"),
                },
                "doubao": {
                    "provider": llm.provider,
                    "api_key_configured": bool(llm.api_key),
                    "endpoint_configured": bool(llm.endpoint_id),
                    "chat_model_configured": bool(llm.endpoint_id or os.environ.get("DOUBAO_MODEL_NAME", "")),
                    "model_name": sanitize_text(llm.model_name, 120),
                    "base_url": sanitize_text(llm.base_url, 180),
                },
                "react_report": sanitize_payload(react_provider_status()),
                "google_alerts": sanitize_payload(google_alerts_status()),
            }
        )

    @app.post("/api/tasks")
    def create_task():
        payload = request.get_json(silent=True) or {}
        task_create = TaskCreate.model_validate(payload)
        task_id = uuid.uuid4().hex
        now = utc_now_iso()
        competitors = task_create.competitors
        industry = infer_industry(task_create.industry, competitors, task_create.notes)
        if industry == "待识别行业" and "实时采集" in task_create.source_mode:
            industry = LLMProvider().classify_industry(competitors, task_create.notes, fallback=industry)
        websites = normalize_websites(task_create.websites, len(competitors))
        task_name = f"{industry}：{'、'.join(competitors)}竞品分析"
        initial_status = "waiting_materials" if task_create.defer_workflow else "created"

        with connect_db(app.config["DATABASE"]) as conn:
            conn.execute(
                """
                INSERT INTO tasks
                (id, name, industry, competitors_json, websites_json, focus_areas_json, source_mode,
                 notes, status, created_at, completed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task_id,
                    task_name,
                    industry,
                    dumps(competitors),
                    dumps(websites),
                    dumps(task_create.focus_areas),
                    task_create.source_mode,
                    sanitize_text(task_create.notes, 1000),
                    initial_status,
                    now,
                    "",
                ),
            )
            for index, competitor in enumerate(competitors):
                conn.execute(
                    """
                    INSERT INTO competitors
                    (id, task_id, name, website, industry, target_users_json, collected_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        uuid.uuid4().hex,
                        task_id,
                        competitor,
                        websites[index] if index < len(websites) else "",
                        industry,
                        "[]",
                        now,
                    ),
                )

        if not task_create.defer_workflow:
            launch_workflow(orchestrator, task_id, bool(app.config.get("WORKFLOW_ASYNC", True)))
        return jsonify(get_task_payload(app.config["DATABASE"], task_id)), 201

    @app.post("/api/research/questionnaire")
    def draft_questionnaire():
        payload = request.get_json(silent=True) or {}
        research_objective = sanitize_text(str(payload.get("objective", "")), 500)
        if not research_objective:
            abort(400, "objective is required")
        task_context = research_context_from_payload(payload)
        target_users = sanitize_text(str(payload.get("target_users", "")), 300)
        dimensions = payload.get("dimensions")
        if dimensions is not None and not isinstance(dimensions, list):
            dimensions = None
        try:
            result = LLMProvider().design_questionnaire(task_context, research_objective, target_users, dimensions)
            design = result.data
            provider = result.provider
            fallback_reason = result.fallback_reason
        except Exception as exc:
            design = fallback_questionnaire_design(task_context, research_objective, target_users, dimensions)
            provider = "fallback"
            fallback_reason = sanitize_text(str(exc), 240)
        return jsonify(
            {
                "design_id": "",
                "design": design,
                "source_id": "",
                "share_url": "",
                "share_path": "",
                "draft_only": True,
                "provider": provider,
                "fallback_reason": fallback_reason,
            }
        ), 201

    @app.post("/api/research/interview-guide")
    def draft_interview_guide():
        payload = request.get_json(silent=True) or {}
        research_objective = sanitize_text(str(payload.get("objective", "")), 500)
        if not research_objective:
            abort(400, "objective is required")
        task_context = research_context_from_payload(payload)
        target_users = sanitize_text(str(payload.get("target_users", "")), 300)
        interview_count = int(payload.get("interview_count", 5) or 5)
        try:
            result = LLMProvider().design_interview_guide(
                task_context,
                research_objective,
                target_users,
                max(1, min(interview_count, 20)),
            )
            guide = result.data
            provider = result.provider
            fallback_reason = result.fallback_reason
        except Exception as exc:
            guide = fallback_interview_guide(
                task_context,
                research_objective,
                target_users,
                max(1, min(interview_count, 20)),
            )
            provider = "fallback"
            fallback_reason = sanitize_text(str(exc), 240)
        return jsonify(
            {
                "guide_id": "",
                "guide": guide,
                "source_id": "",
                "draft_only": True,
                "provider": provider,
                "fallback_reason": fallback_reason,
            }
        ), 201

    @app.post("/api/tasks/<task_id>/start")
    def start_task(task_id: str):
        ensure_task_exists(app.config["DATABASE"], task_id)
        with connect_db(app.config["DATABASE"]) as conn:
            task = conn.execute("SELECT status, completed_at FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if task["completed_at"] or task["status"] in {"collecting", "analyzing", "reanalyzing", "reporting"}:
            return jsonify(get_task_payload(app.config["DATABASE"], task_id))
        launch_workflow(orchestrator, task_id, bool(app.config.get("WORKFLOW_ASYNC", True)))
        return jsonify(get_task_payload(app.config["DATABASE"], task_id)), 202

    @app.post("/api/tasks/<task_id>/stop")
    def stop_task(task_id: str):
        ensure_task_exists(app.config["DATABASE"], task_id)
        result = orchestrator.stop_workflow(task_id)
        payload = get_task_payload(app.config["DATABASE"], task_id)
        payload["stop_result"] = result
        return jsonify(payload), 202

    @app.get("/api/tasks")
    def list_tasks():
        archived = 1 if request.args.get("archived", "0") in {"1", "true", "yes"} else 0
        with connect_db(app.config["DATABASE"]) as conn:
            rows = conn.execute(
                "SELECT * FROM tasks WHERE archived = ? ORDER BY created_at DESC",
                (archived,),
            ).fetchall()
        return jsonify([serialize_task(row) for row in rows])

    @app.post("/api/tasks/<task_id>/archive")
    def archive_task(task_id: str):
        ensure_task_exists(app.config["DATABASE"], task_id)
        payload = request.get_json(silent=True) or {}
        archived = 1 if payload.get("archived", True) else 0
        with connect_db(app.config["DATABASE"]) as conn:
            conn.execute("UPDATE tasks SET archived = ? WHERE id = ?", (archived, task_id))
        return jsonify({"status": "archived" if archived else "restored", "id": task_id, "archived": bool(archived)})

    @app.delete("/api/tasks/<task_id>")
    def delete_task(task_id: str):
        _require_api_token()
        ensure_task_exists(app.config["DATABASE"], task_id)
        with connect_db(app.config["DATABASE"]) as conn:
            for table in [
                "interview_analyses",
                "survey_analyses",
                "questionnaire_responses",
                "questionnaire_designs",
                "evidence_links",
                "swot_items",
                "review_items",
                "persona_items",
                "pricing_items",
                "pricing_facts",
                "feature_items",
                "collection_runs",
                "manual_actions",
                "reports",
                "analysis_artifacts",
                "qa_findings",
                "agent_events",
                "agent_runs",
                "claims",
                "evidence_chunks",
                "sources",
                "competitors",
                "tasks",
            ]:
                conn.execute(f"DELETE FROM {table} WHERE task_id = ?" if table != "tasks" else "DELETE FROM tasks WHERE id = ?", (task_id,))
        return jsonify({"status": "deleted", "id": task_id})

    @app.get("/api/tasks/<task_id>")
    def get_task(task_id: str):
        return jsonify(get_task_payload(app.config["DATABASE"], task_id))

    @app.get("/api/tasks/<task_id>/dag")
    def get_dag(task_id: str):
        ensure_task_exists(app.config["DATABASE"], task_id)
        return jsonify(orchestrator.build_dag(task_id))

    @app.get("/api/tasks/<task_id>/events")
    def get_events(task_id: str):
        ensure_task_exists(app.config["DATABASE"], task_id)
        with connect_db(app.config["DATABASE"]) as conn:
            rows = conn.execute(
                "SELECT * FROM agent_events WHERE task_id = ? ORDER BY created_at, rowid",
                (task_id,),
            ).fetchall()
        return jsonify([serialize_agent_event(row) for row in rows])

    @app.get("/api/tasks/<task_id>/logs")
    def get_logs(task_id: str):
        ensure_task_exists(app.config["DATABASE"], task_id)
        agent = request.args.get("agent", "")
        status = request.args.get("status", "")
        severity = request.args.get("severity", "")
        has_rework = request.args.get("has_rework", "")
        clauses = ["task_id = ?"]
        params: list[Any] = [task_id]
        if agent:
            clauses.append("agent_name = ?")
            params.append(agent)
        if status:
            clauses.append("status = ?")
            params.append(status)
        if severity:
            clauses.append("severity = ?")
            params.append(severity)
        if has_rework in {"0", "1"}:
            clauses.append("has_rework = ?")
            params.append(int(has_rework))
        with connect_db(app.config["DATABASE"]) as conn:
            rows = conn.execute(
                f"SELECT * FROM agent_runs WHERE {' AND '.join(clauses)} ORDER BY started_at, rowid",
                params,
            ).fetchall()
        return jsonify([serialize_agent_run(row) for row in rows])

    @app.get("/api/tasks/<task_id>/logs/download")
    def download_logs(task_id: str):
        ensure_task_exists(app.config["DATABASE"], task_id)
        agent = request.args.get("agent", "")
        status = request.args.get("status", "")
        severity = request.args.get("severity", "")
        has_rework = request.args.get("has_rework", "")
        clauses = ["task_id = ?"]
        params: list[Any] = [task_id]
        if agent:
            clauses.append("agent_name = ?")
            params.append(agent)
        if status:
            clauses.append("status = ?")
            params.append(status)
        if severity:
            clauses.append("severity = ?")
            params.append(severity)
        if has_rework in {"0", "1"}:
            clauses.append("has_rework = ?")
            params.append(int(has_rework))
        with connect_db(app.config["DATABASE"]) as conn:
            rows = conn.execute(
                f"SELECT * FROM agent_runs WHERE {' AND '.join(clauses)} ORDER BY started_at, rowid",
                params,
            ).fetchall()
        return build_logs_zip(task_id, [serialize_agent_run(row) for row in rows])

    @app.get("/api/tasks/<task_id>/sources")
    def get_sources(task_id: str):
        ensure_task_exists(app.config["DATABASE"], task_id)
        with connect_db(app.config["DATABASE"]) as conn:
            sources = conn.execute(
                "SELECT * FROM sources WHERE task_id = ? ORDER BY collected_at, rowid",
                (task_id,),
            ).fetchall()
            claims = conn.execute(
                "SELECT id, content, source_ids FROM claims WHERE task_id = ?",
                (task_id,),
            ).fetchall()
        related = {source["id"]: [] for source in sources}
        for claim in claims:
            for source_id in loads(claim["source_ids"], []):
                if source_id in related:
                    related[source_id].append({"id": claim["id"], "content": claim["content"]})
        result = []
        for source in sources:
            item = dict(source)
            item["related_claims"] = related[source["id"]]
            item["related_claim_ids"] = [claim["id"] for claim in related[source["id"]]]
            result.append(item)
        return jsonify(result)

    @app.get("/api/tasks/<task_id>/evidence")
    def get_evidence(task_id: str):
        ensure_task_exists(app.config["DATABASE"], task_id)
        with connect_db(app.config["DATABASE"]) as conn:
            rows = conn.execute(
                """
                SELECT c.*, s.title AS source_title, s.url_or_path
                FROM evidence_chunks c
                JOIN sources s ON s.id = c.source_id
                WHERE c.task_id = ?
                ORDER BY c.source_id, c.chunk_index
                """,
                (task_id,),
            ).fetchall()
        return jsonify([dict(row) for row in rows])

    @app.get("/api/tasks/<task_id>/claims")
    def get_claims(task_id: str):
        ensure_task_exists(app.config["DATABASE"], task_id)
        with connect_db(app.config["DATABASE"]) as conn:
            rows = conn.execute(
                "SELECT * FROM claims WHERE task_id = ? ORDER BY created_at, rowid",
                (task_id,),
            ).fetchall()
        return jsonify([serialize_claim(row) for row in rows])

    @app.get("/api/tasks/<task_id>/knowledge")
    def get_knowledge(task_id: str):
        ensure_task_exists(app.config["DATABASE"], task_id)
        return jsonify(build_knowledge_payload(app.config["DATABASE"], task_id))

    @app.get("/api/tasks/<task_id>/report")
    def get_report(task_id: str):
        ensure_task_exists(app.config["DATABASE"], task_id)
        report = latest_report(app.config["DATABASE"], task_id)
        if not report:
            abort(404, "report not found")
        return jsonify(report)

    @app.get("/api/tasks/<task_id>/report/pdf")
    def download_report_pdf(task_id: str):
        ensure_task_exists(app.config["DATABASE"], task_id)
        report = latest_report(app.config["DATABASE"], task_id)
        if not report:
            abort(404, "report not found")
        return build_report_pdf(task_id, report)

    @app.post("/api/tasks/<task_id>/manual-actions")
    def post_manual_action(task_id: str):
        ensure_task_exists(app.config["DATABASE"], task_id)
        payload = request.get_json(silent=True) or {}
        user_text = sanitize_text(str(payload.get("user_text", "")), 1200)
        selected_text = sanitize_text(str(payload.get("selected_text", "")), 1200)
        claim_id = sanitize_text(str(payload.get("claim_id", "")), 120)
        action = sanitize_text(str(payload.get("action", "")), 80)
        if not user_text:
            abort(400, "user_text is required")
        result = orchestrator.handle_manual_action(task_id, user_text, selected_text, claim_id, action=action)
        return jsonify(result), 201

    @app.post("/api/tasks/<task_id>/qa/recheck")
    def post_recheck(task_id: str):
        ensure_task_exists(app.config["DATABASE"], task_id)
        result = orchestrator.recheck_qa(task_id)
        return jsonify(result), 201

    @app.post("/api/tasks/<task_id>/qa/findings/<finding_id>/repair")
    def post_repair_qa_finding(task_id: str, finding_id: str):
        ensure_task_exists(app.config["DATABASE"], task_id)
        payload = request.get_json(silent=True) or {}
        action = sanitize_text(str(payload.get("action", "auto_collect")), 80) or "auto_collect"
        user_text = sanitize_text(str(payload.get("user_text", "")), 1200)
        result = orchestrator.repair_qa_finding(task_id, sanitize_text(finding_id, 120), action=action, user_text=user_text)
        return jsonify(result), 201

    @app.post("/api/uploads")
    def upload_file():
        task_id = request.form.get("task_id", "")
        file = request.files.get("file")
        if not file or not file.filename:
            abort(400, "file is required")
        original_name = safe_upload_filename(file.filename)
        extension = Path(original_name).suffix.lower()
        if extension not in ALLOWED_UPLOAD_EXTENSIONS:
            abort(400, "only txt, md, csv, json and pdf files are allowed")
        stored_name = f"{uuid.uuid4().hex}{extension}"
        stored_path = UPLOAD_DIR / stored_name
        data = file.read()
        if not data:
            abort(400, "empty files are not accepted")
        stored_path.write_bytes(data)
        text = extract_upload_text(data, extension)
        response = {
            "filename": original_name,
            "stored_path": str(stored_path.relative_to(BASE_DIR)),
            "excerpt": sanitize_text(text, 500),
        }
        if task_id:
            ensure_task_exists(app.config["DATABASE"], task_id)
            source_id = f"{task_id[:8]}_upload_{uuid.uuid4().hex[:8]}"
            with connect_db(app.config["DATABASE"]) as conn:
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
                        "uploaded_file",
                        original_name,
                        response["stored_path"],
                        "用户上传材料",
                        "",
                        utc_now_iso(),
                        "medium",
                        response["excerpt"],
                        "[]",
                    ),
                )
            orchestrator.process_uploaded_material(task_id, source_id, original_name, text)
            response["source_id"] = source_id
        return jsonify(response), 201

    @app.post("/api/tasks/<task_id>/questionnaire")
    def design_questionnaire(task_id: str):
        """Generate a survey questionnaire design for this task."""
        ensure_task_exists(app.config["DATABASE"], task_id)
        payload = request.get_json(silent=True) or {}
        research_objective = sanitize_text(str(payload.get("objective", "")), 500)
        if not research_objective:
            abort(400, "objective is required")
        target_users = sanitize_text(str(payload.get("target_users", "")), 300)
        dimensions = payload.get("dimensions")
        if dimensions is not None and not isinstance(dimensions, list):
            dimensions = None
        result = orchestrator.design_questionnaire(task_id, research_objective, target_users, dimensions)
        attach_questionnaire_share_url(result)
        return jsonify(result), 201

    @app.get("/api/tasks/<task_id>/questionnaires")
    def list_questionnaires(task_id: str):
        """List all questionnaire designs for this task."""
        ensure_task_exists(app.config["DATABASE"], task_id)
        with connect_db(app.config["DATABASE"]) as conn:
            rows = conn.execute(
                "SELECT * FROM questionnaire_designs WHERE task_id = ? ORDER BY created_at DESC",
                (task_id,),
            ).fetchall()
            target_rows = conn.execute(
                """
                SELECT * FROM questionnaire_publish_targets
                WHERE task_id = ?
                ORDER BY created_at DESC
                """,
                (task_id,),
            ).fetchall()
        targets_by_design: dict[str, list[dict[str, Any]]] = {}
        for target in target_rows:
            targets_by_design.setdefault(target["questionnaire_design_id"], []).append(
                questionnaire_publish_target_payload(target)
            )
        return jsonify([
            {
                **dict(row),
                "content": loads(row["content_json"], {}),
                "focus_dimensions": loads(row["focus_dimensions_json"], []),
                "share_url": questionnaire_share_url(row["id"]),
                "share_path": f"/questionnaires/{row['id']}",
                "publish_targets": targets_by_design.get(row["id"], []),
            }
            for row in rows
        ])

    @app.post("/api/questionnaires/<design_id>/publish/feishu")
    def publish_questionnaire_to_feishu(design_id: str):
        """Publish an existing questionnaire design as a Feishu Base form."""
        started = time.monotonic()
        payload = request.get_json(silent=True) or {}
        folder_token = sanitize_text(str(payload.get("folder_token") or os.environ.get("FEISHU_DEFAULT_FOLDER_TOKEN", "")), 240)
        publish_name = sanitize_text(str(payload.get("publish_name", "")), 160)
        with connect_db(app.config["DATABASE"]) as conn:
            row = conn.execute(
                """
                SELECT q.*, t.name AS task_name, t.industry AS task_industry, t.competitors_json
                FROM questionnaire_designs q
                JOIN tasks t ON t.id = q.task_id
                WHERE q.id = ?
                """,
                (design_id,),
            ).fetchone()
            if not row:
                abort(404, "questionnaire not found")
            existing = conn.execute(
                """
                SELECT * FROM questionnaire_publish_targets
                WHERE questionnaire_design_id = ?
                  AND provider = 'feishu'
                  AND status = 'published'
                  AND share_url != ''
                ORDER BY published_at DESC, created_at DESC
                LIMIT 1
                """,
                (design_id,),
            ).fetchone()
        if existing and not payload.get("force"):
            response = questionnaire_publish_target_payload(existing)
            response.update(
                {
                    "publish_status": "published",
                    "reused": True,
                    "local_share_url": questionnaire_share_url(design_id),
                    "local_share_path": f"/questionnaires/{design_id}",
                }
            )
            return jsonify(response), 200

        task_id = row["task_id"]
        design = loads(row["content_json"], {})
        name = publish_name or row["title"]
        publisher = FeishuQuestionnairePublisher(
            cli_path=os.environ.get("FEISHU_CLI_PATH") or os.environ.get("LARK_CLI_PATH"),
            identity=os.environ.get("FEISHU_IDENTITY", "user"),
            default_folder_token=os.environ.get("FEISHU_DEFAULT_FOLDER_TOKEN", ""),
        )

        try:
            result = publisher.publish_questionnaire(design, name, folder_token=folder_token)
        except FeishuPublishError as exc:
            target_id = record_questionnaire_publish_target(
                app.config["DATABASE"],
                design_id=design_id,
                task_id=task_id,
                provider="feishu",
                external_ids={},
                share_url="",
                status="failed",
                error_message=str(exc),
            )
            record_feishu_publish_run(
                app.config["DATABASE"],
                task_id,
                status="failed",
                duration_ms=int((time.monotonic() - started) * 1000),
                output_summary="飞书问卷发布失败。",
                error=str(exc),
                tool_calls=exc.tool_calls,
            )
            return jsonify(
                {
                    "error": "feishu_publish_failed",
                    "message": sanitize_text(str(exc), 500),
                    "publish_status": "failed",
                    "publish_target_id": target_id,
                    "local_share_url": questionnaire_share_url(design_id),
                    "local_share_path": f"/questionnaires/{design_id}",
                }
            ), exc.status_code

        target_id = record_questionnaire_publish_target(
            app.config["DATABASE"],
            design_id=design_id,
            task_id=task_id,
            provider="feishu",
            external_ids=result.external_ids,
            share_url=result.feishu_url,
            status="published",
            error_message="",
        )
        source_id = record_feishu_questionnaire_source(
            app.config["DATABASE"],
            task_id=task_id,
            design_id=design_id,
            title=row["title"],
            share_url=result.feishu_url,
            external_ids=result.external_ids,
        )
        record_feishu_publish_run(
            app.config["DATABASE"],
            task_id,
            status="completed",
            duration_ms=int((time.monotonic() - started) * 1000),
            output_summary=f"已生成飞书问卷「{row['title']}」。",
            tool_calls=result.tool_calls,
        )
        return jsonify(
            {
                "publish_status": "published",
                "publish_target_id": target_id,
                "source_id": source_id,
                "provider": "feishu",
                "feishu_url": result.feishu_url,
                "share_url": result.feishu_url,
                "base_token": result.base_token,
                "table_id": result.table_id,
                "form_id": result.form_id,
                "local_share_url": questionnaire_share_url(design_id),
                "local_share_path": f"/questionnaires/{design_id}",
            }
        ), 201

    @app.post("/api/tasks/<task_id>/interview-guide")
    def design_interview_guide(task_id: str):
        """Generate an interview guide for this task."""
        ensure_task_exists(app.config["DATABASE"], task_id)
        payload = request.get_json(silent=True) or {}
        research_objective = sanitize_text(str(payload.get("objective", "")), 500)
        if not research_objective:
            abort(400, "objective is required")
        target_users = sanitize_text(str(payload.get("target_users", "")), 300)
        interview_count = int(payload.get("interview_count", 5))
        result = orchestrator.design_interview_guide(task_id, research_objective, target_users, max(1, min(interview_count, 20)))
        return jsonify(result), 201

    @app.get("/api/tasks/<task_id>/interview-guides")
    def list_interview_guides(task_id: str):
        """List all interview guides for this task (stored as sources)."""
        ensure_task_exists(app.config["DATABASE"], task_id)
        with connect_db(app.config["DATABASE"]) as conn:
            rows = conn.execute(
                "SELECT * FROM sources WHERE task_id = ? AND source_type = 'interview_guide' ORDER BY collected_at DESC",
                (task_id,),
            ).fetchall()
        return jsonify([dict(row) for row in rows])

    @app.post("/api/tasks/<task_id>/survey-analysis")
    def trigger_survey_analysis(task_id: str):
        """Trigger deep analysis of an already-uploaded survey data file."""
        ensure_task_exists(app.config["DATABASE"], task_id)
        payload = request.get_json(silent=True) or {}
        source_id = sanitize_text(str(payload.get("source_id", "")), 120)
        if not source_id:
            abort(400, "source_id is required")
        result = orchestrator.analyze_survey_responses(task_id, source_id)
        return jsonify(result), 201

    @app.get("/api/tasks/<task_id>/survey-analyses")
    def list_survey_analyses(task_id: str):
        """List all survey analysis results for this task."""
        ensure_task_exists(app.config["DATABASE"], task_id)
        with connect_db(app.config["DATABASE"]) as conn:
            rows = conn.execute(
                "SELECT * FROM survey_analyses WHERE task_id = ? ORDER BY created_at DESC",
                (task_id,),
            ).fetchall()
        return jsonify([
            {
                **dict(row),
                "segments": loads(row["segments_json"], []),
                "findings": loads(row["findings_json"], []),
                "statistics": loads(row["statistics_json"], []),
                "claims_generated": loads(row["claims_generated_json"], []),
            }
            for row in rows
        ])

    @app.post("/api/tasks/<task_id>/interview-analysis")
    def trigger_interview_analysis(task_id: str):
        """Trigger deep analysis of an already-uploaded interview transcript."""
        ensure_task_exists(app.config["DATABASE"], task_id)
        payload = request.get_json(silent=True) or {}
        source_id = sanitize_text(str(payload.get("source_id", "")), 120)
        if not source_id:
            abort(400, "source_id is required")
        interviewee_profile = payload.get("interviewee_profile", {})
        if not isinstance(interviewee_profile, dict):
            interviewee_profile = {}
        result = orchestrator.extract_interview_insights(task_id, source_id, interviewee_profile)
        return jsonify(result), 201

    @app.get("/api/tasks/<task_id>/interview-analyses")
    def list_interview_analyses(task_id: str):
        """List all interview analysis results for this task."""
        ensure_task_exists(app.config["DATABASE"], task_id)
        with connect_db(app.config["DATABASE"]) as conn:
            rows = conn.execute(
                "SELECT * FROM interview_analyses WHERE task_id = ? ORDER BY created_at DESC",
                (task_id,),
            ).fetchall()
        return jsonify([
            {
                **dict(row),
                "key_quotes": loads(row["key_quotes_json"], []),
                "scenarios": loads(row["scenarios_json"], []),
                "pain_points": loads(row["pain_points_json"], []),
                "needs": loads(row["needs_json"], []),
                "claims_generated": loads(row["claims_generated_json"], []),
            }
            for row in rows
        ])

    return app


def safe_upload_filename(filename: str) -> str:
    name = Path(filename or "").name.replace("\\", "_").replace("/", "_")
    name = re.sub(r"[\x00-\x1f\x7f]+", "", name).strip().strip(".")
    if not name:
        name = "uploaded.txt"
    if len(name) > 180:
        suffix = Path(name).suffix
        name = name[: max(1, 180 - len(suffix))].rstrip(". ") + suffix
    secure = secure_filename(name)
    if secure and Path(secure).suffix:
        return secure
    suffix = Path(name).suffix.lower()
    stem = Path(name).stem or "uploaded"
    return f"{stem[:120] or 'uploaded'}{suffix or '.txt'}"


def extract_upload_text(data: bytes, extension: str) -> str:
    if extension == ".pdf":
        ensure_pdf_dependency_paths()
        try:
            from pypdf import PdfReader
        except ImportError:
            abort(500, "pypdf is required for PDF material upload; install requirements.txt")
        try:
            reader = PdfReader(io.BytesIO(data))
            text = "\n\n".join(page.extract_text() or "" for page in reader.pages)
        except Exception as exc:
            abort(400, f"failed to read PDF text: {sanitize_text(str(exc), 160)}")
        if not text.strip():
            abort(400, "PDF does not contain extractable text; upload a text/markdown copy")
        return text
    return data.decode("utf-8", errors="replace")


def init_db(db_path: str | Path) -> None:
    with connect_db(db_path) as conn:
        conn.executescript(SCHEMA_SQL)
        ensure_column(conn, "tasks", "archived", "INTEGER NOT NULL DEFAULT 0")
        ensure_column(conn, "manual_actions", "claim_id", "TEXT NOT NULL DEFAULT ''")
        ensure_column(conn, "claims", "claim_type", "TEXT NOT NULL DEFAULT 'fact'")
        ensure_column(conn, "qa_findings", "finding_type", "TEXT NOT NULL DEFAULT 'general'")
        ensure_column(conn, "qa_findings", "action_hint", "TEXT NOT NULL DEFAULT ''")
        ensure_column(conn, "qa_findings", "meta_json", "TEXT NOT NULL DEFAULT '{}'")
        for column, definition in {
            "provider": "TEXT NOT NULL DEFAULT ''",
            "search_log_id": "TEXT NOT NULL DEFAULT ''",
            "search_query": "TEXT NOT NULL DEFAULT ''",
            "auth_info": "TEXT NOT NULL DEFAULT ''",
            "auth_level": "INTEGER NOT NULL DEFAULT 0",
            "time_cost_ms": "INTEGER NOT NULL DEFAULT 0",
            "competitor_name": "TEXT NOT NULL DEFAULT ''",
            "module": "TEXT NOT NULL DEFAULT ''",
            "relevance_score": "INTEGER NOT NULL DEFAULT 0",
            "source_role": "TEXT NOT NULL DEFAULT ''",
            "raw_content_status": "TEXT NOT NULL DEFAULT 'summary_only'",
        }.items():
            ensure_column(conn, "sources", column, definition)


def ensure_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def recover_interrupted_tasks(db_path: str | Path) -> None:
    with connect_db(db_path) as conn:
        conn.execute(
            """
            UPDATE tasks
            SET status = 'completed'
            WHERE completed_at != ''
              AND status NOT IN ('completed', 'failed', 'stopped')
              AND id IN (SELECT task_id FROM reports)
            """
        )
        conn.execute(
            """
            UPDATE tasks
            SET status = 'failed', completed_at = ?
            WHERE completed_at = ''
              AND status NOT IN ('completed', 'failed', 'stopped', 'waiting_materials')
            """,
            (utc_now_iso(),),
        )


def connect_db(db_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def launch_workflow(orchestrator: Orchestrator, task_id: str, async_mode: bool) -> None:
    def runner() -> None:
        try:
            orchestrator.run_initial_workflow(task_id)
        except Exception as exc:
            orchestrator.fail_workflow(task_id, exc)

    if async_mode:
        threading.Thread(target=runner, name=f"task-workflow-{task_id[:8]}", daemon=True).start()
    else:
        runner()


def normalize_websites(websites: list[str], expected_count: int) -> list[str]:
    normalized = websites[:expected_count]
    while len(normalized) < expected_count:
        normalized.append("")
    return normalized


def split_research_items(value: str) -> list[str]:
    return [
        item.strip()
        for item in re.split(r"[\n,，、;；]+|(?:以及|和|及)", value or "")
        if item and item.strip()
    ]


def research_context_from_payload(payload: dict[str, Any]) -> dict[str, Any]:
    raw_competitors = payload.get("competitors", [])
    if isinstance(raw_competitors, str):
        competitors = split_research_items(raw_competitors)
    elif isinstance(raw_competitors, list):
        competitors = [sanitize_text(str(item), 80) for item in raw_competitors if str(item).strip()]
    else:
        competitors = []
    competitors = competitors[:8] or ["待补充竞品"]
    raw_focus = payload.get("focus_areas") or payload.get("dimensions") or []
    if isinstance(raw_focus, str):
        focus_areas = split_research_items(raw_focus)
    elif isinstance(raw_focus, list):
        focus_areas = [sanitize_text(str(item), 60) for item in raw_focus if str(item).strip()]
    else:
        focus_areas = []
    if not focus_areas:
        focus_areas = ["市场与赛道", "竞品分层", "核心能力", "商业模式与定价", "增长与分发", "用户与场景", "SWOT与壁垒", "机会建议"]
    industry = sanitize_text(str(payload.get("industry", "")), 80)
    notes = sanitize_text(str(payload.get("notes", "")), 400)
    return {
        "id": "draft_research_context",
        "industry": infer_industry(industry or "待识别行业", competitors, notes),
        "competitors": competitors,
        "focus_areas": focus_areas[:12],
        "notes": notes,
    }


def fallback_questionnaire_design(
    task: dict[str, Any], research_objective: str, target_users: str = "", dimensions: list[str] | None = None
) -> dict[str, Any]:
    competitors = [str(item) for item in task.get("competitors", []) if str(item).strip()]
    competitor_label = "、".join(competitors) or "目标竞品"
    dims = dimensions or task.get("focus_areas") or ["市场与赛道", "竞品分层", "核心能力", "商业模式与定价", "增长与分发", "用户与场景", "SWOT与壁垒", "机会建议"]
    dim_options = [str(item) for item in dims[:6]]
    return {
        "title": f"{competitor_label} 用户调研问卷",
        "description": (
            f"围绕“{sanitize_text(research_objective, 160)}”收集用户背景、使用习惯、竞品对比、满意度和需求痛点。"
        ),
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
                    {
                        "id": "Q7",
                        "type": "open_ended",
                        "question_key": "switch_trigger",
                        "question_text": "什么情况下你会考虑更换到另一个竞品？",
                        "required": False,
                    },
                ],
            },
        ],
        "estimated_time_minutes": 6,
        "recommended_channels": ["线上问卷", "用户群", "访谈前筛选"],
        "target_users": target_users,
    }


def fallback_interview_guide(
    task: dict[str, Any], research_objective: str, target_users: str = "", interview_count: int = 5
) -> dict[str, Any]:
    competitors = "、".join(task.get("competitors", [])) or "目标竞品"
    return {
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
                    {"id": "Q7", "text": "如果只能保留三个能力，你最希望产品做好哪三个？", "probe": "追问排序原因和不可接受的底线。"},
                ],
            },
            {
                "phase": "总结",
                "duration_minutes": 10,
                "goals": ["确认判断", "收集开放建议"],
                "questions": [
                    {"id": "Q8", "text": "如果向同事推荐或不推荐这些产品，你会怎么说？", "probe": "追问推荐对象和前提条件。"},
                    {"id": "Q9", "text": "还有哪些我们没有问到、但会影响你选择的因素？", "probe": "追问合规、安全、协作、迁移成本等隐性因素。"},
                ],
            },
        ],
        "notes_for_interviewer": f"建议访谈 {interview_count} 人以上；只记录脱敏原话，关键结论必须回链到问题编号和原文证据。",
        "dimension_coverage": {
            "用户画像": ["Q1", "Q2"],
            "功能对比": ["Q3", "Q4", "Q7"],
            "定价": ["Q5"],
            "用户评价": ["Q6", "Q8"],
            "SWOT": ["Q4", "Q8", "Q9"],
        },
    }


def questionnaire_share_url(design_id: str) -> str:
    return f"{request.host_url.rstrip('/')}/questionnaires/{design_id}"


def attach_questionnaire_share_url(result: dict[str, Any]) -> None:
    design_id = result.get("design_id")
    if not design_id:
        result["share_url"] = ""
        result["share_path"] = ""
        return
    result["share_url"] = questionnaire_share_url(str(design_id))
    result["share_path"] = f"/questionnaires/{design_id}"


def questionnaire_publish_target_payload(row: sqlite3.Row) -> dict[str, Any]:
    external_ids = loads(row["external_ids_json"], {})
    return {
        "publish_target_id": row["id"],
        "questionnaire_design_id": row["questionnaire_design_id"],
        "provider": row["provider"],
        "share_url": row["share_url"],
        "feishu_url": row["share_url"] if row["provider"] == "feishu" else "",
        "publish_status": row["status"],
        "error_message": row["error_message"],
        "published_at": row["published_at"],
        "base_token": external_ids.get("base_token", ""),
        "table_id": external_ids.get("table_id", ""),
        "form_id": external_ids.get("form_id", ""),
    }


def record_questionnaire_publish_target(
    db_path: str | Path,
    design_id: str,
    task_id: str,
    provider: str,
    external_ids: dict[str, Any],
    share_url: str,
    status: str,
    error_message: str,
) -> str:
    now = utc_now_iso()
    target_id = uuid.uuid4().hex
    with connect_db(db_path) as conn:
        conn.execute(
            """
            INSERT INTO questionnaire_publish_targets
            (id, questionnaire_design_id, task_id, provider, external_ids_json, share_url,
             status, error_message, published_at, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                target_id,
                design_id,
                task_id,
                sanitize_text(provider, 40),
                dumps(sanitize_payload(external_ids)),
                sanitize_text(share_url, 800),
                sanitize_text(status, 40),
                sanitize_text(error_message, 500),
                now if status == "published" else "",
                now,
            ),
        )
    return target_id


def record_feishu_questionnaire_source(
    db_path: str | Path,
    task_id: str,
    design_id: str,
    title: str,
    share_url: str,
    external_ids: dict[str, Any],
) -> str:
    now = utc_now_iso()
    source_id = f"{task_id[:8]}_feishu_q_{uuid.uuid4().hex[:8]}"
    external_summary = {
        "design_id": design_id,
        "base_token": external_ids.get("base_token", ""),
        "table_id": external_ids.get("table_id", ""),
        "form_id": external_ids.get("form_id", ""),
    }
    with connect_db(db_path) as conn:
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
                "feishu_questionnaire",
                sanitize_text(title or "飞书用户调研问卷", 240),
                sanitize_text(share_url, 800),
                "飞书 Base Form",
                now,
                now,
                "generated",
                dumps(external_summary),
                "[]",
                "feishu_cli",
            ),
        )
    return source_id


def record_feishu_publish_run(
    db_path: str | Path,
    task_id: str,
    status: str,
    duration_ms: int,
    output_summary: str,
    error: str = "",
    tool_calls: list[dict[str, Any]] | None = None,
) -> None:
    now = utc_now_iso()
    safe_output = sanitize_text(output_summary, 500)
    with connect_db(db_path) as conn:
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
                "访谈/问卷整理 Agent",
                "发布问卷到飞书 Base Form",
                safe_output,
                sanitize_text(status, 40),
                now,
                now,
                max(1, int(duration_ms)),
                sanitize_text(error, 300),
                0,
                1,
                max(1, len(safe_output) // 4),
                dumps(sanitize_payload(tool_calls or [])),
                "error" if status == "failed" else "info",
                0,
                "",
                "feishu_cli",
            ),
        )


def infer_industry(industry: str, competitors: list[str], notes: str = "") -> str:
    value = (industry or "").strip()
    normalized = value.casefold().strip(" :：")
    should_infer = normalized in {"", "待识别行业", "ai", "人工智能", "竞品", "产品", "行业"}
    haystack = " ".join([value, notes, *competitors]).casefold()
    for keywords, label in INDUSTRY_KEYWORDS:
        if any(keyword.casefold() in haystack for keyword in keywords):
            if should_infer or normalized in {"ai", "人工智能", "汽车", "新能源", "新能源汽车"}:
                return label
    return value or "待识别行业"


def ensure_task_exists(db_path: str | Path, task_id: str) -> None:
    with connect_db(db_path) as conn:
        row = conn.execute("SELECT id FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if not row:
        abort(404, "task not found")


def build_qa_finding_details(conn: sqlite3.Connection, task_id: str) -> list[dict[str, Any]]:
    task = conn.execute("SELECT industry, competitors_json FROM tasks WHERE id = ?", (task_id,)).fetchone()
    industry = task["industry"] if task else ""
    competitors = loads(task["competitors_json"], []) if task else []
    finding_rows = conn.execute(
        "SELECT * FROM qa_findings WHERE task_id = ? ORDER BY created_at, rowid",
        (task_id,),
    ).fetchall()
    claim_rows = conn.execute(
        "SELECT * FROM claims WHERE task_id = ?",
        (task_id,),
    ).fetchall()
    source_rows = conn.execute(
        "SELECT * FROM sources WHERE task_id = ? ORDER BY collected_at, rowid",
        (task_id,),
    ).fetchall()
    claim_by_id = {row["id"]: row for row in claim_rows}
    source_by_id = {row["id"]: row for row in source_rows}
    ref_map = {row["id"]: f"S{index + 1}" for index, row in enumerate(source_rows)}

    def infer_competitor(claim_text: str, source_ids: list[str], meta: dict[str, Any]) -> str:
        explicit = meta.get("affected_competitor") or meta.get("competitor")
        if explicit:
            return sanitize_text(str(explicit), 120)
        affected = meta.get("affected_competitors")
        if isinstance(affected, list) and affected:
            return sanitize_text(str(affected[0]), 120)
        lowered = claim_text.casefold()
        for name in competitors:
            if str(name).casefold() in lowered:
                return str(name)
        for source_id in source_ids:
            source = source_by_id.get(source_id)
            if source and source["competitor_name"]:
                return source["competitor_name"]
        return ""

    def industry_material_terms() -> dict[str, list[str] | str]:
        haystack = f"{industry} {' '.join(str(name) for name in competitors)}".casefold()
        if re.search(r"chatgpt|deepseek|openai|大模型|人工智能|智能助手|\bai\b|llm", haystack, flags=re.I):
            return {
                "price": "官方定价页、API pricing 页面、会员/订阅套餐页或带采集日期的价格材料",
                "official": "自有官网、官方文档、官方博客、产品页或开发者文档",
                "proof_items": ["官方产品/功能页", "官方定价或套餐页", "开发者/API 文档", "可信第三方评价或新闻"],
                "price_label": "订阅/API 价格",
            }
        if re.search(r"汽车|新能源车|电动车|智能车|车型|比亚迪|小鹏|蔚来|理想|特斯拉", haystack, flags=re.I):
            return {
                "price": "官方车型价格页、车型配置表、购车权益、金融方案或带日期的经销/权威价格材料",
                "official": "品牌官网、车型页、配置表、上市发布稿、交付/销量公告或权威测评",
                "proof_items": ["车型指导价和配置表", "续航/电池/智驾配置", "交付/销量或渠道材料", "安全/质量/用户口碑材料"],
                "price_label": "车型/配置价格",
            }
        if re.search(r"光伏|组件|电池片|硅片|硅料|太阳能|储能", haystack, flags=re.I):
            return {
                "price": "组件、电池片、硅片、硅料报价，或带采集日期的招标/行业价格材料",
                "official": "公司官网、产品规格书、投资者公告、出货/产能披露或权威行业报价",
                "proof_items": ["组件/电池片规格书", "组件/材料价格或招标材料", "产能/出货公告", "效率/认证/可靠性材料"],
                "price_label": "组件/材料价格",
            }
        if re.search(r"煤炭|煤矿|煤价|动力煤|焦煤|热值|长协", haystack, flags=re.I):
            return {
                "price": "煤种、热值、产地、长协/现货、港口价或运费口径的带日期价格材料",
                "official": "公司公告、年报、产能披露、交易中心/行业报价、监管或安全环保材料",
                "proof_items": ["煤种/热值/产地说明", "长协/现货/港口价材料", "产能/销量公告", "运输成本/安全环保/政策材料"],
                "price_label": "煤种/热值价格",
            }
        return {
            "price": "官方价格页、报价单、规格/型号/套餐页或带采集日期的权威价格材料",
            "official": "自有官网、官方文档、产品规格页、公告、权威第三方来源或用户提供的一手材料",
            "proof_items": ["官方产品/服务页", "价格/报价/型号材料", "规格、销量/出货或客户案例", "评价、新闻、政策或风险材料"],
            "price_label": "行业价格口径",
        }

    material_terms = industry_material_terms()

    def default_material(finding_type: str, competitor: str, section: str) -> str:
        name = competitor or "对应竞品"
        if finding_type == "source_ownership_mismatch":
            return f"需要补充 {name} 的{material_terms['official']}，或明确命中该竞品的第三方来源。"
        if finding_type in {"pricing_missing_official", "missing_date"} or section == "pricing_model":
            return f"需要补充 {name} 的{material_terms['price']}。"
        if finding_type == "collection_log_content":
            return "需要将采集日志改写为基于证据的分析结论，不能把搜索过程原文写进报告。"
        if finding_type == "duplicate_claim":
            return "需要合并重复结论或保留信息量更高的一条。"
        return "需要补充可追溯来源、人工确认或重做该模块分析。"

    def supplement_guidance(
        finding_type: str,
        competitor: str,
        section: str,
        claim_text: str,
        missing_material: str,
        current_sources: list[dict[str, Any]],
        suggested_queries: list[str],
    ) -> dict[str, Any]:
        name = competitor or "对应竞品"
        section_name = {
            "feature_tree": "功能/产品结论",
            "pricing_model": str(material_terms.get("price_label", "价格结论")),
            "reviews": "用户评价/口碑结论",
            "user_persona": "用户画像结论",
            "swot": "SWOT 结论",
        }.get(section, "该结论")
        if finding_type == "source_ownership_mismatch":
            issue = "当前来源可能没有归属于这家竞品，不能直接支撑结论。"
        elif finding_type in {"pricing_missing_official", "missing_date"} or section == "pricing_model":
            issue = "价格或时间敏感结论需要官方/权威材料和明确日期。"
        elif finding_type == "review_sample_bias":
            issue = "评价样本可能偏少或平台单一，需要补充更多评价来源或说明样本限制。"
        elif finding_type == "overclaim":
            issue = "结论表达偏确定，需要补充更强证据或改成保守表述。"
        else:
            issue = "当前证据链不足，需要补来源、补解释或人工确认。"
        proof_items = list(material_terms.get("proof_items", []))
        current_source_labels = [
            f"{source.get('ref', source.get('id', ''))}：{source.get('title', '未命名来源')}"
            for source in current_sources[:4]
        ]
        return {
            "title": f"建议补充 {name} 的{section_name}证据",
            "issue": issue,
            "what_to_add": [missing_material] + proof_items[:3],
            "accepted_formats": [
                "可打开的网页链接，并说明网页中哪句话能证明结论",
                "截图、PDF、表格或上传材料中的关键文字摘要",
                "你掌握的一手信息、访谈/销售口径，并说明来源和日期",
                "如果结论无误，可写清为什么现有来源足够以及是否需要降级为不确定表述",
            ],
            "current_sources": current_source_labels,
            "suggested_queries": suggested_queries[:4],
            "fill_template": (
                "来源链接/材料名称：\n"
                f"材料类型：{section_name}\n"
                "来源日期或采集日期：\n"
                "这份材料能证明：\n"
                "对应竞品：\n"
                "仍不确定或需要保守表述的地方："
            ),
            "claim_preview": sanitize_text(claim_text, 260),
        }

    details: list[dict[str, Any]] = []
    for row in finding_rows:
        item = dict(row)
        meta = loads(item.get("meta_json", "{}"), {})
        if not isinstance(meta, dict):
            meta = {}
        claim = claim_by_id.get(item["claim_id"])
        source_ids = loads(claim["source_ids"], []) if claim else []
        claim_text = claim["content"] if claim else ""
        competitor = infer_competitor(claim_text, source_ids, meta)
        current_sources = []
        failed_sources = []
        for source_id in source_ids:
            source = source_by_id.get(source_id)
            if not source:
                continue
            source_item = {
                "id": source_id,
                "ref": ref_map.get(source_id, source_id),
                "title": source["title"],
                "url_or_path": source["url_or_path"],
                "excerpt": source["excerpt"],
                "competitor": source["competitor_name"],
                "module": source["module"],
                "role": source["source_role"],
                "raw_content_status": source["raw_content_status"],
                "relevance_score": source["relevance_score"],
                "credibility": source["credibility"],
            }
            current_sources.append(source_item)
            if competitor and source["competitor_name"] and source["competitor_name"].casefold() != competitor.casefold():
                failed_sources.append(source_item)
        finding_type = item.get("finding_type") or meta.get("finding_type") or "general"
        section = claim["section"] if claim else ""
        missing_material = meta.get("missing_material") or default_material(finding_type, competitor, section)
        suggested_queries = meta.get("suggested_queries") if isinstance(meta.get("suggested_queries"), list) else []
        manual_verdict = str(meta.get("manual_verdict") or "")
        manual_review_state = str(meta.get("manual_review_state") or "")
        fix_status = item.get("fix_status") or ""
        if not manual_review_state:
            if manual_verdict == "confirmed":
                manual_review_state = "manual_confirmed"
            elif fix_status == "fixed" and manual_verdict in {"supplemented_source", "revise_claim"}:
                manual_review_state = "system_rechecked"
            elif manual_verdict in {"supplemented_source", "revise_claim"} and fix_status in {"open", "manual_pending"}:
                manual_review_state = "needs_more_input" if item.get("recheck_result") else "awaiting_recheck"
            elif manual_verdict == "disputed":
                manual_review_state = "needs_more_input"
        item.update(
            {
                "finding_type": finding_type,
                "action_hint": item.get("action_hint") or meta.get("action_hint") or default_material(finding_type, competitor, section),
                "meta": meta,
                "claim_section": section,
                "claim_content": claim_text,
                "claim_status": claim["status"] if claim else "",
                "claim_confidence": claim["confidence"] if claim else None,
                "claim_needs_review": bool(claim["needs_review"]) if claim else False,
                "affected_competitor": competitor,
                "source_refs": [ref_map[source_id] for source_id in source_ids if source_id in ref_map],
                "current_sources": current_sources,
                "failed_sources": failed_sources,
                "missing_material": missing_material,
                "suggested_queries": suggested_queries,
                "supplement_guidance": supplement_guidance(finding_type, competitor, section, claim_text, missing_material, current_sources, suggested_queries),
                "repair_action": meta.get("repair_action") or ("auto_collect" if finding_type in {"source_ownership_mismatch", "pricing_missing_official", "missing_date"} else "manual_supplement"),
                "can_auto_repair": bool(meta.get("can_auto_repair", False)),
                "needs_manual_review": bool(meta.get("needs_manual_review", item.get("fix_status") in {"open", "manual_pending"})),
                "manual_review_state": manual_review_state,
                "can_recheck_without_input": item.get("fix_status") != "open" or manual_review_state in {"awaiting_recheck", "needs_more_input"},
            }
        )
        details.append(item)
    return details


def get_task_payload(db_path: str | Path, task_id: str) -> dict[str, Any]:
    with connect_db(db_path) as conn:
        task = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if not task:
            abort(404, "task not found")
        competitors = conn.execute(
            "SELECT * FROM competitors WHERE task_id = ? ORDER BY rowid",
            (task_id,),
        ).fetchall()
        qa_findings = build_qa_finding_details(conn, task_id)
        manual_actions = conn.execute(
            "SELECT * FROM manual_actions WHERE task_id = ? ORDER BY created_at, rowid",
            (task_id,),
        ).fetchall()
    payload = serialize_task(task)
    payload["competitors"] = [serialize_competitor(row) for row in competitors]
    payload["qa_findings"] = qa_findings
    payload["manual_actions"] = [dict(row) for row in manual_actions]
    report = latest_report(db_path, task_id)
    payload["latest_report"] = report
    return payload


def build_knowledge_payload(db_path: str | Path, task_id: str) -> dict[str, Any]:
    table_map = {
        "feature_tree": ("feature_items", "feature_tree"),
        "pricing_model": ("pricing_items", "pricing_model"),
        "user_persona": ("persona_items", "user_persona"),
        "reviews": ("review_items", "reviews"),
        "swot": ("swot_items", "swot"),
    }
    with connect_db(db_path) as conn:
        links = conn.execute(
            """
            SELECT l.*, s.title AS source_title, s.url_or_path
            FROM evidence_links l
            JOIN sources s ON s.id = l.source_id
            WHERE l.task_id = ?
            ORDER BY l.created_at, l.rowid
            """,
            (task_id,),
        ).fetchall()
        grouped_links: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for link in links:
            item = dict(link)
            key = (item["entity_type"], item["entity_id"])
            grouped_links.setdefault(key, []).append(
                {
                    "source_id": item["source_id"],
                    "chunk_id": item["chunk_id"],
                    "quote": item["quote"],
                    "source_title": item["source_title"],
                    "url_or_path": item["url_or_path"],
                }
            )
        result: dict[str, Any] = {}
        for key, (table, entity_type) in table_map.items():
            rows = conn.execute(
                f"SELECT * FROM {table} WHERE task_id = ? ORDER BY created_at, rowid",
                (task_id,),
            ).fetchall()
            result[key] = []
            for row in rows:
                payload = dict(row)
                payload["needs_review"] = bool(payload.get("needs_review", 0))
                payload["source_refs"] = grouped_links.get((entity_type, payload["id"]), [])
                result[key].append(payload)
        claim_rows = conn.execute(
            "SELECT * FROM claims WHERE task_id = ? ORDER BY created_at, rowid",
            (task_id,),
        ).fetchall()
        claims = []
        for row in claim_rows:
            claim = serialize_claim(row)
            refs = grouped_links.get(("claims", claim["id"]), [])
            if not refs:
                refs = [{"source_id": source_id, "chunk_id": "", "quote": ""} for source_id in claim["source_ids"]]
            claim["source_refs"] = refs
            claims.append(claim)
        result["claims"] = claims

        # Include user research analysis results
        survey_rows = conn.execute(
            "SELECT * FROM survey_analyses WHERE task_id = ? ORDER BY created_at DESC",
            (task_id,),
        ).fetchall()
        result["survey_analyses"] = [
            {
                **dict(row),
                "segments": loads(row["segments_json"], []),
                "findings": loads(row["findings_json"], []),
                "statistics": loads(row["statistics_json"], []),
                "claims_generated": loads(row["claims_generated_json"], []),
            }
            for row in survey_rows
        ]

        interview_rows = conn.execute(
            "SELECT * FROM interview_analyses WHERE task_id = ? ORDER BY created_at DESC",
            (task_id,),
        ).fetchall()
        result["interview_analyses"] = [
            {
                **dict(row),
                "key_quotes": loads(row["key_quotes_json"], []),
                "scenarios": loads(row["scenarios_json"], []),
                "pain_points": loads(row["pain_points_json"], []),
                "needs": loads(row["needs_json"], []),
                "claims_generated": loads(row["claims_generated_json"], []),
            }
            for row in interview_rows
        ]

        return result


def latest_report(db_path: str | Path, task_id: str) -> dict[str, Any] | None:
    with connect_db(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM reports WHERE task_id = ? ORDER BY version DESC LIMIT 1",
            (task_id,),
        ).fetchone()
    if not row:
        return None
    result = dict(row)
    result["content"] = loads(result.pop("content_json"), {})
    result["citation_map"] = loads(result["citation_map"], {})
    return result


def build_logs_zip(task_id: str, logs: list[dict[str, Any]]):
    buffer = io.BytesIO()
    logs = sanitize_payload(logs)
    fieldnames = [
        "id",
        "task_id",
        "agent_name",
        "status",
        "severity",
        "has_rework",
        "started_at",
        "ended_at",
        "duration_ms",
        "retry_count",
        "token_input",
        "token_output",
        "model_provider",
        "fallback_reason",
        "input_summary",
        "output_summary",
        "error",
        "tool_calls",
    ]
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        jsonl = "\n".join(dumps(log) for log in logs)
        archive.writestr("agent_runs.jsonl", jsonl + ("\n" if jsonl else ""))
        csv_buffer = io.StringIO()
        writer = csv.DictWriter(csv_buffer, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for log in logs:
            row = dict(log)
            row["tool_calls"] = dumps(row.get("tool_calls", []))
            writer.writerow(row)
        archive.writestr("agent_runs.csv", csv_buffer.getvalue())
    buffer.seek(0)
    return send_file(
        buffer,
        mimetype="application/zip",
        as_attachment=True,
        download_name=f"{task_id}_agent_logs.zip",
    )


def build_report_pdf(task_id: str, report: dict[str, Any]):
    ensure_pdf_dependency_paths()
    try:
        from report_pdf import render_competitive_report_pdf

        buffer = render_competitive_report_pdf(task_id, report)
        return send_file(
            buffer,
            mimetype="application/pdf",
            as_attachment=True,
            download_name=f"{task_id}_competitive_report.pdf",
        )
    except ImportError:
        pass

    try:
        from reportlab.lib import colors
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
        from reportlab.lib.units import mm
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.cidfonts import UnicodeCIDFont
        from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
    except ImportError:
        ensure_pdf_dependency_paths()
        try:
            from reportlab.lib import colors
            from reportlab.lib.pagesizes import A4
            from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
            from reportlab.lib.units import mm
            from reportlab.pdfbase import pdfmetrics
            from reportlab.pdfbase.cidfonts import UnicodeCIDFont
            from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
        except ImportError:
            abort(500, "reportlab is required for PDF export; install requirements.txt")

    pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
    content = report.get("content", {})
    dimension_profile = content.get("dimension_profile", {})
    price_label = dimension_profile.get("price_metric_label", "价格口径")
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        rightMargin=15 * mm,
        leftMargin=15 * mm,
        topMargin=15 * mm,
        bottomMargin=15 * mm,
        title=content.get("title", "竞品分析报告"),
    )
    styles = getSampleStyleSheet()
    base = ParagraphStyle("CN", parent=styles["BodyText"], fontName="STSong-Light", fontSize=9.5, leading=14)
    title_style = ParagraphStyle("CNTitle", parent=styles["Title"], fontName="STSong-Light", fontSize=22, leading=28, textColor=colors.HexColor("#073b3a"))
    h2 = ParagraphStyle("CNH2", parent=styles["Heading2"], fontName="STSong-Light", fontSize=14, leading=18, textColor=colors.HexColor("#075e54"))
    small = ParagraphStyle("CNSmall", parent=base, fontSize=8, leading=11, textColor=colors.HexColor("#50615d"))

    def p(text: Any, style: ParagraphStyle = base):
        return Paragraph(sanitize_text(str(text or ""), 1600), style)

    def table(rows: list[list[Any]], widths: list[float] | None = None):
        safe_rows = [[p(cell, small) for cell in row] for row in rows]
        result = Table(safe_rows, colWidths=widths, repeatRows=1)
        result.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#075e54")),
                    ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                    ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#d9e3df")),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f4f8f6")]),
                    ("LEFTPADDING", (0, 0), (-1, -1), 5),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 5),
                    ("TOPPADDING", (0, 0), (-1, -1), 4),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                ]
            )
        )
        return result

    story = [
        p(content.get("title", "竞品分析报告"), title_style),
        p(f"生成时间：{format_date_for_pdf(report.get('generated_at', ''))}    质检状态：{report.get('qa_status', '')}    可信度：{round(float(report.get('confidence_score', 0)) * 100)}%", small),
        Spacer(1, 8),
        p("执行摘要", h2),
        p(content.get("summary", "")),
        Spacer(1, 8),
    ]

    cards = content.get("executive_cards", [])
    if cards:
        card_cells = [
            f"{card.get('title', '')}\n{card.get('status', '')}\n{card.get('verdict', '')}\n证据：{' '.join(card.get('evidence_refs', [])) or '待补证'}"
            for card in cards[:4]
        ]
        while len(card_cells) < 4:
            card_cells.append("待补证")
        story.append(table([["核心结论", "核心结论"], [card_cells[0], card_cells[1]], [card_cells[2], card_cells[3]]], [88 * mm, 88 * mm]))
        story.append(Spacer(1, 8))

    methodology = content.get("methodology", {})
    story.extend([p("方法与来源可靠性", h2), p(methodology.get("scope", "")), p(methodology.get("source_policy", ""), small)])
    reliability = content.get("source_reliability", [])
    if reliability:
        story.append(table([["来源类型", "数量", "正文抓取", "摘要线索", "高可信"]] + [[row.get("category", ""), row.get("count", 0), row.get("fetched", 0), row.get("summary_only", 0), row.get("high", 0)] for row in reliability], [45 * mm, 20 * mm, 28 * mm, 28 * mm, 24 * mm]))
    story.append(PageBreak())

    story.append(p("执行摘要矩阵：8 维评分", h2))
    score_dimensions = content.get("score_dimensions", [])
    if score_dimensions:
        story.append(
            table(
                [["竞品", "维度", "评分", "口径", "证据"]]
                + [
                    [
                        row.get("competitor", ""),
                        row.get("dimension", ""),
                        f"{row.get('score', 0)}/{row.get('max_score', 5)}",
                        row.get("rationale", ""),
                        " ".join(row.get("evidence_refs", [])),
                    ]
                    for row in score_dimensions
                ],
                [26 * mm, 32 * mm, 18 * mm, 72 * mm, 26 * mm],
            )
        )
    positioning = content.get("positioning_map", {})
    if positioning.get("points"):
        story.append(Spacer(1, 6))
        story.append(
            table(
                [["竞品", positioning.get("x_axis", "X轴"), positioning.get("y_axis", "Y轴"), "定位"]]
                + [[row.get("competitor", ""), row.get("x", ""), row.get("y", ""), row.get("label", "")] for row in positioning.get("points", [])],
                [30 * mm, 48 * mm, 48 * mm, 46 * mm],
            )
        )
        story.append(p(positioning.get("interpretation", ""), small))
    story.append(Spacer(1, 8))

    story.append(p("功能评分热力图", h2))
    feature_scores = content.get("feature_scores", [])
    if feature_scores:
        story.append(table([["竞品", "维度", "得分", "证据"]] + [[row.get("competitor", ""), row.get("dimension", ""), f"{row.get('score', 0)}/{row.get('max_score', 5)}", " ".join(row.get("evidence_refs", []))] for row in feature_scores], [32 * mm, 38 * mm, 20 * mm, 80 * mm]))
    story.append(Spacer(1, 8))

    story.append(p("价格与成本口径", h2))
    pricing = content.get("pricing_comparison", [])
    if pricing:
        story.append(table([["竞品", price_label, "依据", "证据"]] + [[row.get("competitor", ""), row.get("price_text", ""), "；".join([row.get("basis", ""), row.get("calculation_note", "")]).strip("；"), " ".join(row.get("evidence_refs", []))] for row in pricing], [28 * mm, 45 * mm, 72 * mm, 28 * mm]))
    api_cost = content.get("api_cost_data", {})
    if api_cost.get("rows"):
        story.append(Spacer(1, 6))
        story.append(p(f"成本指数公式：{api_cost.get('formula', '')}", small))
        story.append(
            table(
                [["竞品", "模型/套餐", "输出价", "成本指数", "倍数/备注"]]
                + [
                    [
                        row.get("competitor", ""),
                        row.get("plan_name", ""),
                        f"{row.get('output_amount', '')} {row.get('currency', '')}/{row.get('unit', '')}",
                        row.get("cost_index", ""),
                        row.get("note", "") or row.get("multiplier_vs_baseline", ""),
                    ]
                    for row in api_cost.get("rows", [])
                ],
                [28 * mm, 38 * mm, 40 * mm, 25 * mm, 42 * mm],
            )
        )
        story.append(p(api_cost.get("caveat", ""), small))
    pricing_facts = content.get("pricing_facts", [])
    if pricing_facts:
        story.append(Spacer(1, 6))
        story.append(table([["竞品", "产品/套餐/型号", "类型", "金额", "来源"]] + [[row.get("competitor_name", ""), row.get("plan_name", ""), row.get("price_type", ""), f"{row.get('amount', '')} {row.get('currency', '')}/{row.get('unit', '')}", row.get("evidence_ref", "")] for row in pricing_facts[:12]], [28 * mm, 42 * mm, 24 * mm, 50 * mm, 30 * mm]))
    story.append(Spacer(1, 8))

    story.append(p("场景化决策矩阵与风险控制", h2))
    decision = content.get("decision_matrix", [])
    if decision:
        story.append(table([["场景", "竞品", "优先级", "理由", "动作"]] + [[row.get("scenario", ""), row.get("competitor", ""), row.get("priority", ""), row.get("reason", ""), row.get("next_action", "")] for row in decision], [28 * mm, 28 * mm, 18 * mm, 62 * mm, 38 * mm]))
    scenarios = content.get("scenario_recommendations", [])
    if scenarios:
        story.append(Spacer(1, 6))
        story.append(
            table(
                [["场景", "推荐", "置信度", "理由", "下一步"]]
                + [[row.get("scenario", ""), row.get("recommended", ""), row.get("confidence", ""), row.get("reason", ""), row.get("next_action", "")] for row in scenarios],
                [38 * mm, 26 * mm, 20 * mm, 58 * mm, 34 * mm],
            )
        )
    risks = content.get("risk_controls", [])
    if risks:
        story.append(Spacer(1, 6))
        story.append(table([["风险", "影响", "控制动作", "负责人"]] + [[row.get("risk", ""), row.get("impact", ""), row.get("control", ""), row.get("owner", "")] for row in risks], [40 * mm, 52 * mm, 58 * mm, 25 * mm]))

    insights = content.get("key_insights", [])
    if insights:
        story.append(PageBreak())
        story.append(p("关键洞察", h2))
        story.append(
            table(
                [["洞察", "说明", "证据"]]
                + [[row.get("title", ""), row.get("insight", ""), " ".join(row.get("evidence_refs", []))] for row in insights],
                [38 * mm, 104 * mm, 32 * mm],
            )
        )
    fact_notes = content.get("fact_notes", [])
    if fact_notes:
        story.append(Spacer(1, 8))
        story.append(p("事实备注与口径", h2))
        story.append(
            table(
                [["主题", "备注", "证据"]]
                + [[row.get("topic", ""), row.get("note", ""), " ".join(row.get("evidence_refs", []))] for row in fact_notes],
                [42 * mm, 104 * mm, 28 * mm],
            )
        )

    story.append(PageBreak())
    story.append(p("报告正文", h2))
    for section in content.get("sections", []):
        story.append(p(section.get("title", ""), h2))
        story.append(p(section.get("body", "")))
        if section.get("table"):
            story.append(table(section["table"]))
        for claim in section.get("claims", []):
            refs = " ".join(claim.get("source_refs", []))
            story.append(p(f"{claim.get('content', '')} {refs}", small))
        story.append(Spacer(1, 6))

    story.append(PageBreak())
    story.append(p("附录：来源列表", h2))
    catalog = content.get("source_catalog", [])
    if catalog:
        story.append(table([["编号", "标题", "类型", "竞品", "模块", "URL"]] + [[row.get("ref", ""), row.get("title", ""), row.get("type", ""), row.get("competitor", ""), row.get("module", ""), row.get("url_or_path", "")] for row in catalog], [16 * mm, 48 * mm, 25 * mm, 25 * mm, 25 * mm, 45 * mm]))

    def footer(canvas, document):
        canvas.saveState()
        canvas.setFont("STSong-Light", 8)
        canvas.setFillColor(colors.HexColor("#6b7b77"))
        canvas.drawString(15 * mm, 9 * mm, "AI 驱动的竞品分析 Agent 协作系统")
        canvas.drawRightString(195 * mm, 9 * mm, f"第 {document.page} 页")
        canvas.restoreState()

    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    buffer.seek(0)
    return send_file(
        buffer,
        mimetype="application/pdf",
        as_attachment=True,
        download_name=f"{task_id}_competitive_report.pdf",
    )


def ensure_pdf_dependency_paths() -> None:
    import site
    import sys

    candidates = [site.getusersitepackages()]
    bundled = Path.home() / ".cache" / "codex-runtimes" / "codex-primary-runtime" / "dependencies" / "python" / "Lib" / "site-packages"
    candidates.append(str(bundled))
    for candidate in candidates:
        if candidate and Path(candidate).exists() and candidate not in sys.path:
            sys.path.append(candidate)


def format_date_for_pdf(value: str) -> str:
    return sanitize_text(value.replace("T", " ").replace("Z", ""), 40)


def serialize_task(row: sqlite3.Row, agent_duration_ms: int | None = None) -> dict[str, Any]:
    item = dict(row)
    item["competitor_names"] = loads(item.pop("competitors_json"), [])
    item["websites"] = loads(item.pop("websites_json"), [])
    item["focus_areas"] = loads(item.pop("focus_areas_json"), [])
    item["archived"] = bool(item.get("archived", 0))
    item["elapsed_label"] = elapsed_label(item["created_at"], item.get("completed_at") or "")
    item["elapsed_ms"] = elapsed_ms(item["created_at"], item.get("completed_at") or "")
    return item


def serialize_competitor(row: sqlite3.Row) -> dict[str, Any]:
    item = dict(row)
    item["target_users"] = loads(item.pop("target_users_json"), [])
    return item


def serialize_claim(row: sqlite3.Row) -> dict[str, Any]:
    item = dict(row)
    item["source_ids"] = loads(item["source_ids"], [])
    item["needs_review"] = bool(item["needs_review"])
    return item


def serialize_agent_run(row: sqlite3.Row) -> dict[str, Any]:
    item = dict(row)
    item["tool_calls"] = loads(item["tool_calls"], [])
    item["has_rework"] = bool(item.get("has_rework", 0))
    return sanitize_payload(item)


def serialize_agent_event(row: sqlite3.Row) -> dict[str, Any]:
    item = dict(row)
    item["meta"] = loads(item.pop("meta_json"), {})
    return sanitize_payload(item)


def elapsed_label(start: str, end: str) -> str:
    return format_seconds(max(0, round(elapsed_ms(start, end) / 1000)))


def elapsed_ms(start: str, end: str) -> int:
    try:
        start_dt = parse_iso(start)
        end_dt = parse_iso(end) if end else parse_iso(utc_now_iso())
    except ValueError:
        return 0
    return max(0, int((end_dt - start_dt).total_seconds() * 1000))


def format_seconds(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds} 秒"
    return f"{seconds // 60} 分 {seconds % 60:02d} 秒"


def parse_iso(value: str):
    return __import__("datetime").datetime.fromisoformat(value.replace("Z", ""))


app = create_app({"TESTING": True}) if "pytest" in sys.modules else create_app()


if __name__ == "__main__":
    app = create_app()
    app.run(host="127.0.0.1", port=5016, debug=False)
