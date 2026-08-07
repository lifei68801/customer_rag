# 检索层修正设计方案（rerank分数回写 + 三路并行化）

> 状态：设计定稿（经用户确认）
> 背景：架构覆盖度审计发现 `app/retrieval/hybrid_search.py` 存在两处与设计文档（`docs/ARCHITECTURE.md` §5）不一致的地方：① Fallback 触发依据的置信度分数用的是向量原始余弦相似度，rerank 之后的 cross-encoder 精排分数没有回写覆盖，意味着"要不要转人工"的判断实际上没有用到精排结果，是真实的行为偏差；② 原始query向量检索、改写query向量检索、BM25检索三路检索是顺序执行（for/await），不是设计要求的并行执行。这是四个待补事项中拆出的第一个独立子项目，优先做是因为改动小、机制清楚、是真实的行为 bug 而非架构性缺口。

## 1. 现状

`hybrid_search`（`app/retrieval/hybrid_search.py`）：

- 原始 query 与改写 query（如果改写触发）的向量检索在一个 `for text in query_texts` 循环里顺序执行，各自都是 `embed → search` 两次 `await`（各一次真实网络 IO：一次 embedding API、一次 Milvus 查询）；BM25 检索（`bm25_index.search`）是同步内存操作，本身几乎不耗时。
- 融合（RRF）后如果配置了 `rerank_provider`，会调用 rerank 拿到 `RerankResult.hits`（每个 `RerankHit` 有 `index`/`relevance_score`），但最终 `return [fused_records[hit.index] for hit in rerank_result.hits[:final_top_k]]` 直接返回融合阶段的 `VectorRecord`，其 `.score` 字段还是融合前的向量检索原始相似度（`VectorRecord` 是 frozen dataclass，`.score: float | None`）；`rerank_result.hits[i].relevance_score` 从未被写回。
- 下游 `app/agent/graph.py::route_after_retrieval` 用 `max(record.score for record in records if record.score is not None)` 和 `agent_min_relevance_score`（`app/config/settings.py`，默认 `None`，未开启）比较，决定是否转 Fallback（人工工单）。当前这个判断依据的是向量相似度，不是精排分数。

已核实代码库里没有任何地方真正配置 `agent_min_relevance_score`（默认 `None`），所以这不是一个"正在生产环境生效但语义错误"的问题，而是一个"功能尚未真正启用、但启用时行为会不对"的问题——现在修正成本最低。

## 2. 设计

### 2.1 rerank 分数回写

`hybrid_search` 末尾 rerank 分支，把返回值从直接引用融合阶段记录改为用 `dataclasses.replace(record, score=hit.relevance_score)` 生成新记录，`.score` 覆盖为精排分数：

```python
return [
    dataclasses.replace(fused_records[hit.index], score=hit.relevance_score)
    for hit in rerank_result.hits[:final_top_k]
]
```

**语义变化（已与用户确认可接受）**：`agent_min_relevance_score` 阈值判断依据从"向量余弦相似度（0-1 有界）"变为"rerank cross-encoder 分数（不同供应商范围/语义可能不同）"。因为当前没有任何地方真正配置这个阈值，这个变化没有破坏现有部署行为；但会在 `settings.py` 的字段注释里补一句说明，提醒未来配置这个阈值时要参照实际接入的 rerank 模型的分数分布重新标定，不能照搬向量相似度的经验值。

**副作用（正面）**：BM25-only 命中的记录（`.score` 原本恒为 `None`，因为它们没走向量检索）经过 rerank 后也会获得真实分数，不再被 `route_after_retrieval` 的 `if record.score is not None` 过滤条件跳过。

未走 rerank 分支（`rerank_provider is None`）的返回路径不变，`.score` 保持融合前的原始值——没有精排就没有精排分数可用，语义上合理，不属于本次修正范围。

### 2.2 三路并行化

`query_texts` 循环里的向量检索链路（1-2 条，取决于改写是否触发）改用 `asyncio.gather` 并发执行，而不是顺序 `await`：

```python
async def _vector_search_for_text(text: str) -> list[VectorRecord]:
    embed_result = await embedding_registry.run(
        EmbeddingRequest(texts=[text]), provider_name=embedding_provider_name,
    )
    return await vector_store.search(
        embed_result.vectors[0], top_k=vector_top_k, tenant_id=tenant_id
    )

per_text_hits = await asyncio.gather(
    *(_vector_search_for_text(text) for text in query_texts)
)
```

融合结果的 `ranked_id_lists` 顺序仍保持和 `query_texts` 一致（`asyncio.gather` 保序返回），不影响 RRF 融合结果（RRF 不依赖多路列表之间的相对顺序，只依赖各自列表内部的排名）。

BM25 检索保持同步调用、不纳入 `gather`——它是内存操作，没有 IO 等待，纳入并发调度反而增加复杂度、没有实际收益。

## 3. 测试

- 新增测试验证 rerank 后返回记录的 `.score` 确实等于对应 `RerankHit.relevance_score`，复用现有 `test_hybrid_search_uses_rerank_order_when_provided` 用的 `FakeRerankProvider` 模式（该测试已经验证了顺序来自 rerank，新增测试补上分数本身的断言）。
- 并行化本身不改变可观察的融合结果（候选集合、最终排序都不变），现有测试应该原样通过，作为回归验证。不额外写"验证真的并发执行"的时序类测试——这类测试价值有限（容易 flaky，且当前架构下候选数量小，并发收益本身也有限，属于代码质量改进而非性能关键路径），行为不回归即视为通过。

## 4. 范围之外（不做）

- 不改动 BM25 检索本身的并发调度。
- 不新增独立的 `rerank_score` 字段（已与用户确认直接改变 `.score` 语义，不做字段拆分）。
- 不处理 HyDE（架构文档标注为可选进阶项，不在四个待补事项范围内）。
- 不重新标定 `agent_min_relevance_score` 的具体数值——没有实际部署依赖这个值，标定需要结合真实接入的 rerank 模型效果，不在本次代码改动范围内。
