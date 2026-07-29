from __future__ import annotations

from typing import Any

from .diagnosis_adapter import DiagnosisAdapter
from core.diagnosis_service import build_rule_diagnosis_from_context


class RuleDiagnosisAdapter(DiagnosisAdapter):
    """本地规则诊断适配器。"""

    def diagnose(self, payload: dict[str, Any]) -> dict[str, Any]:
        signature_matches = [item for item in payload.get("historical_signature_matches", []) if isinstance(item, dict)]
        performance_status = str((payload.get("performance_event_summary") or {}).get("status") or "")
        diagnostic_hints = payload.get("diagnostic_hints") if isinstance(payload.get("diagnostic_hints"), dict) else {}
        observed_status = str(diagnostic_hints.get("observed_status") or performance_status)
        if observed_status != "ABNORMAL":
            return {
                "mode": "本地规则",
                "status": "NORMAL",
                "summary": "当前观测窗口未形成稳定的性能劣化证据链，暂不输出故障根因。",
                "top_causes": [],
                "recommendations": ["继续观察关键性能指标和业务生命周期事件。"],
                "knowledge_chunk_ids": [str(item.get("chunk_id")) for item in payload.get("knowledge_results", []) if item.get("chunk_id")],
            }
        return build_rule_diagnosis_from_context(
            device_type=str(payload.get("device_type") or "edfa"),
            entity_id=str(payload.get("entity_id") or ""),
            performance_summary=payload.get("performance_event_summary"),
            event_summary=payload.get("event_summary_for_diagnosis"),
            knowledge_results=list(payload.get("knowledge_results") or []),
        )
        if signature_matches:
            top = signature_matches[0]
            if top.get("fault_type") == "NORMAL_STATE" and observed_status != "ABNORMAL":
                return {
                    "mode": "本地规则",
                    "status": "NORMAL",
                    "summary": "历史特征库最相似类别为 NORMAL_STATE，当前窗口未形成稳定故障证据链。",
                    "top_causes": [],
                    "recommendations": ["继续观察关键性能指标和业务生命周期事件。"],
                    "knowledge_chunk_ids": [str(item.get("chunk_id")) for item in payload.get("knowledge_results", []) if item.get("chunk_id")],
                }
            if top.get("fault_type") == "NORMAL_STATE" and observed_status == "ABNORMAL":
                return build_rule_diagnosis_from_context(
                    device_type=str(payload.get("device_type") or "edfa"),
                    entity_id=str(payload.get("entity_id") or ""),
                    performance_summary=payload.get("performance_event_summary"),
                    event_summary=payload.get("event_summary_for_diagnosis"),
                    knowledge_results=list(payload.get("knowledge_results") or []),
                )
            causes = []
            for rank, item in enumerate(signature_matches[:3], 1):
                fault_type = str(item.get("fault_type") or "")
                if fault_type == "NORMAL_STATE":
                    continue
                causes.append(
                    {
                        "rank": len(causes) + 1,
                        "entity_id": str(payload.get("entity_id") or ""),
                        "fault_type": fault_type,
                        "evidence": [
                            f"历史特征库相似度 {float(item.get('similarity') or 0.0):.4f}",
                            f"历史样本支持数 {int(item.get('support') or 0)}",
                        ],
                        "exclusion": "该排序来自历史特征库，需要结合指标变化与知识库物理机理复核。",
                    }
                )
            if causes:
                return {
                    "mode": "本地规则",
                    "status": "ABNORMAL",
                    "summary": "结合历史特征库相似度、性能摘要和知识库内容生成候选根因。",
                    "top_causes": causes,
                    "recommendations": ["优先复核 Top-1 候选设备的关键指标变化。", "结合知识库内容检查对应物理机理和处置动作。"],
                    "knowledge_chunk_ids": [str(item.get("chunk_id")) for item in payload.get("knowledge_results", []) if item.get("chunk_id")],
                }
        return build_rule_diagnosis_from_context(
            device_type=str(payload.get("device_type") or "edfa"),
            entity_id=str(payload.get("entity_id") or ""),
            performance_summary=payload.get("performance_event_summary"),
            event_summary=payload.get("event_summary_for_diagnosis"),
            knowledge_results=list(payload.get("knowledge_results") or []),
        )
