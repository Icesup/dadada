from __future__ import annotations

import re
from collections import defaultdict
from hashlib import sha1
from pathlib import Path
from typing import Any

from .anomaly_service import (
    abnormal_evidence_score,
    is_harmful_change,
    select_key_metric_changes,
)
from .event_service import load_service_lifecycle, load_simulation_events
from .experiment_service import get_run_dir
from .io_utils import write_json
from .telemetry_service import load_telemetry


DEVICE_TYPES = ("edfa", "fiber", "roadm")
TERMINAL_SERVICE_EVENTS = {"RELEASED", "FAILED", "BLOCKED", "TEARDOWN"}
MONITORING_STEP = 5.0
BASELINE_WINDOW = 30.0
RECENT_WINDOW = 8.0
WARMUP_TICK = 60.0
MIN_EVIDENCE_SCORE = 5.0
MIN_HARMFUL_METRICS = 2


def load_replay_bundle(run: dict[str, Any]) -> dict[str, Any]:
    """按需读取一个 episode，作为回放数据源，不读取 ground truth。"""

    run_dir = get_run_dir(run)
    telemetry: dict[str, list[dict[str, Any]]] = {}
    errors: list[str] = []
    for device_type in DEVICE_TYPES:
        rows, row_errors = load_telemetry(run_dir, device_type)
        telemetry[device_type] = rows
        errors.extend(row_errors)
    simulation_events, simulation_errors = load_simulation_events(run_dir)
    lifecycle, lifecycle_errors = load_service_lifecycle(run_dir)
    errors.extend(simulation_errors)
    errors.extend(lifecycle_errors)
    return {
        "run_id": str(run.get("run_id") or ""),
        "run_dir": str(run_dir),
        "telemetry": telemetry,
        "simulation_events": simulation_events,
        "service_lifecycle": lifecycle,
        "timeline": build_replay_timeline(telemetry),
        "errors": errors,
    }


