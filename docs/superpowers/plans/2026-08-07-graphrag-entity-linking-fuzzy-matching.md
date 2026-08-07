# GraphRAG 实体链接模糊匹配 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 给 `app/graphrag/normalization.py::normalize_and_write_relations()` 加一层实体链接模糊匹配兜底：LLM 抽取的候选实体名精确匹配术语表失败时，尝试字符串相似度模糊匹配，命中后**不自动写入图谱**，而是连同"建议标准名"一起进入人工审核队列。

**Architecture:** 3 个任务：① `app/graphrag/normalization.py` 新增 `find_fuzzy_candidate_standard_name()` 纯函数（difflib 相似度，取最优单一建议，不改动既有的 `resolve_to_standard_name()`）；② `app/graphrag/review_queue.py` 的 `graph_review_queue` 表新增两个可空字段存"建议标准名"，`enqueue_for_review()`/`list_pending_reviews()` 同步支持；③ `normalize_and_write_relations()` 编排逻辑接入模糊匹配，精确匹配失败时追加尝试模糊匹配，命中则改走审核队列（带建议名），不再是"精确失败就只有完全丢弃"两种结局。

**Tech Stack:** Python 3.12、stdlib `difflib`（不引入新依赖）、`aiosqlite`、pytest（`asyncio_mode = "auto"`）。

## Global Constraints

- 严格 TDD：RED（写失败测试，确认失败原因正确）→ GREEN（最小实现）→ 跑全量测试 → git commit。
- 本仓库当前在 `dev/0.1` 分支直接工作（非 main/master），不使用隔离 worktree。
- Commit message 格式：一行摘要（`feat:` 前缀）+ 空行 + 中文详细说明（为什么这么做/复用了什么/刻意不做什么）+ 以 `Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>` 结尾。
- 测试命令统一用 `.venv/Scripts/python.exe -m pytest <path> -v`（Windows 环境，本仓库自带 `.venv`）。
- 全量测试跑 `.venv/Scripts/python.exe -m pytest tests/ -q`，预期除了 1 个已知的、与本计划改动完全无关的预先存在失败（`tests/providers/test_voice_factory.py::test_returns_none_when_tts_not_configured`，本地 `.env` 里真实 TTS 凭证泄漏导致的环境问题，历史上反复确认与代码改动无关）之外全部通过。如果发现除此之外还有其他测试失败，必须先排查原因，不要跳过或忽略。
- 设计依据：`docs/superpowers/specs/2026-08-07-graphrag-entity-linking-fuzzy-matching-design.md`（已经用户批准，不要偏离其中的机制决策），尤其是这几条明确决策：**不引入向量相似度/embedding**；模糊命中**一律不自动写入图谱**，进人工审核队列；**不写 schema 迁移脚本**（当前无已部署的旧 schema 数据）；`resolve_to_standard_name()` 精确匹配函数本身**不改动**；`approve_review()` 已知缺 `tenant_id`/`source` 参数的问题**不在本次修复范围**。
- 阈值 0.75 是函数关键字参数的硬编码默认值，不新增 Settings 配置项（沿用 TermGuard/ASR 校正的既定约定）。

---

### Task 1: `find_fuzzy_candidate_standard_name()` 纯函数

**Files:**
- Modify: `app/graphrag/normalization.py`
- Test: `tests/graphrag/test_normalization.py`（当前有 6 个测试，全部保持不变、必须继续通过）

**Interfaces:**
- Consumes：无新依赖（`app.graphrag.ontology.Term` 已有，stdlib `difflib`）。
- Produces：`find_fuzzy_candidate_standard_name(name: str, terms: list[Term], *, threshold: float = 0.75) -> str | None`——对 `name` 和每个术语的标准名+每个别名逐一计算 `difflib.SequenceMatcher(None, name, candidate).ratio()`，返回**相似度最高**的那个候选对应的标准名（`ratio >= threshold` 才算数，多个候选并列最高时取先遍历到的那个，一个都没达到阈值则返回 `None`）。Task 3 会调用这个函数。

- [ ] **Step 1: 写失败测试**

在 `tests/graphrag/test_normalization.py` 文件顶部的 import 区域，把：

```python
from app.graphrag.normalization import normalize_and_write_relations
```

