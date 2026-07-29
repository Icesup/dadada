from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

from .io_utils import read_json, read_jsonl, write_json
from .schemas import DatasetManifest, DatasetStatus, FileCheck

REQUIRED_JSONL = [
    "simulation_events.jsonl",
    "service_lifecycle.jsonl",
    "telemetry_edfa.jsonl",
    "telemetry_fiber.jsonl",
    "telemetry_roadm.jsonl",
]


def infer_dataset_version(run_id: str, ground_truth: dict[str, Any] | None = None) -> str:
    """从目录名或 ground truth 元数据推断数据版本。"""

    match = re.search(r"_v(\d+)_", run_id)
    if match:
        return f"v{match.group(1)}"
    purpose = ((ground_truth or {}).get("metadata") or {}).get("dataset_purpose")
    if isinstance(purpose, str) and purpose:
        return purpose
    return "legacy"


def extract_fault_info(ground_truth: dict[str, Any] | None) -> tuple[str | None, str | None, float | None, list[str]]:
    """从实际嵌套 schema 中提取故障类型、故障实体和注入 tick。"""

    warnings: list[str] = []
    if not ground_truth:
        return None, None, None, ["缺少 ground_truth.json，无法提取故障信息"]

    fault = ground_truth.get("fault_injection")
    if not isinstance(fault, dict):
        return None, None, None, ["ground_truth 缺少 fault_injection 对象"]

    scenario = fault.get("fault_type")
    fault_entity = fault.get("faulty_entity_id")
    trigger = fault.get("trigger_time_simulation")
    trigger_tick: float | None
    try:
        trigger_tick = float(trigger) if trigger is not None else None
    except (TypeError, ValueError):
        trigger_tick = None
        warnings.append(f"trigger_time_simulation 不是数字: {trigger}")

    if not scenario:
        warnings.append("fault_injection 缺少 fault_type")
    if not fault_entity:
        warnings.append("fault_injection 缺少 faulty_entity_id")
    if trigger_tick is None:
        warnings.append("fault_injection 缺少 trigger_time_simulation")
    return scenario, fault_entity, trigger_tick, warnings


def _check_jsonl_file(path: Path) -> FileCheck:
    result = read_jsonl(path)
    size_bytes = path.stat().st_size if path.exists() else None
    schema_fields = sample_jsonl_schema(path) if path.exists() else []
    return FileCheck(
        exists=path.exists(),
        records=len(result.records),
        errors=result.errors,
        size_bytes=size_bytes,
        schema_fields=schema_fields,
    )


def _flatten_schema(obj: dict[str, Any]) -> list[str]:
    """提取一条记录的顶层字段和一层嵌套字段，用于注册表展示。"""

    fields: list[str] = []
    for key, value in obj.items():
        fields.append(key)
        if isinstance(value, dict):
            fields.extend(f"{key}.{nested_key}" for nested_key in value.keys())
    return sorted(set(fields))


def sample_jsonl_schema(path: Path, *, sample_records: int = 5) -> list[str]:
    """抽样 JSONL schema，不把全文件读入内存。"""

    fields: set[str] = set()
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig") as handle:
        seen = 0
        for line in handle:
            raw = line.strip()
            if not raw:
                continue
            obj = json.loads(raw)
            if isinstance(obj, dict):
                fields.update(_flatten_schema(obj))
            seen += 1
            if seen >= sample_records:
                break
    return sorted(fields)


def check_jsonl_file_streaming(path: Path, cache: dict[str, Any] | None = None) -> FileCheck:
    """流式检查 JSONL 文件，并用 size/mtime 缓存记录数和 schema。"""

    if not path.exists():
        return FileCheck(exists=False, records=0, errors=["文件不存在"], size_bytes=None, schema_fields=[])

    stat = path.stat()
    cache_key = str(path.resolve())
    cached = (cache or {}).get(cache_key)
    if (
        isinstance(cached, dict)
        and cached.get("size_bytes") == stat.st_size
        and cached.get("mtime_ns") == stat.st_mtime_ns
    ):
        return FileCheck(
            exists=True,
            records=int(cached.get("records") or 0),
            errors=list(cached.get("errors") or []),
            size_bytes=stat.st_size,
            schema_fields=list(cached.get("schema_fields") or []),
        )

    records = 0
    errors: list[str] = []
    schema_fields: set[str] = set()
    with path.open("r", encoding="utf-8-sig") as handle:
        for line_no, line in enumerate(handle, 1):
            raw = line.strip()
            if not raw:
                continue
            try:
                obj = json.loads(raw)
            except Exception as exc:  # noqa: BLE001
                if len(errors) < 20:
                    errors.append(f"{path.name}:{line_no}: JSONL 解析失败: {exc}")
                continue
            if not isinstance(obj, dict):
                if len(errors) < 20:
                    errors.append(f"{path.name}:{line_no}: 记录顶层不是对象")
                continue
            records += 1
            if records <= 5:
                schema_fields.update(_flatten_schema(obj))

    check = FileCheck(
        exists=True,
        records=records,
        errors=errors,
        size_bytes=stat.st_size,
        schema_fields=sorted(schema_fields),
    )
    if cache is not None:
        cache[cache_key] = {
            "size_bytes": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
            "records": records,
            "errors": errors,
            "schema_fields": check.schema_fields,
        }
    return check


