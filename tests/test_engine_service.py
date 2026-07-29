from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from adapters.diagnosis_adapter import parse_engine_json  # noqa: E402
from core.engine_service import (  # noqa: E402
    build_diagnosis_payload,
    build_event_metric_candidates,
    calibrate_diagnosis_output,
    select_validated_metric_features,
)
import app as app_module  # noqa: E402


def test_diagnosis_payload_does_not_include_ground_truth_fields() -> None:
    payload = build_diagnosis_payload(
        run_id="run_1",
        device_type="edfa",
        entity_id="Edfa_1",
        performance_event_summary={
            "status": "ABNORMAL",
            "key_metric_changes": [
                {
                    "metric": "output_gsnr_db",
                    "delta": -3.0,
                    "relative_delta": -0.1,
                    "significant": True,
                    "harmful": True,
                    "score": 3.0,
                }
            ],
        },
        event_summary_for_diagnosis={
            "raw_simulation_event_count": 3,
            "filtered_injection_event_count": 1,
            "diagnosis_visible_simulation_event_count": 0,
            "service_lifecycle_event_count": 2,
            "events": [],
        },
        knowledge_query={"query_text": "edfa output_gsnr_db decrease"},
        knowledge_results=[{"chunk_id": "SIM_FAULT_EDFA_NOISE_001", "score": 9.0}],
    )
    assert "scenario" not in payload
    assert "fault_entity" not in payload
    assert "ground_truth" not in payload
    assert "status" not in payload["performance_event_summary"]
    assert "significant" not in payload["performance_event_summary"]["key_metric_changes"][0]
    assert "harmful" not in payload["performance_event_summary"]["key_metric_changes"][0]
    assert "score" not in payload["performance_event_summary"]["key_metric_changes"][0]
    assert "filtered_injection_event_count" not in payload["event_summary_for_diagnosis"]
    assert "raw_simulation_event_count" not in payload["event_summary_for_diagnosis"]
    assert payload["diagnostic_hints"]["observed_status"] == "ABNORMAL"


def test_parse_engine_json_accepts_plain_json() -> None:
    result = parse_engine_json('{"status":"NORMAL","summary":"ok","top_causes":[]}')
    assert result["status"] == "NORMAL"
    assert result["top_causes"] == []
    assert result["recommendations"] == []
    assert result["key_metric_features"] == []


def test_metric_selection_accepts_only_observed_candidates_and_fills_missing() -> None:
    candidates = build_event_metric_candidates(
        [
            {
                "entity_id": "Edfa Houston",
                "device_type": "edfa",
                "metric": "output_osnr_db",
                "pre_mean": 19.5,
                "post_mean": 10.6,
                "delta": -8.9,
                "direction": "decrease",
                "fault_type": "MUST_NOT_LEAK",
                "score": 99,
            },
            {
                "entity_id": "Edfa Houston",
                "device_type": "edfa",
                "metric": "output_gsnr_db",
                "pre_mean": 14.0,
                "post_mean": 6.4,
                "delta": -7.6,
                "direction": "decrease",
            },
        ]
    )
    payload = {"event_metric_candidates": candidates}
    diagnosis = {
        "key_metric_features": [
            {
                "rank": 1,
                "entity_id": "Edfa Houston",
                "metric": "output_gsnr_db",
                "reason": "下游质量劣化最明显",
            },
            {
                "rank": 2,
                "entity_id": "invented entity",
                "metric": "invented_metric",
                "reason": "模型幻觉",
            },
        ]
    }

    selected = select_validated_metric_features(diagnosis, payload)

    assert [(item["entity_id"], item["metric"]) for item in selected] == [
        ("Edfa Houston", "output_gsnr_db"),
        ("Edfa Houston", "output_osnr_db"),
    ]
    assert selected[0]["selection_source"] == "online_model"
    assert selected[1]["selection_source"] == "observed_evidence_fallback"
    assert "fault_type" not in candidates[0]
    assert "score" not in candidates[0]


