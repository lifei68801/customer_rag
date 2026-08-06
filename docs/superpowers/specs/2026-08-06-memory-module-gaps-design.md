# 记忆模块剩余架构缺口设计

> 状态：设计定稿（经用户逐节确认）
> 对应 docs/ARCHITECTURE.md §6 记忆模块深化设计
> 范围：4 项独立子设计，统一写在本文档，实施时按各自优先级分批执行

---

## 背景

对照 `docs/ARCHITECTURE.md` §6 与当前 `app/memory/` 实现，识别出 4 项尚未覆盖的缺口：

1. P1 结构化历史检索（按客户ID+时间窗口）未实现——`recall.py` 目前只做了语义向量、长期记忆条目、BM25 三路
2. "记忆纠错入口"目前不是即时通道——客户说"你记错了"走的是异步 consolidation 队列排队处理
3. 主动性引擎只有"工单挂起过久"一种真实触发检测——"已知故障修复后主动告知""客户说稍后再试后主动确认"两种触发只有文案模板、没有检测逻辑
4. 短期记忆用 SQLite，不是架构文档设想的 Redis——`docker-compose.yml` 里的 Redis 服务完全没被引用

这 4 项彼此相对独立（不同的查询机制/对话流程分支/触发检测逻辑/存储后端），但都属于记忆模块，经确认合并写在同一份 spec 里，实施时可以分批做。

---

## 1. P1 结构化历史检索

### 问题

客服场景常见"上周提的那个报错""你上次说的方案"这类按时间定位的追问，现有召回（`recall_memory_items`）只做语义向量+长期记忆+BM25三路，查不到"某个具体时间点发生过的原始对话内容"。

### 考虑过的方案

| 方案 | 说明 | 结论 |
|---|---|---|
| A. 强行并入 RRF+MMR 融合 | 把对话轮次包装成伪 memory_item 参与统一排序 | 放弃——对话轮次没有语义分/置信度分，硬造分数会破坏"不同量纲分数不可比"这条已有原则（RRF/融合层已经明确遵循这条） |
| B（采纳）. 独立上下文块注入 | 不参与融合排序，命中就整体作为一段 system message 拼进去 | 采纳 |

### 架构

```
用户问题 → resolve_time_window(question)（复用 app/memory/temporal_resolver.py，已有 LLM+规则降级链）
  ├─ resolved=False → 跳过，行为不变
  └─ resolved=True，得到 (start, end)
        → query_turns_in_window(conn, tenant_id, user_id, start, end)
             注：跨 session_id 查询——"上周的会话"大概率不是当前 session
        → BM25Index 对窗口内结果按当前问题关键词二次过滤，取 top-K
        → 格式化为 "以下是您在 {时间范围} 提到的相关历史对话：\n{轮次列表}" 的 system message
        → 追加进 memory_context_messages（不参与 RRF/MMR，独立于其余三路）
```

### 组件

- **新增** `app/memory/structured_recall.py`：
  - `query_turns_in_window(conn, *, tenant_id, user_id, start, end) -> list[dict]`：纯 SQL 按 `tenant_id + user_id + created_at BETWEEN` 查询 `conversation_turns`，不限定 `session_id`
  - `search_turns_by_keyword_and_window(conn, *, tenant_id, user_id, start, end, question, top_k) -> list[dict]`：调用上面的窗口查询，结果喂给 `BM25Index` 按 `question` 关键词过滤取 top-K
- **修改** `app/memory/context_injection.py`：`inject_memory_context()` 新增可选参数接住 P1 的格式化结果，拼成独立 system message（不改变已有的长期记忆/近期轮次两段的组装逻辑）
- **修改** `app/memory/recall.py` 或 `graph.py` 的 `memory_recall_node`：在调用 `inject_memory_context()` 之前先跑一次 `resolve_time_window(question, ...)`，结果传入

### 数据流

`memory_recall_node` → `resolve_time_window(question)` → （若解析出窗口）`search_turns_by_keyword_and_window(...)` → 拼入 `inject_memory_context()` 的 system message 列表。

### 错误处理

