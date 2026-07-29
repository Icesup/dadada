from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.telemetry_service import load_telemetry, list_metric_fields, summarize_metric_change  # noqa: E402


def test_telemetry_metrics_are_flattened() -> None:
    run = ROOT / "data" / "runs" / "dataset_aiops_event_driven_v10_20260621_172317"
    records, errors = load_telemetry(run, "edfa")
    assert not errors
    assert records
    assert "actual_gain_db" in records[0]
    assert "metrics" not in records[0]
    assert "output_gsnr_db" in list_metric_fields(records)


def test_metric_change_returns_counts_even_when_post_window_sparse() -> None:
    run = ROOT / "data" / "runs" / "dataset_aiops_event_driven_v10_20260621_172317"
    records, _ = load_telemetry(run, "edfa")
    summary = summarize_metric_change(
        records,
        entity_id="Edfa_booster_roadm Seattle_to_fiber (Seattle → Palo Alto)-_(1/12)",
        metric="output_gsnr_db",
        trigger_tick=60.0,
    )
    assert summary["metric"] == "output_gsnr_db"
    assert "pre_count" in summary
    assert "post_count" in summary


def test_trigger_tick_sample_is_counted_as_post_fault_window() -> None:
    run = ROOT / "data" / "runs" / "dataset_aiops_event_driven_v10_20260621_215831"
    records, errors = load_telemetry(run, "fiber")
    assert not errors
    summary = summarize_metric_change(
        records,
        entity_id="fiber (Seattle → Palo Alto)-_(1/12)",
        metric="pmd_ps",
        trigger_tick=60.0,
        pre_window=30.0,
        post_window=30.0,
    )
    assert summary["pre_count"] == 1
    assert summary["post_count"] == 1
    assert summary["delta"] == 80.0
