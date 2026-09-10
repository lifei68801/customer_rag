# 数字人从租户上解耦 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 一个租户能挂多张脸——「要几张脸」不再决定「切几刀隔离」。

**Architecture:** 隔离仍然只有 `tenant_id` 一条维度，一处不加。改的只是"脸"的身份：`tenant_personas` 主键 `tenant_id` → `(tenant_id, persona_id)`，会话带上 `persona_id`，前台右栏在同租户内切脸时不再切租户。

**Spec:** `docs/adr/0004-tenant-boundary-follows-isolation-not-persona.md`

**为什么是现在：** 库里 `tenant_personas` / `user_tenants` / `organizations` 全是空表，只有 `demo` 一个租户有真实数据。**零迁移**。等多个数字人各自建完本体导完数据再改，要改写每行和每个 Neo4j 节点的 `tenant_id`。

## Global Constraints

同 `2026-09-08-multi-persona-foundation.md` 的十二条，逐字适用。额外四条：

13. **不加第二条隔离维度。** `persona_id` 是展示维度，**绝不出现在任何 WHERE 里当权限判据**。谁能看哪些数据仍然只由 `tenant_id` + `require_tenant_access` 决定。任何一处把 persona_id 用作过滤条件都是把 ADR-0004 明确否掉的东西偷偷放进来。
14. **默认脸的 id 固定是 `"default"`。** 没配过脸的租户由 API 合成一张 `default` 脸（name 退回租户名、avatar/tagline 空）——正是今天的行为，前端不用改判断。
15. **会话的 `persona_id` 回填成 `"default"` 而不是 NULL。** 存量 96 条会话本来就属于那个租户唯一的一张脸，这是准确的，不是猜的。NULL 会逼每个读取方处理"没有脸的会话"这个不存在的状态。
16. **persona 仍然对问答行为零影响。** 不加系统提示词、不加语气、不加工具权限——那是另一条 ADR 的事（ADR-0004「这条 ADR 不主张的事」）。

---

## File Structure

| 文件 | 职责 |
|---|---|
| `app/graphrag/tenant_personas_store.py`（改） | 主键改复合键；`list_personas` / `create_persona` / `delete_persona` |
| `app/memory/schema.py`（改） | `chat_sessions.persona_id`，默认 `'default'` |
| `app/memory/chat_sessions.py`（改） | 写入带 persona_id；列表按 persona 过滤 |
| `app/api/admin_personas_routes.py`（改） | 列表返回多脸；按 persona_id 读写；建/删 |
| `frontend/src/lib/personasApi.ts`（改） | Persona 带 persona_id |
| `frontend/src/components/PersonaRail.tsx`（改） | 同租户内切脸不切租户 |
| `frontend/src/pages/ChatPage.tsx`（改） | 切脸的两种情形分开 |
| `frontend/src/admin/PersonaPage.tsx`（改） | 能建/删/切换在编哪张脸 |

---

## Task 1: `tenant_personas` 支持一个租户多张脸

**Files:**
- Modify: `app/graphrag/tenant_personas_store.py`
- Test: `tests/graphrag/test_tenant_personas_store.py`

**Interfaces:**
- Produces:
  - 主键 `(tenant_id, persona_id)`，新增列 `name TEXT NOT NULL DEFAULT ''`
  - `list_personas(conn, tenant_id) -> list[dict]`
  - `create_persona(conn, *, tenant_id, persona_id, name) -> None`
  - `delete_persona(conn, *, tenant_id, persona_id) -> None`
  - 既有函数全部追加 `persona_id: str = "default"` 形参

- [ ] **Step 1: 写失败测试**

```python
async def test_one_tenant_can_carry_two_faces():
    """一个租户两张脸，各自的头像/一句话/引导问题互不干扰。
    这是整个计划的支点。"""

async def test_faces_are_scoped_to_the_tenant():
    """两个租户各建一个同名 persona_id，各自只看到自己的。
    复合主键让同名并存是合法的。"""

async def test_the_default_face_is_what_existing_callers_get():
    """不传 persona_id 的既有调用方拿到的是 'default' 那张脸——
    存量代码一行不用改。"""

async def test_deleting_a_face_leaves_the_others():
    """删一张，另一张还在，租户本身不受影响。"""

async def test_a_blank_persona_id_is_refused():
    """空/纯空白的 persona_id 拒掉。理由同组织那次：空串是合法主键值，
    建出来之后在任何按 id 定位的地方都会跟"没指定"撞车。"""
```

