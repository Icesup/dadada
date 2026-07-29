from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any

from .analysis_pipeline import diagnose_run_with_local_rules, infer_device_type_from_entity
from .anomaly_service import is_harmful_change
from .experiment_service import get_run_dir
from .telemetry_service import load_telemetry, summarize_metric_change


FAULT_OBSERVABILITY_METRICS = {
    "EDFA_GAIN_DEGRADATION": ["actual_gain_db"],
    "EDFA_NOISE_SURGE": ["output_osnr_db", "output_gsnr_db", "nf_db", "accumulated_ase_dbm"],
    "EDFA_TILT_RIPPLE_ERROR": ["power_ripple_db", "power_variance"],
    "FIBER_ATTENUATION_SURGE": ["output_power_dbm"],
    "FIBER_NONLINEAR_ANOMALY": ["accumulated_nli_dbm"],
    "FIBER_PMD_SURGE": ["pmd_ps"],
    "ROADM_INBAND_CROSSTALK": ["current_osnr_db", "current_gsnr_db", "output_osnr_db", "output_gsnr_db"],
    "ROADM_WSS_FILTER_SHIFT": ["output_power_dbm", "current_osnr_db", "output_gsnr_db"],
}


def assess_injected_fault_observability(
    run: dict[str, Any],
    *,
    pre_window: float = 30.0,
    post_window: float = 30.0,
) -> dict[str, Any]:
    """仅在评估阶段检查已知注入机理是否反映到目标实体 telemetry。"""

    truth_type = str(run.get("scenario") or "UNKNOWN")
    truth_entity = str(run.get("fault_entity") or "")
    metrics = FAULT_OBSERVABILITY_METRICS.get(truth_type, [])
    if truth_type == "NORMAL_STATE":
        return {"applicable": False, "observed": None, "reason": "正常样本不检查故障注入机理。"}
    device_type = infer_device_type_from_entity(truth_entity)
    if not device_type or not metrics:
        return {
            "applicable": False,
            "observed": None,
            "reason": "当前故障类型尚未配置可观测性检查规则。",
        }

    records, errors = load_telemetry(get_run_dir(run), device_type)
    entity_exists = any(str(item.get("entity_id") or "") == truth_entity for item in records)
    trigger_tick = float(run.get("trigger_tick") or 0.0)
    summaries = [
        summarize_metric_change(
            records,
            entity_id=truth_entity,
            metric=metric,
            trigger_tick=trigger_tick,
            pre_window=pre_window,
            post_window=post_window,
        )
        for metric in metrics
    ]
    harmful_metrics = [str(item.get("metric")) for item in summaries if is_harmful_change(item)]

    if truth_type == "ROADM_WSS_FILTER_SHIFT":
        quality_metrics = {"current_osnr_db", "output_gsnr_db"}.intersection(harmful_metrics)
        power_summary = next((item for item in summaries if item.get("metric") == "output_power_dbm"), {})
        power_drop = (
            power_summary.get("direction") == "decrease"
            and isinstance(power_summary.get("delta"), (int, float))
            and abs(float(power_summary["delta"])) >= 1.0
        )
        observed = bool(quality_metrics) and (power_drop or len(quality_metrics) >= 2)
    else:
        observed = bool(harmful_metrics)

    public_summaries = [
        {key: value for key, value in item.items() if not key.startswith("_")}
        for item in summaries
    ]
    reason = (
        f"目标实体出现符合注入机理的显著变化指标: {', '.join(harmful_metrics)}。"
        if observed
        else "目标实体未出现符合注入机理且持续越过阈值的指标变化。"
    )
    return {
        "applicable": True,
        "observed": observed,
        "fault_type": truth_type,
        "device_type": device_type,
        "entity_id": truth_entity,
        "entity_exists": entity_exists,
        "harmful_metrics": harmful_metrics,
        "metric_summaries": public_summaries,
        "errors": errors,
        "reason": reason,
    }