def _event_matches_ground_truth(run_dir: Path, scenario: str | None, fault_entity: str | None, trigger_tick: float | None) -> bool:
    if not scenario or not fault_entity or trigger_tick is None:
        return False
    events = read_jsonl(run_dir / "simulation_events.jsonl")
    for event in events.records:
        try:
            tick = float(event.get("simulation_tick"))
        except (TypeError, ValueError):
            continue
        if (
            event.get("event_type") == scenario
            and event.get("entity_id") == fault_entity
            and abs(tick - trigger_tick) < 1e-6
        ):
            return True
    return False


def _has_duplicate_records(run_dir: Path, file_name: str) -> bool:
    result = read_jsonl(run_dir / file_name)
    seen: set[tuple[Any, ...]] = set()
    for record in result.records:
        key = (
            record.get("timestamp"),
            record.get("simulation_tick"),
            record.get("event_type"),
            record.get("entity_id") or record.get("service_id"),
        )
        if key in seen:
            return True
        seen.add(key)
    return False


def build_manifest(run_dir: Path) -> DatasetManifest:
    """为单个 run 生成 manifest，并执行基础质量检查。"""

    run_id = run_dir.name
    validation_errors: list[str] = []
    validation_warnings: list[str] = []
    notes: list[str] = []

    ground_truth: dict[str, Any] | None = None
    gt_path = run_dir / "ground_truth.json"
    if gt_path.exists():
        try:
            ground_truth = read_json(gt_path)
        except ValueError as exc:
            validation_errors.append(str(exc))
    else:
        validation_warnings.append("缺少 ground_truth.json")

    scenario, fault_entity, trigger_tick, fault_warnings = extract_fault_info(ground_truth)
    validation_warnings.extend(fault_warnings)
    dataset_version = infer_dataset_version(run_id, ground_truth)

    files: dict[str, FileCheck] = {}
    if gt_path.exists():
        files["ground_truth.json"] = FileCheck(exists=True, records=1, errors=[])
    else:
        files["ground_truth.json"] = FileCheck(exists=False, records=0, errors=["文件不存在"])

    for file_name in REQUIRED_JSONL:
        check = _check_jsonl_file(run_dir / file_name)
        files[file_name] = check
        if not check.exists:
            validation_warnings.append(f"缺少 {file_name}")
        if check.records == 0 and check.exists:
            validation_warnings.append(f"{file_name} 无有效记录")
        if check.exists:
            validation_errors.extend(check.errors)

    if gt_path.exists() and files["simulation_events.jsonl"].exists:
        if not _event_matches_ground_truth(run_dir, scenario, fault_entity, trigger_tick):
            message = "ground truth 中的故障事件未能在 simulation_events.jsonl 中精确对齐"
            if run_id.startswith("dataset_fiber_cut"):
                validation_warnings.append(message)
            else:
                validation_errors.append(message)

    for file_name in ["simulation_events.jsonl", "service_lifecycle.jsonl"]:
        if files.get(file_name, FileCheck(False)).exists and _has_duplicate_records(run_dir, file_name):
            validation_warnings.append(f"{file_name} 存在明显重复记录")

    record_counts = {name: check.records for name, check in files.items()}

    if run_id.startswith("dataset_fiber_cut"):
        status = DatasetStatus.LEGACY if not validation_errors else DatasetStatus.PARTIAL
    elif validation_errors:
        status = DatasetStatus.INVALID
    elif not gt_path.exists():
        status = DatasetStatus.LEGACY
    elif any(not files[name].exists for name in REQUIRED_JSONL):
        status = DatasetStatus.PARTIAL
    elif validation_warnings:
        status = DatasetStatus.PARTIAL
    else:
        status = DatasetStatus.VALID

    if run_id.startswith("dataset_fiber_cut") and status == DatasetStatus.VALID:
        notes.append("早期 fiber cut 数据集，字段可用但版本较旧")
    if scenario == "EDFA_NOISE_SURGE":
        notes.append("可作为 EDFA 噪声类首批演示候选")

    return DatasetManifest(
        run_id=run_id,
        dataset_version=dataset_version,
        status=status,
        scenario=scenario,
        fault_entity=fault_entity,
        trigger_tick=trigger_tick,
        files=files,
        record_counts=record_counts,
        notes=notes,
        validation_errors=validation_errors,
        validation_warnings=validation_warnings,
        run_dir=run_dir,
    )