改成：

```python
from app.graphrag.normalization import (
    find_fuzzy_candidate_standard_name,
    normalize_and_write_relations,
)
```

在文件末尾追加（不改动已有的 6 个测试）：

```python
def test_find_fuzzy_candidate_standard_name_matches_via_alias_typo():
    # "网关超时了" 比别名"网关超时"多了一个字，difflib 相似度约 0.8889，
    # 高于默认阈值 0.75，应该建议对齐到"错误码E502"。
    result = find_fuzzy_candidate_standard_name("网关超时了", _TERMS)

    assert result == "错误码E502"


def test_find_fuzzy_candidate_standard_name_matches_at_exact_threshold_boundary():
    # "认正模块"是别名"认证模块"打错1字，difflib 相似度恰好等于默认阈值
    # 0.75，应该命中（>= 判断，不是 >）。
    result = find_fuzzy_candidate_standard_name("认正模块", _TERMS)

    assert result == "登录模块"


def test_find_fuzzy_candidate_standard_name_returns_none_when_below_threshold():
    # 完全不相关的候选名，所有术语的相似度都是 0，远低于阈值。
    result = find_fuzzy_candidate_standard_name("不存在的实体", _TERMS)

    assert result is None


def test_find_fuzzy_candidate_standard_name_respects_custom_threshold():
    # "认正模块" vs 别名"认证模块"相似度 0.75；传入更严格的阈值 0.9 时
    # 不应该命中——验证 threshold 参数真的生效，不是死参数。
    result = find_fuzzy_candidate_standard_name(
        "认正模块", _TERMS, threshold=0.9
    )

    assert result is None
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/graphrag/test_normalization.py -v`
Expected: 原有 6 个测试仍然通过（还没改实现）；新增的 4 个 `test_find_fuzzy_candidate_standard_name_*` 测试因为 `ImportError`（`find_fuzzy_candidate_standard_name` 还不存在）而失败。

- [ ] **Step 3: 实现 `find_fuzzy_candidate_standard_name`**

打开 `app/graphrag/normalization.py`，在文件顶部 import 区域新增：

```python
import difflib
```

（放在 `import logging` 之后、`from typing import Any, Protocol` 之前，按标准库字母序）。

在 `resolve_to_standard_name` 函数定义之后（`normalize_and_write_relations` 函数定义之前）新增：

```python
def find_fuzzy_candidate_standard_name(
    name: str, terms: list[Term], *, threshold: float = 0.75
) -> str | None:
    """精确匹配失败后的模糊匹配兜底：找相似度最高的单一标准名建议。

    和 term_matcher.py::match_terms()（TermGuard）"任意命中就收集一组
    术语"不同——那边是往上下文里塞信息，多塞几个无妨；这里要给人工审核
    一个具体的对齐建议，必须是单一最优解。因为 name 本身就是 LLM 抽取
    出的完整候选实体名（不是需要在长文本里扫描的段落），直接整串比较，
    不需要滑动窗口。

    threshold 默认 0.75（沿用 TermGuard 的保守取值）——这是参考起点，
    需要结合真实数据调整，不是权威值。返回值只是"建议"，调用方（见
    normalize_and_write_relations）不会拿这个结果自动写入图谱，而是
    连同建议一起进人工审核队列，由人工最终确认。
    """
    best_name: str | None = None
    best_ratio = 0.0
    for term in terms:
        for candidate in [term.standard_name, *term.aliases]:
            if not candidate:
                continue
            ratio = difflib.SequenceMatcher(None, name, candidate).ratio()
            if ratio >= threshold and ratio > best_ratio:
                best_ratio = ratio
                best_name = term.standard_name
    return best_name
```

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/graphrag/test_normalization.py -v`
Expected: 10 passed（原有 6 个 + 新增 4 个）

- [ ] **Step 5: 跑全量测试**

Run: `.venv/Scripts/python.exe -m pytest tests/ -q`
Expected: 除了 1 个已知无关的预先存在失败之外全部通过。因为这一步只新增了一个纯函数、没有改动任何现有函数的行为，不应该有任何其他测试失败。

- [ ] **Step 6: 提交**

```bash
git add app/graphrag/normalization.py tests/graphrag/test_normalization.py
git commit -m "feat: add find_fuzzy_candidate_standard_name for entity-linking fuzzy fallback"
```

---

### Task 2: `review_queue.py` 新增"建议标准名"字段

**Files:**
- Modify: `app/graphrag/review_queue.py`
- Test: `tests/graphrag/test_review_queue.py`（当前有 5 个测试，全部保持不变、必须继续通过）

**Interfaces:**
- Consumes：无新依赖。
- Produces：`enqueue_for_review()` 新增两个可选关键字参数 `suggested_subject_standard_name: str | None = None`、`suggested_object_standard_name: str | None = None`（默认 `None`，现有调用方不用改）。`list_pending_reviews()` 返回的每个 dict 新增这两个键。Task 3 会用到这两个参数和返回字段。

- [ ] **Step 1: 写失败测试**

在 `tests/graphrag/test_review_queue.py` 文件末尾追加（不改动已有的 5 个测试）：

```python
async def test_enqueue_with_suggested_names_is_returned_by_list_pending():
    conn = await _connect()

    await enqueue_for_review(
        conn,
        subject_candidate="网关超时了",
        object_candidate="认证模块",
        relation_type="RELATED_TO",
        reason="fuzzy_match_needs_confirmation",
        suggested_subject_standard_name="错误码E502",
        suggested_object_standard_name=None,
    )

    pending = await list_pending_reviews(conn)
    assert len(pending) == 1
    assert pending[0]["suggested_subject_standard_name"] == "错误码E502"
    assert pending[0]["suggested_object_standard_name"] is None