def evaluate_diagnosis(run: dict[str, Any], diagnosis: dict[str, Any]) -> dict[str, Any]:
    """诊断完成后读取 ground truth，并生成单次评测结果。"""

    truth_type = str(run.get("scenario") or "UNKNOWN")
    truth_entity = str(run.get("fault_entity") or "")
    truth_device = infer_device_type_from_entity(truth_entity)
    predicted_status = str(diagnosis.get("status") or "UNKNOWN")
    top_causes = [item for item in diagnosis.get("top_causes", []) if isinstance(item, dict)]
    predicted_types = [str(item.get("fault_type") or "") for item in top_causes]
    predicted_entities = [str(item.get("entity_id") or "") for item in top_causes]
    selected_device = str(diagnosis.get("selected_device_type") or "")
    selected_entity = str(diagnosis.get("selected_entity_id") or "")
    predicted_top1_device = infer_device_type_from_entity(predicted_entities[0]) if predicted_entities else ""
    observability = diagnosis.get("observability") if isinstance(diagnosis.get("observability"), dict) else {}
    direct_anomaly_observed = bool(observability.get("direct_anomaly_observed"))
    injection_observability = (
        diagnosis.get("injected_fault_observability")
        if isinstance(diagnosis.get("injected_fault_observability"), dict)
        else {}
    )
    injected_signature_observed = injection_observability.get("observed")

    is_normal = truth_type == "NORMAL_STATE"
    if is_normal:
        type_hit_1 = predicted_status == "NORMAL" and not top_causes
        type_hit_3 = type_hit_1
        entity_hit_1 = True
        entity_hit_3 = True
        device_hit = True
    else:
        type_hit_1 = bool(predicted_types) and predicted_types[0] == truth_type
        type_hit_3 = truth_type in predicted_types[:3]
        entity_hit_1 = bool(predicted_entities) and predicted_entities[0] == truth_entity
        entity_hit_3 = truth_entity in predicted_entities[:3]
        device_hit = bool(truth_device) and (predicted_top1_device == truth_device or selected_device == truth_device)

    return {
        "run_id": run.get("run_id"),
        "truth_type": truth_type,
        "truth_entity": truth_entity,
        "truth_device": truth_device,
        "predicted_status": predicted_status,
        "selected_device": selected_device,
        "selected_entity": selected_entity,
        "predicted_top1_type": predicted_types[0] if predicted_types else "",
        "predicted_top3_types": predicted_types[:3],
        "type_hit_1": type_hit_1,
        "type_hit_3": type_hit_3,
        "entity_hit_1": entity_hit_1,
        "entity_hit_3": entity_hit_3,
        "device_hit": device_hit,
        "direct_anomaly_observed": direct_anomaly_observed,
        "injected_signature_observed": injected_signature_observed,
        "injected_signature_observability": injection_observability,
    }


