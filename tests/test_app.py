from __future__ import annotations

import io
import json
import os
import sqlite3
import sys
import threading
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import create_app, ensure_pdf_dependency_paths
from appark_collector import parse_appark_text
from collector import VolcWebSearchClient, WebSourceDraft, chunk_text
from feishu_publisher import FeishuPublishError, FeishuQuestionnairePublisher, build_feishu_questions
from llm_provider import LLMProvider
from orchestrator import Orchestrator, sanitize_markdown_text, sanitize_payload
import react_report_agent
from react_report_agent import (
    _coerce_zhipu_report_structure,
    _configured_providers,
    _failover_diagnostic_calls,
    _report_completion_reason,
    _sanitize_zhipu_claims,
    _sanitize_zhipu_sources,
)
from schema import CompetitiveKnowledgeSchema, QAFindingRecord, ReportableClaim, utc_now_iso


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "mock")
    monkeypatch.delenv("VOLC_SEARCH_API_KEY", raising=False)
    monkeypatch.setenv("VOLC_SEARCH_ENV_PATH", str(tmp_path / "missing.env"))
    app = create_app({"TESTING": True, "DATABASE": str(tmp_path / "test.db"), "WORKFLOW_ASYNC": False})
    return app.test_client()


@pytest.fixture()
def local_page_url():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/robots.txt":
                body = b"User-agent: *\nAllow: /\n"
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            body = (
                "<html><head><title>Local Product Pricing</title></head><body>"
                "<h1>Local Product</h1>"
                "<p>Local Product supports collaborative research workflows, source tracking, "
                "pricing plan comparison, evidence review, and report generation for product teams.</p>"
                "<p>The public page describes workflow automation, trace logs, and reviewer handoff.</p>"
                "</body></html>"
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/product"
    finally:
        server.shutdown()
        thread.join(timeout=2)


def create_demo_task(client):
    response = client.post(
        "/api/tasks",
        json={
            "industry": "协同办公与知识管理",
            "competitors": ["飞书", "Notion", "Airtable"],
            "websites": ["https://www.feishu.cn/", "https://www.notion.com/", "https://www.airtable.com/"],
            "focus_areas": ["功能对比", "定价", "用户评价", "SWOT"],
            "source_mode": "缓存样例",
        },
    )
    assert response.status_code == 201
    return response.get_json()


def make_fake_lark_cli(tmp_path, mode="success"):
    tmp_path.mkdir(parents=True, exist_ok=True)
    script = tmp_path / "fake_lark_cli.py"
    script.write_text(
        """
import json
import sys

mode = sys.argv[1]
args = sys.argv[2:]

def emit(payload):
    print(json.dumps(payload, ensure_ascii=False))

if mode == "invalid_json":
    print("not json")
    raise SystemExit(0)
if args[:2] == ["auth", "status"]:
    status = "expired" if mode == "not_ready" else "ready"
    emit({"identity": "user", "identities": {"user": {"status": status}}})
    raise SystemExit(0)
if mode == "not_ready":
    emit({"error": "not ready"})
    raise SystemExit(1)
if "+base-create" in args:
    emit({"data": {"base": {"app_token": "basetest"}}})
elif "+table-create" in args:
    emit({"data": {"table": {"id": "tbltest"}}})
elif "+form-create" in args:
    if mode == "form_list_fallback":
        emit({"data": {"name": "测试问卷"}})
    else:
        emit({"data": {"form": {"id": "frmtest"}}})
elif "+form-list" in args:
    emit({"data": {"items": [{"name": "测试问卷", "view_id": "frmtest"}]}})
elif "+form-questions-create" in args:
    emit({"data": {"created": True}})
elif "+form-get" in args:
    emit({"data": {"form_id": "frmtest", "share_token": "shrtest", "share_url": "https://feishu.cn/base/form/share/shrtest"}})
elif "+form-detail" in args:
    emit({"data": {"share_url": "https://feishu.cn/base/form/share/shrtest"}})
else:
    emit({"ok": True})
""",
        encoding="utf-8",
    )
    if os.name == "nt":
        cli = tmp_path / "lark-cli.cmd"
        cli.write_text(f'@echo off\n"{sys.executable}" "{script}" {mode} %*\n', encoding="utf-8")
    else:
        cli = tmp_path / "lark-cli"
        cli.write_text(f'#!/usr/bin/env sh\n"{sys.executable}" "{script}" {mode} "$@"\n', encoding="utf-8")
        cli.chmod(0o755)
    return str(cli)


def test_index_copy_and_manual_modal_are_current(client):
    response = client.get("/")
    html = response.get_data(as_text=True)
    assert response.status_code == 200
    assert "MOSS多agent智能竞品分析系统——小莫" in html
    assert "人工复查/补充材料" in html
    assert 'id="manualGuide"' in html
    assert 'id="manualTextLabel"' in html
    assert "改进建议" not in html
    assert "第一版演示工作台" not in html
    assert "提交并触发 Agent" not in html
    assert 'id="selectedTextField"' in html
    assert 'id="downloadLogsButton"' in html
    assert 'id="buttonProgress"' not in html
    assert 'id="historyContextMenu"' in html
    assert 'id="historyTabButton"' in html
    assert 'id="archiveTabButton"' in html
    assert 'id="deleteBackdrop"' in html
    assert 'id="editTaskButton"' in html
    assert 'id="questionnaireMenuButton"' in html
    assert 'id="researchFloatBall"' in html
    assert "问卷调研" in html
    assert ">提交</button>" in html


def test_questionnaire_design_creates_shareable_page_and_response(client):
    task = create_demo_task(client)
    response = client.post(
        f"/api/tasks/{task['id']}/questionnaire",
        json={
            "objective": "了解用户对飞书、Notion、Airtable 的协作体验、付费意愿和切换条件",
            "target_users": "近三个月使用过协同办公产品的团队用户",
        },
    )
    assert response.status_code == 201
    payload = response.get_json()
    assert payload["design_id"]
    assert payload["share_path"].startswith("/questionnaires/")
    assert payload["share_url"].endswith(payload["share_path"])
    assert payload["design"]["sections"]

    page = client.get(payload["share_path"])
    html = page.get_data(as_text=True)
    assert page.status_code == 200
    assert "提交问卷" in html
    assert "请勿填写手机号" in html

    submitted = client.post(
        f"{payload['share_path']}/responses",
        data={
            "respondent_label": "R01",
            "user_role": "团队成员",
            "usage_frequency": "每天",
            "overall_satisfaction": "4",
        },
    )
    assert submitted.status_code == 200
    assert "感谢你的反馈" in submitted.get_data(as_text=True)


def test_feishu_questionnaire_mapping_covers_question_types():
    questions = build_feishu_questions(
        {
            "title": "测试问卷",
            "sections": [
                {
                    "section_title": "基本信息",
                    "questions": [
                        {
                            "id": "Q1",
                            "type": "single_choice",
                            "question_text": "你主要使用哪个产品？",
                            "options": ["飞书", "Notion"],
                            "required": True,
                        },
                        {
                            "id": "Q2",
                            "type": "multiple_choice",
                            "question_text": "你看重哪些因素？",
                            "options": ["价格", "生态"],
                        },
                        {"id": "Q3", "type": "likert", "question_text": "整体满意度如何？"},
                        {"id": "Q4", "type": "open_ended", "question_text": "还有什么建议？"},
                    ],
                }
            ],
        }
    )
    assert questions[0]["title"] == "受访者标识"
    assert questions[1]["type"] == "select"
    assert questions[1]["multiple"] is False
    assert questions[2]["multiple"] is True
    assert questions[3]["type"] == "number"
    assert questions[3]["style"]["type"] == "rating"
    assert questions[4]["type"] == "text"


def test_feishu_publisher_uses_fake_cli_successfully(tmp_path):
    publisher = FeishuQuestionnairePublisher(cli_path=make_fake_lark_cli(tmp_path), timeout_seconds=5)
    result = publisher.publish_questionnaire(
        {
            "title": "测试问卷",
            "description": "用于测试",
            "sections": [
                {
                    "section_title": "用户反馈",
                    "questions": [
                        {
                            "id": "Q1",
                            "type": "single_choice",
                            "question_text": "是否愿意继续使用？",
                            "options": ["是", "否"],
                        }
                    ],
                }
            ],
        },
        "测试问卷",
    )
    assert result.feishu_url == "https://feishu.cn/base/form/share/shrtest"
    assert result.base_token == "basetest"
    assert result.table_id == "tbltest"
    assert result.form_id == "frmtest"
    assert [call["name"] for call in result.tool_calls][:4] == [
        "auth status",
        "base +base-create",
        "base +table-create",
        "base +form-create",
    ]


def test_feishu_publisher_falls_back_to_form_list_when_create_omits_id(tmp_path):
    publisher = FeishuQuestionnairePublisher(
        cli_path=make_fake_lark_cli(tmp_path, mode="form_list_fallback"),
        timeout_seconds=5,
    )
    result = publisher.publish_questionnaire(
        {
            "title": "测试问卷",
            "description": "用于测试",
            "sections": [
                {
                    "section_title": "用户反馈",
                    "questions": [
                        {
                            "id": "Q1",
                            "type": "single_choice",
                            "question_text": "是否愿意继续使用？",
                            "options": ["是", "否"],
                        }
                    ],
                }
            ],
        },
        "测试问卷",
    )
    assert result.form_id == "frmtest"
    assert any(call["name"] == "base +form-list" for call in result.tool_calls)


def test_feishu_publisher_reports_missing_auth_and_bad_json(tmp_path):
    with pytest.raises(FeishuPublishError) as not_ready:
        FeishuQuestionnairePublisher(
            cli_path=make_fake_lark_cli(tmp_path / "not_ready", mode="not_ready"),
            timeout_seconds=5,
        ).publish_questionnaire({"sections": [{"questions": [{"question_text": "问题"}]}]}, "测试问卷")
    assert "未就绪" in str(not_ready.value)

    with pytest.raises(FeishuPublishError) as bad_json:
        FeishuQuestionnairePublisher(
            cli_path=make_fake_lark_cli(tmp_path / "invalid_json", mode="invalid_json"),
            timeout_seconds=5,
        ).publish_questionnaire({"sections": [{"questions": [{"question_text": "问题"}]}]}, "测试问卷")
    assert "可解析 JSON" in str(bad_json.value)


def test_publish_questionnaire_to_feishu_endpoint_records_target_and_source(client, tmp_path, monkeypatch):
    monkeypatch.setenv("FEISHU_CLI_PATH", make_fake_lark_cli(tmp_path))
    task = create_demo_task(client)
    created = client.post(
        f"/api/tasks/{task['id']}/questionnaire",
        json={"objective": "了解协同工具用户对飞书问卷链路的接受度"},
    ).get_json()

    response = client.post(f"/api/questionnaires/{created['design_id']}/publish/feishu", json={})
    assert response.status_code == 201
    payload = response.get_json()
    assert payload["publish_status"] == "published"
    assert payload["feishu_url"] == "https://feishu.cn/base/form/share/shrtest"
    assert payload["base_token"] == "basetest"
    assert payload["local_share_url"].endswith(created["share_path"])

    reused = client.post(f"/api/questionnaires/{created['design_id']}/publish/feishu", json={})
    assert reused.status_code == 200
    assert reused.get_json()["reused"] is True

    conn = sqlite3.connect(client.application.config["DATABASE"])
    try:
        target_count = conn.execute(
            "SELECT COUNT(*) FROM questionnaire_publish_targets WHERE questionnaire_design_id = ? AND status = 'published'",
            (created["design_id"],),
        ).fetchone()[0]
        source = conn.execute(
            "SELECT source_type, url_or_path, provider FROM sources WHERE task_id = ? AND source_type = 'feishu_questionnaire'",
            (task["id"],),
        ).fetchone()
    finally:
        conn.close()
    assert target_count == 1
    assert source == ("feishu_questionnaire", "https://feishu.cn/base/form/share/shrtest", "feishu_cli")


def test_publish_questionnaire_to_feishu_endpoint_keeps_local_link_on_failure(client, tmp_path, monkeypatch):
    monkeypatch.setenv("FEISHU_CLI_PATH", make_fake_lark_cli(tmp_path, mode="not_ready"))
    task = create_demo_task(client)
    created = client.post(
        f"/api/tasks/{task['id']}/questionnaire",
        json={"objective": "了解飞书发布失败时的本地问卷降级体验"},
    ).get_json()

    response = client.post(f"/api/questionnaires/{created['design_id']}/publish/feishu", json={})
    assert response.status_code == 503
    payload = response.get_json()
    assert payload["publish_status"] == "failed"
    assert payload["local_share_url"].endswith(created["share_path"])

    conn = sqlite3.connect(client.application.config["DATABASE"])
    try:
        status = conn.execute(
            "SELECT status FROM questionnaire_publish_targets WHERE questionnaire_design_id = ?",
            (created["design_id"],),
        ).fetchone()[0]
    finally:
        conn.close()
    assert status == "failed"


def test_research_draft_api_works_without_task(client):
    response = client.post(
        "/api/research/interview-guide",
        json={
            "objective": "了解开发者对 ChatGPT 和豆包的功能偏好与迁移顾虑",
            "competitors": ["ChatGPT", "豆包"],
            "industry": "AI 大模型与智能助手",
            "focus_areas": ["功能对比", "定价", "用户评价"],
        },
    )
    assert response.status_code == 201
    payload = response.get_json()
    assert payload["draft_only"] is True
    assert payload["guide"]["phases"]
    assert "ChatGPT" in payload["guide"]["title"]


def test_create_task_runs_full_agent_flow(client):
    task = create_demo_task(client)
    assert task["status"] == "completed"
    assert task["latest_report"]["version"] == 1

    logs = client.get(f"/api/tasks/{task['id']}/logs").get_json()
    assert {"采集 Agent", "分析 Agent", "质检 Agent", "报告 Agent"} <= {item["agent_name"] for item in logs}
    assert any(item["agent_name"] == "质检 Agent" and item["status"] == "completed" for item in logs)

    events = client.get(f"/api/tasks/{task['id']}/events").get_json()
    assert any(event["agent_name"] == "采集 Agent" for event in events)
    assert any("质检" in event["agent_name"] for event in events)

    claims = client.get(f"/api/tasks/{task['id']}/claims").get_json()
    reportable = [claim for claim in claims if claim["source_ids"]]
    assert reportable
    assert all(claim["source_ids"] for claim in reportable)

    report = client.get(f"/api/tasks/{task['id']}/report").get_json()
    assert report["content"]["metrics"]["citation_coverage"] == 1.0
    assert report["content"]["metrics"]["qa_rework_visible"] is False
    assert report["content"]["metrics"]["evidence_chunk_count"] > 0
    CompetitiveKnowledgeSchema.model_validate(report["content"])
    assert {
        "feature_tree",
        "pricing_model",
        "user_persona",
        "swot",
        "source_catalog",
        "methodology",
        "chart_data",
    } <= set(report["content"])


def test_schema_accepts_manual_pending_and_rejects_incomplete_knowledge_payload():
    finding = QAFindingRecord(
        id="finding-1",
        task_id="task-1",
        claim_id="claim-1",
        severity="high",
        reason="同一问题自动修复三次仍未通过。",
        target_agent="分析 Agent",
        fix_status="manual_pending",
        created_at=utc_now_iso(),
    )
    assert finding.fix_status == "manual_pending"

    with pytest.raises(ValidationError):
        CompetitiveKnowledgeSchema.model_validate(
            {
                "title": "缺少关键字段的报告",
                "feature_tree": [],
                "pricing_model": [],
                "user_persona": [],
                "swot": {},
                "source_catalog": [],
                "methodology": {},
            }
        )


def test_report_schema_validation_blocks_report_insert(tmp_path, monkeypatch):
    db_path = tmp_path / "report-schema.db"
    app = create_app({"TESTING": True, "DATABASE": str(db_path), "WORKFLOW_ASYNC": False})
    test_client = app.test_client()
    task = create_demo_task(test_client)
    orch = Orchestrator(db_path, ROOT / "data" / "demo_dataset.json")

    with sqlite3.connect(db_path) as conn:
        before = conn.execute("SELECT COUNT(*) FROM reports WHERE task_id = ?", (task["id"],)).fetchone()[0]

    def fail_validation(payload):
        raise ValueError("forced schema failure")

    monkeypatch.setattr("orchestrator.CompetitiveKnowledgeSchema.model_validate", fail_validation)
    with pytest.raises(ValueError):
        orch._generate_report(task["id"], reason="schema_validation_test")

    with sqlite3.connect(db_path) as conn:
        after = conn.execute("SELECT COUNT(*) FROM reports WHERE task_id = ?", (task["id"],)).fetchone()[0]
        event_count = conn.execute(
            "SELECT COUNT(*) FROM agent_events WHERE task_id = ? AND event_type = 'report_schema_validation_failed'",
            (task["id"],),
        ).fetchone()[0]
    assert after == before
    assert event_count == 1


def test_create_task_accepts_mixed_source_mode(client):
    response = client.post(
        "/api/tasks",
        json={
            "industry": "协同办公与知识管理",
            "competitors": ["飞书", "Notion"],
            "focus_areas": ["功能对比"],
            "source_mode": "实时采集+上传资料",
        },
    )
    assert response.status_code == 201
    task = response.get_json()
    assert task["source_mode"] == "实时采集+上传资料"


def test_product_input_infers_ai_industry(client):
    response = client.post(
        "/api/tasks",
        json={
            "industry": "待识别行业",
            "competitors": ["chatgpt", "豆包"],
            "focus_areas": ["功能对比"],
            "source_mode": "缓存样例",
        },
    )
    assert response.status_code == 201
    task = response.get_json()
    assert task["industry"] == "AI 大模型与智能助手"
    assert task["name"].startswith("AI 大模型与智能助手")


def test_product_input_infers_auto_industry(client):
    response = client.post(
        "/api/tasks",
        json={
            "industry": "待识别行业",
            "competitors": ["比亚迪", "小鹏"],
            "focus_areas": ["功能对比"],
            "source_mode": "缓存样例",
        },
    )
    assert response.status_code == 201
    task = response.get_json()
    assert task["industry"] == "新能源汽车与智能汽车"


def test_frontend_enter_submit_and_industry_inference_are_wired(client):
    js = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
    assert 'event.key !== "Enter"' in js
    assert "requestSubmit()" in js
    assert "inferIndustryFromCompetitors" in js
    assert "AI 大模型与智能助手" in js
    assert "新能源汽车与智能汽车" in js
    assert "Asia/Shanghai" in js
    assert "historyArchived" in js
    assert "editCurrentTaskAsNew" in js


def test_unknown_competitors_do_not_reuse_static_demo_report(client):
    response = client.post(
        "/api/tasks",
        json={
            "industry": "AI",
            "competitors": ["chatgpt", "豆包"],
            "focus_areas": ["功能对比", "定价", "用户评价"],
            "source_mode": "缓存样例",
        },
    )
    assert response.status_code == 201
    task = response.get_json()
    report = client.get(f"/api/tasks/{task['id']}/report").get_json()
    payload = str(report["content"])
    assert "chatgpt" in payload
    assert "豆包" in payload
    assert "公开能力材料较少" in payload or "未形成判断" in payload
    assert "飞书" not in payload
    assert "Notion" not in payload
    assert "Airtable" not in payload

    sources = client.get(f"/api/tasks/{task['id']}/sources").get_json()
    assert any(source["source_type"] == "manual_scope" for source in sources)


def test_realtime_collection_without_volc_key_falls_back_to_bing_or_direct(client, local_page_url):
    response = client.post(
        "/api/tasks",
        json={
            "industry": "本地测试行业",
            "competitors": ["本地产品"],
            "websites": [local_page_url],
            "focus_areas": ["功能对比"],
            "source_mode": "实时采集",
        },
    )
    assert response.status_code == 201
    task = response.get_json()

    sources = client.get(f"/api/tasks/{task['id']}/sources").get_json()
    # local_page_url should not be used as a direct source
    assert not any(source["url_or_path"] == local_page_url for source in sources)
    # With fallback chain (BingSearch / direct fetch), we should have some sources
    assert len(sources) > 0

    events = client.get(f"/api/tasks/{task['id']}/events").get_json()
    config_events = [item for item in events if item["event_type"] == "volc_search_config"]
    assert config_events
    assert config_events[-1]["meta"]["api_key_configured"] is False
    # Should have logged volc_search_failed fallback events
    volc_failed = [item for item in events if item["event_type"] == "volc_search_failed"]
    assert volc_failed


def test_volc_search_client_builds_payload_and_parses_results(monkeypatch):
    monkeypatch.setenv("VOLC_SEARCH_API_KEY", "test-key")
    monkeypatch.setenv("VOLC_SEARCH_COUNT", "2")
    captured = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            payload = {
                "Code": 0,
                "Result": {
                    "LogId": "log-123",
                    "TimeCost": 321,
                    "WebResults": [
                        {
                            "Title": "Doubao pricing",
                            "Url": "https://www.doubao.com/pricing",
                            "Summary": "Doubao pricing summary",
                            "Content": "Doubao pricing content",
                            "AuthInfoDes": "official",
                            "AuthInfoLevel": 3,
                            "PublishTime": "2026-05-01",
                        }
                    ],
                },
            }
            return f"data:{json.dumps(payload, ensure_ascii=False)}\n\ndata:[DONE]\n\n".encode("utf-8")

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["headers"] = dict(request.headers)
        captured["payload"] = json.loads(request.data.decode("utf-8"))
        return FakeResponse()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    results = VolcWebSearchClient().search("doubao pricing", "taskxxxx", limit=2)

    assert captured["url"].endswith("/search_api/web_search")
    assert captured["headers"]["Authorization"] == "Bearer test-key"
    assert captured["payload"]["SearchType"] == "web_summary"
    assert captured["payload"]["NeedSummary"] is True
    assert captured["payload"]["Filter"]["NeedContent"] is True
    assert captured["payload"]["Filter"]["NeedUrl"] is True
    assert "bilibili.com" in captured["payload"]["Filter"]["BlockHosts"]
    assert results[0].source_type == "volc_search_result"
    assert results[0].provider == "volc_search"
    assert results[0].search_log_id == "log-123"
    assert results[0].time_cost_ms == 321
    assert results[0].published_at == "2026-05-01"


def test_volc_search_client_accepts_list_numeric_fields(monkeypatch):
    monkeypatch.setenv("VOLC_SEARCH_API_KEY", "test-key")
    captured = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            payload = {
                "Code": 0,
                "Result": {
                    "LogId": "log-list",
                    "TimeCost": [123],
                    "WebResults": [
                        {
                            "Title": "List numeric",
                            "Url": "https://example.com/list",
                            "Summary": "list numeric summary",
                            "AuthInfoLevel": [2],
                        },
                        {
                            "Title": "Empty numeric",
                            "Url": "https://example.com/empty",
                            "Summary": "empty numeric summary",
                            "AuthInfoLevel": [],
                        },
                    ],
                },
            }
            return f"data:{json.dumps(payload, ensure_ascii=False)}\n\ndata:[DONE]\n\n".encode("utf-8")

    def fake_urlopen(request, timeout):
        captured["payload"] = json.loads(request.data.decode("utf-8"))
        return FakeResponse()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    results = VolcWebSearchClient().search("list numeric", "taskxxxx", limit=2)

    assert captured["payload"]["Count"] == 2
    assert [item.time_cost_ms for item in results] == [123, 123]
    assert [item.auth_level for item in results] == [2, 0]


def test_volc_search_client_requires_api_key(tmp_path, monkeypatch):
    monkeypatch.delenv("VOLC_SEARCH_API_KEY", raising=False)
    monkeypatch.setenv("VOLC_SEARCH_ENV_PATH", str(tmp_path / "missing.env"))
    with pytest.raises(RuntimeError, match="VOLC_SEARCH_API_KEY"):
        VolcWebSearchClient().search("query", "task")


def test_volc_search_client_reloads_local_env_without_restart(tmp_path, monkeypatch):
    monkeypatch.delenv("VOLC_SEARCH_API_KEY", raising=False)
    env_path = tmp_path / ".env.local"
    env_path.write_text("VOLC_SEARCH_API_KEY=\nVOLC_SEARCH_COUNT=1\n", encoding="utf-8")
    client = VolcWebSearchClient(env_path=env_path)
    assert client.config_status()["api_key_configured"] is False

    env_path.write_text("VOLC_SEARCH_API_KEY=test-key\nVOLC_SEARCH_COUNT=1\n", encoding="utf-8")

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json.dumps({"Code": 0, "Result": {"WebResults": []}}).encode("utf-8")

    captured = {}

    def fake_urlopen(request, timeout):
        captured["authorization"] = request.headers["Authorization"]
        return FakeResponse()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    assert client.search("query", "task") == []
    assert captured["authorization"] == "Bearer test-key"


def test_realtime_search_fallback_writes_search_sources(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "mock")
    bing_called = False

    def fake_collect(self, urls, task_prefix):
        return [], [{"url": url, "reason": "blocked in test"} for url in urls]

    def fake_search(self, query, task_prefix, start_index=0, limit=3, block_hosts=None):
        title = "豆包 - 字节跳动旗下 AI 智能助手" if "豆包" in query or "Doubao" in query else "ChatGPT"
        url = "https://www.doubao.com/" if "豆包" in query or "Doubao" in query else "https://chatgpt.com/"
        excerpt = f"搜索词：{query}。搜索结果：{title}。摘要：公开搜索结果线索，待二次采集复核。"
        return [
            WebSourceDraft(
                source_id=f"{task_prefix}_volc_{start_index + 1:02d}",
                source_type="volc_search_result",
                title=title,
                url=url,
                author_site=url.split("/")[2],
                excerpt=excerpt,
                credibility="low",
                chunks=chunk_text(excerpt, chunk_size=700, overlap=80),
                provider="volc_search",
                search_log_id="log-test",
                search_query=query,
            )
        ]

    def fake_bing_search(self, query, task_prefix, start_index=0, limit=3):
        nonlocal bing_called
        bing_called = True
        raise AssertionError("Bing should not be called in the Volc collection path")

    monkeypatch.setattr("collector.WebCollector.collect", fake_collect)
    monkeypatch.setattr("collector.VolcWebSearchClient.search", fake_search)
    monkeypatch.setattr("collector.BingSearchClient.search", fake_bing_search)
    app = create_app({"TESTING": True, "DATABASE": str(tmp_path / "search.db"), "WORKFLOW_ASYNC": False})
    test_client = app.test_client()

    response = test_client.post(
        "/api/tasks",
        json={
            "industry": "待识别行业",
            "competitors": ["chatgpt", "豆包"],
            "focus_areas": ["功能对比", "定价"],
            "source_mode": "实时采集",
        },
    )
    assert response.status_code == 201
    task = response.get_json()
    assert task["industry"] == "AI 大模型与智能助手"

    sources = test_client.get(f"/api/tasks/{task['id']}/sources").get_json()
    assert any(source["source_type"] == "volc_search_result" for source in sources)
    assert any(source["provider"] == "volc_search" and source["search_log_id"] == "log-test" for source in sources)
    assert all("competitor_name" in source and "module" in source and "relevance_score" in source for source in sources)
    assert bing_called is False

    knowledge = test_client.get(f"/api/tasks/{task['id']}/knowledge").get_json()
    assert knowledge["feature_tree"] or knowledge["pricing_model"] or knowledge["claims"]
    assert all(item["source_refs"] for group in ["feature_tree", "pricing_model"] for item in knowledge[group])
    assert all(claim["claim_type"] in {"fact", "inference", "recommendation", "assumption"} for claim in knowledge["claims"])

    report = test_client.get(f"/api/tasks/{task['id']}/report").get_json()
    metrics = report["content"]["metrics"]
    assert "volc_search" in metrics["collection_provider"]
    assert metrics["search_result_count"] > 0

    report = test_client.get(f"/api/tasks/{task['id']}/report").get_json()
    payload = str(report["content"])
    assert "chatgpt" in payload
    assert "豆包" in payload
    assert "飞书" not in payload
    assert "Notion" not in payload


def test_realtime_search_filters_unrelated_results(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "mock")

    def fake_collect(self, urls, task_prefix):
        return [], [{"url": url, "reason": "blocked in test"} for url in urls]

    def fake_search(self, query, task_prefix, start_index=0, limit=3, block_hosts=None):
        excerpt = f"搜索词：{query}。搜索结果：哔哩哔哩。摘要：无关页面。"
        return [
            WebSourceDraft(
                source_id=f"{task_prefix}_volc_{start_index + 1:02d}",
                source_type="volc_search_result",
                title="哔哩哔哩",
                url="https://www.bilibili.com/",
                author_site="www.bilibili.com",
                excerpt=excerpt,
                credibility="low",
                chunks=chunk_text(excerpt, chunk_size=700, overlap=80),
                provider="volc_search",
                search_query=query,
            )
        ]

    monkeypatch.setattr("collector.WebCollector.collect", fake_collect)
    monkeypatch.setattr("collector.VolcWebSearchClient.search", fake_search)
    app = create_app({"TESTING": True, "DATABASE": str(tmp_path / "filter.db"), "WORKFLOW_ASYNC": False})
    test_client = app.test_client()

    response = test_client.post(
        "/api/tasks",
        json={
            "industry": "待识别行业",
            "competitors": ["比亚迪", "小鹏"],
            "focus_areas": ["功能对比"],
            "source_mode": "实时采集",
        },
    )
    assert response.status_code == 201
    task = response.get_json()
    sources = test_client.get(f"/api/tasks/{task['id']}/sources").get_json()
    assert not any("bilibili" in source["url_or_path"] for source in sources)
    assert any(source["source_type"] == "manual_scope" for source in sources)


def test_realtime_search_keeps_company_alias_and_domain_results(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "mock")

    def fake_collect(self, urls, task_prefix):
        return [], [{"url": url, "reason": "blocked in test"} for url in urls]

    def fake_search(self, query, task_prefix, start_index=0, limit=6, block_hosts=None):
        if "爱旭" in query:
            rows = [
                ("ABC News", "https://abc.com/zh-cn/", "Australian news, weather and video."),
                ("爱旭科技 ABC 电池产品", "https://aikosolar.com/cn/products", "AIKO Solar 介绍 ABC 电池、光伏组件和产品技术。"),
            ]
        else:
            rows = [
                ("隆基股份 官网 产品中心", "https://www.longi.com/cn/products/", "LONGi 官网展示光伏组件、硅片和产品解决方案。"),
                ("无关视频", "https://www.bilibili.com/video/1", "娱乐视频页面。"),
            ]
        return [
            WebSourceDraft(
                source_id=f"{task_prefix}_volc_{start_index + index + 1:02d}",
                source_type="volc_search_result",
                title=title,
                url=url,
                author_site=url.split("/")[2],
                excerpt=f"搜索词：{query}。搜索结果：{title}。摘要：{snippet}",
                credibility="low",
                chunks=chunk_text(snippet, chunk_size=700, overlap=80),
                provider="volc_search",
                search_query=query,
            )
            for index, (title, url, snippet) in enumerate(rows)
        ]

    monkeypatch.setattr("collector.WebCollector.collect", fake_collect)
    monkeypatch.setattr("collector.VolcWebSearchClient.search", fake_search)
    app = create_app({"TESTING": True, "DATABASE": str(tmp_path / "company-alias.db"), "WORKFLOW_ASYNC": False})
    test_client = app.test_client()

    response = test_client.post(
        "/api/tasks",
        json={
            "industry": "光伏新能源行业",
            "competitors": ["隆基绿能", "爱旭股份"],
            "focus_areas": ["功能对比", "定价", "SWOT"],
            "source_mode": "实时采集",
        },
    )

    assert response.status_code == 201
    task = response.get_json()
    sources = test_client.get(f"/api/tasks/{task['id']}/sources").get_json()
    urls = " ".join(source["url_or_path"] for source in sources)
    assert "longi.com" in urls
    assert "aikosolar.com" in urls
    assert "abc.com" not in urls
    assert "bilibili.com" not in urls
    assert not any(source["source_type"] == "manual_scope" for source in sources)

    events = test_client.get(f"/api/tasks/{task['id']}/events").get_json()
    serialized_events = str(events)
    assert "搜索候选" in serialized_events
    assert "保留" in serialized_events
    assert "丢弃" in serialized_events


def test_realtime_search_keeps_category_comparison_results(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "mock")

    def fake_collect(self, urls, task_prefix):
        return [], [{"url": url, "reason": "blocked in test"} for url in urls]

    def fake_search(self, query, task_prefix, start_index=0, limit=6, block_hosts=None):
        if "二锅头" in query:
            rows = [
                ("北京二锅头价格与消费场景", "https://example.com/er-guo-tou", "二锅头是北京白酒代表，常见于大众价位和餐饮消费场景。"),
                ("二锅头笑话大全", "https://example.com/jokes", "段子和娱乐内容。"),
            ]
        else:
            rows = [
                ("酱香型白酒代表品牌与价格带", "https://example.com/jiangxiang", "酱香型白酒涉及茅台镇、坤沙工艺、代表品牌和价格带。"),
                ("无关游戏页面", "https://example.com/game", "游戏攻略。"),
            ]
        return [
            WebSourceDraft(
                source_id=f"{task_prefix}_volc_{start_index + index + 1:02d}",
                source_type="volc_search_result",
                title=title,
                url=url,
                author_site=url.split("/")[2],
                excerpt=f"搜索词：{query}。搜索结果：{title}。摘要：{snippet}",
                credibility="low",
                chunks=chunk_text(snippet, chunk_size=700, overlap=80),
                provider="volc_search",
                search_query=query,
            )
            for index, (title, url, snippet) in enumerate(rows)
        ]

    monkeypatch.setattr("collector.WebCollector.collect", fake_collect)
    monkeypatch.setattr("collector.VolcWebSearchClient.search", fake_search)
    app = create_app({"TESTING": True, "DATABASE": str(tmp_path / "category.db"), "WORKFLOW_ASYNC": False})
    test_client = app.test_client()

    response = test_client.post(
        "/api/tasks",
        json={
            "industry": "白酒品类",
            "competitors": ["酱香型白酒", "二锅头"],
            "focus_areas": ["功能对比", "定价", "用户画像", "SWOT"],
            "source_mode": "实时采集",
        },
    )

    assert response.status_code == 201
    task = response.get_json()
    sources = test_client.get(f"/api/tasks/{task['id']}/sources").get_json()
    payload = str(sources)
    assert "酱香型白酒代表品牌与价格带" in payload
    assert "北京二锅头价格与消费场景" in payload
    assert "笑话" not in payload
    assert "游戏" not in payload
    assert not any(source["source_type"] == "manual_scope" for source in sources)


def test_ai_products_are_not_classified_as_category(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "mock")
    orchestrator = Orchestrator(tmp_path / "object-type.db", ROOT / "data" / "demo_dataset.json")
    assert orchestrator._analysis_object_type("ChatGPT", "AI 大模型与智能助手") == "product"
    assert orchestrator._analysis_object_type("DeepSeek", "AI 大模型与智能助手") == "product"
    assert orchestrator._analysis_object_type("酱香型白酒", "白酒品类") == "category"


def test_product_search_requires_competitor_identity(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "mock")
    orchestrator = Orchestrator(tmp_path / "identity.db", ROOT / "data" / "demo_dataset.json")
    chatgpt = WebSourceDraft(
        source_id="s_chatgpt",
        source_type="volc_search_result",
        title="ChatGPT 价格 2026 中文详解",
        url="https://example.com/chatgpt-pricing",
        author_site="example.com",
        excerpt="搜索词：DeepSeek 价格套餐。摘要：ChatGPT Plus 价格。",
        credibility="low",
        chunks=chunk_text("ChatGPT Plus 价格"),
    )
    deepseek = WebSourceDraft(
        source_id="s_deepseek",
        source_type="volc_search_result",
        title="DeepSeek API Pricing",
        url="https://api-docs.deepseek.com/quick_start/pricing",
        author_site="api-docs.deepseek.com",
        excerpt="搜索词：DeepSeek 价格套餐。摘要：DeepSeek API pricing page.",
        credibility="low",
        chunks=chunk_text("DeepSeek API pricing page"),
    )
    kept, meta = orchestrator._filter_search_results_for_name([chatgpt, deepseek], "DeepSeek", "AI 大模型与智能助手", "官方价格/API")
    assert [item.source_id for item in kept] == ["s_deepseek"]
    assert kept[0].competitor_name == "DeepSeek"
    assert kept[0].module == "官方价格/API"
    assert kept[0].source_role in {"official_pricing", "official_doc"}
    assert any("产品/公司结果未命中" in reason for reason in meta["dropped_reasons"])


def test_official_pricing_page_extracts_pricing_facts(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "mock")
    orchestrator = Orchestrator(tmp_path / "pricing-facts.db", ROOT / "data" / "demo_dataset.json")
    source = {
        "id": "s_deepseek_pricing",
        "title": "模型 & 价格 | DeepSeek API Docs",
        "excerpt": "deepseek-v4-pro 百万tokens 输入价格（缓存命中）0.025元 输入价格（缓存未命中）3元 百万tokens 输出价格6元",
        "competitor_name": "DeepSeek",
        "collected_at": utc_now_iso(),
        "raw_content_status": "fetched",
    }

    facts = orchestrator._pricing_facts_from_source(source)
    assert any(fact["plan_name"].lower() == "deepseek-v4-pro" and fact["price_type"] == "output" and fact["amount"] == 6 for fact in facts)
    assert any(fact["price_type"] == "input_cached" for fact in facts)

    table_source = {
        "id": "s_deepseek_pricing_usd",
        "title": "pricing-details-usd | DeepSeek API Docs",
        "excerpt": "模型 百万tokens 输入价格（缓存命中） 百万tokens 输入价格（缓存未命中） 百万tokens 输出价格 deepseek-chat 64K 8K 0.07美元 0.27美元 1.10美元 deepseek-reasoner 64K 32K 8K 0.14美元 0.55美元 2.19美元",
        "competitor_name": "DeepSeek",
        "collected_at": utc_now_iso(),
        "raw_content_status": "fetched",
    }
    table_facts = orchestrator._pricing_facts_from_source(table_source)
    assert any(fact["plan_name"].lower() == "deepseek-chat" and fact["price_type"] == "output" and fact["amount"] == 1.10 for fact in table_facts)
    assert any(fact["plan_name"].lower() == "deepseek-reasoner" and fact["price_type"] == "input_cached" and fact["amount"] == 0.14 for fact in table_facts)


def test_dag_includes_agent_events(client):
    task = create_demo_task(client)
    dag = client.get(f"/api/tasks/{task['id']}/dag").get_json()
    assert len(dag["nodes"]) == 4
    assert all("events" in node for node in dag["nodes"])
    assert any(node["events"] for node in dag["nodes"])


def test_qa_model_finding_accepts_list_claim_index(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "mock")

    def fake_qa_review(self, task_id):
        return {
            "provider": "mock",
            "token_input": 1,
            "token_output": 1,
            "fallback_reason": "",
            "tool_calls": [{"name": "mock_qa_review", "result": "list index"}],
            "findings": [
                {
                    "claim_index": [0],
                    "severity": "high",
                    "reason": "模型返回列表 claim_index，需要映射到第一条 claim。",
                    "target_agent": "分析 Agent",
                }
            ],
        }

    monkeypatch.setattr("orchestrator.Orchestrator._model_qa_review_for_task", fake_qa_review)
    app = create_app({"TESTING": True, "DATABASE": str(tmp_path / "qa-list-index.db"), "WORKFLOW_ASYNC": False})
    test_client = app.test_client()

    task = create_demo_task(test_client)
    assert task["status"] == "completed"
    assert task["latest_report"]
    payload = test_client.get(f"/api/tasks/{task['id']}").get_json()
    assert payload["status"] == "completed"
    assert payload["latest_report"]
    findings = payload["qa_findings"]
    assert any("列表 claim_index" in item["reason"] and item["claim_id"] for item in findings)
    detailed = next(item for item in findings if "列表 claim_index" in item["reason"])
    assert detailed["claim_content"]
    assert detailed["claim_section"]
    assert "missing_material" in detailed
    assert "repair_action" in detailed
    events = test_client.get(f"/api/tasks/{task['id']}/events").get_json()
    assert len([event for event in events if event["event_type"] == "qa_rejected"]) >= 3
    assert any(event["event_type"] == "qa_manual_handoff" for event in events)
    dag = test_client.get(f"/api/tasks/{task['id']}/dag").get_json()
    node_map = {node["id"]: node for node in dag["nodes"]}
    assert node_map["analyst"]["status"] == "已完成"
    assert node_map["qa"]["status"] == "需复核"
    assert len(dag["edges"]) == 3
    assert dag["edges"][-1]["label"] == "自动质检/人工复核"
    assert not any(log.get("agent_name") == "编排层" and log.get("status") == "failed" for log in test_client.get(f"/api/tasks/{task['id']}/logs").get_json())


def test_recheck_without_new_inputs_returns_no_change(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "mock")

    def fake_qa_review(self, task_id):
        return {
            "provider": "mock",
            "token_input": 1,
            "token_output": 1,
            "fallback_reason": "",
            "tool_calls": [{"name": "mock_qa_review", "result": "forced finding"}],
            "findings": [
                {
                    "claim_index": 0,
                    "severity": "medium",
                    "reason": "强制保留一个开放质检问题。",
                    "target_agent": "分析 Agent",
                }
            ],
        }

    monkeypatch.setattr("orchestrator.Orchestrator._model_qa_review_for_task", fake_qa_review)
    app = create_app({"TESTING": True, "DATABASE": str(tmp_path / "qa-no-change.db"), "WORKFLOW_ASYNC": False})
    test_client = app.test_client()

    task = create_demo_task(test_client)
    payload = test_client.post(f"/api/tasks/{task['id']}/qa/recheck", json={}).get_json()
    assert payload["status"] == "no_change"
    assert "未检测到新增来源" in payload["result_summary"]


def test_qa_model_finding_skips_invalid_claim_index(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "mock")

    def fake_qa_review(self, task_id):
        return {
            "provider": "mock",
            "token_input": 1,
            "token_output": 1,
            "fallback_reason": "",
            "tool_calls": [{"name": "mock_qa_review", "result": "invalid indexes"}],
            "findings": [
                {"claim_index": [], "reason": "empty list"},
                {"claim_index": ["bad"], "reason": "bad list"},
                {"claim_index": {"x": 1}, "reason": "dict index"},
            ],
        }

    monkeypatch.setattr("orchestrator.Orchestrator._model_qa_review_for_task", fake_qa_review)
    app = create_app({"TESTING": True, "DATABASE": str(tmp_path / "qa-invalid-index.db"), "WORKFLOW_ASYNC": False})
    test_client = app.test_client()

    task = create_demo_task(test_client)
    events = test_client.get(f"/api/tasks/{task['id']}/events").get_json()
    skipped = [event for event in events if event["event_type"] == "qa_model_finding_skipped"]
    assert len(skipped) >= 3
    assert not any(log.get("agent_name") == "编排层" and log.get("status") == "failed" for log in test_client.get(f"/api/tasks/{task['id']}/logs").get_json())


def test_log_filters_and_download_zip(client):
    task = create_demo_task(client)
    completed_logs = client.get(f"/api/tasks/{task['id']}/logs?status=completed").get_json()
    assert completed_logs
    assert all(item["status"] == "completed" for item in completed_logs)

    qa_logs = client.get(f"/api/tasks/{task['id']}/logs?agent=质检 Agent").get_json()
    assert qa_logs
    assert all(item["agent_name"] == "质检 Agent" for item in qa_logs)

    response = client.get(f"/api/tasks/{task['id']}/logs/download")
    assert response.status_code == 200
    assert response.headers["Content-Type"].startswith("application/zip")
    with zipfile.ZipFile(io.BytesIO(response.data)) as archive:
        names = set(archive.namelist())
        assert {"agent_runs.jsonl", "agent_runs.csv"} <= names
        assert "质检 Agent" in archive.read("agent_runs.csv").decode("utf-8")


def test_archive_and_delete_history_tasks(client):
    task = create_demo_task(client)
    archive_response = client.post(f"/api/tasks/{task['id']}/archive", json={})
    assert archive_response.status_code == 200
    history = client.get("/api/tasks").get_json()
    assert task["id"] not in {item["id"] for item in history}
    archived = client.get("/api/tasks?archived=1").get_json()
    assert task["id"] in {item["id"] for item in archived}

    restore_response = client.post(f"/api/tasks/{task['id']}/archive", json={"archived": False})
    assert restore_response.status_code == 200
    history = client.get("/api/tasks").get_json()
    assert task["id"] in {item["id"] for item in history}

    task_to_delete = create_demo_task(client)
    assert client.get(f"/api/tasks/{task_to_delete['id']}/events").get_json()
    delete_response = client.delete(f"/api/tasks/{task_to_delete['id']}")
    assert delete_response.status_code == 200
    assert client.get(f"/api/tasks/{task_to_delete['id']}").status_code == 404


def test_doubao_key_is_read_from_env_but_not_leaked(tmp_path, monkeypatch):
    dummy_key = "test-key"
    monkeypatch.setenv("LLM_PROVIDER", "doubao")
    monkeypatch.setenv("DOUBAO_API_KEY", dummy_key)
    monkeypatch.delenv("DOUBAO_ENDPOINT_ID", raising=False)
    app = create_app({"TESTING": True, "DATABASE": str(tmp_path / "test.db"), "WORKFLOW_ASYNC": False})
    test_client = app.test_client()

    task = create_demo_task(test_client)
    logs = test_client.get(f"/api/tasks/{task['id']}/logs").get_json()
    serialized = str(logs)
    assert "DOUBAO_ENDPOINT_ID is not configured" in serialized
    assert dummy_key not in serialized

    response = test_client.get(f"/api/tasks/{task['id']}/logs/download")
    with zipfile.ZipFile(io.BytesIO(response.data)) as archive:
        payload = archive.read("agent_runs.jsonl").decode("utf-8")
    assert dummy_key not in payload


def test_recursive_sanitizer_redacts_bearer_and_high_entropy_tokens():
    bearer = "Bearer test-token"
    volc_like = "w4dtzhmaKYAZZKiaVUHHkLYzgYdnCRKi"
    payload = sanitize_payload({"tool_calls": [{"headers": {"Authorization": bearer}, "api_key": volc_like}]})
    serialized = json.dumps(payload, ensure_ascii=False)
    assert "test-token" not in serialized
    assert volc_like not in serialized
    assert "[REDACTED" in serialized


def test_doubao_timeout_falls_back_without_500(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "doubao")
    monkeypatch.setenv("DOUBAO_API_KEY", "test-key")
    monkeypatch.setenv("DOUBAO_ENDPOINT_ID", "ep-test")

    def fake_urlopen(request, timeout):
        raise TimeoutError("read timed out")

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    app = create_app({"TESTING": True, "DATABASE": str(tmp_path / "timeout.db"), "WORKFLOW_ASYNC": False})
    test_client = app.test_client()

    task = create_demo_task(test_client)
    assert task["status"] == "completed"
    logs = test_client.get(f"/api/tasks/{task['id']}/logs").get_json()
    assert any("doubao timeout" in item.get("fallback_reason", "") for item in logs)


def test_doubao_provider_accepts_schema_valid_response(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "doubao")
    monkeypatch.setenv("DOUBAO_API_KEY", "test-key")
    monkeypatch.setenv("DOUBAO_ENDPOINT_ID", "ep-test")

    class FakeResponse:
        headers = {}

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return (
                b'{"choices":[{"message":{"content":"[{\\"section\\":\\"overview\\",'
                b'\\"content\\":\\"chatgpt and doubao need evidence-bound comparison\\",'
                b'\\"confidence\\":0.8,\\"source_ids\\":[\\"src1\\"],'
                b'\\"needs_review\\":false,\\"status\\":\\"reportable\\",'
                b'\\"uncertainty\\":\\"\\"}]"}}],"usage":{"prompt_tokens":12,"completion_tokens":8}}'
            )

    def fake_urlopen(request, timeout):
        assert request.headers["Authorization"] == "Bearer test-key"
        return FakeResponse()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    result = LLMProvider().generate_claims(
        {"industry": "AI", "competitors": ["chatgpt", "豆包"], "focus_areas": ["功能对比"]},
        [{"source_id": "src1", "chunk_index": 0, "source_title": "source", "excerpt": "evidence"}],
    )
    assert result.provider == "doubao"
    assert result.claims[0]["source_ids"] == ["src1"]


def test_research_questionnaire_uses_deepseek_before_doubao(monkeypatch):
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "deepseek-test-key")
    monkeypatch.setenv("DEEPSEEK_MODEL", "deepseek-test")
    monkeypatch.setenv("DOUBAO_API_KEY", "doubao-test-key")
    monkeypatch.setenv("DOUBAO_ENDPOINT_ID", "ep-test")
    calls = []
    response_payload = {
        "title": "AI 助手用户调研问卷",
        "description": "DeepSeek generated survey",
        "sections": [{"section_title": "背景", "questions": [{"id": "Q1", "type": "single_choice", "question_text": "角色？", "options": ["个人"], "required": True}]}],
        "estimated_time_minutes": 5,
        "recommended_channels": ["线上问卷"],
    }

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json.dumps({"choices": [{"message": {"content": json.dumps(response_payload, ensure_ascii=False)}}]}).encode("utf-8")

    def fake_urlopen(request, timeout):
        calls.append({"url": request.full_url, "body": json.loads(request.data.decode("utf-8")), "auth": request.headers["Authorization"]})
        return FakeResponse()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    result = LLMProvider().design_questionnaire(
        {"industry": "AI", "competitors": ["ChatGPT", "DeepSeek"], "focus_areas": ["功能对比"]},
        "了解用户如何比较 AI 助手",
    )

    assert result.provider == "deepseek"
    assert result.data["sections"]
    assert result.tool_calls[0]["name"] == "deepseek_chat_completions"
    assert calls[0]["url"] == "https://api.deepseek.com/chat/completions"
    assert calls[0]["body"]["model"] == "deepseek-test"
    assert calls[0]["auth"] == "Bearer deepseek-test-key"


