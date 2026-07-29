from __future__ import annotations

import os
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "configs" / "platform.yaml"


def _contains_experiment_batches(path: Path) -> bool:
    """快速判断目录是否包含已注册的批次数据，不递归扫描 telemetry。"""

    if not path.is_dir():
        return False
    try:
        return next(path.glob("experiment_batch_*"), None) is not None
    except OSError:
        return False


def _normalize_data_root(path: Path) -> Path | None:
    """兼容直接复制 databackup 或保留一层同名目录的情况。"""

    candidates = [path, path / "databackup", path / "databackup" / "databackup"]
    for candidate in candidates:
        if _contains_experiment_batches(candidate):
            return candidate.resolve()
    return None


def _read_simple_yaml_value(path: Path, dotted_key: str) -> str | None:
    """读取当前项目用到的简单 YAML 配置值。"""

    if not path.exists():
        return None
    parts = dotted_key.split(".")
    stack: list[tuple[int, str]] = []
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        if not raw_line.strip() or raw_line.lstrip().startswith("#"):
            continue
        indent = len(raw_line) - len(raw_line.lstrip(" "))
        line = raw_line.strip()
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        while stack and stack[-1][0] >= indent:
            stack.pop()
        current_path = [item[1] for item in stack] + [key]
        if current_path == parts and value:
            return value
        if not value:
            stack.append((indent, key))
    return None


def get_data_root_resolution() -> tuple[Path | None, str]:
    """定位仿真数据根目录，并返回路径来源。"""

    env_value = os.environ.get("DATA_ROOT")
    if env_value:
        env_root = _normalize_data_root(Path(env_value).expanduser())
        return (env_root or Path(env_value).expanduser(), "DATA_ROOT")

    configured_value = _read_simple_yaml_value(CONFIG_PATH, "data.external_data_root")
    if configured_value:
        configured_path = Path(configured_value).expanduser()
        if not configured_path.is_absolute():
            configured_path = ROOT / configured_path
        configured_root = _normalize_data_root(configured_path)
        if configured_root is not None:
            return configured_root, "configs/platform.yaml"

    portable_candidates = [
        ROOT / "portable_data",
        ROOT / "databackup",
        ROOT.parent / "databackup",
        ROOT.parent / "系统仿真数据" / "databackup",
    ]
    for candidate in portable_candidates:
        portable_root = _normalize_data_root(candidate)
        if portable_root is not None:
            return portable_root, f"自动发现:{candidate}"

    if configured_value:
        return configured_path, "configs/platform.yaml（路径不存在）"
    return None, "未配置"


def get_external_data_root() -> Path | None:
    """返回仿真数据根目录，支持配置路径和便携目录自动发现。"""

    root, _ = get_data_root_resolution()
    return root


def get_external_data_roots() -> list[Path]:
    """返回注册阶段使用的全部数据根；运行时仍兼容单根配置。"""

    configured = os.environ.get("DATA_ROOTS") or _read_simple_yaml_value(CONFIG_PATH, "data.external_data_roots")
    roots: list[Path] = []
    if configured:
        for raw_value in configured.split(";"):
            value = raw_value.strip()
            if not value:
                continue
            path = Path(value).expanduser()
            if not path.is_absolute():
                path = ROOT / path
            roots.append(_normalize_data_root(path) or path.resolve())
    if not roots:
        primary = get_external_data_root()
        if primary is not None:
            roots.append(primary)
    unique: list[Path] = []
    seen: set[str] = set()
    for path in roots:
        key = str(path).casefold()
        if key not in seen:
            seen.add(key)
            unique.append(path)
    return unique


def resolve_external_run_dir(relative_path: str) -> Path:
    """根据配置根目录和注册表相对路径定位 episode 目录。"""

    root = get_external_data_root()
    if root is None:
        raise ValueError("未配置外部数据根目录，请设置 DATA_ROOT 或 configs/platform.yaml:data.external_data_root")
    return root / relative_path