async def test_enqueue_without_suggested_names_defaults_to_null():
    conn = await _connect()

    await enqueue_for_review(
        conn,
        subject_candidate="不存在的东西",
        object_candidate="认证模块",
        relation_type="RELATED_TO",
        reason="subject_unresolved",
    )

    pending = await list_pending_reviews(conn)
    assert len(pending) == 1
    assert pending[0]["suggested_subject_standard_name"] is None
    assert pending[0]["suggested_object_standard_name"] is None
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/graphrag/test_review_queue.py -v`
Expected: 原有 5 个测试通过；新增的 2 个测试失败——`test_enqueue_with_suggested_names_is_returned_by_list_pending` 因为 `enqueue_for_review()` 还不接受 `suggested_subject_standard_name`/`suggested_object_standard_name` 关键字参数而抛 `TypeError`；`test_enqueue_without_suggested_names_defaults_to_null` 因为 `list_pending_reviews()` 返回的 dict 里还没有这两个键而在 `pending[0]["suggested_subject_standard_name"]` 这行抛 `KeyError`。

- [ ] **Step 3: 实现 schema 扩展**

打开 `app/graphrag/review_queue.py`，把 `_SCHEMA_SQL` 这一段：

```python
_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS graph_review_queue (
    review_id INTEGER PRIMARY KEY AUTOINCREMENT,
    subject_candidate TEXT NOT NULL,
    object_candidate TEXT NOT NULL,
    relation_type TEXT NOT NULL,
    reason TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    resolved_at TEXT,
    resolved_note TEXT
);
CREATE INDEX IF NOT EXISTS idx_graph_review_queue_status
    ON graph_review_queue (status);
"""
```

改成（新增两列，紧跟在 `reason` 列后面）：

```python
_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS graph_review_queue (
    review_id INTEGER PRIMARY KEY AUTOINCREMENT,
    subject_candidate TEXT NOT NULL,
    object_candidate TEXT NOT NULL,
    relation_type TEXT NOT NULL,
    reason TEXT NOT NULL,
    suggested_subject_standard_name TEXT,
    suggested_object_standard_name TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    resolved_at TEXT,
    resolved_note TEXT
);
CREATE INDEX IF NOT EXISTS idx_graph_review_queue_status
    ON graph_review_queue (status);
"""
```

（当前无已部署的旧 schema 数据需要迁移，直接改 `CREATE TABLE IF NOT EXISTS` 语句即可，不写 `ALTER TABLE` 迁移逻辑。）

把 `enqueue_for_review()` 这一段：

```python
async def enqueue_for_review(
    conn: aiosqlite.Connection,
    *,
    subject_candidate: str,
    object_candidate: str,
    relation_type: str,
    reason: str,
) -> int:
    """把未能对齐术语表（或关系类型不合法）的候选关系存入待审核队列，返回 review_id。

    这里存的是 LLM 抽取出的原始候选名（subject_candidate/object_candidate），
    不是标准名——正是因为它们对不上术语表才会进队列，所以此时还没有标准名可存。
    """
    cursor = await conn.execute(
        "INSERT INTO graph_review_queue "
        "(subject_candidate, object_candidate, relation_type, reason) "
        "VALUES (?, ?, ?, ?)",
        (subject_candidate, object_candidate, relation_type, reason),
    )
    await conn.commit()
    return cursor.lastrowid
