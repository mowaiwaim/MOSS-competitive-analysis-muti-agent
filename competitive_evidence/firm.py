"""FIRM-ReAct：有限情形上的反证引导最小最大遗憾规划器。

仅使用标准库，不调用网络或模型。来源和核验记录由调用方提供，本模块
检查结构与一致性，不验证来源文本的语义真值。全部收益均以有效回答为条件。
"""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from math import isfinite
from numbers import Real
from uuid import uuid4


BOUNDARIES = [
    "仅对输入中明确列出的有限可行情形计算，不保证覆盖无限或遗漏的情形。",
    "情形、效用和查询分支必须有来源依据，来源真实性及情形完备性由调用方负责。",
    "候选必须已在全部输入情形下通过用户硬门槛，本模型不支持情形相关的不可行候选。",
    "来源哈希和核验记录检查不等于语义真值验证，不使用模型置信度删除情形。",
    "查询收益以有效回答为条件，不是包含失败概率的期望收益或效果实验结果。",
    "最多两步自适应前瞻，不保证任意长查询序列或收益成本比的全局最优。",
    "调用方须可信地持久化最新状态并串行提交结果，状态摘要不是身份认证。",
]
STATUSES = {"valid", "no_result", "conflict", "scope_mismatch", "unverified", "error"}
VERSION = "firm-react/1"


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)


def _hash(value):
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _text(value, name):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(name + " must be a nonempty string")
    return value


def _number(value, name, *, positive=False):
    if (not isinstance(value, Real) or isinstance(value, bool) or not isfinite(value)
            or value < 0 or (positive and value == 0)):
        raise ValueError(name + " must be a finite " + ("positive" if positive else "nonnegative") + " number")
    return float(value)


def _records(items, name):
    if not isinstance(items, list) or not items:
        raise ValueError(name + " must be a nonempty list")
    result, seen = [], set()
    for item in items:
        if not isinstance(item, dict):
            raise ValueError(name + " entries must be objects")
        record = deepcopy(item)
        identifier = _text(record.get("id"), name + ".id")
        if identifier in seen:
            raise ValueError("duplicate " + name + " id: " + identifier)
        seen.add(identifier)
        result.append(record)
    return result


def _sources(items):
    result = _records(items, "sources")
    for source in result:
        for key in ("uri", "content", "kind"):
            _text(source.get(key), "source." + key)
        digest = hashlib.sha256(source["content"].encode("utf-8")).hexdigest()
        if source.get("content_hash", digest) != digest:
            raise ValueError("source content_hash mismatch")
        source["content_hash"] = digest
    return result


def _references(values, known, name):
    if (not isinstance(values, list) or not values
            or any(not isinstance(value, str) or value not in known for value in values)
            or len(values) != len(set(values))):
        raise ValueError(name + " needs distinct known source_ids")
    return values


def _problem(problem):
    if not isinstance(problem, dict):
        raise ValueError("problem must be an object")
    p = deepcopy(problem)
    if p.get("hard_constraints_prechecked") is not True:
        raise ValueError("hard_constraints_prechecked must be true; all decisions must be feasible in every scenario")
    p["sources"] = _sources(p.get("sources"))
    source_map = {s["id"]: s for s in p["sources"]}
    p["decisions"] = _records(p.get("decisions"), "decisions")
    p["scenarios"] = _records(p.get("scenarios"), "scenarios")
    decisions = {d["id"] for d in p["decisions"]}
    scenarios = {s["id"] for s in p["scenarios"]}
    for item in p["decisions"] + p["scenarios"]:
        _text(item.get("label"), "label")
    for scenario in p["scenarios"]:
        _references(scenario.get("source_ids"), source_map, "scenario")
    utility = p.get("utility")
    if not isinstance(utility, dict) or not isinstance(utility.get("values"), dict):
        raise ValueError("utility requires a values object")
    _text(utility.get("description"), "utility.description")
    _references(utility.get("source_ids"), source_map, "utility")
    if not any(source_map[s]["kind"] in {"user_specification", "user_preference", "synthetic_fixture"}
               for s in utility["source_ids"]):
        raise ValueError("utility requires a user specification/preference or labeled synthetic fixture")
    if set(utility["values"]) != scenarios:
        raise ValueError("utility must define every scenario exactly once")
    for values in utility["values"].values():
        if not isinstance(values, dict) or set(values) != decisions:
            raise ValueError("utility must define every decision in each scenario")
        for value in values.values():
            # 用户效用可以为负，但不得用 NaN 或无穷代替未知值。
            if not isinstance(value, Real) or isinstance(value, bool) or not isfinite(value):
                raise ValueError("utilities must be finite numbers")
    queries = p.get("queries")
    if not isinstance(queries, list):
        raise ValueError("queries must be a list")
    p["queries"] = _records(queries, "queries") if queries else []
    for query in p["queries"]:
        _text(query.get("tool"), "query.tool")
        if not isinstance(query.get("args"), dict) or not isinstance(query.get("scope"), dict):
            raise ValueError("query args and scope must be objects")
        query["cost"] = _number(query.get("cost"), "query.cost", positive=True)
        _references(query.get("source_ids"), source_map, "query")
        query["outcomes"] = _records(query.get("outcomes"), "query outcomes")
        covered = set()
        for outcome in query["outcomes"]:
            _text(outcome.get("description"), "outcome.description")
            _references(outcome.get("source_ids"), source_map, "query outcome")
            members = outcome.get("scenario_ids")
            if (not isinstance(members, list) or not members
                    or any(not isinstance(s, str) or s not in scenarios for s in members)
                    or len(members) != len(set(members))):
                raise ValueError("outcome must name distinct known scenario_ids")
            if covered.intersection(members):
                raise ValueError("query outcomes must be mutually exclusive")
            covered.update(members)
        if covered != scenarios:
            raise ValueError("query outcomes must cover all explicit scenarios")
    _json(p)
    return p