def test_research_interview_guide_uses_deepseek(monkeypatch):
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "deepseek-test-key")
    monkeypatch.setenv("DEEPSEEK_MODEL", "deepseek-test")
    response_payload = {
        "title": "AI 助手访谈提纲",
        "estimated_duration_minutes": 45,
        "target_profile": "真实用户",
        "phases": [{"phase": "热身", "duration_minutes": 5, "goals": ["确认背景"], "questions": [{"id": "Q1", "text": "请介绍背景", "probe": ""}]}],
        "notes_for_interviewer": "记录原话",
        "dimension_coverage": {"用户画像": ["Q1"]},
    }

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json.dumps({"choices": [{"message": {"content": json.dumps(response_payload, ensure_ascii=False)}}]}).encode("utf-8")

    monkeypatch.setattr("urllib.request.urlopen", lambda request, timeout: FakeResponse())
    result = LLMProvider().design_interview_guide(
        {"industry": "AI", "competitors": ["ChatGPT", "DeepSeek"], "focus_areas": ["用户画像"]},
        "了解用户选择原因",
        interview_count=6,
    )

    assert result.provider == "deepseek"
    assert result.data["phases"]
    assert result.tool_calls[0]["name"] == "deepseek_chat_completions"


def test_research_deepseek_failure_falls_back_to_local_template(monkeypatch):
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    monkeypatch.delenv("DOUBAO_API_KEY", raising=False)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "deepseek-test-key")
    monkeypatch.setenv("DEEPSEEK_MODEL", "deepseek-test")

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return b'{"choices":[{"message":{"content":"not json"}}]}'

    monkeypatch.setattr("urllib.request.urlopen", lambda request, timeout: FakeResponse())
    result = LLMProvider().design_questionnaire(
        {"industry": "AI", "competitors": ["ChatGPT"], "focus_areas": ["功能对比"]},
        "了解用户需求",
    )

    assert result.provider == "mock"
    assert result.used_fallback is True
    assert "DeepSeek questionnaire generation failed" in result.fallback_reason
    assert result.data["sections"]


