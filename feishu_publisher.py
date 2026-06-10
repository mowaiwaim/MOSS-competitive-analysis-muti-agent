from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any


DEFAULT_LARK_CLI_RELATIVE_PATH = (
    "Microsoft\\WinGet\\Packages\\OpenJS.NodeJS.LTS_Microsoft.Winget.Source_8wekyb3d8bbwe\\"
    "node-v24.16.0-win-x64\\lark-cli.cmd"
)


class FeishuPublishError(RuntimeError):
    def __init__(
        self,
        message: str,
        status_code: int = 503,
        tool_calls: list[dict[str, Any]] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.tool_calls = tool_calls or []


@dataclass
class FeishuPublishResult:
    feishu_url: str
    base_token: str
    table_id: str
    form_id: str
    share_token: str
    external_ids: dict[str, str]
    tool_calls: list[dict[str, Any]]


def resolve_feishu_cli_path(cli_path: str | None = None) -> str:
    explicit = (cli_path or os.environ.get("FEISHU_CLI_PATH") or os.environ.get("LARK_CLI_PATH") or "").strip()
    if explicit:
        return os.path.expandvars(explicit)

    local_app_data = os.environ.get("LOCALAPPDATA", "").strip()
    if local_app_data:
        candidate = Path(local_app_data) / DEFAULT_LARK_CLI_RELATIVE_PATH
        if candidate.exists():
            return str(candidate)

    return shutil.which("lark-cli") or shutil.which("feishu") or "lark-cli"


def build_feishu_questions(design: dict[str, Any]) -> list[dict[str, Any]]:
    questions: list[dict[str, Any]] = [
        {
            "type": "text",
            "title": "受访者标识",
            "description": "选填。建议使用 R01、用户组1 等脱敏编号，请勿填写手机号、邮箱或其他敏感信息。",
            "required": False,
        }
    ]
    for section in design.get("sections", []) or []:
        if not isinstance(section, dict):
            continue
        section_title = _clean_text(section.get("section_title"), 80)
        for question in section.get("questions", []) or []:
            if not isinstance(question, dict):
                continue
            item = _map_question(question, section_title)
            if item:
                questions.append(item)
    if len(questions) <= 1:
        raise FeishuPublishError("问卷没有可发布到飞书的题目。", status_code=400)
    return questions


class FeishuQuestionnairePublisher:
    def __init__(
        self,
        cli_path: str | None = None,
        identity: str = "user",
        default_folder_token: str = "",
        timeout_seconds: int = 45,
    ) -> None:
        self.cli_path = resolve_feishu_cli_path(cli_path)
        self.identity = identity if identity in {"user", "bot"} else "user"
        self.default_folder_token = default_folder_token.strip()
        self.timeout_seconds = timeout_seconds
        self.tool_calls: list[dict[str, Any]] = []

    def publish_questionnaire(
        self,
        design: dict[str, Any],
        publish_name: str,
        folder_token: str = "",
    ) -> FeishuPublishResult:
        self.tool_calls = []
        questions = build_feishu_questions(design)
        self._ensure_auth_ready()

        base_name = _clean_text(publish_name or design.get("title") or "竞品调研问卷", 80)
        base_response = self._run(
            [
                "base",
                "+base-create",
                "--as",
                self.identity,
                "--name",
                base_name,
                "--time-zone",
                "Asia/Shanghai",
                "--format",
                "json",
                *self._folder_args(folder_token),
            ],
            "base +base-create",
        )
        base_token = _resource_value(
            base_response,
            ["base", "app"],
            ["base_token", "app_token", "baseToken", "appToken", "token"],
        )
        if not base_token:
            raise FeishuPublishError("飞书 CLI 创建 Base 后没有返回 base/app token。", tool_calls=self.tool_calls)

        table_response = self._run(
            [
                "base",
                "+table-create",
                "--as",
                self.identity,
                "--base-token",
                base_token,
                "--name",
                "问卷结果",
                "--format",
                "json",
            ],
            "base +table-create",
        )
        table_id = _resource_value(
            table_response,
            ["table", "default_table"],
            ["table_id", "tableId", "table.id"],
        )
        if not table_id:
            raise FeishuPublishError("飞书 CLI 创建数据表后没有返回 table_id。", tool_calls=self.tool_calls)

        form_response = self._run(
            [
                "base",
                "+form-create",
                "--as",
                self.identity,
                "--base-token",
                base_token,
                "--table-id",
                table_id,
                "--name",
                base_name,
                "--description",
                _clean_text(design.get("description") or "由竞品分析 Agent 生成的用户调研问卷。", 500),
                "--format",
                "json",
            ],
            "base +form-create",
        )
        form_id = _resource_value(
            form_response,
            ["form"],
            ["form_id", "formId", "form.id", "view_id", "viewId"],
        )
        if not form_id:
            form_id = _top_level_value(form_response, ["id", "view_id", "viewId"])
        if not form_id:
            form_list_response = self._run(
                [
                    "base",
                    "+form-list",
                    "--as",
                    self.identity,
                    "--base-token",
                    base_token,
                    "--table-id",
                    table_id,
                    "--format",
                    "json",
                ],
                "base +form-list",
            )
            form_id = _find_form_id(form_list_response, base_name)
        if not form_id:
            raise FeishuPublishError("飞书 CLI 创建表单后没有返回 form_id。", tool_calls=self.tool_calls)

        question_responses: list[dict[str, Any]] = []
        for batch in _chunks(questions, 10):
            question_responses.append(
                self._run(
                    [
                        "base",
                        "+form-questions-create",
                        "--as",
                        self.identity,
                        "--base-token",
                        base_token,
                        "--table-id",
                        table_id,
                        "--form-id",
                        form_id,
                        "--questions",
                        json.dumps(batch, ensure_ascii=False),
                        "--format",
                        "json",
                    ],
                    "base +form-questions-create",
                )
            )

        form_get_response = self._run(
            [
                "base",
                "+form-get",
                "--as",
                self.identity,
                "--base-token",
                base_token,
                "--table-id",
                table_id,
                "--form-id",
                form_id,
                "--format",
                "json",
            ],
            "base +form-get",
        )
        lookup_payloads = [form_get_response, form_response, *question_responses]
        share_token = _first_value(lookup_payloads, ["share_token", "shareToken", "form_share_token", "formShareToken"])
        feishu_url = _first_url(lookup_payloads)

        if share_token and not feishu_url:
            form_detail_response = self._run(
                ["base", "+form-detail", "--as", self.identity, "--share-token", share_token, "--format", "json"],
                "base +form-detail",
            )
            feishu_url = _first_url(form_detail_response)

        if not feishu_url:
            raise FeishuPublishError("飞书表单已创建，但 CLI 返回结果中没有可打开的问卷链接。", tool_calls=self.tool_calls)

        external_ids = {
            "base_token": base_token,
            "table_id": table_id,
            "form_id": form_id,
            "share_token": share_token,
        }
        return FeishuPublishResult(
            feishu_url=feishu_url,
            base_token=base_token,
            table_id=table_id,
            form_id=form_id,
            share_token=share_token,
            external_ids=external_ids,
            tool_calls=list(self.tool_calls),
        )

    def _folder_args(self, folder_token: str) -> list[str]:
        token = (folder_token or self.default_folder_token).strip()
        return ["--folder-token", token] if token else []

    def _ensure_auth_ready(self) -> None:
        auth = self._run(["auth", "status"], "auth status")
        identities = auth.get("identities", {}) if isinstance(auth, dict) else {}
        identity_status = ""
        if isinstance(identities, dict) and isinstance(identities.get(self.identity), dict):
            identity_status = str(identities[self.identity].get("status", ""))
        ready_anywhere = "ready" in json.dumps(auth, ensure_ascii=False).lower()
        if identity_status and identity_status != "ready":
            raise FeishuPublishError(f"飞书 CLI 当前 {self.identity} 身份未就绪，请先执行 lark-cli auth login。", tool_calls=self.tool_calls)
        if not identity_status and not ready_anywhere:
            raise FeishuPublishError("飞书 CLI 未登录或授权状态不可用，请先执行 lark-cli auth login。", tool_calls=self.tool_calls)

    def _run(self, args: list[str], tool_name: str) -> Any:
        cmd = [self.cli_path, *args]
        try:
            completed = subprocess.run(
                cmd,
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.timeout_seconds,
            )
        except FileNotFoundError as exc:
            raise FeishuPublishError(f"未找到飞书 CLI：{self.cli_path}", tool_calls=self.tool_calls) from exc
        except subprocess.TimeoutExpired as exc:
            raise FeishuPublishError(f"飞书 CLI 调用超时：{tool_name}", tool_calls=self.tool_calls) from exc

        if completed.returncode != 0:
            message = _clean_text(completed.stderr or completed.stdout or f"exit {completed.returncode}", 500)
            self.tool_calls.append({"name": tool_name, "provider": "lark-cli", "result": "failed"})
            raise FeishuPublishError(f"飞书 CLI 调用失败：{tool_name}；{message}", tool_calls=self.tool_calls)

        try:
            data = _parse_first_json(completed.stdout)
        except ValueError as exc:
            self.tool_calls.append({"name": tool_name, "provider": "lark-cli", "result": "invalid_json"})
            raise FeishuPublishError(f"飞书 CLI 返回的不是可解析 JSON：{tool_name}", tool_calls=self.tool_calls) from exc
        self.tool_calls.append({"name": tool_name, "provider": "lark-cli", "result": "ok"})
        return data


def _map_question(question: dict[str, Any], section_title: str) -> dict[str, Any] | None:
    question_type = str(question.get("type") or "single_choice").strip()
    question_id = _clean_text(question.get("id"), 20)
    question_text = _clean_text(question.get("question_text"), 180)
    if not question_text:
        return None
    title = _clean_text(f"{question_id} {question_text}".strip(), 180)
    description = _clean_text(section_title, 200)
    required = bool(question.get("required"))

    if question_type == "open_ended":
        return {"type": "text", "title": title, "description": description, "required": required}

    if question_type == "likert":
        return {
            "type": "number",
            "title": title,
            "description": description,
            "required": required,
            "style": {"type": "rating", "icon": "star", "min": 1, "max": 5},
        }

    options = _question_options(question)
    return {
        "type": "select",
        "title": title,
        "description": description,
        "required": required,
        "multiple": question_type == "multiple_choice",
        "option_display_mode": 1,
        "options": [{"name": option, "hue": "Blue"} for option in options],
    }


def _question_options(question: dict[str, Any]) -> list[str]:
    raw_options = question.get("options") or []
    options = [_clean_text(option, 80) for option in raw_options if _clean_text(option, 80)]
    if not options:
        options = ["是", "否", "不确定"]
    deduped: list[str] = []
    for option in options:
        if option not in deduped:
            deduped.append(option)
    return deduped[:20]


def _parse_first_json(output: str) -> Any:
    text = output.strip()
    if not text:
        raise ValueError("empty output")
    decoder = json.JSONDecoder()
    starts = [idx for idx in (text.find("{"), text.find("[")) if idx >= 0]
    if not starts:
        raise ValueError("no json object")
    data, _ = decoder.raw_decode(text[min(starts) :])
    return data


def _first_value(payload: Any, keys: list[str]) -> str:
    key_set = {key.lower() for key in keys}
    for key, value in _walk_key_values(payload):
        if key.lower() in key_set and value not in (None, ""):
            return str(value)
    return ""


def _resource_value(payload: Any, resource_names: list[str], keys: list[str]) -> str:
    explicit = _first_value(payload, keys)
    if explicit:
        return explicit

    resource_set = {name.lower() for name in resource_names}
    for key, value in _walk_key_values(payload):
        if key.lower() not in resource_set or not isinstance(value, dict):
            continue
        nested = _first_value(value, ["id", *keys, "token"])
        if nested:
            return nested
    return ""


def _top_level_value(payload: Any, keys: list[str]) -> str:
    key_set = {key.lower() for key in keys}
    candidates = [payload]
    if isinstance(payload, dict) and isinstance(payload.get("data"), dict):
        candidates.append(payload["data"])
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        for key, value in candidate.items():
            if key.lower() in key_set and value not in (None, ""):
                return str(value)
    return ""


def _find_form_id(payload: Any, form_name: str) -> str:
    forms = [item for item in _walk_dicts(payload) if _looks_like_form(item)]
    if not forms:
        return ""

    normalized_name = _normalize_lookup_text(form_name)
    named_matches = [
        item for item in forms
        if normalized_name and _normalize_lookup_text(item.get("name") or item.get("title")) == normalized_name
    ]
    for item in [*named_matches, *forms]:
        form_id = _resource_value(item, ["form"], ["form_id", "formId", "view_id", "viewId"])
        if form_id:
            return form_id
        top_level_id = _top_level_value(item, ["id", "view_id", "viewId"])
        if top_level_id:
            return top_level_id
    return ""


def _looks_like_form(value: dict[str, Any]) -> bool:
    keys = {str(key).lower() for key in value.keys()}
    if {"form_id", "formid", "view_id", "viewid"} & keys:
        return True
    type_value = str(value.get("type") or value.get("view_type") or value.get("viewType") or "").lower()
    return type_value in {"form", "form_view", "formview"}


def _first_url(payload: Any) -> str:
    for key, value in _walk_key_values(payload):
        if not isinstance(value, str):
            continue
        if key.lower() in {"share_url", "shareurl", "url", "link", "form_url", "formurl"} and value.startswith("http"):
            return value
    text = json.dumps(payload, ensure_ascii=False)
    match = re.search(r"https?://[^\s\"'<>]+", text)
    return match.group(0) if match else ""


def _walk_key_values(value: Any):
    if isinstance(value, dict):
        for key, child in value.items():
            yield str(key), child
            yield from _walk_key_values(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_key_values(child)


def _walk_dicts(value: Any):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk_dicts(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_dicts(child)


def _chunks(items: list[dict[str, Any]], size: int):
    for index in range(0, len(items), size):
        yield items[index : index + size]


def _clean_text(value: Any, limit: int = 200) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text[:limit]


def _normalize_lookup_text(value: Any) -> str:
    return re.sub(r"\s+", "", str(value or "")).strip().casefold()
