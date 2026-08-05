from app.retrieval.fusion import reciprocal_rank_fusion


def test_document_hit_by_both_sources_outranks_single_source_hit():
    # c 在向量检索排第2，在 BM25 也排第2 -> 两路都命中
    # a 在向量检索排第1，但完全没被 BM25 检索到
    vector_ranked_ids = ["a", "c"]
    bm25_ranked_ids = ["b", "c"]

    fused = reciprocal_rank_fusion(vector_ranked_ids, bm25_ranked_ids)
    fused_ids = [item[0] for item in fused]

    assert fused_ids[0] == "c"


def test_returns_scores_in_descending_order():
    fused = reciprocal_rank_fusion(["x", "y", "z"], [])

    scores = [item[1] for item in fused]
    assert scores == sorted(scores, reverse=True)
