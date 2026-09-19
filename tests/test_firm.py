"""合成功能测试，验证算法约束与状态转换，不是效果实验。"""

from copy import deepcopy
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from competitive_evidence.firm import firm_observe, firm_plan


FIXTURE = Path(__file__).resolve().parents[1] / "competitive_evidence/examples/firm_input.json"


def example():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def observe(plan, outcome_id=None, status="valid", cost=1, **changes):
    outcome = {"status": status}
    if status == "valid":
        outcome.update({"outcome_id": outcome_id, "scope": plan["action"]["scope"],
                        "source_ids": ["fixture-spec"],
                        "verification": {"verified": True, "verifier": "fixture-test",
                                         "method": "核对已知的合成答案"}})
    outcome.update(changes)
    return firm_observe({"state": plan["state"], "action": plan["action"],
                         "actual_cost": cost, "outcome": outcome})


class FirmTests(unittest.TestCase):
    def test_minimax_regret_and_falsifications_have_exact_witnesses(self):
        result = firm_plan(example())
        self.assertEqual(result["decision"]["minimax_regret"], 10)
        self.assertEqual(result["decision"]["regret_by_decision"], {"A": 10, "B": 10})
        self.assertEqual(result["decision"]["recommendation"], "A")
        witnesses = result["decision"]["falsification_conditions"]
        self.assertEqual({w["scenario_id"] for w in witnesses}, {"s01", "s10"})
        self.assertTrue(all(w["challenger_id"] == "B" and w["source_ids"] for w in witnesses))

    def test_complementary_queries_need_two_step_lookahead(self):
        payload = example()
        payload["config"]["lookahead"] = 1
        short = firm_plan(payload)
        self.assertEqual(short["action"]["reason"], "no_guaranteed_gain_within_horizon")
        payload["config"]["lookahead"] = 2
        result = firm_plan(payload)
        self.assertEqual(result["action"]["query_id"], "q-x")
        evaluation = result["query_evaluations"][0]
        self.assertEqual(evaluation["immediate_guaranteed_gain"], 0)
        self.assertEqual(evaluation["guaranteed_gain"], 10)
        self.assertEqual(evaluation["worst_path_estimated_cost"], 2)
        self.assertEqual(evaluation["score"], 5)
        self.assertTrue(evaluation["conditional_on_valid_answers"])
        self.assertEqual({b["second_step"]["query_id"] for b in evaluation["valid_answer_branches"]}, {"q-y"})

    def test_verified_observations_prune_then_change_recommendation(self):
        first = firm_plan(example())
        second = observe(first, "x0")
        self.assertEqual(second["decision"]["active_scenario_ids"], ["s00", "s01"])
        self.assertEqual(second["action"]["query_id"], "q-y")
        final = observe(second, "y1")
        self.assertEqual(final["decision"]["recommendation"], "B")
        self.assertEqual(final["action"]["reason"], "regret_threshold_met")
        self.assertEqual(final["decision"]["minimax_regret"], 0)
        self.assertEqual(final["state"]["spent_cost"], 2)
        self.assertEqual(final["state"]["failures"], 0)
        self.assertEqual(len(first["decision"]["active_scenario_ids"]), 4)

    def test_invalid_answers_preserve_scenarios_and_charge_actual_cost(self):
        for status in ("no_result", "conflict", "scope_mismatch", "unverified", "error"):
            with self.subTest(status=status):
                initial = firm_plan(example())
                result = observe(initial, status=status, cost=1.25)
                self.assertFalse(result["observation_result"]["accepted"])
                self.assertEqual(result["state"]["active_scenario_ids"], initial["state"]["active_scenario_ids"])
                self.assertEqual(result["state"]["spent_cost"], 1.25)
                self.assertEqual(result["state"]["failures"], 1)

    def test_valid_label_requires_verification_and_source_provenance(self):
        cases = [{"verification": None}, {"verification": {"verified": False}},
                 {"source_ids": []}, {"source_ids": ["made-up"]},
                 {"scope": {"fixture": "wrong-billing-period"}}]
        for changes in cases:
            with self.subTest(changes=changes):
                result = observe(firm_plan(example()), "x0", **changes)
                self.assertFalse(result["observation_result"]["accepted"])
                self.assertEqual(len(result["decision"]["active_scenario_ids"]), 4)
                self.assertEqual(result["state"]["calls"], 1)

    def test_failure_limit_and_cost_overshoot_stop(self):
        first = observe(firm_plan(example()), status="no_result")
        final = observe(first, status="no_result")
        self.assertEqual(final["action"]["reason"], "failure_limit_reached")
        self.assertEqual(final["state"]["spent_cost"], 2)
        overspent = observe(firm_plan(example()), status="error", cost=9)
        self.assertEqual(overspent["state"]["spent_cost"], 9)
        self.assertEqual(overspent["action"]["reason"], "cost_budget_exhausted")

    def test_budget_and_call_limits_restrict_lookahead(self):
        for changes in ({"budget": 1}, {"max_calls": 1}):
            payload = example()
            payload["config"].update(changes)
            self.assertEqual(firm_plan(payload)["action"]["reason"],
                             "no_guaranteed_gain_within_horizon")
        for changes, reason in [({"budget": 0}, "cost_budget_exhausted"),
                                ({"max_calls": 0}, "call_budget_exhausted"),
                                ({"budget": .5}, "no_affordable_query")]:
            payload = example()
            payload["config"].update(changes)
            self.assertEqual(firm_plan(payload)["action"]["reason"], reason)

    def test_threshold_equal_utilities_and_no_queries_stop(self):
        payload = example()
        payload["config"]["regret_threshold"] = 10
        self.assertEqual(firm_plan(payload)["action"]["reason"], "regret_threshold_met")
        payload = example()
        payload["problem"]["utility"]["values"] = {s: {"A": 5, "B": 5} for s in ("s00", "s01", "s10", "s11")}
        self.assertEqual(firm_plan(payload)["decision"]["minimax_regret"], 0)
        self.assertTrue(firm_plan(payload)["action"]["terminal"])
        payload = example()
        payload["problem"]["queries"] = []
        self.assertEqual(firm_plan(payload)["action"]["reason"], "no_candidate_query")

    def test_action_must_be_planned_unmodified_and_not_replayed(self):
        first = firm_plan(example())
        altered = deepcopy(first["action"])
        altered["args"]["fact"] = "forged"
        with self.assertRaisesRegex(ValueError, "unplanned or modified"):
            firm_observe({"state": first["state"], "action": altered, "actual_cost": 1})
        second = observe(first, "x0")
        with self.assertRaisesRegex(ValueError, "duplicate"):
            firm_observe({"state": second["state"], "action": first["action"], "actual_cost": 1})
        foreign = firm_plan(example())
        with self.assertRaisesRegex(ValueError, "unplanned or modified"):
            firm_observe({"state": first["state"], "action": foreign["action"], "actual_cost": 1})

    def test_plan_is_idempotent_for_pending_action_and_detects_state_edits(self):
        first = firm_plan(example())
        replay = firm_plan({"state": first["state"]})
        self.assertEqual(replay, first)
        changed = deepcopy(first["state"])
        changed["active_scenario_ids"] = ["s00"]
        with self.assertRaisesRegex(ValueError, "state digest"):
            firm_plan({"state": changed})

    def test_source_utility_and_hard_constraint_requirements(self):
        for field in ("scenarios", "queries"):
            payload = example()
            payload["problem"][field][0]["source_ids"] = []
            with self.assertRaises(ValueError):
                firm_plan(payload)
        for change in ("utility", "hard_constraints", "partial_utility"):
            payload = example()
            if change == "utility":
                payload["problem"]["utility"]["source_ids"] = []
            elif change == "hard_constraints":
                payload["problem"]["hard_constraints_prechecked"] = False
            else:
                del payload["problem"]["utility"]["values"]["s00"]["B"]
            with self.assertRaises(ValueError):
                firm_plan(payload)

    def test_partitions_must_be_complete_and_disjoint(self):
        for invalid in ("missing", "overlap", "unknown"):
            payload = example()
            branch = payload["problem"]["queries"][0]["outcomes"][0]
            if invalid == "missing":
                branch["scenario_ids"].remove("s00")
            elif invalid == "overlap":
                branch["scenario_ids"].append("s11")
            else:
                branch["scenario_ids"].append("ghost")
            with self.assertRaises(ValueError):
                firm_plan(payload)

    def test_numeric_inputs_and_actual_cost_are_required_and_finite(self):
        for value in (float("nan"), float("inf"), True, -1, "1"):
            payload = example()
            payload["config"]["budget"] = value
            with self.assertRaises(ValueError):
                firm_plan(payload)
        first = firm_plan(example())
        with self.assertRaises(ValueError):
            firm_observe({"state": first["state"], "action": first["action"],
                          "outcome": {"status": "no_result"}})

    def test_contradictory_empty_set_is_rejected_but_real_call_is_charged(self):
        payload = example()
        first, second = payload["problem"]["queries"]
        first["outcomes"] = [
            {"id": key, "scenario_ids": members, "source_ids": ["fixture-spec"], "description": key}
            for key, members in [("a", ["s00"]), ("b", ["s01"]), ("c", ["s10", "s11"])]]
        second["outcomes"] = [
            {"id": key, "scenario_ids": members, "source_ids": ["fixture-spec"], "description": key}
            for key, members in [("a", ["s00", "s01"]), ("b", ["s10"]), ("c", ["s11"])]]
        plan = firm_plan(payload)
        self.assertEqual(plan["action"]["query_id"], "q-x")
        current = observe(plan, "c")
        self.assertEqual(current["action"]["query_id"], "q-y")
        rejected = observe(current, "a")
        self.assertEqual(rejected["observation_result"]["reason"], "contradictory_empty_set")
        self.assertEqual(rejected["state"]["active_scenario_ids"], ["s10", "s11"])
        self.assertEqual(rejected["state"]["spent_cost"], 2)
        self.assertEqual(rejected["state"]["calls"], 2)

    def test_second_query_can_differ_by_first_answer(self):
        payload = example()
        problem = payload["problem"]
        problem["utility"]["values"].update({"s10": {"A": 10, "B": 0}, "s11": {"A": 0, "B": 10}})
        problem["sources"][0]["content"] = "合成功能规格：A 在 s00/s10 效用 10，B 在 s01/s11 效用 10，其他为 0。三个查询分别给出下面显式互斥的情形分组。所有候选通过硬门槛。"
        queries = []
        for identifier, partitions in [("0-root", [["s00", "s01"], ["s10", "s11"]]),
                                        ("1-left", [["s00"], ["s01", "s10", "s11"]]),
                                        ("2-right", [["s10"], ["s00", "s01", "s11"]])]:
            query = deepcopy(problem["queries"][0])
            query["id"] = identifier
            query["outcomes"] = [{"id": str(i), "scenario_ids": members, "source_ids": ["fixture-spec"],
                                   "description": "合成分组 " + str(i)} for i, members in enumerate(partitions)]
            queries.append(query)
        problem["queries"] = queries
        result = firm_plan(payload)
        evaluation = next(q for q in result["query_evaluations"] if q["query_id"] == "0-root")
        self.assertEqual([b["second_step"]["query_id"] for b in evaluation["valid_answer_branches"]],
                         ["1-left", "2-right"])
        self.assertEqual(evaluation["worst_residual_regret"], 0)

    def test_gain_per_cost_and_gain_threshold_affect_choice(self):
        payload = example()
        direct = deepcopy(payload["problem"]["queries"][0])
        direct.update({"id": "q-all", "cost": 1.5})
        direct["outcomes"] = [{"id": sid, "scenario_ids": [sid], "source_ids": ["fixture-spec"],
                               "description": sid} for sid in ("s00", "s01", "s10", "s11")]
        payload["problem"]["queries"].append(direct)
        self.assertEqual(firm_plan(payload)["action"]["query_id"], "q-all")
        direct["cost"] = 3
        self.assertEqual(firm_plan(payload)["action"]["query_id"], "q-x")
        payload["config"]["min_gain"] = 10
        self.assertEqual(firm_plan(payload)["action"]["reason"], "no_guaranteed_gain_within_horizon")

    def test_source_hash_and_state_survive_json_round_trip(self):
        plan = firm_plan(example())
        self.assertEqual(len(plan["state"]["sources"]["fixture-spec"]["content_hash"]), 64)
        round_trip = json.loads(json.dumps(plan, ensure_ascii=False, allow_nan=False))
        self.assertEqual(firm_plan({"state": round_trip["state"]}), plan)
        payload = example()
        payload["problem"]["sources"][0]["content_hash"] = "invented"
        with self.assertRaises(ValueError):
            firm_plan(payload)

    def test_model_confidence_is_not_used_to_remove_scenarios(self):
        payload = example()
        payload["problem"]["scenarios"][0]["llm_confidence"] = 0
        result = firm_plan(payload)
        self.assertIn("s00", result["decision"]["active_scenario_ids"])
        self.assertTrue(payload["metadata"]["synthetic"])


if __name__ == "__main__":
    unittest.main()
