# 管理后台操作顺序连续性 —— 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让「本体构建 → 数据上传加工 → 映射」这条主路径在界面上连续：系统已经知道的事不再让用户重新供一遍，产品已经知道的下一步不再让用户自己去侧边栏找。

**Architecture:** 七个任务。前三个是一条依赖链（新增一张与本体同生命周期的映射表 → 引导流程把映射写进草稿 → 表格导入页按映射来源分流首屏），后四个彼此独立（落地路由分流、前向链接、确认框数据影响、预演转正式执行）。不改本体模型、不改 ETL 语义、不改任何后端算法。

**Tech Stack:** Python 3.12 / FastAPI / aiosqlite / pytest（后端）；React 18 + TypeScript + Vite + vitest + @testing-library/react（前端）

**Spec:** [docs/superpowers/specs/2026-09-03-admin-flow-continuity-design.md](../specs/2026-09-03-admin-flow-continuity-design.md)

## Global Constraints

- 不改本体模型、不改 ETL 语义、不改任何后端算法。本计划只动流程接缝。
- 不合并「引导式本体建模」和「配置构建向导」——它们方向相反、互补（一个从表推本体，一个把表映射到已有本体）。
- 不改导航分组结构（`model` / `ingest` / `review` 三段 + 实体列表、问答诊断两个流程外目的地）。
- 不给本体确认加硬闸门。本项目没有批量迁移实体类型的入口，硬闸门等于给用户一扇锁死的门。
- 不动 ETL 的删除传播语义与 `allow_large_sweep` 安全阀（默认关闭这一层不改）。
- **未知态不许折叠进任何已知态。** 凡是依赖异步状态渲染的分支，状态未定时渲染中性/加载态，不许抢先断言一个可能为假的结论。本项目已在 `OntologySchemaPage` 的引导入口上踩过这个坑（`readiness === null` 时不许说「这一步是安全的」）。
- 后端全量：`PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest -q`，当前基线 **1694 passed**。
- 前端全量：`cd frontend && npx vitest run`，当前基线 **34 文件 336 tests**；另需 `npx tsc --noEmit` 和 `npx vite build` 各自通过。
- **每条负向断言都要故意改坏实现确认它变红。** 变异不变红时，先 `grep` 确认变异真的落进了文件——锚点没对上会让变异静默无效，看起来和「断言无效」一模一样。本项目源码是 **CRLF**，脚本改写时锚点要带 `\r\n`。
- **注释里不许写未经验证的因果**（「这个测试防的是 X」）。写之前先让 X 失败一次。
- 前端跨页链接的纯文本必须精确匹配 `NAV_GROUPS` / `NAV_STANDALONE` 里的标签，`frontend/src/admin/emptyStateLinks.test.tsx` 会检查。

## 与 spec 的三处偏差（实施前必读）

**一、Task 7 比 spec 估的便宜得多：运行输入已经永久保留在磁盘上。** spec 说「要存数据文件 + 一条保留策略，有磁盘与隐私成本」。实测 `app/api/admin_schema_etl_routes.py::start_schema_etl_run` 已经把 `config.yaml` 和全部数据文件写进 `upload_dir / "schema-etl" / {tenant_id} / {run_id}`，而四处 `shutil.rmtree` **全部在早期错误路径上**（配置解析失败、tenant 不一致、扩展名非法、并发冲突），成功跑完之后没有任何代码清理这个目录。所以 Task 7 不需要新增存储，只需要一个复用既有目录的新端点。spec 的「待定 2（保留策略）」因此**不阻塞本计划**——现状就是永久保留，本计划不改变它，也不新增保留量。

**二、Task 6 不需要新后端接口。** spec 说「差异计算多一次聚合查询」。实测 `GET /api/admin/{tenant_id}/terms/summary`（`admin_terms_routes.py:154`，走 `count_terms_merged_by_term_type`）已经返回按实体类型分组的条数，前端已有 `fetchTermsSummary`（`frontend/src/admin/termsApi.ts:45`）。Task 6 只在前端把它 join 进差异。

**三、5B′（租户开局进度）不在本计划内。** spec 的「待定 1」明说它的价值取决于租户规模，而规模未定。等规模确认后单独起计划。

---

## File Structure

**新建：**

| 文件 | 职责 |
|---|---|
| `app/graphrag/ontology_etl_mapping.py` | `ontology_etl_mapping` 表的建表与读写。照 `ontology_categories.py` 的范式：模块级 `_SCHEMA_SQL` + `ensure_*_schema` + 若干访问函数 |
| `tests/graphrag/test_ontology_etl_mapping.py` | 上表的单元测试 |
| `frontend/src/admin/AdminLanding.tsx` | `/admin` 索引路由的落地分流组件（三态：未知/未确认/已确认） |
| `frontend/src/admin/adminLanding.test.tsx` | 落地分流的测试 |
| `frontend/src/admin/etlMappingApi.ts` | 读取本体草稿/已确认版本上挂的 ETL 映射 |
| `frontend/src/admin/schemaEtlPage.test.tsx` | 表格导入页首屏与预演转正式的测试（该文件当前不存在） |
| `frontend/src/admin/forwardLinks.test.tsx` | 三个完成点前向出口的测试 |
| `frontend/src/admin/ontologyDiff.test.ts` | 确认差异数据影响的测试（该文件当前不存在则新建） |

**修改：**

| 文件 | 改什么 |
|---|---|
| `app/graphrag/ontology_lifecycle.py` | 新表加进 `_TABLES_WITH_TENANT_LIFECYCLE`、`ensure_ontology_schema`、`checkout_draft`；`replace_draft` 增加可选参数 |
| `app/api/admin_ontology_routes.py` | `ReplaceDraftRequest` 增加可选 `etl_mapping`；新增 `GET /{tenant_id}/etl-mapping` |
| `app/api/admin_schema_etl_routes.py` | 新增 `POST /runs/{run_id}/promote` |
| `frontend/src/App.tsx` | 索引路由改用 `AdminLanding` |
| `frontend/src/admin/guidedOntology/GuidedOntologyPage.tsx` | 提交草稿时一并提交映射；YAML 构造抽成共用函数 |
| `frontend/src/admin/SchemaEtlPage.tsx` | 首屏按映射来源分流；构建器改名；预演报告加「正式执行」；跑完给前向出口 |
| `frontend/src/admin/OntologySchemaPage.tsx` | 确认成功后的前向链接；确认差异带数据影响 |
| `frontend/src/admin/ontologyDiff.ts` | `DiffRow` 增加 `impact?: string` |

---

## Task 1: `ontology_etl_mapping` 表与生命周期集成

**Files:**
- Create: `app/graphrag/ontology_etl_mapping.py`
- Create: `tests/graphrag/test_ontology_etl_mapping.py`
- Modify: `app/graphrag/ontology_lifecycle.py`（`_TABLES_WITH_TENANT_LIFECYCLE` 在 `:74`、`ensure_ontology_schema` 在 `:90`、`checkout_draft` 在 `:120`、`confirm_ontology` 在 `:202`）

**Interfaces:**
- Produces:
  - `EtlMapping` dataclass：`config_yaml: str`、`source_file_name: str`、`created_at: str`
  - `async ensure_etl_mapping_schema(conn: aiosqlite.Connection) -> None`
  - `async set_draft_etl_mapping(conn, tenant_id: str, *, config_yaml: str, source_file_name: str, created_at: str) -> None`
  - `async get_etl_mapping(conn, tenant_id: str, *, status: str) -> EtlMapping | None`