- [ ] **Step 2 – Step 4: 红 → 实现 → 绿**

迁移：`tenant_personas` 今天是**空表**，但仍要按存量库处理——`add_column_if_missing` 加 `persona_id`/`name` 两列，然后重建主键。SQLite 改主键要走"建新表 + 拷 + RENAME"，那是重建型迁移，**后面所有 add_column 都必须排在它之后**（`terms_store.py` 里那段注释解释过为什么）。

- [ ] **Step 5: 变异 + 提交**

```bash
# A：主键仍是 tenant_id 单列      → 预期红 one_tenant_can_carry_two_faces
# B：list_personas 去掉 tenant 过滤 → 预期红 faces_are_scoped_to_the_tenant
# C：默认值不是 'default'          → 预期红 the_default_face_is_what_existing_callers_get
```

```bash
git add app/graphrag/tenant_personas_store.py tests/graphrag/test_tenant_personas_store.py
git commit -m "feat(persona): 一个租户能挂多张脸，主键改成 (tenant_id, persona_id)"
```

---

## Task 2: 会话带上 `persona_id`

**Files:**
- Modify: `app/memory/schema.py`、`app/memory/chat_sessions.py`
- Test: `tests/memory/test_chat_sessions.py`

- [ ] **Step 1: 写失败测试**

```python
async def test_a_session_belongs_to_one_face():
    """spec 裁决补充「一个会话属于一个数字人」。persona==tenant 时这是免费的，
    解耦之后要显式维护。"""

async def test_listing_sessions_is_filtered_by_face():
    """切到另一张脸，左栏列的是那张脸的会话。同一个租户下两张脸各聊各的。"""

async def test_existing_sessions_backfill_to_the_default_face():
    """这一列之前写进来的会话回填成 'default'——它们本来就属于那个租户唯一的
    那张脸。回填成 NULL 的话，每个读取方都要处理一个不存在的状态。"""
```

- [ ] **Step 2 – Step 5: 红 → 实现 → 绿 → 变异 → 提交**

```bash
# A：list 不按 persona 过滤 → 预期红 listing_sessions_is_filtered_by_face
# B：回填成 NULL           → 预期红 existing_sessions_backfill_to_the_default_face
```

---

## Task 3: personas API 支持多脸

**Files:**
- Modify: `app/api/admin_personas_routes.py`
- Test: `tests/api/test_admin_personas_routes.py`

**Interfaces:**
- `GET /api/admin/personas` → 每个可访问租户的**每一张脸**各一项，带 `persona_id`
- `GET|PUT /api/admin/{tenant_id}/persona/{persona_id}`
- `POST /api/admin/{tenant_id}/personas` 建脸 · `DELETE .../{persona_id}` 删脸

> **实际落地的路由形状与上面不同**（执行时的裁决）：
> - `GET|PUT /api/admin/{tenant_id}/persona?persona_id=<id>`（查询参数，缺省 `default`）
> - `GET|POST /api/admin/{tenant_id}/persona/faces` 列脸 / 建脸 · `DELETE .../persona/faces/{persona_id}` 删脸
>
> 原因：`/persona/{persona_id}` 会跟已有的 `/persona/stale-questions` 抢路径——FastAPI 按注册顺序匹配，
> `stale-questions` 会被当成一个 persona_id。对接方以 `app/api/admin_personas_routes.py` 为准。

- [ ] **Step 1: 写失败测试**

