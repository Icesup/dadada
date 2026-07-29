from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.io_utils import write_json  # noqa: E402
from core.overview_service import build_operations_overview  # noqa: E402


def test_overview_uses_latest_rerun_reports(tmp_path: Path) -> None:
    registry = [
        {"status": "VALID", "scenario": "EDFA_GAIN_DEGRADATION", "batch_id": "batch-new"},
        {"status": "PARTIAL", "scenario": "ROADM_WSS_FILTER_SHIFT", "batch_id": "batch-new"},
    ]
    write_json(
        tmp_path / "batch_eval_rerun_20260724.json",
        {"summary": {"type_hit_1": 0.9, "misses": [{"run_id": "r1"}]}},
    )
    write_json(
        tmp_path / "edfa_gain_observability_rerun_20260724.json",
        {"summary": {"total": 2, "gain_drop_observed": 2}},
    )

    result = build_operations_overview(registry, tmp_path)

    assert result["total_runs"] == 2
    assert result["valid_runs"] == 1
    assert result["batch_count"] == 1
    assert result["evaluation_summary"]["type_hit_1"] == 0.9
    assert result["gain_summary"]["gain_drop_observed"] == 2
    assert any(item["级别"] == "数据" for item in result["risks"])
