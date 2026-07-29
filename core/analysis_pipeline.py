from __future__ import annotations

from pathlib import Path
import re
from typing import Any

from adapters.rule_adapter import RuleDiagnosisAdapter

from .anomaly_service import abnormal_evidence_score, build_performance_event_summary, is_harmful_change, metric_change_score
from .event_service import build_event_summary_for_diagnosis, load_service_lifecycle, load_simulation_events
from .experiment_service import get_run_dir
from .engine_service import build_diagnosis_payload, calibrate_diagnosis_output
from .knowledge_service import build_knowledge_query, load_knowledge_chunks, search_knowledge_chunks
from .telemetry_service import load_telemetry, list_metric_fields, summarize_metric_change


DEVICE_TYPES = ["edfa", "fiber", "roadm"]

DEVICE_METRIC_PRIORITY = {
    "edfa": ["output_gsnr_db", "output_osnr_db", "actual_gain_db", "output_power_dbm", "nf_db", "power_ripple_db"],
    "fiber": [
        "output_power_dbm",
        "current_osnr_db",
        "current_gsnr_db",
        "pmd_ps",
        "accumulated_nli_dbm",
        "accumulated_ase_dbm",
    ],
    "roadm": ["output_power_dbm", "current_osnr_db", "current_gsnr_db", "output_gsnr_db", "output_osnr_db"],
}

AUTO_DIAG_EXCLUDED_METRICS = {"cd_ps_nm", "fiber_length_km"}

FAULT_DEVICE_HINTS = {
    "EDFA_GAIN_DEGRADATION": "edfa",
    "EDFA_NOISE_SURGE": "edfa",
    "EDFA_TILT_RIPPLE_ERROR": "edfa",
    "FIBER_ATTENUATION_SURGE": "fiber",
    "FIBER_NONLINEAR_ANOMALY": "fiber",
    "FIBER_PMD_SURGE": "fiber",
    "ROADM_INBAND_CROSSTALK": "roadm",
    "ROADM_WSS_FILTER_SHIFT": "roadm",
}

SOURCE_METRIC_MIN_SCORES = {
    # 仿真中 PMD 注入约 80 ps；小于 5 ps 的漂移不应压过明确的 ROADM 质量证据。
    "pmd_ps": 50.0,
    # 历史非线性注入的 NLI 变化约 15 dB 以上，低强度传播变化只作为佐证。
    "accumulated_nli_dbm": 10.0,
}


def infer_device_type_from_entity(entity_id: str | None) -> str | None:
    """从实体 ID 推断设备类型；不使用故障类型答案。"""

    text = (entity_id or "").lower()
    if text.startswith("edfa"):
        return "edfa"
    if text.startswith("fiber"):
        return "fiber"
    if text.startswith("roadm"):
        return "roadm"
    return None


def select_metrics_for_device(device_type: str, records: list[dict[str, Any]]) -> list[str]:
    """选择该设备上实际存在的诊断指标。"""

    available = list_metric_fields(records)
    available = [metric for metric in available if metric not in AUTO_DIAG_EXCLUDED_METRICS]
    preferred = [metric for metric in DEVICE_METRIC_PRIORITY.get(device_type, []) if metric in available]
    extra = [metric for metric in available if metric not in preferred]
    return preferred + extra


def summarize_entity(
    records: list[dict[str, Any]],
    *,
    entity_id: str,
    metrics: list[str],
    trigger_tick: float,
    pre_window: float,
    post_window: float,
) -> list[dict[str, Any]]:
    """计算单个候选实体的指标变化摘要。"""

    return [
        summarize_metric_change(
            records,
            entity_id=entity_id,
            metric=metric,
            trigger_tick=trigger_tick,
            pre_window=pre_window,
            post_window=post_window,
        )
        for metric in metrics
    ]