```python
def test_a_tenant_with_two_faces_lists_both():
    """右栏要列出两张，不是一张。"""

def test_a_tenant_with_no_face_still_lists_one():
    """没配过脸的租户合成一张 default，name 退回租户名——今天的行为不变。"""

def test_faces_of_an_unauthorized_tenant_do_not_appear():
    """**这一条是安全核心**：多脸不能成为绕过授权的新路径。
    member 只看得到被授权租户的脸。"""

def test_writing_a_face_of_an_unauthorized_tenant_is_refused():
    """读挡住了不等于写挡住了，两条路径各断言一次。"""

def test_deleting_the_last_face_is_refused():
    """删光了这个租户在右栏就消失了，用户会以为租户没了。
    至少留一张。"""
```

- [ ] **Step 2 – Step 5: 红 → 实现 → 绿 → 变异 → 提交**

```bash
# A：列表不按 accessible 过滤   → 预期红 faces_of_an_unauthorized_tenant_do_not_appear
# B：写入路径不校验授权         → 预期红 writing_a_face_of_an_unauthorized_tenant
# C：允许删到零张               → 预期红 deleting_the_last_face_is_refused
```

**做完 Task 3 后端就完整了。** 前端两个任务可以单独排期。

---

## Task 4: 前台右栏切脸不切租户

**Files:**
- Modify: `frontend/src/lib/personasApi.ts`、`frontend/src/components/PersonaRail.tsx`、`frontend/src/pages/ChatPage.tsx`
- Test: `frontend/src/components/personaRail.test.tsx`

- [ ] **Step 1: 写失败测试**

```tsx
it('同一个租户下切脸，不发切租户的请求', async () => {
  // 今天 onSelect={setTenantId}，切脸会 PUT 一次当前租户。同租户内切脸
  // 不该动租户——发了的话，一次纯展示的切换会连带刷新整个知识库上下文。
})

it('跨租户切脸，仍然要先切租户再切脸', async () => {
  // 反面：不切的话，用户点了别的租户的脸，问答还在原租户的知识里跑。
})

it('切脸之后左栏会话列表跟着换', async () => {})

it('右栏用 (租户, 脸) 两段标识，不是只用租户', async () => {
  // 只用 tenant_id 当 key 的话，同一个租户的两张脸会被 React 当成同一项。
})
```

- [ ] **Step 2 – Step 5: 红 → 实现 → 绿 → 变异 → 提交**

---

## Task 5: 后台能建/删/切换编辑哪张脸

**Files:**
- Modify: `frontend/src/admin/PersonaPage.tsx`
- Test: `frontend/src/admin/persona.test.tsx`

没有这一步，「一个租户挂多张脸」就没有入口，等于没做完。

- [ ] **Step 1: 写失败测试**

```tsx
it('能新建一张脸并切过去编辑', async () => {})
it('删除时如果只剩一张，按钮给出理由而不是静默失败', async () => {})
it('切换编辑哪张脸时，引导问题跟着换', async () => {})
```

- [ ] **Step 2 – Step 5: 红 → 实现 → 绿 → 变异 → 提交**

---

## Task 6: 收尾验证

- [ ] **Step 1: 后端全量 + 前端全量 + tsc**
- [ ] **Step 2: 手工核查（追加进 `docs/superpowers/MANUAL-VERIFICATION.md`）**

1. 给 `demo` 建第二张脸 → 前台右栏出现两项
2. 切到第二张 → 欢迎语和引导问题换了，**但问答答案不变**（脸是纯展示层）
3. 切脸时打开网络面板 → **没有**切租户的 PUT
4. 左栏会话列表跟着脸换；切回去，之前的会话还在
5. 存量的 96 条会话在 `default` 那张脸下，一条不少
6. 后台把第二张脸删掉 → 右栏回到一项；再删最后一张 → 被拒绝并说明理由

---

## Self-Review

**Spec coverage**：ADR-0004 的「Consequences」四条各对应一个任务；「不主张的事」两条（不加行为、不动存量租户）在 Global Constraints 16 和本计划范围里各钉了一次。

**Type consistency**：`persona_id: str` 在 store / API / 前端三处同名同型；默认值 `"default"` 是唯一的魔法串，定义在 store 里，其余各处引用它。

**已知的执行前不确定项**：`PersonaPage.tsx` 的实际文件名要确认（可能叫别的）；`chat_sessions` 的读取方有几处要一起改，实现时 grep 一遍。
