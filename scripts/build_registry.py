from __future__ import annotations

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.dataset_registry import build_registry, summarize_registry  # noqa: E402
from core.io_utils import write_json  # noqa: E402


def main() -> int:
    runs_root = ROOT / "data" / "runs"
    registry_path = ROOT / "data" / "dataset_registry.json"
    registry = build_registry(runs_root, registry_path)
    write_json(ROOT / "data" / "cache" / "dataset_registry_summary.json", summarize_registry(registry))
    print(f"已生成注册表: {registry_path}")
    print(f"run 数量: {len(registry)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
