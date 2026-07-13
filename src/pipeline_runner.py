"""PipelineRunner（第七阶段）：统一调度器。

把原来需要手动执行的一长串脚本（run_profile / run_planner / run_executor /
run_critic / run_repair / run_critic / run_report_generator）封装成一个可编程的
调度器，供 run_all.py（一键运行）与 agent_shell.py（交互式 shell）复用。

设计原则：
- 不删除/重写前六阶段代码，本模块只**复用**它们的内部类（FinancialTableProfiler /
  WorkflowPlanner / CodeExecutor / ValidityCritic / RepairLoop / ReportGenerator）。
- 不调用任何外部 LLM API，离线可运行。
- 不连接真实券商系统，不获取真实市场数据，不训练模型，不输出投资建议。
- 路径用 pathlib，兼容 Windows，不写死绝对路径。
- 每个阶段运行后记录 status / start_time / end_time / duration / output_files /
  summary / error_message；失败不静默吞掉。
- 生成 outputs/sessions/latest_session.json 与 session_YYYYMMDD_HHMMSS.json。
"""

from __future__ import annotations

import json
import sys
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

# 让脚本无论从哪里调用都能 import 同级模块
HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from critic import ValidityCritic  # noqa: E402
from executor import CodeExecutor  # noqa: E402
from generate_sample_data import generate_sample_data  # noqa: E402
from planner import DEFAULT_ANALYSIS_GOAL, WorkflowPlanner  # noqa: E402
from profiler import FinancialTableProfiler  # noqa: E402
from repair import RepairLoop  # noqa: E402
from report_generator import ReportGenerator  # noqa: E402

RUNNER_VERSION = "0.1"

# 阶段顺序与展示名
STAGE_ORDER = [
    "profile",
    "planner",
    "executor",
    "initial_critic",
    "repair",
    "repaired_critic",
    "final_report",
]

STAGE_DISPLAY = {
    "profile": "Stage 1 Data Profiler",
    "planner": "Stage 2 Workflow Planner",
    "executor": "Stage 3 Code Executor",
    "initial_critic": "Stage 4 Validity Critic",
    "repair": "Stage 5 Repair Loop",
    "repaired_critic": "Stage 6 Re-run Critic",
    "final_report": "Stage 7 Final Report",
}


