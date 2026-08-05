from app.retrieval.bm25 import BM25Index
from app.retrieval.vector_store import VectorRecord


def test_search_ranks_exact_keyword_match_above_unrelated_text():
    index = BM25Index()
    index.index(
        [
            VectorRecord(
                id="errors/e502.md",
                vector=[],
                text="错误码 E502 表示网关超时，请检查上游服务是否可用。",
                metadata={},
            ),
            VectorRecord(
                id="faq/login.md",
                vector=[],
                text="登录失败请检查账号密码是否正确。",
                metadata={},
            ),
        ]
    )

    hits = index.search("E502 错误码是什么意思", top_k=2)

    assert hits[0].id == "errors/e502.md"
    assert hits[0].score > 0


def test_search_returns_empty_when_no_terms_match():
    index = BM25Index()
    index.index(
        [VectorRecord(id="a", vector=[], text="网络断开请重启路由器", metadata={})]
    )

    hits = index.search("完全无关的查询内容", top_k=2)

    assert hits == []
