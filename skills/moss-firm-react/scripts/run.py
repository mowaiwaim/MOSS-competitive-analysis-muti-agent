"""转发 FIRM 规划或观察命令，不执行输入指定的外部工具。"""

import argparse
from pathlib import Path
import sys


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__,
                                     epilog="其余参数转交共享 CLI：--input 文件 [--output 文件]")
    parser.add_argument("operation", choices=("plan", "observe"))
    args, remaining = parser.parse_known_args(argv)
    # 从脚本位置找共享实现，不依赖工作目录或机器绝对路径。
    root = next((parent for parent in Path(__file__).resolve().parents
                 if (parent / "competitive_evidence" / "algorithms.py").is_file()
                 and (parent / "competitive_evidence" / "firm.py").is_file()), None)
    if root is None:
        print("未找到 MOSS 共享算法。请保留本 Skill 在 MOSS 仓库中的目录结构。", file=sys.stderr)
        return 2
    sys.path.insert(0, str(root))
    from competitive_evidence.algorithms import main as run_algorithm
    return run_algorithm(["firm-" + args.operation, *remaining])


if __name__ == "__main__":
    raise SystemExit(main())
