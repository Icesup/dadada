from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.knowledge_service import build_knowledge_query, load_knowledge_chunks, search_knowledge_chunks, summarize_knowledge_chunks  # noqa: E402
from core.signature_service import classify_with_signature_library  # noqa: E402


def test_knowledge_chunks_load_with_bom() -> None:
    chunks = load_knowledge_chunks(ROOT / "data" / "knowledge" / "knowledge_chunks.jsonl")
    summary = summarize_knowledge_chunks(chunks)
    assert summary["total_chunks"] >= 42
    assert "optical_physics" in summary["domain_counts"]


def test_auto_knowledge_query_retrieves_edfa_noise_feature() -> None:
    chunks = load_knowledge_chunks(ROOT / "data" / "knowledge" / "knowledge_chunks.jsonl")
    query = build_knowledge_query(
        device_type="edfa",
        performance_summary={
            "status": "ABNORMAL",
            "key_metric_changes": [
                {"device_type": "edfa", "metric": "output_gsnr_db", "direction": "decrease"},
                {"device_type": "edfa", "metric": "output_osnr_db", "direction": "decrease"},
            ],
        },
        event_summary={"events": [{"event_type": "PROVISIONED"}]},
    )
    results = search_knowledge_chunks(chunks, query, top_k=5)
    assert results
    assert any(item["chunk_id"] == "SIM_FAULT_EDFA_NOISE_001" for item in results)


def test_diagnosis_knowledge_query_does_not_inject_normal_signature() -> None:
    chunks = load_knowledge_chunks(ROOT / "data" / "knowledge" / "knowledge_chunks.jsonl")
    query = build_knowledge_query(
        device_type="edfa",
        performance_summary={
            "status": "NORMAL",
            "key_metric_changes": [
                {"device_type": "edfa", "metric": "output_gsnr_db", "direction": "decrease", "significant": False},
                {"device_type": "edfa", "metric": "output_osnr_db", "direction": "decrease", "significant": False},
            ],
        },
        event_summary={"events": [{"event_type": "PROVISIONED"}, {"event_type": "RELEASED"}]},
    )
    assert "normal_state_signature" not in query["query_text"]
    assert "NORMAL" not in query["terms"]
    results = search_knowledge_chunks(
        chunks,
        query,
        top_k=5,
        exclude_topics={"normal_state_signature"},
        exclude_chunk_ids={"SIM_FAULT_NORMAL_001"},
    )
    assert all(item["chunk_id"] != "SIM_FAULT_NORMAL_001" for item in results)


def test_signature_matching_excludes_current_run() -> None:
    library = {
        "signatures": {
            "FIBER_PMD_SURGE": {
                "count": 2,
                "samples": [
                    {"run_id": "current", "features": {"fiber.pmd_ps.increase": 10.0}},
                    {"run_id": "train", "features": {"fiber.pmd_ps.increase": 2.0}},
                ],
            }
        }
    }
    matches = classify_with_signature_library(
        {"fiber.pmd_ps.increase": 10.0},
        library,
        exclude_run_id="current",
    )

    assert matches[0]["support"] == 1