- `resolve_time_window` 已有完整降级链（LLM 失败/超时/低置信度 → 规则引擎 → `resolved=False`），P1 只需要在 `resolved=False` 时跳过，不引入新的失败模式
- 窗口查询是纯 SQL，无外部依赖，不会失败；空结果就是空列表，不追加 system message

### 测试

- `structured_recall.py` 单元测试：造若干条不同 `session_id`/`created_at` 的 turns，验证窗口边界 + 跨 session 查询 + 关键词二次过滤的准确性
- `graph.py` 集成测试：问题含可解析的时间表达式时，`memory_context_messages` 里出现对应历史对话的 system message；不含时间表达式时完全不受影响

---

## 2. 即时纠错通道

### 问题

客户说"你记错了，其实我用的是 macOS 不是 Windows"这类话时，现有链路仍然走"事实抽取 → 冲突决策 → 写入"的异步 consolidation 队列，跟其它普通对话一样排队处理，不是文档设想的"即时触发同一条决策链路"。

### 交互行为（已确认）

检测到纠错意图后**短路由**：直接确认已更正，不走检索/LLM回答链路——和现有 `fallback_node`"确定性信号直接短路由"是同一个模式。

### 架构

```
input_safety → correction_check_node（新增） → clarification_check_node → term_guard → ...

correction_check_node(state):
  若 memory_conn 未提供 → 返回 {}（跳过，行为不变）
  intent = detect_correction_intent(question, llm_registry, llm_provider_name)
    LLM 优先判断；规则兜底关键词："记错了"/"弄错了"/"不对，应该是"/"更正一下"/"搞错了"
  若 intent 为 True:
    1. find_similar_memory_items(conn, tenant_id=state["tenant_id"], user_id=state["user_id"], ...)
       找该用户现有相似记忆做候选（复用 Task16 窄化逻辑，similarity.py）
    2. fact_extractor 从这句话抽取新事实（复用 fact_extractor.py，不变）
    3. resolve_memory_actions(...) 判定 ADD/UPDATE/DELETE/NONE（复用 conflict_resolver.py，不变）
    4. apply_memory_actions(...) 立即写入 + 审计（复用 action_executor.py，同步调用而非入队）
    5. 按实际执行的 event 类型生成确认话术：
       UPDATE/DELETE → "好的，已经帮您更正为：{text}"
       ADD           → "好的，已经记下：{text}"（说明这其实是新增而非纠正已有内容）
    6. 返回 {"is_correction_handled": True, "fallback_triggered": False, "final_text": <确认话术>}
  否则 → {}（正常往下走，不设置 is_correction_handled）
```

路由：`route_after_input_safety` 判定安全后，不再直接进 `clarification_check_node`，而是先进 `correction_check_node`；`route_after_correction_check` 读取 `is_correction_handled`——为 True 时直接跳到 `output_safety`，**跳过 `clarification_check_node`/`term_guard`/`memory_recall_node`/检索或 Planner/`responder` 全部中间节点**（这句确认话术不需要指代补全、术语注入或检索上下文）；为 False（含意图检测本身失败/降级为"未检测到"的情况）时正常进入 `clarification_check_node`，走完整原有流程。`output_safety_node` 判断是否跳过完整语义审查时，复用它已有的 `fallback_triggered` 检查——第 6 步返回里把 `fallback_triggered` 设为 `False`（这不是兜底话术，是真实执行结果的确认），因此这句确认话术**仍会**过一次完整语义审查，与 `fallback_node` 的固定话术（跳过语义审查）行为不同，这是刻意的区别：确认话术里拼了用户提供的原始文本（`{text}`），不是纯静态模板，值得过一次安全审查。

### 组件

- **新增** `app/memory/correction_intent.py`：`detect_correction_intent(text, *, llm_registry, llm_provider_name, timeout_sec=2.0) -> bool`，LLM 优先 + 规则兜底（写法对齐 `query_rewrite.py`/`temporal_resolver.py` 的既有降级模式）
- **修改** `app/agent/state.py`：`AgentState` 新增 `is_correction_handled: bool`
- **修改** `app/agent/graph.py`：新增 `correction_check_node`，接入路由
- **复用不变**：`fact_extractor.py`、`conflict_resolver.py`、`action_executor.py`、`similarity.py`——这次只是把它们从"异步 worker 里被调用"改成"同步在图节点里被调用"，函数签名和内部逻辑都不变

