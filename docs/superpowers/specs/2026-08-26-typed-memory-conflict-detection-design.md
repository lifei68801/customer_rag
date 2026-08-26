# 记忆冲突检测类型化升级设计

## 背景

对照开源项目 semantica-agi/semantica 的 `semantica/conflicts/conflict_detector.py` 做的架构比较发现：它对知识冲突有一套类型化分类（值冲突/类型冲突/关系冲突/时间冲突/逻辑冲突）+ 严重度/置信度打分，判断过程是规则驱动、结构化、可审计的。

customer_rag 现在的冲突处理（`app/memory/conflict_resolver.py::resolve_memory_actions`，第23-70行）是一次 LLM 调用直接产出 `ADD`/`UPDATE`/`DELETE`/`NONE` 四选一的决策，没有中间的"这是哪种类型的冲突"这一层。这次的目标是引入 semantica 分类框架背后的思路，但有两层调整，不是照搬：

1. **不照搬"规则驱动打分"**——semantica 能用确定性规则（关键字段检测、数值差异量级）打分，前提是它处理的是结构化的键值属性；customer_rag 的记忆条目是自由文本（`app/memory/fact_extractor.py::extract_facts` 抽出来的是一句话，比如"用户偏好邮件联系"），没有"字段"、"数值"这些结构化维度可比，规则打分无从谈起。这份设计采用的是**分类和决策依然都由 LLM 完成，但输出结构从"裸决策"变成"先分类、再决策"**，换来的是更可审计、更容易发现系统性误判的记录，不是脱离 LLM 的确定性规则引擎。

2. **不照搬全部五个类别，只取三个**——semantica 的分类对象是知识图谱里的结构化实体（多实体、有类型标签、实体间有关系边）；customer_rag 这份 spec 处理的是自由文本、**单一主体**的记忆条目（几乎都是围绕"用户"这一个实体的陈述），跟"多实体互相关联、带类型标签"这种数据形状不是一回事。逐个类别核对下来：`value`（值变化）、`temporal`（时间先后不一致）、`logical`（语义互斥需推理）在自由文本、单主体场景下都能找到清晰的真实场景；`type`（同一实体被打上不同类型标签）在这套记忆模型里没有对应物——记忆条目本身不是"带类型字段的实体"，没有"类型"这个维度可以冲突；`relationship`（两个实体间关系的陈述矛盾）依赖"图里有实体A、实体B、A-B之间有一条关系边"这个前提，customer_rag 的记忆条目通常只围绕用户自身的属性/偏好，不建模"用户与其它具体实体的关系"。这两类在这套内容形态下用不上，硬留着只会给 LLM 添加无意义的分类选项、增加误分类噪音，**这份设计只采用三分类：`value`/`temporal`/`logical`**。

## 目标

- `resolve_memory_actions` 的 LLM 输出增加一个 `conflict_type` 字段（三选一：`value`/`temporal`/`logical`；`ADD`/`NONE` 场景没有真正的"冲突"，`conflict_type` 允许为空），随决策一起落进 `memory_history`。
- 在发起 LLM 调用之前，加一道确定性预过滤：新事实文本跟已有记忆文本完全一致时，直接短路判 `NONE`，不发起 LLM 调用——现在这条规则只存在于超时/失败的降级路径（`_fallback_actions`，第100-113行），正常路径下即使完全重复也会正常调一次 LLM。
- `DELETE` 决策的 prompt 层面要求：LLM 给出的 `reason` 必须引用新事实的具体内容作为依据，不能是空泛的理由。
- 顺带修复一个现有的小缺口：`app/memory/action_executor.py::apply_memory_actions` 的 `UPDATE`/`DELETE` 分支调用 `append_history()` 时，`old_text` 参数固定传 `None`（第100、119行）——`memory_history` 表本身有 `old_text` 这一列，但从来没被真正填过，这次改动顺手把它填上（`UPDATE`/`DELETE` 前先查一次当前的 `text`，见下方"修复 old_text"）。

## 非目标