def _config(config):
    defaults = {"budget": 10.0, "max_calls": 8, "max_failures": 2,
                "regret_threshold": 0.0, "min_gain": 0.0, "lookahead": 2}
    if not isinstance(config, dict) or set(config) - set(defaults):
        raise ValueError("unknown FIRM configuration")
    defaults.update(config)
    for key in ("budget", "regret_threshold", "min_gain"):
        defaults[key] = _number(defaults[key], key)
    for key in ("max_calls", "max_failures", "lookahead"):
        if isinstance(defaults[key], bool) or not isinstance(defaults[key], int):
            raise ValueError(key + " must be an integer")
    if defaults["max_calls"] < 0 or defaults["max_failures"] <= 0 or defaults["lookahead"] not in (1, 2):
        raise ValueError("invalid call/failure limit or lookahead")
    return defaults


def _seal(state):
    state.pop("state_digest", None)
    state["state_digest"] = _hash(state)
    return state


def _load_state(state):
    if not isinstance(state, dict):
        raise ValueError("state must be an object")
    copy = deepcopy(state)
    supplied = copy.pop("state_digest", None)
    if supplied != _hash(copy) or copy.get("version") != VERSION:
        raise ValueError("state digest/version mismatch; use the latest returned state")
    return copy


def _new_state(problem, config):
    return {"version": VERSION, "session_id": uuid4().hex, "revision": 0,
            "problem": problem, "config": config,
            "active_scenario_ids": sorted(s["id"] for s in problem["scenarios"]),
            "sources": {s["id"]: deepcopy(s) for s in problem["sources"]},
            "calls": 0, "failures": 0, "spent_cost": 0.0,
            "history": [], "pending_action": None, "completed_action_ids": []}


def _regret(problem, active):
    if not active:
        raise ValueError("cannot evaluate an empty feasible set")
    decisions = sorted(d["id"] for d in problem["decisions"])
    values = problem["utility"]["values"]
    regret = {d: max(max(values[s].values()) - values[s][d] for s in active)
              for d in decisions}
    best = min(regret.values())
    tied = [d for d in decisions if regret[d] == best]
    return {"recommendation": tied[0], "tied_recommendations": tied,
            "minimax_regret": best, "regret_by_decision": regret}


def _branches(query, active):
    current = set(active)
    return [(outcome, sorted(current.intersection(outcome["scenario_ids"])))
            for outcome in query["outcomes"]
            if current.intersection(outcome["scenario_ids"])]


def _falsifications(problem, active, recommendation):
    result = []
    labels = {s["id"]: s for s in problem["scenarios"]}
    values = problem["utility"]["values"]
    for sid in active:
        scenario, row = labels[sid], values[sid]
        for competitor in sorted(row):
            advantage = row[competitor] - row[recommendation]
            if advantage > 0:
                result.append({"scenario_id": sid, "condition": scenario["label"],
                               "challenger_id": competitor, "utility_advantage": advantage,
                               "source_ids": sorted(set(scenario["source_ids"] +
                                                        problem["utility"]["source_ids"]))})
    return result


