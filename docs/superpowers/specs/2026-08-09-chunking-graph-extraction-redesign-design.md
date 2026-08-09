# 分块与知识图谱关系抽取重新设计

> 状态：设计定稿（经用户 grill-me 逐题确认）
> 背景：现有分块（`app/ingestion/chunking.py` 及各格式 parser）完全没有尺寸上限——某个标题下正文很长、或整篇文档没有任何标题时，会产出一个巨大的 chunk，稀释 embedding 语义。图谱关系抽取（`app/ingestion/graph_extraction.py`）逐 chunk 串行发起 LLM 调用，沿用了项目里"实时对话链路"的 2 秒超时惯例，但摄取其实是后台任务，没有用户在等；关系类型词表只有 `RELATED_TO`/`BELONGS_TO_MODULE` 两种，语义过于单薄。用户澄清实际业务场景可能是门店/酒店客服（而非软件客服，`terminology.yaml` 里的错误码/模块只是占位示例），要求分块和图谱抽取都朝"效果最好、效率最快、方案科学合理"重新设计，同时明确要求维持现有"封闭词表 + 人工审核"精度优先架构，只做效率层面的现代化，不引入开放式实体发现（LightRAG/微软 GraphRAG 那类社区检测+全局摘要方案评估后判定与本项目"专有名词不能编造"的核心要求冲突，不采用）。

## 1. 现状与复用点

- `chunking.py::chunk_markdown`、`docx_parser.py::parse_docx`、`pdf_parser.py::parse_pdf`、`ocr_parser.py::parse_image`、`ticket_parser.py::parse_ticket_csv` 分别按标题/页/表格行/整图/工单行产出 `Chunk`（`text`+`heading_path`+`source`+可选 `parent_text`），是"结构感知分块"的现有实现，本次不推翻。
- `pipeline.py::_ingest_chunks` 把同一份 `chunks` 列表分别喂给 `_embed_and_upsert`（写向量库）和 `_maybe_extract_graph_relations`（写图谱），两条路径目前吃的是同一份切分粒度。
- `graph_extraction.py::extract_and_write_graph_relations` 对每个 chunk 串行调用 `llm_extractor.py::extract_candidate_relations`（每次一个 chunk 的 `text`），抽取结果交给 `normalization.py::normalize_and_write_relations` 做术语表对齐，未对齐的候选转入 `review_queue.py` 人工审核队列或直接丢弃（沿用现有机制，本次不改）。
- `neo4j_client.py::_ALLOWED_RELATION_TYPES` 是关系类型白名单，`merge_relation` 用它拒绝非法类型；`query_subgraph` 是 1 跳邻居查询，被 `term_guard.py::build_term_guard_context`（强制注入路径）和 `agent/tools.py::graph_query_tool`（Agent 自主查询路径）两处复用。
- 关系写入按 `source`（整篇文档路径）+`tenant_id` 做溯源和重摄取时的清理（`delete_relations_by_source`），**不依赖 chunk 粒度**——这是本次能"合并多个 chunk 进一次 LLM 调用"而不破坏任何现有行为的前提。

## 2. 分块：结构优先 + 尺寸兜底

### 2.1 双视图分叉

`_ingest_chunks` 改造后产出两条独立粒度：

- **原始结构 chunk**（各 parser 现有产出，不做任何改动）→ 喂给图谱抽取，保留最大上下文完整性——LLM 抽取关系比 embedding 更需要完整段落，不应该被尺寸兜底切碎。
- **embedding chunk**（原始结构 chunk 经过尺寸兜底二次切分后的结果）→ 只用于 `_embed_and_upsert` 写向量库。

`_ingest_chunks` 内部变化：

```python
embedding_chunks = split_oversized_chunks(chunks)  # 新函数，见 2.2
count = await _embed_and_upsert(embedding_chunks, path, ...)
await _maybe_extract_graph_relations(chunks, ...)  # 仍然吃原始 chunks，不变
```

### 2.2 新函数：`chunking.py::split_oversized_chunks`

```python
def split_oversized_chunks(
    chunks: list[Chunk], *, max_len: int = 800, overlap: int = 90
) -> list[Chunk]
```