def test_calibration_repairs_false_normal_when_metric_evidence_is_abnormal() -> None:
    payload = build_diagnosis_payload(
        run_id="run_pmd",
        device_type="fiber",
        entity_id="fiber_1",
        performance_event_summary={
            "status": "ABNORMAL",
            "device_type": "fiber",
            "entity_id": "fiber_1",
            "key_metric_changes": [
                {
                    "metric": "pmd_ps",
                    "direction": "increase",
                    "delta": 80.0,
                    "relative_delta": 200.0,
                    "significant": True,
                    "harmful": True,
                    "score": 800.0,
                }
            ],
        },
        event_summary_for_diagnosis={"events": []},
        knowledge_query={"query_text": "fiber pmd increase"},
        knowledge_results=[],
        signature_matches=[{"fault_type": "NORMAL_STATE", "similarity": 0.8, "support": 10}],
    )
    diagnosis = calibrate_diagnosis_output({"status": "NORMAL", "summary": "ok", "top_causes": []}, payload)

    assert diagnosis["status"] == "ABNORMAL"
    assert diagnosis["top_causes"][0]["fault_type"] == "FIBER_PMD_SURGE"
    assert diagnosis["_calibrated"] is True


def test_calibration_completes_top_three_when_model_returns_one_cause() -> None:
    payload = build_diagnosis_payload(
        run_id="run_pmd",
        device_type="fiber",
        entity_id="fiber_1",
        performance_event_summary={
            "status": "ABNORMAL",
            "device_type": "fiber",
            "entity_id": "fiber_1",
            "key_metric_changes": [
                {
                    "metric": "pmd_ps",
                    "direction": "increase",
                    "delta": 80.0,
                    "relative_delta": 200.0,
                    "significant": True,
                    "harmful": True,
                    "score": 800.0,
                }
            ],
        },
        event_summary_for_diagnosis={"events": []},
        knowledge_query={"query_text": "fiber pmd increase"},
        knowledge_results=[],
        signature_matches=[
            {"fault_type": "FIBER_PMD_SURGE", "similarity": 0.99, "support": 50},
            {"fault_type": "FIBER_ATTENUATION_SURGE", "similarity": 0.71, "support": 50},
        ],
    )
    diagnosis = calibrate_diagnosis_output(
        {
            "status": "ABNORMAL",
            "summary": "only one",
            "top_causes": [
                {
                    "rank": 1,
                    "entity_id": "fiber_1",
                    "fault_type": "FIBER_PMD_SURGE",
                    "evidence": ["pmd_ps increase"],
                    "exclusion": "top evidence",
                }
            ],
        },
        payload,
    )

    assert [item["rank"] for item in diagnosis["top_causes"]] == [1, 2, 3]
    assert diagnosis["top_causes"][0]["fault_type"] == "FIBER_PMD_SURGE"
    assert len({item["fault_type"] for item in diagnosis["top_causes"]}) == 3


def test_online_result_is_not_forced_to_normal_when_local_hints_are_normal() -> None:
    payload = build_diagnosis_payload(
        run_id="run_online",
        device_type="edfa",
        entity_id="edfa_1",
        performance_event_summary={
            "status": "NORMAL",
            "device_type": "edfa",
            "entity_id": "edfa_1",
            "key_metric_changes": [],
        },
        event_summary_for_diagnosis={"events": []},
        knowledge_query={"query_text": "edfa gain degradation"},
        knowledge_results=[],
        signature_matches=[{"fault_type": "EDFA_GAIN_DEGRADATION", "similarity": 0.99, "support": 50}],
    )
    diagnosis = calibrate_diagnosis_output(
        {
            "status": "ABNORMAL",
            "summary": "online result",
            "top_causes": [
                {
                    "rank": 1,
                    "entity_id": "edfa_1",
                    "fault_type": "EDFA_GAIN_DEGRADATION",
                    "evidence": ["historical signature"],
                }
            ],
        },
        payload,
        force_observed_status=False,
    )

    assert diagnosis["status"] == "ABNORMAL"
    assert diagnosis["top_causes"][0]["fault_type"] == "EDFA_GAIN_DEGRADATION"