- [ ] **Step 1: 写失败测试**

新建 `tests/graphrag/test_ontology_etl_mapping.py`：

```python
import aiosqlite
import pytest

from app.graphrag.ontology_etl_mapping import (
    ensure_etl_mapping_schema,
    get_etl_mapping,
    set_draft_etl_mapping,
)
from app.graphrag.ontology_lifecycle import (
    checkout_draft,
    confirm_ontology,
    ensure_ontology_schema,
    replace_draft,
)


@pytest.fixture
async def conn():
    async with aiosqlite.connect(":memory:") as c:
        await ensure_ontology_schema(c)
        yield c


async def _seed_ontology_draft(conn) -> None:
    """给一份最小的本体草稿——确认动作需要它才会真正执行。"""
    await replace_draft(
        conn,
        "t1",
        term_types=[{"value": "客户", "extra_fields": []}],
        relation_types=[],
        constraints=[],
    )


async def test_draft_mapping_survives_confirm(conn):
    await _seed_ontology_draft(conn)
    await set_draft_etl_mapping(
        conn,
        "t1",
        config_yaml="entities: []",
        source_file_name="orders.csv",
        created_at="2026-09-03T00:00:00",
    )
    await confirm_ontology(conn, "t1")
    confirmed = await get_etl_mapping(conn, "t1", status="confirmed")
    assert confirmed is not None
    assert confirmed.config_yaml == "entities: []"
    assert confirmed.source_file_name == "orders.csv"
    # 草稿被原地提升，不再是 draft。
    assert await get_etl_mapping(conn, "t1", status="draft") is None


async def test_mapping_alone_does_not_trigger_confirm(conn):
    """只有映射、没有本体草稿时，确认必须早退。

    confirm_ontology 的早退判据（has_draft_in_any_table）刻意**不含**映射表：
    一份没有本体草稿的映射不构成"有内容要确认"，把它算进去会让确认误以为
    有东西要提升，从而删掉已确认的本体。
    """
    await set_draft_etl_mapping(
        conn,
        "t1",
        config_yaml="entities: []",
        source_file_name="orders.csv",
        created_at="2026-09-03T00:00:00",
    )
    await confirm_ontology(conn, "t1")
    assert await get_etl_mapping(conn, "t1", status="confirmed") is None
    assert await get_etl_mapping(conn, "t1", status="draft") is not None


async def test_checkout_copies_confirmed_mapping_to_draft(conn):
    """检出要把映射一起复制。

    不复制的话：用户确认后再去本体结构页改两笔（那会触发 checkout_draft），
    映射就只剩 confirmed 那份；等他再确认一次，confirm 会先删掉 confirmed
    再提升 draft——而 draft 里没有映射行，映射凭空消失。
    """
    await _seed_ontology_draft(conn)
    await set_draft_etl_mapping(
        conn,
        "t1",
        config_yaml="entities: []",
        source_file_name="orders.csv",
        created_at="2026-09-03T00:00:00",
    )
    await confirm_ontology(conn, "t1")
    await checkout_draft(conn, "t1")
    draft = await get_etl_mapping(conn, "t1", status="draft")
    assert draft is not None
    assert draft.source_file_name == "orders.csv"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest tests/graphrag/test_ontology_etl_mapping.py -q`

Expected: FAIL，`ModuleNotFoundError: No module named 'app.graphrag.ontology_etl_mapping'`

- [ ] **Step 3: 建模块**

新建 `app/graphrag/ontology_etl_mapping.py`：

```python
"""引导流程产出的 ETL 映射，与本体同生命周期。

为什么挂在本体上而不是让用户保管一个下载下来的 YAML：这份映射描述的是
"这个本体的实体从哪张表的哪几列来"，它本来就是本体定义的一部分。放在
用户的下载目录里会有一个没人管的问题——用户重跑引导覆盖了草稿，旧映射
还躺在磁盘上，两者已经对不上，而没有任何东西告诉他。

表结构与三张本体表同构（tenant_id + status 两列），因此加进
ontology_lifecycle._TABLES_WITH_TENANT_LIFECYCLE 就能白拿 confirm_ontology
的原子提升：那个循环对每张表做"删 confirmed + 把 draft 提升成 confirmed"，
对任何带这两列的表都成立。
"""

from dataclasses import dataclass

import aiosqlite

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS ontology_etl_mapping (
    tenant_id        TEXT NOT NULL,
    status           TEXT NOT NULL,
    config_yaml      TEXT NOT NULL,
    source_file_name TEXT NOT NULL,
    created_at       TEXT NOT NULL,
    PRIMARY KEY (tenant_id, status)
);
"""


@dataclass(frozen=True)
class EtlMapping:
    config_yaml: str
    source_file_name: str
    created_at: str


async def ensure_etl_mapping_schema(conn: aiosqlite.Connection) -> None:
    await conn.executescript(_SCHEMA_SQL)
    await conn.commit()


async def set_draft_etl_mapping(
    conn: aiosqlite.Connection,
    tenant_id: str,
    *,
    config_yaml: str,
    source_file_name: str,
    created_at: str,
) -> None:
    """整份替换草稿映射。一个租户的草稿只有一份，没有增量语义。"""
    await conn.execute(
        "INSERT OR REPLACE INTO ontology_etl_mapping "
        "(tenant_id, status, config_yaml, source_file_name, created_at) "
        "VALUES (?, 'draft', ?, ?, ?)",
        (tenant_id, config_yaml, source_file_name, created_at),
    )
    await conn.commit()


async def get_etl_mapping(
    conn: aiosqlite.Connection, tenant_id: str, *, status: str
) -> EtlMapping | None:
    cursor = await conn.execute(
        "SELECT config_yaml, source_file_name, created_at FROM ontology_etl_mapping "
        "WHERE tenant_id = ? AND status = ?",
        (tenant_id, status),
    )
    row = await cursor.fetchone()
    if row is None:
        return None
    return EtlMapping(config_yaml=row[0], source_file_name=row[1], created_at=row[2])
```

- [ ] **Step 4: 接进生命周期**

在 `app/graphrag/ontology_lifecycle.py` 顶部 import（本任务只用到建表那一个，`set_draft_etl_mapping` 留到 Task 2 再加——现在就导入会留下一个未使用的名字）：

```python
from app.graphrag.ontology_etl_mapping import ensure_etl_mapping_schema
```

`_TABLES_WITH_TENANT_LIFECYCLE`（当前在 `:74`）加一项：

```python
_TABLES_WITH_TENANT_LIFECYCLE = (
    "tenant_relation_types", "term_type_relation_allowlist", "ontology_term_types",
    # 引导产出的 ETL 映射与本体同生命周期，见 ontology_etl_mapping.py。
    # 注意它**不**参与 confirm_ontology 的 has_draft_in_any_table 早退判据。
    "ontology_etl_mapping",
)
```

`ensure_ontology_schema`（`:90`）里补一行 `await ensure_etl_mapping_schema(conn)`。

`checkout_draft`（`:120`）里，在既有三条「从已确认版本复制成草稿」的 `INSERT OR IGNORE` 旁边加第四条：