```

改成：

```python
async def enqueue_for_review(
    conn: aiosqlite.Connection,
    *,
    subject_candidate: str,
    object_candidate: str,
    relation_type: str,
    reason: str,
    suggested_subject_standard_name: str | None = None,
    suggested_object_standard_name: str | None = None,
) -> int:
    """把未能对齐术语表（或关系类型不合法）的候选关系存入待审核队列，返回 review_id。

    这里存的是 LLM 抽取出的原始候选名（subject_candidate/object_candidate），
    不是标准名——正是因为它们对不上术语表才会进队列，所以此时还没有标准名可存。

    suggested_subject_standard_name/suggested_object_standard_name 是可选的
    模糊匹配建议（见 normalization.py::find_fuzzy_candidate_standard_name），
    只是给人工审核参考，不会被自动采纳——approve_review() 仍然要求人工
    明确指定最终标准名。默认 None，对应完全没有模糊候选的场景（如
    reason="subject_unresolved"/"object_unresolved"/"invalid_relation_type"）。
    """
    cursor = await conn.execute(
        "INSERT INTO graph_review_queue "
        "(subject_candidate, object_candidate, relation_type, reason, "
        "suggested_subject_standard_name, suggested_object_standard_name) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            subject_candidate,
            object_candidate,
            relation_type,
            reason,
            suggested_subject_standard_name,
            suggested_object_standard_name,
        ),
    )
    await conn.commit()
    return cursor.lastrowid
```

把 `list_pending_reviews()` 这一段：

```python
async def list_pending_reviews(conn: aiosqlite.Connection) -> list[dict[str, Any]]:
    conn.row_factory = aiosqlite.Row
    cursor = await conn.execute(
        "SELECT review_id, subject_candidate, object_candidate, relation_type, "
        "reason, created_at FROM graph_review_queue "
        "WHERE status = 'pending' ORDER BY review_id"
    )
    rows = await cursor.fetchall()
    return [dict(row) for row in rows]
```

改成（`SELECT` 列表里新增两列）：

```python
async def list_pending_reviews(conn: aiosqlite.Connection) -> list[dict[str, Any]]:
    conn.row_factory = aiosqlite.Row
    cursor = await conn.execute(
        "SELECT review_id, subject_candidate, object_candidate, relation_type, "
        "reason, suggested_subject_standard_name, suggested_object_standard_name, "
        "created_at FROM graph_review_queue "
        "WHERE status = 'pending' ORDER BY review_id"
    )
    rows = await cursor.fetchall()
    return [dict(row) for row in rows]
```

`approve_review()`/`reject_review()`/`_fetch_pending_row()` 不需要改动（`_fetch_pending_row` 用的是 `SELECT *`，新列会自动带出，但目前没有任何调用方读取它，属于既有行为的自然延伸，不需要额外处理）。

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/graphrag/test_review_queue.py -v`
Expected: 7 passed（原有 5 个 + 新增 2 个）

- [ ] **Step 5: 跑全量测试**

Run: `.venv/Scripts/python.exe -m pytest tests/ -q`
Expected: 除了 1 个已知无关的预先存在失败之外全部通过——尤其确认 `tests/graphrag/test_normalization.py` 里 Task 1 已经通过的测试和其余调用 `enqueue_for_review`/`list_pending_reviews` 的测试（如 `tests/graphrag/test_review_cli.py`，如果存在）不受影响，因为新参数都有默认值、新列都可为空。

- [ ] **Step 6: 提交**