### 数据流

检测 → 候选窄化 → 抽取 → 决策 → 执行，全部同步在这一轮请求内完成，不产生 `consolidation_job`。这一轮的 `question`/`final_text` 仍会在 `memory_save_node` 里正常走 `append_turn`；由于 `final_text` 是模板化确认话术，`fact_extractor` 对着它抽不出新事实，不会被二次处理进 consolidation 队列。

### 错误处理

- 意图检测失败/超时 → 按"未检测到纠错意图"处理（规则兜底兜底），走正常问答流程——不确定的判断不强行短路由
- `fact_extractor`/`conflict_resolver` 内部 LLM 失败 → 各自已有的降级规则介入，保证一定能得到确定性的 ADD/NONE 兜底结果，不会卡住请求
- 找不到匹配的旧记忆（实际是新增而非纠正）→ 按 ADD 分支措辞处理，不报错

### 测试

- `correction_intent.py` 单元测试：含糊话术（"记错了"/"弄错了"）判 True；正常提问（"网络连不上"）判 False；LLM 失败时规则兜底介入
- graph 层测试：已有记忆条目 + 说"你记错了，其实是 X" → 断言 `final_text` 含确认话术、`memory_items` 表对应条目已更新、且没有调用检索/responder 的 LLM（用 scripted LLM provider 验证调用次数/顺序）

---

## 3. 主动跟进新增两种触发

### 3a. 已知故障修复后主动告知

**架构**：

```
[人工/管理员操作]
register_known_fix(conn, *, tenant_id, description, fixed_at, embedding_registry, embedding_provider_name)
  → 写入 known_fixes 表，同时对 description 做一次 embedding 存起来

scan_and_send_known_fix_followups(conn, *, tenant_id, llm_registry, llm_provider_name, channel, now):
  for fix in list_known_fixes(conn, tenant_id=tenant_id):
    候选工单 = tickets 表里 status='pending' 且 created_at < fix.fixed_at
              （修复之后才提的工单大概率是别的问题，不参与匹配）
    for ticket in 候选工单:
      若 is_already_notified(ticket_id, fix_id) → 跳过
      similarity = cosine(embed(ticket.question), fix.embedding)
      若 similarity >= 阈值（默认 0.5，需结合真实数据标定，参考此前 agent_min_relevance_score 的标定方式）:
        trigger = FollowupTrigger(reason="known_fix_available",
                                   context=f"您反馈的「{ticket.question}」问题已修复")
        result = send_followup_if_allowed(...)
        若 result.sent → mark_notified(ticket_id, fix_id, now)
```

**关键设计点**：**不复用 `tickets.notified_at`**——它是"挂起过久"触发专用标记，语义是"已经因超时跟进过"。已知修复是完全不同的触发原因，一张工单可能先被"挂起过久"通知过，之后又该被"已修复"通知，两者不能共用同一个布尔标记互相掩盖。因此新增独立的 `ticket_fix_notifications` 表，按 `(ticket_id, fix_id)` 维度去重。

**组件**：
- **新增** `app/memory/known_fixes.py`：`ensure_known_fixes_schema`、`register_known_fix(...)`、`list_known_fixes(conn, *, tenant_id)`
- **新增** `app/memory/ticket_fix_notifications.py`：`ensure_schema`、`is_already_notified(conn, *, ticket_id, fix_id)`、`mark_notified(conn, *, ticket_id, fix_id, now)`
- **修改** `app/memory/proactive_scan.py`：新增 `scan_and_send_known_fix_followups(...)`，编排结构与现有 `scan_and_send_ticket_followups` 同构
- **新增** `app/memory/known_fix_cli.py`：`register_known_fix` 的简单 CLI 包装（参考 `app/graphrag/review_cli.py` 风格）——本次不建真实管理后台/API，只提供脚本化录入入口

**错误处理**：
- `register_known_fix` 里 embedding 调用失败应该让异常上抛——这是管理员的主动操作，失败要让操作者知道，不能静默丢弃
- 扫描阶段单条工单 embedding 失败 → 跳过该条 + 记日志，不影响同批次其它工单（与 `proactive_scan.py` 现有的单条失败隔离模式一致）

