from __future__ import annotations

import os
from typing import Any

from adapters.diagnosis_adapter import CompatibleDiagnosisAdapter
from adapters.rule_adapter import RuleDiagnosisAdapter


FAULT_ALTERNATIVES = {
    "FIBER_PMD_SURGE": [
        ("FIBER_ATTENUATION_SURGE", "若输出光功率显著下降且 PMD 不持续升高，则链路衰耗突增优先。"),
        ("FIBER_NONLINEAR_ANOMALY", "若 NLI 累积量明显升高且 GSNR 下降更突出，则非线性异常优先。"),
    ],
    "FIBER_ATTENUATION_SURGE": [
        ("FIBER_PMD_SURGE", "若 PMD 指标突增且功率基本稳定，则 PMD 异常优先。"),
        ("FIBER_NONLINEAR_ANOMALY", "若 NLI 累积量升高并伴随 GSNR 劣化，则非线性异常优先。"),
    ],
    "FIBER_NONLINEAR_ANOMALY": [
        ("FIBER_PMD_SURGE", "若 pmd_ps 发生持续阶跃式升高，则 PMD 异常优先。"),
        ("FIBER_ATTENUATION_SURGE", "若主要表现为输出光功率下降，则链路衰耗突增优先。"),
    ],
    "EDFA_NOISE_SURGE": [
        ("EDFA_GAIN_DEGRADATION", "若 actual_gain_db 与 output_power_dbm 同步下降，则增益衰退优先。"),
        ("EDFA_TILT_RIPPLE_ERROR", "若 power_ripple_db 或通道不均衡明显升高，则倾斜/波纹异常优先。"),
    ],
    "EDFA_GAIN_DEGRADATION": [
        ("EDFA_NOISE_SURGE", "若增益基本稳定但 OSNR/GSNR 下降，则噪声异常优先。"),
        ("EDFA_TILT_RIPPLE_ERROR", "若功率波纹或通道不均衡升高，则倾斜/波纹异常优先。"),
    ],
    "EDFA_TILT_RIPPLE_ERROR": [
        ("EDFA_NOISE_SURGE", "若 OSNR/GSNR 下降但波纹不明显，则噪声异常优先。"),
        ("EDFA_GAIN_DEGRADATION", "若实际增益和输出功率同步下降，则增益衰退优先。"),
    ],
    "ROADM_INBAND_CROSSTALK": [
        ("ROADM_WSS_FILTER_SHIFT", "若输出功率同步下降，WSS/滤波偏移优先。"),
        ("EDFA_NOISE_SURGE", "若异常主要出现在上游放大器输出 OSNR/GSNR，需排查 EDFA 噪声异常。"),
    ],
    "ROADM_WSS_FILTER_SHIFT": [
        ("ROADM_INBAND_CROSSTALK", "若功率未下降但 OSNR/GSNR 下降，带内串扰优先。"),
        ("EDFA_GAIN_DEGRADATION", "若下游多点功率下降且上游放大器增益异常，需排查 EDFA 增益衰退。"),
    ],
}


