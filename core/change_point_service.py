from __future__ import annotations

from collections import defaultdict
from statistics import median
from typing import Any

from .anomaly_service import (
    ABS_METRIC_THRESHOLDS,
    DB_METRIC_THRESHOLDS,
    HARMFUL_DIRECTIONS,
    QUALITY_METRICS,
)
from .experiment_service import get_run_dir
from .telemetry_service import list_metric_fields, load_telemetry


MIN_SERIES_POINTS = 8
MIN_PRE_POINTS = 4
LOCAL_WINDOW_POINTS = 6
MIN_PERSISTENCE = 0.6
MIN_CHANGE_SCORE = 3.5
CHANGE_CLUSTER_TOLERANCE = 5.0


def _metric_threshold(metric: str) -> float | None:
    """返回指标的可观测变化阈值。"""

    return DB_METRIC_THRESHOLDS.get(metric) or ABS_METRIC_THRESHOLDS.get(metric)


def _persistent_candidates(
    records: list[dict[str, Any]],
    *,
    device_type: str,
) -> list[dict[str, Any]]:
    """提取实体指标中持续存在的有害变化点候选。"""

    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        entity_id = record.get("entity_id")
        tick = record.get("simulation_tick")
        if entity_id and isinstance(tick, (int, float)):
            grouped[str(entity_id)].append(record)

    candidates: list[dict[str, Any]] = []
    for entity_id, entity_records in grouped.items():
        entity_records.sort(key=lambda item: float(item["simulation_tick"]))
        for metric in list_metric_fields(entity_records):
            threshold = _metric_threshold(metric)
            harmful_direction = HARMFUL_DIRECTIONS.get(metric)
            if not threshold or not harmful_direction:
                continue
            series = [
                (float(item["simulation_tick"]), float(item[metric]))
                for item in entity_records
                if isinstance(item.get(metric), (int, float))
            ]
            if len(series) < MIN_SERIES_POINTS:
                continue
            for index in range(MIN_PRE_POINTS, len(series) - 3):
                pre_values = [value for _, value in series[max(0, index - LOCAL_WINDOW_POINTS) : index]]
                local_post = [value for _, value in series[index : index + LOCAL_WINDOW_POINTS]]
                remaining_post = [value for _, value in series[index:]]
                baseline = median(pre_values)
                post_level = median(local_post)
                delta = post_level - baseline
                harmful = (
                    harmful_direction == "increase" and delta >= threshold
                ) or (
                    harmful_direction == "decrease" and delta <= -threshold
                )
                if not harmful:
                    continue

                def crosses_threshold(value: float) -> bool:
                    if harmful_direction == "increase":
                        return value >= baseline + threshold
                    return value <= baseline - threshold

                local_ratio = sum(crosses_threshold(value) for value in local_post) / len(local_post)
                persistence_ratio = sum(crosses_threshold(value) for value in remaining_post) / len(remaining_post)
                if local_ratio < 0.67 or persistence_ratio < MIN_PERSISTENCE:
                    continue
                score = abs(delta) / threshold * local_ratio * persistence_ratio
                if score < MIN_CHANGE_SCORE:
                    continue
                detected_tick = next(
                    (
                        tick
                        for tick, value in series[index : index + LOCAL_WINDOW_POINTS]
                        if crosses_threshold(value)
                    ),
                    series[index][0],
                )
                candidates.append(
                    {
                        "detected_tick": detected_tick,
                        "device_type": device_type,
                        "entity_id": entity_id,
                        "metric": metric,
                        "direction": harmful_direction,
                        "baseline": baseline,
                        "post_level": post_level,
                        "delta": delta,
                        "score": score,
                        "persistence_ratio": persistence_ratio,
                    }
                )
    return candidates


def _quality_family(metric: str) -> str:
    if "osnr" in metric:
        return "osnr"
    if "gsnr" in metric:
        return "gsnr"
    return metric


def _supported_candidates(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """过滤容易由业务调度产生的单点变化，保留跨实体或跨域一致变化。"""

    supported: list[dict[str, Any]] = []
    for candidate in candidates:
        metric = str(candidate.get("metric") or "")
        tick = float(candidate.get("detected_tick") or 0.0)
        nearby = [
            item
            for item in candidates
            if abs(float(item.get("detected_tick") or 0.0) - tick) <= CHANGE_CLUSTER_TOLERANCE
        ]
        if metric == "output_power_dbm":
            entities = {
                (str(item.get("device_type") or ""), str(item.get("entity_id") or ""))
                for item in nearby
                if item.get("metric") == metric
            }
            if len(entities) < 2:
                continue
        elif metric in QUALITY_METRICS:
            family = _quality_family(metric)
            family_nearby = [
                item
                for item in nearby
                if str(item.get("metric") or "") in QUALITY_METRICS
                and _quality_family(str(item.get("metric") or "")) == family
            ]
            device_types = {str(item.get("device_type") or "") for item in family_nearby}
            entities = {str(item.get("entity_id") or "") for item in family_nearby}
            if len(device_types) < 2 or len(entities) < 2:
                continue
        supported.append(candidate)
    return supported


def detect_run_change_point(run: dict[str, Any]) -> dict[str, Any]:
    """仅根据三类telemetry自动估计异常时刻，不读取故障标注字段。"""

    run_dir = get_run_dir(run)
    candidates: list[dict[str, Any]] = []
    all_ticks: list[float] = []
    read_errors: list[str] = []
    for device_type in ("edfa", "fiber", "roadm"):
        records, errors = load_telemetry(run_dir, device_type)
        read_errors.extend(errors[:3])
        all_ticks.extend(
            float(item["simulation_tick"])
            for item in records
            if isinstance(item.get("simulation_tick"), (int, float))
        )
        candidates.extend(_persistent_candidates(records, device_type=device_type))

    candidates = _supported_candidates(candidates)
    best = min(
        candidates,
        key=lambda item: (
            float(item.get("detected_tick") or 0.0),
            -float(item.get("score") or 0.0),
        ),
        default=None,
    )
    minimum_tick = min(all_ticks, default=0.0)
    maximum_tick = max(all_ticks, default=0.0)
    neutral_tick = (minimum_tick + maximum_tick) / 2 if all_ticks else 0.0
    if best:
        return {
            "status": "DETECTED",
            "anomaly_detected": True,
            "analysis_tick": float(best["detected_tick"]),
            "detected_tick": float(best["detected_tick"]),
            "device_type": best["device_type"],
            "entity_id": best["entity_id"],
            "metric": best["metric"],
            "direction": best["direction"],
            "delta": best["delta"],
            "score": round(float(best["score"]), 4),
            "persistence_ratio": round(float(best["persistence_ratio"]), 4),
            "observation_range": [minimum_tick, maximum_tick],
            "candidate_count": len(candidates),
            "read_errors": read_errors,
            "method": "persistent_telemetry_change",
        }
    return {
        "status": "STABLE",
        "anomaly_detected": False,
        "analysis_tick": neutral_tick,
        "detected_tick": None,
        "device_type": "",
        "entity_id": "",
        "metric": "",
        "direction": "stable",
        "delta": None,
        "score": 0.0,
        "persistence_ratio": None,
        "observation_range": [minimum_tick, maximum_tick],
        "candidate_count": 0,
        "read_errors": read_errors,
        "method": "persistent_telemetry_change",
    }