- 不把记忆条目从自由文本改造成结构化键值事实——那是一次牵动整条记忆流水线（抽取→存储→召回→冲突决策）的结构性改造，工作量远超"给冲突决策加类型化"这一件事本身，讨论时明确排除。
- 不引入脱离 LLM 判断的确定性严重度/置信度打分规则。
- **不做记忆过期/保留策略**——讨论时明确排除：现在写入长期记忆的内容偏"长期稳定的用户画像/偏好"，基于时间的自动清理会误伤低频提及但依然重要的信息（比如"用户对某成分过敏"可能几个月不被提及但依旧关键），这类内容该被淘汰的时机应该是"有新事实明确否定/更新了它"（也就是这份设计要升级的冲突检测本身），不是"时钟走了多少天"。
- 不新增记忆条目的溯源字段——`memory_items.confidence` 和 `memory_history` 现有的 event/old_text/new_text/reason 已经够用，更细粒度的"这条记忆最初来自哪一轮对话原文"可以通过 `consolidation_jobs`/`conversation_turns` 按时间窗口反查，不需要在 `memory_items` 上加冗余关联字段。
- `DELETE` **已经是软删除**（`memory_store.py::mark_deleted`，第52-60行，`UPDATE memory_items SET status='deleted'`，不是真的从表里删行；`list_active_memory_items` 已经过滤 `status = 'active'`）——这不是新工作，这份设计只是在现有软删除基础上，给 `DELETE` 这个决策本身的产出质量加约束（见目标第3条），不改变软删除机制本身。

## 架构

### `memory_history` 表新增列

```sql
ALTER TABLE memory_history ADD COLUMN conflict_type TEXT;
```

用 `app/db_migrations.py::add_column_if_missing`（`review_queue.py` 已经在用这个工具函数做类似的列迁移，照抄同一个模式）做这次迁移，不是直接改 `_SCHEMA_SQL` 里的 `CREATE TABLE`（SQLite 的 `CREATE TABLE IF NOT EXISTS` 对已存在的表不会补列，已经建过库的部署环境需要显式的 `ALTER TABLE` 迁移路径）。

### `resolve_memory_actions` 的 prompt/输出 schema 调整

`app/memory/conflict_resolver.py` 的 `_SYSTEM_PROMPT`（第13-20行）改写：

```python
_SYSTEM_PROMPT = (
    "你是记忆冲突决策器。根据新事实和历史记忆，为每条新事实决定动作和冲突类型。"
    "动作仅允许 ADD/UPDATE/DELETE/NONE："
    "ADD=历史不存在该信息；UPDATE=同主题但内容更新（需给出 target_memory_id）；"
    "DELETE=新事实明确否定旧事实（需给出 target_memory_id，reason 必须引用新事实"
    "的具体内容作为依据，不能是空泛的理由）；NONE=重复或无价值。"
    "冲突类型（conflict_type）仅在 UPDATE/DELETE 时给出，三选一："
    "value=同一属性的值发生变化（如住址、联系方式偏好）；"
    "temporal=不同时间点的陈述不一致，需要判断谁更新；"
    "logical=语义上互斥、需要推理才能发现的矛盾。"
    "ADD/NONE 不需要 conflict_type。"
    '只输出 JSON：{"actions":[{"event":"...","target_memory_id":"","text":"...",'
    '"reason":"...","conflict_type":"..."}]}'
)
```

`_parse_actions`（第73-97行）新增对 `conflict_type` 的解析：

```python
conflict_type = str(item.get("conflict_type", "")).strip().lower()
if conflict_type not in {"value", "temporal", "logical"}:
    conflict_type = ""
actions.append({
    "event": event,
    "memory_id": str(item.get("target_memory_id", "")).strip(),
    "text": str(item.get("text", "")).strip(),
    "reason": str(item.get("reason", "")).strip(),
    "conflict_type": conflict_type,
})
```

非法/缺失的 `conflict_type` 归一化成空字符串，不是拒绝整条 action——分类判断本身允许出错/缺失，不应该因为分类字段有问题就丢弃一个本来合法的 ADD/UPDATE/DELETE 决策。`_fallback_actions`（第100-113行，超时/失败降级路径）产出的 action 也要补上 `"conflict_type": ""`，保持返回结构一致，调用方不需要区分"走的是正常路径还是降级路径"。

