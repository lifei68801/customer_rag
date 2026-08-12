# 知识图谱审核补充原文证据 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 LLM 抽取知识图谱候选关系时同时输出支持这条关系的原文引用，存进人工审核队列，并在审核页面（待审核+历史记录两个 tab）展示出来——现状审核员只能看到"两个候选词字符串 + 关系类型 + 一个未翻译的内部原因代码"，没有任何办法验证文档里是不是真的这么说，只能凭字面意思猜。

**Architecture:** 证据沿着现有的抽取→归一化→审核队列这条链路往下传一层：`llm_extractor.py` 的输出 schema 加一个 `evidence` 字段（LLM 给出的原文引用）→ `normalization.py` 把它透传给 `enqueue_for_review`（只有进人工审核队列的候选才需要，精确对齐术语表、直接自动写图的那条路径不碰）→ `review_queue.py` 给表加一列、查询函数把这一列带出来→前端把它渲染出来。范围明确限定在人工审核队列这一条路径，不涉及 Neo4j 边（`merge_relation`/`Neo4jGraphClient` 签名不变），也不需要改 `app/api/admin_graph_review_routes.py`——`ReviewListResponse.reviews` 本来就是 `list[dict]`，查询函数多返回一列，会自动透传到 API 响应里，不用改路由代码。

**Tech Stack:** 后端 FastAPI + aiosqlite，测试用 pytest。前端 React + TypeScript——项目没有配置任何前端自动化测试框架，验证手段是 `npm run typecheck` + `npm run build` + 手动核对。

## Global Constraints

- `evidence` 必须是可选的、有合理默认值的字段（LLM 没给出/给出空值时不能让整条候选关系抽取失败）——抽取阶段用空字符串兜底，不是 `None`，避免下游拼接/展示时到处判空（跟这张表现有的 `source` 列同一个约定）。
- 现有的 `enqueue_for_review` 调用方（`tests/graphrag/test_review_cli.py`、`tests/api/test_admin_graph_review_routes.py` 等，本次改动不touch这些文件）不能因为新增这个参数而报错——`evidence` 必须有默认值，不能改成必填。
- 只改人工审核队列这条路径；`app/graphrag/neo4j_client.py`（`merge_relation`/`Neo4jGraphClient`）、`app/graphrag/provenance.py` 均不在本次改动范围内。
- 每个任务改完就提交一次。

---

## Task 1: 抽取阶段输出原文证据

**Files:**
- Modify: `app/graphrag/llm_extractor.py`（`_SYSTEM_PROMPT` 常量、`extract_candidate_relations` 的解析循环）
- Test: `tests/graphrag/test_llm_extractor.py`

**Interfaces:**
- Produces: `extract_candidate_relations(...) -> list[dict[str, str]]`，返回的每个 dict 除了原有的 `subject`/`object`/`relation_type`，固定多一个 `evidence` 键（LLM 给出时是原文引用字符串，LLM 未给出时是空字符串 `""`——永远存在这个键，不会缺失）。

- [ ] **Step 1: 写失败的测试**

在 `tests/graphrag/test_llm_extractor.py` 里，把 `test_extracts_relations_from_valid_json_response`（当前第 39-58 行）整个替换成：

```python
async def test_extracts_relations_from_valid_json_response():
    text = (
        '{"relations": ['
        '{"subject": "错误码E502", "object": "登录模块", "relation_type": "RELATED_TO", '
        '"evidence": "出现错误码E502时请检查登录模块状态"}'
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
            "evidence": "出现错误码E502时请检查登录模块状态",
        }
    ]
```

在文件末尾追加两条新测试：

```python
async def test_falls_back_to_empty_string_evidence_when_llm_omits_it():
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
            "evidence": "",
        }
    ]


async def test_system_prompt_requires_evidence_quote_in_output_schema():
    provider = SpyLLMProvider('{"relations": []}')

    await extract_candidate_relations(
        ["片段"],
        llm_registry=_registry(provider),
        llm_provider_name="llm",
        timeout_sec=1.0,
    )

    system_message = provider.received_requests[0].messages[0]["content"]
    assert '"evidence":"..."' in system_message
    assert "原文摘录" in system_message
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/graphrag/test_llm_extractor.py -q`
Expected: FAIL——`test_extracts_relations_from_valid_json_response` 断言的返回值里多了 `evidence` 键，当前实现还不会产出它；另外两条新测试同理会失败。