def select_candidate_entity(
    records: list[dict[str, Any]],
    *,
    metrics: list[str],
    trigger_tick: float,
    pre_window: float,
    post_window: float,
) -> dict[str, Any]:
    """按故障窗口前后指标变化自动选择候选实体，不读取 ground truth。"""

    grouped_records: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        entity_id = record.get("entity_id")
        if entity_id:
            grouped_records.setdefault(str(entity_id), []).append(record)
    best: dict[str, Any] | None = None
    for entity_id in sorted(grouped_records):
        summaries = summarize_entity(
            grouped_records[entity_id],
            entity_id=entity_id,
            metrics=metrics,
            trigger_tick=trigger_tick,
            pre_window=pre_window,
            post_window=post_window,
        )
        score = abnormal_evidence_score(summaries)
        candidate = {"entity_id": entity_id, "score": score, "metric_summaries": summaries}
        if best is None or score > float(best.get("score") or 0.0):
            best = candidate
    return best or {"entity_id": "", "score": 0.0, "metric_summaries": []}


def select_source_candidate_entity(
    records: list[dict[str, Any]],
    *,
    source_metrics: list[str],
    summary_metrics: list[str],
    trigger_tick: float,
    pre_window: float,
    post_window: float,
) -> dict[str, Any]:
    """先选择异常链路，再定位该链路上最早出现直接特征的实体。"""

    grouped_records: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        entity_id = record.get("entity_id")
        if entity_id:
            grouped_records.setdefault(str(entity_id), []).append(record)

    candidates: list[dict[str, Any]] = []
    for entity_id, entity_records in grouped_records.items():
        source_summaries = summarize_entity(
            entity_records,
            entity_id=entity_id,
            metrics=source_metrics,
            trigger_tick=trigger_tick,
            pre_window=pre_window,
            post_window=post_window,
        )
        harmful = [item for item in source_summaries if is_harmful_change(item)]
        if not harmful:
            continue
        match = re.search(r"\((\d+)/(\d+)\)\s*$", entity_id)
        span_index = int(match.group(1)) if match else None
        route_match = re.search(r"\(([^()]*(?:→|->)[^()]*)\)", entity_id)
        if route_match and match:
            route_key = f"{route_match.group(1).strip()}|{match.group(2)}"
        else:
            route_key = entity_id[: match.start()].rstrip("_- ") if match else entity_id
        candidates.append(
            {
                "entity_id": entity_id,
                "route_key": route_key,
                "span_index": span_index,
                "source_score": max(metric_change_score(item) for item in harmful),
            }
        )

    if not candidates:
        return {"entity_id": "", "score": 0.0, "metric_summaries": []}

    route_scores: dict[str, float] = {}
    for item in candidates:
        route_key = str(item["route_key"])
        route_scores[route_key] = max(route_scores.get(route_key, 0.0), float(item["source_score"]))
    best_route = max(route_scores, key=route_scores.get)
    route_candidates = [item for item in candidates if item["route_key"] == best_route]
    positioned = [item for item in route_candidates if isinstance(item.get("span_index"), int)]
    selected = (
        min(positioned, key=lambda item: int(item["span_index"]))
        if positioned
        else max(route_candidates, key=lambda item: float(item["source_score"]))
    )
    entity_id = str(selected["entity_id"])
    metric_summaries = summarize_entity(
        grouped_records[entity_id],
        entity_id=entity_id,
        metrics=summary_metrics,
        trigger_tick=trigger_tick,
        pre_window=pre_window,
        post_window=post_window,
    )
    return {
        "entity_id": entity_id,
        "score": abnormal_evidence_score(metric_summaries),
        "metric_summaries": metric_summaries,
    }


def select_candidate_device_and_entity(
    run: dict[str, Any],
    *,
    pre_window: float = 30.0,
    post_window: float = 30.0,
    analysis_tick: float | None = None,
) -> dict[str, Any]:
    """在三类设备中选择最值得分析的候选对象，不使用故障实体答案。"""

    run_dir = get_run_dir(run)
    trigger_tick = float(analysis_tick if analysis_tick is not None else run.get("trigger_tick") or 0.0)
    best: dict[str, Any] | None = None
    for device_type in DEVICE_TYPES:
        records, errors = load_telemetry(run_dir, device_type)
        metrics = select_metrics_for_device(device_type, records)
        candidate = select_candidate_entity(
            records,
            metrics=metrics,
            trigger_tick=trigger_tick,
            pre_window=pre_window,
            post_window=post_window,
        )
        candidate.update(
            {
                "device_type": device_type,
                "metrics": metrics,
                "records": records,
                "errors": errors,
            }
        )
        if best is None or float(candidate.get("score") or 0.0) > float(best.get("score") or 0.0):
            best = candidate
    return best or {"device_type": "edfa", "entity_id": "", "score": 0.0, "metric_summaries": [], "metrics": []}