def test_manual_action_generates_new_report_version(client):
    task = create_demo_task(client)
    response = client.post(
        f"/api/tasks/{task['id']}/manual-actions",
        json={
            "user_text": "这个结论证据不够，请补充来源后重新质检。",
            "selected_text": "权限治理成熟度判断",
        },
    )
    assert response.status_code == 201
    result = response.get_json()
    assert result["interpreted_intent"] == "recheck_qa" or result["interpreted_intent"] == "supplement_source"

    report = client.get(f"/api/tasks/{task['id']}/report").get_json()
    assert report["version"] >= 2

    actions = client.get(f"/api/tasks/{task['id']}").get_json()["manual_actions"]
    assert actions[-1]["status"] == "completed"


def test_context_menu_revision_updates_selected_report_text(client):
    task = create_demo_task(client)
    initial_report = client.get(f"/api/tasks/{task['id']}/report").get_json()
    claims = client.get(f"/api/tasks/{task['id']}/claims").get_json()
    target = next((claim for claim in claims if claim["source_ids"]), claims[0])
    selected_text = target["content"][:160]

    response = client.post(
        f"/api/tasks/{task['id']}/manual-actions",
        json={
            "action": "revise_claim",
            "user_text": "修正结论：这段应降级为待复核判断，并重新搜索补证后再定稿。",
            "selected_text": selected_text,
        },
    )

    assert response.status_code == 201
    payload = response.get_json()
    assert payload["interpreted_intent"] == "revise_claim"
    assert payload["status"] == "completed"

    refreshed_report = client.get(f"/api/tasks/{task['id']}/report").get_json()
    assert refreshed_report["version"] > initial_report["version"]
    report_text = json.dumps(refreshed_report["content"], ensure_ascii=False)
    assert "人工修正待复核" in report_text
    assert "重新搜索补证" in report_text

    task_payload = client.get(f"/api/tasks/{task['id']}").get_json()
    actions = task_payload["manual_actions"]
    assert actions[-1]["interpreted_intent"] == "revise_claim"
    assert actions[-1]["status"] == "completed"
    revision_findings = [finding for finding in task_payload["qa_findings"] if finding["finding_type"] == "manual_revision"]
    assert revision_findings
    assert revision_findings[-1]["manual_review_state"] in {"awaiting_recheck", "system_rechecked", "needs_more_input"}
    sources = client.get(f"/api/tasks/{task['id']}/sources").get_json()
    assert any(source["source_type"] == "manual_input" and "人工修正" in source["title"] for source in sources)


