"""一键运行入口（第七阶段）。

把原来需要手动执行的一长串脚本封装成一条命令::

    python src/run_all.py

可选参数::

    python src/run_all.py --input_dir data/sample --output_root outputs
    python src/run_all.py --analysis_goal "构建一个用于 5 日收益率预测的日频建模宽表"
    python src/run_all.py --no_repair
    python src/run_all.py --skip_report
    python src/run_all.py --clean_outputs
    python src/run_all.py --verbose

行为：
1. 构造 PipelineRunner（复用前六阶段内部类）。
2. 运行完整 pipeline（含 auto_repair / skip_report 逻辑）。
3. 保存 session log（latest_session.json + session_YYYYMMDD_HHMMSS.json）。
4. 打印 summary dashboard。

设计原则：
- 只作为统一入口，业务调度放在 PipelineRunner 中，不复制粘贴业务逻辑。
- 不调用外部 LLM API，离线可运行，不连接真实券商系统，不获取真实市场数据。
- 路径用 pathlib，兼容 Windows，不写死绝对路径。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# 让脚本无论从哪里调用都能 import 同级模块
HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from pipeline_runner import PipelineRunner  # noqa: E402


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="One-click runner for the full Financial Table Workflow Agent pipeline."
    )
    p.add_argument(
        "--input_dir",
        default="data/sample",
        help="Directory containing raw CSV files (default: data/sample)",
    )
    p.add_argument(
        "--output_root",
        default="outputs",
        help="Root directory for all stage outputs (default: outputs)",
    )
    p.add_argument(
        "--analysis_goal",
        default=None,
        help="Downstream analysis goal. If omitted, the planner default is used.",
    )
    p.add_argument(
        "--no_repair",
        action="store_true",
        help="Do not auto-run the Repair Loop even if the initial Critic fails.",
    )
    p.add_argument(
        "--skip_report",
        action="store_true",
        help="Skip the Final Report Generator stage.",
    )
    p.add_argument(
        "--clean_outputs",
        action="store_true",
        help="Remove the output_root directory before running (use with care).",
    )
    p.add_argument(
        "--verbose",
        action="store_true",
        help="Print verbose progress and tracebacks.",
    )
    return p.parse_args()


def _clean_outputs(output_root: Path) -> None:
    """删除 output_root 目录（用于 --clean_outputs）。"""
    import shutil

    if output_root.exists():
        print(f"[run_all] cleaning {output_root} ...")
        shutil.rmtree(output_root)


def main() -> int:
    args = parse_args()
    input_dir = Path(args.input_dir)
    output_root = Path(args.output_root)

    if args.clean_outputs:
        _clean_outputs(output_root)

    runner = PipelineRunner(
        input_dir=input_dir,
        output_root=output_root,
        analysis_goal=args.analysis_goal,
        auto_repair=not args.no_repair,
        skip_report=args.skip_report,
        verbose=args.verbose,
    )

    runner.run_full_pipeline()
    session_log = runner.save_session_log()
    runner.print_dashboard()

    print()
    print(f"[run_all] session log saved: {session_log}")

    # 退出码：任一阶段 failed 则返回 1
    failed = [
        s for s, rec in runner.stages.items() if rec["status"] == "failed"
    ]
    if failed:
        print(
            f"[run_all] pipeline completed with failed stages: {failed}",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