def select_candidate_for_device(
    run: dict[str, Any],
    device_type: str,
    *,
    pre_window: float = 30.0,
    post_window: float = 30.0,
    analysis_tick: float | None = None,
) -> dict[str, Any]:
    """选择指定设备类型下的候选实体。"""

    run_dir = get_run_dir(run)
    trigger_tick = float(analysis_tick if analysis_tick is not None else run.get("trigger_tick") or 0.0)
    records, errors = load_telemetry(run_dir, device_type)
    metrics = select_metrics_for_device(device_type, records)
    candidate = select_candidate_entity(
        records,
        metrics=metrics,
        trigger_tick=trigger_tick,
        pre_window=pre_window,
        post_window=post_window,
    )
    candidate.update({"device_type": device_type, "metrics": metrics, "records": records, "errors": errors})
    return candidate


def allow_signature_override(
    *,
    global_top_type: str,
    global_top_source_strength: float,
    signature_top_type: str,
    signature_top_margin: float,
    rule_candidate_types: set[str],
    roadm_signature_margin: float,
) -> bool:
    """仅在没有设备专属直接证据时允许历史相似度改变 Top-1。"""

    if signature_top_type == "EDFA_GAIN_DEGRADATION" and global_top_type == "EDFA_GAIN_DEGRADATION":
        return True
    if global_top_type == "EDFA_GAIN_DEGRADATION" and global_top_source_strength >= 8.0:
        return False
    if signature_top_type not in {"ROADM_WSS_FILTER_SHIFT", "ROADM_INBAND_CROSSTALK"}:
        return False
    supported = (
        signature_top_type in rule_candidate_types
        or signature_top_margin >= 0.01
        or (signature_top_margin >= 0.005 and roadm_signature_margin >= 0.003)
    )
    noise_margin_ok = global_top_type != "EDFA_NOISE_SURGE" or roadm_signature_margin >= 0.003
    return supported and noise_margin_ok


def is_strong_source_change(changes: dict[str, dict[str, Any]], metric: str) -> bool:
    """判断设备专属源指标是否达到根因锚点强度。"""

    item = changes.get(metric)
    if not item or not is_harmful_change(item):
        return False
    return metric_change_score(item) >= SOURCE_METRIC_MIN_SCORES.get(metric, 1.0)