def test_manual_confirmation_updates_exact_claim(client):
    task = create_demo_task(client)
    initial_report = client.get(f"/api/tasks/{task['id']}/report").get_json()
    claims = client.get(f"/api/tasks/{task['id']}/claims").get_json()
    target = next(claim for claim in claims if claim["needs_review"])

    response = client.post(
        f"/api/tasks/{task['id']}/manual-actions",
        json={
            "user_text": "确认这条低置信度结论，并记录为人工确认。",
            "selected_text": target["content"],
            "claim_id": target["id"],
        },
    )

    assert response.status_code == 201
    payload = response.get_json()
    assert payload["claim_id"] == target["id"]
    updated_claims = client.get(f"/api/tasks/{task['id']}/claims").get_json()
    updated = next(claim for claim in updated_claims if claim["id"] == target["id"])
    assert updated["needs_review"] is False
    assert updated["status"] == "confirmed"
    assert updated["confidence"] >= 0.85
    sources = client.get(f"/api/tasks/{task['id']}/sources").get_json()
    confirmation_sources = [source for source in sources if source["source_type"] == "manual_confirmation"]
    assert confirmation_sources
    assert any(source["id"] in updated["source_ids"] for source in confirmation_sources)
    refreshed_report = client.get(f"/api/tasks/{task['id']}/report").get_json()
    assert refreshed_report["version"] > initial_report["version"]