**测试**：
- `known_fixes.py`/`ticket_fix_notifications.py` 增删查单元测试
- `scan_and_send_known_fix_followups` 用固定 embedding 的 fake provider 测试：相似度够高才通知、`created_at` 晚于 `fixed_at` 的工单不参与匹配、同一 `(ticket, fix)` 不重复通知

### 3b. 客户说"稍后再试"后到时确认

**设计取舍**：不做成新的同步图节点短路由（与纠错通道不同——"稍后再试"通常嵌在一轮正常问答里，例如"我先按您说的重启路由器试试，不行再联系"，这轮该有的答案仍要正常给出）。改为并入现有异步 consolidation 流程，作为 `run_memory_consolidation` 除"事实抽取"外的第二条并行检查。

**架构**：

```
run_memory_consolidation(conn, tenant_id, user_id, session_id, user_input, assistant_output, ...):
  [已有] fact_extractor → conflict_resolver → action_executor
  [新增]
  intent = detect_delay_intent(user_input, llm_registry, llm_provider_name)
    LLM 优先；规则兜底关键词："稍后再试"/"待会试试"/"过会儿再弄"/"我先试试"
  若 intent 为 True:
    time_result = resolve_time_window(user_input, reference_time=now)
    confirm_after 取值规则：
      time_result.resolved 为 True 且 time_result.start > now（解析出的是未来时间）→ 用 time_result.start
      否则（未解析出时间，或解析出的时间早于/等于 now，说明是在追溯而非约定确认时间）→ now + 默认 2 小时
    schedule_delayed_confirmation(conn, tenant_id, user_id, context=user_input, confirm_after)

scan_and_send_delayed_confirmation_followups(conn, *, tenant_id, llm_registry, llm_provider_name, channel, now):
  for item in list_due_confirmations(conn, tenant_id=tenant_id, now=now):
    trigger = FollowupTrigger(reason="delayed_confirmation",
                               context=f"之前您提到{item.context}，想确认一下现在情况如何？")
    result = send_followup_if_allowed(...)
    若 result.sent → mark_confirmed(item.id, now)
```

**组件**：
- **新增** `app/memory/delayed_confirmation.py`：`ensure_schema`、`schedule_delayed_confirmation(conn, *, tenant_id, user_id, context, confirm_after)`、`list_due_confirmations(conn, *, tenant_id, now)`、`mark_confirmed(conn, *, id, now)`
- **新增** `app/memory/delay_intent.py`：`detect_delay_intent(...)`，结构与 `correction_intent.py` 几乎一致（LLM 优先+规则兜底两段式）
- **修改** `app/memory/consolidation.py`：`run_memory_consolidation()` 新增这一步检查
- **修改** `app/memory/proactive_scan.py`：新增 `scan_and_send_delayed_confirmation_followups(...)`

**错误处理**：
- 延迟意图检测失败/超时 → 按"没有延迟意图"处理，不影响这轮已在跑的事实抽取
- `resolve_time_window` 解析失败 → 已有降级（用默认 2 小时兜底），不会导致整个 consolidation 任务失败

**测试**：
- `delay_intent.py` 单元测试
- `run_memory_consolidation` 新增测试：验证"稍后再试"类话语正确写入 `delayed_confirmations`，解析出具体时间时用该时间、解析不到时用默认 2 小时
- `scan_and_send_delayed_confirmation_followups` 测试：到期才通知、通知过不重复

---

## 4. Redis 可插拔后端（会话短期滑窗）

### 背景

当前 `session_window.py` 直接对 SQLite 的 `conversation_turns` 表做 `append_turn`/`get_recent_turns`，功能上没有问题；Redis 是并发扩展性考虑（多进程/高并发下 SQLite 单文件的写入争用），不是功能缺口。

### 架构

