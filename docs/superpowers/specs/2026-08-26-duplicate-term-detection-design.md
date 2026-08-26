# 术语重复实体检测设计

## 背景

对照开源项目 semantica-agi/semantica（`semantica/deduplication/duplicate_detector.py`）做的架构比较发现：customer_rag 现在完全没有主动发现"两个已存在的 Term 实际指向同一个真实实体"的机制。

`resolve_term()`（`app/graphrag/ontology.py:20-71`）是这个仓库全部"按名字查 Term"路径统一依赖的消歧函数，但它做的是**精确字符串匹配**（`name == t.standard_name or name in t.aliases`），不是相似度计算——它能命中"coke"、"可乐"这类口语化写法，前提是这些写法已经被提前登记进某个 Term 的 `aliases` 列表。如果 ETL 分批导入、或不同渠道各自创建了 "Coca-Cola" 和 "可口可乐"两条独立的 Term（没有人告诉系统它们是同一个实体），`resolve_term()` 对这两条各自都能精确命中，但永远不会把它们识别成"应该是一条"——这正是本次会话早些时候排查"coke-cola公司有多少个订单查不出结果"这个问题时观察到的现象类别之一。

semantica 的 `DuplicateDetector` 用多因子相似度+置信度组合+union-find 传递闭包分组+增量检测，是一个面向"处理海量、持续涌入的实体数据"场景设计的通用去重引擎。customer_rag 的术语表规模小（demo 租户几十条量级）、写入渠道少（ETL批量导入、管理后台手工创建、review_queue 审核通过），不需要照搬这套复杂度——这份设计是一个规模匹配的轻量版本：**创建/审核时点的相似度提示 + 独立后台 worker 定期批跑生成合并建议列表**，所有合并动作都要经过人工审核队列确认，不做任何自动合并。

## 目标

- 术语创建时（管理后台手工创建、ETL 批量导入触发的审核流程），如果新术语的名字/别名跟同 `term_type` 下已有术语的相似度超过阈值，能提示"这个可能和已有的 XX 是同一个实体"。
- 一个独立的后台 worker，定期批量扫描全量术语表（按 `term_type` 分组两两比对），把超阈值的疑似重复对写入一张新的审核队列表。
- 审核人员能在管理后台看到这份"疑似重复"列表，批准（合并成别名关系）或驳回（确认是两个不同实体，以后不再对这一对重复提示）。
- 批准合并只影响 SQLite 的 `terms` 表（把其中一个术语的名字追加进另一个的 `aliases`），**不触碰 Neo4j 里已经写入的图节点/关系**——图数据层面的物理合并（转移边、删除冗余节点）风险高、需要谨慎的事务设计，是一个独立的、更谨慎的后续计划，不在这份设计的范围内。

## 非目标

- 不做 union-find 式的传递闭包分组（A像B、B像C就把三者聚成一组）——customer_rag 的写入渠道少、单次批跑发现的疑似重复对数量级不大，两两独立提示、人工逐条确认，比自动聚类更安全，也更容易审计"这条建议是怎么来的"。
- 不做增量/流式检测（每次新建术语立刻触发一次全量比对）——创建时点的提示只跟同 `term_type` 下已有术语比对（见下方"创建时点提示"，这个操作本身很轻，可以做成同步的），全量批跑仍然是定期离线任务，不在请求路径里。
- 不动 Neo4j 图数据（见上方目标最后一条）。

## 架构

### 相似度算法：复用 `longest_common_substring_score`

`app/graphrag/ontology_recall.py` 里已经有一个经过测试、用于本体候选召回的字符级相似度函数：

```python
def longest_common_substring_score(a: str, b: str) -> float:
    """最长公共连续子串长度（大小写不敏感）除以 b 的长度，归一化成 0~1
    分数……重叠长度小于 _MIN_OVERLAP_LENGTH 个字符时直接返回0"""
```

这份设计直接复用这个函数做术语相似度判断，不引入新的相似度算法——两处场景本质相同（"这两个字符串描述的是不是同一件事"），复用同一套评分逻辑，行为可预期，也不需要再维护第二套相似度实现。判重时取对称的最大值（`max(longest_common_substring_score(a, b), longest_common_substring_score(b, a))`，因为这个函数本身对 `a`/`b` 不对称），比对范围是**候选术语的 `standard_name` 和全部 `aliases`，两两取最高分**。

