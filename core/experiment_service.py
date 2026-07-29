from __future__ import annotations

from pathlib import Path
from typing import Any

from .io_utils import read_json, write_json
from .config import resolve_external_run_dir


ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
RUNS_DIR = DATA_DIR / "runs"
REGISTRY_PATH = DATA_DIR / "dataset_registry.json"
CURRENT_RUN_PATH = DATA_DIR / "cache" / "current_run.json"


def load_registry(registry_path: Path = REGISTRY_PATH) -> list[dict[str, Any]]:
    """读取实验注册表，供页面选择已有仿真实验。"""

    if not registry_path.exists():
        raise FileNotFoundError(f"实验注册表不存在: {registry_path}")
    data = read_json_list(registry_path)
    return data


def read_json_list(path: Path) -> list[dict[str, Any]]:
    """读取顶层为列表的 JSON 文件。"""

    import json

    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"JSON 解析失败: {path}: {exc}") from exc
    if not isinstance(data, list):
        raise ValueError(f"JSON 顶层应为列表: {path}")
    return [item for item in data if isinstance(item, dict)]


def find_run(registry: list[dict[str, Any]], run_id: str) -> dict[str, Any]:
    """从注册表中查找一个实验。"""

    for item in registry:
        if item.get("run_id") == run_id:
            return item
    raise KeyError(f"注册表中找不到实验: {run_id}")


def get_run_dir(run: dict[str, Any]) -> Path:
    """返回实验目录；优先使用注册表中的绝对路径，兼容多数据根目录。"""

    run_dir = run.get("run_dir")
    if isinstance(run_dir, str) and run_dir:
        resolved = Path(run_dir)
        if resolved.exists():
            return resolved
    relative_path = run.get("source_relative_path")
    if isinstance(relative_path, str) and relative_path:
        return resolve_external_run_dir(relative_path)
    if isinstance(run_dir, str) and run_dir:
        return Path(run_dir)
    run_id = run.get("run_id")
    if not isinstance(run_id, str) or not run_id:
        raise ValueError("实验缺少 run_id，无法定位目录")
    return RUNS_DIR / run_id


def save_current_run(run_id: str, path: Path = CURRENT_RUN_PATH) -> None:
    """保存当前实验选择，供不同 Streamlit 页面复用。"""

    write_json(path, {"run_id": run_id})


def load_current_run_id(path: Path = CURRENT_RUN_PATH) -> str | None:
    """读取当前实验选择；没有选择时返回 None。"""

    if not path.exists():
        return None
    try:
        data = read_json(path)
    except ValueError:
        return None
    run_id = data.get("run_id")
    return run_id if isinstance(run_id, str) and run_id else None


def choose_default_run(registry: list[dict[str, Any]]) -> str | None:
    """选择一个适合演示的默认实验，优先 EDFA 噪声类 VALID 数据。"""

    replay_preferred = "experiment_batch_20260623_194654__episode_0119"
    for item in registry:
        if item.get("run_id") == replay_preferred and item.get("status") == "VALID":
            return replay_preferred
    preferred_new = "experiment_batch_20260623_094644__episode_0108"
    for item in registry:
        if item.get("run_id") == preferred_new and item.get("status") == "VALID":
            return preferred_new
    preferred = "dataset_aiops_event_driven_v10_20260621_172317"
    for item in registry:
        if item.get("run_id") == preferred and item.get("status") == "VALID":
            return preferred
    for item in registry:
        if item.get("status") == "VALID" and item.get("scenario") == "EDFA_NOISE_SURGE":
            return item.get("run_id")
    for item in registry:
        if item.get("status") == "VALID":
            return item.get("run_id")
    return registry[0].get("run_id") if registry else None