- [ ] **Step 3: 实现**

把 `app/graphrag/llm_extractor.py` 里的 `_SYSTEM_PROMPT`（当前第 16-43 行）整个替换成：

```python
_SYSTEM_PROMPT = (
    "你是知识图谱关系抽取器。"
    "请从给定文档片段中抽取专有名词之间的关系。"
    '专有名词指在这门具体生意里有实际业务含义、值得单独作为图谱节点的'
    '名词或短语：产品/型号名（如"大床房"）、编号（如"302号房"）、地点名'
    '（如"三楼健身房"）、机构/品牌名（如"某连锁酒店"）、职务/角色头衔'
    '（如"值班经理"），也包括具体的业务流程、状态、动作、活动名称（如'
    '"入住登记""房间异味""更换房间""促销活动"——这些虽然语法上是动作/'
    '状态/类别词，但本身就是这门生意的业务术语，relation_type 里 IS_A/'
    'PART_OF/PRECEDES/ADDRESSED_BY/RELATED_TO 的例子都依赖这类词）；不要'
    '抽取脱离具体业务场景、换成任何行业都通用的空泛填充词，例如孤立出现'
    '的"设备""问题""服务""顾客""流程"这类没有具体指代对象的泛称。'
    '只输出 JSON：{"relations":[{"subject":"...","object":"...",'
    '"relation_type":"RELATED_TO","evidence":"..."}]}。evidence 是原文里'
    '支持这条关系的一句话原文摘录，给人工审核用，必须是原文摘录、不能'
    '改写或概括；实在找不到能直接引用的完整单句时，摘取最贴近的一小段'
    '原文，不要留空。'
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
```

（只改了 JSON 输出格式那一句、加了 evidence 字段的说明；实体范围约束段落、10 种 relation_type 的文字、结尾两句都一个字不动。）

把 `extract_candidate_relations` 函数体内的解析循环（当前第 113-124 行）替换成：

```python
    relations: list[dict[str, str]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        subject = str(item.get("subject", "")).strip()
        obj = str(item.get("object", "")).strip()
        relation_type = str(item.get("relation_type", "")).strip()
        evidence = str(item.get("evidence", "")).strip()
        if subject and obj and relation_type:
            relations.append(
                {
                    "subject": subject,
                    "object": obj,
                    "relation_type": relation_type,
                    "evidence": evidence,
                }
            )
    return relations
```

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/graphrag/test_llm_extractor.py -q`
Expected: PASS，全部用例（包括改动前就有的用例，尤其是两条 `test_system_prompt_*` 断言 10 种 relation_type 和实体范围约束文字还在的用例）通过。

- [ ] **Step 5: 提交**

```bash
git add app/graphrag/llm_extractor.py tests/graphrag/test_llm_extractor.py
git commit -m "feat(graphrag): extract a source-text evidence quote alongside each candidate relation"
```

---

## Task 2: 归一化阶段把证据透传给审核队列

**Files:**
- Modify: `app/graphrag/normalization.py`（`normalize_and_write_relations` 函数体内三处 `enqueue_for_review` 调用）
- Test: `tests/graphrag/test_normalization.py`

**Interfaces:**
- Consumes: Task 1 产出的 `relation` dict 里的 `evidence` 键（用 `.get("evidence", "")` 读取，不用 `relation["evidence"]`——这个函数处理的 `relations` 列表不只来自 Task 1 的真实抽取，也来自测试里手写的、大多数不带 `evidence` 键的候选关系 dict，直接下标访问会在这些既有测试上抛 `KeyError`）
- Produces: 无新对外接口——`normalize_and_write_relations` 的签名和返回值都不变，只是它调用 `enqueue_for_review` 时多传一个 `evidence` 关键字参数（Task 3 会给 `enqueue_for_review` 加上这个可选参数）。

- [ ] **Step 1: 写失败的测试**

在 `tests/graphrag/test_normalization.py` 文件末尾追加：

```python
async def test_enqueued_review_carries_evidence_from_relation_candidate():
    graph_client = FakeGraphClient()
    review_conn = await aiosqlite.connect(":memory:")
    await ensure_review_schema(review_conn)
    relations = [
        {
            "subject": "网关超时",
            "object": "不存在的实体",
            "relation_type": "RELATED_TO",
            "evidence": "文档中提到网关超时时会影响不存在的实体",
        }
    ]

    await normalize_and_write_relations(
        relations,
        terms=_TERMS,
        graph_client=graph_client,
        source="a.md",
        tenant_id="t1",
        now=_NOW,
        review_conn=review_conn,
    )

    pending = await list_pending_reviews(review_conn, tenant_id="t1")
    assert pending[0]["evidence"] == "文档中提到网关超时时会影响不存在的实体"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/graphrag/test_normalization.py::test_enqueued_review_carries_evidence_from_relation_candidate -v`
Expected: FAIL——`list_pending_reviews` 返回的字典里目前还没有 `evidence` 这个键，`pending[0]["evidence"]` 会抛 `KeyError`（Task 3 完成之前，这一步的失败其实是"表里没这一列"，属于预期中的失败，Task 3 做完后这条测试才会真正因为"归一化没有透传 evidence"这个原因而失败/通过——这里先确认它现在确实是失败的）。

- [ ] **Step 3: 实现**

在 `app/graphrag/normalization.py` 的 `normalize_and_write_relations` 函数体内，给三处 `enqueue_for_review` 调用都加上 `evidence=relation.get("evidence", "")`：

第一处（"fuzzy_match_needs_confirmation"分支，当前第 124-135 行）：

```python
                if review_conn is not None:
                    await enqueue_for_review(
                        review_conn,
                        subject_candidate=relation["subject"],
                        object_candidate=relation["object"],
                        relation_type=relation["relation_type"],
                        reason="fuzzy_match_needs_confirmation",
                        source=source,
                        tenant_id=tenant_id,
                        suggested_subject_standard_name=suggested_subject,
                        suggested_object_standard_name=suggested_object,
                        evidence=relation.get("evidence", ""),
                    )