def summarize_evaluation(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """汇总全量样本及直接可观测样本的评测指标。"""

    total = len(rows)
    by_type: dict[str, dict[str, Any]] = {}
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[str(row.get("truth_type") or "UNKNOWN")].append(row)

    def ratio(key: str, data: list[dict[str, Any]]) -> float:
        return sum(1 for item in data if item.get(key)) / len(data) if data else 0.0

    for truth_type, data in sorted(grouped.items()):
        abnormal_data = data if truth_type != "NORMAL_STATE" else []
        observable_data = [item for item in abnormal_data if item.get("direct_anomaly_observed")]
        signature_observable_data = [item for item in abnormal_data if item.get("injected_signature_observed") is True]
        by_type[truth_type] = {
            "count": len(data),
            "observable_count": len(observable_data),
            "observable_rate": len(observable_data) / len(abnormal_data) if abnormal_data else 1.0,
            "type_hit_1": ratio("type_hit_1", data),
            "type_hit_3": ratio("type_hit_3", data),
            "observable_type_hit_1": ratio("type_hit_1", observable_data),
            "signature_observable_count": len(signature_observable_data),
            "signature_observable_rate": len(signature_observable_data) / len(abnormal_data) if abnormal_data else 1.0,
            "signature_observable_type_hit_1": ratio("type_hit_1", signature_observable_data),
            "entity_hit_1": ratio("entity_hit_1", data),
            "device_hit": ratio("device_hit", data),
            "predicted_top1_counts": dict(Counter(item.get("predicted_top1_type") or "NONE" for item in data)),
        }

    confusion: dict[str, dict[str, int]] = {}
    for row in rows:
        truth_type = str(row.get("truth_type") or "UNKNOWN")
        predicted_type = str(row.get("predicted_top1_type") or "NONE")
        confusion.setdefault(truth_type, {})
        confusion[truth_type][predicted_type] = confusion[truth_type].get(predicted_type, 0) + 1

    misses = [
        {
            "run_id": row.get("run_id"),
            "truth_type": row.get("truth_type"),
            "predicted_top1_type": row.get("predicted_top1_type") or "NONE",
            "predicted_top3_types": row.get("predicted_top3_types") or [],
            "truth_entity": row.get("truth_entity"),
            "selected_entity": row.get("selected_entity"),
            "type_hit_1": row.get("type_hit_1"),
            "type_hit_3": row.get("type_hit_3"),
            "entity_hit_1": row.get("entity_hit_1"),
            "device_hit": row.get("device_hit"),
            "direct_anomaly_observed": row.get("direct_anomaly_observed"),
            "injected_signature_observed": row.get("injected_signature_observed"),
        }
        for row in rows
        if not row.get("type_hit_1") or not row.get("entity_hit_1")
    ]

    abnormal_rows = [row for row in rows if row.get("truth_type") != "NORMAL_STATE"]
    observable_abnormal_rows = [row for row in abnormal_rows if row.get("direct_anomaly_observed")]
    signature_observable_rows = [row for row in abnormal_rows if row.get("injected_signature_observed") is True]
    return {
        "total": total,
        "abnormal_total": len(abnormal_rows),
        "observable_abnormal": len(observable_abnormal_rows),
        "observable_rate": len(observable_abnormal_rows) / len(abnormal_rows) if abnormal_rows else 0.0,
        "observable_type_hit_1": ratio("type_hit_1", observable_abnormal_rows),
        "observable_type_hit_3": ratio("type_hit_3", observable_abnormal_rows),
        "signature_observable_abnormal": len(signature_observable_rows),
        "signature_observable_rate": len(signature_observable_rows) / len(abnormal_rows) if abnormal_rows else 0.0,
        "signature_observable_type_hit_1": ratio("type_hit_1", signature_observable_rows),
        "signature_observable_type_hit_3": ratio("type_hit_3", signature_observable_rows),
        "type_hit_1": ratio("type_hit_1", rows),
        "type_hit_3": ratio("type_hit_3", rows),
        "entity_hit_1": ratio("entity_hit_1", rows),
        "entity_hit_3": ratio("entity_hit_3", rows),
        "device_hit": ratio("device_hit", rows),
        "by_type": by_type,
        "confusion": confusion,
        "misses": misses,
    }


def evaluate_runs_with_local_rules(
    runs: list[dict[str, Any]],
    *,
    knowledge_chunks_path,
    signature_library_path=None,
    pre_window: float = 30.0,
    post_window: float = 30.0,
) -> dict[str, Any]:
    """对一组实验运行本地规则诊断并评估命中率。"""

    rows: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    for run in runs:
        try:
            diagnosis = diagnose_run_with_local_rules(
                run,
                knowledge_chunks_path=knowledge_chunks_path,
                signature_library_path=signature_library_path,
                pre_window=pre_window,
                post_window=post_window,
            )
            diagnosis["injected_fault_observability"] = assess_injected_fault_observability(
                run,
                pre_window=pre_window,
                post_window=post_window,
            )
            rows.append(evaluate_diagnosis(run, diagnosis))
        except Exception as exc:  # noqa: BLE001
            errors.append({"run_id": str(run.get("run_id")), "error": str(exc)})
    return {
        "summary": summarize_evaluation(rows),
        "rows": rows,
        "errors": errors,
    }
