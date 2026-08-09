# 分块与知识图谱关系抽取重新设计 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 给分块流水线加尺寸兜底、给图谱关系抽取加批量+并发效率优化、把关系类型词表从 2 种扩到 10 种跨领域通用拓扑关系，并让图查询支持链式关系的 2 跳遍历。

**Architecture:** 分块保持"结构优先"不变，新增一个后处理层只作用于 embedding 路径；图谱抽取从"逐 chunk 串行"改成"按字符预算攒批 + Semaphore 限流并发抽取 + 顺序写入"；`neo4j_client.py` 扩充关系类型白名单并把 1 跳查询改成"1 跳全类型 UNION 2 跳链式类型"。

**Tech Stack:** Python 3.12 / asyncio / pytest-asyncio / Neo4j Cypher（无 APOC 依赖）

**设计文档：** `docs/superpowers/specs/2026-08-09-chunking-graph-extraction-redesign-design.md`（本计划的所有数字/取舍理由都来自这份文档，实施时如有疑问以它为准）

## Global Constraints

- 分块尺寸兜底阈值：**800 字符**，子 chunk 间重叠 **90 字符**（字符数计量，不用 token）。
- 图谱抽取超时：**30 秒**（`extract_timeout_sec`/`timeout_sec` 默认值，从原来复用的 2 秒实时对话链路惯例中解耦）。
- 攒批字符预算：**3000 字符**（`batch_max_chars`）。
- 批次并发上限：**8**（`max_concurrency`，`asyncio.Semaphore`）。
- 关系类型白名单固定为这 10 种：`RELATED_TO`、`PART_OF`、`IS_A`、`REQUIRES`、`ALTERNATIVE_TO`、`CAUSES`、`ADDRESSED_BY`、`LOCATED_IN`、`APPLIES_TO`、`PRECEDES`；`BELONGS_TO_MODULE` 清理式退休，不做迁移。
- 多跳查询只对 `REQUIRES`/`PRECEDES`/`PART_OF` 放开到恰好 2 跳，其余关系类型保持 1 跳；2 跳路径必须用 `ALL(rel IN r WHERE rel.tenant_id = $tenant_id)` 校验路径上每一条边的租户归属。
- 图谱抽取永远吃未经尺寸切分的原始结构 chunk；embedding 永远吃尺寸兜底切分后的 chunk。两条路径不再共享同一份切分粒度。
- 不引入开放式实体发现/社区检测/Global Search；不做 `terminology.yaml`/前端演示文案更新；不做本计划涉及数字的自动调优。

---

### Task 1: 分块尺寸兜底 — `chunking.py::split_oversized_chunks`

**Files:**
- Modify: `app/ingestion/chunking.py`
- Test: `tests/ingestion/test_chunking.py`

**Interfaces:**
- Produces: `def split_oversized_chunks(chunks: list[Chunk], *, max_len: int = 800, overlap: int = 90) -> list[Chunk]`（供 Task 2 在 `pipeline.py` 里调用）

- [ ] **Step 1: 写失败测试**

把 `tests/ingestion/test_chunking.py` 顶部的 `from app.ingestion.chunking import chunk_markdown` 改成：

```python
from app.ingestion.chunking import Chunk, chunk_markdown, split_oversized_chunks
```

然后在文件末尾追加：

```python
def test_chunk_under_threshold_is_returned_unchanged():
    chunk = Chunk(text="短文本", heading_path=["标题"], source="a.md")

    result = split_oversized_chunks([chunk], max_len=800)

    assert result == [chunk]


def test_chunk_with_parent_text_is_never_split():
    """parent-child chunk（比如 PDF 表格行）本来就很小，且二次拆分会
    破坏 parent-child 对应关系，即使文本超阈值也要跳过。"""
    long_text = "x" * 1000
    chunk = Chunk(
        text=long_text, heading_path=[], source="a.md", parent_text="完整表格文本"
    )

    result = split_oversized_chunks([chunk], max_len=800)

    assert result == [chunk]


def test_oversized_chunk_splits_on_paragraph_boundaries():
    text = ("第一段。" * 50) + "\n\n" + ("第二段。" * 50)
    chunk = Chunk(text=text, heading_path=["标题"], source="a.md")

    result = split_oversized_chunks([chunk], max_len=120, overlap=0)

    assert len(result) > 1
    for piece in result:
        assert piece.heading_path == ["标题"]
        assert piece.source == "a.md"
        assert piece.parent_text is None


def test_sub_chunks_include_overlap_from_previous_piece():
    text = "。".join(f"第{i}句内容比较长一些用于测试重叠效果" for i in range(30))
    chunk = Chunk(text=text, heading_path=[], source="a.md")

    result = split_oversized_chunks([chunk], max_len=100, overlap=20)

    assert len(result) > 1
    # 第二个子 chunk 应该以第一个子 chunk 结尾的 20 个字符开头
    assert result[1].text.startswith(result[0].text[-20:])


def test_falls_back_to_hard_cut_when_no_punctuation_or_paragraph_breaks():
    text = "a" * 2000
    chunk = Chunk(text=text, heading_path=[], source="a.md")

    result = split_oversized_chunks([chunk], max_len=500, overlap=0)

    assert len(result) == 4
    assert all(len(piece.text) <= 500 for piece in result)


def test_multiple_chunks_are_each_split_independently():
    """不同结构单元（比如两个不同的 ## 标题）之间不应该互相拼接/重叠。"""
    short_chunk = Chunk(text="短的", heading_path=["A"], source="a.md")
    long_chunk = Chunk(text="超长" * 500, heading_path=["B"], source="a.md")

    result = split_oversized_chunks([short_chunk, long_chunk], max_len=800)

    assert result[0] == short_chunk
    assert len(result) > 2
    assert all(piece.heading_path == ["B"] for piece in result[1:])
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/ingestion/test_chunking.py -v`
Expected: 新增的 6 个测试全部 FAIL，报 `ImportError: cannot import name 'split_oversized_chunks'`

- [ ] **Step 3: 实现 `split_oversized_chunks`**

