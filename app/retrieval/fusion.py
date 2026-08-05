from __future__ import annotations


def reciprocal_rank_fusion(
    *ranked_id_lists: list[str], k: int = 60
) -> list[tuple[str, float]]:
    """RRF 融合：只依据各路排名位置计算分数，不比较原始分数量纲。

    score(id) = sum(1 / (k + rank)) across all ranked lists that contain it,
    rank 从 0 开始计。返回按融合分数降序排列的 (id, score) 列表。
    """
    scores: dict[str, float] = {}
    for ranked_ids in ranked_id_lists:
        for rank, doc_id in enumerate(ranked_ids):
            scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (k + rank)
    return sorted(scores.items(), key=lambda item: item[1], reverse=True)