def test_manual_dispute_lowers_confidence_and_creates_finding(client):
    task = create_demo_task(client)
    initial_report = client.get(f"/api/tasks/{task['id']}/report").get_json()
    claims = client.get(f"/api/tasks/{task['id']}/claims").get_json()
    target = next((claim for claim in claims if claim["section"] == "overview"), claims[0])

    response = client.post(
        f"/api/tasks/{task['id']}/manual-actions",
        json={
            "user_text": "我质疑这条结论不准确，请打回重写并降级置信度。",
            "selected_text": target["content"],
            "claim_id": target["id"],
        },
    )

    assert response.status_code == 201
    payload = response.get_json()
    assert payload["interpreted_intent"] == "dispute_claim"
    updated_claims = client.get(f"/api/tasks/{task['id']}/claims").get_json()
    updated = next(claim for claim in updated_claims if claim["id"] == target["id"])
    assert updated["needs_review"] is True
    assert updated["status"] == "needs_review"
    assert updated["confidence"] <= 0.45
    assert "人工质疑" in updated["uncertainty"]
    task_payload = client.get(f"/api/tasks/{task['id']}").get_json()
    manual_disputes = [finding for finding in task_payload["qa_findings"] if finding["finding_type"] == "manual_dispute"]
    assert manual_disputes
    assert manual_disputes[-1]["claim_id"] == target["id"]
    assert manual_disputes[-1]["fix_status"] == "open"
    assert manual_disputes[-1]["manual_review_state"] == "needs_more_input"
    refreshed_report = client.get(f"/api/tasks/{task['id']}/report").get_json()
    assert refreshed_report["version"] > initial_report["version"]


def test_manual_supplement_binds_source_to_claim_and_closes_after_recheck(client):
    task = create_demo_task(client)
    db_path = client.application.config["DATABASE"]
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        source = conn.execute("SELECT id FROM sources WHERE task_id = ? ORDER BY rowid LIMIT 1", (task["id"],)).fetchone()
        claim_id = "manual-supplement-claim"
        finding_id = "manual-supplement-finding"
        conn.execute(
            """
            INSERT INTO claims
            (id, task_id, section, content, confidence, source_ids, generated_agent, needs_review, status, uncertainty, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                claim_id,
                task["id"],
                "overview",
                "人工补证测试结论：需要把补充 URL 绑定回原 claim。",
                0.61,
                json.dumps([source["id"]], ensure_ascii=False),
                "分析 Agent",
                1,
                "needs_review",
                "等待人工补充来源后复核。",
                utc_now_iso(),
            ),
        )
        conn.execute(
            """
            INSERT INTO qa_findings
            (id, task_id, claim_id, severity, reason, target_agent, finding_type, action_hint, meta_json, fix_status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                finding_id,
                task["id"],
                claim_id,
                "medium",
                "测试：该结论需要人工补充来源。",
                "分析 Agent",
                "missing_source",
                "补充 URL 后重新质检。",
                json.dumps({"repair_action": "manual_supplement"}, ensure_ascii=False),
                "open",
                utc_now_iso(),
            ),
        )

    response = client.post(
        f"/api/tasks/{task['id']}/qa/findings/{finding_id}/repair",
        json={
            "action": "manual_supplement",
            "user_text": "来源链接：https://example.com/qa-proof 这份材料能证明该结论。",
        },
    )

    assert response.status_code == 201
    assert response.get_json()["status"] == "completed"
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        claim = conn.execute("SELECT * FROM claims WHERE id = ?", (claim_id,)).fetchone()
        source_ids = json.loads(claim["source_ids"])
        manual_source = conn.execute(
            "SELECT * FROM sources WHERE task_id = ? AND source_type = 'manual_url' ORDER BY collected_at DESC LIMIT 1",
            (task["id"],),
        ).fetchone()
        finding = conn.execute("SELECT fix_status, meta_json FROM qa_findings WHERE id = ?", (finding_id,)).fetchone()
    assert manual_source["id"] in source_ids
    assert claim["confidence"] >= 0.72
    assert claim["needs_review"] == 1
    assert finding["fix_status"] == "fixed"
    finding_meta = json.loads(finding["meta_json"])
    assert finding_meta["manual_source_id"] == manual_source["id"]
    assert finding_meta["manual_review_state"] == "system_rechecked"
    task_payload = client.get(f"/api/tasks/{task['id']}").get_json()
    api_finding = next(item for item in task_payload["qa_findings"] if item["id"] == finding_id)
    assert api_finding["manual_review_state"] == "system_rechecked"


def test_reportable_claim_requires_source():
    with pytest.raises(ValidationError):
        ReportableClaim(
            id="claim_without_source",
            task_id="task",
            section="overview",
            content="无来源结论不能进入报告。",
            confidence=0.5,
            source_ids=[],
            generated_agent="分析 Agent",
            created_at=utc_now_iso(),
        )


def test_upload_rejects_unsupported_extension(client):
    task = create_demo_task(client)
    response = client.post(
        "/api/uploads",
        data={
            "task_id": task["id"],
            "file": (io.BytesIO(b"<html></html>"), "unsafe.html"),
        },
        content_type="multipart/form-data",
    )
    assert response.status_code == 400


def test_upload_registers_text_source(client):
    task = create_demo_task(client)
    response = client.post(
        "/api/uploads",
        data={
            "task_id": task["id"],
            "file": (io.BytesIO("用户访谈脱敏摘要".encode("utf-8")), "interview.md"),
        },
        content_type="multipart/form-data",
    )
    assert response.status_code == 201
    payload = response.get_json()
    assert payload["source_id"]

    sources = client.get(f"/api/tasks/{task['id']}/sources").get_json()
    assert any(source["id"] == payload["source_id"] for source in sources)

    evidence = client.get(f"/api/tasks/{task['id']}/evidence").get_json()
    assert any(chunk["source_id"] == payload["source_id"] for chunk in evidence)

    claims = client.get(f"/api/tasks/{task['id']}/claims").get_json()
    assert any(claim["generated_agent"] == "访谈/问卷整理 Agent" for claim in claims)


def test_deferred_upload_material_starts_workflow_without_cache(client):
    response = client.post(
        "/api/tasks",
        json={
            "industry": "AI 大模型与智能助手",
            "competitors": ["ChatGPT", "DeepSeek"],
            "focus_areas": ["功能对比", "定价", "用户评价", "SWOT"],
            "source_mode": "上传资料",
            "defer_workflow": True,
        },
    )
    assert response.status_code == 201
    task = response.get_json()
    assert task["status"] == "waiting_materials"

    material = (
        "ChatGPT 产品资料：包含网页、数据分析、文件处理和团队协作能力。"
        "DeepSeek 产品资料：包含 API、推理模型、价格页面和开发者文档。"
        "用户评价材料：部分用户关注响应速度、价格和中文使用体验。"
    )
    upload = client.post(
        "/api/uploads",
        data={
            "task_id": task["id"],
            "file": (io.BytesIO(material.encode("utf-8")), "ai-materials.md"),
        },
        content_type="multipart/form-data",
    )
    assert upload.status_code == 201

    started = client.post(f"/api/tasks/{task['id']}/start")
    assert started.status_code == 202
    assert started.get_json()["status"] == "completed"

    sources = client.get(f"/api/tasks/{task['id']}/sources").get_json()
    assert any(source["source_type"] == "uploaded_file" for source in sources)
    assert not any(source.get("raw_content_status") == "cached" for source in sources)
    assert not any(source["source_type"] == "manual_scope" for source in sources)

    report = client.get(f"/api/tasks/{task['id']}/report").get_json()
    assert report["content"]["metrics"]["source_count"] >= 1


def test_pdf_upload_extracts_text_into_evidence(client):
    task = client.post(
        "/api/tasks",
        json={
            "industry": "AI 大模型与智能助手",
            "competitors": ["ChatGPT"],
            "focus_areas": ["功能对比"],
            "source_mode": "上传资料",
            "defer_workflow": True,
        },
    ).get_json()

    ensure_pdf_dependency_paths()
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    buffer = io.BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=A4)
    pdf.drawString(72, 760, "ChatGPT uploaded PDF evidence for product and pricing material.")
    pdf.save()
    buffer.seek(0)

    response = client.post(
        "/api/uploads",
        data={
            "task_id": task["id"],
            "file": (buffer, "chatgpt-material.pdf"),
        },
        content_type="multipart/form-data",
    )
    assert response.status_code == 201
    source_id = response.get_json()["source_id"]
    evidence = client.get(f"/api/tasks/{task['id']}/evidence").get_json()
    assert any(
        chunk["source_id"] == source_id and "uploaded PDF evidence" in chunk["excerpt"]
        for chunk in evidence
    )


def test_report_respects_selected_focus_areas(client):
    response = client.post(
        "/api/tasks",
        json={
            "industry": "待识别行业",
            "competitors": ["比亚迪", "小鹏"],
            "focus_areas": ["功能对比", "定价", "SWOT", "改进建议"],
            "source_mode": "缓存样例",
        },
    )
    assert response.status_code == 201
    task = response.get_json()
    report = client.get(f"/api/tasks/{task['id']}/report").get_json()
    section_keys = [section["key"] for section in report["content"]["sections"]]
    assert section_keys == ["feature_tree", "pricing_model", "swot"]
    assert "user_persona" not in section_keys
    assert "reviews" not in section_keys
    assert "recommendations" not in section_keys


def test_report_content_hides_trace_and_template_terms(client):
    task = create_demo_task(client)
    report = client.get(f"/api/tasks/{task['id']}/report").get_json()
    payload = str(report["content"])
    assert "source_id" not in payload
    assert "Trace" not in payload
    assert "证据状态" not in payload
    assert "{{" not in payload


def test_report_content_contains_target_report_structures(client):
    task = create_demo_task(client)
    report = client.get(f"/api/tasks/{task['id']}/report").get_json()
    content = report["content"]
    for key in [
        "executive_cards",
        "methodology",
        "source_reliability",
        "feature_scores",
        "score_dimensions",
        "pricing_comparison",
        "pricing_facts",
        "api_cost_data",
        "positioning_map",
        "review_summary",
        "decision_matrix",
        "scenario_recommendations",
        "key_insights",
        "fact_notes",
        "risk_controls",
        "source_catalog",
        "dimension_profile",
        "chart_data",
    ]:
        assert key in content
    assert all(card["evidence_refs"] or card["status"] == "未形成判断" for card in content["executive_cards"])
    assert not any("搜索词" in card["verdict"] or "搜索结果" in card["verdict"] for card in content["executive_cards"])
    assert content["source_catalog"]


def test_report_dimensions_follow_industry_not_chat_ai_template(client):
    response = client.post(
        "/api/tasks",
        json={
            "industry": "新能源汽车与智能汽车",
            "competitors": ["比亚迪", "小鹏"],
            "focus_areas": ["功能对比", "定价", "用户评价", "SWOT"],
            "source_mode": "缓存样例",
        },
    )
    assert response.status_code == 201
    task = response.get_json()
    report = client.get(f"/api/tasks/{task['id']}/report").get_json()
    content = report["content"]
    profile = content["dimension_profile"]

    assert profile["industry_bucket"] == "automotive"
    assert profile["show_api_cost"] is False
    assert "车型" in profile["price_metric_label"]
    assert content["api_cost_data"]["enabled"] is False
    assert not any(row["dimension"] == "API 成本效率" for row in content["score_dimensions"])
    assert any(row["dimension"] == "三电/续航" for row in content["score_dimensions"])