- 遍历输入的 `chunks`，`len(chunk.text) <= max_len` 或 `chunk.parent_text is not None` 的原样保留（parent-child chunk 如 PDF 表格行本来就很小，且二次拆分会破坏 parent-child 对应关系，直接跳过）。
- 超过阈值的按**递归三级策略**切：优先按空行/段落边界（`\n\n`）切；单段仍超阈值则按中文句末标点（`。！？`）切；单句仍超阈值才硬按字符数截断。
- 阈值用**字符数**而非 token 数——项目里没有任何 tokenizer 依赖，中文语料下字符数和 token 数近似成正比，不需要为了"精确对齐某个 tokenizer"引入新依赖（引入了也对不上 Qwen 系列模型的真实分词器，是假精确）。
- 同一个原始 chunk 内部切出的子 chunk 之间，前后各带约 90 字符重叠，避免硬切边界正好切在关键信息中间；**不同原始 chunk 之间不重叠**（不然会打乱 `heading_path` 溯源）。
- 子 chunk 的 `heading_path`/`source` 与原始 chunk 保持一致，`parent_text` 留空（本身就是 embedding 专用的最细粒度文本）。
- 800 字符 / 90 字符重叠都是参考起点，不是通过真实数据标定的权威值，写在函数 docstring 里说明。

### 2.3 影响范围

- `pipeline.py::_ingest_chunks` 是唯一改动点，五个 `ingest_*_file` 上层函数签名不变。
- `chunk_markdown`/`parse_docx`/`parse_pdf`/`parse_image`/`parse_ticket_csv` 自身**不改动**——尺寸兜底是在它们的输出之上叠加的后处理层，不侵入各自的结构切分逻辑。

## 3. 图谱抽取：批量化 + 并发 + 独立超时

### 3.1 超时从实时对话链路的 2 秒惯例中解耦

项目里 `timeout_sec=2.0` 是"实时对话请求里 LLM 辅助步骤必须快速失败"的统一惯例（`query_rewrite`/`fact_extractor`/`semantic_review` 等都用这个值），但图谱抽取只发生在摄取的后台任务（`process_pending_jobs`）里，没有用户在等。`extract_candidate_relations`/`extract_and_write_graph_relations` 的 `timeout_sec`/`extract_timeout_sec` 默认值从 `2.0` 改为 **`30.0`**——这两个函数目前只有摄取路径这一个生产调用方（`grep` 确认过），改默认值不影响其它模块。

### 3.2 按字符预算攒批，减少 LLM 调用次数

`llm_extractor.py::extract_candidate_relations` 签名改为接受多个片段：

```python
async def extract_candidate_relations(
    segments: list[str],
    *,
    llm_registry: ProviderRegistry,
    llm_provider_name: str,
    timeout_sec: float = 30.0,
) -> list[dict[str, str]]
```

- `len(segments) == 1` 时直接把该片段当 user message 发送（不加分隔标记，兼容现有单片段场景的 prompt 简洁性）。
- `len(segments) > 1` 时按 `[片段1]\n{text}\n\n[片段2]\n{text}...` 格式拼接，system prompt 追加一条指令："只抽取同一个片段内部出现的关系，不要把不同片段里的实体强行关联起来"，防止 LLM 把毫不相关的片段内容编造成跨片段关系。
- system prompt 同时把关系类型词表从 2 种扩到第 4 节定义的 10 种，每种配一个极简中文示例短语（如 `PART_OF: "客房 PART_OF 酒店"`）而不是完整 few-shot 例句——控制每次调用的固定 token 开销，避免类型变多导致语义混淆和"完整例句每种配 2-3 个"的高开销之间失衡。

`graph_extraction.py::extract_and_write_graph_relations` 新增按字符预算攒批的私有函数：

```python
def _batch_chunks_by_char_budget(
    chunks: list[Chunk], *, max_chars: int = 3000
) -> list[list[Chunk]]
```

依次把 chunk 塞进当前批次，累计字符数超过 `max_chars` 就切下一批；单个 chunk 本身已经超过 `max_chars` 时自己单独成一批（不因为攒批逻辑被拆散或跳过）。3000 字符是参考起点，不是标定值。

### 3.3 批次间并发（新引入 `asyncio.Semaphore` 模式）

`extract_and_write_graph_relations` 新增 `max_concurrency: int = 8` 参数，用 `asyncio.Semaphore(max_concurrency)` 控制同时在途的批次数：

```python
semaphore = asyncio.Semaphore(max_concurrency)

async def _process_batch(batch: list[Chunk]) -> list[dict[str, str]]:
    async with semaphore:
        return await extract_candidate_relations(
            [c.text for c in batch],
            llm_registry=llm_registry,
            llm_provider_name=llm_provider_name,
            timeout_sec=extract_timeout_sec,
        )

batches = _batch_chunks_by_char_budget(chunks, max_chars=batch_max_chars)
all_relation_lists = await asyncio.gather(*(_process_batch(b) for b in batches))

# 写入阶段（Neo4j + 人工审核队列）保持严格顺序执行，不并发——
# review_conn 是调用方传入的单个 aiosqlite 连接，在多个协程间并发
# execute/commit 是否安全没有把握验证过，不值得为了这一步的效率
# 冒险；真正的效率瓶颈是网络 IO 占主导的 LLM 调用，并发只用在那一步。
total_written = 0
for relations in all_relation_lists:
    total_written += await normalize_and_write_relations(
        relations, terms=terms, graph_client=graph_client,
        source=source, tenant_id=tenant_id, review_conn=review_conn,
    )
return total_written
```

