"""CAPER：按完整、同口径的双边证据包重排材料，仅使用标准库。

本模块验证字段、引文、来源版本和时间，不验证自然语言语义真值。
review 字段由人工或上游核验器提供；不得将其解释为本模块完成的核验。
完整包为每侧一条事实及其显式 support_ids 依赖闭包。“最小”相对于
调用者声明的依赖而言，并非自动发现的最短证明。选择策略为带互补性的
预算贪心，没有全局最优或普通次模覆盖算法的近似保证。
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, time, timezone
import hashlib
import json
import math
from urllib.parse import parse_qsl, urlencode, urlsplit


REVIEW_FIELDS = ("value_supported", "scope_supported", "normalization_verified", "validity_verified")
BOUNDARY = "结构检查及上游核验标记不构成本模块对事实语义真值的独立核验。"


def _fail(message):
    raise ValueError(message)


def _json_check(value, path="payload"):
    """严格接受 JSON 数据，拒绝 NaN、Infinity、非字符串键及隐式转换。"""
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            _fail(path + ": non-finite number")
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _json_check(item, f"{path}[{index}]")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                _fail(path + ": keys must be strings")
            _json_check(item, path + "." + key)
        return
    _fail(path + ": unsupported JSON type")


def _string(value, label):
    if not isinstance(value, str) or not value.strip():
        _fail(label + ": nonempty string required")
    return value


def _number(value, label, *, positive=False, integer=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _fail(label + ": number required")
    try:
        finite = math.isfinite(value)
    except OverflowError:
        finite = False
    if not finite or (value <= 0 if positive else value < 0):
        _fail(label + ": invalid numeric range")
    if integer and not isinstance(value, int):
        _fail(label + ": integer required")
    return value


def _date(value, label, *, end=False):
    _string(value, label)
    try:
        if len(value) == 10:
            parsed = datetime.combine(datetime.fromisoformat(value).date(), time.max if end else time.min)
        else:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        _fail(label + ": ISO-8601 date required")


def _scope(value, label):
    if not isinstance(value, dict):
        _fail(label + ": object required")
    for key, item in value.items():
        _string(key, label + " key")
        if item is None or not isinstance(item, (str, bool, int, float)):
            _fail(label + ": scope values must be known JSON scalars")
        if isinstance(item, str) and not item.strip():
            _fail(label + ": empty scope value")
    return value


def _canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _hash(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _hash_string(value, label):
    _string(value, label)
    if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
        _fail(label + ": lowercase SHA-256 required")
    return value


def _url(value):
    """仅合并明确相同的 URL；不推测未标注的转载关系。"""
    _string(value, "source.url")
    try:
        parts = urlsplit(value)
        if parts.scheme.lower() not in {"http", "https"} or not parts.hostname:
            _fail("source.url: absolute HTTP(S) URL required")
        if parts.username or parts.password:
            _fail("source.url: embedded credentials unsupported")
        host = parts.hostname.lower()
        port = parts.port
        if port and (parts.scheme.lower(), port) not in {("http", 80), ("https", 443)}:
            host += ":" + str(port)
        query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
                 if not k.lower().startswith("utm_") and k.lower() not in {"gclid", "fbclid", "msclkid"}]
        return host + (parts.path or "/") + "?" + urlencode(sorted(query))
    except (ValueError, TypeError):
        _fail("source.url: invalid HTTP(S) URL")


def _rows(payload, key, *, required_nonempty=False):
    values = payload.get(key)
    if not isinstance(values, list) or (required_nonempty and not values):
        _fail(key + ": list required")
    result = {}
    for item in values:
        if not isinstance(item, dict):
            _fail(key + ": each item must be an object")
        identifier = _string(item.get("id"), key + ".id")
        if identifier in result:
            _fail(key + ": duplicate id " + identifier)
        result[identifier] = item
    return result


def _source_groups(sources):
    """按 origin_id 或规范化 URL 传递归并，不按材料数量给产品加分。"""
    parent = {identifier: identifier for identifier in sources}

    def find(identifier):
        while parent[identifier] != identifier:
            parent[identifier] = parent[parent[identifier]]
            identifier = parent[identifier]
        return identifier

    aliases = {}
    for identifier, source in sources.items():
        for key in (("origin", source["origin_id"]), ("url", _url(source["url"]))):
            if key in aliases:
                parent[find(identifier)] = find(aliases[key])
            aliases[key] = identifier
    groups = {}
    for identifier in sources:
        groups.setdefault(find(identifier), []).append(identifier)
    return {identifier: min(groups[find(identifier)]) for identifier in sources}


def _prepare(payload):
    _json_check(payload)
    if not isinstance(payload, dict):
        _fail("payload must be an object")
    data = deepcopy(payload)
    now = _date(data.get("as_of"), "as_of", end=True)
    budget = _number(data.get("token_budget"), "token_budget", integer=True)
    max_combinations = _number(data.get("max_bundle_combinations", 4096),
                               "max_bundle_combinations", positive=True, integer=True)
    sources = _rows(data, "sources")
    comparisons = _rows(data, "comparisons", required_nonempty=True)
    evidence = _rows(data, "evidence")
    for source in sources.values():
        for key in ("url", "origin_id", "content"):
            _string(source.get(key), "source." + key)
        _url(source["url"])
        version = _hash_string(source.get("version_hash"), "source.version_hash")
        if version != _hash(source["content"]):
            _fail("source content hash mismatch: " + source["id"])
        _date(source.get("observed_at"), "source.observed_at")
        if source.get("published_at") is not None:
            _date(source["published_at"], "source.published_at")
        tier = _number(source.get("credibility_tier"), "source.credibility_tier", integer=True)
        if tier > 3:
            _fail("source.credibility_tier must be in 0..3")
    for comparison in comparisons.values():
        for key in ("left_competitor", "right_competitor", "left_offer_id", "right_offer_id", "dimension", "unit"):
            _string(comparison.get(key), "comparison." + key)
        if comparison["left_competitor"] == comparison["right_competitor"]:
            _fail("comparison must name two different competitors")
        _scope(comparison.get("scope"), "comparison.scope")
        if _date(comparison.get("valid_at"), "comparison.valid_at", end=True) > now:
            _fail("comparison.valid_at cannot be after as_of")
        _number(comparison.setdefault("weight", 1.0), "comparison.weight")
        tier = _number(comparison.setdefault("min_source_tier", 2), "comparison.min_source_tier", integer=True)
        if tier > 3:
            _fail("comparison.min_source_tier must be in 0..3")
        limit = comparison.setdefault("max_age_days", 365)
        if limit is not None:
            _number(limit, "comparison.max_age_days", positive=True)
        if not isinstance(comparison.setdefault("require_published_at", False), bool):
            _fail("comparison.require_published_at must be boolean")
    _number(sum(item["weight"] for item in comparisons.values()), "aggregate comparison weight")
    for item in evidence.values():
        if not isinstance(item.get("kind"), str) or item["kind"] not in {"fact", "support"}:
            _fail("evidence.kind must be fact or support")
        source_id = _string(item.get("source_id"), "evidence.source_id")
        if source_id not in sources:
            _fail("unknown source_id: " + source_id)
        _hash_string(item.get("source_version_hash"), "evidence.source_version_hash")
        _string(item.get("quote"), "evidence.quote")
        _number(item.get("token_cost"), "evidence.token_cost", positive=True, integer=True)
        review = item.get("review")
        if not isinstance(review, dict) or set(review) != set(REVIEW_FIELDS):
            _fail("evidence.review must contain exactly: " + ",".join(REVIEW_FIELDS))
        if any(not isinstance(review[key], bool) for key in REVIEW_FIELDS):
            _fail("evidence.review values must be boolean")
        start = _date(item.get("valid_from"), "evidence.valid_from")
        if "valid_until" not in item:
            _fail("evidence.valid_until must be explicit (ISO-8601 or null)")
        if item["valid_until"] is not None and _date(item["valid_until"], "evidence.valid_until", end=True) < start:
            _fail("evidence validity interval is reversed")
        support_ids = item.setdefault("support_ids", [])
        if not isinstance(support_ids, list) or any(not isinstance(key, str) for key in support_ids):
            _fail("support_ids must be a list of evidence ids")
        if len(support_ids) != len(set(support_ids)):
            _fail("duplicate support_id")
        if item["kind"] == "fact":
            for key in ("competitor", "offer_id", "dimension", "unit"):
                _string(item.get(key), "fact." + key)
            _scope(item.get("scope"), "fact.scope")
            if "normalized_value" not in item:
                _fail("fact.normalized_value is required; use null for unknown")
            value = item["normalized_value"]
            if value is not None and (not isinstance(value, (str, bool, int, float))
                                      or (isinstance(value, str) and not value.strip())):
                _fail("fact.normalized_value must be a JSON scalar or null")
            value_status = item.setdefault("value_status", "unknown" if value is None else "known")
            if not isinstance(value_status, str) or value_status not in {"known", "unknown"}:
                _fail("fact.value_status must be known or unknown")
        else:
            ids = item.get("comparison_ids")
            if (not isinstance(ids, list) or not ids or any(not isinstance(key, str) for key in ids)
                    or len(ids) != len(set(ids)) or any(key not in comparisons for key in ids)):
                _fail("support.comparison_ids must list distinct known comparison ids")
        for dependency in support_ids:
            if dependency not in evidence or evidence[dependency].get("kind") != "support":
                _fail("support_ids must reference existing support evidence: " + dependency)
    closures, visiting = {}, set()

    def closure(identifier):
        if identifier in closures:
            return closures[identifier]
        if identifier in visiting:
            _fail("cyclic support dependencies")
        visiting.add(identifier)
        result = {identifier}
        for child in evidence[identifier]["support_ids"]:
            result.update(closure(child))
        visiting.remove(identifier)
        closures[identifier] = result
        return result

    for identifier in evidence:
        closure(identifier)
    groups = _source_groups(sources)
    materials, material_for = {}, {}
    for identifier, item in evidence.items():
        material_id = _hash(_canonical([groups[item["source_id"]], item["source_version_hash"], item["quote"]]))
        material_for[identifier] = material_id
        if material_id in materials and materials[material_id]["token_cost"] != item["token_cost"]:
            _fail("identical material has inconsistent token_cost")
        materials.setdefault(material_id, {"id": material_id, "token_cost": item["token_cost"],
                                          "quote": item["quote"], "evidence_ids": [], "source_ids": [],
                                          "source_version_hash": item["source_version_hash"],
                                          "origin_group": groups[item["source_id"]]})
        materials[material_id]["evidence_ids"].append(identifier)
        materials[material_id]["source_ids"].append(item["source_id"])
    for material in materials.values():
        material["evidence_ids"].sort()
        material["source_ids"] = sorted(set(material["source_ids"]))
        material["citations"] = [{"source_id": key, "url": sources[key]["url"],
                                  "source_version_hash": sources[key]["version_hash"]}
                                 for key in material["source_ids"]]
    return now, budget, max_combinations, sources, comparisons, evidence, closures, materials, material_for


def _eligible(item, comparison, sources, now):
    """机械可查的门槛；语义与标准化正确性仍依赖明确的上游标记。"""
    reasons = []
    source = sources[item["source_id"]]
    if source["version_hash"] != item["source_version_hash"]:
        reasons.append("source_version_mismatch")
    if item["quote"] not in source["content"]:
        reasons.append("quote_not_found_in_source")
    reasons.extend("upstream_review_missing:" + key for key in REVIEW_FIELDS if not item["review"][key])
    if source["credibility_tier"] < comparison["min_source_tier"]:
        reasons.append("source_tier_below_common_threshold")
    observed = _date(source["observed_at"], "observed_at")
    published = _date(source["published_at"], "published_at") if source.get("published_at") is not None else None
    if observed > now or (published is not None and published > now):
        reasons.append("source_date_after_as_of")
    if published is not None and published > observed:
        reasons.append("published_after_observed")
    if comparison["require_published_at"] and published is None:
        reasons.append("published_date_required")
    age = (now - (published or observed)).total_seconds() / 86400
    if comparison["max_age_days"] is not None and age > comparison["max_age_days"]:
        reasons.append("source_too_old")
    valid_at = _date(comparison["valid_at"], "valid_at", end=True)
    if valid_at < _date(item["valid_from"], "valid_from") or (
            item["valid_until"] is not None and valid_at > _date(item["valid_until"], "valid_until", end=True)):
        reasons.append("fact_not_valid_at_comparison_time")
    if item["kind"] == "support":
        if comparison["id"] not in item["comparison_ids"]:
            reasons.append("support_not_applicable_to_comparison")
    else:
        if item["normalized_value"] is None or item["value_status"] == "unknown":
            reasons.append("unknown_value")
        if item["dimension"] != comparison["dimension"]:
            reasons.append("dimension_mismatch")
        if _canonical(item["scope"]) != _canonical(comparison["scope"]):
            reasons.append("scope_not_exactly_aligned")
        if item["unit"] != comparison["unit"]:
            reasons.append("unit_mismatch")
    return reasons


def _value_key(value):
    # 同量纲下 20 与 20.0 为同值；布尔值不能与 0/1 混为一谈。
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return ("number", value)
    return (type(value).__name__, value)


def rank_comparisons(payload: dict) -> dict:
    """JSON 接口：完整双边包的新增比较权重 / 新增唯一材料成本。

    token_budget 只约束调用者声明的材料 token_cost。调用方须将所用序列化格式
    中的引文、来源标识及必要元数据计入成本，不计系统提示词等外部开销。
    normalized_value、scope、单位换算及有效区间由上游提供，本模块
    不自动换汇、不把年付折算价当月付价、不推断缺少资料的产品更差。
    """
    now, budget, max_combinations, sources, comparisons, evidence, closures, materials, material_for = _prepare(payload)
    reports, bundles, rejected = {}, {}, []
    considered_combinations = 0
    for cid, comparison in comparisons.items():
        sides, errors = {}, {}
        for side in ("left", "right"):
            subject = comparison[side + "_competitor"]
            offer = comparison[side + "_offer_id"]
            candidates = [item for item in evidence.values() if item["kind"] == "fact"
                          and item["competitor"] == subject and item["offer_id"] == offer]
            valid = []
            for item in candidates:
                failures = []
                for identifier in sorted(closures[item["id"]]):
                    issues = _eligible(evidence[identifier], comparison, sources, now)
                    if issues:
                        failures.append({"evidence_id": identifier, "reasons": issues})
                        rejected.append({"comparison_id": cid, "evidence_id": identifier, "reasons": issues})
                if failures:
                    errors[item["id"]] = failures
                else:
                    valid.append(item["id"])
            sides[side] = sorted(valid)
        conflicts = []
        for side, ids in sides.items():
            values = {_value_key(evidence[identifier]["normalized_value"]): evidence[identifier]["normalized_value"]
                      for identifier in ids}
            if len(values) > 1:
                conflicts.append({"side": side, "fact_ids": ids,
                                  "values": sorted(values.values(), key=_canonical)})
        status = "conflict" if conflicts else "supported" if all(sides.values()) else "unknown"
        reports[cid] = {"id": cid, "status": status, "selected": False, "sides": sides,
                        "invalid_facts": errors, "conflicts": conflicts,
                        "missing_sides": [side for side, ids in sides.items() if not ids],
                        "semantic_truth_verified": False}
        bundles[cid] = []
        if status != "supported":
            continue
        # 先检查全部合格事实的冲突，再去掉完全相同的材料闭包。
        # 不用低成本前 K 截断，以免删掉单独较贵但可与其他比较共享的包。
        distinct_sides = {}
        for side, ids in sides.items():
            distinct = {}
            for identifier in ids:
                material_set = frozenset(material_for[key] for key in closures[identifier])
                distinct.setdefault(material_set, identifier)
            distinct_sides[side] = sorted(distinct.values())
        considered_combinations += len(distinct_sides["left"]) * len(distinct_sides["right"])
        if considered_combinations > max_combinations:
            _fail("candidate bundle combinations exceed max_bundle_combinations=" + str(max_combinations)
                  + "; narrow the comparison scope or explicitly raise this limit")
        reports[cid]["distinct_side_closures"] = {side: len(ids) for side, ids in distinct_sides.items()}
        seen = set()
        for left in distinct_sides["left"]:
            for right in distinct_sides["right"]:
                ids = closures[left] | closures[right]
                key = frozenset(material_for[identifier] for identifier in ids)
                # 相同材料闭包只保留一个确定性见证，不按重复材料增加收益。
                if key in seen:
                    continue
                seen.add(key)
                bundles[cid].append({"comparison_id": cid, "left_fact_id": left,
                                     "right_fact_id": right, "evidence_ids": sorted(ids),
                                     "materials": key})

    selected, supported, trace = set(), set(), []
    all_bundles = [bundle for cid in sorted(bundles) for bundle in bundles[cid]]

    def completed(material_ids):
        return {cid for cid, choices in bundles.items()
                if any(bundle["materials"] <= material_ids for bundle in choices)}

    def cost(material_ids):
        return sum(materials[identifier]["token_cost"] for identifier in material_ids)

    while True:
        candidates = []
        for bundle in all_bundles:
            added = bundle["materials"] - selected
            new_comparisons = completed(selected | added) - supported
            gain = sum(comparisons[cid]["weight"] for cid in new_comparisons)
            added_cost = cost(added)
            if not added or gain <= 0 or cost(selected) + added_cost > budget:
                continue
            candidates.append((gain / added_cost, added_cost, bundle["comparison_id"],
                               bundle["left_fact_id"], bundle["right_fact_id"],
                               added, new_comparisons, gain))
        if not candidates:
            break
        best = min(candidates, key=lambda item: (-item[0], item[1], item[2], item[3], item[4]))
        ratio, added_cost, cid, left, right, added, gained, gain = best
        selected.update(added)
        supported.update(gained)
        trace.append({"round": len(trace) + 1, "trigger_comparison_id": cid,
                      "left_fact_id": left, "right_fact_id": right,
                      "added_material_ids": sorted(added), "added_token_cost": added_cost,
                      "newly_supported_comparison_ids": sorted(gained), "weight_gain": gain,
                      "gain_per_token": ratio, "token_used": cost(selected)})
    selected_evidence = set()
    for cid, report in reports.items():
        report["selected"] = cid in supported
        if cid not in supported:
            report["selection_reason"] = "budget_or_zero_weight" if report["status"] == "supported" else report["status"]
            continue
        choices = [bundle for bundle in bundles[cid] if bundle["materials"] <= selected]
        witness = min(choices, key=lambda b: (cost(b["materials"]), b["left_fact_id"], b["right_fact_id"]))
        selected_evidence.update(witness["evidence_ids"])
        report["witness"] = {key: value for key, value in witness.items() if key != "materials"}
        report["normalized_values"] = {side: evidence[witness[side + "_fact_id"]]["normalized_value"]
                                       for side in ("left", "right")}
        report["unit"] = comparisons[cid]["unit"]
    return {"algorithm": "CAPER", "status": "complete" if len(supported) == len(comparisons) else "partial",
            "boundary": BOUNDARY, "semantic_truth_verified": False,
            "as_of": now.isoformat(), "selected_comparison_ids": sorted(supported),
            "comparisons": [reports[cid] for cid in sorted(reports)],
            "selected_evidence_ids": sorted(selected_evidence),
            "selected_materials": [materials[key] for key in sorted(selected)],
            "token_used": cost(selected), "token_budget": budget,
            "supported_weight": sum(comparisons[cid]["weight"] for cid in supported),
            "trace": trace,
            "rejected_evidence": sorted({ _canonical(item): item for item in rejected }.values(),
                                        key=lambda item: (item["comparison_id"], item["evidence_id"])),
            "policy": {"selection": "complete_bundle_marginal_weight_per_unique_material_token",
                       "optimality_guarantee": False, "source_tiers_are_caller_assigned": True,
                       "normalization_is_upstream_reviewed": True,
                       "max_bundle_combinations": max_combinations,
                       "candidate_combinations_considered": considered_combinations,
                       "candidate_truncation": False,
                       "token_budget_scope": "declared unique material costs only"}}
