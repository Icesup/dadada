from pathlib import Path
import json
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.incident_service import build_incident_snapshot, update_incident_with_diagnosis  # noqa: E402


def test_incident_is_created_from_observations_without_answer_fields() -> None:
    incident = build_incident_snapshot(
        run_id="run_001",
        reference_tick=37.5,
        observations=[
            {
                "device_type": "fiber",
                "candidate_entity_id": "fiber candidate",
                "candidate_score": 9.2,
                "key_changes": [{"metric": "pmd_ps", "delta": 80.0, "direction": "increase"}],
            }
        ],
    )

    assert incident["incident_id"].startswith("INC-")
    assert incident["status"] == "待诊断"
    assert incident["severity"] == "严重"
    serialized = json.dumps(incident, ensure_ascii=False)
    assert "fault_type" not in serialized
    assert "fault_entity" not in serialized
    assert "ground_truth" not in serialized


def test_normal_observations_do_not_create_false_alarm() -> None:
    incident = build_incident_snapshot(
        run_id="run_normal",
        observations=[
            {
                "device_type": "edfa",
                "candidate_entity_id": "edfa candidate",
                "candidate_score": 0.0,
                "key_changes": [],
            }
        ],
    )

    assert incident["alarm_count"] == 0
    assert incident["incident_id"] == ""
    assert incident["status"] == "持续监测"


def test_incident_moves_to_pending_action_after_abnormal_diagnosis() -> None:
    incident = build_incident_snapshot(
        run_id="run_002",
        observations=[
            {
                "device_type": "edfa",
                "candidate_entity_id": "edfa candidate",
                "candidate_score": 5.0,
                "key_changes": [{"metric": "output_osnr_db", "delta": -5.0, "direction": "decrease"}],
            }
        ],
    )
    updated = update_incident_with_diagnosis(
        incident,
        {"status": "ABNORMAL", "top_causes": [{"rank": 1}, {"rank": 2}, {"rank": 3}]},
    )

    assert updated["status"] == "待处置"
    assert updated["candidate_count"] == 3