def build_replay_timeline(telemetry: dict[str, list[dict[str, Any]]]) -> list[float]:
    """生成固定监测节拍；tick来自数据范围，不伪造中间遥测值。"""

    ticks = [
        float(row["simulation_tick"])
        for rows in telemetry.values()
        for row in rows
        if isinstance(row.get("simulation_tick"), (int, float))
    ]
    if not ticks:
        return [0.0]
    start = max(0.0, min(ticks))
    end = max(ticks)
    current = float(int(start // MONITORING_STEP) * MONITORING_STEP)
    output: list[float] = []
    while current <= end:
        output.append(round(current, 3))
        current += MONITORING_STEP
    if output[-1] < end:
        output.append(round(end, 3))
    return output


def _route_key(entity_id: str) -> str:
    match = re.search(r"\(([^()]*)\)", entity_id)
    if match:
        value = re.sub(r"\s+", "", match.group(1))
        return value.replace("Ўъ", "→")
    if entity_id.lower().startswith("roadm "):
        return entity_id.strip()
    return entity_id.strip()


def _metric_summaries(
    rows: list[dict[str, Any]],
    *,
    current_tick: float,
) -> list[dict[str, Any]]:
    baseline = [
        row
        for row in rows
        if current_tick - BASELINE_WINDOW
        <= float(row.get("simulation_tick") or 0.0)
        < current_tick - RECENT_WINDOW
    ]
    recent = [
        row
        for row in rows
        if current_tick - RECENT_WINDOW
        <= float(row.get("simulation_tick") or 0.0)
        <= current_tick
    ]
    if len(baseline) < 2 or len(recent) < 2:
        return []
    excluded = {"simulation_tick", "timestamp", "entity_id"}
    metrics = set().union(*(set(row) - excluded for row in rows))
    summaries: list[dict[str, Any]] = []
    for metric in metrics:
        pre_values = [
            float(row[metric])
            for row in baseline
            if isinstance(row.get(metric), (int, float))
        ]
        post_values = [
            float(row[metric])
            for row in recent
            if isinstance(row.get(metric), (int, float))
        ]
        if len(pre_values) < 2 or len(post_values) < 2:
            continue
        pre_mean = sum(pre_values) / len(pre_values)
        post_mean = sum(post_values) / len(post_values)
        delta = post_mean - pre_mean
        summaries.append(
            {
                "metric": metric,
                "pre_count": len(pre_values),
                "post_count": len(post_values),
                "pre_mean": pre_mean,
                "post_mean": post_mean,
                "delta": delta,
                "relative_delta": delta / abs(pre_mean) if pre_mean else None,
                "direction": "increase" if delta > 0 else "decrease" if delta < 0 else "stable",
                "_post_values": post_values,
            }
        )
    return summaries


def scan_telemetry(
    bundle: dict[str, Any],
    *,
    current_tick: float,
) -> dict[str, Any]:
    """仅使用 current_tick 以前的数据扫描异常，不读取注入事件或标注。"""

    observations: list[dict[str, Any]] = []
    monitored_entities: set[str] = set()
    telemetry = bundle.get("telemetry") or {}
    for device_type in DEVICE_TYPES:
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in telemetry.get(device_type, []):
            tick = row.get("simulation_tick")
            if not isinstance(tick, (int, float)) or float(tick) > current_tick:
                continue
            entity_id = str(row.get("entity_id") or "")
            if entity_id:
                monitored_entities.add(entity_id)
                grouped[entity_id].append(row)
        for entity_id, rows in grouped.items():
            summaries = _metric_summaries(rows, current_tick=current_tick)
            score = abnormal_evidence_score(summaries)
            harmful_count = sum(1 for item in summaries if is_harmful_change(item))
            if (
                current_tick < WARMUP_TICK
                or score < MIN_EVIDENCE_SCORE
                or harmful_count < MIN_HARMFUL_METRICS
            ):
                continue
            key_changes = []
            for item in select_key_metric_changes(summaries, limit=5):
                if not is_harmful_change(item):
                    continue
                key_changes.append(
                    {
                        key: value
                        for key, value in item.items()
                        if key != "_post_values"
                    }
                )
            observations.append(
                {
                    "device_type": device_type,
                    "entity_id": entity_id,
                    "route_key": _route_key(entity_id),
                    "evidence_score": round(float(score), 4),
                    "harmful_metric_count": harmful_count,
                    "key_metric_changes": key_changes,
                }
            )
    observations.sort(key=lambda item: float(item["evidence_score"]), reverse=True)
    return {
        "current_tick": current_tick,
        "monitored_entity_count": len(monitored_entities),
        "observations": observations,
        "candidate_paths": sorted({str(item["route_key"]) for item in observations}),
    }


def active_services_at_tick(
    lifecycle: list[dict[str, Any]],
    current_tick: float,
) -> list[dict[str, Any]]:
    """根据业务生命周期事件还原当前活动业务，不读取故障答案。"""

    latest: dict[str, dict[str, Any]] = {}
    ordered = sorted(
        lifecycle,
        key=lambda item: float(item.get("simulation_tick") or 0.0),
    )
    for event in ordered:
        tick = event.get("simulation_tick")
        service_id = str(event.get("service_id") or "")
        if not service_id or not isinstance(tick, (int, float)) or float(tick) > current_tick:
            continue
        latest[service_id] = event
    active = [
        event
        for event in latest.values()
        if str(event.get("event_type") or "").upper() not in TERMINAL_SERVICE_EVENTS
    ]
    return sorted(active, key=lambda item: str(item.get("service_id") or ""))


def _service_path_text(service: dict[str, Any]) -> str:
    details = service.get("routing_details")
    if not isinstance(details, list):
        return ""
    return " ".join(
        str(item.get("physical_path_details") or "")
        for item in details
        if isinstance(item, dict)
    )


def affected_services_for_path(
    active_services: list[dict[str, Any]],
    route_key: str,
) -> list[dict[str, Any]]:
    compact_key = re.sub(r"\s+", "", route_key).replace("Ўъ", "→")
    endpoints = [part for part in compact_key.split("→") if part]
    affected: list[dict[str, Any]] = []
    for service in active_services:
        path = re.sub(r"\s+", "", _service_path_text(service)).replace("Ўъ", "→")
        if compact_key and compact_key in path:
            affected.append(service)
            continue
        if len(endpoints) >= 2 and all(endpoint in path for endpoint in endpoints[:2]):
            affected.append(service)
    return affected


def should_create_incident(
    current_paths: list[str],
    previous_paths: list[str],
) -> str | None:
    """同一路径连续两个监测周期异常才聚合成事件，抑制业务调度瞬态。"""

    repeated = sorted(set(current_paths).intersection(previous_paths))
    return repeated[0] if repeated else None


def build_replay_incident(
    *,
    run_id: str,
    current_tick: float,
    route_key: str,
    observations: list[dict[str, Any]],
    active_services: list[dict[str, Any]],
) -> dict[str, Any]:
    """把同一路径的多设备、多指标异常聚合为一条运维事件。"""

    matched = [item for item in observations if item.get("route_key") == route_key]
    matched.sort(key=lambda item: float(item.get("evidence_score") or 0.0), reverse=True)
    affected = affected_services_for_path(active_services, route_key)
    changes: list[dict[str, Any]] = []
    for observation in matched:
        for change in observation.get("key_metric_changes", []):
            if not isinstance(change, dict):
                continue
            changes.append(
                {
                    "entity_id": observation.get("entity_id"),
                    "device_type": observation.get("device_type"),
                    **change,
                }
            )
    changes.sort(key=lambda item: abs(float(item.get("delta") or 0.0)), reverse=True)
    peak_score = max((float(item.get("evidence_score") or 0.0) for item in matched), default=0.0)
    digest = sha1(f"{run_id}:{route_key}".encode("utf-8")).hexdigest()[:8].upper()
    return {
        "incident_id": f"INC-{digest}",
        "run_id": run_id,
        "first_abnormal_tick": max(0.0, current_tick - MONITORING_STEP),
        "detected_tick": current_tick,
        "severity": "CRITICAL" if peak_score >= 8.0 or affected else "MAJOR",
        "status": "UNDER_ANALYSIS",
        "abnormal_entities": [str(item.get("entity_id") or "") for item in matched],
        "affected_path": [part for part in route_key.split("→") if part] or [route_key],
        "affected_services": [
            {
                "service_id": str(item.get("service_id") or ""),
                "priority": str(item.get("priority") or ""),
            }
            for item in affected
        ],
        "key_metric_changes": changes[:5],
        "current_stage": "EVENT_CORRELATION",
        "primary_suspected_cause": "",
        "diagnosis": None,
        "actions": [],
    }


ACTION_TEMPLATES = {
    "EDFA_NOISE_SURGE": {
        "action_name": "恢复EDFA噪声参数",
        "parameters": {"nf_target": "baseline"},
        "expected_effect": "改善下游OSNR和GSNR",
        "risk": "LOW",
    },
    "EDFA_GAIN_DEGRADATION": {
        "action_name": "恢复EDFA目标增益",
        "parameters": {"gain_target": "baseline"},
        "expected_effect": "恢复输出功率并改善下游信号质量",
        "risk": "MEDIUM",
    },
    "ROADM_WSS_FILTER_SHIFT": {
        "action_name": "复核并恢复WSS中心频率",
        "parameters": {"center_frequency": "baseline"},
        "expected_effect": "恢复通道滤波对准和业务信号质量",
        "risk": "MEDIUM",
    },
    "ROADM_INBAND_CROSSTALK": {
        "action_name": "复核ROADM端口隔离与串扰配置",
        "parameters": {"crosstalk_penalty": "baseline"},
        "expected_effect": "降低带内串扰并改善OSNR",
        "risk": "MEDIUM",
    },
}


def build_structured_actions(
    incident: dict[str, Any],
    diagnosis: dict[str, Any],
) -> list[dict[str, Any]]:
    """将诊断 Top-N 转换为结构化处置建议，不执行真实设备下发。"""

    actions: list[dict[str, Any]] = []
    for index, cause in enumerate((diagnosis.get("top_causes") or [])[:3], 1):
        if not isinstance(cause, dict):
            continue
        fault_type = str(cause.get("fault_type") or "")
        template = ACTION_TEMPLATES.get(
            fault_type,
            {
                "action_name": "复核候选设备参数与链路状态",
                "parameters": {"target": "baseline"},
                "expected_effect": "验证候选根因与业务影响是否一致",
                "risk": "MEDIUM",
            },
        )
        actions.append(
            {
                "action_id": f"ACT-{index:03d}",
                "incident_id": incident.get("incident_id"),
                "action_name": template["action_name"],
                "target_entity": str(cause.get("entity_id") or ""),
                "parameters": template["parameters"],
                "priority": "HIGH" if index == 1 else "MEDIUM",
                "expected_effect": template["expected_effect"],
                "risk": template["risk"],
                "status": "SUGGESTED",
                "candidate_fault": fault_type,
            }
        )
    return actions


def attach_diagnosis(
    incident: dict[str, Any],
    diagnosis: dict[str, Any],
) -> dict[str, Any]:
    """把现有诊断输出挂接到事件，并生成结构化处置建议。"""

    updated = dict(incident)
    top_causes = diagnosis.get("top_causes") or []
    top = top_causes[0] if top_causes and isinstance(top_causes[0], dict) else {}
    updated["diagnosis"] = diagnosis
    updated["primary_suspected_cause"] = str(top.get("fault_type") or "")
    updated["actions"] = build_structured_actions(updated, diagnosis)
    updated["status"] = "RECOMMENDATION_READY"
    updated["current_stage"] = "RECOMMENDATION_READY"
    return updated


def build_validation_task(
    incident: dict[str, Any],
    action: dict[str, Any],
) -> dict[str, Any]:
    """生成等待外部仿真引擎执行的任务，不伪造恢复结果。"""

    return {
        "validation_task_id": f"VAL-{incident.get('incident_id', 'UNKNOWN')}",
        "incident_id": incident.get("incident_id"),
        "run_id": incident.get("run_id"),
        "status": "WAITING_SIMULATION_ENGINE",
        "adapter": "SimulationAdapter",
        "action": action,
        "result": None,
        "note": "当前未接入在线GNPy；任务已生成，等待外部仿真引擎执行。",
    }


def write_validation_task(
    incident: dict[str, Any],
    action: dict[str, Any],
    output_path: Path,
) -> dict[str, Any]:
    task = build_validation_task(incident, action)
    write_json(output_path, task)
    return task