阈值：新增一个模块级常量 `_DUPLICATE_SIMILARITY_THRESHOLD = 0.6`（比 `ontology_recall.py` 的候选召回阈值 `_MIN_SCORE = 0.3` 更高——召回场景宁可多召回、由后续步骤过滤；这里的场景是"值得打扰人工审核"，阈值要更保守，避免把大量明显不相关的术语对糊进审核队列）。

### 新表：`duplicate_review_queue`

不复用 `graph_review_queue`（`app/graphrag/review_queue.py:13-27`）——那张表的 schema 是专门为关系三元组审核设计的（`subject_candidate`/`object_candidate`/`relation_type`/`suggested_subject_standard_name`/`suggested_object_standard_name`），没有通用的 `kind` 字段，语义上也装不下"一对疑似重复的术语"这种数据形状。新建一张结构匹配的表，复用同一套生命周期模式（pending/resolved 状态机、`resolved_at`+`resolved_note`）：

```sql
CREATE TABLE IF NOT EXISTS duplicate_review_queue (
    review_id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id TEXT NOT NULL,
    candidate_a_node_key TEXT NOT NULL,
    candidate_b_node_key TEXT NOT NULL,
    similarity_score REAL NOT NULL,
    reason TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    resolved_at TEXT,
    resolved_note TEXT
);
CREATE INDEX IF NOT EXISTS idx_duplicate_review_queue_status
    ON duplicate_review_queue (tenant_id, status);
CREATE UNIQUE INDEX IF NOT EXISTS idx_duplicate_review_queue_pair
    ON duplicate_review_queue (tenant_id, candidate_a_node_key, candidate_b_node_key)
    WHERE status = 'pending';
```

`candidate_a_node_key`/`candidate_b_node_key` 存 `node_key`（不是 `standard_name`——`node_key` 创建后永不改变，是术语的稳定身份标识，`standard_name` 可能被改名；参照 `terms` 表本身的既有约定）。`idx_duplicate_review_queue_pair` 这条唯一索引（限定 `status='pending'`）防止同一对术语被批跑重复插入多条 pending 记录——如果这一对之前已经被驳回过（`status='rejected'`），批跑时应该跳过，不再重新插入，具体见下方"驳回后不再提示"。

新建 `app/graphrag/duplicate_review_queue.py`，函数形状照抄 `review_queue.py` 的既有模式：

```python
async def ensure_duplicate_review_schema(conn: aiosqlite.Connection) -> None: ...

async def enqueue_duplicate_suggestion(
    conn: aiosqlite.Connection, *,
    tenant_id: str, candidate_a_node_key: str, candidate_b_node_key: str,
    similarity_score: float, reason: str,
) -> None:
    """INSERT OR IGNORE 到 duplicate_review_queue，撞到 idx_duplicate_review_queue_pair
    唯一索引（这一对已经有一条 pending 记录）时静默跳过，不报错——批跑是幂等的，
    重复调用不会产生重复建议。"""

async def list_pending_duplicate_suggestions(
    conn: aiosqlite.Connection, *, tenant_id: str, limit: int | None = None, offset: int = 0,
) -> list[dict[str, Any]]: ...

async def approve_duplicate_suggestion(
    conn: aiosqlite.Connection, *, review_id: int, tenant_id: str, keep_node_key: str,
) -> None:
    """keep_node_key 是 candidate_a_node_key/candidate_b_node_key 之一——保留哪一条、
    把另一条的 standard_name 追加进这一条的 aliases。被合并掉的那条 Term 本身不删除
    （terms 表这一行依然存在，只是它的 standard_name 变成了 keep_node_key 那条 Term
    的一个 alias）——不删除是因为 node_key 可能已经被其它地方引用（比如已经写入 Neo4j
    的图数据用的就是这个 node_key），删除会破坏已有的引用完整性；只在术语表层面
    建立"这两个名字指向同一个实体"这层关系，查询时 resolve_term() 能通过别名统一
    找到同一条 Term。"""

async def reject_duplicate_suggestion(
    conn: aiosqlite.Connection, *, review_id: int, tenant_id: str, note: str | None = None,
) -> None:
    """status 改成 'rejected'，resolved_note 记录人工给出的理由（可选）。驳回后，
    批跑重新扫描到同一对候选时（见下方"驳回后不再提示"）应该跳过，不再重新入队。"""
```