这是项目里第一次引入有并发上限的调度模式（此前唯一的并发案例 `hybrid_search.py` 是固定 2-3 条分支的 `asyncio.gather`，没有处理过"批次数量不固定、可能是几十批"的场景，直接无上限 `gather` 会把几十个并发请求同时砸向 LLM 供应商 API，大概率触发限流）。`delete_relations_by_source` 仍然在批处理开始前调用一次（不变）。并发只作用于 LLM 抽取这一步（无共享可变状态，天然安全）；写入 Neo4j/审核队列的步骤保持顺序执行，避免引入"多协程并发操作同一个 aiosqlite 连接是否安全"这个没有把握的未知数。

**已知的、被接受的代价**：合并批次后，一批失败会丢整批涉及 chunk 的关系（现状是一个 chunk 失败只丢一个 chunk）。因为已经把超时放宽到 30 秒，真正因超时失败的概率会明显下降；即使某批确实失败，也只影响这批的关系抽取，不影响向量检索这条客服问答的核心链路，下次重新摄取该文档时会重新尝试。

## 4. 关系类型词表：扩到 10 种跨领域通用拓扑关系

`neo4j_client.py::_ALLOWED_RELATION_TYPES` 从 `{RELATED_TO, BELONGS_TO_MODULE}` 改为：

```python
_ALLOWED_RELATION_TYPES = frozenset({
    "RELATED_TO",      # 兜底：弱关联，语义不明确时的默认选项
    "PART_OF",         # 部分-整体（取代 BELONGS_TO_MODULE，语义是其超集）
    "IS_A",            # 类别从属/分类层级
    "REQUIRES",        # 前提/依赖
    "ALTERNATIVE_TO",  # 替代/类似
    "CAUSES",          # 因果
    "ADDRESSED_BY",    # 问题→解决方案（原提案叫 RESOLVED_BY，用户改名）
    "LOCATED_IN",      # 空间/组织归属
    "APPLIES_TO",      # 适用范围（政策/优惠/规则作用于谁）
    "PRECEDES",        # 流程先后顺序
})
```

10 种词表刻意不含任何行业色彩（不是"错误码/模块"这类软件运维语义，也不是"房型/商品"这类酒店/门店专属语义）——领域信息由术语表的 `term_type`/`product_line` 字段和具体标准名称承载，关系类型词表本身保持跨租户通用，适配当前多租户架构（不同租户可能是完全不同的业务领域）。

`BELONGS_TO_MODULE` → `PART_OF` 是**清理式切换，不写迁移脚本**——用户确认本地 Neo4j 无真实数据需要迁移，涉及的测试断言（`tests/graphrag/test_normalization.py`、`llm_extractor.py` 的 prompt 词表）直接同步改名。

## 5. 查询时：链式关系放开到 2 跳

`neo4j_client.py::query_subgraph` 现状是纯 1 跳邻居查询（`MATCH (t)-[r]-(related)`），关系类型只有 2 种时够用；扩到 10 种之后，`REQUIRES`/`PRECEDES`/`PART_OF` 这三种"链式"关系语义上经常需要连续追问两步才能拿到完整信息（比如"办理 A 业务" `REQUIRES` "满足条件 B"，B 本身又 `REQUIRES` "先完成 C"）。

调整为：1 跳查询覆盖全部关系类型（现有行为不变），额外追加一次**只对链式关系类型放开的 2 跳查询**：

```cypher
-- 1 跳（现状不变，全部关系类型）
MATCH (t:Term {standard_name: $standard_name})-[r]-(related:Term)
WHERE r.tenant_id = $tenant_id
RETURN related.standard_name AS related_name, type(r) AS relation_type, 1 AS hops

UNION

-- 新增：恰好 2 跳，仅链式关系类型
MATCH (t:Term {standard_name: $standard_name})-[r:REQUIRES|PRECEDES|PART_OF*2..2]-(related:Term)
WHERE ALL(rel IN r WHERE rel.tenant_id = $tenant_id)
RETURN related.standard_name AS related_name,
       [rel IN r | type(rel)][-1] AS relation_type,
       2 AS hops
```

