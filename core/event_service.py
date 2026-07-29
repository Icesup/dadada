from __future__ import annotations

from pathlib import Path
from typing import Any

from .io_utils import read_jsonl

FAULT_EVENT_TYPES = {
    "EDFA_GAIN_DEGRADATION",
    "EDFA_NOISE_SURGE",
    "EDFA_TILT_RIPPLE_ERROR",
    "FIBER_ATTENUATION_SURGE",
    "FIBER_NONLINEAR_ANOMALY",
    "FIBER_PMD_SURGE",
    "ROADM_INBAND_CROSSTALK",
    "ROADM_WSS_FILTER_SHIFT",
    "NORMAL_STATE",
}

INJECTION_DETAIL_KEYS = {
    "added_nf_db",
    "drop_db",
    "added_loss_db",
    "added_crosstalk_penalty_db",
    "power_drop_db",
    "added_osnr_penalty_db",
}


def load_simulation_events(run_dir: Path) -> tuple[list[dict[str, Any]], list[str]]:
    """读取结构化仿真事件。注意：这不是工业设备原生日志。"""

    result = read_jsonl(run_dir / "simulation_events.jsonl")
    return result.records, result.errors


def load_service_lifecycle(run_dir: Path) -> tuple[list[dict[str, Any]], list[str]]:
    """读取业务生命周期事件。"""

    result = read_jsonl(run_dir / "service_lifecycle.jsonl")
    return result.records, result.errors


def filter_events(
    events: list[dict[str, Any]],
    *,
    event_type: str | None = None,
    entity_id: str | None = None,
    start_tick: float | None = None,
    end_tick: float | None = None,
) -> list[dict[str, Any]]:
    """按事件类型、实体和时间窗口过滤事件。"""

    output: list[dict[str, Any]] = []
    for event in events:
        if event_type and event.get("event_type") != event_type:
            continue
        if entity_id and event.get("entity_id") != entity_id and event.get("service_id") != entity_id:
            continue
        tick = event.get("simulation_tick")
        if isinstance(tick, (int, float)):
            if start_tick is not None and float(tick) < start_tick:
                continue
            if end_tick is not None and float(tick) > end_tick:
                continue
        output.append(event)
    return output


def is_fault_injection_event(event: dict[str, Any]) -> bool:
    """判断事件是否包含故障注入答案，供诊断输入过滤使用。"""

    event_type = event.get("event_type")
    details = event.get("details") or {}
    if event.get("layer") == "L0":
        return True
    if isinstance(event_type, str) and event_type in FAULT_EVENT_TYPES:
        return True
    if isinstance(details, dict) and any(key in details for key in INJECTION_DETAIL_KEYS):
        return True
    return False


def filter_diagnosis_visible_events(
    events: list[dict[str, Any]],
    *,
    start_tick: float | None = None,
    end_tick: float | None = None,
) -> list[dict[str, Any]]:
    """过滤出可进入诊断输入的结构化仿真事件。"""

    visible: list[dict[str, Any]] = []
    for event in events:
        if is_fault_injection_event(event):
            continue
        tick = event.get("simulation_tick")
        if isinstance(tick, (int, float)):
            if start_tick is not None and float(tick) < start_tick:
                continue
            if end_tick is not None and float(tick) > end_tick:
                continue
        visible.append(event)
    return visible


def build_event_summary_for_diagnosis(
    simulation_events: list[dict[str, Any]],
    service_lifecycle: list[dict[str, Any]],
    *,
    start_tick: float,
    end_tick: float,
    limit: int = 20,
) -> dict[str, Any]:
    """生成诊断可见事件摘要，不包含故障注入事件。"""

    raw_window_events = filter_events(simulation_events, start_tick=start_tick, end_tick=end_tick)
    visible_sim_events = filter_diagnosis_visible_events(
        simulation_events,
        start_tick=start_tick,
        end_tick=end_tick,
    )
    visible_lifecycle = filter_events(service_lifecycle, start_tick=start_tick, end_tick=end_tick)
    combined: list[dict[str, Any]] = []
    for event in visible_sim_events:
        combined.append(
            {
                "source": "simulation_events",
                "simulation_tick": event.get("simulation_tick"),
                "event_type": event.get("event_type"),
                "entity_id": event.get("entity_id"),
                "layer": event.get("layer"),
                "details": event.get("details") if isinstance(event.get("details"), dict) else {},
            }
        )
    for event in visible_lifecycle:
        cause = event.get("cause")
        if isinstance(cause, str) and cause in FAULT_EVENT_TYPES:
            cause = None
        combined.append(
            {
                "source": "service_lifecycle",
                "simulation_tick": event.get("simulation_tick"),
                "event_type": event.get("event_type"),
                "service_id": event.get("service_id"),
                "priority": event.get("priority"),
                "cause": cause,
            }
        )
    combined.sort(key=lambda item: float(item.get("simulation_tick") or 0.0))
    return {
        "window": {"start_tick": start_tick, "end_tick": end_tick},
        "raw_simulation_event_count": len(raw_window_events),
        "diagnosis_visible_simulation_event_count": len(visible_sim_events),
        "filtered_injection_event_count": len(raw_window_events) - len(visible_sim_events),
        "service_lifecycle_event_count": len(visible_lifecycle),
        "events": combined[:limit],
    }