def _metric_change_map(summary: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    changes: dict[str, dict[str, Any]] = {}
    for item in (summary or {}).get("key_metric_changes", []) or []:
        if isinstance(item, dict) and item.get("metric"):
            changes[str(item["metric"])] = item
    return changes


def normalize_model_config(config: dict[str, Any] | None = None) -> dict[str, Any]:
    """合并页面配置和环境变量，页面配置优先。"""

    config = config or {}
    base_url = str(config.get("base_url") or os.getenv("ENGINE_BASE_URL") or "").strip()
    api_key = str(config.get("api_key") or os.getenv("ENGINE_API_KEY") or os.getenv("DASHSCOPE_API_KEY") or "").strip()
    model = str(config.get("model") or os.getenv("ENGINE_MODEL") or os.getenv("QWEN_MODEL") or "qwen3.7-plus").strip()
    return {
        "base_url": base_url,
        "api_key": api_key,
        "model": model,
        "max_tokens": int(config.get("max_tokens") or os.getenv("ENGINE_MAX_TOKENS") or 2000),
        "timeout_seconds": float(config.get("timeout_seconds") or os.getenv("ENGINE_TIMEOUT_SECONDS") or 120),
    }


def model_config_ready(config: dict[str, Any] | None = None) -> bool:
    """base_url、model、api_key 填齐后启用真实模型。"""

    normalized = normalize_model_config(config)
    return bool(normalized["base_url"] and normalized["api_key"] and normalized["model"])


def get_engine_status(config: dict[str, Any] | None = None) -> dict[str, Any]:
    """返回页面展示用的模型状态。"""

    normalized = normalize_model_config(config)
    if not model_config_ready(normalized):
        return {"mode": "本地规则", "ready": True, "model": "本地规则", "provider": "local_rules"}
    return {
        "mode": "在线引擎",
        "ready": True,
        "model": normalized["model"],
        "provider": "compatible_chat",
        "base_url": normalized["base_url"],
    }


def sanitize_performance_summary_for_engine(summary: dict[str, Any] | None) -> dict[str, Any] | None:
    """移除预判状态标签，只保留可观测指标变化。"""

    if not isinstance(summary, dict):
        return None
    cleaned = {
        key: value
        for key, value in summary.items()
        if key not in {"status"}
    }
    cleaned_changes: list[dict[str, Any]] = []
    for item in summary.get("key_metric_changes", []) or []:
        if not isinstance(item, dict):
            continue
        cleaned_changes.append(
            {
                key: value
                for key, value in item.items()
                if key not in {"significant", "harmful", "score"}
            }
        )
    cleaned["key_metric_changes"] = cleaned_changes
    return cleaned


def build_event_metric_candidates(
    changes: list[dict[str, Any]] | None,
    *,
    limit: int = 12,
) -> list[dict[str, Any]]:
    """仅从事件已观测遥测中构造指标候选，不携带故障标签或规则评分。"""

    candidates: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for item in changes or []:
        if not isinstance(item, dict):
            continue
        entity_id = str(item.get("entity_id") or "").strip()
        metric = str(item.get("metric") or "").strip()
        if not entity_id or not metric or (entity_id, metric) in seen:
            continue
        seen.add((entity_id, metric))
        candidates.append(
            {
                "entity_id": entity_id,
                "device_type": str(item.get("device_type") or "").strip(),
                "metric": metric,
                "pre_mean": item.get("pre_mean"),
                "post_mean": item.get("post_mean"),
                "delta": item.get("delta"),
                "relative_delta": item.get("relative_delta"),
                "direction": item.get("direction"),
            }
        )
        if len(candidates) >= limit:
            break
    return candidates


def _metric_candidates_from_payload(payload: dict[str, Any]) -> list[dict[str, Any]]:
    candidates = build_event_metric_candidates(payload.get("event_metric_candidates"))
    performance = payload.get("performance_event_summary")
    if not isinstance(performance, dict):
        return candidates

    additions: list[dict[str, Any]] = []
    parent_entity = str(performance.get("entity_id") or payload.get("entity_id") or "")
    parent_device = str(performance.get("device_type") or payload.get("device_type") or "")
    for change in performance.get("key_metric_changes") or []:
        if isinstance(change, dict):
            additions.append(
                {
                    "entity_id": parent_entity,
                    "device_type": parent_device,
                    **change,
                }
            )
    for observation in performance.get("experiment_wide_observations") or []:
        if not isinstance(observation, dict):
            continue
        for change in observation.get("key_changes") or []:
            if isinstance(change, dict):
                additions.append(
                    {
                        "entity_id": observation.get("candidate_entity_id"),
                        "device_type": observation.get("device_type"),
                        **change,
                    }
                )

    seen = {(item["entity_id"], item["metric"]) for item in candidates}
    for item in build_event_metric_candidates(additions):
        key = (item["entity_id"], item["metric"])
        if key not in seen:
            candidates.append(item)
            seen.add(key)
    return candidates


def select_validated_metric_features(
    diagnosis: dict[str, Any],
    payload: dict[str, Any],
    *,
    limit: int = 3,
) -> list[dict[str, Any]]:
    """校验在线模型选出的事件指标；无效或不足部分仅用可见异常证据补齐。"""

    candidates = _metric_candidates_from_payload(payload)
    candidate_map = {
        (str(item.get("entity_id") or ""), str(item.get("metric") or "")): item
        for item in candidates
    }
    selected: list[dict[str, Any]] = []
    selected_keys: set[tuple[str, str]] = set()
    for item in diagnosis.get("key_metric_features") or []:
        if not isinstance(item, dict):
            continue
        key = (str(item.get("entity_id") or "").strip(), str(item.get("metric") or "").strip())
        candidate = candidate_map.get(key)
        if not candidate or key in selected_keys:
            continue
        selected.append(
            {
                **candidate,
                "rank": len(selected) + 1,
                "reason": str(item.get("reason") or "该指标与当前事件的异常传播最相关。").strip(),
                "selection_source": "online_model",
            }
        )
        selected_keys.add(key)
        if len(selected) >= limit:
            break

    for candidate in candidates:
        key = (str(candidate.get("entity_id") or ""), str(candidate.get("metric") or ""))
        if key in selected_keys:
            continue
        selected.append(
            {
                **candidate,
                "rank": len(selected) + 1,
                "reason": "在线模型未返回足够的有效指标，按当前事件的遥测异常证据顺序补充。",
                "selection_source": "observed_evidence_fallback",
            }
        )
        selected_keys.add(key)
        if len(selected) >= limit:
            break
    return selected


def compact_performance_summary_for_engine(summary: dict[str, Any] | None, limit: int = 8) -> dict[str, Any] | None:
    """压缩性能摘要，降低在线诊断请求长度。"""

    cleaned = sanitize_performance_summary_for_engine(summary)
    if not isinstance(cleaned, dict):
        return None
    compact_changes: list[dict[str, Any]] = []
    for item in cleaned.get("key_metric_changes", [])[:limit] or []:
        if not isinstance(item, dict):
            continue
        compact_changes.append(
            {
                key: item.get(key)
                for key in (
                    "metric",
                    "pre_mean",
                    "post_mean",
                    "delta",
                    "relative_delta",
                    "direction",
                )
                if key in item
            }
        )
    experiment_observations: list[dict[str, Any]] = []
    for observation in cleaned.get("experiment_wide_observations", []) or []:
        if not isinstance(observation, dict):
            continue
        observation_changes: list[dict[str, Any]] = []
        for item in observation.get("key_changes", [])[:6] or []:
            if not isinstance(item, dict):
                continue
            observation_changes.append(
                {
                    key: item.get(key)
                    for key in ("metric", "pre_mean", "post_mean", "delta", "relative_delta", "direction")
                    if key in item
                }
            )
        experiment_observations.append(
            {
                "device_type": observation.get("device_type"),
                "candidate_entity_id": observation.get("candidate_entity_id"),
                "candidate_score": observation.get("candidate_score"),
                "key_changes": observation_changes,
            }
        )
    return {
        "run_id": cleaned.get("run_id"),
        "device_type": cleaned.get("device_type"),
        "entity_id": cleaned.get("entity_id"),
        "trigger_tick": cleaned.get("trigger_tick"),
        "key_metric_changes": compact_changes,
        "experiment_wide_observations": experiment_observations,
    }


def sanitize_event_summary_for_engine(summary: dict[str, Any] | None) -> dict[str, Any] | None:
    """移除故障注入过滤计数，只保留诊断可见事件。"""

    if not isinstance(summary, dict):
        return None
    allowed_keys = {
        "window",
        "diagnosis_visible_simulation_event_count",
        "service_lifecycle_event_count",
        "events",
    }
    cleaned = {key: value for key, value in summary.items() if key in allowed_keys}
    events: list[dict[str, Any]] = []
    for item in cleaned.get("events", [])[:8] or []:
        if not isinstance(item, dict):
            continue
        events.append(
            {
                key: item.get(key)
                for key in ("simulation_tick", "event_type", "entity_id", "service_id")
                if key in item
            }
        )
    cleaned["events"] = events
    return cleaned


def compact_text(value: Any, max_chars: int) -> str:
    """限制文本长度，避免知识块撑大在线请求。"""

    text = str(value or "")
    return text if len(text) <= max_chars else text[: max_chars - 1] + "…"


def build_diagnostic_hints(
    performance_summary: dict[str, Any] | None,
    signature_matches: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """根据可观测指标生成工程诊断提示，不读取 ground truth。"""

    if not isinstance(performance_summary, dict):
        return {"observed_status": "UNKNOWN", "top_candidates": [], "notes": ["未收到性能摘要。"]}

    device_type = str(performance_summary.get("device_type") or "").lower()
    entity_id = str(performance_summary.get("entity_id") or "")
    changes = _metric_change_map(performance_summary)
    harmful_changes = [item for item in changes.values() if bool(item.get("harmful"))]
    observed_status = "ABNORMAL" if str(performance_summary.get("status")) == "ABNORMAL" or harmful_changes else "NORMAL"

    def has(metric: str, direction: str | None = None) -> bool:
        item = changes.get(metric)
        if not item:
            return False
        if direction and item.get("direction") != direction:
            return False
        return bool(item.get("harmful"))

    def has_observed_drop(metric: str, min_score: float = 1.0) -> bool:
        item = changes.get(metric)
        if not item or item.get("direction") != "decrease":
            return False
        score = item.get("score")
        return isinstance(score, (int, float)) and float(score) >= min_score

    direction_labels = {"increase": "升高", "decrease": "下降", "stable": "稳定", "unknown": "样本不足"}

    def evidence(metrics: list[str]) -> list[str]:
        rows: list[str] = []
        for metric in metrics:
            item = changes.get(metric)
            if not item:
                continue
            delta = item.get("delta")
            direction = item.get("direction")
            direction_text = direction_labels.get(str(direction), str(direction))
            if isinstance(delta, (int, float)):
                rows.append(f"{metric} 在窗口后{direction_text}，均值变化 {delta:.4f}")
            else:
                rows.append(f"{metric} 在窗口后{direction_text}")
        return rows

    candidates: list[dict[str, Any]] = []
    if observed_status == "ABNORMAL" and device_type == "fiber":
        if has("pmd_ps", "increase"):
            candidates.append(
                {
                    "fault_type": "FIBER_PMD_SURGE",
                    "entity_id": entity_id,
                    "evidence": evidence(["pmd_ps", "output_power_dbm", "current_osnr_db", "current_gsnr_db"]),
                    "priority": "HIGH",
                    "rule": "光纤实体 pmd_ps 在故障窗口后显著升高。",
                }
            )
        if has("accumulated_nli_dbm", "increase"):
            candidates.append(
                {
                    "fault_type": "FIBER_NONLINEAR_ANOMALY",
                    "entity_id": entity_id,
                    "evidence": evidence(["accumulated_nli_dbm", "current_gsnr_db", "output_power_dbm"]),
                    "priority": "MEDIUM",
                    "rule": "NLI 噪声累计量升高并伴随链路质量变化。",
                }
            )
        if has("output_power_dbm", "decrease") or has_observed_drop("output_power_dbm"):
            candidates.append(
                {
                    "fault_type": "FIBER_ATTENUATION_SURGE",
                    "entity_id": entity_id,
                    "evidence": evidence(["output_power_dbm", "current_osnr_db", "current_gsnr_db"]),
                    "priority": "MEDIUM",
                    "rule": "输出光功率下降符合链路衰耗增大特征。",
                }
            )
    elif observed_status == "ABNORMAL" and device_type == "edfa":
        quality_drop = has("output_osnr_db", "decrease") or has("output_gsnr_db", "decrease")
        gain_drop = has("actual_gain_db", "decrease")
        if quality_drop and not gain_drop:
            candidates.append(
                {
                    "fault_type": "EDFA_NOISE_SURGE",
                    "entity_id": entity_id,
                    "evidence": evidence(["output_osnr_db", "output_gsnr_db", "actual_gain_db", "output_power_dbm"]),
                    "priority": "HIGH",
                    "rule": "OSNR/GSNR 下降而增益未同步下降，优先考虑噪声异常。",
                }
            )
        if gain_drop:
            candidates.append(
                {
                    "fault_type": "EDFA_GAIN_DEGRADATION",
                    "entity_id": entity_id,
                    "evidence": evidence(["actual_gain_db", "output_power_dbm", "output_osnr_db", "output_gsnr_db"]),
                    "priority": "HIGH",
                    "rule": "实际增益下降是增益衰退的直接观测证据。",
                }
            )
        if has("power_ripple_db", "increase"):
            candidates.append(
                {
                    "fault_type": "EDFA_TILT_RIPPLE_ERROR",
                    "entity_id": entity_id,
                    "evidence": evidence(["power_ripple_db", "power_variance", "output_osnr_db"]),
                    "priority": "MEDIUM",
                    "rule": "功率波纹或方差升高符合倾斜/波纹异常特征。",
                }
            )
    elif observed_status == "ABNORMAL":
        if has("output_power_dbm", "decrease"):
            candidates.append(
                {
                    "fault_type": "ROADM_WSS_FILTER_SHIFT",
                    "entity_id": entity_id,
                    "evidence": evidence(["output_power_dbm", "current_osnr_db", "output_gsnr_db"]),
                    "priority": "MEDIUM",
                    "rule": "ROADM 侧功率或质量指标下降时优先检查 WSS/滤波偏移。",
                }
            )
            candidates.append(
                {
                    "fault_type": "EDFA_GAIN_DEGRADATION",
                    "entity_id": entity_id,
                    "evidence": evidence(["output_power_dbm", "current_osnr_db", "output_gsnr_db"]),
                    "priority": "LOW",
                    "rule": "下游设备出现功率下降时，需要排查上游放大器增益衰退或链路衰耗传播。",
                }
            )
        if has("current_osnr_db", "decrease") or has("output_gsnr_db", "decrease"):
            candidates.append(
                {
                    "fault_type": "ROADM_INBAND_CROSSTALK",
                    "entity_id": entity_id,
                    "evidence": evidence(["current_osnr_db", "output_gsnr_db", "output_power_dbm"]),
                    "priority": "HIGH",
                    "rule": "ROADM 侧 OSNR/GSNR 下降但输出功率未同步下降，优先考虑带内串扰。",
                }
            )

    signature_notes = [
        f"{item.get('fault_type')} similarity={item.get('similarity')}"
        for item in (signature_matches or [])[:3]
        if isinstance(item, dict)
    ]
    notes = [
        "当前指标证据优先于历史相似度；历史相似度只作为排序辅助。",
        "PROVISIONED/RELEASED 仅表示业务生命周期事件，不能直接作为正常结论。",
    ]
    if signature_notes:
        notes.append("历史相似项：" + "；".join(signature_notes))
    return {
        "observed_status": observed_status,
        "device_type": device_type,
        "entity_id": entity_id,
        "top_candidates": candidates[:3],
        "notes": notes,
    }


def _cause_from_rule_candidate(item: dict[str, Any], *, rank: int, entity_id: str) -> dict[str, Any]:
    """把工程规则候选转换为页面候选根因。"""

    return {
        "rank": rank,
        "entity_id": item.get("entity_id") or entity_id,
        "fault_type": item.get("fault_type"),
        "evidence": item.get("evidence") or [item.get("rule") or "当前窗口存在显著指标劣化。"],
        "exclusion": item.get("rule") or "依据当前窗口指标变化进行排序。",
    }


def _cause_from_signature(item: dict[str, Any], *, rank: int, entity_id: str) -> dict[str, Any]:
    """把历史相似项转换为候选根因。"""

    return {
        "rank": rank,
        "entity_id": entity_id,
        "fault_type": item.get("fault_type"),
        "evidence": [
            f"历史特征相似度 {float(item.get('similarity') or 0.0):.4f}",
            f"历史样本支持数 {int(item.get('support') or 0)}",
        ],
        "exclusion": "历史相似项只作为候选排序证据，仍需结合当前窗口指标变化复核。",
    }


def _complete_top_causes(top_causes: list[dict[str, Any]], payload: dict[str, Any]) -> list[dict[str, Any]]:
    """异常场景下补足候选根因，避免标题写 Top-3 但只返回一个候选。"""

    hints = payload.get("diagnostic_hints") if isinstance(payload, dict) else {}
    hints = hints if isinstance(hints, dict) else {}
    entity_id = str(hints.get("entity_id") or payload.get("entity_id") or "")
    completed: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add(cause: dict[str, Any]) -> None:
        fault_type = str(cause.get("fault_type") or "")
        if not fault_type or fault_type == "NORMAL_STATE" or fault_type in seen:
            return
        normalized = dict(cause)
        normalized["rank"] = len(completed) + 1
        normalized["entity_id"] = normalized.get("entity_id") or entity_id
        normalized.setdefault("evidence", ["当前窗口存在相关指标变化。"])
        normalized.setdefault("exclusion", "证据弱于更高排序候选。")
        completed.append(normalized)
        seen.add(fault_type)

    for cause in top_causes:
        if isinstance(cause, dict):
            add(cause)

    primary_type = str((completed[0] if completed else {}).get("fault_type") or "")
    for fault_type, exclusion in FAULT_ALTERNATIVES.get(primary_type, []):
        add(
            {
                "rank": len(completed) + 1,
                "entity_id": entity_id,
                "fault_type": fault_type,
                "evidence": ["作为相近故障类型保留，用于排查边界对比。"],
                "exclusion": exclusion,
            }
        )

    for item in hints.get("top_candidates", []) or []:
        if isinstance(item, dict):
            add(_cause_from_rule_candidate(item, rank=len(completed) + 1, entity_id=entity_id))

    for item in payload.get("historical_signature_matches", []) or []:
        if isinstance(item, dict):
            add(_cause_from_signature(item, rank=len(completed) + 1, entity_id=entity_id))

    return completed[:3]


def calibrate_diagnosis_output(
    diagnosis: dict[str, Any],
    payload: dict[str, Any],
    *,
    force_observed_status: bool = True,
) -> dict[str, Any]:
    """用工程证据链校验诊断结果，避免历史相似项或模型措辞覆盖当前观测证据。"""

    hints = payload.get("diagnostic_hints") if isinstance(payload, dict) else {}
    if not isinstance(hints, dict):
        return diagnosis

    if not force_observed_status:
        diagnosis.setdefault("top_causes", [])
        diagnosis.setdefault("recommendations", [])
        diagnosis.setdefault("knowledge_chunk_ids", [])
        if diagnosis.get("status") == "ABNORMAL":
            original_count = len([item for item in diagnosis.get("top_causes", []) if isinstance(item, dict)])
            diagnosis["top_causes"] = _complete_top_causes(
                [item for item in diagnosis.get("top_causes", []) if isinstance(item, dict)],
                payload,
            )
            if len(diagnosis["top_causes"]) > original_count:
                diagnosis["_supplemented_candidates"] = len(diagnosis["top_causes"]) - original_count
        return diagnosis

    candidates = [item for item in hints.get("top_candidates", []) if isinstance(item, dict)]
    if hints.get("observed_status") != "ABNORMAL" or not candidates:
        if not force_observed_status:
            diagnosis.setdefault("top_causes", [])
            diagnosis.setdefault("recommendations", [])
            diagnosis.setdefault("knowledge_chunk_ids", [])
            if diagnosis.get("status") == "ABNORMAL":
                diagnosis["top_causes"] = _complete_top_causes(
                    [item for item in diagnosis.get("top_causes", []) if isinstance(item, dict)],
                    payload,
                )
            return diagnosis
        diagnosis["status"] = "NORMAL"
        diagnosis["summary"] = "当前观测窗口未形成稳定的性能劣化证据链，暂不输出故障根因。"
        diagnosis["top_causes"] = []
        diagnosis.setdefault("recommendations", ["继续观察关键性能指标和业务生命周期事件。"])
        diagnosis.setdefault("knowledge_chunk_ids", [])
        return diagnosis

    candidate_types = [str(item.get("fault_type") or "") for item in candidates]
    top_causes = [item for item in diagnosis.get("top_causes", []) if isinstance(item, dict)]
    top1_type = str((top_causes[0] if top_causes else {}).get("fault_type") or "")
    need_repair = diagnosis.get("status") == "NORMAL" or not top_causes or top1_type not in candidate_types
    if need_repair:
        repaired_causes = []
        for rank, item in enumerate(candidates, 1):
            repaired_causes.append(
                _cause_from_rule_candidate(item, rank=rank, entity_id=str(hints.get("entity_id") or payload.get("entity_id") or ""))
            )
        diagnosis["status"] = "ABNORMAL"
        diagnosis["top_causes"] = repaired_causes
        diagnosis["summary"] = "当前窗口存在明确性能劣化，诊断结果已按可观测指标证据校正。"
        diagnosis["_calibrated"] = True
    else:
        for cause in top_causes:
            fault_type = str(cause.get("fault_type") or "")
            matched = next((item for item in candidates if item.get("fault_type") == fault_type), None)
            if not matched:
                continue
            evidence = [str(item) for item in (cause.get("evidence") or [])]
            metric_evidence = [str(item) for item in (matched.get("evidence") or [])]
            for item in reversed(metric_evidence):
                if item and item not in evidence:
                    evidence.insert(0, item)
            cause["evidence"] = evidence[:6]

    diagnosis["top_causes"] = _complete_top_causes(
        [item for item in diagnosis.get("top_causes", []) if isinstance(item, dict)],
        payload,
    )
    diagnosis.setdefault("recommendations", [])
    if not diagnosis["recommendations"]:
        diagnosis["recommendations"] = ["复核 Top-1 候选对象的关键指标曲线。", "结合相邻设备与业务路径确认影响范围。"]
    diagnosis.setdefault("knowledge_chunk_ids", [])
    return diagnosis


def build_diagnosis_payload(
    *,
    run_id: str,
    device_type: str,
    entity_id: str,
    performance_event_summary: dict[str, Any] | None,
    event_summary_for_diagnosis: dict[str, Any] | None,
    knowledge_query: dict[str, Any],
    knowledge_results: list[dict[str, Any]],
    signature_matches: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """组装模型输入，不包含 ground truth 的故障类型和真实故障设备。"""

    compact_knowledge = [
        {
            "chunk_id": item.get("chunk_id"),
            "score": item.get("score"),
            "topic": item.get("topic"),
            "content": compact_text(item.get("content"), 220),
            "source": item.get("source"),
        }
        for item in knowledge_results[:3]
    ]
    return {
        "run_id": run_id,
        "device_type": device_type,
        "entity_id": entity_id,
        "performance_event_summary": compact_performance_summary_for_engine(performance_event_summary),
        "event_summary_for_diagnosis": sanitize_event_summary_for_engine(event_summary_for_diagnosis),
        "diagnostic_hints": build_diagnostic_hints(performance_event_summary, signature_matches),
        "historical_signature_matches": [
            {
                "fault_type": item.get("fault_type"),
                "similarity": item.get("similarity"),
                "support": item.get("support"),
            }
            for item in (signature_matches or [])[:3]
        ],
        "knowledge_query": {"query_text": compact_text((knowledge_query or {}).get("query_text"), 260)},
        "knowledge_results": compact_knowledge,
    }


def diagnose_with_config(payload: dict[str, Any], model_config: dict[str, Any] | None = None) -> dict[str, Any]:
    """按页面配置选择本地规则或在线接口诊断。"""

    config = normalize_model_config(model_config)
    if model_config_ready(config):
        adapter = CompatibleDiagnosisAdapter(
            api_key=config["api_key"],
            base_url=config["base_url"],
            model=config["model"],
            max_tokens=config["max_tokens"],
            timeout_seconds=config["timeout_seconds"],
        )
        diagnosis = adapter.diagnose(payload)
        diagnosis["mode"] = "在线引擎"
        return calibrate_diagnosis_output(diagnosis, payload, force_observed_status=False)
    diagnosis = RuleDiagnosisAdapter().diagnose(payload)
    diagnosis["mode"] = "本地规则"
    return calibrate_diagnosis_output(diagnosis, payload)


def test_online_model_config(model_config: dict[str, Any] | None = None) -> dict[str, Any]:
    """轻量检查在线接口配置是否可用。"""

    config = normalize_model_config(model_config)
    if not model_config_ready(config):
        raise ValueError("接口地址、引擎代号或访问密钥未填完整")
    adapter = CompatibleDiagnosisAdapter(
        api_key=config["api_key"],
        base_url=config["base_url"],
        model=config["model"],
        max_tokens=50,
        timeout_seconds=min(float(config["timeout_seconds"]), 30),
    )
    return adapter.ping()
