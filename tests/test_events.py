from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.event_service import build_event_summary_for_diagnosis, filter_events, load_simulation_events  # noqa: E402


def test_ground_truth_fault_event_can_be_found() -> None:
    run = ROOT / "data" / "runs" / "dataset_aiops_event_driven_v10_20260621_172317"
    events, errors = load_simulation_events(run)
    assert not errors
    matched = filter_events(
        events,
        event_type="EDFA_NOISE_SURGE",
        entity_id="Edfa_booster_roadm Seattle_to_fiber (Seattle → Palo Alto)-_(1/12)",
        start_tick=60.0,
        end_tick=60.0,
    )
    assert len(matched) == 1


def test_fault_injection_events_are_filtered_from_diagnosis_summary() -> None:
    simulation_events = [
        {
            "simulation_tick": 10.0,
            "layer": "L0",
            "event_type": "EDFA_NOISE_SURGE",
            "entity_id": "Edfa_1",
            "details": {"added_nf_db": 12.0},
        },
        {
            "simulation_tick": 11.0,
            "layer": "SERVICE_L2",
            "event_type": "PROVISIONED",
            "entity_id": "SRV-1",
            "details": {"priority": "GOLD"},
        },
    ]
    service_lifecycle = [
        {
            "simulation_tick": 12.0,
            "event_type": "RELEASED",
            "service_id": "SRV-2",
            "priority": "SILVER",
            "cause": None,
        }
    ]
    summary = build_event_summary_for_diagnosis(simulation_events, service_lifecycle, start_tick=0.0, end_tick=20.0)
    assert summary["filtered_injection_event_count"] == 1
    assert summary["diagnosis_visible_simulation_event_count"] == 1
    assert all(item.get("event_type") != "EDFA_NOISE_SURGE" for item in summary["events"])


def test_fault_answer_in_service_cause_is_not_visible_to_diagnosis() -> None:
    summary = build_event_summary_for_diagnosis(
        [],
        [
            {
                "simulation_tick": 12.0,
                "event_type": "DEGRADED",
                "service_id": "SRV-2",
                "priority": "GOLD",
                "cause": "EDFA_NOISE_SURGE",
            }
        ],
        start_tick=0.0,
        end_tick=20.0,
    )

    assert summary["events"][0]["cause"] is None