- 第二段用 `*2..2`（恰好 2 跳，不是 `*1..2`）避免和第一段的 1 跳结果重复。
- **`ALL(rel IN r WHERE rel.tenant_id = $tenant_id)`——2 跳路径上的每一条边都必须属于同一租户**，不能只查中间/首尾某一条边的 `tenant_id`。标准节点（`:Term`）本身不区分租户、可能被多个租户共用，如果只检查其中一跳，2 跳路径有可能"借道"另一个租户写入的边，把不该出现的信息泄露给当前租户。这是本次设计里唯一一个如果实现疏忽会导致真实安全问题的点，必须在实现和测试里明确覆盖。
- `query_subgraph` 返回值新增 `hops` 字段（`1` 或 `2`），`related_name`/`relation_type` 两个既有字段不变——`term_guard.py`/`agent/tools.py` 两个消费方现有的字典取值方式不受影响（多一个字段不影响 `row['related_name']` 这类既有访问）。
- `term_guard.py::build_term_guard_context` 组织注入上下文时读取 `row.get('hops', 1)`，`hops=1` 沿用现有"关联"措辞，`hops=2` 用"间接关联（经过 N 跳）"标注，让 LLM 能区分直接事实和推导出的间接事实，不至于把两者当同等确定性的信息。

## 6. 测试

- `chunking.py::split_oversized_chunks`：未超阈值的 chunk 原样返回；超阈值按段落/句子/硬切三级递归正确切分；`parent_text` 非空的 chunk 跳过不处理；子 chunk 之间的重叠长度符合预期；`heading_path`/`source` 在子 chunk 上保持一致。
- `pipeline.py::_ingest_chunks`：新增集成测试验证"图谱抽取收到的是未切分的原始 chunks，向量库收到的是切分后的 embedding chunks"（用一个刻意超过 800 字符阈值的 chunk 构造，断言两条路径拿到的 chunk 数量/文本不同）。
- `llm_extractor.py::extract_candidate_relations`：单片段场景现有测试改成传 `[text]` 保持通过；新增多片段场景测试（验证分隔标记、验证"不臆造跨片段关系"这条 prompt 指令确实被拼进 system prompt）。
- `graph_extraction.py`：新增 `_batch_chunks_by_char_budget` 的攒批边界测试（单个超大 chunk 独立成批、多个小 chunk 累计到刚好超阈值时切批）；新增并发场景测试（用一个记录"同时在途请求数"的 fake LLM provider，验证同时在途数不超过 `max_concurrency`）；现有的 4 个 `extract_and_write_graph_relations` 集成测试需要适配新的批量签名但断言的行为（写入图谱的内容、审核队列分流、跨租户隔离、重摄取清旧边）保持不变。
- `neo4j_client.py::query_subgraph`：现有 1 跳测试保持通过；新增 2 跳查询的 Cypher 结构测试（用现有 `FakeSession`/`FakeDriver` 模式，断言生成的查询字符串包含 `REQUIRES|PRECEDES|PART_OF` 和 `tenant_id` 过滤）；新增关系类型白名单包含全部 10 种、拒绝旧的 `BELONGS_TO_MODULE` 的测试。
- `term_guard.py::build_term_guard_context`：新增"`hops=2` 时上下文里带"间接关联"措辞"的测试；确认 `hops=1`/无 `hops` 字段时现有措辞不受影响（向后兼容）。
- `tests/graphrag/test_normalization.py` 里引用 `BELONGS_TO_MODULE` 的断言同步改成 `PART_OF`。

## 7. 范围之外（不做）

- 不引入开放式实体发现（LightRAG/微软 GraphRAG 的社区检测+分层摘要）——与"专有名词不能编造"的核心要求冲突，评估后明确排除，见本文档开头背景说明。
- 不做 Global Search（全局性摘要问答）——客服问答目前都是局部问题（"这个术语/关系是什么"），没有"整个知识库讲了什么"这类全局性问题的真实需求。
- 不给 `approve_review()` 的人工审核路径加术语表校验——人工输入的标准名本来就是审核环节的最终权威，不在本次范围内讨论（且这是另一个此前审查已经记录过的独立话题）。
- 不做 800 字符阈值/3000 字符批量预算/并发上限 8/相似度阈值等具体数字的自动调优或 A/B 测试——均为参考起点，需要接入真实业务数据后再复核，不是本次交付物。
- 不更新 `terminology.yaml`、`data/uploads` 示例文档、前端演示文案（`ChatWindow`/`Hero` 里"网关超时示例"这类软件客服措辞）以匹配酒店/门店场景——这些是独立于本次分块/图谱抽取机制重新设计的示例内容更新，不在本次范围。
- 不做 PDF 内部标题层级识别（当前按页切分，页内不感知视觉标题）——超出本次"尺寸兜底"这个具体问题的范围，是分块结构感知能力本身的进一步增强，留待后续单独评估。
- 不接入 `app/eval/runner.py` 做上线前 A/B 对比——项目里没有现成的、覆盖图谱关系问题的评测数据集（现有 eval 基础设施面向端到端 Agent 问答场景），本次改动的正确性由第 6 节的单元/集成测试覆盖，不做端到端评测门槛。