在 `app/ingestion/chunking.py` 的 `chunk_markdown` 函数后面追加（`import re` 已经在文件顶部）：

```python
def _greedy_merge(pieces: list[str], *, join: str, max_len: int) -> list[str]:
    """把切出来的小片段依次贪心拼接，尽量凑到接近 max_len 但不超过；
    单个片段本身已经超过 max_len 时原样保留（不在这一步再切，交给调用方
    用更细的分隔符继续递归）。
    """
    merged: list[str] = []
    current = ""
    for piece in pieces:
        candidate = f"{current}{join}{piece}" if current else piece
        if len(candidate) <= max_len:
            current = candidate
            continue
        if current:
            merged.append(current)
        current = piece
    if current:
        merged.append(current)
    return merged


def _split_text_recursive(text: str, *, max_len: int) -> list[str]:
    """递归三级切分：段落（\\n\\n）-> 中文句末标点 -> 硬按字符数截断。
    每一级先贪心合并到接近 max_len，合并后仍超阈值的单个片段再用下一级
    更细的分隔符继续递归；硬切这一级没有更细的分隔符可用，直接截断，
    保证递归一定收敛。
    """
    if len(text) <= max_len:
        return [text]

    paragraphs = [p for p in text.split("\n\n") if p.strip()]
    if len(paragraphs) > 1:
        merged = _greedy_merge(paragraphs, join="\n\n", max_len=max_len)
        result: list[str] = []
        for piece in merged:
            result.extend(_split_text_recursive(piece, max_len=max_len))
        return result

    sentences = [s for s in re.split(r"(?<=[。！？])", text) if s.strip()]
    if len(sentences) > 1:
        merged = _greedy_merge(sentences, join="", max_len=max_len)
        result = []
        for piece in merged:
            result.extend(_split_text_recursive(piece, max_len=max_len))
        return result

    return [text[i : i + max_len] for i in range(0, len(text), max_len)]


def _add_overlap(pieces: list[str], *, overlap: int) -> list[str]:
    """同一个原始 chunk 内部切出的子片段之间加小段重叠，避免硬切边界
    正好切在关键信息中间。第一个子片段不加前缀（它前面没有"上一段"）。
    """
    if overlap <= 0 or len(pieces) <= 1:
        return pieces
    result = [pieces[0]]
    for i in range(1, len(pieces)):
        prev_tail = pieces[i - 1][-overlap:]
        result.append(prev_tail + pieces[i])
    return result


def split_oversized_chunks(
    chunks: list[Chunk], *, max_len: int = 800, overlap: int = 90
) -> list[Chunk]:
    """尺寸兜底：结构感知分块本身没有尺寸上限，某个标题下正文很长、或
    整篇没有任何标题时会产出巨大的 chunk，稀释 embedding 语义。这里对
    超过 max_len 的 chunk 做递归二次切分，只用于 embedding 路径——图谱
    抽取需要更完整的上下文，应该继续吃未经切分的原始 chunk（调用方
    不要把这个函数的输出传给图谱抽取）。

    parent_text 非空的 chunk（PDF 表格行）原样跳过，不做二次切分——那些
    本来就很小，且切分会破坏 parent-child 对应关系。

    不同原始 chunk 之间不重叠、不合并，重叠只发生在"同一个原始 chunk
    内部被迫二次切分"这种情况，否则 heading_path 溯源会失真。

    800/90 这两个默认值是参考起点，不是通过真实数据标定的权威值。
    """
    result: list[Chunk] = []
    for chunk in chunks:
        if chunk.parent_text is not None or len(chunk.text) <= max_len:
            result.append(chunk)
            continue
        pieces = _split_text_recursive(chunk.text, max_len=max_len)
        pieces = _add_overlap(pieces, overlap=overlap)
        for piece in pieces:
            result.append(
                Chunk(text=piece, heading_path=chunk.heading_path, source=chunk.source)
            )
    return result
```

- [ ] **Step 4: 运行测试确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/ingestion/test_chunking.py -v`
Expected: 全部 PASS（包括原有的 2 个测试）

- [ ] **Step 5: 提交**

```bash
git add app/ingestion/chunking.py tests/ingestion/test_chunking.py
git commit -m "feat(ingestion): add size-bounded fallback split for oversized chunks"
```

---

### Task 2: 双视图分叉 — `pipeline.py::_ingest_chunks` 接入尺寸兜底

**Files:**
- Modify: `app/ingestion/pipeline.py:93-131`（`_ingest_chunks` 函数）
- Test: `tests/ingestion/test_ingest_pipeline.py`

**Interfaces:**
- Consumes: `split_oversized_chunks(chunks: list[Chunk], *, max_len: int = 800, overlap: int = 90) -> list[Chunk]`（Task 1）
- Produces: `_ingest_chunks` 对外行为不变（签名不变），但内部向量化路径和图谱抽取路径吃到的 chunk 粒度不再相同

- [ ] **Step 1: 写失败测试**

在 `tests/ingestion/test_ingest_pipeline.py` 的 `FakeGraphClient` 类定义之后追加：

```python
async def test_ingest_markdown_file_splits_only_the_embedding_path_not_graph_extraction(
    tmp_path,
):
    """超过尺寸阈值的正文只影响写入向量库的粒度，图谱抽取仍然拿到完整的
    原始 chunk 文本——见设计文档第 2.1 节的"双视图分叉"。
    """
    long_body = "网关超时示例通常与示例认证模块相关。" * 60  # 远超 800 字符阈值
    md_file = tmp_path / "long.md"
    md_file.write_text(f"## 网络故障\n{long_body}\n", encoding="utf-8")

    embedding_registry = EmbeddingRegistry()
    embedding_registry.register("fake-embedding", FakeEmbeddingProvider())
    vector_store = InMemoryVectorStore()

    llm_registry = ProviderRegistry()
    llm_registry.register(
        ProviderCapability.LLM,
        "llm",
        FixedLLMProvider(
            '{"relations": [{"subject": "网关超时示例", '
            '"object": "示例认证模块", "relation_type": "RELATED_TO"}]}'
        ),
    )
    terms = [
        Term(
            standard_name="示例错误码E502", aliases=["网关超时示例"],
            term_type="error_code", product_line="示例产品线",
        ),
        Term(
            standard_name="示例登录模块", aliases=["示例认证模块"],
            term_type="module", product_line="示例产品线",
        ),
    ]
    graph_client = FakeGraphClient()

    count = await ingest_markdown_file(
        md_file,
        embedding_registry=embedding_registry,
        embedding_provider_name="fake-embedding",
        vector_store=vector_store,
        tenant_id="t1",
        graph_llm_registry=llm_registry,
        graph_llm_provider_name="llm",
        graph_terms=terms,
        graph_client=graph_client,
    )

    # 向量库那一侧被切分成了多条记录
    assert count > 1
    # 图谱抽取只对"网络故障"这一个未切分的原始 chunk 调用了一次 LLM，
    # 抽取结果被正确写入——如果误把切分后的小 chunk 传给了图谱抽取，
    # 这里的 written 断言不会变化（FixedLLMProvider 对任何输入都返回同一段
    # JSON），但如果代码退化成对每个小 chunk 都重复写入，deleted_sources/
    # written 的调用次数或去重行为会跟这里的断言对不上；更直接的信号是
    # graph_client.written 里只应该有一条记录，不会因为切分份数变化。
    assert graph_client.written == [
        {
            "subject": "示例错误码E502",
            "object": "示例登录模块",
            "relation_type": "RELATED_TO",
            "source": str(md_file),
            "tenant_id": "t1",
        }
    ]
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/ingestion/test_ingest_pipeline.py::test_ingest_markdown_file_splits_only_the_embedding_path_not_graph_extraction -v`
Expected: FAIL，`count > 1` 断言不成立（当前 `_ingest_chunks` 没有做任何尺寸切分，一个 `##` 标题下的长正文仍然是 1 个 chunk，`count == 1`）