```python
    await conn.execute(
        "INSERT OR IGNORE INTO ontology_etl_mapping "
        "(tenant_id, status, config_yaml, source_file_name, created_at) "
        "SELECT tenant_id, 'draft', config_yaml, source_file_name, created_at "
        "FROM ontology_etl_mapping WHERE tenant_id = ? AND status = 'confirmed'",
        (tenant_id,),
    )
```

`confirm_ontology` 的 `has_draft_in_any_table` **不改**——它现在检查的三张表保持原样。

- [ ] **Step 5: 跑测试确认通过**

Run: `PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest tests/graphrag/test_ontology_etl_mapping.py -q`

Expected: 3 passed

- [ ] **Step 6: 三次变异验证**

依次做，每次跑 `pytest tests/graphrag/test_ontology_etl_mapping.py -q`，确认变红后还原：

1. 从 `_TABLES_WITH_TENANT_LIFECYCLE` 里删掉 `"ontology_etl_mapping"` → `test_draft_mapping_survives_confirm` 应 FAIL
2. 把 `"ontology_etl_mapping"` 加进 `has_draft_in_any_table` 的判据 → `test_mapping_alone_does_not_trigger_confirm` 应 FAIL
3. 删掉 `checkout_draft` 里新加的那条 `INSERT OR IGNORE` → `test_checkout_copies_confirmed_mapping_to_draft` 应 FAIL

三次都变红才算数。任何一次不变红，先 `grep` 确认变异真的落进了文件。

- [ ] **Step 7: 全量与提交**

Run: `PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest -q`

Expected: 1697 passed（1694 + 3）

```bash
git add app/graphrag/ontology_etl_mapping.py app/graphrag/ontology_lifecycle.py tests/graphrag/test_ontology_etl_mapping.py
git commit -m "feat(graphrag): ETL 映射与本体同生命周期

引导产出的映射此前只能下载成文件由用户保管。用户重跑引导覆盖草稿后，旧
映射还躺在下载目录里、和新本体对不上，没有任何东西告诉他。

表与三张本体表同构，加进 _TABLES_WITH_TENANT_LIFECYCLE 即白拿
confirm_ontology 的原子提升。刻意不加进 has_draft_in_any_table：一份没有
本体草稿的映射不构成有内容要确认。"
```

---

## Task 2: 引导流程把映射写进草稿

**Files:**
- Modify: `app/graphrag/ontology_lifecycle.py`（`replace_draft` 在 `:245`）
- Modify: `app/api/admin_ontology_routes.py`（`ReplaceDraftRequest` 在 `:471`、`replace_ontology_draft` 在 `:477`）
- Create: `frontend/src/admin/etlMappingApi.ts`
- Modify: `frontend/src/admin/guidedOntology/GuidedOntologyPage.tsx`
- Test: `tests/api/test_admin_ontology_routes.py`、`frontend/src/admin/guidedOntology/guidedSubmit.test.tsx`

**Interfaces:**
- Consumes: Task 1 的 `set_draft_etl_mapping`、`get_etl_mapping`、`EtlMapping`
- Produces:
  - `replace_draft(conn, tenant_id, *, term_types, relation_types, constraints, etl_mapping: dict | None = None)`——`etl_mapping` 形如 `{"config_yaml": str, "source_file_name": str}`
  - `GET /api/admin/ontology/{tenant_id}/etl-mapping?status=draft|confirmed` → `{"mapping": {"config_yaml": str, "source_file_name": str, "created_at": str} | null}`
  - 前端 `fetchEtlMapping(sessionToken: string, tenantId: string, status: 'draft' | 'confirmed'): Promise<EtlMapping | null>`

- [ ] **Step 1: 写失败的后端测试**

追加到 `tests/api/test_admin_ontology_routes.py`（`client` / `tenant` 两个 fixture 照该文件既有用例的写法，不要新造）：

```python
async def test_replace_draft_stores_etl_mapping(client, tenant):
    """引导一次提交同时写本体草稿和映射。

    分两次请求写不行：中途失败会留下一份没有映射的草稿，而用户不知道
    映射没写进去——他在表格导入页会看到"从头配置"的界面。
    """
    response = await client.post(
        f"/api/admin/ontology/{tenant}/draft/replace",
        json={
            "term_types": [{"value": "客户", "extra_fields": []}],
            "relation_types": [],
            "constraints": [],
            "etl_mapping": {
                "config_yaml": "entities: []",
                "source_file_name": "orders.csv",
            },
        },
    )
    assert response.status_code == 200

    got = await client.get(f"/api/admin/ontology/{tenant}/etl-mapping?status=draft")
    assert got.status_code == 200
    assert got.json()["mapping"]["source_file_name"] == "orders.csv"


async def test_replace_draft_without_mapping_leaves_it_absent(client, tenant):
    """etl_mapping 是可选的。

    本体结构页那三个 tab 也会改草稿，它们不带映射；不能因为没带就把引导
    写的映射抹掉，也不能凭空造一份。
    """
    response = await client.post(
        f"/api/admin/ontology/{tenant}/draft/replace",
        json={"term_types": [], "relation_types": [], "constraints": []},
    )
    assert response.status_code == 200
    got = await client.get(f"/api/admin/ontology/{tenant}/etl-mapping?status=draft")
    assert got.json()["mapping"] is None
```

- [ ] **Step 2: 跑测试确认失败**

Run: `PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest tests/api/test_admin_ontology_routes.py -q -k etl_mapping`

Expected: FAIL，404（`/etl-mapping` 端点不存在）

- [ ] **Step 3: 后端实现**

`ontology_lifecycle.py` 的 import 补上 `set_draft_etl_mapping`（Task 1 只导入了 `ensure_etl_mapping_schema`）：

```python
from app.graphrag.ontology_etl_mapping import ensure_etl_mapping_schema, set_draft_etl_mapping
```

`replace_draft`（`ontology_lifecycle.py:245`）增加关键字参数：

```python
async def replace_draft(
    conn: aiosqlite.Connection,
    tenant_id: str,
    *,
    term_types: list[dict],
    relation_types: list[dict],
    constraints: list[dict],
    etl_mapping: dict | None = None,
) -> None:
```

函数体末尾（三张表都写完之后）：

```python
    # 映射跟本体草稿同一次提交落库。为 None 时**不动**已有映射——本体结构页
    # 那三个 tab 改草稿时不带映射，不能因此把引导写的映射抹掉。
    if etl_mapping is not None:
        await set_draft_etl_mapping(
            conn,
            tenant_id,
            config_yaml=etl_mapping["config_yaml"],
            source_file_name=etl_mapping["source_file_name"],
            created_at=datetime.now().isoformat(),
        )
```

（`datetime` 若该模块尚未导入，加 `from datetime import datetime`。）

`admin_ontology_routes.py`：

```python
class DraftEtlMappingPayload(BaseModel):
    config_yaml: str
    source_file_name: str


class ReplaceDraftRequest(BaseModel):
    term_types: list[DraftTermTypePayload]
    relation_types: list[DraftRelationTypePayload]
    constraints: list[DraftConstraintPayload]
    etl_mapping: DraftEtlMappingPayload | None = None
```

`replace_ontology_draft` 调用处透传：

```python
            etl_mapping=payload.etl_mapping.model_dump() if payload.etl_mapping else None,
```