def test_ai_dimension_profile_keeps_api_cost_for_chatgpt_deepseek(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "mock")
    orchestrator = Orchestrator(tmp_path / "dimension-profile.db", ROOT / "data" / "demo_dataset.json")
    profile = orchestrator._build_dimension_profile(
        {"id": "task", "industry": "AI 大模型与智能助手", "focus_areas_json": "[]"},
        ["ChatGPT", "DeepSeek"],
    )
    assert profile["industry_bucket"] == "ai"
    assert profile["show_api_cost"] is True
    assert any(item["name"] == "API 成本效率" for item in profile["score_dimensions"])


def test_report_pdf_endpoint_returns_pdf(client):
    task = create_demo_task(client)
    response = client.get(f"/api/tasks/{task['id']}/report/pdf")
    assert response.status_code == 200
    assert response.headers["Content-Type"].startswith("application/pdf")
    assert response.data[:4] == b"%PDF"


def test_pricing_section_uses_business_wording(client):
    task = create_demo_task(client)
    report = client.get(f"/api/tasks/{task['id']}/report").get_json()
    pricing = next(section for section in report["content"]["sections"] if section["key"] == "pricing_model")
    payload = str(pricing)
    assert "价格区间" in payload or "套餐" in payload or "定价" in payload
    assert "时间敏感信息" not in payload


def test_doubao_flow_records_provider_without_leaking_key(tmp_path, monkeypatch):
    test_key = "test-key"
    monkeypatch.setenv("LLM_PROVIDER", "doubao")
    monkeypatch.setenv("DOUBAO_API_KEY", test_key)
    monkeypatch.setenv("DOUBAO_ENDPOINT_ID", "ep-test")

    class FakeResponse:
        def __init__(self, content):
            self._content = content

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json_bytes(self._content)

    def fake_urlopen(request, timeout):
        body = json.loads(request.data.decode("utf-8", errors="replace"))
        user_prompt = body["messages"][1]["content"]
        if "搜索规划器" in user_prompt:
            content = (
                '{"summary":"已生成模块化搜索计划。","queries":['
                '{"competitor":"飞书","module":"官网/功能","query":"飞书 官网 产品 功能"},'
                '{"competitor":"飞书","module":"定价","query":"飞书 定价 套餐 官方"},'
                '{"competitor":"Notion","module":"官网/功能","query":"Notion 官网 产品 功能"},'
                '{"competitor":"Airtable","module":"官网/功能","query":"Airtable 官网 产品 功能"}'
                ']}'
            )
        elif "采集 Agent" in user_prompt:
            content = '{"summary":"采集来源覆盖功能和定价。","covered_modules":["功能对比","定价"],"search_gaps":[],"next_queries":[]}'
        elif "报告撰写 Agent" in user_prompt:
            content = (
                '{"summary":"报告已按用户选择模块生成。","sections":['
                '{"key":"feature_tree","title":"功能对比","body":"功能对比聚焦业务范围和核心能力。","claims":["飞书覆盖协同办公能力。"]},'
                '{"key":"pricing_model","title":"定价对比","body":"定价对比聚焦套餐和限制条件。","claims":["Notion 存在套餐分层线索。"]},'
                '{"key":"reviews","title":"用户评价","body":"用户评价来自公开评价摘要。","claims":["公开评价提到迁移成本。"]},'
                '{"key":"swot","title":"SWOT","body":"SWOT 基于来源归纳。","claims":["优势来自一体化能力。"]}'
                ']}'
            )
        elif "质检 Agent" in user_prompt:
            content = '{"passed":true,"findings":[],"summary":"复核通过"}'
        else:
            content = (
                '{"claims":[{"section":"feature_tree","content":"飞书覆盖协同办公、文档和会议等能力。",'
                '"confidence":0.82,"source_ids":["'
                + current_source_id
                + '"],"needs_review":false,"status":"reportable","uncertainty":""}]}'
            )
        return FakeResponse(content)

    def json_bytes(content):
        return json.dumps(
            {
                "choices": [{"message": {"content": content}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 6},
            }
        ).encode("utf-8")

    current_source_id = ""

    def fake_generate_claims(self, task, evidence):
        nonlocal current_source_id
        current_source_id = evidence[0]["source_id"]
        return original_generate_claims(self, task, evidence)

    original_generate_claims = LLMProvider.generate_claims
    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    monkeypatch.setattr("llm_provider.LLMProvider.generate_claims", fake_generate_claims)
    app = create_app({"TESTING": True, "DATABASE": str(tmp_path / "doubao-flow.db"), "WORKFLOW_ASYNC": False})
    test_client = app.test_client()

    task = create_demo_task(test_client)
    logs = test_client.get(f"/api/tasks/{task['id']}/logs").get_json()
    serialized = str(logs)
    assert "doubao_chat_completions" in serialized
    assert any(log["model_provider"] == "doubao" for log in logs)
    assert any(log["agent_name"] == "采集 Agent" and log["model_provider"] == "doubao" for log in logs)
    assert test_key not in serialized


def test_realtime_collection_logs_doubao_query_plan(tmp_path, monkeypatch):
    test_key = "test-key"
    monkeypatch.setenv("LLM_PROVIDER", "doubao")
    monkeypatch.setenv("DOUBAO_API_KEY", test_key)
    monkeypatch.setenv("DOUBAO_ENDPOINT_ID", "ep-test")

    class FakeResponse:
        def __init__(self, content):
            self._content = content

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json.dumps(
                {
                    "choices": [{"message": {"content": self._content}}],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 6},
                }
            ).encode("utf-8")

    def fake_urlopen(request, timeout):
        body = json.loads(request.data.decode("utf-8", errors="replace"))
        prompt = body["messages"][1]["content"]
        if "搜索规划器" in prompt:
            content = (
                '{"summary":"豆包已生成搜索计划。","queries":['
                '{"competitor":"chatgpt","module":"定价","query":"chatgpt pricing plan"},'
                '{"competitor":"豆包","module":"定价","query":"豆包 定价 套餐"}'
                ']}'
            )
        elif "采集 Agent" in prompt:
            content = '{"summary":"采集覆盖定价线索。","covered_modules":["定价"],"search_gaps":[],"next_queries":[]}'
        elif "质检 Agent" in prompt:
            content = '{"passed":true,"findings":[],"summary":"复核通过"}'
        elif "报告撰写 Agent" in prompt:
            content = '{"summary":"报告已生成。","sections":[]}'
        else:
            content = (
                '{"claims":[{"section":"pricing_model","content":"公开搜索结果出现定价线索。",'
                '"confidence":0.72,"source_ids":["source-placeholder"],"needs_review":false,'
                '"status":"reportable","uncertainty":""}]}'
            )
        return FakeResponse(content)

    def fake_collect(self, urls, task_prefix):
        return [], [{"url": url, "reason": "blocked in test"} for url in urls]

    def fake_search(self, query, task_prefix, start_index=0, limit=4, block_hosts=None):
        name = "豆包" if "豆包" in query else "ChatGPT"
        url = "https://www.doubao.com/" if name == "豆包" else "https://chatgpt.com/pricing"
        excerpt = f"搜索词：{query}。公开页面包含{name}定价、功能和产品线索。"
        return [
            WebSourceDraft(
                source_id=f"{task_prefix}_volc_{start_index + 1:02d}",
                source_type="volc_search_result",
                title=name,
                url=url,
                author_site=url.split("/")[2],
                excerpt=excerpt,
                credibility="low",
                chunks=chunk_text(excerpt, chunk_size=700, overlap=80),
                provider="volc_search",
                search_query=query,
            )
        ]

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    monkeypatch.setattr("collector.WebCollector.collect", fake_collect)
    monkeypatch.setattr("collector.VolcWebSearchClient.search", fake_search)
    app = create_app({"TESTING": True, "DATABASE": str(tmp_path / "doubao-query-plan.db"), "WORKFLOW_ASYNC": False})
    test_client = app.test_client()

    response = test_client.post(
        "/api/tasks",
        json={
            "industry": "AI 大模型与智能助手",
            "competitors": ["chatgpt", "豆包"],
            "focus_areas": ["定价"],
            "source_mode": "实时采集",
        },
    )

    assert response.status_code == 201
    task = response.get_json()
    logs = test_client.get(f"/api/tasks/{task['id']}/logs?agent=采集 Agent").get_json()
    serialized = str(logs)
    assert "collection_query_plan" in serialized
    assert "doubao_chat_completions" in serialized
    assert "定价" in serialized
    assert test_key not in serialized


def test_swot_outputs_direct_items(client):
    task = create_demo_task(client)
    report = client.get(f"/api/tasks/{task['id']}/report").get_json()
    swot = next(section for section in report["content"]["sections"] if section["key"] == "swot")
    payload = " ".join(claim["content"] for claim in swot["claims"])
    assert "优势：" in payload
    assert "劣势：" in payload
    assert "机会：" in payload
    assert "威胁：" in payload
    assert "再下判断" not in payload
    assert report["content"]["competitor_swot"]


def test_swot_is_competitor_specific(client):
    task = create_demo_task(client)
    report = client.get(f"/api/tasks/{task['id']}/report").get_json()
    competitor_swot = report["content"]["competitor_swot"]
    assert len(competitor_swot) >= 2
    joined = [" ".join(values.values()) for values in competitor_swot.values()]
    assert len(set(joined)) > 1


def test_recommendations_section_is_removed(client):
    response = client.post(
        "/api/tasks",
        json={
            "industry": "协同办公与知识管理",
            "competitors": ["飞书", "Notion"],
            "focus_areas": ["功能对比", "定价", "SWOT", "改进建议"],
            "source_mode": "缓存样例",
        },
    )
    assert response.status_code == 201
    task = response.get_json()
    report = client.get(f"/api/tasks/{task['id']}/report").get_json()
    section_keys = [section["key"] for section in report["content"]["sections"]]
    assert "recommendations" not in section_keys
    assert "company_recommendations" not in report["content"]


def test_report_frontend_defaults_to_full_text_without_board_toggle():
    html = (ROOT / "templates" / "index.html").read_text(encoding="utf-8")
    js = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
    assert 'id="reportBoards" class="report-grid hidden"' in html
    assert 'id="fullReport" class="full-report"' in html
    assert 'id="toggleReportModeButton"' in html and "hidden" in html
    assert 'setHidden("#reportBoards", true)' in js
    assert 'setHidden("#fullReport", false)' in js


def test_react_deep_analysis_belongs_to_analysis_agent(client, monkeypatch):
    calls = []

    def fake_run_react_report(task, sources, claims, output_dir):
        calls.append({"task": task["id"], "source_count": len(sources), "claim_count": len(claims)})
        titles = [
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
            "结语",
        ]
        sections = [
            {
                "key": f"react_{index}",
                "title": title,
                "body": f"ChatGPT 和 Notion 的第 {index} 章分析。",
                "markdown": f"ChatGPT 和 Notion 的第 {index} 章分析。",
            }
            for index, title in enumerate(titles, 1)
        ]
        return SimpleNamespace(
            enabled=True,
            provider="deepseek-react",
            markdown="\n\n".join(f"## {section['title']}\n{section['body']}" for section in sections),
            sections=sections,
            tool_calls=[{"name": "fake_react_analysis", "result": "ok"}],
            screenshots=[],
            token_input=11,
            token_output=22,
            fallback_reason="",
        )

    monkeypatch.setattr("orchestrator.run_react_report", fake_run_react_report)

    task = create_demo_task(client)
    assert task["status"] == "completed"
    assert len(calls) == 1

    logs = client.get(f"/api/tasks/{task['id']}/logs").get_json()
    analysis_log = next(log for log in logs if log["agent_name"] == "分析 Agent")
    report_log = next(log for log in logs if log["agent_name"] == "报告 Agent")
    assert analysis_log["model_provider"] == "deepseek-react"
    assert "fake_react_analysis" in str(analysis_log["tool_calls"])
    assert "fake_react_analysis" not in str(report_log["tool_calls"])
    assert "doubao_report_rewrite" not in str(report_log["tool_calls"])
    assert report_log["model_provider"] != "deepseek-react"
    assert report_log["model_provider"] == "report-renderer"

    report = client.get(f"/api/tasks/{task['id']}/report").get_json()
    content = report["content"]
    assert content["metrics"]["analysis_provider"] == "deepseek-react"
    assert len(content["display_sections"]) == 12
    joined_sections = "\n".join(str(section.get("markdown", "")) for section in content["display_sections"])
    for heading in [
        "核心能力对比",
        "商业模式对比",
        "增长策略对比",
        "用户场景对比",
        "SWOT 对比矩阵",
        "差异化、壁垒与避雷对比",
    ]:
        assert heading in joined_sections
    assert "| 维度 |" in joined_sections
    assert "| 场景 |" in joined_sections
    assert content["chart_data"]["radar"]
    for row in content["score_dimensions"]:
        assert row["rationale"]
        assert row.get("evidence_refs") or row.get("section_refs") or row.get("status") == "NA"


