from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.evaluation_service import evaluate_diagnosis, summarize_evaluation  # noqa: E402


def test_evaluation_summary_contains_confusion_and_misses() -> None:
    run = {
        "run_id": "run_1",
        "scenario": "FIBER_PMD_SURGE",
        "fault_entity": "fiber (A -> B)-_(1/1)",
    }
    diagnosis = {
        "status": "ABNORMAL",
        "selected_device_type": "fiber",
        "top_causes": [
            {"fault_type": "FIBER_ATTENUATION_SURGE", "entity_id": "fiber (A -> B)-_(1/1)"},
            {"fault_type": "FIBER_PMD_SURGE", "entity_id": "fiber (A -> B)-_(1/1)"},
        ],
    }

    row = evaluate_diagnosis(run, diagnosis)
    summary = summarize_evaluation([row])

    assert row["predicted_top3_types"] == ["FIBER_ATTENUATION_SURGE", "FIBER_PMD_SURGE"]
    assert summary["confusion"]["FIBER_PMD_SURGE"]["FIBER_ATTENUATION_SURGE"] == 1
    assert summary["misses"][0]["run_id"] == "run_1"


def test_evaluation_separates_unobservable_faults() -> None:
    observable_run = {
        "run_id": "run_observable",
        "scenario": "FIBER_PMD_SURGE",
        "fault_entity": "fiber (A -> B)-_(1/1)",
    }
    observable_diagnosis = {
        "status": "ABNORMAL",
        "selected_device_type": "fiber",
        "top_causes": [
            {"fault_type": "FIBER_PMD_SURGE", "entity_id": "fiber (A -> B)-_(1/1)"},
        ],
        "observability": {"direct_anomaly_observed": True},
        "injected_fault_observability": {"applicable": True, "observed": True},
    }
    unobservable_run = {
        "run_id": "run_unobservable",
        "scenario": "EDFA_GAIN_DEGRADATION",
        "fault_entity": "Edfa_booster_roadm A_to_fiber (A -> B)-_(1/1)",
    }
    unobservable_diagnosis = {
        "status": "NORMAL",
        "selected_device_type": "edfa",
        "top_causes": [],
        "observability": {"direct_anomaly_observed": False},
        "injected_fault_observability": {"applicable": True, "observed": False},
    }

    rows = [
        evaluate_diagnosis(observable_run, observable_diagnosis),
        evaluate_diagnosis(unobservable_run, unobservable_diagnosis),
    ]
    summary = summarize_evaluation(rows)

    assert summary["abnormal_total"] == 2
    assert summary["observable_abnormal"] == 1
    assert summary["observable_rate"] == 0.5
    assert summary["observable_type_hit_1"] == 1.0
    assert summary["signature_observable_abnormal"] == 1
    assert summary["signature_observable_rate"] == 0.5
    assert summary["signature_observable_type_hit_1"] == 1.0
    assert summary["by_type"]["EDFA_GAIN_DEGRADATION"]["observable_count"] == 0
    assert summary["by_type"]["EDFA_GAIN_DEGRADATION"]["observable_rate"] == 0.0
    assert summary["by_type"]["EDFA_GAIN_DEGRADATION"]["signature_observable_count"] == 0
