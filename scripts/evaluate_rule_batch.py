from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.evaluation_service import evaluate_runs_with_local_rules  # noqa: E402
from core.experiment_service import load_registry  # noqa: E402
from core.io_utils import write_json  # noqa: E402


def pick_runs(
    registry: list[dict],
    max_per_type: int,
    offset_per_type: int = 0,
    batch_ids: set[str] | None = None,
) -> list[dict]:
    """按故障类型分层抽样，避免评测结果偏向单一类别。"""

    grouped: dict[str, list[dict]] = defaultdict(list)
    for item in registry:
        batch_matches = not batch_ids or str(item.get("batch_id") or "") in batch_ids
        if item.get("status") == "VALID" and batch_matches:
            grouped[str(item.get("scenario") or "UNKNOWN")].append(item)
    picked: list[dict] = []
    for scenario in sorted(grouped):
        candidates = grouped[scenario][offset_per_type:]
        picked.extend(candidates[:max_per_type] if max_per_type > 0 else candidates)
    return picked


def main() -> None:
    parser = argparse.ArgumentParser(description="批量评估当前本地规则诊断闭环。")
    parser.add_argument("--max-per-type", type=int, default=3, help="每类故障抽样数量，默认 3。")
    parser.add_argument("--offset-per-type", type=int, default=0, help="每类故障跳过的样本数，用于数据切分。")
    parser.add_argument(
        "--signature-library",
        default=str(ROOT / "data" / "knowledge" / "fault_signature_library.json"),
        help="故障特征库路径；文件不存在时退回规则诊断。",
    )
    parser.add_argument("--pre-window", type=float, default=30.0)
    parser.add_argument("--post-window", type=float, default=30.0)
    parser.add_argument("--output", default="")
    parser.add_argument(
        "--batch-id",
        action="append",
        default=[],
        help="只评估指定批次；可重复传入。默认评估全部已注册批次。",
    )
    args = parser.parse_args()

    registry = load_registry(ROOT / "data" / "dataset_registry.json")
    runs = pick_runs(registry, args.max_per_type, args.offset_per_type, set(args.batch_id))
    signature_path = Path(args.signature_library)
    result = evaluate_runs_with_local_rules(
        runs,
        knowledge_chunks_path=ROOT / "data" / "knowledge" / "knowledge_chunks.jsonl",
        signature_library_path=signature_path if signature_path.exists() else None,
        pre_window=args.pre_window,
        post_window=args.post_window,
    )
    output_path = (
        Path(args.output)
        if args.output
        else ROOT
        / "data"
        / "cache"
        / f"batch_eval_offset{args.offset_per_type}_n{args.max_per_type}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    )
    write_json(output_path, result)

    summary = result["summary"]
    print(f"evaluated_runs: {summary['total']}")
    print(f"type_hit_1: {summary['type_hit_1']:.3f}")
    print(f"type_hit_3: {summary['type_hit_3']:.3f}")
    print(f"entity_hit_1: {summary['entity_hit_1']:.3f}")
    print(f"device_hit: {summary['device_hit']:.3f}")
    print(
        f"observable_abnormal: {summary['observable_abnormal']}/{summary['abnormal_total']} "
        f"({summary['observable_rate']:.3f})"
    )
    print(f"observable_type_hit_1: {summary['observable_type_hit_1']:.3f}")
    print(f"observable_type_hit_3: {summary['observable_type_hit_3']:.3f}")
    print(
        f"signature_observable_abnormal: {summary['signature_observable_abnormal']}/{summary['abnormal_total']} "
        f"({summary['signature_observable_rate']:.3f})"
    )
    print(f"signature_observable_type_hit_1: {summary['signature_observable_type_hit_1']:.3f}")
    print(f"signature_observable_type_hit_3: {summary['signature_observable_type_hit_3']:.3f}")
    print("by_type:")
    for truth_type, item in summary["by_type"].items():
        print(
            f"  {truth_type}: n={item['count']} "
            f"type@1={item['type_hit_1']:.3f} type@3={item['type_hit_3']:.3f} "
            f"entity@1={item['entity_hit_1']:.3f} device={item['device_hit']:.3f} "
            f"observable={item['observable_count']}/{item['count']} "
            f"observable@1={item['observable_type_hit_1']:.3f} "
            f"signature={item['signature_observable_count']}/{item['count']} "
            f"signature@1={item['signature_observable_type_hit_1']:.3f} "
            f"pred={item['predicted_top1_counts']}"
        )
    print("confusion:")
    for truth_type, predictions in summary["confusion"].items():
        print(f"  {truth_type}: {predictions}")
    misses = summary.get("misses", [])
    if misses:
        print("misses_top10:")
        for item in misses[:10]:
            print(
                f"  {item['run_id']}: truth={item['truth_type']} "
                f"pred={item['predicted_top1_type']} top3={item['predicted_top3_types']} "
                f"entity_hit={item['entity_hit_1']} device_hit={item['device_hit']} "
                f"observable={item['direct_anomaly_observed']} "
                f"signature={item['injected_signature_observed']}"
            )
    if result["errors"]:
        print("errors:")
        for item in result["errors"][:10]:
            print(f"  {item['run_id']}: {item['error']}")
    print(f"output_path: {output_path}")


if __name__ == "__main__":
    main()