def test_manual_source_refreshes_deep_analysis_before_report(client, monkeypatch):
    calls = []

    def fake_run_react_report(task, sources, claims, output_dir):
        calls.append({"source_count": len(sources), "claim_count": len(claims)})
        sections = [
            {
                "key": f"react_{index}",
                "title": f"{index}章测试",
                "body": f"第 {index} 章基于 {len(claims)} 条结论生成。",
                "markdown": f"第 {index} 章基于 {len(claims)} 条结论生成。",
            }
            for index in range(1, 13)
        ]
        return SimpleNamespace(
            enabled=True,
            provider="doubao-react",
            markdown="\n\n".join(f"## {section['title']}\n{section['body']}" for section in sections),
            sections=sections,
            tool_calls=[{"name": "fake_manual_refresh", "result": "ok"}],
            screenshots=[],
            token_input=9,
            token_output=18,
            fallback_reason="",
        )

    monkeypatch.setattr("orchestrator.run_react_report", fake_run_react_report)

    task = create_demo_task(client)
    initial_calls = len(calls)
    response = client.post(
        f"/api/tasks/{task['id']}/manual-actions",
        json={
            "user_text": "请补充来源 https://example.com/product 后重新分析和质检。",
            "selected_text": "需要新的来源。",
        },
    )

    assert response.status_code == 201
    assert response.get_json()["interpreted_intent"] == "supplement_source"
    assert len(calls) >= initial_calls + 1
    logs = client.get(f"/api/tasks/{task['id']}/logs").get_json()
    analysis_logs = [log for log in logs if log["agent_name"] == "分析 Agent"]
    report_logs = [log for log in logs if log["agent_name"] == "报告 Agent"]
    assert any("refresh_analysis_artifact" in str(log["tool_calls"]) for log in analysis_logs)
    assert "fake_manual_refresh" not in str(report_logs[-1]["tool_calls"])


def test_report_frontend_uses_linked_toc_and_app_market_visual_panel():
    js = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
    html = (ROOT / "templates" / "index.html").read_text(encoding="utf-8")
    assert "Deep Report" not in js
    assert "renderDeepReportHeader" not in js
    assert "function renderReportToc" in js
    assert "function normalizeMarkdownText" in js
    assert "function parseMarkdownTableRow" in js
    assert "document.createElementNS" in js
    assert "豆包结构化" in js and "DeepSeek ReAct" in js
    assert "市场与赛道" in html and "商业模式与定价" in html
    assert "href: `#${sectionAnchorId(index)}`" in js
    assert "buildRenderedReportItems" in js
    assert "可视化总览" in js
    assert "isConclusionSection" in js
    assert "function reportSectionOrdinal" in js
    assert "function syncMarkdownSectionNumbers" in js
    assert "syncMarkdownSectionNumbers(section.markdown, index)" in js
    assert "reportSectionOrdinal(section, index)" in js
    assert "reportSectionTitle(item, index, true)" in js
    assert "`${index + 1}. ${section.title}`" not in js
    assert "sectionOrdinal(index)" in js
    assert "参考文献（来源链接）" in js
    assert "renderSourceCatalog(content.source_catalog || [], index)" in js
    assert "reference-list" in js
    assert "sourceCitationNumber(row, url)" in js
    assert "function appendTrailingCitations" in js
    assert "citationSourcesForText(text)" in js
    assert 'renderMarkdownInline("p", text, { appendCitations: true })' in js
    assert 'suffix.replace(/、+/g, "")' in js
    assert "^\\s*、+\\s*$" in js
    visual_block = js[js.index("function renderVisualSection"):js.index("function renderScoreHeatmap")]
    assert "renderScoreHeatmap" in visual_block
    assert "renderApiCostData" in visual_block
    assert "renderRadar" in visual_block
    assert "renderAppMarketData" in visual_block
    assert ": `#${value}`" not in js
    assert "renderReferenceSourceLine" in js
    assert "renderReferenceSourceInline" in js
    assert "isReferenceSourceTitle" in js
    assert "ordered-table" in js
    assert "renderTableCellContent" in js
    assert "function appendTextWithExplicitSourceRefs" in js
    assert "function sourceRecordForCitationNumber" in js
    assert "sourceCitationLabel(source, ref)" in js
    assert "function appendTextWithBracketedSourceRefs" in js
    assert "function sourceNumbersFromBracketContent" in js
    assert "function cleanSourceRefToken" in js
    assert "function hasSourceCue" in js
    assert "findingStatusLabel" in js
    assert "findingRecheckText" in js
    assert "manual_review_state" in js
    assert "待复核/复检中" in js
    assert "系统复检通过" in js
    assert "人工确认已修复" in js
    assert 'finding.fix_status === "fixed"' in js
    assert "【" in js and "［" in js
    assert "allowPlainNumber" in js
    assert "bracket !== \"(\" && bracket !== \"（\"" in js
    assert "isInternalSourceIdToken(content)" in js
    assert "来源|出处)[：:]?\\\\s*" in js
    assert "appendTextWithBracketedSourceRefs(node, part)" in js
    assert "function stripInternalSourceIdsForDisplay" in js
    assert "function replaceInternalSourceIdsWithCatalogRefs" in js
    assert "function catalogSourceRecordForRef" in js
    assert "function hasSourceCueForDisplay" in js
    assert "(?![A-Za-z0-9_])" in js
    assert "manualQaAction" in js
    assert "revise_claim" in js
    assert "actionMap" in js
    manual_form_block = js[js.index("async function submitManualForm"):js.index("function toggleReportMode")]
    assert "manualSubmitting" in js
    assert "manualSubmitButton" in (ROOT / "templates" / "index.html").read_text(encoding="utf-8")
    assert "$(\"#manualSubmitButton\").addEventListener(\"click\"" in js
    assert "submitManualTextForTask" in js
    assert "repairQaFindingForTask" in js
    assert "backgroundTask.finally" in manual_form_block
    assert "const findingId = state.manualFindingId" in manual_form_block
    assert "closeManualModal();" in manual_form_block
    assert "show(\"#boardView\");" in manual_form_block
    assert "animatePlan(true);" in manual_form_block
    assert "await submitManualText(" not in manual_form_block
    assert "await repairQaFinding(" not in manual_form_block
    assert "重跑中..." in js
    assert "质疑/打回" in js
    assert "dispute_claim" in js
    assert "manual_confirmation" in js
    assert "renderPositioningMap" not in visual_block
    assert "renderPricingBars" not in visual_block
    assert "renderReviewSummary" not in visual_block


def test_parse_appark_competitor_dashboard_text():
    text = """应用排行榜
操作
应用
下载量
收入额
免费榜排名
付费榜排名
排行榜排名
DeepSeek R1 的聊天 AI
AppZone AI
12.9K
13000美元
157
-
108
豆包 - 抖音名为AI智能助手
北京春天智云科技有限公司
1.276亿
14.9万美元
2
-
-
ChatGPT
OpenAI OpCo, LLC
13亿
33亿美元
5
-
3
中文 (简体)
"""
    result = parse_appark_text(text, ["deepseek", "豆包", "ChatGPT"])

    assert result["enabled"] is True
    assert len(result["rows"]) == 3
    by_name = {row["competitor"]: row for row in result["rows"]}
    assert by_name["ChatGPT"]["downloads_value"] == pytest.approx(1_300_000_000)
    assert by_name["豆包"]["revenue_usd"] == pytest.approx(149_000)
    assert by_name["deepseek"]["overall_rank"] == 108


def test_pdf_template_uses_toc_sections_and_app_market_visualization():
    pdf_source = (ROOT / "report_pdf.py").read_text(encoding="utf-8")
    block = pdf_source[pdf_source.index("def render_competitive_report_pdf") :]
    assert "markdown_to_pdf_flows" in pdf_source
    assert "_flush_md_bullet_table" in pdf_source
    assert "ROWBACKGROUNDS" in pdf_source
    assert 'P("目录", "H1")' in block
    assert "_rendered_report_items" in pdf_source
    assert "_source_refs_flow" in pdf_source
    assert "_build_pdf_source_ref_urls" in pdf_source
    assert "_source_catalog_flows" in pdf_source
    assert "_trailing_pdf_citations" in pdf_source
    assert "append_citations: bool = False" in pdf_source
    assert "def _pdf_source_ref_markup" in pdf_source
    assert "result[match.group(1)]" in pdf_source
    assert "source_catalog=source_catalog" in pdf_source
    assert "_report_item_section_index" in pdf_source
    assert "可视化总览" in block
    assert "参考文献（来源链接）" in block
    assert 'item.get("type") in {"visual", "sources"}' in block
    assert "_source_catalog_flows(content, len(report_items) + 1)" in block
    assert 'f"{index}.1 评分矩阵"' in block
    assert 'f"{index}.2 API 成本柱状图"' in block
    assert 'f"{index}.3 能力雷达图"' in block
    assert "App 市场表现" in block
    assert "score_table = [[" not in block
    assert "可视化 1：评分矩阵" not in block
    assert "_section_title(title, section_index, True)" in block
    assert "_sync_section_markdown_numbering(body, section_index)" in block
    assert "source_catalog=source_catalog" in block
    assert 'P(f"{index}. {title}", "H1")' not in block
    assert "story.append(PageBreak())" not in block
    assert 'href="{safe_url}"' in pdf_source
    assert 'f"#{row.get' not in block
    assert "PositioningMap(" not in block
    assert "SWOT 分析" not in block
    assert "关键洞察" not in block
    assert "_write_report_pdf_file" in (ROOT / "orchestrator.py").read_text(encoding="utf-8")


def test_pdf_inline_numeric_references_are_linked():
    from report_pdf import _build_pdf_source_ref_labels, _build_pdf_source_ref_urls, _md_inline

    content = {
        "source_catalog": [
            {"id": "src_a", "ref": "S9", "url_or_path": "https://example.com/a"},
            {"id": "src_b", "ref": "S29", "url_or_path": "https://example.com/b"},
            {"id": "54e81a36_ga_03_03", "ref": "S76", "url_or_path": "https://example.com/c"},
            {"id": "54e81a36_volc_189", "ref": "S32", "url_or_path": "https://example.com/d"},
        ]
    }
    source_ref_urls = _build_pdf_source_ref_urls(content)
    source_ref_labels = _build_pdf_source_ref_labels(content)

    assert source_ref_urls["S9"] == "https://example.com/a"
    assert source_ref_urls["9"] == "https://example.com/a"
    assert source_ref_urls["[9]"] == "https://example.com/a"
    assert source_ref_urls["54e81a36_ga_03_03"] == "https://example.com/c"
    assert source_ref_labels["54e81a36_ga_03_03"] == "76"
    rendered = _md_inline(
        "model commercialization [9][29] plus [54e81a36_ga_03_03] and [54e81a36_volc_189]",
        url_refs={},
        source_catalog=content["source_catalog"],
        append_citations=False,
    )

    assert 'href="https://example.com/a"' in rendered
    assert 'href="https://example.com/b"' in rendered
    assert 'href="https://example.com/c"' in rendered
    assert 'href="https://example.com/d"' in rendered
    assert '<super>[9]</super>' in rendered
    assert '<super>[29]</super>' in rendered
    assert '<super>[76]</super>' in rendered
    assert '<super>[32]</super>' in rendered
    assert "54e81a36_ga_03_03" not in rendered
    assert "54e81a36_volc_189" not in rendered

    cue_rendered = _md_inline(
        "豆包[来源: 54e81a36_ga_03_03]，DeepSeek[来源：54e81a36_volc_189。]，未知[来源: 54e81a36_search_404。]",
        url_refs={},
        source_catalog=content["source_catalog"],
        append_citations=False,
    )
    assert '<super>[76]</super>' in cue_rendered
    assert '<super>[32]</super>' in cue_rendered
    assert "54e81a36" not in cue_rendered
    assert "来源" not in cue_rendered
    assert "search_404" not in cue_rendered


def test_analysis_artifact_preserves_markdown_and_marks_unsupported_terms():
    markdown = "# 竞品调研报告：测试\n\n## 一、报告概述（Executive Summary）\n### 1.1 核心发现\nChatGPT 的 GPT-5.5 系列模型能力领先。\n\n## 二、市场与赛道分析（Market Context）\n2026 年市场规模预计 680 亿元。"
    assert "\n### 1.1 核心发现\n" in sanitize_markdown_text(markdown)
    footer = sanitize_markdown_text("*本报告由竞争情报分析师基于公开信息编制，仅供参考，不构成投资建议。*")
    assert "MOSS团队" in footer
    assert "竞争情报分析师" not in footer

    orch = object.__new__(Orchestrator)
    guarded = orch._guard_analysis_markdown(
        markdown,
        [{"title": "OpenAI pricing", "excerpt": "ChatGPT Plus costs $20/month.", "url_or_path": "https://openai.com/chatgpt/pricing", "author_site": "openai.com"}],
        [{"content": "ChatGPT Plus costs $20/month.", "source_ids": ["src1"]}],
    )
    assert "GPT-5.5（待核实）" in guarded
    assert "证据边界" in guarded


def test_full_report_has_manual_and_recheck_actions():
    js = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
    assert "function renderClaimActions" in js
    assert "function claimTypeLabel" in js
    assert "renderFullReportSection" in js
    assert "采集提供方" in js
    assert "搜索日志编号" in js
    assert "确认该结论" in js
    assert "补充复查说明" in js
    assert "重新质检" in js
    assert "claim_id: claimId" in js
    assert "withButtonLoading" in js
    assert "showToast" in js


def test_board_summary_removed_and_markdown_bullets_render_as_tables():
    js = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
    board_block = js[js.index("function renderBoard") : js.index("function renderInlinePlanFromDag")]
    assert "board-summary" not in board_block
    assert "canvas.replaceChildren(stages)" in board_block
    assert "人工复核工作台" in js
    assert "质检发现问题，已进入人工复核工作台" in js
    assert "复核处理中" in js
    assert "cleanMarkdownLinkLabel" in js
    assert "isEvidenceStyleLinkLabel" in js
    assert 'className: "bullet-table"' in js
    assert 'show("#reportView")' in js
    assert 'state.task && state.task.status === "completed"' in js
    assert 'bulletRows.map((item) => [renderMarkdownInline("span", item, { appendCitations: true })])' in js

    css = (ROOT / "static" / "styles.css").read_text(encoding="utf-8")
    assert ".markdown-report .bullet-table" in css
    assert "tr:nth-child(odd)" in css


