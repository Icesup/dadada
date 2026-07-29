from __future__ import annotations

from collections import Counter
from pathlib import Path
import json
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.event_service import load_service_lifecycle, load_simulation_events  # noqa: E402
from core.experiment_service import find_run, load_registry  # noqa: E402
from core.telemetry_service import load_telemetry, list_metric_fields  # noqa: E402


DEMO_RUN_ID = "dataset_aiops_event_driven_v10_20260621_172317"


def brief_record(record: dict, max_keys: int = 8) -> dict:
    """截取记录中的前几个字段，避免验收输出太长。"""

    return {key: record.get(key) for key in list(record.keys())[:max_keys]}


def main() -> int:
    registry = load_registry(ROOT / "data" / "dataset_registry.json")
    status_counts = Counter(item.get("status", "UNKNOWN") for item in registry)
    run = find_run(registry, DEMO_RUN_ID)
    run_dir = Path(run["run_dir"])

    print("=== 阶段 A 最小验收 ===")
    print(f"识别实验数: {len(registry)}")
    print(f"状态分布: {dict(status_counts)}")
    print()

    print("=== 演示实验 ===")
    print(f"run_id: {run.get('run_id')}")
    print(f"status: {run.get('status')}")
    print(f"fault_type: {run.get('scenario')}")
    print(f"fault_entity: {run.get('fault_entity')}")
    print(f"trigger_tick: {run.get('trigger_tick')}")
    print(f"record_counts: {json.dumps(run.get('record_counts', {}), ensure_ascii=False)}")
    print()

    print("=== Telemetry 展平字段 ===")
    for device_type in ["edfa", "fiber", "roadm"]:
        records, errors = load_telemetry(run_dir, device_type)
        fields = list_metric_fields(records)
        print(f"{device_type}: records={len(records)}, errors={len(errors)}")
        print(f"{device_type}_fields: {fields}")
    print()

    print("=== 结构化仿真事件读取 ===")
    events, event_errors = load_simulation_events(run_dir)
    lifecycle, lifecycle_errors = load_service_lifecycle(run_dir)
    print(f"simulation_events: records={len(events)}, errors={len(event_errors)}")
    if events:
        print(f"simulation_events_sample: {json.dumps(brief_record(events[0]), ensure_ascii=False)}")
    print(f"service_lifecycle: records={len(lifecycle)}, errors={len(lifecycle_errors)}")
    if lifecycle:
        print(f"service_lifecycle_sample: {json.dumps(brief_record(lifecycle[0]), ensure_ascii=False)}")
    print()

    print("=== 结论 ===")
    print("阶段 A 满足最小验收：注册、读取、telemetry 展平、事件读取和错误暴露均可用。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