```bash
git add app/graphrag/review_queue.py tests/graphrag/test_review_queue.py
git commit -m "feat: add suggested standard-name columns to graph review queue"
```

---

### Task 3: `normalize_and_write_relations()` 接入模糊匹配编排逻辑

**Files:**
- Modify: `app/graphrag/normalization.py`
- Test: `tests/graphrag/test_normalization.py`（Task 1 完成后应有 10 个测试，全部保持不变、必须继续通过）

**Interfaces:**
- Consumes：Task 1 的 `find_fuzzy_candidate_standard_name(name, terms, *, threshold=0.75) -> str | None`；Task 2 的 `enqueue_for_review(..., suggested_subject_standard_name=None, suggested_object_standard_name=None)`。
- Produces：`normalize_and_write_relations()` 的对外签名/返回类型不变（仍是 `(relations, *, terms, graph_client, source, tenant_id, review_conn=None) -> int`），只是内部编排逻辑增加模糊匹配分支。这是本计划最后一个任务，后续没有任务依赖它的内部实现细节。

- [ ] **Step 1: 写失败测试**

在 `tests/graphrag/test_normalization.py` 文件末尾追加（在 Task 1 新增的 4 个测试之后）：

```python
async def test_fuzzy_candidate_goes_to_review_queue_instead_of_auto_writing():
    graph_client = FakeGraphClient()
    review_conn = await aiosqlite.connect(":memory:")
    await ensure_review_schema(review_conn)
    relations = [
        {
            "subject": "网关超时了",  # 模糊匹配"错误码E502"（经由别名"网关超时"）
            "object": "认证模块",  # 精确匹配"登录模块"
            "relation_type": "RELATED_TO",
        }
    ]

    written = await normalize_and_write_relations(
        relations,
        terms=_TERMS,
        graph_client=graph_client,
        source="a.md",
        tenant_id="t1",
        review_conn=review_conn,
    )

    assert written == 0
    assert graph_client.written == []
    pending = await list_pending_reviews(review_conn)
    assert len(pending) == 1
    assert pending[0]["reason"] == "fuzzy_match_needs_confirmation"
    assert pending[0]["subject_candidate"] == "网关超时了"
    assert pending[0]["object_candidate"] == "认证模块"
    assert pending[0]["suggested_subject_standard_name"] == "错误码E502"
    assert pending[0]["suggested_object_standard_name"] is None


async def test_totally_unresolved_candidate_still_uses_unresolved_reason_not_fuzzy():
    # "不存在的实体"和任何术语的相似度都是 0，没有模糊候选——必须继续走
    # 原有的 reason="object_unresolved" 分支，不能被误判成模糊匹配。
    graph_client = FakeGraphClient()
    review_conn = await aiosqlite.connect(":memory:")
    await ensure_review_schema(review_conn)
    relations = [
        {
            "subject": "网关超时",
            "object": "不存在的实体",
            "relation_type": "RELATED_TO",
        }
    ]

    written = await normalize_and_write_relations(
        relations,
        terms=_TERMS,
        graph_client=graph_client,
        source="a.md",
        tenant_id="t1",
        review_conn=review_conn,
    )

    assert written == 0
    pending = await list_pending_reviews(review_conn)
    assert len(pending) == 1
    assert pending[0]["reason"] == "object_unresolved"
    assert pending[0]["suggested_subject_standard_name"] is None
    assert pending[0]["suggested_object_standard_name"] is None
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/graphrag/test_normalization.py -v`
Expected: Task 1 的 10 个测试仍然通过；`test_fuzzy_candidate_goes_to_review_queue_instead_of_auto_writing` 失败在 `assert pending[0]["reason"] == "fuzzy_match_needs_confirmation"`（当前实现还没有模糊匹配分支，"网关超时了"精确匹配失败后直接走原有的 `reason="subject_unresolved"` 分支）；`test_totally_unresolved_candidate_still_uses_unresolved_reason_not_fuzzy` 此时应该已经通过（这是正常的，用于在 Step 4 之后继续保护这条行为不被破坏）。

- [ ] **Step 3: 实现编排逻辑**

打开 `app/graphrag/normalization.py`，把 `normalize_and_write_relations()` 里这一段（循环体的开头部分）：