新增读取端点（放在该 router 里，注意不要被形如 `/{tenant_id}/...` 的通配路由抢先匹配）：

```python
@router.get("/{tenant_id}/etl-mapping")
async def get_ontology_etl_mapping(
    tenant_id: str,
    status: str = "draft",
    review_conn: aiosqlite.Connection = Depends(deps.get_review_conn),
) -> dict:
    """读该租户挂在本体上的 ETL 映射。表格导入页用它决定首屏形态。"""
    await require_active_tenant_or_404(review_conn, tenant_id)
    if status not in ("draft", "confirmed"):
        raise HTTPException(status_code=400, detail="status 只能是 draft 或 confirmed")
    mapping = await get_etl_mapping(review_conn, tenant_id, status=status)
    if mapping is None:
        return {"mapping": None}
    return {
        "mapping": {
            "config_yaml": mapping.config_yaml,
            "source_file_name": mapping.source_file_name,
            "created_at": mapping.created_at,
        }
    }
```

- [ ] **Step 4: 跑测试确认通过**

Run: `PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest tests/api/test_admin_ontology_routes.py -q -k etl_mapping`

Expected: 2 passed

- [ ] **Step 5: 后端两次变异验证**

1. 把 `replace_draft` 里的 `if etl_mapping is not None:` 改成 `if False:` → `test_replace_draft_stores_etl_mapping` 应 FAIL
2. 把它改成无条件执行（去掉 `if`，`etl_mapping` 为 None 时用空串兜底）→ `test_replace_draft_without_mapping_leaves_it_absent` 应 FAIL

两次都确认后还原。

- [ ] **Step 6: 写失败的前端测试**

先读 `frontend/src/admin/guidedOntology/GuidedOntologyPage.tsx` 的 `handleSubmit` 与 `handleDownloadMapping`，确认当前 YAML 是怎么构造的（`toEtlBuilder` + `buildConfigYaml` + `GUIDED_FILE_ID`）。

追加到 `frontend/src/admin/guidedOntology/guidedSubmit.test.tsx`（`submitGuidedFlow` 按该文件既有辅助函数的风格组织，返回被 stub 捕获的 `/draft/replace` 请求）：

```tsx
it('写入草稿时把 ETL 映射一并提交，不让用户自己保管文件', async () => {
  // 映射是系统自己算出来的。让用户下载成文件再传回来，中途会关标签页、
  // 会在下载目录里找不着、过两天会分不清哪个 YAML 对应哪个租户。
  const posted = await submitGuidedFlow()
  const body = JSON.parse(posted.body as string)
  expect(body.etl_mapping.source_file_name).toBe('orders.csv')
  expect(body.etl_mapping.config_yaml).toMatch(/entities:/)
})
```

- [ ] **Step 7: 前端实现**

`GuidedOntologyPage.tsx`：把 `handleDownloadMapping` 里构造 YAML 的那段抽成共用函数：

```ts
  /**
   * 引导收集的信息已经够生成映射了。抽出来给提交和下载共用——两处各算一次
   * 的话，两份结果可能不一致，那时以哪个为准没有答案。
   */
  const buildMappingYaml = (): { yaml: string; fileName: string } | null => {
    if (!roled || !decision || !uploadedFile) return null
    const { entities, relations } = toEtlBuilder(roled, decision, GUIDED_FILE_ID)
    return {
      yaml: buildConfigYaml({
        tenantId,
        entities,
        relations,
        files: [
          { id: GUIDED_FILE_ID, file: uploadedFile, columns: roled.map((c) => c.stats.name) },
        ],
      }),
      fileName: uploadedFile.name,
    }
  }
```

`handleDownloadMapping` 改为调用它；`handleSubmit` 的 POST body 增加：

```ts
        etl_mapping: (() => {
          const built = buildMappingYaml()
          return built ? { config_yaml: built.yaml, source_file_name: built.fileName } : null
        })(),
```

新建 `frontend/src/admin/etlMappingApi.ts`：

```ts
import { adminFetch, extractErrorDetail } from './adminApi'

export interface EtlMapping {
  config_yaml: string
  source_file_name: string
  created_at: string
}

/** 读挂在本体上的 ETL 映射。表格导入页用它决定首屏形态。 */
export async function fetchEtlMapping(
  sessionToken: string,
  tenantId: string,
  status: 'draft' | 'confirmed',
): Promise<EtlMapping | null> {
  const response = await adminFetch(
    `/api/admin/ontology/${encodeURIComponent(tenantId)}/etl-mapping?status=${status}`,
    sessionToken,
  )
  if (!response.ok) {
    const body = await response.json().catch(() => ({}))
    throw new Error(extractErrorDetail(body, '加载 ETL 映射失败'))
  }
  return ((await response.json()) as { mapping: EtlMapping | null }).mapping
}
```

- [ ] **Step 8: 前端变异验证**

把 `handleSubmit` body 里的 `etl_mapping` 改成恒 `null` → 新增那条测试应 FAIL。确认后还原。

- [ ] **Step 9: 全量与提交**

Run: `PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest -q`（1699 passed）

Run: `cd frontend && npx vitest run && npx tsc --noEmit && npx vite build`（337 tests）

```bash
git add app/graphrag/ontology_lifecycle.py app/api/admin_ontology_routes.py tests/api/test_admin_ontology_routes.py frontend/src/admin/etlMappingApi.ts frontend/src/admin/guidedOntology/GuidedOntologyPage.tsx frontend/src/admin/guidedOntology/guidedSubmit.test.tsx
git commit -m "feat: 引导写入草稿时一并提交 ETL 映射

映射跟本体草稿同一次提交落库。分两次写会留下一份没有映射的草稿，而用户
不知道——他在表格导入页会看到从头配置的界面。

etl_mapping 可选且为 None 时不动已有映射：本体结构页那三个 tab 改草稿时
不带映射，不能因此把引导写的抹掉。"
```

---

## Task 3: 表格导入页首屏按映射来源分流 + 构建器改名

**Files:**
- Modify: `frontend/src/admin/SchemaEtlPage.tsx`（`builderExpanded` 在 `:92`、构建器折叠按钮标题在 `:340`）
- Create: `frontend/src/admin/schemaEtlPage.test.tsx`（render 包装照 `guidedPage.test.tsx` 的写法自建 `signIn` / `renderAt` / `stubApi`，本仓库前端测试没有共享 render 助手）

**Interfaces:**
- Consumes: Task 2 的 `fetchEtlMapping(sessionToken, tenantId, 'confirmed')`、`EtlMapping`

- [ ] **Step 1: 写失败测试**