```python
class SessionWindowStore(Protocol):
    async def append_turn(self, *, tenant_id, session_id, user_id, role, content) -> None: ...
    async def get_recent_turns(self, *, tenant_id, session_id, limit) -> list[dict]: ...

class SQLiteSessionWindowStore:
    """薄封装现有 append_turn()/get_recent_turns() 自由函数，零行为变化——默认实现。"""
    def __init__(self, conn: aiosqlite.Connection): ...

class RedisSessionWindowStore:
    """key = f"session_turns:{tenant_id}:{session_id}"
    append_turn: RPUSH + LTRIM(只保留最近 max_turns 条) + EXPIRE(刷新滑动过期时间)
    get_recent_turns: LRANGE 全部 + JSON 解析 + 按 limit 截断
    """
    def __init__(self, redis_client, *, max_turns: int = 50, ttl_seconds: int = 86400): ...
```

**关键设计点**：`memory_conn`（一个 aiosqlite 连接）目前身兼数职——长期记忆条目、consolidation 队列、澄清状态、客户画像、工单全挂在上面。这次只把"会话滑窗"这一层拆出来单独走 `SessionWindowStore`，其余仍然用 `memory_conn` 直连——这是架构文档本身的分层（"会话短期记忆"vs"长期结构化记忆"是两个不同生命周期的东西），不是把整个记忆体系都往 Redis 搬。`graph.py`/`agent_routes.py` 因此需要**额外**接一个 `session_window_store` 参数，和 `memory_conn` 并存；不配置 Redis 时，这个 store 就是包着同一个 `memory_conn` 的 SQLite 实现，行为和现在完全一样。

### 组件

- **新增** `app/memory/session_window_store.py`：`SessionWindowStore` Protocol + `SQLiteSessionWindowStore` + `RedisSessionWindowStore`
  - Redis 客户端本身也做成可注入的 Protocol（`RedisClientProtocol`：`rpush`/`ltrim`/`lrange`/`expire`），测试用纯 Python 字典实现的假客户端，不需要真实 Redis 服务
- **修改** `app/config/settings.py`：新增 `session_window_backend: str = "sqlite"`、`redis_url: str | None = None`
- **新增** `app/memory/session_window_factory.py`：`build_session_window_store_from_settings(settings, *, memory_conn) -> SessionWindowStore`——`redis` 后端时才需要装 `redis` 包（新增可选依赖）
- **修改** `app/memory/context_injection.py`/`app/agent/graph.py`：`memory_recall_node`/`memory_save_node` 里原本直接调 `append_turn(conn,...)`/`get_recent_turns(conn,...)` 的地方改成调 `session_window_store.append_turn(...)`/`.get_recent_turns(...)`

### 错误处理

Redis 连接失败应该在启动时（构建 store 时）就报错，不要拖到运行时某次 `append_turn` 才发现——保持"配置错了就快速失败"，不在对话中途才暴雷。

### 测试

- `SQLiteSessionWindowStore` 直接复用现有 `session_window.py` 测试（同一套底层函数，只是多一层薄封装）
- `RedisSessionWindowStore` 用纯 Python 字典实现的假 Redis 客户端测试滑窗截断（`LTRIM` 行为）+ 过期时间设置逻辑
- graph 层测试：验证"不配置 Redis 时行为和现在完全一致"这条兼容性（默认路径的回归测试）

### 范围说明

这次只做会话滑窗这一层的可插拔，不牵动长期记忆/consolidation 队列/工单等其它 SQLite 表——那些没有 TTL 自然过期的需求，硬塞进 Redis 没有实际收益。

---

## 跨项依赖与实施顺序建议

- 第 2 项（即时纠错通道）依赖第 1 项之外的现有模块（`fact_extractor`/`conflict_resolver`/`action_executor`），彼此无依赖，可独立实施
- 第 3a/3b 两项都依赖 `proactive_scan.py`/`followup_engine.py` 现有基础设施，但互相独立，可分开实施
- 第 4 项（Redis）是纯基础设施改造，不依赖前 3 项，可随时插入，也可以最后做（默认 SQLite 路径不变，不阻塞其它工作）
- 建议顺序：① 即时纠错通道（复用现有模块最多，改动面最小）② P1 结构化检索 ③ 已知修复主动告知 ④ 稍后确认 ⑤ Redis 可插拔后端（优先级最低，无功能紧迫性）