```python
    written = 0
    for relation in relations:
        subject_std = resolve_to_standard_name(relation["subject"], terms)
        object_std = resolve_to_standard_name(relation["object"], terms)
        if subject_std is None or object_std is None:
            logger.info(
                "关系候选未能对齐术语表，丢弃 subject=%s object=%s",
                relation["subject"],
                relation["object"],
            )
            if review_conn is not None:
                reason = "subject_unresolved" if subject_std is None else "object_unresolved"
                await enqueue_for_review(
                    review_conn,
                    subject_candidate=relation["subject"],
                    object_candidate=relation["object"],
                    relation_type=relation["relation_type"],
                    reason=reason,
                )
            continue
```

改成：

```python
    written = 0
    for relation in relations:
        subject_std = resolve_to_standard_name(relation["subject"], terms)
        object_std = resolve_to_standard_name(relation["object"], terms)
        if subject_std is None or object_std is None:
            suggested_subject = (
                None
                if subject_std is not None
                else find_fuzzy_candidate_standard_name(relation["subject"], terms)
            )
            suggested_object = (
                None
                if object_std is not None
                else find_fuzzy_candidate_standard_name(relation["object"], terms)
            )
            if suggested_subject is not None or suggested_object is not None:
                logger.info(
                    "关系候选模糊匹配到建议标准名，转人工审核 subject=%s "
                    "(建议=%s) object=%s (建议=%s)",
                    relation["subject"],
                    suggested_subject,
                    relation["object"],
                    suggested_object,
                )
                if review_conn is not None:
                    await enqueue_for_review(
                        review_conn,
                        subject_candidate=relation["subject"],
                        object_candidate=relation["object"],
                        relation_type=relation["relation_type"],
                        reason="fuzzy_match_needs_confirmation",
                        suggested_subject_standard_name=suggested_subject,
                        suggested_object_standard_name=suggested_object,
                    )
                continue
            logger.info(
                "关系候选未能对齐术语表，丢弃 subject=%s object=%s",
                relation["subject"],
                relation["object"],
            )
            if review_conn is not None:
                reason = "subject_unresolved" if subject_std is None else "object_unresolved"
                await enqueue_for_review(
                    review_conn,
                    subject_candidate=relation["subject"],
                    object_candidate=relation["object"],
                    relation_type=relation["relation_type"],
                    reason=reason,
                )
            continue
```

（`normalize_and_write_relations()` 函数体的其余部分——`merge_relation` 调用、`ValueError` 处理、`written += 1`——保持不变，不在这一步的改动范围内。）

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/graphrag/test_normalization.py -v`
Expected: 12 passed（Task 1 的 10 个 + 本任务新增的 2 个）

- [ ] **Step 5: 跑全量测试**

Run: `.venv/Scripts/python.exe -m pytest tests/ -q`
Expected: 除了 1 个已知无关的预先存在失败之外全部通过。特别注意：`app/ingestion/graph_extraction.py`/`app/ingestion/pipeline.py` 等调用 `normalize_and_write_relations()` 的上游代码没有改动这个函数的对外签名，理论上不应该受影响；如果发现有其他测试因为这个改动失败，需要先排查是否有遗漏的调用方依赖了"精确失败就必然是 subject_unresolved/object_unresolved 二选一"这个旧行为细节，不要跳过直接忽略。

- [ ] **Step 6: 提交**

```bash
git add app/graphrag/normalization.py tests/graphrag/test_normalization.py
git commit -m "feat: route fuzzy-matched entity candidates to review queue instead of auto-writing"
```

---

## 完成后

任务全部提交后，`normalize_and_write_relations()` 在精确匹配失败时不再只有"丢弃/进队列但无建议"这一种结局——能模糊匹配上的候选会连同建议标准名一起进入人工审核队列，`review_queue.py` 的 `list_pending_reviews()` 直接带出建议名，方便人工审核时参考，同时不改变"模糊命中不自动写入图谱"这条经过用户确认的核心安全约束。架构覆盖度审计标记的这一项行为偏差解决，本次会话拆分出的 4 个独立子项目（检索层修正、TermGuard 模糊匹配、输入/输出安全增强、GraphRAG 实体链接模糊匹配）全部完成。