- [ ] **Step 3: 实现改动**

修改 `app/ingestion/pipeline.py` 顶部 import（第 9 行）：

```python
from app.ingestion.chunking import Chunk, chunk_markdown, split_oversized_chunks
```

修改 `_ingest_chunks` 函数体（原第 113-120 行的 `_embed_and_upsert` 调用之前）：

```python
async def _ingest_chunks(
    chunks: list[Chunk],
    path: Path,
    *,
    embedding_registry: EmbeddingRegistry,
    embedding_provider_name: str,
    vector_store: VectorStore,
    tenant_id: str,
    graph_llm_registry: ProviderRegistry | None,
    graph_llm_provider_name: str | None,
    graph_terms: list[Term] | None,
    graph_client: GraphWriteClientProtocol | None,
    graph_review_conn: aiosqlite.Connection | None,
) -> int:
    """已解析出 chunk 之后共用的写入逻辑：向量化+入库，可选做图谱抽取。

    向量化和图谱抽取吃的是两份不同粒度的 chunk：embedding 路径先经过
    split_oversized_chunks 做尺寸兜底（避免巨大 chunk 稀释 embedding
    语义），图谱抽取路径吃未经切分的原始 chunks（LLM 关系抽取需要更
    完整的上下文）。见设计文档第 2.1 节"双视图分叉"。

    各文件格式的 ingest_*_file 只负责"怎么把文件解析成 chunk 列表"这一步
    不同，解析完之后的处理管线完全一致，抽出来避免四份文件格式各写一遍
    近乎相同的代码。
    """
    embedding_chunks = split_oversized_chunks(chunks)
    count = await _embed_and_upsert(
        embedding_chunks,
        path,
        embedding_registry=embedding_registry,
        embedding_provider_name=embedding_provider_name,
        vector_store=vector_store,
        tenant_id=tenant_id,
    )
    await _maybe_extract_graph_relations(
        chunks,
        source=str(path),
        tenant_id=tenant_id,
        graph_llm_registry=graph_llm_registry,
        graph_llm_provider_name=graph_llm_provider_name,
        graph_terms=graph_terms,
        graph_client=graph_client,
        graph_review_conn=graph_review_conn,
    )
    return count
```

- [ ] **Step 4: 运行测试确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/ingestion/test_ingest_pipeline.py -v`
Expected: 全部 PASS（新增的测试 + 原有的全部测试，包括图谱相关的 3 个既有测试）

- [ ] **Step 5: 提交**

```bash
git add app/ingestion/pipeline.py tests/ingestion/test_ingest_pipeline.py
git commit -m "feat(ingestion): split embedding chunks by size while keeping graph extraction on full sections"
```

---

### Task 3: LLM 抽取器 — 批量输入 + 10 种关系类型 prompt + 30 秒超时

**Files:**
- Modify: `app/graphrag/llm_extractor.py`
- Test: `tests/graphrag/test_llm_extractor.py`

**Interfaces:**
- Produces: `async def extract_candidate_relations(segments: list[str], *, llm_registry: ProviderRegistry, llm_provider_name: str, timeout_sec: float = 30.0) -> list[dict[str, str]]`（供 Task 4 的 `graph_extraction.py` 调用；**签名从单个 `text: str` 改成 `segments: list[str]`，是破坏性变更**）

- [ ] **Step 1: 写失败测试**

用以下内容整个替换 `tests/graphrag/test_llm_extractor.py`：

```python
import asyncio

from app.graphrag.llm_extractor import extract_candidate_relations
from app.providers.base import ProviderCapability, ProviderRequest, ProviderResult
from app.providers.registry import ProviderRegistry


class FixedLLMProvider:
    def __init__(self, text: str) -> None:
        self._text = text

    async def complete(self, request: ProviderRequest) -> ProviderResult:
        return ProviderResult(text=self._text)


class SpyLLMProvider:
    """记录收到的完整请求，用来断言 prompt 内容和多片段拼接格式。"""

    def __init__(self, text: str) -> None:
        self._text = text
        self.received_requests: list[ProviderRequest] = []

    async def complete(self, request: ProviderRequest) -> ProviderResult:
        self.received_requests.append(request)
        return ProviderResult(text=self._text)


class FailingLLMProvider:
    async def complete(self, request: ProviderRequest) -> ProviderResult:
        raise RuntimeError("provider unavailable")


