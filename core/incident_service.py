from __future__ import annotations

from copy import deepcopy
from hashlib import sha1
from typing import Any


DEVICE_LABELS = {"edfa": "EDFA", "fiber": "Fiber", "roadm": "ROADM"}


def _incident_id(run_id: str) -> str:
    """根据运行任务生成稳定且不包含答案信息的事件编号。"""

    digest = sha1(run_id.encode("utf-8")).hexdigest()[:8].upper()
    return f"INC-{digest}"


def _severity(score: float) -> str:
    """把异常证据分值映射为运维告警级别。"""

    if score >= 8.0:
        return "严重"
    if score >= 3.0:
        return "较高"
    if score > 0.0:
        return "一般"
    return "正常"


def build_active_alerts(observations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """从全实验设备域观测中生成活动告警，不读取故障标注。"""

    alerts: list[dict[str, Any]] = []
    for observation in observations:
        score = float(observation.get("candidate_score") or 0.0)
        if score <= 0.0:
            continue
        changes = [item for item in observation.get("key_changes", []) if isinstance(item, dict)]
        strongest = next((item for item in changes if item.get("delta") is not None), {})
        alerts.append(
            {
                "device_type": str(observation.get("device_type") or "unknown"),
                "device_label": DEVICE_LABELS.get(
                    str(observation.get("device_type") or ""),
                    str(observation.get("device_type") or "未知设备"),
                ),
                "entity_id": str(observation.get("candidate_entity_id") or ""),
                "metric": str(strongest.get("metric") or ""),
                "direction": str(strongest.get("direction") or "unknown"),
                "delta": strongest.get("delta"),
                "evidence_score": score,
                "severity": _severity(score),
                "status": "待确认",
            }
        )
    return sorted(alerts, key=lambda item: float(item["evidence_score"]), reverse=True)


def build_incident_snapshot(
    *,
    run_id: str,
    observations: list[dict[str, Any]],
    reference_tick: float | None = None,
) -> dict[str, Any]:
    """构造诊断前运维事件快照，不写入故障类型、故障实体或标注答案。"""

    alerts = build_active_alerts(observations)
    primary = alerts[0] if alerts else {}
    if not alerts:
        return {
            "incident_id": "",
            "run_id": run_id,
            "status": "持续监测",
            "severity": "正常",
            "title": "当前未形成活动告警",
            "reference_tick": reference_tick,
            "alarm_count": 0,
            "primary_device_type": "",
            "primary_entity_id": "",
            "alerts": [],
        }
    return {
        "incident_id": _incident_id(run_id),
        "run_id": run_id,
        "status": "待诊断",
        "severity": str(primary.get("severity") or "一般"),
        "title": "光性能异常事件",
        "reference_tick": reference_tick,
        "alarm_count": len(alerts),
        "primary_device_type": str(primary.get("device_type") or ""),
        "primary_entity_id": str(primary.get("entity_id") or ""),
        "alerts": alerts,
    }


def update_incident_with_diagnosis(
    incident: dict[str, Any] | None,
    diagnosis: dict[str, Any],
) -> dict[str, Any]:
    """根据诊断结果推进运维事件状态，不写入评估标注。"""

    updated = deepcopy(incident or {})
    status = str(diagnosis.get("status") or "")
    if status == "FAILED":
        updated["status"] = "诊断失败"
    elif status == "ABNORMAL":
        updated["status"] = "待处置"
    elif status == "NORMAL":
        updated["status"] = "持续监测"
        if not updated.get("alerts"):
            updated["severity"] = "正常"
    updated["diagnosis_status"] = status
    updated["candidate_count"] = len(diagnosis.get("top_causes") or [])
    return updated
