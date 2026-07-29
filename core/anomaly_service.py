from __future__ import annotations

from typing import Any


DB_METRIC_THRESHOLDS = {
    "actual_gain_db": 1.0,
    "output_power_dbm": 1.0,
    "output_osnr_db": 1.0,
    "output_gsnr_db": 1.0,
    "current_osnr_db": 1.0,
    "current_gsnr_db": 1.0,
    "power_ripple_db": 0.5,
    "accumulated_ase_dbm": 1.0,
    "accumulated_nli_dbm": 1.0,
}

ABS_METRIC_THRESHOLDS = {
    "nf_db": 1.0,
    "pmd_ps": 0.1,
    "cd_ps_nm": 1.0,
    "fiber_length_km": 0.01,
    "power_variance": 0.05,
}

HARMFUL_DIRECTIONS = {
    "actual_gain_db": "decrease",
    "output_power_dbm": "decrease",
    "output_osnr_db": "decrease",
    "output_gsnr_db": "decrease",
    "current_osnr_db": "decrease",
    "current_gsnr_db": "decrease",
    "nf_db": "increase",
    "power_ripple_db": "increase",
    "power_variance": "increase",
    "accumulated_ase_dbm": "increase",
    "accumulated_nli_dbm": "increase",
    "pmd_ps": "increase",
}

QUALITY_METRICS = {
    "output_osnr_db",
    "output_gsnr_db",
    "current_osnr_db",
    "current_gsnr_db",
}


def metric_change_score(summary: dict[str, Any]) -> float:
    """把单个指标变化转换为可排序分数，不直接混用不同量纲的绝对值。"""

    metric = str(summary.get("metric") or "")
    delta = summary.get("delta")
    relative_delta = summary.get("relative_delta")
    if not isinstance(delta, (int, float)):
        return 0.0
    threshold = DB_METRIC_THRESHOLDS.get(metric) or ABS_METRIC_THRESHOLDS.get(metric)
    if threshold and threshold > 0:
        return abs(float(delta)) / threshold
    if isinstance(relative_delta, (int, float)):
        return abs(float(relative_delta))
    return abs(float(delta))


def post_persistence_ratio(summary: dict[str, Any]) -> float | None:
    """计算后窗口内同向越过阈值的样本比例。"""

    metric = str(summary.get("metric") or "")
    pre_mean = summary.get("pre_mean")
    post_values = summary.get("_post_values")
    direction = summary.get("direction")
    threshold = DB_METRIC_THRESHOLDS.get(metric) or ABS_METRIC_THRESHOLDS.get(metric)
    if not isinstance(pre_mean, (int, float)) or not isinstance(post_values, list) or not post_values or not threshold:
        return None
    if direction == "decrease":
        matched = [value for value in post_values if isinstance(value, (int, float)) and value <= float(pre_mean) - threshold]
    elif direction == "increase":
        matched = [value for value in post_values if isinstance(value, (int, float)) and value >= float(pre_mean) + threshold]
    else:
        return 0.0
    return len(matched) / len(post_values)


def is_significant_change(summary: dict[str, Any]) -> bool:
    """判断指标变化是否足以进入性能事件摘要。"""

    if int(summary.get("pre_count") or 0) == 0 or int(summary.get("post_count") or 0) == 0:
        return False
    if metric_change_score(summary) < 1.0:
        return False
    ratio = post_persistence_ratio(summary)
    return True if ratio is None else ratio >= 0.6


def is_harmful_change(summary: dict[str, Any]) -> bool:
    """判断变化方向是否符合故障劣化方向。"""

    metric = str(summary.get("metric") or "")
    expected = HARMFUL_DIRECTIONS.get(metric)
    if not expected:
        return False
    return is_significant_change(summary) and summary.get("direction") == expected


def abnormal_evidence_score(summaries: list[dict[str, Any]]) -> float:
    """按证据链强度给实体打分，降低正常业务调度导致的单质量指标误报。"""

    harmful = [item for item in summaries if is_harmful_change(item)]
    if not harmful:
        return 0.0
    direct_scores = [
        metric_change_score(item)
        for item in harmful
        if str(item.get("metric") or "") not in QUALITY_METRICS
    ]
    quality_scores = [
        metric_change_score(item)
        for item in harmful
        if str(item.get("metric") or "") in QUALITY_METRICS
    ]
    best_direct = max(direct_scores, default=0.0)
    best_quality = max(quality_scores, default=0.0)
    if best_direct > 0:
        return max(best_direct, best_quality)
    if len(quality_scores) >= 2:
        return best_quality
    if best_quality >= 3.0:
        return best_quality
    return 0.0


def select_key_metric_changes(summaries: list[dict[str, Any]], limit: int = 5) -> list[dict[str, Any]]:
    """按归一化变化强度选择关键指标变化。"""

    ranked = sorted(summaries, key=metric_change_score, reverse=True)
    return [item for item in ranked if metric_change_score(item) > 0][:limit]


def build_performance_event_summary(
    *,
    run_id: str,
    device_type: str,
    entity_id: str,
    trigger_tick: float,
    metric_summaries: list[dict[str, Any]],
) -> dict[str, Any]:
    """生成可供诊断流程读取的性能事件摘要，不包含 ground truth 答案字段。"""

    key_changes = []
    for summary in select_key_metric_changes(metric_summaries):
        key_changes.append(
            {
                "entity_id": entity_id,
                "device_type": device_type,
                "metric": summary.get("metric"),
                "trigger_tick": trigger_tick,
                "pre_mean": summary.get("pre_mean"),
                "post_mean": summary.get("post_mean"),
                "delta": summary.get("delta"),
                "relative_delta": summary.get("relative_delta"),
                "direction": summary.get("direction"),
                "pre_count": summary.get("pre_count"),
                "post_count": summary.get("post_count"),
                "significant": is_significant_change(summary),
                "harmful": is_harmful_change(summary),
                "score": round(metric_change_score(summary), 4),
                "post_persistence_ratio": post_persistence_ratio(summary),
            }
        )
    status = "ABNORMAL" if abnormal_evidence_score(metric_summaries) > 0 else "NORMAL"
    return {
        "run_id": run_id,
        "status": status,
        "device_type": device_type,
        "entity_id": entity_id,
        "trigger_tick": trigger_tick,
        "key_metric_changes": key_changes,
    }
