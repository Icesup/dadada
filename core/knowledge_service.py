from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path
from typing import Any


def load_knowledge_chunks(path: Path) -> list[dict[str, Any]]:
    """读取知识块。兼容 UTF-8 BOM。"""

    chunks: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8-sig") as f:
        for line_no, line in enumerate(f, 1):
            if not line.strip():
                continue
            try:
                chunk = json.loads(line)
            except Exception as exc:  # noqa: BLE001
                raise ValueError(f"知识块第 {line_no} 行解析失败: {exc}") from exc
            chunks.append(chunk)
    return chunks


def summarize_knowledge_chunks(chunks: list[dict[str, Any]]) -> dict[str, Any]:
    """统计知识库条目数量和类型分布。"""

    domains = Counter(chunk.get("domain") or "UNKNOWN" for chunk in chunks)
    topics = Counter(chunk.get("topic") or "UNKNOWN" for chunk in chunks)
    return {
        "total_chunks": len(chunks),
        "domain_counts": dict(domains),
        "top_topics": topics.most_common(20),
    }


def build_knowledge_query(
    *,
    device_type: str,
    performance_summary: dict[str, Any] | None,
    event_summary: dict[str, Any] | None,
) -> dict[str, Any]:
    """根据性能摘要和诊断可见事件生成知识库查询，不使用 ground truth。"""

    terms: list[str] = [device_type]
    changes: dict[str, dict[str, Any]] = {}
    for item in (performance_summary or {}).get("key_metric_changes", []) or []:
        if not isinstance(item, dict):
            continue
        metric = str(item.get("metric") or "")
        if metric:
            changes[metric] = item
        terms.extend(
            [
                str(item.get("device_type") or ""),
                metric,
                str(item.get("direction") or ""),
            ]
        )
    device = device_type.lower()
    if device == "edfa":
        quality_drop = any(
            (changes.get(metric) or {}).get("significant") and (changes.get(metric) or {}).get("direction") == "decrease"
            for metric in ["output_osnr_db", "output_gsnr_db"]
        )
        gain_drop = (changes.get("actual_gain_db") or {}).get("significant") and (changes.get("actual_gain_db") or {}).get("direction") == "decrease"
        ripple = (changes.get("power_ripple_db") or {}).get("significant")
        if quality_drop and not gain_drop:
            terms.extend(["edfa_noise_surge_signature", "noise", "ASE", "noise figure"])
        if gain_drop:
            terms.extend(["edfa_gain_degradation_signature", "gain degradation"])
        if ripple:
            terms.extend(["edfa_tilt_ripple_signature", "tilt", "ripple"])
    elif device == "fiber":
        if (changes.get("pmd_ps") or {}).get("significant"):
            terms.extend(["fiber_pmd_surge_signature", "PMD"])
        if (changes.get("output_power_dbm") or {}).get("direction") == "decrease":
            terms.extend(["fiber_attenuation_surge_signature", "attenuation", "loss"])
        if (changes.get("accumulated_nli_dbm") or {}).get("significant"):
            terms.extend(["fiber_nonlinear_anomaly_signature", "NLI", "nonlinear"])
    elif device == "roadm":
        if any((changes.get(metric) or {}).get("direction") == "decrease" for metric in ["output_power_dbm", "current_osnr_db", "output_gsnr_db"]):
            terms.extend(["roadm_wss_filter_shift_signature", "WSS", "filter shift"])
    event_types = Counter()
    for event in (event_summary or {}).get("events", []) or []:
        if isinstance(event, dict):
            event_type = event.get("event_type")
            if isinstance(event_type, str):
                event_types[event_type] += 1
    terms.extend(event_types.keys())
    query_text = " ".join(term for term in terms if term)
    return {
        "query_text": query_text,
        "terms": [term for term in terms if term],
        "event_type_counts": dict(event_types),
    }


def _tokenize(text: str) -> list[str]:
    """按英文标识符和中文短语做轻量分词。"""

    text = text.lower()
    tokens = re.findall(r"[a-z0-9_]+|[\u4e00-\u9fff]{2,}", text)
    return tokens


def search_knowledge_chunks(
    chunks: list[dict[str, Any]],
    query: dict[str, Any] | str,
    top_k: int = 5,
    exclude_topics: set[str] | None = None,
    exclude_chunk_ids: set[str] | None = None,
) -> list[dict[str, Any]]:
    """轻量知识库 Top-K 检索，score 只表示文本相关性。"""

    if isinstance(query, dict):
        query_text = str(query.get("query_text") or " ".join(str(item) for item in query.get("terms", [])))
    else:
        query_text = query
    query_tokens = set(_tokenize(query_text))
    exclude_topics = exclude_topics or set()
    exclude_chunk_ids = exclude_chunk_ids or set()
    scored: list[dict[str, Any]] = []
    for chunk in chunks:
        if str(chunk.get("topic") or "") in exclude_topics:
            continue
        if str(chunk.get("chunk_id") or "") in exclude_chunk_ids:
            continue
        searchable = " ".join(
            str(chunk.get(key, ""))
            for key in ["chunk_id", "domain", "topic", "device_type", "metric", "content", "use_for"]
        )
        chunk_tokens = set(_tokenize(searchable))
        overlap = query_tokens & chunk_tokens
        metric_bonus = 0.0
        topic = str(chunk.get("topic", "")).lower()
        for token in query_tokens:
            if token and token in str(chunk.get("metric", "")).lower():
                metric_bonus += 0.5
            if token and token in topic:
                metric_bonus += 0.5
        if topic and topic in query_text.lower():
            metric_bonus += 3.0
        score = float(len(overlap)) + metric_bonus
        if score > 0:
            scored.append({**chunk, "score": round(score, 4)})
    if not scored:
        scored = [{**chunk, "score": 0.0} for chunk in chunks[:top_k]]
    return sorted(scored, key=lambda item: item["score"], reverse=True)[:top_k]