```

第二处（"subject_unresolved"/"object_unresolved"分支，当前第 142-152 行）：

```python
            if review_conn is not None:
                reason = "subject_unresolved" if subject_std is None else "object_unresolved"
                await enqueue_for_review(
                    review_conn,
                    subject_candidate=relation["subject"],
                    object_candidate=relation["object"],
                    relation_type=relation["relation_type"],
                    reason=reason,
                    source=source,
                    tenant_id=tenant_id,
                    evidence=relation.get("evidence", ""),
                )
```

第三处（"invalid_relation_type"分支，当前第 169-178 行）：

```python
            if review_conn is not None:
                await enqueue_for_review(
                    review_conn,
                    subject_candidate=relation["subject"],
                    object_candidate=relation["object"],
                    relation_type=relation["relation_type"],
                    reason="invalid_relation_type",
                    source=source,
                    tenant_id=tenant_id,
                    evidence=relation.get("evidence", ""),
                )
```

（三处都是在已有的调用基础上加一行 `evidence=relation.get("evidence", ""),`，函数体其它部分不动。这一步单独跑测试会先看到 Task 3 没做完导致的 `TypeError: enqueue_for_review() got an unexpected keyword argument 'evidence'`，是预期中的——Task 3 做完 `enqueue_for_review` 才认识这个参数。）

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/graphrag/test_normalization.py -q`
Expected: 这一步单独跑会失败（`enqueue_for_review` 还不认识 `evidence` 参数）——**这是这个任务里唯一一处允许"Step 4 暂时不全绿"的地方**，因为 Task 2 和 Task 3 是同一条链路的上下游，`enqueue_for_review` 的参数是 Task 3 才加。正常情况下应该先做 Task 3 再做 Task 2，但为了让每个任务都能各自独立地"先写测试确认失败"，这里刻意保留了这个短暂的顺序依赖。跑完确认失败原因确实是 `unexpected keyword argument 'evidence'`（而不是别的意外错误）之后，直接进入 Task 3；Task 3 做完后回来跑一次 `tests/graphrag/test_normalization.py -q` 确认全部通过，再进行本任务的 Step 5 提交。

- [ ] **Step 5: 提交**

Task 3 完成、`tests/graphrag/test_normalization.py -q` 全部通过后：

