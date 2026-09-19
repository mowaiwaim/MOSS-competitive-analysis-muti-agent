"""验证仓库 Skill 启动器的可迁移调用边界，不评价算法效果。"""

import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


class RepositorySkillsTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.temp = Path(self.directory.name)

    def run_skill(self, name, *args, script=None):
        runner = script or ROOT / "skills" / name / "scripts" / "run.py"
        return subprocess.run([sys.executable, "-X", "utf8", "-B", str(runner), *map(str, args)],
                              cwd=self.temp, capture_output=True, text=True, encoding="utf-8")

    def read_success(self, result):
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_caper_runs_outside_repository_working_directory(self):
        result = self.read_success(self.run_skill("moss-caper", "--input",
                    ROOT / "competitive_evidence/examples/caper_input.json"))
        self.assertEqual(result["algorithm"], "CAPER")
        self.assertTrue(result["selected_comparison_ids"])
        self.assertLessEqual(result["token_used"], result["token_budget"])

    def test_firm_plan_and_real_status_observation_round_trip(self):
        plan = self.read_success(self.run_skill("moss-firm-react", "plan", "--input",
                    ROOT / "competitive_evidence/examples/firm_input.json"))
        self.assertFalse(plan["action"]["terminal"])
        # 合成功能用例明确回传无结果，不伪造已核验事实。
        observation = self.temp / "observation.json"
        observation.write_text(json.dumps({"state": plan["state"], "action": plan["action"],
            "actual_cost": plan["action"]["estimated_cost"], "outcome": {"status": "no_result"}}), encoding="utf-8")
        result = self.read_success(self.run_skill("moss-firm-react", "observe", "--input", observation))
        self.assertFalse(result["observation_result"]["accepted"])
        self.assertEqual(result["state"]["active_scenario_ids"], plan["state"]["active_scenario_ids"])
        self.assertEqual(result["state"]["calls"], 1)

    def test_tool_string_is_data_and_is_not_executed(self):
        payload = json.loads((ROOT / "competitive_evidence/examples/firm_input.json").read_text(encoding="utf-8"))
        marker = self.temp / "should-not-exist.txt"
        command = f'echo unexpected > "{marker}"'
        for query in payload["problem"]["queries"]:
            query["tool"] = command
        source = self.temp / "untrusted-tool.json"
        source.write_text(json.dumps(payload), encoding="utf-8")
        result = self.read_success(self.run_skill("moss-firm-react", "plan", "--input", source))
        self.assertEqual(result["action"]["tool"], command)
        self.assertFalse(marker.exists())

    def test_detached_skill_reports_missing_repository(self):
        for name, command in (("moss-caper", []), ("moss-firm-react", ["plan"])):
            with self.subTest(name=name):
                destination = self.temp / name / "scripts" / "run.py"
                destination.parent.mkdir(parents=True)
                shutil.copy2(ROOT / "skills" / name / "scripts" / "run.py", destination)
                result = self.run_skill(name, *command, "--input", "missing.json", script=destination)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("未找到 MOSS 共享算法", result.stderr)
                self.assertNotIn("Traceback", result.stderr)

    def test_shared_cli_same_file_protection_is_preserved(self):
        source = self.temp / "job.json"
        shutil.copy2(ROOT / "competitive_evidence/examples/caper_input.json", source)
        before = source.read_bytes()
        result = self.run_skill("moss-caper", "--input", source, "--output", source)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(source.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