def _registry(provider) -> ProviderRegistry:
    registry = ProviderRegistry()
    registry.register(ProviderCapability.LLM, "llm", provider)
    return registry


async def test_extracts_relations_from_valid_json_response():
    text = (
        '{"relations": ['
        '{"subject": "错误码E502", "object": "登录模块", "relation_type": "RELATED_TO"}'
        "]}"
    )
    relations = await extract_candidate_relations(
        ["文档片段..."],
        llm_registry=_registry(FixedLLMProvider(text)),
        llm_provider_name="llm",
        timeout_sec=1.0,
    )

    assert relations == [
        {
            "subject": "错误码E502",
            "object": "登录模块",
            "relation_type": "RELATED_TO",
        }
    ]


async def test_falls_back_to_empty_list_when_llm_fails():
    relations = await extract_candidate_relations(
        ["文档片段..."],
        llm_registry=_registry(FailingLLMProvider()),
        llm_provider_name="llm",
        timeout_sec=1.0,
    )

    assert relations == []


async def test_falls_back_to_empty_list_when_response_is_malformed_json():
    relations = await extract_candidate_relations(
        ["文档片段..."],
        llm_registry=_registry(FixedLLMProvider("这不是JSON")),
        llm_provider_name="llm",
        timeout_sec=1.0,
    )

    assert relations == []


async def test_single_segment_is_sent_without_segment_markers():
    provider = SpyLLMProvider('{"relations": []}')

    await extract_candidate_relations(
        ["单独一个片段的文本"],
        llm_registry=_registry(provider),
        llm_provider_name="llm",
        timeout_sec=1.0,
    )

    user_message = provider.received_requests[0].messages[1]
    assert user_message["content"] == "单独一个片段的文本"


async def test_multiple_segments_are_joined_with_segment_markers():
    provider = SpyLLMProvider('{"relations": []}')

    await extract_candidate_relations(
        ["第一个片段", "第二个片段"],
        llm_registry=_registry(provider),
        llm_provider_name="llm",
        timeout_sec=1.0,
    )

    user_message = provider.received_requests[0].messages[1]
    assert "[片段1]\n第一个片段" in user_message["content"]
    assert "[片段2]\n第二个片段" in user_message["content"]


async def test_system_prompt_lists_all_ten_relation_types_and_forbids_cross_segment_relations():
    provider = SpyLLMProvider('{"relations": []}')

    await extract_candidate_relations(
        ["片段"],
        llm_registry=_registry(provider),
        llm_provider_name="llm",
        timeout_sec=1.0,
    )

    system_message = provider.received_requests[0].messages[0]["content"]
    for relation_type in [
        "RELATED_TO", "PART_OF", "IS_A", "REQUIRES", "ALTERNATIVE_TO",
        "CAUSES", "ADDRESSED_BY", "LOCATED_IN", "APPLIES_TO", "PRECEDES",
    ]:
        assert relation_type in system_message
    assert "BELONGS_TO_MODULE" not in system_message
    assert "不要把不同片段里的实体强行关联起来" in system_message
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/graphrag/test_llm_extractor.py -v`
Expected: 大部分 FAIL——现有函数签名是 `text: str`，传 `["文档片段..."]`（一个列表）会被当成 `text` 参数直接塞进 prompt，JSON 解析行为不变所以前三个测试可能碰巧还过，但新增的 `test_single_segment_is_sent_without_segment_markers` 等断言 `.messages` 结构的测试会 FAIL（拼接逻辑、10 种关系类型都还不存在）

- [ ] **Step 3: 实现改动**

整个替换 `app/graphrag/llm_extractor.py`：

```python
from __future__ import annotations

import asyncio
import json
import logging

from app.providers.base import ProviderCapability, ProviderRequest
from app.providers.registry import ProviderRegistry

logger = logging.getLogger(__name__)

# 10 种跨领域通用拓扑关系，每种配一个极简中文示例短语（不是完整
# few-shot 例句）帮助 LLM 锚定语义边界，同时把每次调用的固定 token
# 开销控制在可接受范围——见 docs/superpowers/specs/2026-08-09-
# chunking-graph-extraction-redesign-design.md 第 3.2/4 节。
_SYSTEM_PROMPT = (
    "你是知识图谱关系抽取器。"
    "请从给定文档片段中抽取专有名词之间的关系。"
    '只输出 JSON：{"relations":[{"subject":"...","object":"...","relation_type":"RELATED_TO"}]}。'
    "relation_type 仅允许以下 10 种，每种给一个例子帮助理解：\n"
    'RELATED_TO（兜底弱关联，如"促销活动 RELATED_TO 会员日"）、\n'
    'PART_OF（部分-整体，如"客房 PART_OF 酒店"）、\n'
    'IS_A（类别从属，如"大床房 IS_A 客房"）、\n'
    'REQUIRES（前提依赖，如"预订套餐 REQUIRES 会员资格"）、\n'
    'ALTERNATIVE_TO（替代/类似，如"标准间 ALTERNATIVE_TO 大床房"）、\n'
    'CAUSES（因果，如"恶劣天气 CAUSES 接送延误"）、\n'
    'ADDRESSED_BY（问题由方案解决，如"房间异味 ADDRESSED_BY 更换房间"）、\n'
    'LOCATED_IN（空间/组织归属，如"健身房 LOCATED_IN 三楼"）、\n'
    'APPLIES_TO（适用范围，如"会员折扣 APPLIES_TO 非节假日预订"）、\n'
    'PRECEDES（流程先后，如"入住登记 PRECEDES 领取房卡"）。\n'
    "不确定的内容不要编造，抽不出关系就返回空列表。"
    "如果输入包含多个用 [片段N] 标记分隔的片段，只抽取同一个片段内部出现的"
    "关系，不要把不同片段里的实体强行关联起来。"
)


