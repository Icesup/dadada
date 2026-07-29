from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.anomaly_service import build_performance_event_summary, is_significant_change  # noqa: E402


def test_performance_summary_marks_significant_gsnr_drop() -> None:
    metric_summary = {
        "entity_id": "Edfa_1",
        "metric": "output_gsnr_db",
        "trigger_tick": 30.0,
        "pre_count": 5,
        "post_count": 5,
        "pre_mean": 20.0,
        "post_mean": 12.0,
        "delta": -8.0,
        "relative_delta": -0.4,
        "direction": "decrease",
    }
    assert is_significant_change(metric_summary)
    summary = build_performance_event_summary(
        run_id="run_1",
        device_type="edfa",
        entity_id="Edfa_1",
        trigger_tick=30.0,
        metric_summaries=[metric_summary],
    )
    assert summary["status"] == "ABNORMAL"
    assert summary["key_metric_changes"][0]["significant"] is True


def test_performance_summary_keeps_small_change_normal() -> None:
    metric_summary = {
        "entity_id": "Edfa_1",
        "metric": "output_gsnr_db",
        "trigger_tick": 30.0,
        "pre_count": 5,
        "post_count": 5,
        "pre_mean": 20.0,
        "post_mean": 19.8,
        "delta": -0.2,
        "relative_delta": -0.01,
        "direction": "decrease",
    }
    summary = build_performance_event_summary(
        run_id="run_1",
        device_type="edfa",
        entity_id="Edfa_1",
        trigger_tick=30.0,
        metric_summaries=[metric_summary],
    )
    assert summary["status"] == "NORMAL"
    assert summary["key_metric_changes"][0]["significant"] is False


def test_performance_summary_does_not_mark_quality_improvement_abnormal() -> None:
    metric_summary = {
        "entity_id": "Edfa_1",
        "metric": "output_gsnr_db",
        "trigger_tick": 30.0,
        "pre_count": 5,
        "post_count": 5,
        "pre_mean": 20.0,
        "post_mean": 24.0,
        "delta": 4.0,
        "relative_delta": 0.2,
        "direction": "increase",
    }
    assert is_significant_change(metric_summary)
    summary = build_performance_event_summary(
        run_id="run_1",
        device_type="edfa",
        entity_id="Edfa_1",
        trigger_tick=30.0,
        metric_summaries=[metric_summary],
    )
    assert summary["status"] == "NORMAL"
    assert summary["key_metric_changes"][0]["harmful"] is False


def test_performance_summary_ignores_short_transient_drop() -> None:
    metric_summary = {
        "entity_id": "Edfa_1",
        "metric": "output_gsnr_db",
        "trigger_tick": 30.0,
        "pre_count": 5,
        "post_count": 10,
        "pre_mean": 25.0,
        "post_mean": 22.0,
        "delta": -3.0,
        "relative_delta": -0.12,
        "direction": "decrease",
        "_post_values": [10.0, 10.0, 25.0, 25.0, 25.0, 25.0, 25.0, 25.0, 25.0, 25.0],
    }
    summary = build_performance_event_summary(
        run_id="run_1",
        device_type="edfa",
        entity_id="Edfa_1",
        trigger_tick=30.0,
        metric_summaries=[metric_summary],
    )
    assert summary["status"] == "NORMAL"
    assert summary["key_metric_changes"][0]["post_persistence_ratio"] == 0.2
