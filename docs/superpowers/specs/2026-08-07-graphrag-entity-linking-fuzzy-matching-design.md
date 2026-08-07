# GraphRAG 实体链接模糊匹配设计方案

> 状态：设计定稿（经用户确认）
> 背景：架构覆盖度审计发现 `app/graphrag/normalization.py::resolve_to_standard_name(name, terms) -> str | None` 只做精确匹配（`name == term.standard_name or name in term.aliases`），docstring 自己承认"这里用精确匹配……而非 term_matcher 的子串包含匹配——候选名来自 LLM 抽取，通常已经是较短的实体名，用更严格的精确匹配降低误对齐风险"。这是拆分出的 4 个独立子项目（检索层修正、TermGuard 模糊匹配、输入/输出安全增强、GraphRAG 实体链接模糊匹配）里的最后一个。用户判断当前行为"比设计更保守，不一定是缺陷"，但决定仍要做模糊匹配增强，让 LLM 抽取时的轻微用词偏差（如"服务器链接超时" vs 术语表"服务器连接超时"）也能被捕捉，而不是整条关系候选直接丢弃或依赖完全无提示的日志。

## 1. 现状与复用点

`resolve_to_standard_name` 只有一个调用方：`normalize_and_write_relations()`（同文件），在关系抽取归一化流程里对 LLM 抽取出的候选关系的 subject/object 两侧实体名分别做对齐，任一侧未命中就整条候选丢弃（`review_conn` 为 `None` 时只记日志）或写入 `app/graphrag/review_queue.py` 的人工待审核队列（`review_conn` 非空时）。

本次会话已有两个高度相关的先例，但都不能直接照搬：
- `app/graphrag/term_matcher.py::match_terms()`（TermGuard，本次会话第 2 个子项目新增）：滑动窗口 + `difflib.SequenceMatcher`，阈值 0.75，模糊命中直接和精确命中一样触发强制注入，不过 LLM 二次确认。误命中代价很轻（多塞一段可能不相关的图谱上下文）。
- `app/voice/asr_term_correction.py::_find_fuzzy_candidates`：同样的滑动窗口+difflib 算法，模糊候选额外过一道 LLM 判断是否真的替换，误命中代价是改写用户可见的转写文本。

这次的场景风险等级更高：模糊匹配一旦命中错了，会把一条**图谱关系**写歪（比如把 LLM 抽出的"服务器链接超时"误对齐到术语表的"登录模块"），写错的数据后续会被检索/TermGuard 当作正确知识持续使用，污染范围和持续时间都超过前两个先例的"单次误注入"或"单次误改写"。因此本次不采用"模糊命中即自动生效"的路线。

`review_queue.py` 的人工审核队列机制已经存在（`enqueue_for_review`/`list_pending_reviews`/`approve_review`/`reject_review`），为"低置信度候选不直接自动入库"提供了现成的分流出口，本次直接复用，不新建队列机制。

## 2. 设计

### 2.1 相似度算法：复用 difflib 字符串相似度

不引入向量相似度（embedding）。理由：`resolve_to_standard_name` 的候选名是 LLM 抽取出的完整实体名（通常已经是较短的专有名词短语），和术语表标准名/别名之间的偏差主要是"打错字/用词习惯差异"这类字面层面的问题（如"链接"vs"连接"），difflib 字符串相似度已经能覆盖；引入向量相似度需要为每条候选名额外发起一次 embedding 调用（成本/延迟）、为术语表预计算并缓存向量、且相似度量纲和阈值标定都要重新摸索，复杂度明显更高，本次不做（YAGNI——先用简单方案跑出真实数据，需要时再评估）。

### 2.2 新函数：`find_fuzzy_candidate_standard_name`

`app/graphrag/normalization.py` 新增：

```python
def find_fuzzy_candidate_standard_name(
    name: str, terms: list[Term], *, threshold: float = 0.75
) -> str | None
```

对 `name` 和每个术语的标准名+每个别名逐一计算 `difflib.SequenceMatcher(None, name, candidate).ratio()`，**返回相似度最高的那一个候选对应的标准名**（相似度需要 `>= threshold` 才算数，否则返回 `None`；多个候选并列最高时取遍历顺序里先出现的那个）。

这里刻意和 `match_terms()`（TermGuard）的"任意命中就收集，返回一组术语"不同：`match_terms` 是往上下文里塞信息，多塞几个无妨；这里要给人工审核一个**具体的对齐建议**，必须是单一最优解，不是一组模糊候选。

因为 `name` 本身就是 LLM 抽取出的完整候选实体名（不是需要在长文本里逐位置扫描的段落），直接整串比较即可，不需要 TermGuard/ASR 校正那种"滑动窗口在文本里找子串"的算法——这是和前两个先例在实现细节上的本质区别（比较对象不同：那两处是"标准名是否作为子串出现在一段长文本里"，这里是"候选名整体和标准名整体有多相似"）。

