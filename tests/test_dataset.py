from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.dataset_registry import build_databackup_manifest, build_manifest, build_registry  # noqa: E402
from core.config import get_external_data_roots  # noqa: E402
from core.experiment_service import get_run_dir  # noqa: E402


def test_preferred_run_manifest_is_valid_or_partial() -> None:
    run = ROOT / "data" / "runs" / "dataset_aiops_event_driven_v10_20260621_172317"
    manifest = build_manifest(run)
    assert manifest.scenario == "EDFA_NOISE_SURGE"
    assert manifest.fault_entity is not None
    assert manifest.trigger_tick == 60.0
    assert not manifest.validation_errors


def test_registry_builds_for_all_copied_runs() -> None:
    registry = build_registry(ROOT / "data" / "runs")
    assert len(registry) >= 29
    statuses = {item["status"] for item in registry}
    assert "VALID" in statuses or "PARTIAL" in statuses


def test_get_run_dir_prefers_existing_absolute_path(tmp_path: Path) -> None:
    run_dir = tmp_path / "batch" / "episode"
    run_dir.mkdir(parents=True)

    resolved = get_run_dir(
        {
            "run_id": "multi-root-run",
            "run_dir": str(run_dir),
            "source_relative_path": "unavailable/elsewhere",
        }
    )

    assert resolved == run_dir


def test_databackup_manifest_keeps_absolute_episode_path(tmp_path: Path) -> None:
    episode = tmp_path / "experiment_batch_test" / "episode_0001"
    episode.mkdir(parents=True)

    manifest = build_databackup_manifest(episode, tmp_path)

    assert manifest.run_dir == episode.resolve()


def test_multiple_data_roots_from_environment(tmp_path: Path, monkeypatch) -> None:
    root_a = tmp_path / "old"
    root_b = tmp_path / "new"
    (root_a / "experiment_batch_a").mkdir(parents=True)
    (root_b / "experiment_batch_b").mkdir(parents=True)
    monkeypatch.setenv("DATA_ROOTS", f"{root_a};{root_b}")

    roots = get_external_data_roots()

    assert roots == [root_a.resolve(), root_b.resolve()]