def test_payload_keeps_experiment_wide_observations() -> None:
    payload = build_diagnosis_payload(
        run_id="run_full",
        device_type="fiber",
        entity_id="fiber_1",
        performance_event_summary={
            "status": "ABNORMAL",
            "device_type": "fiber",
            "entity_id": "fiber_1",
            "key_metric_changes": [],
            "experiment_wide_observations": [
                {
                    "device_type": "edfa",
                    "candidate_entity_id": "edfa_1",
                    "candidate_score": 2.0,
                    "key_changes": [{"metric": "output_osnr_db", "delta": -1.0, "direction": "decrease"}],
                },
                {
                    "device_type": "fiber",
                    "candidate_entity_id": "fiber_1",
                    "candidate_score": 20.0,
                    "key_changes": [{"metric": "pmd_ps", "delta": 80.0, "direction": "increase"}],
                },
                {
                    "device_type": "roadm",
                    "candidate_entity_id": "roadm_1",
                    "candidate_score": 0.0,
                    "key_changes": [],
                },
            ],
        },
        event_summary_for_diagnosis={"events": []},
        knowledge_query={"query_text": "fiber pmd increase"},
        knowledge_results=[],
    )

    observations = payload["performance_event_summary"]["experiment_wide_observations"]
    assert {item["device_type"] for item in observations} == {"edfa", "fiber", "roadm"}
    assert observations[1]["key_changes"][0]["metric"] == "pmd_ps"


def test_online_top_one_is_not_replaced_by_local_candidate() -> None:
    payload = build_diagnosis_payload(
        run_id="run_online_conflict",
        device_type="fiber",
        entity_id="fiber_1",
        performance_event_summary={
            "status": "ABNORMAL",
            "device_type": "fiber",
            "entity_id": "fiber_1",
            "key_metric_changes": [
                {
                    "metric": "pmd_ps",
                    "direction": "increase",
                    "delta": 80.0,
                    "relative_delta": 20.0,
                    "harmful": True,
                    "score": 80.0,
                }
            ],
        },
        event_summary_for_diagnosis={"events": []},
        knowledge_query={"query_text": "fiber pmd increase"},
        knowledge_results=[],
    )
    diagnosis = calibrate_diagnosis_output(
        {
            "status": "ABNORMAL",
            "summary": "online conclusion",
            "top_causes": [
                {
                    "rank": 1,
                    "entity_id": "fiber_1",
                    "fault_type": "FIBER_ATTENUATION_SURGE",
                    "evidence": ["online evidence"],
                }
            ],
        },
        payload,
        force_observed_status=False,
    )

    assert diagnosis["top_causes"][0]["fault_type"] == "FIBER_ATTENUATION_SURGE"
    assert diagnosis.get("_calibrated") is not True


def test_online_primary_retries_transient_failure(monkeypatch) -> None:
    calls = []

    def fake_diagnose(_payload, _config):
        calls.append(1)
        if len(calls) == 1:
            raise TimeoutError("temporary timeout")
        return {
            "status": "ABNORMAL",
            "top_causes": [],
            "_engine_call": {"model": "qwen3.7-plus", "request_id": "req-1"},
        }

    monkeypatch.setattr(app_module, "diagnose_with_config", fake_diagnose)
    monkeypatch.setattr(app_module, "append_engine_call_log", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(app_module.time, "sleep", lambda _seconds: None)

    _diagnosis, metadata = app_module.diagnose_online_with_retry(
        {},
        {"model": "qwen3.7-plus"},
        run_id="run_retry",
    )

    assert len(calls) == 2
    assert metadata["mode"] == "ONLINE"
    assert metadata["attempts"] == 2
    assert metadata["fallback"] is False


def test_online_primary_does_not_retry_auth_failure(monkeypatch) -> None:
    calls = []

    def fake_diagnose(_payload, _config):
        calls.append(1)
        raise RuntimeError("HTTP Error 401: Unauthorized")

    monkeypatch.setattr(app_module, "diagnose_with_config", fake_diagnose)
    monkeypatch.setattr(app_module, "append_engine_call_log", lambda *_args, **_kwargs: None)

    try:
        app_module.diagnose_online_with_retry({}, {"model": "qwen3.7-plus"}, run_id="run_auth")
    except app_module.OnlineDiagnosisFailed as exc:
        assert exc.attempts == 1
    else:
        raise AssertionError("authentication failure should stop online retries")

    assert len(calls) == 1