```tsx
describe('表格导入页首屏', () => {
  it('本体带着引导配好的映射时，只要数据文件，不邀请他重配', async () => {
    // 刚走完引导的用户看到一个邀请他从头配置的界面，等于让他重做刚做完的
    // 工作；重做出来的两份还可能不一致，那时以哪个为准没有答案。
    signIn('admin')
    stubEtlMapping({
      config_yaml: 'entities: []',
      source_file_name: 'orders.csv',
      created_at: '2026-09-03T00:00:00',
    })
    renderAt(ADMIN_ROUTES.etl)
    expect(await screen.findByText(/引导流程已为这个本体配好映射/)).toBeTruthy()
    expect(screen.getByText('orders.csv')).toBeTruthy()
    // 构建器降级成折叠的次级入口，不是主角。
    expect(screen.getByRole('button', { name: /改这份映射／再接一张表/ })).toBeTruthy()
  })

  it('没有映射时维持原样，构建器是主角', async () => {
    signIn('admin')
    stubEtlMapping(null)
    renderAt(ADMIN_ROUTES.etl)
    expect(await screen.findByRole('button', { name: /把这张表映射到已有本体/ })).toBeTruthy()
    expect(screen.queryByText(/引导流程已为这个本体配好映射/)).toBeNull()
  })

  it('映射状态未知时，不抢先渲染任何一种形态', async () => {
    // 抢先渲染"从头配置"会让刚走完引导的用户看到一个邀请他重做的界面，
    // 然后闪一下变掉。未知就是未知，不许折叠进任何一个已知态。
    signIn('admin')
    stubEtlMappingNeverResolves()
    renderAt(ADMIN_ROUTES.etl)
    expect(await screen.findByTestId('etl-mapping-loading')).toBeTruthy()
    expect(screen.queryByText(/引导流程已为这个本体配好映射/)).toBeNull()
    expect(screen.queryByRole('button', { name: /把这张表映射到已有本体/ })).toBeNull()
  })
})
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd frontend && npx vitest run src/admin/schemaEtlPage.test.tsx`

Expected: FAIL

- [ ] **Step 3: 实现**

`SchemaEtlPage.tsx` 增加三态映射状态（`undefined` = 未知）：

```ts
  const [mapping, setMapping] = useState<EtlMapping | null | undefined>(undefined)
```

在既有的加载 effect 旁边拉一次 `fetchEtlMapping(sessionToken, tenantId, 'confirmed')`，失败时 `setMapping(null)`（读不到就按没有映射处理——那条路径不会对用户断言任何假话，只是少一个便利）。

首屏渲染改成三分支：

```tsx
{mapping === undefined ? (
  <div data-testid="etl-mapping-loading" className="text-sm text-ink-soft">
    正在读取这个本体的映射…
  </div>
) : mapping ? (
  <p className={`${card} text-sm text-ink`}>
    引导流程已为这个本体配好映射（来自 <code className="font-mono">{mapping.source_file_name}</code>）。
    传入数据文件即可运行，不用再配一遍。
  </p>
) : null}
```

构建器的折叠按钮标题按 `mapping` 取值：有映射时「改这份映射／再接一张表」，无映射时「把这张表映射到已有本体」。`builderExpanded` 的初值改成跟着 `mapping` 走——有映射时默认折叠，无映射时默认展开；`mapping` 从 `undefined` 变成已知时同步一次。

- [ ] **Step 4: 跑测试确认通过**

Run: `cd frontend && npx vitest run src/admin/schemaEtlPage.test.tsx`

Expected: 3 passed

- [ ] **Step 5: 两次变异验证**

1. 删掉 `mapping === undefined` 分支、未知时走「无映射」形态 → 第三条测试应 FAIL
2. 把有映射那个分支的条件改成恒 false → 第一条测试应 FAIL

- [ ] **Step 6: 全量与提交**

Run: `cd frontend && npx vitest run && npx tsc --noEmit && npx vite build`

```bash
git add frontend/src/admin/SchemaEtlPage.tsx frontend/src/admin/schemaEtlPage.test.tsx
git commit -m "feat(frontend): 表格导入页按映射来源分流首屏，构建器改名

刚走完引导的用户此前会在这一屏看到一个邀请他从头配置映射的向导——而他
刚配过。分流把这份映射从哪来摆在明面上，改它是一个显式动作。

不把映射预填进构建器：那样界面显示的和实际生效的会脱钩，用户改了几笔以为
改的是绑定草稿的那份，其实是本地状态。

配置构建向导改名把这张表映射到已有本体，名字说清前提和方向，与从表格开始
引导建模区分开。"
```

---

## Task 4: 落地路由按本体确认状态分流

**Files:**
- Create: `frontend/src/admin/AdminLanding.tsx`
- Create: `frontend/src/admin/adminLanding.test.tsx`
- Modify: `frontend/src/App.tsx:31`

- [ ] **Step 1: 写失败测试**

```tsx
describe('后台落地路由', () => {
  it('本体没确认时落在本体结构页', async () => {
    // 侧边栏顺序表达的是依赖：ETL 会拒绝未确认本体的租户，文档管线会跳过
    // 图谱抽取。落地在第二阶段等于教用户走一条产品会拒绝的路。
    signIn('admin')
    stubOntologyStatus({ confirmed: false })
    renderAt('/admin')
    expect(await screen.findByText(PAGE_TITLES.ontology)).toBeTruthy()
  })

  it('本体已确认时落在文档上传', async () => {
    // 老租户天天来传文档，不该每次被丢回建模页。
    signIn('admin')
    stubOntologyStatus({ confirmed: true })
    renderAt('/admin')
    expect(await screen.findByText(PAGE_TITLES.documents)).toBeTruthy()
  })

  it('状态未知时不跳转、也不空白', async () => {
    signIn('admin')
    stubOntologyStatusNeverResolves()
    renderAt('/admin')
    expect(await screen.findByTestId('admin-landing-loading')).toBeTruthy()
    expect(screen.queryByText(PAGE_TITLES.documents)).toBeNull()
    expect(screen.queryByText(PAGE_TITLES.ontology)).toBeNull()
  })
})
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd frontend && npx vitest run src/admin/adminLanding.test.tsx`

Expected: FAIL

- [ ] **Step 3: 实现**

新建 `frontend/src/admin/AdminLanding.tsx`：

```tsx
import { useEffect, useState } from 'react'
import { Navigate } from 'react-router-dom'
import { ADMIN_ROUTES } from '../adminRoutes'
import { adminFetch } from './adminApi'
import { useAdminAuth } from './useAdminAuth'
import { useAdminTenant } from './TenantContext'

/**
 * /admin 的落地分流。
 *
 * 侧边栏分组的顺序表达的是依赖（建模 → 接入 → 审核）：ETL 会拒绝未确认
 * 本体的租户（admin_schema_etl_routes.py::_build_sample_files），文档管线
 * 在本体未确认时会跳过图谱抽取（ingestion/pipeline.py）。此前索引路由静态
 * 跳文档上传，跳过了第一阶段——新租户第一屏是一个主能力被禁用的页面，而
 * 修复动作要他自己去另一个分组里找。
 *
 * 静态选任何一个都必然对一半人是错的：落在本体结构对老租户是错的，他们
 * 天天来传文档。这条依赖本来就是状态相关的。
 *
 * 三态：状态未知时既不跳转也不空白——在不知情时跳任何一边，都是对用户
 * 断言一件可能为假的事。
 */
export function AdminLanding() {
  const { sessionToken } = useAdminAuth()
  const { tenantId } = useAdminTenant()
  const [confirmed, setConfirmed] = useState<boolean | null>(null)

  useEffect(() => {
    let cancelled = false
    const load = async () => {
      try {
        const res = await adminFetch(
          `/api/admin/ontology/${encodeURIComponent(tenantId)}/status`,
          sessionToken,
        )
        const body = (await res.json()) as { confirmed: boolean }
        if (!cancelled) setConfirmed(body.confirmed)
      } catch {
        // 读不到状态时落在本体结构页：那一页对两种租户都是完全可用的，而
        // 文档上传页在本体未确认时主能力是禁用的。读失败时选代价小的那边。
        if (!cancelled) setConfirmed(false)
      }
    }
    load()
    return () => {
      cancelled = true
    }
  }, [sessionToken, tenantId])

  if (confirmed === null) {
    return (
      <div data-testid="admin-landing-loading" className="text-sm text-ink-soft">
        正在确认这个租户走到哪一步…
      </div>
    )
  }
  return <Navigate to={confirmed ? ADMIN_ROUTES.documents : ADMIN_ROUTES.ontology} replace />
}
```

