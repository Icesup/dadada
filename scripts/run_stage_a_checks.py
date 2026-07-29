from __future__ import annotations

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.dataset_registry import build_manifest, build_registry  # noqa: E402
from core.event_service import filter_events, load_simulation_events  # noqa: E402
from core.knowledge_service import load_knowledge_chunks, summarize_knowledge_chunks  # noqa: E402
from core.telemetry_service import load_telemetry, list_metric_fields, summarize_metric_change  # noqa: E402


def check(condition: bool, message: str) -> None:
    """简单断言，避免阶段 A 因 pytest 未安装而无法验证。"""

    if not condition:
        raise AssertionError(message)
    print(f"[OK] {message}")


def main() -> int:
    run = ROOT / "data" / "runs" / "dataset_aiops_event_driven_v10_20260621_172317"

    manifest = build_manifest(run)
    check(manifest.scenario == "EDFA_NOISE_SURGE", "首选 run 的故障类型为 EDFA_NOISE_SURGE")
    check(manifest.trigger_tick == 60.0, "首选 run 的故障注入 tick 为 60.0")
    check(not manifest.validation_errors, "首选 run 没有验证错误")

    # 自检只扫描内置样例，不覆盖平台正在使用的正式注册表。
    registry = build_registry(ROOT / "data" / "runs")
    check(len(registry) >= 29, "注册表包含至少 29 个带 ground truth 的数据集")
    check(any(item["status"] == "VALID" for item in registry), "注册表中存在 VALID 数据集")
    check(any(item["status"] == "LEGACY" for item in registry), "旧版 fiber cut 数据被保留为 LEGACY")

    telemetry, errors = load_telemetry(run, "edfa")
    check(not errors, "首选 run 的 EDFA telemetry 无解析错误")
    check("actual_gain_db" in telemetry[0], "telemetry metrics 已展平成顶层字段")
    check("output_gsnr_db" in list_metric_fields(telemetry), "可识别 EDFA output_gsnr_db 指标")
    change = summarize_metric_change(
        telemetry,
        entity_id="Edfa_booster_roadm Seattle_to_fiber (Seattle → Palo Alto)-_(1/12)",
        metric="output_gsnr_db",
        trigger_tick=60.0,
    )
    check("pre_count" in change and "post_count" in change, "异常摘要函数返回故障前后窗口计数")

    events, event_errors = load_simulation_events(run)
    check(not event_errors, "首选 run 的 simulation_events 无解析错误")
    matched = filter_events(
        events,
        event_type="EDFA_NOISE_SURGE",
        entity_id="Edfa_booster_roadm Seattle_to_fiber (Seattle → Palo Alto)-_(1/12)",
        start_tick=60.0,
        end_tick=60.0,
    )
    check(len(matched) == 1, "ground truth 故障事件可在 simulation_events 中精确找到")

    chunks = load_knowledge_chunks(ROOT / "data" / "knowledge" / "knowledge_chunks.jsonl")
    knowledge_summary = summarize_knowledge_chunks(chunks)
    check(knowledge_summary["total_chunks"] >= 42, "知识库可读取且数量不少于 42")
    check("optical_physics" in knowledge_summary["domain_counts"], "知识库包含 optical_physics 类型")

    print("阶段 A 自检全部通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
