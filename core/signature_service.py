from __future__ import annotations

import math
from collections import defaultdict
from pathlib import Path
from typing import Any

from .anomaly_service import metric_change_score
from .analysis_pipeline import DEVICE_TYPES, select_metrics_for_device, summarize_entity
from .experiment_service import get_run_dir
from .io_utils import read_json, write_json
from .telemetry_service import load_telemetry


def _cap(value: float, limit: float = 20.0) -> float:
    return max(0.0, min(abs(value), limit))


def extract_run_signature_features(
    run: dict[str, Any],
    *,
    pre_window: float = 30.0,
    post_window: float = 30.0,
    analysis_tick: float | None = None,
) -> dict[str, float]:
    """从单个 episode 提取诊断特征，不读取故障类型作为输入。"""

    run_dir = get_run_dir(run)
    trigger_tick = float(analysis_tick if analysis_tick is not None else run.get("trigger_tick") or 0.0)
    features: dict[str, float] = {}
    for device_type in DEVICE_TYPES:
        records, _ = load_telemetry(run_dir, device_type)
        metrics = select_metrics_for_device(device_type, records)
        grouped_records: dict[str, list[dict[str, Any]]] = {}
        for record in records:
            entity_id = record.get("entity_id")
            if entity_id:
                grouped_records.setdefault(str(entity_id), []).append(record)
        device_best = 0.0
        for entity_id in sorted(grouped_records):
            summaries = summarize_entity(
                grouped_records[entity_id],
                entity_id=entity_id,
                metrics=metrics,
                trigger_tick=trigger_tick,
                pre_window=pre_window,
                post_window=post_window,
            )
            for summary in summaries:
                metric = str(summary.get("metric") or "")
                direction = str(summary.get("direction") or "")
                if direction not in {"increase", "decrease"}:
                    continue
                score = _cap(metric_change_score(summary))
                if score <= 0:
                    continue
                key = f"{device_type}.{metric}.{direction}"
                features[key] = max(features.get(key, 0.0), score)
                device_best = max(device_best, score)
        features[f"{device_type}.best_change"] = device_best
    return features


def _mean_feature_vector(vectors: list[dict[str, float]]) -> dict[str, float]:
    totals: dict[str, float] = defaultdict(float)
    keys: set[str] = set()
    for vector in vectors:
        keys.update(vector.keys())
        for key, value in vector.items():
            totals[key] += float(value)
    count = max(len(vectors), 1)
    return {key: totals[key] / count for key in sorted(keys)}


def build_signature_library(
    runs: list[dict[str, Any]],
    *,
    pre_window: float = 30.0,
    post_window: float = 30.0,
) -> dict[str, Any]:
    """根据历史标注 episode 生成故障特征库。"""

    grouped: dict[str, list[dict[str, float]]] = defaultdict(list)
    run_ids: dict[str, list[str]] = defaultdict(list)
    errors: list[dict[str, str]] = []
    for run in runs:
        scenario = str(run.get("scenario") or "UNKNOWN")
        try:
            grouped[scenario].append(
                extract_run_signature_features(run, pre_window=pre_window, post_window=post_window)
            )
            run_ids[scenario].append(str(run.get("run_id")))
        except Exception as exc:  # noqa: BLE001
            errors.append({"run_id": str(run.get("run_id")), "error": str(exc)})

    signatures = {
        scenario: {
            "count": len(vectors),
            "centroid": _mean_feature_vector(vectors),
            "samples": [
                {"run_id": run_id, "features": vector}
                for run_id, vector in zip(run_ids[scenario], vectors)
            ],
            "train_run_ids": run_ids[scenario],
        }
        for scenario, vectors in sorted(grouped.items())
        if vectors
    }
    return {
        "version": "fault_signature_library_v2",
        "pre_window": pre_window,
        "post_window": post_window,
        "signatures": signatures,
        "errors": errors,
    }


def _cosine_similarity(a: dict[str, float], b: dict[str, float]) -> float:
    keys = set(a) | set(b)
    dot = sum(a.get(key, 0.0) * b.get(key, 0.0) for key in keys)
    norm_a = math.sqrt(sum(a.get(key, 0.0) ** 2 for key in keys))
    norm_b = math.sqrt(sum(b.get(key, 0.0) ** 2 for key in keys))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


def classify_with_signature_library(
    features: dict[str, float],
    library: dict[str, Any],
    *,
    top_n: int = 3,
    exclude_run_id: str | None = None,
) -> list[dict[str, Any]]:
    """用故障特征库返回 Top-N 相似故障类型，并排除当前测试样本。"""

    results: list[dict[str, Any]] = []
    signatures = library.get("signatures") if isinstance(library, dict) else {}
    if not isinstance(signatures, dict):
        return []
    for scenario, item in signatures.items():
        if not isinstance(item, dict):
            continue
        sample_scores: list[float] = []
        samples = item.get("samples")
        if isinstance(samples, list):
            for sample in samples:
                if not isinstance(sample, dict) or not isinstance(sample.get("features"), dict):
                    continue
                if exclude_run_id and str(sample.get("run_id") or "") == exclude_run_id:
                    continue
                sample_features = {str(k): float(v) for k, v in sample["features"].items()}
                sample_scores.append(_cosine_similarity(features, sample_features))
        if sample_scores:
            top_scores = sorted(sample_scores, reverse=True)[:3]
            score = sum(top_scores) / len(top_scores)
            support = len(sample_scores)
        elif isinstance(samples, list):
            # 样本列表存在但排除后为空时，不能退回包含当前样本的质心。
            continue
        elif isinstance(item.get("centroid"), dict):
            score = _cosine_similarity(features, {str(k): float(v) for k, v in item["centroid"].items()})
            support = int(item.get("count") or 0)
        else:
            continue
        results.append(
            {
                "fault_type": scenario,
                "similarity": round(score, 4),
                "support": support,
            }
        )
    return sorted(results, key=lambda item: item["similarity"], reverse=True)[:top_n]


def load_signature_library(path: Path) -> dict[str, Any] | None:
    """读取故障特征库；不存在时返回 None。"""

    if not path.exists():
        return None
    return read_json(path)


def save_signature_library(path: Path, library: dict[str, Any]) -> None:
    """保存故障特征库。"""

    write_json(path, library)