`App.tsx:31` 改为：

```tsx
        <Route index element={<AdminLanding />} />
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd frontend && npx vitest run src/admin/adminLanding.test.tsx`

Expected: 3 passed

- [ ] **Step 5: 两次变异验证**

1. 删掉 `confirmed === null` 分支、未知时按 `false` 处理 → 第三条测试应 FAIL
2. 把 `Navigate` 的目标改成恒 `ADMIN_ROUTES.documents` → 第一条测试应 FAIL

- [ ] **Step 6: 全量与提交**

Run: `cd frontend && npx vitest run && npx tsc --noEmit && npx vite build`

```bash
git add frontend/src/admin/AdminLanding.tsx frontend/src/admin/adminLanding.test.tsx frontend/src/App.tsx
git commit -m "fix(frontend): 落地路由按本体确认状态分流

索引路由此前静态跳文档上传，跳过了侧边栏顺序所表达的第一阶段——而
adminRoutes.ts 的注释里正写着把接入排在前面等于教用户走一条产品会拒绝的
路。静态选任何一个都必然对一半人是错的。

状态未知时既不跳转也不空白：在不知情时跳任何一边，都是对用户断言一件
可能为假的事。"
```

---

## Task 5: 三个完成点的前向链接

**Files:**
- Modify: `frontend/src/admin/OntologySchemaPage.tsx`（确认成功处，`showToast('已确认')` 在 `:266`）
- Modify: `frontend/src/admin/SchemaEtlPage.tsx`（运行详情展示区）
- Create: `frontend/src/admin/forwardLinks.test.tsx`

后台已有两条出口——「这里是空的，你该先去哪」（`emptyStateLinks.test.tsx` 的约定）和「有活等着你」（`useNavBadges` + `NavBadge`）——缺第三条：做完一步不告诉你下一步。徽标补不上这个洞：待审关系能计数，「你该去传数据了」不能。

- [ ] **Step 1: 写失败测试**

```tsx
describe('前向出口', () => {
  it('本体确认成功后给出去表格导入的入口', async () => {
    signIn('admin')
    renderAt(ADMIN_ROUTES.ontology)
    await confirmOntology()
    const link = await screen.findByRole('link', { name: '表格导入' })
    expect(link.getAttribute('href')).toBe(ADMIN_ROUTES.etl)
  })

  it('表格导入成功跑完后给出去看结果的入口', async () => {
    signIn('admin')
    stubCompletedRun({ dry_run: false })
    renderAt(ADMIN_ROUTES.etl)
    const link = await screen.findByRole('link', { name: '实体列表' })
    expect(link.getAttribute('href')).toBe(ADMIN_ROUTES.terms)
  })

  it('预演跑完不给"去看结果"——什么都还没写进去', async () => {
    signIn('admin')
    stubCompletedRun({ dry_run: true })
    renderAt(ADMIN_ROUTES.etl)
    await screen.findByText(/这是一次预演/)
    expect(screen.queryByRole('link', { name: '实体列表' })).toBeNull()
  })
})
```

链接文字必须精确用 `NAV_GROUPS` / `NAV_STANDALONE` 里的标签（「表格导入」「实体列表」「待审关系」），`emptyStateLinks.test.tsx` 会检查。

- [ ] **Step 2: 跑测试确认失败**

Run: `cd frontend && npx vitest run src/admin/forwardLinks.test.tsx`

Expected: FAIL

- [ ] **Step 3: 实现**

`OntologySchemaPage`：确认成功后除 toast 外，用一个 `justConfirmed` 状态渲染常驻提示：

```tsx
{justConfirmed && (
  <p className={`${card} text-sm text-ink`}>
    本体已确认。接下来把业务表的数据装进来——去
    <Link to={ADMIN_ROUTES.etl} className={`ml-1 font-bold underline ${focusRing}`}>
      表格导入
    </Link>
    。
  </p>
)}
```

`SchemaEtlPage`：选中的运行 `status === 'completed'` 且 `report?.dry_run !== true` 时，在报告区顶部渲染：

```tsx
<p className="text-sm text-ink">
  数据已写入。去
  <Link to={ADMIN_ROUTES.terms} className={`mx-1 font-bold underline ${focusRing}`}>
    实体列表
  </Link>
  看结果；有待人工确认的关系时去
  <Link to={ADMIN_ROUTES.reviewRelations} className={`mx-1 font-bold underline ${focusRing}`}>
    待审关系
  </Link>
  。
</p>
```

引导流程完成页已有「本体结构」链接，不改。

- [ ] **Step 4: 跑测试确认通过**

Run: `cd frontend && npx vitest run src/admin/forwardLinks.test.tsx`

Expected: 3 passed

- [ ] **Step 5: 三次变异验证**

1. 删掉 `OntologySchemaPage` 里新加的 `<Link>` → 第一条应 FAIL
2. 删掉 `SchemaEtlPage` 里的「实体列表」链接 → 第二条应 FAIL
3. 把 `dry_run !== true` 这个条件去掉（预演也渲染）→ 第三条应 FAIL

- [ ] **Step 6: 全量与提交**

Run: `cd frontend && npx vitest run && npx tsc --noEmit && npx vite build`

`emptyStateLinks.test.tsx` 必须仍然绿——新链接的纯文本要精确匹配侧边栏标签。

```bash
git add frontend/src/admin/OntologySchemaPage.tsx frontend/src/admin/SchemaEtlPage.tsx frontend/src/admin/forwardLinks.test.tsx
git commit -m "feat(frontend): 三个完成点补上前向出口

后台此前只有两条出口：空状态告诉你该先去哪，徽标告诉你有活等着。缺的是
这一步做完了接下来去哪——本体确认成功只弹一个 toast，SchemaEtlPage 里
ADMIN_ROUTES 引用数为零。徽标补不上：待审关系能计数，你该去传数据了不能。

预演跑完不给去看结果的入口：什么都还没写进去。"
```

---

## Task 6: 确认框对破坏性变更附带数据影响

**Files:**
- Modify: `frontend/src/admin/ontologyDiff.ts`（`DiffRow` 在 `:13`、`OntologyDiff` 在 `:21`）
- Modify: `frontend/src/admin/OntologySchemaPage.tsx`（`describeConfirmDiff` 在 `:35`）
- Create: `frontend/src/admin/ontologyDiff.test.ts`

`confirm_ontology` 只动三张本体表，完全不碰 `terms`，确认前也不查已有实体。删掉一个还有实体在用的类型，那些实体原地留在表里但不再属于任何已确认类型，对下游查询不可见。确认框会说「删除实体类型 客户」，不会说「这会让 9335 条已有实体失效」。

数据来源用现成的 `fetchTermsSummary`（`frontend/src/admin/termsApi.ts:45`），**不新增后端接口**。