阈值默认 0.75，沿用 TermGuard 的保守取值——本次没有真实数据支撑更精确的标定，作为参考起点写在函数 docstring 里，不假装这是权威值。不新增 Settings 配置项，阈值是函数关键字参数的硬编码默认值（沿用 TermGuard/ASR 校正的既定约定）。

`resolve_to_standard_name`（精确匹配）本身**不改动**——函数签名、行为、docstring 都保持原样。模糊匹配是编排层在精确匹配失败后才调用的独立下一层，不侵入精确匹配函数内部。

### 2.3 `normalize_and_write_relations` 编排逻辑

当前逻辑（简化）：

```python
subject_std = resolve_to_standard_name(relation["subject"], terms)
object_std = resolve_to_standard_name(relation["object"], terms)
if subject_std is None or object_std is None:
    # 记日志；review_conn 非空则 enqueue_for_review(reason="subject_unresolved"/"object_unresolved")
    continue
# 两侧都命中，写入图谱
```

调整后：任一侧精确匹配失败时，追加尝试 `find_fuzzy_candidate_standard_name`：

- **两侧都精确命中**：行为完全不变，直接写入图谱。
- **有一侧未精确命中，且该侧（或两侧中至少一侧）能找到模糊候选**：整条候选**不自动写入图谱**，`written` 计数不增加；`review_conn` 非空时调用 `enqueue_for_review`，`reason="fuzzy_match_needs_confirmation"`，把找到的建议标准名通过新增的 `suggested_subject_standard_name`/`suggested_object_standard_name` 参数一并传入（某一侧没有模糊候选、或该侧本来就精确命中了，对应参数传 `None`）。`review_conn` 为空时，日志里同样带出"发现模糊候选建议 X"这条信息，帮助运维排查，但不落库（复用现有"`review_conn` 为空就是不落库"的分支设计，不额外新增行为）。
- **有一侧未精确命中，且两侧都没有任何模糊候选**：保持现有行为不变（`reason="subject_unresolved"`/`"object_unresolved"`的既有判断逻辑，`review_conn` 为空时只记日志丢弃）。

模糊匹配只在精确匹配失败之后才尝试，对已经精确命中的一侧不重复计算——和 TermGuard"精确匹配层不变，模糊匹配层仅对未命中术语跑"的设计原则一致。

### 2.4 `review_queue.py` schema 扩展

`graph_review_queue` 表新增两个可空字段：

```sql
suggested_subject_standard_name TEXT,
suggested_object_standard_name TEXT
```

直接修改 `_SCHEMA_SQL` 里的 `CREATE TABLE IF NOT EXISTS` 语句（用户已确认当前无已部署的旧 schema 数据需要迁移，不写 `ALTER TABLE` 迁移逻辑）。

`enqueue_for_review()` 新增两个同名可选关键字参数，默认 `None`（非模糊场景的现有调用方——`normalize_and_write_relations` 里"两侧都没有模糊候选"的分支——不受影响，两个字段保持 `NULL`）：

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
) -> int
```

`list_pending_reviews()` 的 `SELECT` 语句同步带出这两个新字段，供人工审核界面/CLI 直接参考。

`approve_review()` **不改动**——人工审核时仍然必须显式指定最终写入图谱的 `subject_standard_name`/`object_standard_name`（建议名只是参考，不是自动采纳），这是该函数现有 docstring 就明确强调的设计原则（"正是因为自动归一化时这两个候选名没能命中术语表才会进队列，这里不能再退回自动解析，必须由人明确给出"），本次模糊匹配的"建议"不改变这条原则，只是让人工审核时少一步手动查术语表的功夫。

## 3. 测试

- `find_fuzzy_candidate_standard_name`：命中（相似度超阈值，返回最相似的标准名）、不命中（相似度不足，返回 `None`）、多个候选中正确取相似度最高的那一个，三类用例。
- `normalize_and_write_relations`：新增"一侧模糊命中时不自动写入图谱、进审核队列且带上建议标准名"的集成测试；确认"两侧都精确命中"的现有测试行为不受影响；确认"两侧都无模糊候选"时现有 `reason="subject_unresolved"`/`"object_unresolved"` 行为不受影响。
- `review_queue.py`：新增字段的建表测试（`ensure_review_schema` 后表结构包含新列）、`enqueue_for_review` 写入建议名后 `list_pending_reviews` 能正确带出、不传建议名参数时两个新字段为 `NULL` 的向后兼容测试。

## 4. 范围之外（不做）

- 不改动 `resolve_to_standard_name` 本身的精确匹配契约。
- 不引入向量相似度/embedding（见 2.1 理由）。
- 不写 schema 迁移脚本（`ALTER TABLE`），因为当前无已部署的旧数据需要迁移。
- 不改动 `approve_review()` 的既有已知问题——它调用 `graph_client.merge_relation()` 时缺少 `tenant_id`/`source` 两个必需参数（这是 Neo4j 租户隔离子项目的最终审查阶段发现并记录的独立遗留问题，需要新的接口设计才能修，不在本次范围）。
- 不做模糊匹配阈值的自动调优/AB测试，阈值 0.75 是参考起点，不是最终标定值。
