from __future__ import annotations

from collections import Counter
import json
from pathlib import Path
from typing import Any


def _read_json(path: Path | None) -> dict[str, Any]:
    if path is None or not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _latest_report(cache_dir: Path, patterns: tuple[str, ...]) -> Path | None:
    candidates: list[Path] = []
    for pattern in patterns:
        candidates.extend(cache_dir.glob(pattern))
    existing = [path for path in candidates if path.is_file()]
    return max(existing, key=lambda path: path.stat().st_mtime, default=None)


def build_operations_overview(registry: list[dict[str, Any]], cache_dir: Path) -> dict[str, Any]:
    """汇总态势页需要的数据，避免在展示层散落统计逻辑。"""

    status_counts = Counter(str(item.get("status") or "UNKNOWN") for item in registry)
    scenario_counts = Counter(str(item.get("scenario") or "UNKNOWN") for item in registry)
    batch_counts = Counter(str(item.get("batch_id") or "本地数据") for item in registry)
    total = len(registry)
    valid = status_counts.get("VALID", 0)

    evaluation_path = _latest_report(
        cache_dir,
        (
            "batch_eval_rerun_*.json",
            "batch_eval_final_*.json",
            "batch_eval_offset*.json",
        ),
    )
    gain_audit_path = _latest_report(
        cache_dir,
        (
            "edfa_gain_observability_rerun_*.json",
            "edfa_gain_observability_full.json",
            "edfa_gain_observability_*.json",
        ),
    )
    evaluation = _read_json(evaluation_path)
    evaluation_summary = evaluation.get("summary") if isinstance(evaluation.get("summary"), dict) else {}
    gain_audit = _read_json(gain_audit_path)
    gain_summary = gain_audit.get("summary") if isinstance(gain_audit.get("summary"), dict) else {}

    risks: list[dict[str, str]] = []
    invalid_count = total - valid
    if invalid_count:
        risks.append({"级别": "数据", "事项": f"{invalid_count} 组数据未达到 VALID", "建议": "先处理注册校验错误"})
    misses = evaluation_summary.get("misses")
    if isinstance(misses, list) and misses:
        risks.append({"级别": "诊断", "事项": f"最新评测存在 {len(misses)} 组 Top-1 偏差", "建议": "进入评测治理查看混淆类型"})
    gain_total = int(gain_summary.get("total") or 0)
    gain_observed = int(gain_summary.get("gain_drop_observed") or 0)
    if gain_total and gain_observed < gain_total:
        risks.append(
            {
                "级别": "可观测性",
                "事项": f"EDFA 增益下降可观测 {gain_observed}/{gain_total}",
                "建议": "核查注入参数到 telemetry 的映射",
            }
        )
    if not risks:
        risks.append({"级别": "正常", "事项": "当前未发现阻断性数据或评测风险", "建议": "继续扩大留出集验证"})

    scenario_rows = [
        {"故障场景": name, "样本数": count, "占比": count / total if total else 0.0}
        for name, count in scenario_counts.most_common()
    ]
    batch_rows = [
        {"批次": name, "实验数": count}
        for name, count in sorted(batch_counts.items(), key=lambda item: item[0], reverse=True)
    ]
    return {
        "total_runs": total,
        "valid_runs": valid,
        "valid_rate": valid / total if total else 0.0,
        "status_counts": dict(status_counts),
        "scenario_count": len(scenario_counts),
        "scenario_rows": scenario_rows,
        "batch_count": len(batch_counts),
        "batch_rows": batch_rows,
        "evaluation_path": str(evaluation_path) if evaluation_path else "",
        "evaluation_summary": evaluation_summary,
        "gain_audit_path": str(gain_audit_path) if gain_audit_path else "",
        "gain_summary": gain_summary,
        "risks": risks,
    }
