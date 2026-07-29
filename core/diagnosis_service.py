from __future__ import annotations

from typing import Any

from .anomaly_service import is_harmful_change


def _change_map(performance_summary: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    changes: dict[str, dict[str, Any]] = {}
    for item in (performance_summary or {}).get("key_metric_changes", []) or []:
        if isinstance(item, dict) and item.get("metric"):
            changes[str(item["metric"])] = item
    return changes


def _change_is_significant(changes: dict[str, dict[str, Any]], metric: str) -> bool:
    item = changes.get(metric)
    if not item:
        return False
    if isinstance(item.get("harmful"), bool):
        return bool(item.get("harmful"))
    return is_harmful_change(item)


def _metric_evidence(changes: dict[str, dict[str, Any]], metrics: list[str]) -> list[str]:
    evidence: list[str] = []
    direction_labels = {"increase": "升高", "decrease": "下降", "stable": "稳定", "unknown": "样本不足"}
    for metric in metrics:
        item = changes.get(metric)
        if not item:
            continue
        delta = item.get("delta")
        direction = direction_labels.get(str(item.get("direction")), str(item.get("direction")))
        if isinstance(delta, (int, float)):
            evidence.append(f"{metric} 在窗口后 {direction}，变化量 {delta:.4f}")
        else:
            evidence.append(f"{metric} 在窗口后 {direction}")
    return evidence


def _knowledge_ids(knowledge_results: list[dict[str, Any]]) -> list[str]:
    return [str(item.get("chunk_id")) for item in knowledge_results if item.get("chunk_id")]


def _knowledge_titles(knowledge_results: list[dict[str, Any]]) -> list[str]:
    titles: list[str] = []
    for item in knowledge_results[:3]:
        title = item.get("topic") or item.get("chunk_id")
        if title:
            titles.append(str(title))
    return titles


def build_rule_diagnosis_from_context(
    *,
    device_type: str,
    entity_id: str,
    performance_summary: dict[str, Any] | None,
    event_summary: dict[str, Any] | None,
    knowledge_results: list[dict[str, Any]],
) -> dict[str, Any]:
    """基于摘要和知识库检索结果生成可展示的本地诊断结果。"""

    changes = _change_map(performance_summary)
    status = str((performance_summary or {}).get("status") or "")
    if not status:
        status = "ABNORMAL" if any(is_harmful_change(item) for item in changes.values()) else "NORMAL"
    if status == "NORMAL":
        return {
            "mode": "本地规则",
            "status": "NORMAL",
            "summary": "当前窗口内未发现显著性能异常，结构化仿真事件未形成故障证据链。",
            "top_causes": [],
            "recommendations": ["继续观察关键指标窗口变化。", "如业务事件异常增多，可扩大时间窗口复核。"],
            "knowledge_chunk_ids": _knowledge_ids(knowledge_results),
        }

    device = device_type.lower()
    top_causes: list[dict[str, Any]]
    if device == "edfa":
        gain_drop = _change_is_significant(changes, "actual_gain_db") and (changes.get("actual_gain_db") or {}).get("direction") == "decrease"
        quality_drop = any(
            _change_is_significant(changes, metric) and (changes.get(metric) or {}).get("direction") == "decrease"
            for metric in ["output_osnr_db", "output_gsnr_db"]
        )
        ripple = _change_is_significant(changes, "power_ripple_db")
        if quality_drop and not gain_drop:
            primary = "EDFA_NOISE_SURGE"
            second = "EDFA_GAIN_DEGRADATION"
            third = "EDFA_TILT_RIPPLE_ERROR"
        elif gain_drop:
            primary = "EDFA_GAIN_DEGRADATION"
            second = "EDFA_NOISE_SURGE"
            third = "EDFA_TILT_RIPPLE_ERROR"
        elif ripple:
            primary = "EDFA_TILT_RIPPLE_ERROR"
            second = "EDFA_NOISE_SURGE"
            third = "EDFA_GAIN_DEGRADATION"
        else:
            primary, second, third = "EDFA_NOISE_SURGE", "EDFA_GAIN_DEGRADATION", "EDFA_TILT_RIPPLE_ERROR"
        top_causes = [
            {
                "rank": 1,
                "entity_id": entity_id,
                "fault_type": primary,
                "evidence": _metric_evidence(changes, ["output_osnr_db", "output_gsnr_db", "actual_gain_db", "output_power_dbm"]) + _knowledge_titles(knowledge_results),
                "exclusion": "若 actual_gain_db 与 output_power_dbm 基本稳定，可降低增益衰退优先级。",
            },
            {
                "rank": 2,
                "entity_id": entity_id,
                "fault_type": second,
                "evidence": _metric_evidence(changes, ["actual_gain_db", "output_power_dbm"]),
                "exclusion": "当前证据弱于 Top-1，需要结合相邻设备和更多指标复核。",
            },
            {
                "rank": 3,
                "entity_id": entity_id,
                "fault_type": third,
                "evidence": _metric_evidence(changes, ["power_ripple_db", "output_osnr_db"]),
                "exclusion": "若未观察到功率波纹或通道不均衡，该候选优先级较低。",
            },
        ]
    elif device == "fiber":
        pmd = _change_is_significant(changes, "pmd_ps") and (changes.get("pmd_ps") or {}).get("direction") == "increase"
        nli = _change_is_significant(changes, "accumulated_nli_dbm") and (changes.get("accumulated_nli_dbm") or {}).get("direction") == "increase"
        power_drop = (changes.get("output_power_dbm") or {}).get("direction") == "decrease"
        if pmd:
            primary, second, third = "FIBER_PMD_SURGE", "FIBER_ATTENUATION_SURGE", "FIBER_NONLINEAR_ANOMALY"
        elif nli:
            primary, second, third = "FIBER_NONLINEAR_ANOMALY", "FIBER_ATTENUATION_SURGE", "FIBER_PMD_SURGE"
        elif power_drop:
            primary, second, third = "FIBER_ATTENUATION_SURGE", "FIBER_PMD_SURGE", "FIBER_NONLINEAR_ANOMALY"
        else:
            primary, second, third = "FIBER_ATTENUATION_SURGE", "FIBER_NONLINEAR_ANOMALY", "FIBER_PMD_SURGE"
        top_causes = [
            {
                "rank": 1,
                "entity_id": entity_id,
                "fault_type": primary,
                "evidence": _metric_evidence(changes, ["output_power_dbm", "current_osnr_db", "current_gsnr_db", "pmd_ps", "accumulated_nli_dbm"]) + _knowledge_titles(knowledge_results),
                "exclusion": "根据功率、PMD 与 NLI 的主导变化排除较弱候选。",
            },
            {"rank": 2, "entity_id": entity_id, "fault_type": second, "evidence": _metric_evidence(changes, ["output_power_dbm", "pmd_ps"]), "exclusion": "证据弱于 Top-1。"},
            {"rank": 3, "entity_id": entity_id, "fault_type": third, "evidence": _metric_evidence(changes, ["accumulated_nli_dbm", "current_gsnr_db"]), "exclusion": "需结合入纤功率和相邻跨段复核。"},
        ]
    else:
        quality_drop = any(
            _change_is_significant(changes, metric) and (changes.get(metric) or {}).get("direction") == "decrease"
            for metric in ["current_osnr_db", "output_gsnr_db", "output_power_dbm"]
        )
        power_drop = _change_is_significant(changes, "output_power_dbm") and (changes.get("output_power_dbm") or {}).get("direction") == "decrease"
        primary = "ROADM_WSS_FILTER_SHIFT" if quality_drop and power_drop else "ROADM_INBAND_CROSSTALK"
        second = "ROADM_INBAND_CROSSTALK" if primary == "ROADM_WSS_FILTER_SHIFT" else "ROADM_WSS_FILTER_SHIFT"
        top_causes = [
            {
                "rank": 1,
                "entity_id": entity_id,
                "fault_type": primary,
                "evidence": _metric_evidence(changes, ["output_power_dbm", "current_osnr_db", "output_gsnr_db"]) + _knowledge_titles(knowledge_results),
                "exclusion": "若输出功率同步下降，WSS/滤波偏移优先；若功率未下降但 OSNR/GSNR 下降，串扰优先。",
            },
            {"rank": 2, "entity_id": entity_id, "fault_type": second, "evidence": _metric_evidence(changes, ["current_osnr_db", "output_gsnr_db"]), "exclusion": "证据弱于 Top-1。"},
            {"rank": 3, "entity_id": entity_id, "fault_type": "ROADM_PORT_OR_VOA_CONFIGURATION_ERROR", "evidence": _metric_evidence(changes, ["output_power_dbm"]), "exclusion": "需要端口映射或 VOA 配置证据增强。"},
        ]

    visible_events = (event_summary or {}).get("events", [])
    event_note = f"诊断可见事件 {len(visible_events)} 条，已排除故障注入事件。"
    return {
        "mode": "本地规则",
        "status": "ABNORMAL",
        "summary": f"当前窗口内检测到显著性能异常。{event_note}",
        "top_causes": top_causes,
        "recommendations": [
            "优先复核 Top-1 设备的关键性能指标和相邻设备同步变化。",
            "结合知识库依据检查对应物理机制和排障动作。",
            "生成处置验证任务后交由外部仿真程序对比验证。",
        ],
        "knowledge_chunk_ids": _knowledge_ids(knowledge_results),
    }
