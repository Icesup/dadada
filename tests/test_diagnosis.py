from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.diagnosis_service import build_rule_diagnosis_from_context  # noqa: E402
from core.analysis_pipeline import allow_signature_override, is_strong_source_change, select_source_candidate_entity  # noqa: E402


def test_local_rules_diagnosis_outputs_edfa_noise_top1() -> None:
    diagnosis = build_rule_diagnosis_from_context(
        device_type="edfa",
        entity_id="Edfa_1",
        performance_summary={
            "status": "ABNORMAL",
            "key_metric_changes": [
                {"metric": "output_gsnr_db", "direction": "decrease", "delta": -9.7, "significant": True},
                {"metric": "output_osnr_db", "direction": "decrease", "delta": -11.5, "significant": True},
                {"metric": "actual_gain_db", "direction": "stable", "delta": 0.0, "significant": False},
            ],
        },
        event_summary={"events": [{"event_type": "PROVISIONED"}]},
        knowledge_results=[{"chunk_id": "SIM_FAULT_EDFA_NOISE_001", "topic": "edfa_noise_surge_signature"}],
    )
    assert diagnosis["status"] == "ABNORMAL"
    assert diagnosis["top_causes"][0]["fault_type"] == "EDFA_NOISE_SURGE"


def test_local_rules_diagnosis_allows_normal_without_causes() -> None:
    diagnosis = build_rule_diagnosis_from_context(
        device_type="edfa",
        entity_id="Edfa_1",
        performance_summary={"status": "NORMAL", "key_metric_changes": []},
        event_summary={"events": []},
        knowledge_results=[{"chunk_id": "SIM_FAULT_NORMAL_001"}],
    )
    assert diagnosis["status"] == "NORMAL"
    assert diagnosis["top_causes"] == []


def test_source_candidate_selects_first_affected_span() -> None:
    records = []
    for span, post_value in ((1, 1.0), (2, 20.0), (3, 25.0)):
        entity_id = (
            "Edfa_preamp_roadm B_from_fiber (A -> B)-_(3/3)"
            if span == 3
            else f"Edfa_fiber (A -> B)-_({span}/3)"
        )
        records.extend(
            [
                {"entity_id": entity_id, "simulation_tick": 0.0, "pmd_ps": 1.0},
                {"entity_id": entity_id, "simulation_tick": 5.0, "pmd_ps": 1.0},
                {"entity_id": entity_id, "simulation_tick": 10.0, "pmd_ps": post_value},
                {"entity_id": entity_id, "simulation_tick": 15.0, "pmd_ps": post_value},
            ]
        )

    candidate = select_source_candidate_entity(
        records,
        source_metrics=["pmd_ps"],
        summary_metrics=["pmd_ps"],
        trigger_tick=10.0,
        pre_window=10.0,
        post_window=10.0,
    )

    assert candidate["entity_id"] == "Edfa_fiber (A -> B)-_(2/3)"


def test_roadm_similarity_cannot_override_direct_edfa_gain_drop() -> None:
    allowed = allow_signature_override(
        global_top_type="EDFA_GAIN_DEGRADATION",
        global_top_source_strength=10.0,
        signature_top_type="ROADM_WSS_FILTER_SHIFT",
        signature_top_margin=0.12,
        rule_candidate_types={"EDFA_GAIN_DEGRADATION", "ROADM_WSS_FILTER_SHIFT"},
        roadm_signature_margin=0.10,
    )

    assert allowed is False


def test_small_pmd_drift_is_not_a_root_cause_anchor() -> None:
    changes = {
        "pmd_ps": {
            "metric": "pmd_ps",
            "direction": "increase",
            "delta": 1.05,
            "pre_count": 8,
            "post_count": 8,
            "_post_values": [2.05] * 8,
            "pre_mean": 1.0,
        }
    }

    assert is_strong_source_change(changes, "pmd_ps") is False


def test_consistent_roadm_similarity_can_override_propagated_edfa_quality_drop() -> None:
    allowed = allow_signature_override(
        global_top_type="EDFA_NOISE_SURGE",
        global_top_source_strength=13.5,
        signature_top_type="ROADM_INBAND_CROSSTALK",
        signature_top_margin=0.0085,
        rule_candidate_types={"EDFA_NOISE_SURGE", "ROADM_WSS_FILTER_SHIFT"},
        roadm_signature_margin=0.0085,
    )

    assert allowed is True
