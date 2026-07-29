from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import core.change_point_service as change_point_service  # noqa: E402


def _records(metric: str, values: list[float], entity_id: str = "fiber-1") -> list[dict[str, object]]:
    return [
        {"simulation_tick": float(index), "entity_id": entity_id, metric: value}
        for index, value in enumerate(values)
    ]


def test_detects_persistent_direct_metric_change(monkeypatch) -> None:
    telemetry = {
        "fiber": _records("pmd_ps", [1.0] * 8 + [21.0] * 12),
        "edfa": [],
        "roadm": [],
    }
    monkeypatch.setattr(change_point_service, "get_run_dir", lambda run: Path("unused"))
    monkeypatch.setattr(
        change_point_service,
        "load_telemetry",
        lambda run_dir, device_type: (telemetry[device_type], []),
    )

    result = change_point_service.detect_run_change_point({"run_id": "persistent"})

    assert result["status"] == "DETECTED"
    assert result["analysis_tick"] == 8.0
    assert result["metric"] == "pmd_ps"
    assert result["entity_id"] == "fiber-1"


def test_rejects_transient_spike(monkeypatch) -> None:
    telemetry = {
        "fiber": _records("pmd_ps", [1.0] * 8 + [30.0] + [1.0] * 11),
        "edfa": [],
        "roadm": [],
    }
    monkeypatch.setattr(change_point_service, "get_run_dir", lambda run: Path("unused"))
    monkeypatch.setattr(
        change_point_service,
        "load_telemetry",
        lambda run_dir, device_type: (telemetry[device_type], []),
    )

    result = change_point_service.detect_run_change_point({"run_id": "spike"})

    assert result["status"] == "STABLE"
    assert result["detected_tick"] is None


def test_quality_metric_change_is_evidence_not_alarm_trigger(monkeypatch) -> None:
    telemetry = {
        "fiber": _records("current_osnr_db", [25.0] * 8 + [15.0] * 12),
        "edfa": [],
        "roadm": [],
    }
    monkeypatch.setattr(change_point_service, "get_run_dir", lambda run: Path("unused"))
    monkeypatch.setattr(
        change_point_service,
        "load_telemetry",
        lambda run_dir, device_type: (telemetry[device_type], []),
    )

    result = change_point_service.detect_run_change_point({"run_id": "quality-only"})

    assert result["status"] == "STABLE"
    assert result["candidate_count"] == 0


def test_cross_domain_quality_degradation_can_trigger(monkeypatch) -> None:
    telemetry = {
        "fiber": _records("current_osnr_db", [25.0] * 8 + [15.0] * 12, "fiber-1"),
        "edfa": _records("output_osnr_db", [26.0] * 8 + [16.0] * 12, "edfa-1"),
        "roadm": [],
    }
    monkeypatch.setattr(change_point_service, "get_run_dir", lambda run: Path("unused"))
    monkeypatch.setattr(
        change_point_service,
        "load_telemetry",
        lambda run_dir, device_type: (telemetry[device_type], []),
    )

    result = change_point_service.detect_run_change_point({"run_id": "quality-consensus"})

    assert result["status"] == "DETECTED"
    assert result["analysis_tick"] == 8.0
    assert result["metric"] in {"current_osnr_db", "output_osnr_db"}


def test_single_entity_power_drop_is_rejected(monkeypatch) -> None:
    telemetry = {
        "fiber": [],
        "edfa": [],
        "roadm": _records("output_power_dbm", [-2.0] * 8 + [-20.0] * 12, "roadm-1"),
    }
    monkeypatch.setattr(change_point_service, "get_run_dir", lambda run: Path("unused"))
    monkeypatch.setattr(
        change_point_service,
        "load_telemetry",
        lambda run_dir, device_type: (telemetry[device_type], []),
    )

    result = change_point_service.detect_run_change_point({"run_id": "single-power"})

    assert result["status"] == "STABLE"
