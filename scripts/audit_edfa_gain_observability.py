from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.analysis_pipeline import select_metrics_for_device, summarize_entity  # noqa: E402
from core.experiment_service import get_run_dir, load_registry  # noqa: E402
from core.io_utils import read_json, write_json  # noqa: E402
from core.telemetry_service import load_telemetry  # noqa: E402


def audit_run(run: dict[str, Any], *, pre_window: float, post_window: float) -> dict[str, Any]:
    """核对单个增益衰退实验的注入参数与真实实体遥测变化。"""

    run_dir = get_run_dir(run)
    ground_truth = read_json(run_dir / "ground_truth.json")
    injection = ground_truth.get("fault_injection") if isinstance(ground_truth, dict) else {}
    injection = injection if isinstance(injection, dict) else {}
    details = injection.get("details") if isinstance(injection.get("details"), dict) else {}
    entity_id = str(injection.get("faulty_entity_id") or run.get("fault_entity") or "")
    trigger_tick = float(injection.get("trigger_time_simulation") or run.get("trigger_tick") or 0.0)
    expected_drop_db = float(details.get("drop_db") or 0.0)

    records, errors = load_telemetry(run_dir, "edfa")
    metrics = select_metrics_for_device("edfa", records)
    summaries = summarize_entity(
        records,
        entity_id=entity_id,
        metrics=metrics,
        trigger_tick=trigger_tick,
        pre_window=pre_window,
        post_window=post_window,
    )
    changes = {str(item.get("metric")): item for item in summaries if item.get("metric")}
    gain_change = changes.get("actual_gain_db") or {}
    gain_delta = gain_change.get("delta")
    threshold = max(0.5, expected_drop_db * 0.25)
    gain_drop_observed = isinstance(gain_delta, (int, float)) and float(gain_delta) <= -threshold
    target_record_count = sum(1 for item in records if item.get("entity_id") == entity_id)

    return {
        "run_id": run.get("run_id"),
        "fault_entity": entity_id,
        "trigger_tick": trigger_tick,
        "expected_drop_db": expected_drop_db,
        "target_record_count": target_record_count,
        "gain_pre_mean": gain_change.get("pre_mean"),
        "gain_post_mean": gain_change.get("post_mean"),
        "gain_delta": gain_delta,
        "gain_drop_observed": gain_drop_observed,
        "output_power_delta": (changes.get("output_power_dbm") or {}).get("delta"),
        "output_osnr_delta": (changes.get("output_osnr_db") or {}).get("delta"),
        "output_gsnr_delta": (changes.get("output_gsnr_db") or {}).get("delta"),
        "errors": errors,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="审计 EDFA 增益衰退注入是否反映到 telemetry。")
    parser.add_argument("--limit", type=int, default=0, help="最多审计多少组；0 表示全部。")
    parser.add_argument("--pre-window", type=float, default=30.0)
    parser.add_argument("--post-window", type=float, default=30.0)
    parser.add_argument("--output", default="")
    parser.add_argument(
        "--batch-id",
        action="append",
        default=[],
        help="只审计指定批次；可重复传入。",
    )
    args = parser.parse_args()

    runs = [
        item
        for item in load_registry()
        if item.get("status") == "VALID" and item.get("scenario") == "EDFA_GAIN_DEGRADATION"
        and (not args.batch_id or item.get("batch_id") in set(args.batch_id))
    ]
    if args.limit > 0:
        runs = runs[: args.limit]
    rows = [audit_run(run, pre_window=args.pre_window, post_window=args.post_window) for run in runs]
    observed = [row for row in rows if row.get("gain_drop_observed")]
    missing_entity = [row for row in rows if not row.get("target_record_count")]
    summary = {
        "total": len(rows),
        "gain_drop_observed": len(observed),
        "gain_drop_observable_rate": len(observed) / len(rows) if rows else 0.0,
        "missing_target_entity": len(missing_entity),
    }
    output_path = (
        Path(args.output)
        if args.output
        else ROOT / "data" / "cache" / f"edfa_gain_observability_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    )
    write_json(output_path, {"summary": summary, "rows": rows})
    print(f"audited_runs: {summary['total']}")
    print(f"gain_drop_observed: {summary['gain_drop_observed']}")
    print(f"gain_drop_observable_rate: {summary['gain_drop_observable_rate']:.3f}")
    print(f"missing_target_entity: {summary['missing_target_entity']}")
    print(f"output_path: {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