### 精确文本去重预过滤

`resolve_memory_actions` 函数体最前面（`asyncio.wait_for` 发起 LLM 调用之前）新增：

```python
existing_texts = {str(item.get("text", "")).strip() for item in existing_memories}
llm_facts = []
short_circuit_actions = []
for fact in new_facts:
    text = fact.strip()
    if text in existing_texts:
        short_circuit_actions.append(
            {"event": "NONE", "memory_id": "", "text": text, "reason": "精确文本重复", "conflict_type": ""}
        )
    else:
        llm_facts.append(fact)
if not llm_facts:
    return short_circuit_actions
# 原有的 LLM 调用逻辑，new_facts 换成 llm_facts；返回前把 short_circuit_actions
# 和 LLM 产出的 actions 合并
```

这一步只做精确字符串相等判断（跟 `_fallback_actions` 现在的判重逻辑完全一致），不做相似度计算——相似度层面的"这两句话说的是不是同一件事"判断，本来就是留给 LLM 通过 `conflict_type` 分类去做的事，这里的预过滤只处理"字面完全一样"这种最明确、不需要任何判断力的情况。

### 修复 `old_text`

`app/memory/action_executor.py::apply_memory_actions`（第78-104行 `UPDATE` 分支、第106-122行 `DELETE` 分支）在调用 `append_history()` 之前，先查一次这条记忆当前的 `text`：

```python
# UPDATE/DELETE 分支共用：查当前文本作为 old_text
cursor = await conn.execute(
    "SELECT text FROM memory_items WHERE memory_id = ? AND tenant_id = ? AND user_id = ?",
    (memory_id, tenant_id, user_id),
)
row = await cursor.fetchone()
old_text = row[0] if row else None
```

把这个 `old_text` 传给 `append_history(..., old_text=old_text, ...)`，替换掉现在硬编码的 `None`。这个查询要在 `upsert_memory_item()`/`mark_deleted()` **之前**执行（拿到变更前的旧值），不能在之后查（那时候 `text` 已经被新值覆盖，`mark_deleted` 也不会清空 `text` 列，但语义上"变更前的值"应该在变更发生前读取，避免未来代码调整顺序时引入难以察觉的 bug）。

`append_history()` 的调用点顺带把 `conflict_type` 传进去（`memory_store.py::append_history` 的签名新增一个可选参数 `conflict_type: str | None = None`，INSERT 语句相应补上这一列）：

```python
async def append_history(
    conn: aiosqlite.Connection, *,
    memory_id: str, tenant_id: str, user_id: str, event: str,
    old_text: str | None, new_text: str | None, reason: str | None,
    conflict_type: str | None = None,
) -> None: ...
```

`apply_memory_actions` 从 `action.get("conflict_type") or None` 取值传入。

## Global Constraints

- 分类和决策依然完全由 LLM 完成，不引入脱离 LLM 的确定性打分规则。
- `conflict_type` 三选一：`value`/`temporal`/`logical`（不采用 semantica 的 `type`/`relationship` 两类——这套记忆模型是自由文本、单一主体，没有"实体类型标签"和"实体间关系边"这两个维度可以对应），`ADD`/`NONE` 允许为空，非法值归一化成空字符串而不是拒绝整条 action。
- `memory_history` 通过 `add_column_if_missing` 迁移新增 `conflict_type` 列，不改 `_SCHEMA_SQL` 里已有的 `CREATE TABLE` 语句本身（照顾已建库的部署环境）。
- 精确文本去重预过滤只做字符串相等判断，不做相似度计算；相似度层面的判断留给 LLM 的 `conflict_type` 分类。
- `DELETE` 保持现有的软删除机制（`mark_deleted` 不变），这份设计只加强 `DELETE` 决策本身的 prompt 约束（`reason` 必须引用新事实具体内容）。
- `old_text` 的查询必须发生在 `upsert_memory_item()`/`mark_deleted()` 执行之前。
- 不做记忆过期/保留策略，不新增记忆溯源字段——这两条在讨论阶段已经明确排除，不属于这份设计的范围。
