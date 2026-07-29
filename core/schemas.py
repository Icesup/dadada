from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any


class DatasetStatus(str, Enum):
    """数据集注册状态。"""

    VALID = "VALID"
    PARTIAL = "PARTIAL"
    INVALID = "INVALID"
    LEGACY = "LEGACY"


@dataclass
class JsonlReadResult:
    """JSONL 文件读取结果，保留错误行而不是静默吞掉。"""

    records: list[dict[str, Any]]
    errors: list[str] = field(default_factory=list)


@dataclass
class FileCheck:
    """单个数据文件的存在性、记录数和错误信息。"""

    exists: bool
    records: int = 0
    errors: list[str] = field(default_factory=list)
    size_bytes: int | None = None
    schema_fields: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "exists": self.exists,
            "records": self.records,
            "errors": self.errors,
            "size_bytes": self.size_bytes,
            "schema_fields": self.schema_fields,
        }


@dataclass
class DatasetManifest:
    """实验数据集 manifest，供页面、诊断和评估统一使用。"""

    run_id: str
    dataset_version: str
    status: DatasetStatus
    scenario: str | None
    fault_entity: str | None
    trigger_tick: float | None
    files: dict[str, FileCheck]
    record_counts: dict[str, int]
    notes: list[str] = field(default_factory=list)
    validation_errors: list[str] = field(default_factory=list)
    validation_warnings: list[str] = field(default_factory=list)
    run_dir: Path | None = None
    batch_id: str | None = None
    episode_id: str | None = None
    source_relative_path: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "dataset_version": self.dataset_version,
            "status": self.status.value,
            "scenario": self.scenario,
            "fault_entity": self.fault_entity,
            "trigger_tick": self.trigger_tick,
            "files": {name: check.to_dict() for name, check in self.files.items()},
            "record_counts": self.record_counts,
            "notes": self.notes,
            "validation_errors": self.validation_errors,
            "validation_warnings": self.validation_warnings,
            "run_dir": str(self.run_dir) if self.run_dir else None,
            "batch_id": self.batch_id,
            "episode_id": self.episode_id,
            "source_relative_path": self.source_relative_path,
        }
