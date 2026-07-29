from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .schemas import JsonlReadResult


def read_json(path: Path) -> dict[str, Any]:
    """读取 UTF-8 JSON 文件，出错时抛出带路径的异常。"""

    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"JSON 解析失败: {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"JSON 顶层应为对象: {path}")
    return data


def read_jsonl(path: Path, *, strict: bool = False) -> JsonlReadResult:
    """读取 JSONL 文件；默认保留坏行并继续读取，strict=True 时立即报错。"""

    records: list[dict[str, Any]] = []
    errors: list[str] = []
    if not path.exists():
        return JsonlReadResult(records=[], errors=[f"文件不存在: {path}"])

    with path.open("r", encoding="utf-8-sig") as f:
        for line_no, line in enumerate(f, 1):
            raw = line.strip()
            if not raw:
                continue
            try:
                obj = json.loads(raw)
            except Exception as exc:  # noqa: BLE001
                msg = f"{path.name}:{line_no}: JSONL 解析失败: {exc}"
                if strict:
                    raise ValueError(msg) from exc
                errors.append(msg)
                continue
            if not isinstance(obj, dict):
                msg = f"{path.name}:{line_no}: 记录顶层不是对象"
                if strict:
                    raise ValueError(msg)
                errors.append(msg)
                continue
            records.append(obj)
    return JsonlReadResult(records=records, errors=errors)


def write_json(path: Path, data: Any) -> None:
    """以 UTF-8 写出 JSON。"""

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
