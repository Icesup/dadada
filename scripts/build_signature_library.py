from __future__ import annotations

import argparse
from collections import defaultdict
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.experiment_service import load_registry  # noqa: E402
from core.signature_service import build_signature_library, save_signature_library  # noqa: E402


def pick_train_runs(registry: list[dict], train_per_type: int) -> list[dict]:
    grouped: dict[str, list[dict]] = defaultdict(list)
    for item in registry:
        if item.get("status") == "VALID":
            grouped[str(item.get("scenario") or "UNKNOWN")].append(item)
    picked: list[dict] = []
    for scenario in sorted(grouped):
        picked.extend(grouped[scenario][:train_per_type])
    return picked


def main() -> None:
    parser = argparse.ArgumentParser(description="根据历史 episode 构建故障特征库。")
    parser.add_argument("--train-per-type", type=int, default=10, help="每类故障用于建库的 episode 数量。")
    parser.add_argument("--pre-window", type=float, default=30.0)
    parser.add_argument("--post-window", type=float, default=30.0)
    parser.add_argument(
        "--output",
        default=str(ROOT / "data" / "knowledge" / "fault_signature_library.json"),
    )
    args = parser.parse_args()

    registry = load_registry(ROOT / "data" / "dataset_registry.json")
    runs = pick_train_runs(registry, args.train_per_type)
    library = build_signature_library(
        runs,
        pre_window=args.pre_window,
        post_window=args.post_window,
    )
    output_path = Path(args.output)
    save_signature_library(output_path, library)

    print(f"signature_types: {len(library['signatures'])}")
    for scenario, item in library["signatures"].items():
        print(f"  {scenario}: {item['count']}")
    if library["errors"]:
        print("errors:")
        for item in library["errors"][:10]:
            print(f"  {item['run_id']}: {item['error']}")
    print(f"output_path: {output_path}")


if __name__ == "__main__":
    main()
