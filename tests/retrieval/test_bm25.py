import math

from app.retrieval.bm25 import BM25Index, _tokenize
from app.retrieval.vector_store import VectorRecord


def test_search_ranks_exact_keyword_match_above_unrelated_text():
    index = BM25Index()
    index.index(
        [
            VectorRecord(
                id="errors/e502.md",
                vector=[],
                text="错误码 E502 表示网关超时，请检查上游服务是否可用。",
                tenant_id="t1",
                metadata={},
            ),
            VectorRecord(
                id="faq/login.md",
                vector=[],
                text="登录失败请检查账号密码是否正确。",
                tenant_id="t1",
                metadata={},
            ),
        ]
    )

    hits = index.search("E502 错误码是什么意思", top_k=2, tenant_id="t1")

    assert hits[0].id == "errors/e502.md"
    assert hits[0].score > 0


def test_search_returns_empty_when_no_terms_match():
    index = BM25Index()
    index.index(
        [
            VectorRecord(
                id="a", vector=[], text="网络断开请重启路由器", tenant_id="t1", metadata={}
            )
        ]
    )

    hits = index.search("完全无关的查询内容", top_k=2, tenant_id="t1")

    assert hits == []


def test_search_does_not_return_hits_from_a_different_tenant():
    index = BM25Index()
    index.index(
        [
            VectorRecord(
                id="a",
                vector=[],
                text="错误码 E502 表示网关超时",
                tenant_id="t1",
                metadata={},
            ),
            VectorRecord(
                id="b",
                vector=[],
                text="错误码 E502 表示网关超时",
                tenant_id="t2",
                metadata={},
            ),
        ]
    )

    hits = index.search("E502 错误码", top_k=5, tenant_id="t1")

    assert [h.id for h in hits] == ["a"]


def test_index_incrementally_maintains_postings_and_doc_freq_per_tenant():
    """直接断言内部倒排表状态，不只测最终 search() 结果——方便日后如果
    排序结果算错了，能分清是"索引维护错了"还是"打分逻辑错了"。"""
    index = BM25Index()
    index.index(
        [VectorRecord(id="a", vector=[], text="网络故障", tenant_id="t1", metadata={})]
    )

    tenant = index._tenants["t1"]
    assert tenant.doc_freq["网"] == 1
    assert tenant.doc_freq["故"] == 1
    assert tenant.postings["网"] == {0}
    assert tenant.total_token_count == 4  # "网" "络" "故" "障" 四个单字 token

    # 增量追加第二条记录，验证是合并进已有租户索引，不是覆盖或重算
    index.index(
        [VectorRecord(id="b", vector=[], text="网络连接超时", tenant_id="t1", metadata={})]
    )

    tenant = index._tenants["t1"]
    assert tenant.doc_freq["网"] == 2  # 两条记录都含"网"
    assert tenant.postings["网"] == {0, 1}
    assert tenant.total_token_count == 4 + 6  # 第二条记录 6 个单字 token

    # 另一个租户的索引完全独立，不受 t1 的更新影响
    index.index(
        [VectorRecord(id="c", vector=[], text="网络故障", tenant_id="t2", metadata={})]
    )
    assert "t2" in index._tenants
    assert index._tenants["t1"].doc_freq["网"] == 2  # t1 的计数不受 t2 影响


