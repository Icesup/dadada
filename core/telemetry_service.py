from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

from .io_utils import read_jsonl

DEVICE_FILE_MAP = {
    "edfa": "telemetry_edfa.jsonl",
    "fiber": "telemetry_fiber.jsonl",
    "roadm": "telemetry_roadm.jsonl",
}


def flatten_telemetry_record(record: dict[str, Any]) -> dict[str, Any]:
    """把 telemetry 的 metrics 字段展平为普通列，便于图表和统计。"""

    base = {
        "timestamp": record.get("timestamp"),
        "simulation_tick": record.get("simulation_tick"),
        "entity_id": record.get("entity_id"),
    }
    metrics = record.get("metrics") or {}
    if isinstance(metrics, dict):
        base.update(metrics)
    return base


@lru_cache(maxsize=48)
def _load_flattened_telemetry(
    file_path: str,
    modified_ns: int,
    file_size: int,
) -> tuple[tuple[dict[str, Any], ...], tuple[str, ...]]:
    """按文件版本缓存最近使用的 telemetry，避免一次诊断内重复解析。"""

    del modified_ns, file_size
    result = read_jsonl(Path(file_path))
    records = tuple(flatten_telemetry_record(record) for record in result.records)
    return records, tuple(result.errors)


def load_telemetry(run_dir: Path, device_type: str) -> tuple[list[dict[str, Any]], list[str]]:
    """读取某类设备 telemetry，并返回展平后的记录和错误列表。"""

    key = device_type.lower()
    if key not in DEVICE_FILE_MAP:
        raise ValueError(f"未知设备类型: {device_type}; 可选: {sorted(DEVICE_FILE_MAP)}")
    file_path = run_dir / DEVICE_FILE_MAP[key]
    try:
        stat = file_path.stat()
        modified_ns, file_size = stat.st_mtime_ns, stat.st_size
    except OSError:
        modified_ns, file_size = 0, 0
    records, errors = _load_flattened_telemetry(str(file_path.resolve()), modified_ns, file_size)
    return [dict(record) for record in records], list(errors)


def list_metric_fields(records: list[dict[str, Any]]) -> list[str]:
    """从展平 telemetry 中提取可绘制的数值指标字段。"""

    excluded = {"timestamp", "simulation_tick", "entity_id"}
    metrics: set[str] = set()
    for record in records:
        for key, value in record.items():
            if key in excluded:
                continue
            if isinstance(value, (int, float)):
                metrics.add(key)
    return sorted(metrics)


def summarize_metric_change(
    records: list[dict[str, Any]],
    *,
    entity_id: str,
    metric: str,
    trigger_tick: float,
    pre_window: float = 30.0,
    post_window: float = 30.0,
) -> dict[str, Any]:
    """用故障前后窗口计算单指标变化，作为第一版异常摘要基础。"""

    pre: list[float] = []
    post: list[float] = []
    for record in records:
        if record.get("entity_id") != entity_id:
            continue
        tick = record.get("simulation_tick")
        value = record.get(metric)
        if not isinstance(tick, (int, float)) or not isinstance(value, (int, float)):
            continue
        if trigger_tick - pre_window <= float(tick) < trigger_tick:
            pre.append(float(value))
        elif trigger_tick <= float(tick) <= trigger_tick + post_window:
            post.append(float(value))

    pre_mean = sum(pre) / len(pre) if pre else None
    post_mean = sum(post) / len(post) if post else None
    delta = post_mean - pre_mean if pre_mean is not None and post_mean is not None else None
    rel_delta = delta / abs(pre_mean) if delta is not None and pre_mean not in (None, 0) else None
    direction = "unknown"
    if delta is not None:
        direction = "increase" if delta > 0 else "decrease" if delta < 0 else "stable"
    return {
        "entity_id": entity_id,
        "metric": metric,
        "trigger_tick": trigger_tick,
        "pre_count": len(pre),
        "post_count": len(post),
        "pre_mean": pre_mean,
        "post_mean": post_mean,
        "delta": delta,
        "relative_delta": rel_delta,
        "direction": direction,
        "_pre_values": pre,
        "_post_values": post,
    }