当前签名是 `buildOntologyDiff(draft, confirmed): OntologyDiff`（`ontologyDiff.ts:102`，两个**位置**参数，不是对象）。本任务给它加第三个位置参数 `termCounts: Record<string, number>`，默认 `{}`——加默认值是为了不动 `OntologySchemaPage` 之外可能存在的调用方。

- [ ] **Step 1: 写失败测试**

```ts
describe('确认差异的数据影响', () => {
  it('删除还有实体在用的类型时说清会失效多少条', () => {
    // "旧的已确认版本会被换掉、无法恢复"这句话用户理解成"本体定义会被换掉"
    // ——没错，但不完整。真正的后果在数据侧，而那一半此前没说。
    const diff = buildOntologyDiff(
      { termTypes: [], relationTypes: [], constraints: [] },
      { termTypes: [{ value: '客户', extra_fields: [] }], relationTypes: [], constraints: [] },
      { 客户: 9335 },
    )
    const removed = diff.termTypes.find((r) => r.kind === 'removed')
    expect(removed?.impact).toMatch(/9335/)
  })

  it('没有实体在用的类型不加噪音', () => {
    const diff = buildOntologyDiff(
      { termTypes: [], relationTypes: [], constraints: [] },
      { termTypes: [{ value: '空类型', extra_fields: [] }], relationTypes: [], constraints: [] },
      {},
    )
    expect(diff.termTypes.find((r) => r.kind === 'removed')?.impact).toBeUndefined()
  })

  it('新增类型不带数据影响——没有东西会失效', () => {
    const diff = buildOntologyDiff(
      { termTypes: [{ value: '新类型', extra_fields: [] }], relationTypes: [], constraints: [] },
      { termTypes: [], relationTypes: [], constraints: [] },
      { 新类型: 5 },
    )
    expect(diff.termTypes.find((r) => r.kind === 'added')?.impact).toBeUndefined()
  })
})
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd frontend && npx vitest run src/admin/ontologyDiff.test.ts`

Expected: FAIL

- [ ] **Step 3: 实现**

`DiffRow` 增加字段：

```ts
export interface DiffRow {
  kind: 'added' | 'removed' | 'changed'
  /** 展示用的标识，比如实体类型名、或 "主语 -关系-> 宾语"。 */
  label: string
  /** changed 时说明改了什么；added/removed 时为空。 */
  detail?: string
  /**
   * 这一条变更会波及多少已有数据。只有被删除的实体类型才有。
   *
   * confirm_ontology 不碰 terms 表：删掉类型不会删实体，实体会原地留在表
   * 里但不再属于任何已确认类型，对下游查询不可见。
   */
  impact?: string
}
```

实体类型的差异计算里，`kind === 'removed'` 且 `termCounts[value] > 0` 时填：

```ts
impact: `当前有 ${count} 条实体，确认后将不再属于任何已确认类型`
```

`OntologySchemaPage` 在打开确认框之前拉一次 `fetchTermsSummary`，把 `TermTypeGroup[]` 转成 `Record<string, number>` 传给差异计算；`describeConfirmDiff` 渲染每行时把 `impact` 接在 `label` 后面。拉取失败时传空对象——少一条提示好过阻断确认。

- [ ] **Step 4: 跑测试确认通过**

Run: `cd frontend && npx vitest run src/admin/ontologyDiff.test.ts`

Expected: 3 passed

- [ ] **Step 5: 两次变异验证**

1. 去掉 `impact` 的填充 → 第一条应 FAIL
2. 把 `impact` 改成无条件填充（不判 `kind === 'removed'`、不判 `count > 0`）→ 第二、三条应 FAIL

- [ ] **Step 6: 全量与提交**

Run: `cd frontend && npx vitest run && npx tsc --noEmit && npx vite build`

```bash
git add frontend/src/admin/ontologyDiff.ts frontend/src/admin/ontologyDiff.test.ts frontend/src/admin/OntologySchemaPage.tsx
git commit -m "feat(frontend): 确认框对破坏性变更说清数据影响

confirm_ontology 只动三张本体表、完全不碰 terms。删掉一个还有实体在用的
类型，那些实体原地留在表里但不再属于任何已确认类型，对下游查询不可见。
此前确认框只说删除实体类型客户，不说这会让 9335 条实体失效。

不加硬闸门：本项目没有批量迁移实体类型的入口，硬闸门等于给用户一扇锁死
的门。这里只让他知道自己在换掉什么。

数据来自现成的 /terms/summary，不新增后端接口。"
```

---

## Task 7: 预演转正式执行

**Files:**
- Modify: `app/api/admin_schema_etl_routes.py`（`start_schema_etl_run` 在 `:194`）
- Modify: `frontend/src/admin/SchemaEtlPage.tsx`（预演提示在 `:545` 一带）
- Test: `tests/api/test_admin_schema_etl_routes.py`、`frontend/src/admin/schemaEtlPage.test.tsx`

`POST /runs` 是唯一的写入入口，每次都要 `config` + `data_files` 全新上传。而空状态自己写着「首次运行建议勾选『预演』」——照做的人多传一遍文件，不照做的人一步到位，**推荐的路比不推荐的路更贵**，结果是没人预演。

关键事实（已实测）：运行输入已经永久保留在 `upload_dir / "schema-etl" / {tenant_id} / {run_id}`，四处 `shutil.rmtree` 全在早期错误路径上，成功跑完之后没有任何清理。本任务**不新增存储**，只复用既有目录。

**Interfaces:**
- Produces: `POST /api/admin/{tenant_id}/schema-etl/runs/{run_id}/promote` → `StartRunResponse`（`{"run_id": <新的 run_id>}`）

`EtlRunNotFoundError` / `EtlRunAlreadyRunningError` / `create_etl_run` / `get_etl_run` 四个符号该文件已经导入（`:23-26`），不用新增 import。

读取单次运行用 `get_etl_run(conn, *, tenant_id: str, run_id: str) -> EtlRunDetail`（`app/graphrag/etl_runs_store.py:108`）。**注意它在找不到时抛 `EtlRunNotFoundError`，不是返回 `None`**——下面的实现必须用 `try/except` 而不是判空。`EtlRunDetail.report` 是 `dict | None`。

- [ ] **Step 1: 写失败的后端测试**

追加到 `tests/api/test_admin_schema_etl_routes.py`（`start_run` / `upload_dir` 按该文件既有用例的写法组织）：

```python
async def test_promote_dry_run_reuses_stored_inputs(client, tenant, upload_dir):
    """预演转正式不要求重新上传。

    输入已经在磁盘上（start_schema_etl_run 写进 run_dir，成功路径不清理）。
    让用户再传一遍，等于产品推荐的路比它不推荐的路更贵。
    """
    dry = await start_run(client, tenant, dry_run=True)
    response = await client.post(
        f"/api/admin/{tenant}/schema-etl/runs/{dry['run_id']}/promote"
    )
    assert response.status_code == 200
    new_run_id = response.json()["run_id"]
    assert new_run_id != dry["run_id"]
    # 新运行有自己的输入副本。
    assert (upload_dir / "schema-etl" / tenant / new_run_id / "config.yaml").exists()


async def test_promote_rejects_a_real_run(client, tenant):
    """只有预演能被转正。

    对一次已经真正写入过的运行再点一次是重复执行，那要走正常的新建运行
    路径，让用户显式确认。
    """
    real = await start_run(client, tenant, dry_run=False)
    response = await client.post(
        f"/api/admin/{tenant}/schema-etl/runs/{real['run_id']}/promote"
    )
    assert response.status_code == 400
    assert "预演" in response.json()["detail"]
```