```bash
git add app/graphrag/normalization.py tests/graphrag/test_normalization.py
git commit -m "feat(graphrag): thread extraction evidence through to the review queue"
```

---

## Task 3: 审核队列存证据、查询函数把证据带出来

**Files:**
- Modify: `app/graphrag/review_queue.py`（`ensure_review_schema` 加列迁移；`enqueue_for_review` 加参数+INSERT；`list_pending_reviews`/`list_resolved_reviews` 的 SELECT 加列）
- Test: `tests/graphrag/test_review_queue.py`

**Interfaces:**
- Produces:
  - `enqueue_for_review(..., evidence: str = "") -> int`（新增可选参数，默认空字符串，不传时行为和改动前完全一致）
  - `list_pending_reviews(...)`/`list_resolved_reviews(...)` 返回的每个 dict 里多一个 `"evidence"` 键（历史数据、未传 `evidence` 的记录，值是空字符串 `""`，不是 `None`）

- [ ] **Step 1: 写失败的测试**

在 `tests/graphrag/test_review_queue.py` 文件末尾追加：

```python
async def test_enqueue_with_evidence_is_returned_by_list_pending():
    conn = await _connect()

    await enqueue_for_review(
        conn,
        subject_candidate="网关超时",
        object_candidate="登录模块",
        relation_type="RELATED_TO",
        reason="subject_unresolved",
        source="s.md",
        tenant_id="t1",
        evidence="文档提到网关超时会影响登录模块",
    )

    pending = await list_pending_reviews(conn, tenant_id="t1")
    assert pending[0]["evidence"] == "文档提到网关超时会影响登录模块"


async def test_enqueue_without_evidence_defaults_to_empty_string():
    conn = await _connect()

    await enqueue_for_review(
        conn, subject_candidate="a", object_candidate="b", relation_type="RELATED_TO",
        reason="subject_unresolved", source="s.md", tenant_id="t1",
    )

    pending = await list_pending_reviews(conn, tenant_id="t1")
    assert pending[0]["evidence"] == ""


async def test_list_resolved_reviews_includes_evidence():
    conn = await _connect()
    review_id = await enqueue_for_review(
        conn, subject_candidate="a", object_candidate="b", relation_type="RELATED_TO",
        reason="subject_unresolved", source="s.md", tenant_id="t1",
        evidence="原文引用示例",
    )
    await reject_review(conn, review_id=review_id, tenant_id="t1")

    resolved = await list_resolved_reviews(conn, tenant_id="t1")
    assert resolved[0]["evidence"] == "原文引用示例"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/graphrag/test_review_queue.py -q`
Expected: FAIL——`enqueue_for_review() got an unexpected keyword argument 'evidence'`（新参数还不存在）。

- [ ] **Step 3: 实现**

在 `app/graphrag/review_queue.py` 的 `ensure_review_schema` 函数体内，紧接着 `source` 列的迁移（当前第 68-71 行）之后插入：

```python
    # evidence 记录支持这条候选关系的原文引用（见 llm_extractor.py），
    # 人工审核时用来判断"文档里是不是真的这么说的"——历史数据没有这个
    # 信息，回填空字符串（不是 NULL，跟 source 同样的理由：下游拼接/
    # 展示时不用到处判空）。
    await add_column_if_missing(
        conn, table="graph_review_queue", column="evidence",
        ddl="TEXT NOT NULL DEFAULT ''",
    )
```

把 `enqueue_for_review`（当前第 92-128 行）整个替换成：

