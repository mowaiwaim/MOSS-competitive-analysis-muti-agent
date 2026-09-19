"""CAPER 与 FIRM-ReAct 的独立命令行入口，仅使用 Python 标准库。"""

from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import sys
import tempfile


COMMANDS = ("caper", "firm-plan", "firm-observe")


def _reject_constant(value: str):
    raise ValueError(f"JSON 不允许非有限数值：{value}")


def _finite_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"JSON 数值超出有限范围：{value}")
    return number


def execute(command: str, payload: dict) -> dict:
    """按子命令调用公开函数，算法模块在实际使用时才导入。"""
    if not isinstance(payload, dict):
        raise ValueError("输入 JSON 的顶层必须是对象")
    if command == "caper":
        from .caper import rank_comparisons
        result = rank_comparisons(payload)
    elif command == "firm-plan":
        from .firm import firm_plan
        result = firm_plan(payload)
    elif command == "firm-observe":
        from .firm import firm_observe
        result = firm_observe(payload)
    else:
        raise ValueError(f"未知算法命令：{command}")
    if not isinstance(result, dict):
        raise TypeError("算法返回值必须是 JSON 对象")
    return result


def _ensure_distinct_paths(source: Path, destination: Path) -> None:
    # resolve 同时处理相对路径和符号链接，samefile 补充检查硬链接。
    if os.path.normcase(str(source.resolve())) == os.path.normcase(str(destination.resolve())):
        raise ValueError("--input 与 --output 不得指向同一文件")
    if source.exists() and destination.exists() and source.samefile(destination):
        raise ValueError("--input 与 --output 不得指向同一文件或硬链接")


def _write_atomic(destination: Path, text: str) -> None:
    """先完整写入同目录临时文件，再替换结果，错误时保留原输出。"""
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", newline="\n", delete=False,
            dir=destination.parent, prefix=f".{destination.name}.", suffix=".tmp",
        ) as handle:
            temporary = Path(handle.name)
            handle.write(text)
        os.replace(temporary, destination)
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                # 清理失败不应掩盖原始读写错误。
                pass


def main(argv=None) -> int:
    # Windows 终端或管道也固定输出 UTF-8，不依赖本地代码页。
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=COMMANDS, help="选择排序、规划或观察回写")
    parser.add_argument("--input", required=True, type=Path, help="UTF-8 JSON 输入，可带 BOM")
    parser.add_argument("--output", type=Path, help="UTF-8 JSON 输出，省略时写标准输出")
    args = parser.parse_args(argv)
    try:
        if args.output is not None:
            _ensure_distinct_paths(args.input, args.output)
        payload = json.loads(
            args.input.read_text(encoding="utf-8-sig"),
            parse_constant=_reject_constant, parse_float=_finite_float,
        )
        if not isinstance(payload, dict):
            raise ValueError("输入 JSON 的顶层必须是对象")
        result = execute(args.command, payload)
        # 序列化在写文件之前完成，也拒绝算法返回的 NaN 或 Infinity。
        text = json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
        if args.output is None:
            sys.stdout.write(text)
        else:
            _write_atomic(args.output, text)
    except (KeyError, ValueError, TypeError, OverflowError, OSError, RuntimeError, ImportError) as exc:
        parser.exit(2, f"算法输入或执行错误：{exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