**`approve_duplicate_suggestion` 的具体合并操作**：调用 `terms_store.py` 现有的 `update_term()`（`terms_store.py:486`），把被合并掉那条 Term 的 `standard_name` 追加进 `keep_node_key` 那条 Term 的 `aliases` 列表，`keep_node_key` 那条自身的 `standard_name`/`term_type`/`extra_properties` 不变。**这个函数不新建**，复用 `update_term()` 已有的别名冲突检查（`_check_name_conflict`，`terms_store.py:357`）——如果追加的别名跟其它术语冲突，`update_term()` 会按已有规则报错，合并操作在这种情况下应该失败并让审核人员看到明确的错误信息，不能静默失败。

### 创建时点的相似度提示

在 `admin_terms_routes.py` 的创建术语路由（`POST /`，`admin_terms_routes.py:114`，内部调用 `create_term`）里，在实际调用 `create_term()` 之前，先用 `list_terms(conn, tenant_id)` 拉出同 `term_type` 下的全部现有术语（术语表规模小，这个操作足够轻，不需要专门的索引/近似搜索结构），把**新术语的 `standard_name`**，跟每条现有术语的 **`standard_name` 和全部 `aliases`**（不只是 `standard_name`，理由见上一节"相似度算法"——只比 `standard_name` 对 `standard_name` 抓不住"新术语的标准名恰好是已有术语的一个别名"这类场景，比如新建 `standard_name="可口可乐"`，而已有术语 `standard_name="Coca-Cola"` 早就把"可口可乐"登记成了别名）逐一计算相似度，两两取最高分，超阈值就在响应里附带一个 `similar_terms: [{node_key, standard_name, similarity_score}]` 字段（不阻断创建，创建请求本身仍然成功——这是"提示"，不是"拦截"）。新术语自己请求体里如果也带了 `aliases`（`create_term` 本身支持这个参数），这一步暂不额外比对这些新别名——创建时点提示优先覆盖"新标准名撞已有术语"这个最常见的场景，新别名撞已有术语属于更边缘的情况，留给批跑 worker（覆盖全字段两两比对）兜底，不在创建时点这个更轻量的检查里重复做。前端收到这个字段后，在创建成功的提示里额外展示"检测到可能相似的已有术语：XX（相似度0.7），要不要改成给它加别名而不是新建"，具体交互细节由前端实现决定，这份设计只定义后端返回的数据形状，不展开前端交互设计。

### 定期批跑 worker

新建 `app/graphrag/duplicate_detection_worker.py`，结构完全照抄 `app/memory/consolidation_worker.py`（`consolidation_worker.py:21-71`）的既有模式——"跑一批就退出"的单次入口，不内置常驻循环，部署为 cron/systemd timer 周期调用：

```python
async def main(
    *,
    settings: Settings | None = None,
    review_conn: aiosqlite.Connection | None = None,
    tenant_id: str | None = None,  # None 时遍历全部租户
) -> int:
    """扫描全量术语表，按 term_type 分组两两比对相似度，超阈值且尚未有
    pending/rejected 记录的候选对写入 duplicate_review_queue。返回本次
    新增的建议条数。

    用法：python -m app.graphrag.duplicate_detection_worker
    """
```

**驳回后不再提示**：批跑扫描到一对候选时，先检查 `duplicate_review_queue` 里这一对（`tenant_id` + 两个 `node_key`，顺序不敏感）是否已经存在**任意状态**（pending 或 rejected）的记录——只有完全没有记录时才新增。`rejected` 的记录不会被清理/重新触发，这是刻意的：人工已经确认过"这两个不是同一个实体"，批跑不应该反复用同样的建议打扰审核人员。

### 管理后台