```python
async def enqueue_for_review(
    conn: aiosqlite.Connection,
    *,
    subject_candidate: str,
    object_candidate: str,
    relation_type: str,
    reason: str,
    source: str,
    tenant_id: str,
    suggested_subject_standard_name: str | None = None,
    suggested_object_standard_name: str | None = None,
    evidence: str = "",
) -> int:
    """把未能对齐术语表（或关系类型不合法）的候选关系存入待审核队列，返回 review_id。

    source/tenant_id 是批准时写入图谱边所必需的信息，来自调用方
    normalize_and_write_relations() 本身已有的同名参数，这里改为必填，
    不给默认值——遗漏它们会让批准动作在写图谱这一步直接失败。

    evidence 是抽取阶段 LLM 给出的原文引用（见 llm_extractor.py），给
    人工审核用；默认空字符串——不是所有调用方都一定拿得到这个信息
    （比如未来可能有的非 LLM 抽取来源），不强制要求。
    """
    cursor = await conn.execute(
        "INSERT INTO graph_review_queue "
        "(subject_candidate, object_candidate, relation_type, reason, "
        "suggested_subject_standard_name, suggested_object_standard_name, "
        "source, tenant_id, evidence) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            subject_candidate,
            object_candidate,
            relation_type,
            reason,
            suggested_subject_standard_name,
            suggested_object_standard_name,
            source,
            tenant_id,
            evidence,
        ),
    )
    await conn.commit()
    return cursor.lastrowid
```

把 `list_pending_reviews` 函数体内的 SQL（当前第 144-150 行）里的 SELECT 列表替换成：

```python
    cursor = await conn.execute(
        "SELECT review_id, subject_candidate, object_candidate, relation_type, "
        "reason, suggested_subject_standard_name, suggested_object_standard_name, "
        "source, evidence, created_at FROM graph_review_queue "
        "WHERE status = 'pending' AND tenant_id = ? ORDER BY review_id LIMIT ? OFFSET ?",
        (tenant_id, limit if limit is not None else -1, offset),
    )
```

把 `list_resolved_reviews` 函数体内两处 SQL（当前第 170-186 行）都加上 `evidence`：

```python
    if status is None:
        cursor = await conn.execute(
            "SELECT review_id, subject_candidate, object_candidate, relation_type, "
            "reason, status, resolved_at, resolved_note, source, evidence, created_at "
            "FROM graph_review_queue "
            "WHERE tenant_id = ? AND status IN ('approved', 'rejected') "
            "ORDER BY resolved_at DESC, review_id DESC LIMIT ? OFFSET ?",
            (tenant_id, limit, offset),
        )
    else:
        cursor = await conn.execute(
            "SELECT review_id, subject_candidate, object_candidate, relation_type, "
            "reason, status, resolved_at, resolved_note, source, evidence, created_at "
            "FROM graph_review_queue "
            "WHERE tenant_id = ? AND status = ? "
            "ORDER BY resolved_at DESC, review_id DESC LIMIT ? OFFSET ?",
            (tenant_id, status, limit, offset),
        )
```

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/graphrag/test_review_queue.py -q`
Expected: PASS，全部用例（包括改动前就有的用例）通过。

回头把 Task 2 的测试也跑一遍确认联调通过：

Run: `.venv/Scripts/python.exe -m pytest tests/graphrag/test_normalization.py -q`
Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add app/graphrag/review_queue.py tests/graphrag/test_review_queue.py
git commit -m "feat(graphrag): store and return evidence quote on the review queue"
```

（这一步提交的是 Task 3 自己的改动；Task 2 的改动单独按 Task 2 的 Step 5 提交，两个是各自独立的 commit。）

---

## Task 4: 审核页面展示原文引用和来源文档

**Files:**
- Modify: `frontend/src/admin/GraphReviewsPage.tsx`

**Interfaces:**
- Consumes: Task 3 之后 `GET /api/admin/graph-reviews` 响应里每条记录新增的 `evidence`/`source` 字段（`app/api/admin_graph_review_routes.py` 不需要改，`ReviewListResponse.reviews` 本来就是 `list[dict]`，Task 3 的查询函数一改，这两个字段自动出现在响应里）。

- [ ] **Step 1: 给两个 TypeScript 接口加字段**

把 `PendingReview`（当前第 9-17 行）：

```tsx
interface PendingReview {
  review_id: number
  subject_candidate: string
  object_candidate: string
  relation_type: string
  reason: string
  suggested_subject_standard_name: string | null
  suggested_object_standard_name: string | null
}
```

改成：

```tsx
interface PendingReview {
  review_id: number
  subject_candidate: string
  object_candidate: string
  relation_type: string
  reason: string
  source: string
  evidence: string
  suggested_subject_standard_name: string | null
  suggested_object_standard_name: string | null
}
```

把 `ResolvedReview`（当前第 19-27 行）：

```tsx
interface ResolvedReview {
  review_id: number
  subject_candidate: string
  object_candidate: string
  relation_type: string
  status: string
  resolved_at: string
  resolved_note: string | null
}
```

