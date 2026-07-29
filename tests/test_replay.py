from __future__ import annotations

import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.experiment_service import find_run, load_registry  # noqa: E402
from core.replay_service import (  # noqa: E402
    active_services_at_tick,
    attach_diagnosis,
    build_replay_incident,
    build_structured_actions,
    build_validation_task,
    load_replay_bundle,
    scan_telemetry,
    should_create_incident,
)
from app import build_selected_metric_series  # noqa: E402


FAULT_RUN_ID = "experiment_batch_20260623_194654__episode_0119"
NORMAL_RUN_ID = "experiment_batch_20260624_085020__episode_0001"


def _replay_until_incident(run_id: str) -> tuple[dict | None, dict]:
    run = find_run(load_registry(), run_id)
    bundle = load_replay_bundle(run)
    previous_paths: list[str] = []
    for tick in bundle["timeline"]:
        snapshot = scan_telemetry(bundle, current_tick=tick)
        route_key = should_create_incident(snapshot["candidate_paths"], previous_paths)
        if route_key:
            active = active_services_at_tick(bundle["service_lifecycle"], tick)
            return (
                build_replay_incident(
                    run_id=run_id,
                    current_tick=tick,
                    route_key=route_key,
                    observations=snapshot["observations"],
                    active_services=active,
                ),
                bundle,
            )
        previous_paths = snapshot["candidate_paths"]
    return None, bundle


def test_fault_replay_generates_one_correlated_incident_without_answer_fields() -> None:
    incident, _ = _replay_until_incident(FAULT_RUN_ID)

    assert incident is not None
    assert incident["first_abnormal_tick"] == 80.0
    assert incident["detected_tick"] == 85.0
    assert incident["severity"] == "CRITICAL"
    assert incident["affected_path"] == ["Houston", "CollegePark"]
    assert len(incident["affected_services"]) == 2
    assert len(incident["abnormal_entities"]) > 1
    serialized = json.dumps(incident, ensure_ascii=False)
    assert "EDFA_NOISE_SURGE" not in serialized
    assert "ground_truth" not in serialized
    assert "added_nf_db" not in serialized


def test_normal_replay_stays_in_monitoring_without_incident() -> None:
    incident, bundle = _replay_until_incident(NORMAL_RUN_ID)

    assert not bundle["errors"]
    assert incident is None


def test_diagnosis_can_be_attached_as_structured_suggestion() -> None:
    incident = {
        "incident_id": "INC-0001",
        "run_id": "run-1",
        "status": "UNDER_ANALYSIS",
    }
    diagnosis = {
        "status": "ABNORMAL",
        "top_causes": [
            {
                "rank": 1,
                "fault_type": "EDFA_NOISE_SURGE",
                "entity_id": "Edfa-1",
            }
        ],
    }

    actions = build_structured_actions(incident, diagnosis)
    updated = attach_diagnosis(incident, diagnosis)
    task = build_validation_task(updated, actions[0])

    assert actions[0]["parameters"] == {"nf_target": "baseline"}
    assert actions[0]["status"] == "SUGGESTED"
    assert updated["status"] == "RECOMMENDATION_READY"
    assert task["status"] == "WAITING_SIMULATION_ENGINE"
    assert task["result"] is None


def test_selected_metric_series_uses_model_selected_entity_and_no_future_data() -> None:
    bundle = {
        "telemetry": {
            "edfa": [
                {"simulation_tick": 1.0, "entity_id": "Edfa Houston", "output_osnr_db": 18.0},
                {"simulation_tick": 4.5, "entity_id": "Edfa Houston", "output_osnr_db": 15.0},
                {"simulation_tick": 4.5, "entity_id": "Edfa Other", "output_osnr_db": 2.0},
                {"simulation_tick": 8.0, "entity_id": "Edfa Houston", "output_osnr_db": 3.0},
            ],
        }
    }
    features = [
        {
            "entity_id": "Edfa Houston",
            "device_type": "edfa",
            "metric": "output_osnr_db",
            "selection_source": "online_model",
        }
    ]

    series = build_selected_metric_series(bundle, features, current_tick=5.0)

    assert series[0]["entity_id"] == "Edfa Houston"
    assert series[0]["unit"] == "dB"
    assert [point["指标值"] for point in series[0]["points"]] == [18.0, 15.0]
