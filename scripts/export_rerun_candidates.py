"""根据批量评测与可观测性审计结果导出待重跑实验清单。"""

from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REGISTRY = PROJECT_ROOT / "data" / "dataset_registry.json"
DEFAULT_EVALUATION = PROJECT_ROOT / "data" / "cache" / "batch_eval_final_20260711.json"
DEFAULT_GAIN_AUDIT = PROJECT_ROOT / "data" / "cache" / "edfa_gain_observability_full.json"


def load_json(path: Path) -> Any:
    """读取JSON文件并返回解析结果。"""
    return json.loads(path.read_text(encoding="utf-8"))


def build_registry_index(registry_path: Path) -> dict[str, dict[str, Any]]:
    """按run_id建立实验注册信息索引。"""
    rows = load_json(registry_path)
    if not isinstance(rows, list):
        raise ValueError(f"实验注册表格式错误，应为列表: {registry_path}")
    return {str(row["run_id"]): row for row in rows}


def registry_fields(run_id: str, registry: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """提取便于交接和定位原始数据的注册字段。"""
    row = registry.get(run_id, {})
    return {
        "run_id": run_id,
        "batch_id": row.get("batch_id", ""),
        "episode_id": row.get("episode_id", ""),
        "fault_type": row.get("scenario", ""),
        "fault_entity": row.get("fault_entity", ""),
        "trigger_tick": row.get("trigger_tick", ""),
        "dataset_status": row.get("status", ""),
        "source_relative_path": row.get("source_relative_path", ""),
    }


def export_candidates(
    registry_path: Path,
    evaluation_path: Path,
    gain_audit_path: Path,
    output_dir: Path,
) -> dict[str, Any]:
    """导出原始数据重跑项与诊断困难项，并返回统计摘要。"""
    registry = build_registry_index(registry_path)
    evaluation = load_json(evaluation_path)
    gain_audit = load_json(gain_audit_path)
    evaluation_rows = evaluation.get("rows", [])
    gain_rows = gain_audit.get("rows", [])
    evaluated_by_run = {str(row["run_id"]): row for row in evaluation_rows}

    rerun_rows: list[dict[str, Any]] = []
    for audit in gain_rows:
        if audit.get("gain_drop_observed"):
            continue
        run_id = str(audit["run_id"])
        row = registry_fields(run_id, registry)
        row.update(
            {
                "priority": "P0",
                "category": "仿真注入需复核并重跑",
                "reason": (
                    "EDFA增益衰退注入未反映到目标设备actual_gain_db；"
                    "目标记录存在，ground truth期望下降10 dB，但故障前后增益差为0；"
                    "需检查故障注入逻辑或telemetry导出逻辑"
                ),
                "expected_gain_drop_db": audit.get("expected_drop_db", ""),
                "observed_gain_delta_db": audit.get("gain_delta", ""),
                "target_record_count": audit.get("target_record_count", ""),
                "evaluated_in_108_group_batch": run_id in evaluated_by_run,
            }
        )
        rerun_rows.append(row)

    review_rows: list[dict[str, Any]] = []
    for result in evaluation_rows:
        run_id = str(result["run_id"])
        signature = result.get("injected_signature_observability", {})
        signature_observed = bool(signature.get("observed"))
        type_miss = not bool(result.get("type_hit_1"))
        entity_miss = not bool(result.get("entity_hit_1"))
        if not signature_observed or not (type_miss or entity_miss):
            continue
        reasons: list[str] = []
        if type_miss:
            reasons.append(
                f"故障特征可观测，但Top-1类型误判为{result.get('predicted_top1_type') or 'NORMAL'}"
            )
        if entity_miss:
            reasons.append("故障特征可观测，但Top-1故障实体定位未命中")
        row = registry_fields(run_id, registry)
        row.update(
            {
                "priority": "P1",
                "category": "诊断困难样本补充",
                "reason": "；".join(reasons),
                "predicted_top1_type": result.get("predicted_top1_type", ""),
                "predicted_top3_types": " | ".join(result.get("predicted_top3_types", [])),
                "selected_entity": result.get("selected_entity", ""),
                "type_hit_1": bool(result.get("type_hit_1")),
                "type_hit_3": bool(result.get("type_hit_3")),
                "entity_hit_1": bool(result.get("entity_hit_1")),
                "signature_observed": signature_observed,
            }
        )
        review_rows.append(row)

    output_dir.mkdir(parents=True, exist_ok=True)
    write_csv(output_dir / "01_EDFA增益衰退_注入效应未反映_需复核重跑.csv", rerun_rows)
    write_csv(output_dir / "02_诊断困难样本建议补跑.csv", review_rows)

    combined_by_run: dict[str, dict[str, Any]] = {}
    for row in rerun_rows + review_rows:
        run_id = str(row["run_id"])
        if run_id not in combined_by_run:
            combined_by_run[run_id] = dict(row)
            continue
        combined_by_run[run_id]["category"] += "；" + str(row["category"])
        combined_by_run[run_id]["reason"] += "；" + str(row["reason"])
    combined_rows = sorted(
        combined_by_run.values(),
        key=lambda row: (str(row.get("priority", "")), str(row.get("fault_type", "")), str(row["run_id"])),
    )
    write_csv(output_dir / "00_待重跑与补充样本总清单.csv", combined_rows)

    summary = {
        "source_evaluation": str(evaluation_path),
        "source_gain_audit": str(gain_audit_path),
        "evaluation_coverage": len(evaluation_rows),
        "injection_review_count": len(rerun_rows),
        "diagnosis_review_count": len(review_rows),
        "unique_candidate_count": len(combined_rows),
        "injection_review_by_batch": dict(Counter(row["batch_id"] for row in rerun_rows)),
        "diagnosis_review_by_fault": dict(Counter(row["fault_type"] for row in review_rows)),
    }
    (output_dir / "待重跑实验清单.json").write_text(
        json.dumps({"summary": summary, "rows": combined_rows}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    write_readme(output_dir / "说明.md", summary, review_rows)
    return summary


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    """以Excel可直接打开的UTF-8 BOM编码写入CSV。"""
    fieldnames: list[str] = []
    for row in rows:
        for key in row:
            if key not in fieldnames:
                fieldnames.append(key)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def write_readme(path: Path, summary: dict[str, Any], review_rows: list[dict[str, Any]]) -> None:
    """写入面向数据提供方的简要重跑说明。"""
    review_counts = Counter(row["fault_type"] for row in review_rows)
    lines = [
        "# 待重跑实验说明",
        "",
        "## 结论",
        "",
        f"- 需仿真侧复核并重跑：{summary['injection_review_count']}组，均为EDFA_GAIN_DEGRADATION。",
        "- 直接现象：目标EDFA记录存在，ground truth期望增益下降10 dB，但actual_gain_db在故障注入前后保持不变。",
        "- 这说明预期注入效应没有进入当前telemetry；问题可能位于仿真注入逻辑或telemetry导出逻辑，不能简单表述为全部原始文件损坏。",
        f"- 建议补跑的诊断困难样本：{summary['diagnosis_review_count']}组。",
        f"- 去重后候选总数：{summary['unique_candidate_count']}组。",
        "",
        "## 文件用途",
        "",
        "- `01_EDFA增益衰退_注入效应未反映_需复核重跑.csv`：优先交给仿真侧检查注入与telemetry导出逻辑，修复后重跑。",
        "- `02_诊断困难样本建议补跑.csv`：故障已反映到telemetry，但类型或实体定位容易混淆，可补充不同实体、严重度和业务负载的样本。",
        "- `00_待重跑与补充样本总清单.csv`：两类清单的去重汇总。",
        "- `待重跑实验清单.json`：供程序读取。",
        "",
        "## 重跑要求",
        "",
        "1. EDFA_GAIN_DEGRADATION重跑后，应确认目标设备actual_gain_db在trigger_tick后出现与注入参数一致的持续下降。",
        "2. 同时保留output_power_dbm、output_osnr_db、output_gsnr_db和nf_db，便于区分增益衰退与噪声异常。",
        "3. ROADM诊断困难样本建议增加不同故障实体、不同严重度和不同业务负载，避免特征只在单一拓扑位置出现。",
        "4. ground_truth只用于评估，不应写入诊断可见事件或性能摘要。",
        "",
        "## 覆盖范围说明",
        "",
        f"- 诊断效果来自{summary['evaluation_coverage']}组分层评测，不代表1000组数据全部经过在线模型逐组调用。",
        f"- EDFA增益衰退专项审计覆盖{summary['injection_review_count']}组，即注册表中该故障类型的全部实验。",
        "- 当前建议补跑样本按故障类型统计："
        + ("，".join(f"{key} {value}组" for key, value in sorted(review_counts.items())) or "无"),
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def parse_args() -> argparse.Namespace:
    """解析命令行参数。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)
    parser.add_argument("--evaluation", type=Path, default=DEFAULT_EVALUATION)
    parser.add_argument("--gain-audit", type=Path, default=DEFAULT_GAIN_AUDIT)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def main() -> None:
    """命令行入口。"""
    args = parse_args()
    summary = export_candidates(args.registry, args.evaluation, args.gain_audit, args.output)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
