"""向量相似度的公共实现。

放在 app/retrieval 而不是 app/memory：调用方横跨两个包（记忆召回、记忆去重、
主动跟进扫描、向量库检索），而 memory -> retrieval 这条依赖边本来就存在，
反过来没有——放这边不新增任何包间依赖。

合并前这个函数在四个文件里各有一份逐字节相同的副本（app/memory/similarity.py、
app/memory/recall.py、app/memory/proactive_scan.py、app/retrieval/vector_store.py），
去掉空白后的哈希完全一致。没有任何机制保证它们同步演进，而这两个包都在注释里
写明"暂不引入 ANN 索引、先全量在 Python 里算"——也就是说它们迟早会撞上同一堵
扩展性墙，届时要改的是同一段数学，不该改四遍。
"""
from __future__ import annotations

import math


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """两个向量的余弦相似度，取值 [-1, 1]。

    任一向量为零向量时返回 0.0 而不是抛 ZeroDivisionError：记忆条目和向量库
    记录里都可能存在没写过 embedding 的历史数据，调用方按"分数低就排在后面"
    处理这种情况。

    长度不等时按较短的那个截断（zip 的语义），与合并前四份副本的行为一致。
    """
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)