def _build_user_content(segments: list[str]) -> str:
    """单片段时直接发原文，不加标记（兼容单片段场景的 prompt 简洁性）；
    多片段时用 [片段N] 标记分隔，配合 system prompt 里的"不跨片段关联"
    指令，防止批量抽取时 LLM 把毫不相关的片段内容编造成跨片段关系。
    """
    if len(segments) == 1:
        return segments[0]
    return "\n\n".join(
        f"[片段{i}]\n{segment}" for i, segment in enumerate(segments, start=1)
    )


async def extract_candidate_relations(
    segments: list[str],
    *,
    llm_registry: ProviderRegistry,
    llm_provider_name: str,
    timeout_sec: float = 30.0,
) -> list[dict[str, str]]:
    """LLM 抽取候选关系；失败/超时/JSON 解析失败均回退空列表，不阻塞摄取流程。

    segments 支持一次传入多个 chunk 的文本，合并成一次 LLM 调用（见
    graph_extraction.py 的攒批逻辑）——关系写入只按整篇文档 source 溯源，
    不依赖 chunk 粒度，合并调用是纯效率提升。

    timeout_sec 默认 30 秒：这是后台摄取任务专用的默认值，比项目里"实时
    对话链路"惯用的 2 秒宽松得多——摄取没有用户在等，用更长的超时换取
    更高的抽取成功率是合算的。

    这里只产出"候选"，尚未与术语表归一化对齐——归一化在
    normalize_candidate_relations 中完成，二者分开是为了保持每个
    函数职责单一，便于分别测试。
    """
    try:
        result = await asyncio.wait_for(
            llm_registry.run(
                ProviderCapability.LLM,
                ProviderRequest(
                    messages=[
                        {"role": "system", "content": _SYSTEM_PROMPT},
                        {"role": "user", "content": _build_user_content(segments)},
                    ]
                ),
                provider_name=llm_provider_name,
            ),
            timeout=timeout_sec,
        )
    except asyncio.TimeoutError:
        logger.info("关系抽取超时，回退空列表")
        return []
    except Exception:
        logger.warning("关系抽取失败，回退空列表", exc_info=True)
        return []

    try:
        payload = json.loads(result.text)
    except json.JSONDecodeError:
        logger.warning("关系抽取返回非 JSON，回退空列表")
        return []

    raw = payload.get("relations") if isinstance(payload, dict) else None
    if not isinstance(raw, list):
        return []

    relations: list[dict[str, str]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        subject = str(item.get("subject", "")).strip()
        obj = str(item.get("object", "")).strip()
        relation_type = str(item.get("relation_type", "")).strip()
        if subject and obj and relation_type:
            relations.append(
                {"subject": subject, "object": obj, "relation_type": relation_type}
            )
    return relations
```

- [ ] **Step 4: 运行测试确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/graphrag/test_llm_extractor.py -v`
Expected: 全部 PASS

- [ ] **Step 5: 提交**

```bash
git add app/graphrag/llm_extractor.py tests/graphrag/test_llm_extractor.py
git commit -m "feat(graphrag): batch multi-segment extraction with 10-type relation vocabulary and 30s timeout"
```

---

### Task 4: 图谱抽取批量化 + 并发 — `graph_extraction.py`

**Files:**
- Modify: `app/ingestion/graph_extraction.py`
- Test: `tests/ingestion/test_graph_extraction.py`

**Interfaces:**
- Consumes: `extract_candidate_relations(segments: list[str], *, llm_registry, llm_provider_name, timeout_sec=30.0) -> list[dict[str, str]]`（Task 3）
- Produces: `_batch_chunks_by_char_budget(chunks: list[Chunk], *, max_chars: int = 3000) -> list[list[Chunk]]`；`extract_and_write_graph_relations(..., extract_timeout_sec: float = 30.0, batch_max_chars: int = 3000, max_concurrency: int = 8) -> int`（新增 3 个带默认值的关键字参数，`pipeline.py::_maybe_extract_graph_relations` 不需要跟着改，继续用默认值）

- [ ] **Step 1: 写失败测试**

修改 `tests/ingestion/test_graph_extraction.py` 顶部的 import 区：文件第一行 `import aiosqlite` 之前加一行 `import asyncio`；把 `from app.ingestion.graph_extraction import extract_and_write_graph_relations` 这一行改成：

```python
from app.ingestion.graph_extraction import (
    _batch_chunks_by_char_budget,
    extract_and_write_graph_relations,
)
```

然后在文件末尾追加：

```python
def test_batch_chunks_by_char_budget_groups_up_to_the_limit():
    chunks = [
        Chunk(text="a" * 1000, heading_path=[], source="a.md"),
        Chunk(text="b" * 1000, heading_path=[], source="a.md"),
        Chunk(text="c" * 1000, heading_path=[], source="a.md"),
    ]

    batches = _batch_chunks_by_char_budget(chunks, max_chars=2500)

    assert len(batches) == 2
    assert len(batches[0]) == 2  # a+b 累计 2000 字符 <= 2500
    assert len(batches[1]) == 1  # c 单独成批


def test_batch_chunks_by_char_budget_keeps_oversized_single_chunk_alone():
    chunks = [
        Chunk(text="x" * 5000, heading_path=[], source="a.md"),
        Chunk(text="y" * 100, heading_path=[], source="a.md"),
    ]

    batches = _batch_chunks_by_char_budget(chunks, max_chars=3000)

    assert len(batches) == 2
    assert len(batches[0]) == 1
    assert batches[0][0].text == "x" * 5000
    assert len(batches[1]) == 1


async def test_extract_and_write_graph_relations_respects_max_concurrency():
    """并发批次数不能超过 max_concurrency——用一个记录"同时在途请求数"的
    fake provider，真的 sleep 一下让并发窗口重叠，验证峰值不超过限制、
    同时确认真的发生了并发（不是退化成串行）。
    """
    concurrent_count = {"current": 0, "peak": 0}

    class TrackingLLMProvider:
        async def complete(self, request):
            concurrent_count["current"] += 1
            concurrent_count["peak"] = max(
                concurrent_count["peak"], concurrent_count["current"]
            )
            await asyncio.sleep(0.05)
            concurrent_count["current"] -= 1
            return ProviderResult(text='{"relations": []}')

    llm_registry = ProviderRegistry()
    llm_registry.register(ProviderCapability.LLM, "llm", TrackingLLMProvider())
    graph_client = FakeGraphClient()
    # 20 个 chunk，每个都单独超过 max_chars，逼出 20 个独立批次
    chunks = [Chunk(text="x" * 4000, heading_path=[], source="a.md") for _ in range(20)]

    await extract_and_write_graph_relations(
        chunks,
        llm_registry=llm_registry,
        llm_provider_name="llm",
        terms=_TERMS,
        graph_client=graph_client,
        source="a.md",
        tenant_id="t1",
        batch_max_chars=3000,
        max_concurrency=4,
    )

    assert concurrent_count["peak"] <= 4
    assert concurrent_count["peak"] > 1


async def test_one_failing_batch_does_not_prevent_other_batches_from_writing():
    class ContentBasedFailingLLMProvider:
        async def complete(self, request):
            user_content = request.messages[1]["content"]
            if "FAIL_MARKER" in user_content:
                raise RuntimeError("模拟这一批调用失败")
            return ProviderResult(
                text='{"relations": [{"subject": "网关超时示例", '
                '"object": "示例认证模块", "relation_type": "RELATED_TO"}]}'
            )

    llm_registry = ProviderRegistry()
    llm_registry.register(ProviderCapability.LLM, "llm", ContentBasedFailingLLMProvider())
    graph_client = FakeGraphClient()
    chunks = [
        Chunk(text="FAIL_MARKER" + "a" * 4000, heading_path=[], source="a.md"),
        Chunk(text="网关超时示例通常与示例认证模块相关", heading_path=[], source="a.md"),
    ]

    written = await extract_and_write_graph_relations(
        chunks,
        llm_registry=llm_registry,
        llm_provider_name="llm",
        terms=_TERMS,
        graph_client=graph_client,
        source="a.md",
        tenant_id="t1",
        batch_max_chars=100,
    )

    assert written == 1
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/ingestion/test_graph_extraction.py -v`
Expected: 新增的 4 个测试 FAIL（`_batch_chunks_by_char_budget` 不存在；`extract_and_write_graph_relations` 不接受 `batch_max_chars`/`max_concurrency` 关键字参数）；原有的 4 个测试此时应该仍然 PASS（它们用的是单 chunk 列表，还没受影响）

- [ ] **Step 3: 实现改动**

整个替换 `app/ingestion/graph_extraction.py`：

```python
from __future__ import annotations

import asyncio

import aiosqlite

from app.graphrag.llm_extractor import extract_candidate_relations
from app.graphrag.normalization import GraphWriteClientProtocol, normalize_and_write_relations
from app.graphrag.ontology import Term
from app.ingestion.chunking import Chunk
from app.providers.registry import ProviderRegistry


def _batch_chunks_by_char_budget(
    chunks: list[Chunk], *, max_chars: int = 3000
) -> list[list[Chunk]]:
    """依次把 chunk 塞进当前批次，累计字符数超过 max_chars 就切下一批；
    单个 chunk 本身已经超过 max_chars 时自己单独成一批，不因为攒批逻辑被
    拆散或跳过。批次之间不重叠——攒批只是为了减少 LLM 调用次数，不改变
    任何一个 chunk 的内容。
    """
    if not chunks:
        return []
    batches: list[list[Chunk]] = []
    current: list[Chunk] = []
    current_len = 0
    for chunk in chunks:
        chunk_len = len(chunk.text)
        if current and current_len + chunk_len > max_chars:
            batches.append(current)
            current = []
            current_len = 0
        current.append(chunk)
        current_len += chunk_len
    if current:
        batches.append(current)
    return batches


async def extract_and_write_graph_relations(
    chunks: list[Chunk],
    *,
    llm_registry: ProviderRegistry,
    llm_provider_name: str,
    terms: list[Term],
    graph_client: GraphWriteClientProtocol,
    source: str,
    tenant_id: str,
    review_conn: aiosqlite.Connection | None = None,
    extract_timeout_sec: float = 30.0,
    batch_max_chars: int = 3000,
    max_concurrency: int = 8,
) -> int:
    """摄取时的图谱构建：按字符预算把 chunk 攒批，批次间有限并发地做
    LLM 关系抽取，再顺序做术语表归一化 + 写入 Neo4j。

    这是可选步骤（未接入 ingest_markdown_file/ingest_pdf_file 的默认路径），
    调用方需要显式提供 llm_registry/terms/graph_client 才会执行；不提供
    则摄取流程只做向量化写入，与阶段2的行为保持完全兼容。

    写入前先删掉 source+tenant_id 这个文档、这个租户之前写过的全部关系边
    （delete_relations_by_source），再重新抽取写入——和
    vector_store.delete_by_source() 同样的道理：文档内容变更后，旧版本
    抽取出的关系不会永久残留在图谱里。对全新文档这是无害的空操作。
    tenant_id 同时保证了这个清理动作不会波及其它租户摄取过的同名文档。

    review_conn 同样可选：提供时，未能对齐术语表的候选关系会进入人工
    待审核队列而不是直接丢弃（见 normalize_and_write_relations）。

    攒批+并发是效率改造的核心：关系写入只按 source+tenant_id 溯源，不
    依赖 chunk 粒度，合并多个 chunk 进一次 LLM 调用是纯效率提升；
    max_concurrency 用 Semaphore 限制同时在途的批次数，避免大文档一次性
    发出几十个并发请求触发 LLM 供应商限流。并发只作用于 LLM 抽取这一步
    （无共享可变状态，天然安全）；写入 Neo4j/审核队列的步骤保持顺序执行，
    避免"多协程并发操作同一个 aiosqlite 连接是否安全"这个没有把握的
    未知数。已知代价：一批失败会丢整批涉及 chunk 的关系（现状是一个
    chunk 失败只丢一个 chunk），接受这个代价换取更少的调用次数。
    """
    await graph_client.delete_relations_by_source(source, tenant_id=tenant_id)
    batches = _batch_chunks_by_char_budget(chunks, max_chars=batch_max_chars)
    semaphore = asyncio.Semaphore(max_concurrency)

    async def _process_batch(batch: list[Chunk]) -> list[dict[str, str]]:
        async with semaphore:
            return await extract_candidate_relations(
                [chunk.text for chunk in batch],
                llm_registry=llm_registry,
                llm_provider_name=llm_provider_name,
                timeout_sec=extract_timeout_sec,
            )

    all_relation_lists = await asyncio.gather(
        *(_process_batch(batch) for batch in batches)
    )

    total_written = 0
    for relations in all_relation_lists:
        total_written += await normalize_and_write_relations(
            relations,
            terms=terms,
            graph_client=graph_client,
            source=source,
            tenant_id=tenant_id,
            review_conn=review_conn,
        )
    return total_written
```

- [ ] **Step 4: 运行测试确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/ingestion/test_graph_extraction.py -v`
Expected: 全部 PASS（新增 4 个 + 原有 4 个）

- [ ] **Step 5: 运行完整摄取测试套件确认没有破坏上游调用方**

Run: `.venv/Scripts/python.exe -m pytest tests/ingestion/ -v`
Expected: 全部 PASS（`test_ingest_pipeline.py` 里依赖图谱抽取的既有测试不受影响，因为新增的 3 个关键字参数都有默认值）

- [ ] **Step 6: 提交**

```bash
git add app/ingestion/graph_extraction.py tests/ingestion/test_graph_extraction.py
git commit -m "feat(ingestion): batch and concurrently extract graph relations with bounded concurrency"
```

---

### Task 5: 关系类型词表扩充 + 2 跳链式查询 — `neo4j_client.py`

**Files:**
- Modify: `app/graphrag/neo4j_client.py`
- Modify: `tests/graphrag/test_normalization.py:40`（无关但引用了旧关系类型的一处断言）
- Test: `tests/graphrag/test_neo4j_client.py`

**Interfaces:**
- Produces: `_ALLOWED_RELATION_TYPES`（10 种新类型，供 `merge_relation` 校验用）；`query_subgraph(standard_name: str, *, tenant_id: str) -> list[dict[str, Any]]` 返回值新增 `hops` 字段（`1` 或 `2`），供 Task 6 的 `term_guard.py` 消费

- [ ] **Step 1: 写失败测试**

在 `tests/graphrag/test_neo4j_client.py` 末尾追加：

```python
def test_allowed_relation_types_include_all_ten_generic_types_and_not_the_old_one():
    from app.graphrag.neo4j_client import _ALLOWED_RELATION_TYPES

    assert _ALLOWED_RELATION_TYPES == {
        "RELATED_TO", "PART_OF", "IS_A", "REQUIRES", "ALTERNATIVE_TO",
        "CAUSES", "ADDRESSED_BY", "LOCATED_IN", "APPLIES_TO", "PRECEDES",
    }
    assert "BELONGS_TO_MODULE" not in _ALLOWED_RELATION_TYPES


async def test_merge_relation_accepts_new_part_of_type():
    session = FakeSession(rows=[])
    client = Neo4jGraphClient(driver=FakeDriver(session))

    await client.merge_relation(
        subject_standard_name="大床房",
        object_standard_name="酒店",
        relation_type="PART_OF",
        source="a.md",
        tenant_id="t1",
    )

    assert "PART_OF" in session.last_query


async def test_merge_relation_rejects_the_retired_belongs_to_module_type():
    session = FakeSession(rows=[])
    client = Neo4jGraphClient(driver=FakeDriver(session))

    try:
        await client.merge_relation(
            subject_standard_name="a",
            object_standard_name="b",
            relation_type="BELONGS_TO_MODULE",
            source="a.md",
            tenant_id="t1",
        )
        assert False, "BELONGS_TO_MODULE 已经被 PART_OF 取代，应该拒绝"
    except ValueError:
        pass


async def test_query_subgraph_sends_two_hop_union_query_for_chain_relations():
    session = FakeSession(rows=[])
    client = Neo4jGraphClient(driver=FakeDriver(session))

    await client.query_subgraph("错误码E502", tenant_id="t1")

    assert "UNION" in session.last_query
    assert "REQUIRES|PRECEDES|PART_OF*2..2" in session.last_query
    assert "ALL(rel IN r WHERE rel.tenant_id = $tenant_id)" in session.last_query
    assert session.last_parameters == {"standard_name": "错误码E502", "tenant_id": "t1"}
```

同时修改 `tests/graphrag/test_normalization.py:40`：

```python
        if relation_type not in {
            "RELATED_TO", "PART_OF", "IS_A", "REQUIRES", "ALTERNATIVE_TO",
            "CAUSES", "ADDRESSED_BY", "LOCATED_IN", "APPLIES_TO", "PRECEDES",
        }:
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/graphrag/test_neo4j_client.py -v`
Expected: 新增的 4 个测试 FAIL（白名单还是旧的 2 种；`_SUBGRAPH_QUERY` 还没有 `UNION`）

- [ ] **Step 3: 实现改动**

修改 `app/graphrag/neo4j_client.py` 顶部的 `_SUBGRAPH_QUERY` 和 `_ALLOWED_RELATION_TYPES`（原第 7-15 行）：

```python
_SUBGRAPH_QUERY = """
MATCH (t:Term {standard_name: $standard_name})-[r]-(related:Term)
WHERE r.tenant_id = $tenant_id
RETURN related.standard_name AS related_name, type(r) AS relation_type, 1 AS hops

UNION

MATCH (t:Term {standard_name: $standard_name})-[r:REQUIRES|PRECEDES|PART_OF*2..2]-(related:Term)
WHERE ALL(rel IN r WHERE rel.tenant_id = $tenant_id)
RETURN related.standard_name AS related_name,
       [rel IN r | type(rel)][-1] AS relation_type,
       2 AS hops
"""
# 第二段 UNION 只对 REQUIRES/PRECEDES/PART_OF 这三种"链式"关系放开到
# 恰好 2 跳（*2..2，不是 *1..2，避免和第一段的 1 跳结果重复）——前提链、
# 流程顺序、包含层级经常需要连续追问两步；其余关系类型语义上查 1 跳就
# 有意义，继续放开多跳容易发散、引入噪声上下文。
#
# ALL(rel IN r WHERE rel.tenant_id = $tenant_id) 必须校验路径上每一条边
# 的租户归属，不能只查其中一条——:Term 标准节点本身不分租户、可能被
# 多个租户共用，如果只检查一跳，2 跳路径有可能"借道"另一个租户写入的边，
# 把不该出现的信息泄露给当前租户。这是本次改动里唯一一个如果实现疏忽
# 会导致真实安全问题的点。

# 关系类型白名单：10 种跨领域通用拓扑关系，刻意不含任何行业色彩（不是
# "错误码/模块"这类软件运维语义，也不是"房型/商品"这类某个垂直领域专属
# 语义）——领域信息由术语表的 term_type/product_line 字段承载，关系类型
# 词表本身保持跨租户通用。PART_OF 取代了旧的 BELONGS_TO_MODULE（语义
# 超集），本地无生产数据需要迁移，清理式切换，不写迁移脚本。
_ALLOWED_RELATION_TYPES = frozenset({
    "RELATED_TO",
    "PART_OF",
    "IS_A",
    "REQUIRES",
    "ALTERNATIVE_TO",
    "CAUSES",
    "ADDRESSED_BY",
    "LOCATED_IN",
    "APPLIES_TO",
    "PRECEDES",
})
```

`query_subgraph`/`merge_relation`/`delete_relations_by_source` 三个方法本身**不改动**——`query_subgraph` 已经是 `return await result.data()` 纯透传，`_SUBGRAPH_QUERY` 换内容它不用跟着变；`merge_relation` 的白名单校验逻辑不变，只是校验的集合内容变了。

- [ ] **Step 4: 运行测试确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/graphrag/test_neo4j_client.py tests/graphrag/test_normalization.py -v`
Expected: 全部 PASS

- [ ] **Step 5: 提交**

```bash
git add app/graphrag/neo4j_client.py tests/graphrag/test_neo4j_client.py tests/graphrag/test_normalization.py
git commit -m "feat(graphrag): expand relation type vocabulary to 10 domain-agnostic types and add 2-hop chain query"
```

---

### Task 6: TermGuard 区分直接/间接关联 — `term_guard.py`

**Files:**
- Modify: `app/graphrag/term_guard.py`
- Test: `tests/graphrag/test_term_guard.py`

**Interfaces:**
- Consumes: `query_subgraph` 返回的字典里可能带 `hops: int`（Task 5，默认视为 `1`，向后兼容不带这个字段的调用方）

- [ ] **Step 1: 写失败测试**

在 `tests/graphrag/test_term_guard.py` 末尾追加：

```python
async def test_marks_two_hop_results_as_indirect_association():
    graph_client = FakeGraphClient(
        subgraph_rows=[
            {"related_name": "登录模块", "relation_type": "RELATED_TO", "hops": 1},
            {"related_name": "会员资格", "relation_type": "REQUIRES", "hops": 2},
        ]
    )

    context = await build_term_guard_context(
        "我这边报了网关超时", terms=_TERMS, tenant_id="t1", graph_client=graph_client
    )

    assert "关联: 登录模块" in context
    assert "间接关联（经过 2 跳）: 会员资格" in context


async def test_defaults_to_direct_association_when_hops_field_is_missing():
    """向后兼容：query_subgraph 的 fake/旧实现不带 hops 字段时，仍然按
    直接关联展示，不报错。"""
    graph_client = FakeGraphClient(
        subgraph_rows=[{"related_name": "登录模块", "relation_type": "RELATED_TO"}]
    )

    context = await build_term_guard_context(
        "我这边报了网关超时", terms=_TERMS, tenant_id="t1", graph_client=graph_client
    )

    assert "关联: 登录模块" in context
    assert "间接关联" not in context
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/graphrag/test_term_guard.py -v`
Expected: `test_marks_two_hop_results_as_indirect_association` FAIL（当前实现不区分 hops，输出里不会有"间接关联"字样）；`test_defaults_to_direct_association_when_hops_field_is_missing` 此时应该已经 PASS（现状本来就没有"间接关联"字样）

- [ ] **Step 3: 实现改动**

修改 `app/graphrag/term_guard.py` 里 `build_term_guard_context` 函数体的 for 循环部分（原第 43-46 行）：

```python
        for row in subgraph:
            # hops 字段区分直接事实（1 跳）和推导出的间接事实（2 跳，只有
            # REQUIRES/PRECEDES/PART_OF 这类链式关系才会出现），标注清楚
            # 避免 LLM 把两者当同等确定性的信息——见
            # neo4j_client.py::query_subgraph 的 UNION 查询设计。
            hops = row.get("hops", 1)
            label = "关联" if hops == 1 else f"间接关联（经过 {hops} 跳）"
            lines.append(
                f"  {label}: {row['related_name']}（关系: {row['relation_type']}）"
            )
```

- [ ] **Step 4: 运行测试确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/graphrag/test_term_guard.py -v`
Expected: 全部 PASS

- [ ] **Step 5: 提交**

```bash
git add app/graphrag/term_guard.py tests/graphrag/test_term_guard.py
git commit -m "feat(graphrag): distinguish direct vs indirect associations in term guard context"
```

---

### Task 7: 全量验证

**Files:** 无改动，纯验证

- [ ] **Step 1: 跑全量测试套件**

Run: `.venv/Scripts/python.exe -m pytest -q`
Expected: 全部 PASS，除了会话开始前就已知、与本次改动无关的 `tests/providers/test_voice_factory.py::test_returns_none_when_tts_not_configured`（本地 `.env` 配了真实 TTS 凭证，和测试"未配置"的前提冲突）

- [ ] **Step 2: 确认没有遗留对旧接口的引用**

Run: `grep -rn "BELONGS_TO_MODULE" app/ tests/`
Expected: 无匹配结果（全部清理式切换到 `PART_OF`）

- [ ] **Step 3: 如果 Step 1/2 有非预期失败，定位并修复后回到 Step 1**

不新增功能代码，只修复本计划引入的回归。