- [ ] **Step 2: 跑测试确认失败**

Run: `PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest tests/api/test_admin_schema_etl_routes.py -q -k promote`

Expected: FAIL，404

- [ ] **Step 3: 后端实现**

在 `admin_schema_etl_routes.py` 增加：

```python
@router.post("/runs/{run_id}/promote", response_model=StartRunResponse)
async def promote_dry_run(
    tenant_id: str,
    run_id: str,
    background_tasks: BackgroundTasks,
    upload_dir: Path = Depends(deps.get_upload_dir),
    review_conn: aiosqlite.Connection = Depends(deps.get_review_conn),
    graph_client: SchemaEtlGraphProtocol = Depends(deps.get_graph_client),
) -> StartRunResponse:
    """把一次预演按原样正式执行一遍，复用它已经保存在磁盘上的输入。

    为什么不是"重跑同一个 run_id"：create_etl_run 用一条部分唯一索引保证同
    租户串行，而历史里也应该同时留下预演和正式两条记录——预演报告说的是
    "将会发生什么"，正式那条说的是"发生了什么"，合并成一条会丢掉前者。

    为什么只允许预演转正：对一次已经真正写入过的运行再点一次是重复执行，
    那要走正常的新建运行路径，让用户显式确认。
    """
    await require_active_tenant_or_404(review_conn, tenant_id)
    if not await is_ontology_confirmed(review_conn, tenant_id):
        raise HTTPException(
            status_code=400, detail=f"租户 {tenant_id!r} 的本体 schema 还没有确认"
        )

    source_dir = upload_dir / "schema-etl" / tenant_id / run_id
    try:
        detail = await get_etl_run(review_conn, tenant_id=tenant_id, run_id=run_id)
    except EtlRunNotFoundError:
        raise HTTPException(status_code=404, detail="找不到这次运行，无法正式执行")
    if not source_dir.is_dir():
        # 运行记录还在但输入目录没了（人工清理过磁盘）。说清是哪一半没了，
        # 别让用户对着一条看得见的历史记录点一个永远失败的按钮。
        raise HTTPException(status_code=404, detail="这次运行的输入文件已不在磁盘上，无法正式执行")
    if not (detail.report or {}).get("dry_run"):
        raise HTTPException(status_code=400, detail="只有预演可以转成正式执行")

    new_run_id = uuid.uuid4().hex
    new_dir = upload_dir / "schema-etl" / tenant_id / new_run_id
    shutil.copytree(source_dir, new_dir)

    started_at = datetime.now().isoformat()
    try:
        await create_etl_run(
            review_conn, run_id=new_run_id, tenant_id=tenant_id, started_at=started_at
        )
    except EtlRunAlreadyRunningError as exc:
        shutil.rmtree(new_dir, ignore_errors=True)
        raise HTTPException(status_code=409, detail=str(exc))

    background_tasks.add_task(
        _run_schema_etl_job,
        conn=review_conn,
        graph_client=graph_client,
        run_id=new_run_id,
        tenant_id=tenant_id,
        config_path=new_dir / "config.yaml",
        data_dir=new_dir,
        dry_run=False,
        allow_large_sweep=False,
    )
    return StartRunResponse(run_id=new_run_id)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest tests/api/test_admin_schema_etl_routes.py -q -k promote`

Expected: 2 passed

- [ ] **Step 5: 后端两次变异验证**

1. 去掉 `if not (detail.report or {}).get("dry_run")` 那道检查 → `test_promote_rejects_a_real_run` 应 FAIL
2. 删掉 `shutil.copytree` → `test_promote_dry_run_reuses_stored_inputs` 应 FAIL

- [ ] **Step 6: 前端按钮与测试**

追加到 `frontend/src/admin/schemaEtlPage.test.tsx`：

```tsx
it('预演报告页能直接正式执行，不用重传文件', async () => {
  signIn('admin')
  stubSelectedRun({ dry_run: true, status: 'completed' })
  renderAt(ADMIN_ROUTES.etl)
  const user = userEvent.setup()
  await user.click(await screen.findByRole('button', { name: '按这次预演正式执行' }))
  expect(lastRequest().url).toMatch(/\/promote$/)
})

it('正式运行的报告页没有这个按钮', async () => {
  signIn('admin')
  stubSelectedRun({ dry_run: false, status: 'completed' })
  renderAt(ADMIN_ROUTES.etl)
  await screen.findByText(/已完成/)
  expect(screen.queryByRole('button', { name: '按这次预演正式执行' })).toBeNull()
})
```

在 `SchemaEtlPage.tsx` 那段「这是一次预演」的提示里加按钮，POST 到 `/api/admin/{tenantId}/schema-etl/runs/{run_id}/promote`，成功后踢一次轮询（复用既有的 `pollNowRef`）。

- [ ] **Step 7: 前端变异验证**

把按钮的渲染条件改成恒 true → 第二条测试应 FAIL。

- [ ] **Step 8: 全量与提交**

Run: `PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest -q`

Run: `cd frontend && npx vitest run && npx tsc --noEmit && npx vite build`

```bash
git add app/api/admin_schema_etl_routes.py tests/api/test_admin_schema_etl_routes.py frontend/src/admin/SchemaEtlPage.tsx frontend/src/admin/schemaEtlPage.test.tsx
git commit -m "feat: 预演可以直接转成正式执行

此前预演之后要正式跑，得把两个文件再传一遍。而空状态自己写着首次运行建议
勾选预演——照做的人多传一遍，不照做的人一步到位，推荐的路比不推荐的路更
贵，结果是没人预演。

不新增存储：运行输入本来就永久保留在 upload_dir/schema-etl 下，四处 rmtree
全在早期错误路径上。这里只是复用。

新建 run_id 而不是重跑原来那条：预演报告说的是将会发生什么，正式那条说的是
发生了什么，合并成一条会丢掉前者。"
```

---

## 阶段验收

```bash
PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest -q
cd frontend && npx vitest run && npx tsc --noEmit && npx vite build
```

**手工验证**（自动化测不到真实浏览器行为与跨页流程）：

1. 新建一个租户，登录后确认落在「本体结构」而不是「文档上传」
2. 走完引导，**不下载任何文件**，直接去本体结构页确认
3. 去表格导入页，确认第一屏说的是「引导流程已为这个本体配好映射（来自 xxx.csv）」，且构建器是折叠的
4. 只传数据文件、勾预演，跑一次
5. 在预演报告页点「按这次预演正式执行」，确认不要求重新选文件，且历史里出现两条运行记录
6. 回本体结构页，把一个有实体在用的类型删掉，点确认，确认弹框里说清了会失效多少条实体（**此步只看文案，看完取消，不要真的确认**）
7. 确认成功的提示里能点进「表格导入」；ETL 跑完的报告区能点进「实体列表」
8. 用一个本体已确认的老租户登录，确认落在「文档上传」

**若第 3 步看到的是「从头配置」的界面**：查 `checkout_draft` 有没有把映射复制进草稿——本体结构页那三个 tab 每次加载都会触发一次 checkout，没复制的话映射会在确认时消失。