def build_databackup_manifest(episode_dir: Path, data_root: Path, cache: dict[str, Any] | None = None) -> DatasetManifest:
    """为 databackup 的单个 episode 生成注册信息，不预加载 telemetry。"""

    batch_id = episode_dir.parent.name
    episode_id = episode_dir.name
    run_id = f"{batch_id}__{episode_id}"
    relative_path = episode_dir.relative_to(data_root).as_posix()
    validation_errors: list[str] = []
    validation_warnings: list[str] = []
    notes: list[str] = []

    ground_truth: dict[str, Any] | None = None
    gt_path = episode_dir / "ground_truth.json"
    if gt_path.exists():
        try:
            ground_truth = read_json(gt_path)
        except ValueError as exc:
            validation_errors.append(str(exc))
    else:
        validation_errors.append("缺少 ground_truth.json")

    scenario, fault_entity, trigger_tick, fault_warnings = extract_fault_info(ground_truth)
    validation_warnings.extend(fault_warnings)
    dataset_version = infer_dataset_version(run_id, ground_truth)

    files: dict[str, FileCheck] = {}
    if gt_path.exists():
        files["ground_truth.json"] = FileCheck(
            exists=True,
            records=1,
            errors=[],
            size_bytes=gt_path.stat().st_size,
            schema_fields=sorted((ground_truth or {}).keys()),
        )
    else:
        files["ground_truth.json"] = FileCheck(exists=False, records=0, errors=["文件不存在"])

    for file_name in REQUIRED_JSONL:
        check = check_jsonl_file_streaming(episode_dir / file_name, cache)
        files[file_name] = check
        if not check.exists:
            validation_errors.append(f"缺少 {file_name}")
        elif check.records == 0:
            validation_warnings.append(f"{file_name} 无有效记录")
        validation_errors.extend(check.errors)

    if scenario == "NORMAL_STATE":
        notes.append("正常状态样例，可用于正常/异常分支演示")
    elif scenario == "EDFA_NOISE_SURGE":
        notes.append("可作为 EDFA 噪声类演示候选")

    if validation_errors:
        status = DatasetStatus.INVALID
    elif any(not files[name].exists for name in REQUIRED_JSONL):
        status = DatasetStatus.PARTIAL
    elif validation_warnings:
        status = DatasetStatus.PARTIAL
    else:
        status = DatasetStatus.VALID

    record_counts = {name: check.records for name, check in files.items()}
    return DatasetManifest(
        run_id=run_id,
        dataset_version=dataset_version,
        status=status,
        scenario=scenario,
        fault_entity=fault_entity,
        trigger_tick=trigger_tick,
        files=files,
        record_counts=record_counts,
        notes=notes,
        validation_errors=validation_errors,
        validation_warnings=validation_warnings,
        run_dir=episode_dir.resolve(),
        batch_id=batch_id,
        episode_id=episode_id,
        source_relative_path=relative_path,
    )


def scan_runs(runs_root: Path) -> list[DatasetManifest]:
    """扫描 runs 目录并返回所有 manifest。"""

    if not runs_root.exists():
        raise FileNotFoundError(f"runs 目录不存在: {runs_root}")
    manifests: list[DatasetManifest] = []
    for run_dir in sorted(p for p in runs_root.iterdir() if p.is_dir()):
        manifests.append(build_manifest(run_dir))
    return manifests


def build_registry(runs_root: Path, output_path: Path | None = None) -> list[dict[str, Any]]:
    """生成数据集注册表，并把每个 run 的 manifest.json 写回 run 目录。"""

    manifests = scan_runs(runs_root)
    data = [manifest.to_dict() for manifest in manifests]
    for manifest in manifests:
        if manifest.run_dir is not None:
            write_json(manifest.run_dir / "manifest.json", manifest.to_dict())
    if output_path is not None:
        write_json(output_path, data)
    return data


def summarize_registry(registry: list[dict[str, Any]]) -> dict[str, Any]:
    """给界面首页使用的注册表概览。"""

    statuses = Counter(item["status"] for item in registry)
    scenarios = Counter(item.get("scenario") or "UNKNOWN" for item in registry)
    return {
        "total_runs": len(registry),
        "status_counts": dict(statuses),
        "scenario_counts": dict(scenarios),
    }