改成：

```tsx
interface ResolvedReview {
  review_id: number
  subject_candidate: string
  object_candidate: string
  relation_type: string
  source: string
  evidence: string
  status: string
  resolved_at: string
  resolved_note: string | null
}
```

- [ ] **Step 2: 待审核卡片展示来源文档+原文引用**

把待审核列表卡片里的候选信息段落（当前第 274-277 行）：

```tsx
            <p className="text-sm text-ink-soft">
              候选：{review.subject_candidate} —[{review.relation_type}]→{' '}
              {review.object_candidate}（原因：{review.reason}）
            </p>
```

改成：

```tsx
            <p className="text-sm text-ink-soft">
              候选：{review.subject_candidate} —[{review.relation_type}]→{' '}
              {review.object_candidate}（原因：{review.reason}）
            </p>
            <p className="text-xs text-ink-soft">来源文档：{review.source || '（无记录）'}</p>
            {review.evidence && (
              <p className="border-l-2 border-ink pl-2 text-sm italic text-ink">
                原文引用："{review.evidence}"
              </p>
            )}
```

- [ ] **Step 3: 历史记录卡片同样展示来源文档+原文引用**

把历史记录列表卡片（当前第 382-388 行）：

```tsx
            <p className="text-sm text-ink">
              {review.subject_candidate} —[{review.relation_type}]→ {review.object_candidate}
            </p>
            <p className="text-xs text-ink-soft">
              {review.status === 'approved' ? '已批准' : '已驳回'} · {review.resolved_at}
              {review.resolved_note && ` · ${review.resolved_note}`}
            </p>
```

改成：

```tsx
            <p className="text-sm text-ink">
              {review.subject_candidate} —[{review.relation_type}]→ {review.object_candidate}
            </p>
            <p className="text-xs text-ink-soft">来源文档：{review.source || '（无记录）'}</p>
            {review.evidence && (
              <p className="border-l-2 border-ink pl-2 text-sm italic text-ink">
                原文引用："{review.evidence}"
              </p>
            )}
            <p className="text-xs text-ink-soft">
              {review.status === 'approved' ? '已批准' : '已驳回'} · {review.resolved_at}
              {review.resolved_note && ` · ${review.resolved_note}`}
            </p>
```

- [ ] **Step 4: 类型检查 + 构建**

Run（在 `frontend/` 目录下）:
```bash
npm run typecheck
npm run build
```
Expected: 两条命令都无错误退出。

- [ ] **Step 5: 手动验证**

项目没有配置浏览器自动化/前端测试框架，这一步需要人工（或执行计划时环境里可用的浏览器工具）核对：

1. 确认后端已经跑了 Task 1-3（数据库里旧数据的 `evidence` 列会是空字符串，新摄取的文档产生的候选关系会带上真实原文引用）。
2. 打开"知识图谱审核"页面，待审核 tab 下每张候选卡片应该在"候选：...（原因：...）"下面多两行：一行"来源文档：xxx"，一行斜体的"原文引用：'...'"（如果这条候选的 `evidence` 是空字符串，原文引用这一行应该完全不出现，不是留一个空壳）。
3. 切到历史记录 tab，同样能看到来源文档和原文引用（针对 Task 1-3 上线之后新产生的历史记录；上线之前就已经在库里的旧记录 `evidence` 是空字符串，只会显示来源文档，不会显示原文引用行）。
4. 找一条候选，对照它显示的"来源文档"字段去摄取目录下找到对应的原始文档，确认"原文引用"这段文字真的能在文档里找到（或者是文档里那句话的合理摘录），验证证据链路是真实可信的，不是随便拼出来的占位文本。

- [ ] **Step 6: 提交**

```bash
git add frontend/src/admin/GraphReviewsPage.tsx
git commit -m "feat(admin): show source document and evidence quote on graph review cards"
```

---

## 全部任务完成后

Run: `.venv/Scripts/python.exe -m pytest tests/ -q`
Expected: 除了已知无关的 `test_returns_none_when_tts_not_configured` 之外全部通过。

Run（`frontend/` 目录下）: `npm run typecheck && npm run build`
Expected: 均无错误退出。