def _one_step(problem, query, active):
    return max(_regret(problem, members)["minimax_regret"]
               for _, members in _branches(query, active))


def _query_evaluation(problem, query, active, current_regret, remaining, calls_left, config):
    branches = []
    for outcome, members in _branches(query, active):
        after_first = _regret(problem, members)["minimax_regret"]
        best = {"query_id": None, "cost": 0.0, "residual_regret": after_first}
        if config["lookahead"] == 2 and calls_left >= 2:
            for second in problem["queries"]:
                if second["id"] == query["id"] or query["cost"] + second["cost"] > remaining:
                    continue
                residual = _one_step(problem, second, members)
                candidate = {"query_id": second["id"], "cost": second["cost"],
                             "residual_regret": residual}
                # 分支内先最小化最坏遗憾，再选择费用更小的第二步。
                if (residual < best["residual_regret"] or
                    (residual == best["residual_regret"] and candidate["cost"] < best["cost"])):
                    best = candidate
        branches.append({"outcome_id": outcome["id"], "description": outcome["description"],
                         "scenario_ids": members, "source_ids": outcome["source_ids"],
                         "regret_after_first": after_first, "second_step": best})
    single_residual = max(b["regret_after_first"] for b in branches)
    single_gain = max(0.0, current_regret - single_residual)
    two_residual = max(b["second_step"]["residual_regret"] for b in branches)
    worst_cost = query["cost"] + max(b["second_step"]["cost"] for b in branches)
    two_gain = max(0.0, current_regret - two_residual)
    # 对一阶段和有条件第二阶段分别计分，不把尚未执行的费用计入账本。
    use_two = two_gain / worst_cost > single_gain / query["cost"]
    residual = two_residual if use_two else single_residual
    planned_cost = worst_cost if use_two else query["cost"]
    gain = two_gain if use_two else single_gain
    if not use_two:
        for branch in branches:
            branch["second_step"] = {"query_id": None, "cost": 0.0,
                                     "residual_regret": branch["regret_after_first"]}
    return {"query_id": query["id"], "affordable": query["cost"] <= remaining,
            "lookahead_steps": 2 if use_two else 1,
            "immediate_worst_residual_regret": single_residual,
            "immediate_guaranteed_gain": single_gain,
            "worst_residual_regret": residual, "guaranteed_gain": gain,
            "worst_path_estimated_cost": planned_cost, "score": gain / planned_cost,
            "conditional_on_valid_answers": True, "valid_answer_branches": branches}


def _plan(state):
    p, c, active = state["problem"], state["config"], state["active_scenario_ids"]
    decision = _regret(p, active)
    decision["active_scenario_ids"] = deepcopy(active)
    decision["falsification_conditions"] = _falsifications(p, active, decision["recommendation"])
    remaining = max(0.0, c["budget"] - state["spent_cost"])
    evaluations = [_query_evaluation(p, q, active, decision["minimax_regret"], remaining,
                                    c["max_calls"] - state["calls"], c)
                   for q in p["queries"]]
    base = {"decision": decision, "query_evaluations": evaluations,
            "boundaries": BOUNDARIES[:], "remaining_budget": remaining}
    # 重复 plan 复用同一个待执行动作，不生成多份可计费调用。
    if state["pending_action"] is not None:
        return {**base, "state": _seal(state), "action": deepcopy(state["pending_action"])}
    reason = None
    if decision["minimax_regret"] <= c["regret_threshold"]:
        reason = "regret_threshold_met"
    elif state["calls"] >= c["max_calls"]:
        reason = "call_budget_exhausted"
    elif state["failures"] >= c["max_failures"]:
        reason = "failure_limit_reached"
    elif remaining <= 0:
        reason = "cost_budget_exhausted"
    affordable = [e for e in evaluations if e["affordable"]]
    useful = [e for e in affordable if e["guaranteed_gain"] > c["min_gain"]]
    if reason is None and not affordable:
        reason = "no_affordable_query" if evaluations else "no_candidate_query"
    if reason is None and not useful:
        reason = "no_guaranteed_gain_within_horizon"
    if reason is not None:
        action = {"type": "stop", "terminal": True, "reason": reason,
                  "recommendation": decision["recommendation"],
                  "residual_regret": decision["minimax_regret"]}
        return {**base, "state": _seal(state), "action": action}
    chosen = min(useful, key=lambda e: (-e["score"], e["worst_residual_regret"],
                                       e["worst_path_estimated_cost"], e["query_id"]))
    query = next(q for q in p["queries"] if q["id"] == chosen["query_id"])
    action = {"type": "tool", "terminal": False, "query_id": query["id"],
              "tool": query["tool"], "args": deepcopy(query["args"]),
              "scope": deepcopy(query["scope"]), "estimated_cost": query["cost"],
              "score": chosen["score"], "lookahead_steps": chosen["lookahead_steps"],
              "valid_outcome_ids": [b["outcome_id"] for b in chosen["valid_answer_branches"]]}
    action["action_id"] = _hash({"session": state["session_id"], "revision": state["revision"],
                                  "active": active, "action": action})
    state["pending_action"] = deepcopy(action)
    return {**base, "state": _seal(state), "action": action}