def _reference_full_scan_bm25_search(
    records: list[VectorRecord],
    query: str,
    *,
    top_k: int,
    tenant_id: str,
    k1: float = 1.5,
    b: float = 0.75,
) -> list[tuple[str, float]]:
    """重构前 BM25Index.search() 的算法原样复制，作为独立参照实现——
    不调用被测的 BM25Index，只用来和新的倒排索引实现做差分对比，证明
    这次重构没有偷偷改变排序/打分语义。"""
    query_tokens = _tokenize(query)
    scoped = [
        (record, _tokenize(record.text))
        for record in records
        if record.tenant_id == tenant_id
    ]
    if not query_tokens or not scoped:
        return []

    scoped_docs = [tokens for _, tokens in scoped]
    doc_count = len(scoped_docs)
    avgdl = sum(len(d) for d in scoped_docs) / doc_count

    doc_freq: dict[str, int] = {}
    for tokens in scoped_docs:
        for token in set(tokens):
            doc_freq[token] = doc_freq.get(token, 0) + 1

    idf: dict[str, float] = {}
    for token in set(query_tokens):
        freq = doc_freq.get(token, 0)
        idf[token] = math.log(1.0 + (doc_count - freq + 0.5) / (freq + 0.5))

    scored: list[tuple[float, VectorRecord]] = []
    for record, tokens in scoped:
        if not tokens:
            continue
        term_freq: dict[str, int] = {}
        for token in tokens:
            term_freq[token] = term_freq.get(token, 0) + 1
        dl = len(tokens)
        score = 0.0
        for token in query_tokens:
            tf = term_freq.get(token, 0)
            if tf <= 0:
                continue
            denom = tf + k1 * (1.0 - b + b * (dl / avgdl))
            score += idf[token] * ((tf * (k1 + 1.0)) / denom)
        if score > 0:
            scored.append((score, record))

    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [(record.id, score) for score, record in scored[:top_k]]


_DIFFERENTIAL_TEST_RECORDS = [
    VectorRecord(id="t1-1", vector=[], text="网络故障排查手册第一章", tenant_id="t1", metadata={}),
    VectorRecord(id="t1-2", vector=[], text="登录失败排查手册", tenant_id="t1", metadata={}),
    VectorRecord(id="t1-3", vector=[], text="错误码E502网关超时排查", tenant_id="t1", metadata={}),
    VectorRecord(id="t1-4", vector=[], text="账号密码重置流程说明", tenant_id="t1", metadata={}),
    VectorRecord(id="t2-1", vector=[], text="网络故障排查手册第一章", tenant_id="t2", metadata={}),
    VectorRecord(id="t2-2", vector=[], text="发票开具流程说明文档", tenant_id="t2", metadata={}),
]

_DIFFERENTIAL_TEST_QUERIES = [
    ("网络故障怎么排查", "t1"),   # "排查" 在 t1 内是高频词（3 篇都有），弱区分度
    ("网络故障怎么排查", "t2"),   # 同一个查询在另一个租户，验证租户隔离下分数也独立
    ("错误码E502网关超时", "t1"),  # 混合中英文+数字的查询
    ("完全不存在的查询内容", "t1"),  # 查询词不在任何文档里，两边都应该返回空
    ("流程说明", "t1"),          # "流程说明" 在 t1/t2 各只有一篇命中，测试低频词
    ("流程说明", "t2"),
]


def test_new_implementation_matches_reference_full_scan_algorithm_exactly():
    index = BM25Index()
    index.index(_DIFFERENTIAL_TEST_RECORDS)

    for query, tenant_id in _DIFFERENTIAL_TEST_QUERIES:
        new_hits = index.search(query, top_k=10, tenant_id=tenant_id)
        new_result = [(hit.id, hit.score) for hit in new_hits]
        reference_result = _reference_full_scan_bm25_search(
            _DIFFERENTIAL_TEST_RECORDS, query, top_k=10, tenant_id=tenant_id
        )
        assert new_result == reference_result, (
            f"query={query!r} tenant_id={tenant_id!r}: "
            f"新实现={new_result} 参照实现={reference_result}"
        )


def test_search_on_empty_index_returns_empty():
    index = BM25Index()
    assert index.search("任意查询", top_k=5, tenant_id="t1") == []


def test_search_with_query_that_matches_no_document_returns_empty_without_scanning_error():
    index = BM25Index()
    index.index(
        [VectorRecord(id="a", vector=[], text="网络故障", tenant_id="t1", metadata={})]
    )
    # 查询词完全不在任何文档的 postings 里，candidate_indices 应该是空集，
    # 提前返回而不是继续往下跑除零之类的路径。
    assert index.search("完全无关内容", top_k=5, tenant_id="t1") == []


def test_search_with_single_document_corpus_does_not_divide_by_zero():
    index = BM25Index()
    index.index(
        [VectorRecord(id="a", vector=[], text="网络故障", tenant_id="t1", metadata={})]
    )
    hits = index.search("网络", top_k=5, tenant_id="t1")
    assert len(hits) == 1
    assert hits[0].id == "a"
