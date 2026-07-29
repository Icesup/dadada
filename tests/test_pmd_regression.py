from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import KNOWLEDGE_CHUNKS_PATH, build_full_experiment_context  # noqa: E402
from core.experiment_service import load_registry  # noqa: E402
from core.engine_service import build_diagnosis_payload, diagnose_with_config  # noqa: E402
from core.knowledge_service import build_knowledge_query, load_knowledge_chunks, search_knowledge_chunks  # noqa: E402


def test_fiber_pmd_surge_run_is_diagnosed_from_full_experiment_context() -> None:
    registry = load_registry()
    run = next(
        item
        for item in registry
        if item.get("scenario") == "FIBER_PMD_SURGE" and item.get("status") == "VALID"
    )

    context = build_full_experiment_context(run, pre_window=30.0, post_window=30.0)
    assert context["candidate"]["device_type"] == "fiber"
    assert context["candidate"]["entity_id"] == run["fault_entity"]
    assert context["performance_summary"]["status"] == "ABNORMAL"

    chunks = load_knowledge_chunks(KNOWLEDGE_CHUNKS_PATH)
    knowledge_query = build_knowledge_query(
        device_type="fiber",
        performance_summary=context["performance_summary"],
        event_summary=context["event_summary"],
    )
    knowledge_results = search_knowledge_chunks(
        chunks,
        knowledge_query,
        top_k=5,
        exclude_topics={"normal_state_signature"},
        exclude_chunk_ids={"SIM_FAULT_NORMAL_001"},
    )
    payload = build_diagnosis_payload(
        run_id=run["run_id"],
        device_type="fiber",
        entity_id=context["candidate"]["entity_id"],
        performance_event_summary=context["performance_summary"],
        event_summary_for_diagnosis=context["event_summary"],
        knowledge_query=knowledge_query,
        knowledge_results=knowledge_results,
        signature_matches=context["signature_matches"],
    )
    diagnosis = diagnose_with_config(payload, {})

    assert diagnosis["status"] == "ABNORMAL"
    assert diagnosis["top_causes"][0]["fault_type"] == "FIBER_PMD_SURGE"
    assert diagnosis["top_causes"][0]["entity_id"] == run["fault_entity"]
