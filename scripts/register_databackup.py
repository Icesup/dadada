from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.config import get_external_data_roots  # noqa: E402
from core.dataset_registry import build_databackup_manifest, summarize_registry  # noqa: E402
from core.io_utils import write_json  # noqa: E402


CACHE_PATH = ROOT / "data" / "cache" / "databackup_file_stats.json"
REGISTRY_PATH = ROOT / "data" / "dataset_registry.json"
SUMMARY_PATH = ROOT / "data" / "cache" / "dataset_registry_summary.json"


def load_cache() -> dict[str, object]:
    """读取文件统计缓存。"""

    if not CACHE_PATH.exists():
        return {}
    try:
        data = json.loads(CACHE_PATH.read_text(encoding="utf-8-sig"))
    except Exception:  # noqa: BLE001
        return {}
    return data if isinstance(data, dict) else {}


def find_episode_dirs(data_root: Path) -> list[Path]:
    """扫描 batch/episode 目录。"""

    return sorted(
        path
        for path in data_root.glob("experiment_batch_*/*")
        if path.is_dir() and path.name.startswith("episode_")
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="注册一个或多个外部仿真实验数据根目录。")
    parser.add_argument(
        "--data-root",
        action="append",
        type=Path,
        default=[],
        help="包含 experiment_batch_* 的目录；可重复传入。",
    )
    parser.add_argument(
        "--merge-existing",
        action="store_true",
        help="保留现有注册表中的其他批次，并用本次扫描结果覆盖同名 run。",
    )
    return parser.parse_args()


def load_existing_registry() -> list[dict[str, object]]:
    if not REGISTRY_PATH.exists():
        return []
    try:
        data = json.loads(REGISTRY_PATH.read_text(encoding="utf-8-sig"))
    except Exception:  # noqa: BLE001
        return []
    return [item for item in data if isinstance(item, dict)] if isinstance(data, list) else []


def main() -> int:
    args = parse_args()
    start = time.perf_counter()
    data_roots = [path.expanduser().resolve() for path in args.data_root]
    if not data_roots:
        data_roots = get_external_data_roots()
    if not data_roots:
        print("未配置外部数据根目录，请设置 DATA_ROOT 或 configs/platform.yaml:data.external_data_root")
        return 2
    missing_roots = [path for path in data_roots if not path.exists()]
    if missing_roots:
        for path in missing_roots:
            print(f"外部数据根目录不存在: {path}")
        return 2

    cache = load_cache()
    scanned: list[dict[str, object]] = []
    for data_root in data_roots:
        episode_dirs = find_episode_dirs(data_root)
        scanned.extend(build_databackup_manifest(path, data_root, cache).to_dict() for path in episode_dirs)
    registry_by_id: dict[str, dict[str, object]] = {}
    if args.merge_existing:
        registry_by_id.update(
            {
                str(item.get("run_id")): item
                for item in load_existing_registry()
                if item.get("run_id")
            }
        )
    registry_by_id.update({str(item.get("run_id")): item for item in scanned if item.get("run_id")})
    registry = sorted(registry_by_id.values(), key=lambda item: str(item.get("run_id") or ""))
    write_json(CACHE_PATH, cache)
    write_json(REGISTRY_PATH, registry)
    write_json(SUMMARY_PATH, summarize_registry(registry))

    statuses = Counter(item.get("status") for item in registry)
    scenarios = Counter(item.get("scenario") or "UNKNOWN" for item in registry)
    batches = Counter(item.get("batch_id") for item in registry)
    preferred = next((item for item in registry if item.get("run_id") == "experiment_batch_20260623_094644__episode_0108"), None)
    normal = next(
        (item for item in registry if item.get("run_id") == "experiment_batch_20260624_085020__episode_0007"),
        None,
    )
    if normal is None:
        normal = next((item for item in registry if item.get("scenario") == "NORMAL_STATE" and item.get("status") == "VALID"), None)

    elapsed = time.perf_counter() - start
    print("data_roots:")
    for data_root in data_roots:
        print(f"  {data_root}")
    print(f"scanned_episodes: {len(scanned)}")
    print(f"registered_episodes: {len(registry)}")
    print(f"batch_count: {len(batches)}")
    for batch_id, count in sorted(batches.items()):
        print(f"  {batch_id}: {count}")
    print("status_counts:")
    for status, count in sorted(statuses.items()):
        print(f"  {status}: {count}")
    print("scenario_counts:")
    for scenario, count in scenarios.most_common():
        print(f"  {scenario}: {count}")
    if preferred:
        print("preferred_fault_demo:")
        print(f"  run_id: {preferred.get('run_id')}")
        print(f"  status: {preferred.get('status')}")
        print(f"  fault_type: {preferred.get('scenario')}")
        print(f"  fault_entity: {preferred.get('fault_entity')}")
        print(f"  trigger_tick: {preferred.get('trigger_tick')}")
        print(f"  record_counts: {preferred.get('record_counts')}")
    if normal:
        print("normal_demo:")
        print(f"  run_id: {normal.get('run_id')}")
        print(f"  status: {normal.get('status')}")
        print(f"  trigger_tick: {normal.get('trigger_tick')}")
        print(f"  record_counts: {normal.get('record_counts')}")
    print(f"registry_path: {REGISTRY_PATH}")
    print(f"cache_path: {CACHE_PATH}")
    print(f"elapsed_seconds: {elapsed:.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
