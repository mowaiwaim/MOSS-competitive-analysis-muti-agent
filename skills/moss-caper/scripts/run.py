"""从仓库任意位置启动共享 CAPER CLI，不包含算法副本。"""

from pathlib import Path
import sys


def main(argv=None):
    # 从脚本位置找仓库，避免依赖工作目录或机器绝对路径。
    root = next((parent for parent in Path(__file__).resolve().parents
                 if (parent / "competitive_evidence" / "algorithms.py").is_file()
                 and (parent / "competitive_evidence" / "caper.py").is_file()), None)
    if root is None:
        print("未找到 MOSS 共享算法。请保留本 Skill 在 MOSS 仓库中的目录结构。", file=sys.stderr)
        return 2
    sys.path.insert(0, str(root))
    from competitive_evidence.algorithms import main as run_algorithm
    return run_algorithm(["caper", *(sys.argv[1:] if argv is None else argv)])


if __name__ == "__main__":
    raise SystemExit(main())