class PipelineRunner:
    """统一调度器：复用前六阶段内部类，按顺序运行并记录状态。

    用法::

        runner = PipelineRunner(
            input_dir="data/sample",
            output_root="outputs",
            analysis_goal=None,
            auto_repair=True,
        )
        runner.run_full_pipeline()
        status = runner.get_status()
        runner.save_session_log()
    """

    def __init__(
        self,
        input_dir: str | Path = "data/sample",
        output_root: str | Path = "outputs",
        analysis_goal: str | None = None,
        auto_repair: bool = True,
        skip_report: bool = False,
        verbose: bool = False,
    ) -> None:
        self.input_dir = Path(input_dir)
        self.output_root = Path(output_root)
        self.analysis_goal = analysis_goal or DEFAULT_ANALYSIS_GOAL
        self.auto_repair = auto_repair
        self.skip_report = skip_report
        self.verbose = verbose

        # 各阶段输出目录（与前六阶段默认保持一致）
        self.profiles_dir = self.output_root / "profiles"
        self.plans_dir = self.output_root / "plans"
        self.prepared_dir = self.output_root / "prepared"
        self.validation_dir = self.output_root / "validation"
        self.repaired_dir = self.output_root / "repaired"
        self.validation_repaired_dir = self.output_root / "validation_repaired"
        self.final_report_dir = self.output_root / "final_report"
        self.sessions_dir = self.output_root / "sessions"

        # 关键产物路径（供后续阶段与 shell 读取）
        self.profile_json = self.profiles_dir / "profile.json"
        self.profile_md = self.profiles_dir / "profile_report.md"
        self.plan_json = self.plans_dir / "workflow_plan.json"
        self.plan_md = self.plans_dir / "workflow_plan_report.md"
        self.prepared_panel = self.prepared_dir / "prepared_panel.csv"
        self.data_dictionary = self.prepared_dir / "data_dictionary.json"
        self.execution_log = self.prepared_dir / "execution_log.json"
        self.execution_report = self.prepared_dir / "execution_report.md"
        self.initial_validation_json = self.validation_dir / "validation_report.json"
        self.initial_validation_md = self.validation_dir / "validation_report.md"
        self.initial_approved = self.validation_dir / "approved_feature_columns.json"
        self.repair_plan = self.repaired_dir / "repair_plan.json"
        self.repaired_panel = self.repaired_dir / "repaired_panel.csv"
        self.repair_log = self.repaired_dir / "repair_log.json"
        self.repair_report = self.repaired_dir / "repair_report.md"
        self.final_validation_json = (
            self.validation_repaired_dir / "validation_report.json"
        )
        self.final_validation_md = (
            self.validation_repaired_dir / "validation_report.md"
        )
        self.final_approved = (
            self.validation_repaired_dir / "approved_feature_columns.json"
        )
        self.summary_json = self.final_report_dir / "final_workflow_summary.json"
        self.full_report_md = self.final_report_dir / "final_workflow_report.md"
        self.one_page_md = self.final_report_dir / "final_workflow_one_page.md"
        self.artifacts_index = (
            self.final_report_dir / "pipeline_artifacts_index.json"
        )

        # executor.py 源码路径（Critic 静态检查需要）
        self.executor_source = HERE / "executor.py"
        self.calendar_csv = self.input_dir / "calendar.csv"

        # 阶段状态记录
        self.stages: dict[str, dict[str, Any]] = {
            s: self._fresh_stage_record(s) for s in STAGE_ORDER
        }
        self._final_summary_cache: dict[str, Any] | None = None

    # ------------------------------------------------------------------
    # 公共：单阶段运行
    # ------------------------------------------------------------------

    def run_profile(self) -> dict[str, Any]:
        """Stage 1: Data Profiler。"""
        return self._run_stage("profile", self._profile_impl)

    def run_planner(self) -> dict[str, Any]:
        """Stage 2: Workflow Planner。"""
        return self._run_stage("planner", self._planner_impl)

    def run_executor(self) -> dict[str, Any]:
        """Stage 3: Code Executor。"""
        return self._run_stage("executor", self._executor_impl)

    def run_initial_critic(self) -> dict[str, Any]:
        """Stage 4: Validity Critic（初始）。"""
        return self._run_stage("initial_critic", self._initial_critic_impl)

    def run_repair(self) -> dict[str, Any]:
        """Stage 5: Repair Loop。"""
        return self._run_stage("repair", self._repair_impl)

    def run_repaired_critic(self) -> dict[str, Any]:
        """Stage 6: 对 repaired panel 重新运行 Critic。"""
        return self._run_stage("repaired_critic", self._repaired_critic_impl)

    def run_final_report(self) -> dict[str, Any]:
        """Stage 7: Final Report Generator。"""
        return self._run_stage("final_report", self._final_report_impl)

    # ------------------------------------------------------------------
    # 公共：完整 pipeline
    # ------------------------------------------------------------------

    def run_full_pipeline(self) -> dict[str, Any]:
        """一键运行完整 workflow，含 auto_repair 与 skip_report 逻辑。"""
        self._log("Financial Table Workflow Agent — full pipeline start")
        self._log(f"Input dir: {self.input_dir}")
        self._log(f"Output root: {self.output_root}")
        self._log(f"Analysis goal: {self.analysis_goal}")
        self._log(f"Auto repair: {self.auto_repair}")
        self._log(f"Skip report: {self.skip_report}")

        # 1. profile
        self.run_profile()
        if self.stages["profile"]["status"] == "failed":
            self._fail_fast("profile")
            return self.get_status()

        # 2. planner
        self.run_planner()
        if self.stages["planner"]["status"] == "failed":
            self._fail_fast("planner")
            return self.get_status()

        # 3. executor
        self.run_executor()
        if self.stages["executor"]["status"] == "failed":
            self._fail_fast("executor")
            return self.get_status()

        # 4. initial critic
        self.run_initial_critic()
        if self.stages["initial_critic"]["status"] == "failed":
            self._fail_fast("initial_critic")
            return self.get_status()

        initial_status = self.stages["initial_critic"]["summary"].get(
            "overall_status", "unknown"
        )

        # 5. repair（仅当 initial critic failed 且 auto_repair=True）
        if initial_status == "failed" and self.auto_repair:
            self.run_repair()
            if self.stages["repair"]["status"] == "failed":
                self._fail_fast("repair")
                return self.get_status()
            # 6. repaired critic
            self.run_repaired_critic()
            if self.stages["repaired_critic"]["status"] == "failed":
                self._fail_fast("repaired_critic")
                return self.get_status()
        else:
            # 跳过实际 repair 与 repaired critic，但生成统一 no-op 产物，
            # 让 final_report 阶段的输入全部存在。区分两种 no-op：
            #   - no_repair_needed: initial critic 未失败（passed/passed_with_warnings）
            #   - repair_disabled:  initial critic failed 但 --no_repair（最终仍 failed）
            if initial_status == "failed" and not self.auto_repair:
                no_op_kind = "repair_disabled"
            else:
                no_op_kind = "no_repair_needed"
            self._mark_skipped("repair", reason=self._skip_repair_reason(initial_status))
            self._mark_skipped(
                "repaired_critic", reason="repair skipped; no re-critic needed"
            )
            self._write_noop_repair_artifacts(initial_status, no_op_kind)

        # 7. final report
        if not self.skip_report:
            self.run_final_report()
            if self.stages["final_report"]["status"] == "failed":
                self._fail_fast("final_report")
                return self.get_status()
        else:
            self._mark_skipped("final_report", reason="--skip_report set")

        self._log("Full pipeline finished.")
        return self.get_status()

    # ------------------------------------------------------------------
    # 公共：状态与 session log
    # ------------------------------------------------------------------

    def get_status(self) -> dict[str, Any]:
        """返回当前 pipeline 状态快照。"""
        final_status = self._read_final_validation_status()
        initial_status = self._read_initial_validation_status()
        rows_prepared = self._count_rows(self.prepared_panel)
        rows_repaired = self._count_rows(self.repaired_panel)
        rows_removed = self._read_rows_removed()

        approved, label_col, label_in_features = self._read_approved_features()

        return {
            "project": "financial_table_workflow_agent",
            "runner_version": RUNNER_VERSION,
            "input_dir": str(self.input_dir).replace("\\", "/"),
            "output_root": str(self.output_root).replace("\\", "/"),
            "analysis_goal": self.analysis_goal,
            "auto_repair": self.auto_repair,
            "skip_report": self.skip_report,
            "stages": {s: dict(self.stages[s]) for s in STAGE_ORDER},
            "initial_validation_status": initial_status,
            "final_validation_status": final_status,
            "prepared_panel_rows": rows_prepared,
            "repaired_panel_rows": rows_repaired,
            "rows_removed_by_repair": rows_removed,
            "failed_checks_initial": self._read_failed_count(
                self.initial_validation_json
            ),
            "failed_checks_final": self._read_failed_count(
                self.final_validation_json
            ),
            "approved_feature_columns": approved,
            "label_column": label_col,
            "label_in_approved_features": label_in_features,
            "final_report_path": (
                str(self.full_report_md).replace("\\", "/")
                if self.full_report_md.exists()
                else None
            ),
            "one_page_path": (
                str(self.one_page_md).replace("\\", "/")
                if self.one_page_md.exists()
                else None
            ),
            "session_log_path": (
                str(self.sessions_dir / "latest_session.json").replace("\\", "/")
            ),
        }

    def save_session_log(self) -> Path:
        """保存 session log：latest_session.json + session_YYYYMMDD_HHMMSS.json。"""
        self.sessions_dir.mkdir(parents=True, exist_ok=True)
        status = self.get_status()
        # 用固定时间戳，避免同一秒覆盖；run_all/shell 传入的时间戳由调用方控制
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        payload = {
            "project": "financial_table_workflow_agent",
            "runner_version": RUNNER_VERSION,
            "generated_at": ts,
            "status": status,
        }

        latest_path = self.sessions_dir / "latest_session.json"
        with latest_path.open("w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)

        stamped_path = self.sessions_dir / f"session_{ts}.json"
        with stamped_path.open("w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)

        return latest_path

    # ------------------------------------------------------------------
    # dashboard 打印（供 run_all / shell 复用）
    # ------------------------------------------------------------------

    def print_dashboard(self) -> None:
        """打印 summary dashboard。"""
        status = self.get_status()
        print()
        print("[run_all] Financial Table Workflow Agent")
        print()
        print(f"Input dir: {status['input_dir']}")
        print(f"Output root: {status['output_root']}")
        goal = status["analysis_goal"]
        if len(goal) > 80:
            goal = goal[:77] + "..."
        print(f"Analysis goal: {goal}")
        print()
        for s in STAGE_ORDER:
            display = STAGE_DISPLAY[s]
            st = status["stages"][s]["status"]
            dots = "." * max(2, 38 - len(display))
            print(f"{display} {dots} {st}")
        print()
        print(f"Final status: {status['final_validation_status'] or 'n/a'}")
        if status["prepared_panel_rows"] is not None and (
            status["repaired_panel_rows"] is not None
        ):
            print(
                f"Rows: {status['prepared_panel_rows']} -> "
                f"{status['repaired_panel_rows']}"
            )
        elif status["prepared_panel_rows"] is not None:
            print(f"Rows: {status['prepared_panel_rows']}")
        print(f"Rows removed by repair: {status['rows_removed_by_repair']}")
        label_in = status["label_in_approved_features"]
        label_msg = (
            "FAILED (label in features!)" if label_in else "passed"
        )
        print(f"Label leakage: {label_msg}")
        print(f"Approved features: {len(status['approved_feature_columns'])}")
        if status["final_report_path"]:
            print(f"Final report: {status['final_report_path']}")
        if status["one_page_path"]:
            print(f"One-page summary: {status['one_page_path']}")
        print(f"Session log: {status['session_log_path']}")

    # ------------------------------------------------------------------
    # 内部：各阶段实现
    # ------------------------------------------------------------------

    def _profile_impl(self) -> dict[str, Any]:
        """Stage 1 实现：必要时生成样例数据，再剖析。"""
        # 若 input_dir 下没有 CSV，自动生成样例数据
        csv_files = (
            sorted(self.input_dir.glob("*.csv"))
            if self.input_dir.exists()
            else []
        )
        if not csv_files:
            print(
                "[pipeline] Sample data missing; generating synthetic sample "
                "data for demo."
            )
            generate_sample_data(self.input_dir)
            csv_files = sorted(self.input_dir.glob("*.csv"))
        if not csv_files:
            raise RuntimeError(f"no CSV files in {self.input_dir}")

        profiler = FinancialTableProfiler(self.input_dir)
        profile = profiler.run()
        profiler.save_json(profile, self.profile_json)
        profiler.save_markdown(profile, self.profile_md)

        n_tables = len(profile.get("tables", []))
        total_issues = sum(
            len(t.get("potential_issues", [])) for t in profile.get("tables", [])
        )
        total_issues += len(
            profile.get("cross_table_findings", {}).get(
                "global_potential_issues", []
            )
        )
        return {
            "output_files": [
                str(self.profile_json).replace("\\", "/"),
                str(self.profile_md).replace("\\", "/"),
            ],
            "summary": {
                "n_tables": n_tables,
                "total_issues": total_issues,
            },
        }

    def _planner_impl(self) -> dict[str, Any]:
        """Stage 2 实现。"""
        if not self.profile_json.exists():
            raise FileNotFoundError(
                f"profile not found: {self.profile_json}. Run profile first."
            )
        planner = WorkflowPlanner()
        profile = planner.load_profile(self.profile_json)
        plan = planner.build_plan(profile, self.analysis_goal)
        plan["input_profile_path"] = str(self.profile_json).replace("\\", "/")
        planner.save_plan(plan, self.plan_json)
        planner.save_markdown_report(plan, self.plan_md)

        return {
            "output_files": [
                str(self.plan_json).replace("\\", "/"),
                str(self.plan_md).replace("\\", "/"),
            ],
            "summary": {
                "n_workflow_steps": len(plan.get("workflow_steps", [])),
                "n_validation_checks": len(
                    plan.get("validation_plan", {}).get("checks", [])
                ),
                "analysis_goal": self.analysis_goal,
            },
        }

    def _executor_impl(self) -> dict[str, Any]:
        """Stage 3 实现。"""
        if not self.plan_json.exists():
            raise FileNotFoundError(
                f"plan not found: {self.plan_json}. Run planner first."
            )
        ex = CodeExecutor()
        plan = ex.load_workflow_plan(self.plan_json)
        result = ex.execute(plan, self.input_dir)
        paths = ex.save_outputs(result, self.prepared_dir)
        ex.save_execution_report(result, self.prepared_dir)

        panel: pd.DataFrame = result["panel"]
        pk_unique = not panel.duplicated(subset=["date", "ticker"]).any()
        fts = result["execution_log"].get("final_table_summary", {})
        return {
            "output_files": [str(p).replace("\\", "/") for p in paths.values()],
            "summary": {
                "n_rows": int(len(panel)),
                "n_columns": int(panel.shape[1]),
                "primary_key_unique": bool(pk_unique),
                "date_min": fts.get("date_min"),
                "date_max": fts.get("date_max"),
            },
        }

    def _initial_critic_impl(self) -> dict[str, Any]:
        """Stage 4 实现：对 prepared_panel 运行 Critic。"""
        self._check_critic_inputs(self.prepared_panel)
        report = self._run_critic(
            panel_path=self.prepared_panel,
            output_dir=self.validation_dir,
        )
        s = report.get("summary", {})
        return {
            "output_files": [
                str(self.initial_validation_json).replace("\\", "/"),
                str(self.initial_validation_md).replace("\\", "/"),
                str(self.initial_approved).replace("\\", "/"),
            ],
            "summary": {
                "overall_status": report.get("overall_status", "unknown"),
                "total_checks": s.get("total_checks"),
                "passed": s.get("passed"),
                "warnings": s.get("warnings"),
                "failed": s.get("failed"),
            },
        }

    def _repair_impl(self) -> dict[str, Any]:
        """Stage 5 实现：Repair Loop。"""
        for label, path in [
            ("panel", self.prepared_panel),
            ("validation_report", self.initial_validation_json),
            ("data_dictionary", self.data_dictionary),
            ("approved_features", self.initial_approved),
        ]:
            if not Path(path).exists():
                raise FileNotFoundError(f"{label} not found: {path}")

        loop = RepairLoop()
        loop.load_inputs(
            panel_path=self.prepared_panel,
            validation_report_path=self.initial_validation_json,
            data_dictionary_path=self.data_dictionary,
            approved_features_path=self.initial_approved,
        )
        plan = loop.build_repair_plan()
        result = loop.apply_repairs(plan)
        paths = loop.save_outputs(result, self.repaired_dir)
        loop.save_report(result, self.repaired_dir)

        log = result["repair_log"]
        return {
            "output_files": [str(p).replace("\\", "/") for p in paths.values()]
            + [str(self.repair_report).replace("\\", "/")],
            "summary": {
                "rows_before": log.get("rows_before"),
                "rows_after": log.get("rows_after"),
                "rows_removed": log.get("rows_removed"),
                "input_validation_status": log.get("input_validation_status"),
            },
        }

    def _repaired_critic_impl(self) -> dict[str, Any]:
        """Stage 6 实现：对 repaired_panel 重新运行 Critic。"""
        if not self.repaired_panel.exists():
            raise FileNotFoundError(
                f"repaired panel not found: {self.repaired_panel}. Run repair first."
            )
        report = self._run_critic(
            panel_path=self.repaired_panel,
            output_dir=self.validation_repaired_dir,
        )
        s = report.get("summary", {})
        return {
            "output_files": [
                str(self.final_validation_json).replace("\\", "/"),
                str(self.final_validation_md).replace("\\", "/"),
                str(self.final_approved).replace("\\", "/"),
            ],
            "summary": {
                "overall_status": report.get("overall_status", "unknown"),
                "total_checks": s.get("total_checks"),
                "passed": s.get("passed"),
                "warnings": s.get("warnings"),
                "failed": s.get("failed"),
            },
        }

    def _final_report_impl(self) -> dict[str, Any]:
        """Stage 7 实现：Final Report Generator。"""
        inputs = [
            self.profile_json,
            self.plan_json,
            self.prepared_panel,
            self.execution_log,
            self.initial_validation_json,
            self.repair_plan,
            self.repair_log,
            self.repaired_panel,
            self.final_validation_json,
            self.final_approved,
            self.data_dictionary,
        ]
        for p in inputs:
            if not p.exists():
                raise FileNotFoundError(f"report input not found: {p}")

        gen = ReportGenerator()
        gen.load_inputs(
            profile_json=self.profile_json,
            workflow_plan_json=self.plan_json,
            prepared_panel=self.prepared_panel,
            execution_log=self.execution_log,
            initial_validation_report=self.initial_validation_json,
            repair_plan=self.repair_plan,
            repair_log=self.repair_log,
            repaired_panel=self.repaired_panel,
            final_validation_report=self.final_validation_json,
            approved_features=self.final_approved,
            data_dictionary=self.data_dictionary,
        )
        paths = gen.save_all(self.final_report_dir)
        summary = gen.build_summary()
        cl = summary.get("closed_loop_result", {})
        return {
            "output_files": [str(p).replace("\\", "/") for p in paths.values()],
            "summary": {
                "initial_validation_status": summary.get(
                    "initial_validation_status"
                ),
                "final_validation_status": summary.get(
                    "final_validation_status"
                ),
                "rows_removed_by_repair": summary.get("rows_removed_by_repair"),
                "one_line": cl.get("one_line", ""),
            },
        }

    # ------------------------------------------------------------------
    # 内部：Critic 复用
    # ------------------------------------------------------------------

    def _run_critic(
        self,
        panel_path: Path,
        output_dir: Path,
    ) -> dict[str, Any]:
        """对指定 panel 运行 Critic，输出到 output_dir。"""
        self._check_critic_inputs(panel_path)
        critic = ValidityCritic()
        critic.load_inputs(
            panel_path=panel_path,
            data_dictionary_path=self.data_dictionary,
            execution_log_path=self.execution_log,
            plan_path=self.plan_json,
            executor_source_path=self.executor_source,
            calendar_path=self.calendar_csv if self.calendar_csv.exists() else None,
        )
        report = critic.run_all_checks()
        critic.save_json_report(report, output_dir / "validation_report.json")
        critic.save_markdown_report(report, output_dir / "validation_report.md")
        critic.save_approved_feature_columns(
            report, output_dir / "approved_feature_columns.json"
        )
        return report

    def _check_critic_inputs(self, panel_path: Path) -> None:
        for label, path in [
            ("panel", panel_path),
            ("data_dictionary", self.data_dictionary),
            ("execution_log", self.execution_log),
            ("plan", self.plan_json),
            ("executor_source", self.executor_source),
        ]:
            if not Path(path).exists():
                raise FileNotFoundError(f"{label} not found: {path}")

    # ------------------------------------------------------------------
    # 内部：阶段执行框架
    # ------------------------------------------------------------------

    def _run_stage(
        self,
        stage: str,
        impl: "callable",  # type: ignore[type-arg]
    ) -> dict[str, Any]:
        """运行单个阶段，记录 start/end/duration/status/output/summary/error。"""
        rec = self.stages[stage]
        rec["status"] = "running"
        rec["start_time"] = _now_iso()
        start_dt = datetime.now()
        try:
            result = impl()
            end_dt = datetime.now()
            rec["end_time"] = _now_iso()
            rec["duration_seconds"] = round((end_dt - start_dt).total_seconds(), 3)
            rec["status"] = "completed"
            rec["output_files"] = result.get("output_files", [])
            rec["summary"] = result.get("summary", {})
            rec["error_message"] = None
            self._log(f"{STAGE_DISPLAY[stage]} ... completed")
        except Exception as exc:  # noqa: BLE001
            end_dt = datetime.now()
            rec["end_time"] = _now_iso()
            rec["duration_seconds"] = round((end_dt - start_dt).total_seconds(), 3)
            rec["status"] = "failed"
            rec["error_message"] = f"{type(exc).__name__}: {exc}"
            rec["traceback"] = traceback.format_exc()
            print(
                f"[pipeline] ERROR in {STAGE_DISPLAY[stage]}: "
                f"{type(exc).__name__}: {exc}",
                file=sys.stderr,
            )
            if self.verbose:
                traceback.print_exc()
        return rec

    def _mark_skipped(self, stage: str, reason: str) -> None:
        rec = self.stages[stage]
        rec["status"] = "skipped"
        rec["start_time"] = _now_iso()
        rec["end_time"] = rec["start_time"]
        rec["duration_seconds"] = 0.0
        rec["summary"] = {"skip_reason": reason}
        rec["error_message"] = None
        self._log(f"{STAGE_DISPLAY[stage]} ... skipped ({reason})")

    def _fail_fast(self, stage: str) -> None:
        print(
            f"[pipeline] {STAGE_DISPLAY[stage]} failed; stopping full pipeline.",
            file=sys.stderr,
        )

    def _skip_repair_reason(self, initial_status: str) -> str:
        if initial_status in ("passed", "passed_with_warnings"):
            return (
                f"initial critic status={initial_status} (not failed); "
                "no repair needed"
            )
        if not self.auto_repair:
            return "auto_repair=False; repair skipped by user"
        return f"initial critic status={initial_status}; repair not triggered"

    # ------------------------------------------------------------------
    # 内部：no-op repair 产物（无需 Repair 时生成，区分两种 kind）
    # ------------------------------------------------------------------

    def _write_noop_repair_artifacts(
        self, initial_status: str, no_op_kind: str
    ) -> None:
        """当无需实际 Repair 时生成统一 no-op 产物，让 final_report 输入齐全。

        no_op_kind:
          - no_repair_needed: initial critic 未失败（passed/passed_with_warnings）。
            final_status = initial_status。
          - repair_disabled:  initial critic failed 但 --no_repair。最终仍 failed。
        """
        import shutil

        self.repaired_dir.mkdir(parents=True, exist_ok=True)
        self.validation_repaired_dir.mkdir(parents=True, exist_ok=True)

        # 1. prepared_panel -> repaired_panel（原样复制，不修复）
        if self.prepared_panel.exists() and not self.repaired_panel.exists():
            shutil.copyfile(self.prepared_panel, self.repaired_panel)
        elif self.prepared_panel.exists():
            shutil.copyfile(self.prepared_panel, self.repaired_panel)

        # 行数实算
        rows_n = self._count_rows(self.prepared_panel) or 0

        # 2. repair_plan.json
        failed_checks: list[dict[str, Any]] = []
        if self.initial_validation_json.exists():
            try:
                with self.initial_validation_json.open("r", encoding="utf-8") as f:
                    init_report = json.load(f)
                failed_checks = [
                    {
                        "check_name": c.get("check_name"),
                        "category": c.get("category"),
                        "severity": c.get("severity"),
                        "status": c.get("status"),
                        "description": c.get("description"),
                        "evidence": c.get("evidence"),
                        "recommendation": c.get("recommendation"),
                    }
                    for c in init_report.get("checks", [])
                    if c.get("status") == "failed"
                ]
            except Exception:  # noqa: BLE001
                init_report = {}
        else:
            init_report = {}

        if no_op_kind == "no_repair_needed":
            plan_reason = "initial critic did not fail; no repair needed"
            log_next = "no repair needed; initial validation copied as repaired validation"
        else:  # repair_disabled
            plan_reason = "initial critic failed but --no_repair set; repair disabled by user"
            log_next = "repair disabled; panel unchanged; final status remains failed"

        repair_plan = {
            "project": "financial_table_workflow_agent",
            "repair_version": "0.1",
            "input_validation_status": initial_status,
            "failed_checks": failed_checks,
            "warning_checks": [],
            "repair_actions": [],
            "not_repaired_items": [],
            "next_validation_required": False,
            "no_op": True,
            "no_op_kind": no_op_kind,
            "reason": plan_reason,
        }
        with self.repair_plan.open("w", encoding="utf-8") as f:
            json.dump(repair_plan, f, ensure_ascii=False, indent=2)

        # 3. repair_log.json
        checks_after = self._noop_checks_after_repair()
        repair_log = {
            "project": "financial_table_workflow_agent",
            "repair_version": "0.1",
            "input_panel_path": str(self.prepared_panel).replace("\\", "/"),
            "input_validation_report_path": str(self.initial_validation_json).replace("\\", "/"),
            "rows_before": rows_n,
            "rows_after": rows_n,
            "rows_removed": 0,
            "actions_applied": [],
            "checks_after_repair": checks_after,
            "warnings": [],
            "no_op": True,
            "no_op_kind": no_op_kind,
            "next_step": log_next,
        }
        with self.repair_log.open("w", encoding="utf-8") as f:
            json.dump(repair_log, f, ensure_ascii=False, indent=2)

        # 4. repair_report.md
        self.repair_report.write_text(
            self._render_noop_repair_report(initial_status, no_op_kind, rows_n, failed_checks),
            encoding="utf-8",
        )

        # 5. 复制 initial validation -> repaired validation（json/md/approved）
        for src, dst in [
            (self.initial_validation_json, self.final_validation_json),
            (self.initial_validation_md, self.final_validation_md),
            (self.initial_approved, self.final_approved),
        ]:
            if src.exists():
                shutil.copyfile(src, dst)

        self._log(
            f"no-op repair artifacts written (kind={no_op_kind}, "
            f"initial_status={initial_status})"
        )

    def _noop_checks_after_repair(self) -> dict[str, Any]:
        """no-op 场景的 checks_after_repair（实算自 prepared_panel）。"""
        close_missing = -1
        pk_dup = -1
        label_preserved = False
        label_not_in_features = True
        approved: list[str] = []
        if self.prepared_panel.exists():
            try:
                df = pd.read_csv(self.prepared_panel)
                close_missing = int(df["close"].isna().sum()) if "close" in df.columns else -1
                if all(c in df.columns for c in ["date", "ticker"]):
                    pk_dup = int(df.duplicated(subset=["date", "ticker"]).sum())
                label_preserved = "label_next_5d" in df.columns
            except Exception:  # noqa: BLE001
                pass
        if self.initial_approved.exists():
            try:
                with self.initial_approved.open("r", encoding="utf-8") as f:
                    approved = json.load(f).get("approved_feature_columns", [])
                label_not_in_features = "label_next_5d" not in approved
            except Exception:  # noqa: BLE001
                pass
        return {
            "close_missing_count": close_missing,
            "primary_key_unique": pk_dup == 0,
            "primary_key_duplicate_count": pk_dup,
            "label_column_preserved": label_preserved,
            "label_not_in_approved_features": label_not_in_features,
            "approved_feature_columns_unchanged": approved,
        }

    def _render_noop_repair_report(
        self,
        initial_status: str,
        no_op_kind: str,
        rows_n: int | None,
        failed_checks: list[dict[str, Any]],
    ) -> str:
        lines: list[str] = []
        lines.append("# Repair Loop Report (no-op)")
        lines.append("")
        lines.append("- project: `financial_table_workflow_agent`  |  repair_version: `0.1`")
        lines.append(f"- no_op_kind: `{no_op_kind}`")
        lines.append(f"- input_validation_status: `{initial_status}`")
        lines.append("")
        if no_op_kind == "no_repair_needed":
            lines.append("## 1. Why No Repair Was Needed")
            lines.append("")
            lines.append(
                f"The initial Validity Critic reported `overall_status = {initial_status}`, "
                "which is not `failed`. No failed checks require repair, so the Repair Loop "
                "was skipped and `prepared_panel.csv` was copied unchanged to "
                "`repaired_panel.csv`. The initial validation report was copied as the "
                "repaired (re-run) validation report."
            )
        else:  # repair_disabled
            lines.append("## 1. Why Repair Was Disabled")
            lines.append("")
            lines.append(
                f"The initial Validity Critic reported `overall_status = {initial_status}` "
                "(failed), but `--no_repair` was set, so the Repair Loop was disabled by "
                "the user. `prepared_panel.csv` was copied unchanged to `repaired_panel.csv`; "
                "the panel is NOT repaired and the final validation status remains `failed`."
            )
        lines.append("")
        lines.append("## 2. Failed Checks From Initial Critic")
        lines.append("")
        if failed_checks:
            lines.append("| check_name | category | description |")
            lines.append("|---|---|---|")
            for c in failed_checks:
                lines.append(
                    f"| {c.get('check_name')} | {c.get('category')} | {c.get('description')} |"
                )
        else:
            lines.append("(none)")
        lines.append("")
        lines.append("## 3. Repair Result (no-op)")
        lines.append("")
        lines.append(f"- rows before: {rows_n}")
        lines.append(f"- rows after: {rows_n}")
        lines.append("- rows removed: 0")
        lines.append("- actions applied: (none)")
        lines.append("")
        lines.append("## 4. Next Step")
        lines.append("")
        if no_op_kind == "no_repair_needed":
            lines.append("No repair needed; the Final Report Generator reads the copied "
                         "validation artifacts and proceeds.")
        else:
            lines.append("Repair is disabled; the final status remains `failed`. Re-run "
                         "without `--no_repair` to enable the Repair Loop.")
        lines.append("")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # 内部：读取产物状态
    # ------------------------------------------------------------------

    def _read_final_validation_status(self) -> str | None:
        if self.final_validation_json.exists():
            try:
                with self.final_validation_json.open("r", encoding="utf-8") as f:
                    return json.load(f).get("overall_status")
            except Exception:  # noqa: BLE001
                return None
        return None

    def _read_initial_validation_status(self) -> str | None:
        if self.initial_validation_json.exists():
            try:
                with self.initial_validation_json.open("r", encoding="utf-8") as f:
                    return json.load(f).get("overall_status")
            except Exception:  # noqa: BLE001
                return None
        return None

    def _read_rows_removed(self) -> int | None:
        if self.repair_log.exists():
            try:
                with self.repair_log.open("r", encoding="utf-8") as f:
                    return int(json.load(f).get("rows_removed", 0))
            except Exception:  # noqa: BLE001
                return None
        return None

    def _read_failed_count(self, path: Path) -> int | None:
        if not path.exists():
            return None
        try:
            with path.open("r", encoding="utf-8") as f:
                return int(json.load(f).get("summary", {}).get("failed", 0))
        except Exception:  # noqa: BLE001
            return None

    def _read_approved_features(
        self,
    ) -> tuple[list[str], str, bool]:
        """返回 (approved_features, label_column, label_in_features)。"""
        for path in (self.final_approved, self.initial_approved):
            if path.exists():
                try:
                    with path.open("r", encoding="utf-8") as f:
                        data = json.load(f)
                    approved = data.get("approved_feature_columns", [])
                    label_col = data.get("label_column", "label_next_5d")
                    return approved, label_col, label_col in approved
                except Exception:  # noqa: BLE001
                    continue
        return [], "label_next_5d", False

    @staticmethod
    def _count_rows(path: Path) -> int | None:
        if not path.exists():
            return None
        try:
            df = pd.read_csv(path)
            return int(len(df))
        except Exception:  # noqa: BLE001
            return None

    @staticmethod
    def _fresh_stage_record(stage: str) -> dict[str, Any]:
        return {
            "stage": stage,
            "display": STAGE_DISPLAY[stage],
            "status": "pending",
            "start_time": None,
            "end_time": None,
            "duration_seconds": None,
            "output_files": [],
            "summary": {},
            "error_message": None,
        }

    def _log(self, msg: str) -> None:
        if self.verbose:
            print(f"[pipeline] {msg}")


def _now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