def firm_plan(payload: dict) -> dict:
    """首次输入 problem/config，续跑只输入上一次返回的 state。"""
    if not isinstance(payload, dict):
        raise ValueError("payload must be an object")
    if "state" in payload:
        if "problem" in payload or "config" in payload:
            raise ValueError("cannot replace problem/config in an existing state")
        state = _load_state(payload["state"])
    else:
        state = _new_state(_problem(payload.get("problem")), _config(payload.get("config", {})))
    return _plan(state)


def _accepted_outcome(state, query, outcome, incoming_sources):
    """返回可保留情形及拒绝原因。拒绝有效标签不撤销已经发生的调用费用。"""
    active = state["active_scenario_ids"]
    if not isinstance(outcome, dict) or outcome.get("status") not in STATUSES:
        return active, "invalid_outcome_status", []
    if outcome["status"] != "valid":
        return active, outcome["status"], []
    if _json(outcome.get("scope")) != _json(query["scope"]):
        return active, "scope_mismatch", []
    verification = outcome.get("verification")
    if (not isinstance(verification, dict) or verification.get("verified") is not True
            or not isinstance(verification.get("verifier"), str) or not verification["verifier"].strip()
            or not isinstance(verification.get("method"), str) or not verification["method"].strip()):
        return active, "unverified", []
    branch = next((o for o in query["outcomes"] if o["id"] == outcome.get("outcome_id")), None)
    if branch is None:
        return active, "unknown_outcome_id", []
    try:
        added = _sources(incoming_sources) if incoming_sources else []
        combined = {**state["sources"], **{s["id"]: s for s in added}}
        for source in added:
            if source["id"] in state["sources"] and source != state["sources"][source["id"]]:
                raise ValueError("source IDs are immutable; use a new version ID")
        _references(outcome.get("source_ids"), combined, "observed outcome")
    except (ValueError, TypeError):
        return active, "invalid_source_provenance", []
    surviving = sorted(set(active).intersection(branch["scenario_ids"]))
    if not surviving:
        return active, "contradictory_empty_set", []
    return surviving, None, added


def firm_observe(payload: dict) -> dict:
    """对完整匹配计划的真实调用计费，仅以带来源且经过核验的有效回答收缩情形。"""
    if not isinstance(payload, dict):
        raise ValueError("payload must be an object")
    state = _load_state(payload.get("state"))
    action = payload.get("action")
    if not isinstance(action, dict):
        raise ValueError("action must be the complete planned action")
    if action.get("action_id") in state["completed_action_ids"]:
        raise ValueError("duplicate action result")
    planned = state["pending_action"]
    if planned is None or _json(action) != _json(planned):
        raise ValueError("unplanned or modified action")
    cost = _number(payload.get("actual_cost"), "actual_cost")
    query = next(q for q in state["problem"]["queries"] if q["id"] == planned["query_id"])
    outcome = deepcopy(payload.get("outcome"))
    before = state["active_scenario_ids"][:]
    active, reason, sources = _accepted_outcome(state, query, outcome, payload.get("sources", []))
    for source in sources:
        state["sources"][source["id"]] = source
    removed = sorted(set(before) - set(active))
    state["active_scenario_ids"] = active
    state["calls"] += 1
    state["revision"] += 1
    state["spent_cost"] += cost
    if reason is not None or not removed:
        state["failures"] += 1
    observation = {"accepted": reason is None, "reason": reason or "verified_valid_outcome",
                   "removed_scenario_ids": removed, "actual_cost": cost,
                   "new_information": bool(removed), "semantic_truth_verified_by_algorithm": False}
    state["history"].append({"action": deepcopy(action), "outcome": outcome,
                              "source_ids": [s["id"] for s in sources],
                              "observation_result": deepcopy(observation)})
    state["completed_action_ids"].append(action["action_id"])
    state["pending_action"] = None
    return {**_plan(state), "observation_result": observation}