def build_global_diagnostic_hints(
    run: dict[str, Any],
    *,
    pre_window: float = 30.0,
    post_window: float = 30.0,
    analysis_tick: float | None = None,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """扫描三类设备的全部候选摘要，生成不依赖页面指标选择的诊断候选。"""

    trigger_tick = float(analysis_tick if analysis_tick is not None else run.get("trigger_tick") or 0.0)
    ranked: list[dict[str, Any]] = []
    candidate_by_device: dict[str, dict[str, Any]] = {}

    def change_map(candidate: dict[str, Any]) -> dict[str, dict[str, Any]]:
        return {
            str(item.get("metric")): item
            for item in candidate.get("metric_summaries", [])
            if isinstance(item, dict) and item.get("metric")
        }

    def score_of(changes: dict[str, dict[str, Any]], metrics: list[str]) -> float:
        return max((metric_change_score(changes[m]) for m in metrics if m in changes), default=0.0)

    def harmful(changes: dict[str, dict[str, Any]], metric: str, direction: str | None = None) -> bool:
        item = changes.get(metric)
        if not item or not is_harmful_change(item):
            return False
        return direction is None or item.get("direction") == direction

    def observed_drop(changes: dict[str, dict[str, Any]], metric: str, min_score: float = 1.0) -> bool:
        item = changes.get(metric)
        return bool(item and item.get("direction") == "decrease" and metric_change_score(item) >= min_score)

    direction_labels = {"increase": "升高", "decrease": "下降", "stable": "稳定", "unknown": "样本不足"}

    def evidence(changes: dict[str, dict[str, Any]], metrics: list[str]) -> list[str]:
        rows: list[str] = []
        for metric in metrics:
            item = changes.get(metric)
            if not item:
                continue
            delta = item.get("delta")
            direction = direction_labels.get(str(item.get("direction")), str(item.get("direction")))
            if isinstance(delta, (int, float)):
                rows.append(f"{metric} 在窗口后{direction}，均值变化 {delta:.4f}")
            else:
                rows.append(f"{metric} 在窗口后{direction}")
        return rows

    def add_candidate(
        *,
        fault_type: str,
        candidate: dict[str, Any],
        changes: dict[str, dict[str, Any]],
        metrics: list[str],
        base_score: float,
        rule: str,
    ) -> None:
        ranked.append(
            {
                "fault_type": fault_type,
                "entity_id": str(candidate.get("entity_id") or ""),
                "device_type": str(candidate.get("device_type") or ""),
                "evidence": evidence(changes, metrics),
                "priority": "HIGH" if base_score >= 80 else "MEDIUM",
                "rule": rule,
                "source_strength": round(score_of(changes, metrics[:1]), 4),
                "_score": base_score + score_of(changes, metrics),
            }
        )

    for device_type in DEVICE_TYPES:
        candidate = select_candidate_for_device(
            run,
            device_type,
            pre_window=pre_window,
            post_window=post_window,
            analysis_tick=trigger_tick,
        )
        candidate_by_device[device_type] = candidate
        changes = change_map(candidate)

        def source_candidate(source_metrics: list[str]) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
            located = select_source_candidate_entity(
                list(candidate.get("records") or []),
                source_metrics=source_metrics,
                summary_metrics=list(candidate.get("metrics") or []),
                trigger_tick=trigger_tick,
                pre_window=pre_window,
                post_window=post_window,
            )
            if not located.get("entity_id"):
                return candidate, changes
            located.update(
                {
                    "device_type": device_type,
                    "metrics": candidate.get("metrics") or [],
                    "records": candidate.get("records") or [],
                    "errors": candidate.get("errors") or [],
                }
            )
            return located, change_map(located)

        if device_type == "fiber":
            pmd_candidate, pmd_changes = source_candidate(["pmd_ps"])
            if is_strong_source_change(pmd_changes, "pmd_ps"):
                add_candidate(
                    fault_type="FIBER_PMD_SURGE",
                    candidate=pmd_candidate,
                    changes=pmd_changes,
                    metrics=["pmd_ps", "output_power_dbm", "current_osnr_db", "current_gsnr_db"],
                    base_score=100,
                    rule="光纤 PMD 指标在故障窗口后显著升高。",
                )
            nli_candidate, nli_changes = source_candidate(["accumulated_nli_dbm"])
            if is_strong_source_change(nli_changes, "accumulated_nli_dbm"):
                add_candidate(
                    fault_type="FIBER_NONLINEAR_ANOMALY",
                    candidate=nli_candidate,
                    changes=nli_changes,
                    metrics=["accumulated_nli_dbm", "current_gsnr_db", "output_power_dbm"],
                    base_score=100,
                    rule="光纤 NLI 累积量显著升高。",
                )
            attenuation_candidate, attenuation_changes = source_candidate(["output_power_dbm"])
            if harmful(attenuation_changes, "output_power_dbm", "decrease"):
                add_candidate(
                    fault_type="FIBER_ATTENUATION_SURGE",
                    candidate=attenuation_candidate,
                    changes=attenuation_changes,
                    metrics=["output_power_dbm", "current_osnr_db", "current_gsnr_db"],
                    base_score=100,
                    rule="光纤输出功率显著下降，符合链路衰耗突增特征。",
                )
        elif device_type == "edfa":
            gain_candidate, gain_changes = source_candidate(["actual_gain_db"])
            gain_drop = harmful(gain_changes, "actual_gain_db", "decrease")
            noise_candidate, noise_changes = source_candidate(["nf_db", "output_osnr_db", "output_gsnr_db"])
            quality_drop = harmful(noise_changes, "output_osnr_db", "decrease") or harmful(
                noise_changes, "output_gsnr_db", "decrease"
            )
            if gain_drop:
                add_candidate(
                    fault_type="EDFA_GAIN_DEGRADATION",
                    candidate=gain_candidate,
                    changes=gain_changes,
                    metrics=["actual_gain_db", "output_power_dbm", "output_osnr_db", "output_gsnr_db"],
                    base_score=120,
                    rule="EDFA 实际增益显著下降。",
                )
            if quality_drop and not gain_drop:
                add_candidate(
                    fault_type="EDFA_NOISE_SURGE",
                    candidate=noise_candidate,
                    changes=noise_changes,
                    metrics=["output_osnr_db", "output_gsnr_db", "actual_gain_db", "output_power_dbm"],
                    base_score=95,
                    rule="EDFA 输出 OSNR/GSNR 下降而增益未同步下降，优先考虑噪声异常。",
                )
            tilt_candidate, tilt_changes = source_candidate(["power_ripple_db", "power_variance"])
            if harmful(tilt_changes, "power_ripple_db", "increase") or harmful(
                tilt_changes, "power_variance", "increase"
            ):
                add_candidate(
                    fault_type="EDFA_TILT_RIPPLE_ERROR",
                    candidate=tilt_candidate,
                    changes=tilt_changes,
                    metrics=["power_ripple_db", "power_variance", "output_osnr_db"],
                    base_score=100,
                    rule="EDFA 功率波纹或功率方差显著升高。",
                )
        else:
            quality_drop = harmful(changes, "current_osnr_db", "decrease") or harmful(changes, "output_gsnr_db", "decrease")
            power_harm = harmful(changes, "output_power_dbm", "decrease")
            power_drop = power_harm or observed_drop(changes, "output_power_dbm")
            output_power = changes.get("output_power_dbm", {})
            output_power_score = metric_change_score(output_power) if output_power else 0.0
            output_power_increase = output_power.get("direction") == "increase" and output_power_score >= 3.0
            quality_score = max(
                metric_change_score(changes.get("current_osnr_db", {})) if changes.get("current_osnr_db") else 0.0,
                metric_change_score(changes.get("output_gsnr_db", {})) if changes.get("output_gsnr_db") else 0.0,
            )
            if (power_harm and (quality_drop or output_power_score >= 3.0)) or (quality_drop and power_drop):
                add_candidate(
                    fault_type="ROADM_WSS_FILTER_SHIFT",
                    candidate=candidate,
                    changes=changes,
                    metrics=["output_power_dbm", "current_osnr_db", "output_gsnr_db"],
                    base_score=85,
                    rule="ROADM 质量指标下降且输出功率同步下降，优先考虑 WSS/滤波偏移。",
                )
            elif quality_drop and (quality_score >= 2.0 or not output_power_increase):
                add_candidate(
                    fault_type="ROADM_INBAND_CROSSTALK",
                    candidate=candidate,
                    changes=changes,
                    metrics=["current_osnr_db", "output_gsnr_db", "output_power_dbm"],
                    base_score=85,
                    rule="ROADM 质量指标下降但输出功率未形成同步下降证据，优先考虑带内串扰。",
                )
                add_candidate(
                    fault_type="ROADM_WSS_FILTER_SHIFT",
                    candidate=candidate,
                    changes=changes,
                    metrics=["output_power_dbm", "current_osnr_db", "output_gsnr_db"],
                    base_score=75,
                    rule="ROADM 质量指标下降时，WSS/滤波偏移作为同族候选保留，由历史特征进一步排序。",
                )
            if power_harm:
                add_candidate(
                    fault_type="EDFA_GAIN_DEGRADATION",
                    candidate=candidate,
                    changes=changes,
                    metrics=["output_power_dbm", "current_osnr_db", "output_gsnr_db"],
                    base_score=55,
                    rule="下游 ROADM 出现功率下降时，需要排查上游放大器增益衰退或链路衰耗传播。",
                )

    ranked = sorted(ranked, key=lambda item: float(item.get("_score") or 0.0), reverse=True)
    for rank, item in enumerate(ranked, 1):
        item["rank"] = rank
    public_candidates = [{key: value for key, value in item.items() if key != "_score"} for item in ranked[:3]]
    if not public_candidates:
        return {
            "observed_status": "NORMAL",
            "evidence_source": "telemetry",
            "top_candidates": [],
            "notes": ["全设备扫描未发现稳定的性能劣化证据链。"],
        }, None
    top_device = str(public_candidates[0].get("device_type") or "")
    top_entity = str(public_candidates[0].get("entity_id") or "")
    selected_candidate = candidate_by_device.get(top_device)
    if selected_candidate is not None and top_entity and selected_candidate.get("entity_id") != top_entity:
        selected_candidate = dict(selected_candidate)
        selected_candidate["entity_id"] = top_entity
        selected_candidate["metric_summaries"] = summarize_entity(
            list(selected_candidate.get("records") or []),
            entity_id=top_entity,
            metrics=list(selected_candidate.get("metrics") or []),
            trigger_tick=trigger_tick,
            pre_window=pre_window,
            post_window=post_window,
        )
    return {
        "observed_status": "ABNORMAL",
        "evidence_source": "telemetry",
        "device_type": top_device,
        "entity_id": public_candidates[0].get("entity_id") or "",
        "top_candidates": public_candidates,
        "notes": ["诊断候选来自全设备遥测扫描，不依赖页面当前选择的指标。"],
    }, selected_candidate


def _signature_to_top_causes(
    signature_matches: list[dict[str, Any]],
    *,
    run: dict[str, Any],
    fallback_candidate: dict[str, Any],
    pre_window: float,
    post_window: float,
    analysis_tick: float | None = None,
) -> tuple[str, list[dict[str, Any]], dict[str, Any]]:
    """把历史特征库 Top-N 转成诊断候选根因。"""

    if not signature_matches:
        return "", [], fallback_candidate
    top = signature_matches[0]
    signature_status = "NORMAL" if top.get("fault_type") == "NORMAL_STATE" else "ABNORMAL"
    causes: list[dict[str, Any]] = []
    selected_candidate = fallback_candidate
    for rank, match in enumerate(signature_matches[:3], 1):
        fault_type = str(match.get("fault_type") or "")
        if fault_type == "NORMAL_STATE":
            continue
        device_type = FAULT_DEVICE_HINTS.get(fault_type, str(fallback_candidate.get("device_type") or "edfa"))
        candidate = select_candidate_for_device(
            run,
            device_type,
            pre_window=pre_window,
            post_window=post_window,
            analysis_tick=analysis_tick,
        )
        if not causes:
            selected_candidate = candidate
        causes.append(
            {
                "rank": len(causes) + 1,
                "entity_id": str(candidate.get("entity_id") or ""),
                "fault_type": fault_type,
                "evidence": [
                    f"历史特征库相似度 {float(match.get('similarity') or 0.0):.4f}",
                    f"历史样本支持数 {int(match.get('support') or 0)}",
                ],
                "exclusion": "候选排序来自历史样本特征相似度，需结合知识库内容和关键指标变化复核。",
            }
        )
    return signature_status, causes, selected_candidate


def diagnose_run_with_local_rules(
    run: dict[str, Any],
    *,
    knowledge_chunks_path,
    signature_library_path: Path | None = None,
    pre_window: float = 30.0,
    post_window: float = 30.0,
    analysis_tick: float | None = None,
) -> dict[str, Any]:
    """运行单个 episode 的最小诊断闭环，ground truth 只由调用方评估使用。"""

    run_dir = get_run_dir(run)
    trigger_tick = float(analysis_tick if analysis_tick is not None else run.get("trigger_tick") or 0.0)
    candidate = select_candidate_device_and_entity(
        run,
        pre_window=pre_window,
        post_window=post_window,
        analysis_tick=trigger_tick,
    )
    signature_matches: list[dict[str, Any]] = []
    signature_status = ""
    signature_causes: list[dict[str, Any]] = []
    if signature_library_path is not None:
        from .signature_service import classify_with_signature_library, extract_run_signature_features, load_signature_library

        library = load_signature_library(signature_library_path)
        if library:
            features = extract_run_signature_features(
                run,
                pre_window=pre_window,
                post_window=post_window,
                analysis_tick=trigger_tick,
            )
            signature_matches = classify_with_signature_library(
                features,
                library,
                top_n=4,
                exclude_run_id=str(run.get("run_id") or ""),
            )
            signature_status, signature_causes, _signature_candidate = _signature_to_top_causes(
                signature_matches,
                run=run,
                fallback_candidate=candidate,
                pre_window=pre_window,
                post_window=post_window,
                analysis_tick=trigger_tick,
            )
    global_hints, global_candidate = build_global_diagnostic_hints(
        run,
        pre_window=pre_window,
        post_window=post_window,
        analysis_tick=trigger_tick,
    )
    direct_telemetry_observable = global_hints.get("observed_status") == "ABNORMAL"
    if global_candidate is None and signature_matches:
        top_signature = signature_matches[0]
        normal_similarity = max(
            (
                float(item.get("similarity") or 0.0)
                for item in signature_matches
                if item.get("fault_type") == "NORMAL_STATE"
            ),
            default=0.0,
        )
        top_similarity = float(top_signature.get("similarity") or 0.0)
        if (
            top_signature.get("fault_type") == "EDFA_GAIN_DEGRADATION"
            and top_similarity >= 0.985
            and top_similarity - normal_similarity >= 0.006
        ):
            global_candidate = select_candidate_for_device(
                run,
                "edfa",
                pre_window=pre_window,
                post_window=post_window,
                analysis_tick=trigger_tick,
            )
            global_hints = {
                "observed_status": "INCONCLUSIVE",
                "evidence_source": "historical_similarity",
                "device_type": "edfa",
                "entity_id": global_candidate.get("entity_id") or "",
                "top_candidates": [
                    {
                        "fault_type": "EDFA_GAIN_DEGRADATION",
                        "entity_id": global_candidate.get("entity_id") or "",
                        "device_type": "edfa",
                        "evidence": [
                            f"历史特征库中 EDFA_GAIN_DEGRADATION 相似度 {top_similarity:.4f}",
                            f"与 NORMAL_STATE 相似度差值 {top_similarity - normal_similarity:.4f}",
                        ],
                        "priority": "MEDIUM",
                        "rule": "当前窗口直接劣化证据较弱，但历史案例特征更接近 EDFA 增益衰退。",
                    }
                ],
                "notes": ["该候选仅由历史特征库弱证据召回，不能替代当前遥测证据。"],
            }
    if global_candidate is not None:
        candidate = global_candidate
    performance_summary = build_performance_event_summary(
        run_id=str(run.get("run_id")),
        device_type=str(candidate.get("device_type")),
        entity_id=str(candidate.get("entity_id")),
        trigger_tick=trigger_tick,
        metric_summaries=list(candidate.get("metric_summaries") or []),
    )
    events, _ = load_simulation_events(run_dir)
    lifecycle, _ = load_service_lifecycle(run_dir)
    event_summary = build_event_summary_for_diagnosis(
        events,
        lifecycle,
        start_tick=trigger_tick - pre_window,
        end_tick=trigger_tick + post_window,
        limit=20,
    )
    chunks = load_knowledge_chunks(knowledge_chunks_path)
    knowledge_query = build_knowledge_query(
        device_type=str(candidate.get("device_type")),
        performance_summary=performance_summary,
        event_summary=event_summary,
    )
    knowledge_results = search_knowledge_chunks(
        chunks,
        knowledge_query,
        top_k=5,
        exclude_topics={"normal_state_signature"},
        exclude_chunk_ids={"SIM_FAULT_NORMAL_001"},
    )
    payload = build_diagnosis_payload(
        run_id=str(run.get("run_id")),
        device_type=str(candidate.get("device_type")),
        entity_id=str(candidate.get("entity_id")),
        performance_event_summary=performance_summary,
        event_summary_for_diagnosis=event_summary,
        knowledge_query=knowledge_query,
        knowledge_results=knowledge_results,
        signature_matches=signature_matches,
    )
    payload["diagnostic_hints"] = global_hints
    diagnostic_hints = payload.get("diagnostic_hints") if isinstance(payload, dict) else {}
    if not isinstance(diagnostic_hints, dict):
        diagnostic_hints = {}
    observed_status = str(diagnostic_hints.get("observed_status") or "")
    rule_candidates = [
        item
        for item in diagnostic_hints.get("top_candidates", [])
        if isinstance(item, dict)
    ]
    rule_candidate_types = {str(item.get("fault_type") or "") for item in rule_candidates}
    global_top_type = str((rule_candidates[0] if rule_candidates else {}).get("fault_type") or "")
    signature_top_type = str((signature_causes[0] if signature_causes else {}).get("fault_type") or "")
    signature_scores = {
        str(item.get("fault_type") or ""): float(item.get("similarity") or 0.0)
        for item in signature_matches
        if isinstance(item, dict)
    }
    roadm_signature_margin = max(
        signature_scores.get("ROADM_WSS_FILTER_SHIFT", 0.0),
        signature_scores.get("ROADM_INBAND_CROSSTALK", 0.0),
    ) - signature_scores.get("EDFA_NOISE_SURGE", 0.0)
    signature_top_margin = (
        float(signature_matches[0].get("similarity") or 0.0)
        - float(signature_matches[1].get("similarity") or 0.0)
        if len(signature_matches) >= 2
        else 0.0
    )
    signature_override_allowed = allow_signature_override(
        global_top_type=global_top_type,
        global_top_source_strength=float((rule_candidates[0] if rule_candidates else {}).get("source_strength") or 0.0),
        signature_top_type=signature_top_type,
        signature_top_margin=signature_top_margin,
        rule_candidate_types=rule_candidate_types,
        roadm_signature_margin=roadm_signature_margin,
    )
    diagnosis = RuleDiagnosisAdapter().diagnose(payload)
    if signature_status == "NORMAL" and observed_status != "ABNORMAL":
        diagnosis = {
            "mode": "本地规则",
            "status": "NORMAL",
            "summary": "历史特征库最相似类别为 NORMAL_STATE，当前窗口未形成稳定故障证据链。",
            "top_causes": [],
            "recommendations": ["继续观察关键性能指标和业务生命周期事件。"],
            "knowledge_chunk_ids": [str(item.get("chunk_id")) for item in knowledge_results],
        }
    elif signature_causes and observed_status == "ABNORMAL" and signature_override_allowed:
        diagnosis["status"] = "ABNORMAL"
        diagnosis["top_causes"] = signature_causes
        diagnosis["summary"] = "结合历史特征库相似度、性能摘要和知识库内容生成候选根因。"
    diagnosis = calibrate_diagnosis_output(diagnosis, payload)
    if signature_causes and observed_status == "ABNORMAL" and signature_override_allowed:
        diagnosis["status"] = "ABNORMAL"
        diagnosis["top_causes"] = signature_causes
        diagnosis["summary"] = "结合当前性能摘要、知识库内容和历史特征库相似样本生成候选根因。"
    diagnosis.update(
        {
            "selected_device_type": candidate.get("device_type"),
            "selected_entity_id": candidate.get("entity_id"),
            "candidate_score": candidate.get("score"),
            "signature_matches": signature_matches,
            "knowledge_query": knowledge_query,
            "knowledge_results": knowledge_results,
            "diagnosis_payload": payload,
            "performance_event_summary": performance_summary,
            "event_summary_for_diagnosis": event_summary,
            "observability": {
                "direct_anomaly_observed": direct_telemetry_observable,
                "status": "OBSERVABLE" if direct_telemetry_observable else "NO_SIGNIFICANT_CHANGE",
                "evidence_source": "telemetry",
                "note": (
                    "三类设备扫描发现直接性能劣化证据。"
                    if direct_telemetry_observable
                    else "三类设备数据完整，但未形成直接性能劣化证据。"
                ),
            },
        }
    )
    return diagnosis
