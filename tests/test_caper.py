"""CAPER 逻辑单测。人工构造边界输入，不用于宣称实际检索效果。"""

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import unittest

from competitive_evidence.caper import rank_comparisons


def source(identifier, text):
    return dict(id=identifier, url="https://example.invalid/" + identifier, origin_id=identifier,
                content=text, version_hash=hashlib.sha256(text.encode()).hexdigest(),
                published_at="2026-09-01", observed_at="2026-09-01", credibility_tier=2)


def fixture():
    sources = [source("source-a", "A-Pro-v1：美国区，单席位月付，每月20美元，自2026-09-01起生效。"),
               source("source-b", "B-Team-v2：美国区，单席位月付，每月22美元，自2026-09-01起生效。")]
    scope = dict(region="US", billing_cycle="monthly", seats=1)
    comparison = dict(id="a-vs-b", left_competitor="A", right_competitor="B",
                      left_offer_id="A-Pro-v1", right_offer_id="B-Team-v2",
                      dimension="price", scope=scope, unit="USD/month", valid_at="2026-09-10")
    evidence = []
    for vendor, src, amount, offer in zip(("A", "B"), sources, (20, 22), ("A-Pro-v1", "B-Team-v2")):
        evidence.append(dict(id=vendor + "-fact", kind="fact", competitor=vendor,
                             offer_id=offer, dimension="price", scope=deepcopy(scope), unit="USD/month",
                             normalized_value=amount, source_id=src["id"], source_version_hash=src["version_hash"],
                             quote=src["content"], token_cost=40,
                             review=dict(value_supported=True, scope_supported=True,
                                         normalization_verified=True, validity_verified=True),
                             valid_from="2026-09-01", valid_until=None, support_ids=[]))
    return dict(as_of="2026-09-10", token_budget=80, sources=sources,
                comparisons=[comparison], evidence=evidence)


