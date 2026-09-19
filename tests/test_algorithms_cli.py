"""检查独立 CLI 的文件边界、UTF-8 和错误处理，不评价算法效果。"""

import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

from competitive_evidence import algorithms


ROOT = Path(__file__).resolve().parents[1]


class AlgorithmsCLITests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.source = self.root / "输入.json"
        self.output = self.root / "输出.json"
        self.source.write_text('{"说明": "合成输入"}', encoding="utf-8")

    def invoke_process(self, command="caper", output=True, source=None):
        args = [sys.executable, "-X", "utf8", "-B", "-m",
                "competitive_evidence.algorithms", command,
                "--input", str(source or self.source)]
        if output:
            args += ["--output", str(self.output)]
        return subprocess.run(args, cwd=ROOT, capture_output=True,
                              text=True, encoding="utf-8")

    def assert_rejected_without_overwrite(self, raw):
        self.source.write_text(raw, encoding="utf-8")
        self.output.write_bytes(b"previous-result\n")
        result = self.invoke_process()
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(result.stderr.strip())
        self.assertNotIn("Traceback", result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertEqual(self.output.read_bytes(), b"previous-result\n")
        return result

    def test_invalid_json_preserves_existing_output(self):
        self.assert_rejected_without_overwrite('{"broken":')

    def test_non_object_json_is_rejected(self):
        for raw in ('[]', 'null', '"文本"', '12'):
            with self.subTest(raw=raw):
                result = self.assert_rejected_without_overwrite(raw)
                self.assertIn("顶层必须是对象", result.stderr)

    def test_nonfinite_and_overflow_numbers_are_rejected_at_any_depth(self):
        for value in ("NaN", "Infinity", "-Infinity", "1e999", "-1e999"):
            with self.subTest(value=value):
                result = self.assert_rejected_without_overwrite('{"nested": [{"value": ' + value + '}]}')
                self.assertIn("有限", result.stderr)

    def test_unreadable_encoding_does_not_create_output(self):
        self.source.write_bytes(b"\xff\xfe\xff")
        result = self.invoke_process()
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.output.exists())
        self.assertNotIn("Traceback", result.stderr)

    def test_missing_input_preserves_existing_output(self):
        self.output.write_text("原结果", encoding="utf-8")
        result = self.invoke_process(source=self.root / "missing.json")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.output.read_text(encoding="utf-8"), "原结果")

    def test_identical_path_is_rejected_before_processing(self):
        self.output = self.source
        before = self.source.read_bytes()
        result = self.invoke_process()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("同一文件", result.stderr)
        self.assertEqual(self.source.read_bytes(), before)

    def test_hardlink_to_input_is_rejected(self):
        try:
            os.link(self.source, self.output)
        except OSError as exc:
            self.skipTest(f"测试文件系统不支持硬链接：{exc}")
        before = self.source.read_bytes()
        result = self.invoke_process()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("同一文件", result.stderr)
        self.assertEqual(self.source.read_bytes(), before)

    def test_unknown_subcommand_preserves_output(self):
        self.output.write_text("原结果", encoding="utf-8")
        result = self.invoke_process(command="unknown")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("invalid choice", result.stderr)
        self.assertEqual(self.output.read_text(encoding="utf-8"), "原结果")

    def test_bom_input_and_utf8_output_file(self):
        self.source.write_text('{"说明":"中文输入"}', encoding="utf-8-sig")
        self.output = self.root / "新目录" / "结果.json"
        with patch.object(algorithms, "execute", return_value={"结果": "保留中文"}) as execute:
            code = algorithms.main(["caper", "--input", str(self.source), "--output", str(self.output)])
        self.assertEqual(code, 0)
        execute.assert_called_once_with("caper", {"说明": "中文输入"})
        data = self.output.read_bytes()
        self.assertFalse(data.startswith(b"\xef\xbb\xbf"))
        self.assertIn("保留中文".encode("utf-8"), data)
        self.assertEqual(json.loads(data), {"结果": "保留中文"})

    def test_omitted_output_writes_only_json_to_stdout(self):
        stdout = io.StringIO()
        with patch.object(algorithms, "execute", return_value={"结果": "中文"}), contextlib.redirect_stdout(stdout):
            code = algorithms.main(["firm-plan", "--input", str(self.source)])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(stdout.getvalue()), {"结果": "中文"})
        self.assertFalse(self.output.exists())

    def test_algorithm_error_preserves_output(self):
        self.output.write_text("原结果", encoding="utf-8")
        stderr = io.StringIO()
        with patch.object(algorithms, "execute", side_effect=ValueError("缺少 comparisons")), contextlib.redirect_stderr(stderr):
            with self.assertRaises(SystemExit) as failure:
                algorithms.main(["caper", "--input", str(self.source), "--output", str(self.output)])
        self.assertEqual(failure.exception.code, 2)
        self.assertIn("缺少 comparisons", stderr.getvalue())
        self.assertEqual(self.output.read_text(encoding="utf-8"), "原结果")

    def test_numeric_conversion_overflow_is_reported_without_traceback(self):
        self.output.write_text("原结果", encoding="utf-8")
        stderr = io.StringIO()
        with patch.object(algorithms, "execute", side_effect=OverflowError("数值过大")), contextlib.redirect_stderr(stderr):
            with self.assertRaises(SystemExit) as failure:
                algorithms.main(["caper", "--input", str(self.source), "--output", str(self.output)])
        self.assertEqual(failure.exception.code, 2)
        self.assertIn("数值过大", stderr.getvalue())
        self.assertNotIn("Traceback", stderr.getvalue())
        self.assertEqual(self.output.read_text(encoding="utf-8"), "原结果")

    def test_nonfinite_result_preserves_output(self):
        for value in (float("nan"), float("inf"), -float("inf")):
            with self.subTest(value=value):
                self.output.write_text("原结果", encoding="utf-8")
                with patch.object(algorithms, "execute", return_value={"value": value}), contextlib.redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit) as failure:
                        algorithms.main(["caper", "--input", str(self.source), "--output", str(self.output)])
                self.assertEqual(failure.exception.code, 2)
                self.assertEqual(self.output.read_text(encoding="utf-8"), "原结果")

    def test_atomic_replace_failure_preserves_output_and_cleans_temporary(self):
        self.output.write_text("原结果", encoding="utf-8")
        with patch.object(algorithms, "execute", return_value={"结果": "新结果"}), patch.object(algorithms.os, "replace", side_effect=OSError("替换失败")), contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                algorithms.main(["caper", "--input", str(self.source), "--output", str(self.output)])
        self.assertEqual(self.output.read_text(encoding="utf-8"), "原结果")
        self.assertEqual(list(self.root.glob(".*.tmp")), [])

    def test_execute_routes_all_three_public_functions(self):
        caper = types.ModuleType("competitive_evidence.caper")
        firm = types.ModuleType("competitive_evidence.firm")
        caper.rank_comparisons = Mock(return_value={"api": "caper"})
        firm.firm_plan = Mock(return_value={"api": "plan"})
        firm.firm_observe = Mock(return_value={"api": "observe"})
        payload = {"输入": "原样传递"}
        with patch.dict(sys.modules, {caper.__name__: caper, firm.__name__: firm}):
            for command, expected in (("caper", "caper"), ("firm-plan", "plan"), ("firm-observe", "observe")):
                with self.subTest(command=command):
                    self.assertEqual(algorithms.execute(command, payload), {"api": expected})
        caper.rank_comparisons.assert_called_once_with(payload)
        firm.firm_plan.assert_called_once_with(payload)
        firm.firm_observe.assert_called_once_with(payload)

    def test_execute_rejects_bad_command_input_or_return_type(self):
        with self.assertRaises(ValueError):
            algorithms.execute("missing", {})
        with self.assertRaises(ValueError):
            algorithms.execute("caper", [])
        module = types.ModuleType("competitive_evidence.caper")
        module.rank_comparisons = Mock(return_value=[])
        with patch.dict(sys.modules, {module.__name__: module}), self.assertRaises(TypeError):
            algorithms.execute("caper", {})


if __name__ == "__main__":
    unittest.main()