在现有的审核队列页面（`frontend/src/admin/` 下已有的关系审核界面）里新增一个 tab（"疑似重复术语"），复用同一个页面容器，不新开一个独立页面——审核人员的操作路径基本不变，只是多了一种要处理的事项类型。具体前端组件/路由改动，交给对应的实现任务展开，这份设计只确定"复用同一个审核入口，不新开页面"这个决定。

## Global Constraints

- 相似度算法复用 `app/graphrag/ontology_recall.py::longest_common_substring_score`，不引入新的字符串相似度实现。
- 相似度比对范围统一是"一侧的 `standard_name`，跟另一侧的 `standard_name` 和全部 `aliases`"，两两取最高分——批跑 worker 和创建时点提示都遵守这条，不只比 `standard_name` 对 `standard_name`（否则抓不住"新标准名恰好是已有术语别名"这类场景，这正是这份设计要解决的核心问题）。创建时点提示不额外比对新术语请求体里可能带的 `aliases`，这部分留给批跑 worker 兜底。
- 阈值 `_DUPLICATE_SIMILARITY_THRESHOLD = 0.6`，比 `ontology_recall.py` 的召回阈值 `0.3` 更保守。
- `duplicate_review_queue` 是新表，不复用 `graph_review_queue`。
- 合并操作只影响 SQLite `terms` 表（通过已有的 `update_term()` 追加别名），不删除被合并的 Term 行本身，不触碰 Neo4j 图数据。
- 批跑 worker 是独立的"跑一批就退出"脚本，模式照抄 `app/memory/consolidation_worker.py`，不在 FastAPI 请求路径里，不内置常驻循环。
- 驳回状态永久生效，批跑不重新提示已驳回的候选对。
- 相似度比对只在同一个 `tenant_id` 内、同一个 `term_type` 内进行，不跨租户、不跨类型（跨类型重名是合法的，见 `docs/superpowers/plans/2026-08-22-cross-type-duplicate-standard-name.md`，这份设计不改变那份计划的前提）。

## 已知缺口 / 后续跟进（实现阶段发现，记录在这里避免变成未文档化的意外）

- **合并后被合并术语原来关联的 Neo4j 边会变得不可达**——这份设计原本假设"被合并那条 Term 的 `standard_name` 保持不变，只是多了一条指向它的别名关系"（见上方"目标"第4条），但实现阶段发现这个假设跟 `update_term()` 现有的别名冲突检查（`_check_name_conflict`）不兼容：如果被合并那条自己的 `standard_name`/`aliases` 原样保留，追加同样的字符串到保留项的 `aliases` 时会被判定为"名字已被占用"而报错。最终实现改成"墓碑化"被合并那条 Term——把它的 `standard_name` 改写成一个不会跟真实术语碰撞的占位名（`[已合并] {node_key}`），`aliases` 清空。这修复了合并操作本身的正确性，但也意味着：合并之后，`resolve_term()` 精确匹配已经不会再匹配到被合并那条 Term 原来的名字（该名字已经变成保留项的一个别名，统一路由到保留项的 `node_key`）——如果 Neo4j 图谱里已经有挂在被合并那条 Term 原 `node_key` 下的关系边，这些边在按名字查询的路径下会变得不可达（除非直接按被合并那条的 `node_key` 查询，但正常查询路径不会这么做）。这份设计最初"不触碰 Neo4j 图数据"的非目标假设的是"合并不影响图数据的可查询性"，现在这个假设不再成立。**后续跟进**：在任何已经有真实 Neo4j 图数据的租户上使用合并功能前，需要先规划一次图数据层面的边迁移（把被合并 `node_key` 下的边转移到保留项的 `node_key` 下），这是一个独立的、更谨慎的后续计划，本次不实现，仅记录风险。
- **创建时点相似度提示（`similar_terms` 字段）目前没有被前端消费**——后端 `POST /api/admin/{tenant}/terms` 正确返回了这个字段（见上方"创建时点的相似度提示"一节），但前端的术语创建流程目前不读取/展示这个字段，这份设计文档里"目标"第1条（创建时提示相似术语）在端到端意义上还没有真正触达用户。**后续跟进**：需要一个单独的前端任务，在创建成功的响应处理里读取 `similar_terms`，参照文档描述的交互（"检测到可能相似的已有术语：XX，要不要改成加别名"）展示出来。