class CaperTests(unittest.TestCase):
    def test_only_a_complete_pair_is_selected(self):
        payload = fixture()
        self.assertEqual(rank_comparisons(payload)["selected_comparison_ids"], ["a-vs-b"])
        payload["evidence"].pop()
        result = rank_comparisons(payload)
        self.assertEqual(result["token_used"], 0)
        self.assertEqual(result["comparisons"][0]["missing_sides"], ["right"])

    def test_hard_budget_never_selects_half_a_pair(self):
        for budget in (0, 39, 40, 79):
            payload = fixture()
            payload["token_budget"] = budget
            result = rank_comparisons(payload)
            self.assertEqual(result["selected_evidence_ids"], [])
            self.assertEqual(result["token_used"], 0)

    def test_same_scope_unit_and_offer_are_required(self):
        for change in ("scope", "unit", "offer_id"):
            payload = fixture()
            if change == "scope":
                payload["evidence"][1]["scope"]["billing_cycle"] = "annual"
            else:
                payload["evidence"][1][change] = "different"
            self.assertFalse(rank_comparisons(payload)["selected_comparison_ids"])

    def test_explicit_support_dependency_must_also_fit_budget(self):
        payload = fixture()
        support = deepcopy(payload["evidence"][0])
        support.update(id="billing-witness", kind="support", comparison_ids=["a-vs-b"], token_cost=10,
                       quote="每月20美元")
        payload["evidence"].append(support)
        payload["evidence"][0]["support_ids"] = [support["id"]]
        self.assertFalse(rank_comparisons(payload)["selected_comparison_ids"])
        payload["token_budget"] = 90
        result = rank_comparisons(payload)
        self.assertEqual(result["token_used"], 90)
        self.assertIn("billing-witness", result["selected_evidence_ids"])
        support["review"]["normalization_verified"] = False
        self.assertFalse(rank_comparisons(payload)["selected_comparison_ids"])

    def test_unreviewed_wrong_quote_stale_version_and_weak_source_fail(self):
        for failure in ("review", "quote", "version", "tier"):
            payload = fixture()
            item = payload["evidence"][1]
            if failure == "review":
                item["review"]["value_supported"] = False
            elif failure == "quote":
                item["quote"] = "未出现的文字"
            elif failure == "version":
                item["source_version_hash"] = "0" * 64
            else:
                payload["sources"][1]["credibility_tier"] = 1
            result = rank_comparisons(payload)
            self.assertFalse(result["selected_comparison_ids"])
            self.assertTrue(result["rejected_evidence"])
            self.assertFalse(result["semantic_truth_verified"])

    def test_conflicting_values_are_not_resolved_by_cherry_picking(self):
        payload = fixture()
        conflicting = deepcopy(payload["evidence"][1])
        conflicting.update(id="B-conflict", normalized_value=18)
        payload["evidence"].append(conflicting)
        result = rank_comparisons(payload)
        self.assertEqual(result["comparisons"][0]["status"], "conflict")
        self.assertEqual(result["token_used"], 0)

    def test_unknown_is_not_a_negative_feature_and_equal_numbers_do_not_conflict(self):
        payload = fixture()
        payload["evidence"][1]["normalized_value"] = None
        result = rank_comparisons(payload)
        self.assertEqual(result["comparisons"][0]["status"], "unknown")
        payload = fixture()
        duplicate = deepcopy(payload["evidence"][1])
        duplicate.update(id="B-float", normalized_value=22.0)
        payload["evidence"].append(duplicate)
        self.assertEqual(rank_comparisons(payload)["comparisons"][0]["status"], "supported")

    def test_same_material_shared_by_multiple_comparisons_is_charged_once(self):
        payload = fixture()
        second = deepcopy(payload["comparisons"][0])
        second.update(id="second-question", weight=3)
        payload["comparisons"].append(second)
        result = rank_comparisons(payload)
        self.assertEqual(len(result["selected_comparison_ids"]), 2)
        self.assertEqual(result["token_used"], 80)
        self.assertEqual(result["trace"][0]["weight_gain"], 4)

    def test_copied_fact_does_not_change_product_score_or_material_cost(self):
        payload = fixture()
        duplicate = deepcopy(payload["evidence"][0])
        duplicate["id"] = "A-copy"
        payload["evidence"].append(duplicate)
        result = rank_comparisons(payload)
        self.assertEqual(result["supported_weight"], 1)
        self.assertEqual(result["token_used"], 80)
        self.assertEqual(len(result["selected_materials"]), 2)

    def test_combination_limit_is_explicit_and_duplicate_closures_do_not_consume_it(self):
        payload = fixture()
        payload["max_bundle_combinations"] = 1
        duplicate = deepcopy(payload["evidence"][0])
        duplicate["id"] = "A-copy"
        payload["evidence"].append(duplicate)
        self.assertEqual(rank_comparisons(payload)["policy"]["candidate_combinations_considered"], 1)
        new_source = source("another-a", "另一份A-Pro-v1美国单席位月付每月20美元证据。")
        payload["sources"].append(new_source)
        independent = deepcopy(payload["evidence"][0])
        independent.update(id="A-independent", source_id=new_source["id"],
                           source_version_hash=new_source["version_hash"], quote=new_source["content"])
        payload["evidence"].append(independent)
        with self.assertRaisesRegex(ValueError, "max_bundle_combinations"):
            rank_comparisons(payload)
        # 即便上限很小，也不能先截断而隐藏冲突。
        independent["normalized_value"] = 18
        result = rank_comparisons(payload)
        self.assertEqual(result["comparisons"][0]["status"], "conflict")

    def test_boolean_and_string_values_are_distinct_from_numbers(self):
        for value in (True, "20"):
            payload = fixture()
            duplicate = deepcopy(payload["evidence"][0])
            duplicate.update(id="A-other-type", normalized_value=value)
            payload["evidence"].append(duplicate)
            self.assertEqual(rank_comparisons(payload)["comparisons"][0]["status"], "conflict")

    def test_time_rules_and_required_publication(self):
        for change in ("future", "expired", "too_old", "missing_publication"):
            payload = fixture()
            if change == "future":
                payload["sources"][1]["observed_at"] = "2027-01-01"
            elif change == "expired":
                payload["evidence"][1]["valid_until"] = "2026-09-09"
            elif change == "too_old":
                payload["comparisons"][0]["max_age_days"] = 2
            else:
                payload["comparisons"][0]["require_published_at"] = True
                payload["sources"][1]["published_at"] = None
            self.assertFalse(rank_comparisons(payload)["selected_comparison_ids"])

    def test_malformed_input_raises_before_producing_output(self):
        changes = [lambda p: p.update(token_budget=-1),
                   lambda p: p["comparisons"][0].update(weight=float("nan")),
                   lambda p: p["evidence"][0].update(token_cost=-3),
                   lambda p: p["evidence"].append(deepcopy(p["evidence"][0])),
                   lambda p: p["sources"][0].update(version_hash="0" * 64),
                   lambda p: p["evidence"][0].update(support_ids=["missing"]),
                   lambda p: p["evidence"][0]["review"].update(value_supported="true")]
        for change in changes:
            payload = fixture()
            change(payload)
            with self.assertRaises(ValueError):
                rank_comparisons(payload)

    def test_finite_individual_weights_cannot_overflow_aggregate_score(self):
        payload = fixture()
        payload["comparisons"][0]["weight"] = 1e308
        second = deepcopy(payload["comparisons"][0])
        second["id"] = "second"
        payload["comparisons"].append(second)
        with self.assertRaisesRegex(ValueError, "aggregate comparison weight"):
            rank_comparisons(payload)

    def test_dependency_cycles_and_inconsistent_material_costs_are_rejected(self):
        for cycle in (True, False):
            payload = fixture()
            item = deepcopy(payload["evidence"][0])
            item.update(id="copy", kind="support", comparison_ids=["a-vs-b"])
            if cycle:
                item["support_ids"] = ["copy"]
            else:
                item["token_cost"] = 10
            payload["evidence"].append(item)
            with self.assertRaises(ValueError):
                rank_comparisons(payload)

    def test_inputs_are_preserved_and_output_is_json(self):
        payload = fixture()
        before = deepcopy(payload)
        result = rank_comparisons(payload)
        self.assertEqual(payload, before)
        json.dumps(result, allow_nan=False)

    def test_checked_in_example_is_runnable(self):
        path = Path(__file__).resolve().parents[1] / "competitive_evidence/examples/caper_input.json"
        result = rank_comparisons(json.loads(path.read_text(encoding="utf-8")))
        self.assertLessEqual(result["token_used"], result["token_budget"])
        self.assertTrue(result["selected_comparison_ids"])


if __name__ == "__main__":
    unittest.main()