def test_provider_status_reports_provider_timeouts(tmp_path, monkeypatch):
    monkeypatch.setenv("REACT_AGENT_PROVIDER", "auto")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "deepseek-test-key")
    monkeypatch.setenv("DEEPSEEK_MODEL", "deepseek-test")
    monkeypatch.setenv("ZHIPU_API_KEY", "zhipu-test-key")
    monkeypatch.setenv("ZHIPU_MODEL", "your-zhipu-model")
    monkeypatch.setenv("DOUBAO_API_KEY", "doubao-test-key")
    monkeypatch.setenv("DOUBAO_ENDPOINT_ID", "ep-test")
    monkeypatch.setenv("DEEPSEEK_REACT_MAX_SECONDS", "900")
    monkeypatch.setenv("ZHIPU_REACT_MAX_SECONDS", "600")
    monkeypatch.setenv("DOUBAO_REACT_MAX_SECONDS", "450")
    app = create_app({"TESTING": True, "DATABASE": str(tmp_path / "provider.db"), "WORKFLOW_ASYNC": False})
    payload = app.test_client().get("/api/provider-status").get_json()["react_report"]
    assert payload["configured_provider"] == "deepseek-react"
    assert payload["react_timeout_seconds"]["deepseek-react"] == 900
    assert payload["react_timeout_seconds"]["zhipu-react"] == 600
    assert payload["react_timeout_seconds"]["doubao-react"] == 450
    assert [item["provider"] for item in payload["preferred_order"]][:3] == ["deepseek-react", "zhipu-react", "doubao-react"]


def test_initial_workflow_records_top_level_langgraph_trace(client):
    task = create_demo_task(client)
    logs = client.get(f"/api/tasks/{task['id']}/logs").get_json()
    workflow_run = next(
        log for log in logs
        if log["agent_name"] == "编排层" and log["model_provider"] == "langgraph_stategraph"
    )
    trace_nodes = [
        call["workflow_node"]
        for call in workflow_run["tool_calls"]
        if call.get("name") == "workflow_node_trace"
    ]
    assert trace_nodes[:4] == ["prepare", "collect", "analyze", "qa_review"]
    assert "report" in trace_nodes
    assert trace_nodes[-1] == "complete"

    events = client.get(f"/api/tasks/{task['id']}/events").get_json()
    workflow_events = [
        event for event in events
        if event["meta"].get("workflow_engine") == "langgraph_stategraph"
    ]
    assert workflow_events
    assert {event["meta"].get("workflow_node") for event in workflow_events} >= {"collect", "analyze", "qa_review", "report"}

    report = client.get(f"/api/tasks/{task['id']}/report").get_json()
    assert report["content"]["metrics"]["workflow_engine"] == "langgraph_stategraph"


def test_deepseek_direct_thinking_keeps_quality_path_and_marks_execution_mode(tmp_path, monkeypatch):
    monkeypatch.setenv("REACT_REPORT_ENABLED", "1")
    monkeypatch.setenv("REACT_AGENT_PROVIDER", "deepseek")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "deepseek-test-key")
    monkeypatch.setenv("DEEPSEEK_DIRECT_THINKING_MODE", "1")
    monkeypatch.delenv("ZHIPU_API_KEY", raising=False)
    monkeypatch.delenv("DOUBAO_API_KEY", raising=False)

    def fake_direct_report(provider, user_task, max_seconds, tool_calls):
        urls = "\n".join(f"https://example.com/source-{index}" for index in range(1, 8))
        sections = "\n\n".join(
            f"## {title}\n{urls}\n" + ("DeepSeek direct thinking 生成的高密度长报告内容。" * 45)
            for title in react_report_agent.REACT_REPORT_H2
        )
        return f"# 竞品调研报告：DeepSeek direct 测试\n\n{sections}", tool_calls + [{"name": "fake_deepseek_direct", "result": "ok"}]

    monkeypatch.setattr(react_report_agent, "_run_deepseek_direct_thinking_report", fake_direct_report)

    result = react_report_agent.run_react_report(
        {"id": "direct-test", "industry": "AI 工具", "competitors": ["ChatGPT", "DeepSeek"], "focus_areas": []},
        [{"id": "s1", "title": "Source", "url_or_path": "https://example.com/source-1", "excerpt": "公开来源"}],
        [{"id": "c1", "content": "ChatGPT 和 DeepSeek 均有公开材料。", "source_ids": ["s1"]}],
        tmp_path,
    )

    assert result.provider == "deepseek-react"
    assert result.execution_mode == "deepseek_direct_thinking"
    assert any(call.get("deep_report_execution_mode") == "deepseek_direct_thinking" for call in result.tool_calls)
    assert not any(call.get("deep_report_execution_mode") == "stategraph_react_tools" for call in result.tool_calls)


def test_public_docs_and_entrypoint_have_unified_product_port_and_provider_order():
    files = [
        ROOT / "README.md",
        ROOT / "docs" / "DEPLOYMENT.md",
        ROOT / "docs" / "ARCHITECTURE.md",
        ROOT / "docs" / "AGENT_PROTOCOL.md",
        ROOT / "docs" / "REPORT_TARGET_SPEC.md",
        ROOT / ".env.example",
        ROOT / "app.py",
    ]
    combined = "\n".join(path.read_text(encoding="utf-8") for path in files)
    for marker in [
        "第五" + "版",
        "新" + "四版",
        "新" + "三版",
        "新" + "一版",
        "50" + "07",
        "50" + "12",
        "50" + "10",
        "豆包为" + "主",
        "豆包真" + "实",
    ]:
        assert marker not in combined
    assert "MOSS多agent智能竞品分析系统——小莫" in combined
    assert "127.0.0.1:5016" in combined
    assert "DeepSeek -> 智谱 -> 豆包" in combined
    assert "ZHIPU_MODEL=your-zhipu-model" in combined


def test_explicit_deepseek_provider_disables_doubao_react_failover(monkeypatch):
    monkeypatch.setenv("REACT_AGENT_PROVIDER", "deepseek")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "deepseek-test-key")
    monkeypatch.setenv("DOUBAO_API_KEY", "doubao-test-key")
    monkeypatch.setenv("DOUBAO_ENDPOINT_ID", "ep-test")

    providers = _configured_providers()

    assert [item["provider"] for item in providers] == ["deepseek-react"]


def test_zhipu_safety_filter_records_source_ids(monkeypatch):
    monkeypatch.setenv("ZHIPU_SAFETY_FILTER_ENABLED", "1")
    monkeypatch.setenv("ZHIPU_DROP_SAFETY_HIT_SOURCES", "1")
    monkeypatch.delenv("ZHIPU_EXCLUDED_SOURCE_IDS", raising=False)
    sources, meta = _sanitize_zhipu_sources(
        [
            {
                "id": "ga-1",
                "title": "Google Alerts",
                "excerpt": "新闻摘要提到战争、制裁和数据泄漏风险。",
            },
            {
                "id": "safe-1",
                "title": "Product page",
                "excerpt": "官方功能更新与定价说明。",
            },
        ]
    )
    assert meta["filtered_source_count"] == 1
    assert "ga-1" in meta["source_ids"]
    assert "ga-1" in meta["dropped_source_ids"]
    assert "politics_conflict" in meta["categories"]
    assert "cyber_privacy" in meta["categories"]
    assert [source["id"] for source in sources] == ["safe-1"]


def test_zhipu_safety_filter_respects_explicit_source_blocklist(monkeypatch):
    monkeypatch.setenv("ZHIPU_SAFETY_FILTER_ENABLED", "1")
    monkeypatch.setenv("ZHIPU_EXCLUDED_SOURCE_IDS", "block-1")
    sources, meta = _sanitize_zhipu_sources(
        [
            {"id": "block-1", "title": "App Store noisy page", "excerpt": "普通文字"},
            {"id": "keep-1", "title": "Official pricing", "excerpt": "官方定价说明"},
        ]
    )
    assert [source["id"] for source in sources] == ["keep-1"]
    assert "block-1" in meta["dropped_source_ids"]


def test_zhipu_claim_safety_filter_sanitizes_structured_claims(monkeypatch):
    monkeypatch.setenv("ZHIPU_SAFETY_FILTER_ENABLED", "1")
    claims, meta = _sanitize_zhipu_claims(
        [
            {"section": "风险", "content": "公开新闻摘要提到战争和数据泄漏风险。"},
            {"section": "功能", "content": "官方功能更新与定价说明。"},
        ]
    )

    assert meta["filtered_claim_count"] == 1
    assert "politics_conflict" in meta["categories"]
    assert "cyber_privacy" in meta["categories"]
    assert "战争" not in claims[0]["content"]


def test_zhipu_report_structure_coercion_accepts_substantial_model_text():
    urls = "\n".join(
        [
            "https://openai.com/chatgpt/pricing",
            "https://www.doubao.com/",
            "https://www.deepseek.com/",
            "https://api-docs.deepseek.com/quick_start/pricing",
        ]
    )
    raw = "# AI 大模型竞品分析\n\n" + ((f"智谱返回的正文段落，围绕 ChatGPT、豆包和 DeepSeek 展开。{urls}\n\n") * 80)

    fixed = _coerce_zhipu_report_structure(raw, {"industry": "AI 大模型与智能助手"})

    assert _report_completion_reason(fixed, ["ChatGPT"]) == ""


def test_failover_diagnostics_keeps_zhipu_filter_context():
    calls = [{"name": "zhipu_input_safety_filter", "provider": "zhipu-react", "source_ids": ["ga-1"]}]
    diagnostics = _failover_diagnostic_calls("zhipu-react", calls, "BadRequestError 1301 contentFilter")
    assert diagnostics[0]["name"] == "zhipu_input_safety_filter"
    assert diagnostics[-1]["name"] == "react_provider_failover_diagnostics"


def test_short_react_refresh_does_not_replace_richer_artifact(tmp_path, monkeypatch):
    monkeypatch.setenv("REACT_ARTIFACT_REPLACE_MIN_RATIO", "0.85")
    db_path = tmp_path / "artifact-protect.db"
    create_app({"TESTING": True, "DATABASE": str(db_path), "WORKFLOW_ASYNC": False})
    orch = Orchestrator(db_path, ROOT / "data" / "demo_dataset.json")
    task_id = "artifact-task"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO tasks (id, name, industry, competitors_json, websites_json, focus_areas_json, source_mode, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (task_id, "artifact", "AI", "[]", "[]", "[]", "缓存样例", "analyzing", utc_now_iso()),
        )
    long_markdown = "## one\n" + ("rich analysis " * 600)
    short_markdown = "## one\n" + ("short " * 300)
    assert orch._save_analysis_artifact(
        task_id,
        {"provider": "deepseek-react", "analysis_markdown": long_markdown, "sections": [{"title": "one", "body": long_markdown}] * 12},
    )
    assert not orch._save_analysis_artifact(
        task_id,
        {"provider": "doubao-react", "analysis_markdown": short_markdown, "sections": [{"title": "one", "body": short_markdown}] * 12},
    )
    with sqlite3.connect(db_path) as conn:
        count = conn.execute("SELECT COUNT(*) FROM analysis_artifacts WHERE task_id = ?", (task_id,)).fetchone()[0]
        protected = conn.execute("SELECT COUNT(*) FROM agent_events WHERE task_id = ? AND event_type = 'analysis_artifact_protected'", (task_id,)).fetchone()[0]
    assert count == 1
    assert protected == 1


def test_repeated_qa_failure_handoff_is_manual_pending(tmp_path):
    db_path = tmp_path / "manual-pending.db"
    create_app({"TESTING": True, "DATABASE": str(db_path), "WORKFLOW_ASYNC": False})
    orch = Orchestrator(db_path, ROOT / "data" / "demo_dataset.json")
    task_id = "qa-task"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO tasks (id, name, industry, competitors_json, websites_json, focus_areas_json, source_mode, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (task_id, "qa", "AI", "[]", "[]", "[]", "缓存样例", "qa_rework", utc_now_iso()),
        )
        conn.execute(
            "INSERT INTO qa_findings (id, task_id, claim_id, severity, reason, target_agent, fix_status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("finding-1", task_id, "claim-1", "high", "same issue", "分析 Agent", "open", utc_now_iso()),
        )
    orch._handoff_open_findings_to_manual_review(task_id, 3, 2, "claim-1|same")
    with sqlite3.connect(db_path) as conn:
        row = conn.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()
        finding = conn.execute("SELECT fix_status, recheck_result FROM qa_findings WHERE id = 'finding-1'").fetchone()
    assert row[0] == "qa_passed"
    assert finding[0] == "manual_pending"
    assert finding[1]


def test_manual_recheck_is_guarded_while_workflow_active(tmp_path):
    db_path = tmp_path / "busy.db"
    create_app({"TESTING": True, "DATABASE": str(db_path), "WORKFLOW_ASYNC": False})
    orch = Orchestrator(db_path, ROOT / "data" / "demo_dataset.json")
    task_id = "busy-task"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO tasks (id, name, industry, competitors_json, websites_json, focus_areas_json, source_mode, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (task_id, "busy", "AI", "[]", "[]", "[]", "缓存样例", "reanalyzing", utc_now_iso()),
        )
    result = orch.recheck_qa(task_id)
    assert result["status"] == "busy"
    assert result["task_status"] == "reanalyzing"
