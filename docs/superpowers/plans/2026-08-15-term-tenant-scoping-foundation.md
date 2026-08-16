# Term 核心模型多租户基础设施 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 `Term`/`terms` 表/Neo4j `:Term` 节点从"全局共享、不分租户"改造为"按租户隔离"，同时把身份键从展示名（`standard_name`）拆分成独立的稳定身份键（`node_key`），并把 `term_type` 分类枚举从全局改为按租户隔离，为后续的结构化 ETL 接入（MUJI 等租户）打好数据模型地基。

**Architecture:** 沿用现有 aiosqlite + Neo4j 双存储架构，不引入新组件。对已上线表做一次性、幂等的原地迁移（SQLite 表重建、Neo4j 属性回填），迁移目标统一落到 `tenant_id='default'`，保证现有单租户部署行为不变。所有 CRUD 函数、Neo4j Cypher、REST 路由按租户参数化，`product_line` 分类保持全局不变。

**Tech Stack:** Python 3.12、aiosqlite、Neo4j（`neo4j` 异步驱动）、FastAPI、pytest + pytest-asyncio（`anyio` 标记）。

**Spec:** `docs/superpowers/specs/2026-08-15-etl-driven-schema-construction-design.md`（第 2-5 节），关联 ADR：`docs/adr/0001-term-type-tenant-scoped-for-muji.md`、`docs/adr/0003-term-gets-stable-identity-key-separate-from-display-name.md`。

## Global Constraints

- 迁移目标租户统一是字符串字面量 `"default"`——所有一次性迁移（`terms` 表、`ontology_term_types` 表、Neo4j `:Term` 节点回填）都把存量无租户数据归到这个租户，不引入配置项。
- `node_key` 在本计划范围内（`extraction` 数据接入模式）创建时直接取当时的 `standard_name` 值，此后永不改变，即使该术语后来被改名（`standard_name` 变了，`node_key` 不变）。
- `node_key` 只需要在**同一租户内**唯一，不要求跨租户全局唯一；`standard_name` 同样只需要在同一租户内唯一（不再是全局唯一约束）。
- `product_line` 分类枚举（`ontology_product_lines` 表）**不受本计划影响**，继续保持全局、不分租户。
- `tenant_id` 参数一律用关键字参数传递（`tenant_id: str`），不允许默认值——调用方必须显式传入，防止漏传导致悄悄落到某个租户。
- Neo4j 关系类型白名单（`_ALLOWED_RELATION_TYPES`）、`merge_relation`/`normalize_and_write_relations`/`review_queue.py` 的抽取管道内部逻辑**不在本计划范围内**，本计划只改这些函数为了保持租户隔离所必须触碰的 Cypher 匹配条件，不改它们的业务逻辑、不改它们对外的函数签名。
- 每个 SQLite 表结构迁移都必须是幂等的（可在已迁移的库上重复调用 `ensure_*_schema` 而不报错、不重复迁移）——所有生产环境的 `ensure_*_schema` 函数都在每次进程启动、每次建立新连接时被调用。
- **任务间的临时状态**：Task 1 完成后，`terms_store.py::_validate_categories` 对 `term_type` 的校验暂时仍是全局的（因为 `ontology_categories.py` 还没改造），这是一个刻意的、有文档说明的中间状态，不是遗漏——Task 2 会补上收尾的一行改动，把校验收紧成按租户。任何审查者看到 Task 1 的 diff 里这个"暂不生效"的说明，不应视为缺陷。

---

### Task 1: Term 数据模型 + terms 表迁移 + terms_store.py 全部函数改造

**Files:**
- Modify: `app/graphrag/ontology.py`
- Modify: `app/graphrag/terms_store.py`（全文件：`_SCHEMA_SQL`、迁移函数、`ensure_terms_schema`、`_row_to_term`、`list_terms`、`get_term`、`_check_name_conflict`、`_validate_categories`、`create_term`、`update_term`、`delete_term`）
- Test: `tests/graphrag/test_terms_store.py`

**Interfaces:**
- Produces：
  - `Term(tenant_id: str, node_key: str, standard_name: str, aliases: list[str], term_type: str, product_line: str, extra_properties: dict[str, str] = {})` —— dataclass，供 Task 2-6 的所有调用方使用。
  - `async def list_terms(conn, tenant_id: str) -> list[Term]`
  - `async def get_term(conn, tenant_id: str, standard_name: str) -> Term`（找不到抛 `TermNotFoundError`）
  - `async def create_term(conn, *, tenant_id: str, standard_name: str, aliases: list[str], term_type: str, product_line: str, extra_properties: dict[str, str] | None = None) -> None`
  - `async def update_term(conn, *, tenant_id: str, standard_name: str, new_standard_name: str, aliases: list[str], term_type: str, product_line: str, extra_properties: dict[str, str] | None = None) -> None`
  - `async def delete_term(conn, tenant_id: str, standard_name: str) -> None`
- Consumes（Task 2 完成前只能部分工作，见 Global Constraints 的"任务间临时状态"）：`ontology_categories.py::list_term_types(conn)`（本任务开始时的旧签名，不带 `tenant_id`）、`list_product_lines(conn)`（不变）

- [ ] **Step 1: 改 Term dataclass，写失败的测试**

`app/graphrag/ontology.py` 现状（`Term` 缺 `tenant_id`/`node_key`）：

```python
@dataclass(frozen=True)
class Term:
    standard_name: str
    aliases: list[str]
    term_type: str
    product_line: str
    extra_properties: dict[str, str] = field(default_factory=dict)
```

在 `tests/graphrag/test_terms_store.py` 顶部新增：

```python
def test_term_dataclass_has_tenant_id_and_node_key():
    from app.graphrag.ontology import Term

    term = Term(
        tenant_id="t1", node_key="k1", standard_name="错误码E502",
        aliases=[], term_type="error_code", product_line="核心平台",
    )
    assert term.tenant_id == "t1"
    assert term.node_key == "k1"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/graphrag/test_terms_store.py::test_term_dataclass_has_tenant_id_and_node_key -v`
Expected: FAIL，`TypeError: Term.__init__() got an unexpected keyword argument 'tenant_id'`

- [ ] **Step 3: 改 Term dataclass 和 load_terminology**

```python
@dataclass(frozen=True)
class Term:
    tenant_id: str
    node_key: str
    standard_name: str
    aliases: list[str]
    term_type: str
    product_line: str
    extra_properties: dict[str, str] = field(default_factory=dict)


def load_terminology(path: Path) -> list[Term]:
    """加载人工维护的术语表（标准名称+别名+类型+产品线）。

    这是第4节设计的"基准真相"：LLM 抽取的实体必须向这份表对齐，
    而不是反过来。真实内容需由业务/技术支持团队协作产出，本函数
    只负责解析格式，不提供任何示例数据本身。

    这份 YAML 只在术语表首次建表时一次性导入（见 terms_store.py::
    ensure_terms_schema），导入目标固定是 tenant_id="default"——YAML
    种子文件本身不区分租户，是单租户部署时代遗留的初始化路径。
    node_key 在导入时直接取 standard_name 的值（Global Constraints 的
    node_key 生成规则）。
    """
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    terms = data.get("terms", []) if isinstance(data, dict) else []
    return [
        Term(
            tenant_id="default",
            node_key=str(item["standard_name"]),
            standard_name=str(item["standard_name"]),
            aliases=[str(a) for a in item.get("aliases", [])],
            term_type=str(item.get("term_type", "")),
            product_line=str(item.get("product_line", "")),
        )
        for item in terms
    ]
```

- [ ] **Step 4: 运行测试确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/graphrag/test_terms_store.py::test_term_dataclass_has_tenant_id_and_node_key -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add app/graphrag/ontology.py tests/graphrag/test_terms_store.py
git commit -m "feat(graphrag): add tenant_id/node_key fields to Term dataclass"
```

- [ ] **Step 6: 写 terms 表迁移 + 全部 CRUD 的失败测试**

在 `tests/graphrag/test_terms_store.py` 新增：

```python
async def test_ensure_terms_schema_migrates_legacy_table_to_tenant_scoped():
    """模拟一个 2026-08-15 之前建的 terms 表（老结构：standard_name 主键，
    没有 tenant_id/node_key 列），验证 ensure_terms_schema 能把它原地迁移
    成新结构，且存量数据全部归到 tenant_id='default'，node_key 回填成
    当时的 standard_name 值，不丢数据。"""
    conn = await aiosqlite.connect(":memory:")
    await conn.executescript(
        """
        CREATE TABLE terms (
            standard_name TEXT PRIMARY KEY,
            aliases TEXT NOT NULL,
            term_type TEXT NOT NULL,
            product_line TEXT NOT NULL,
            extra_properties TEXT NOT NULL DEFAULT '{}'
        );
        """
    )
    await conn.execute(
        "INSERT INTO terms (standard_name, aliases, term_type, product_line, extra_properties) "
        "VALUES ('错误码E502', '[\"网关超时\"]', 'error_code', '核心平台', '{}')"
    )
    await conn.commit()

    await ensure_terms_schema(conn)

    terms = await list_terms(conn, tenant_id="default")
    assert len(terms) == 1
    assert terms[0].tenant_id == "default"
    assert terms[0].node_key == "错误码E502"
    assert terms[0].standard_name == "错误码E502"
    assert terms[0].aliases == ["网关超时"]


async def test_ensure_terms_schema_migration_is_idempotent():
    """重复调用 ensure_terms_schema 不应该报错、不应该重复迁移导致数据翻倍。"""
    conn = await aiosqlite.connect(":memory:")
    await ensure_terms_schema(conn)
    await create_term(
        conn, tenant_id="default", standard_name="A", aliases=[],
        term_type="t", product_line="p",
    )
    await ensure_terms_schema(conn)
    await ensure_terms_schema(conn)

    terms = await list_terms(conn, tenant_id="default")
    assert len(terms) == 1


async def test_create_term_is_isolated_per_tenant():
    """两个不同租户可以各自创建 standard_name 相同的术语，互不冲突——
    这是本次改造前不可能做到的（standard_name 曾经是全局主键）。"""
    conn = await aiosqlite.connect(":memory:")
    await ensure_terms_schema(conn)
    await create_term(
        conn, tenant_id="tenant_a", standard_name="错误码E502", aliases=[],
        term_type="t", product_line="p",
    )
    await create_term(
        conn, tenant_id="tenant_b", standard_name="错误码E502", aliases=[],
        term_type="t", product_line="p",
    )

    terms_a = await list_terms(conn, tenant_id="tenant_a")
    terms_b = await list_terms(conn, tenant_id="tenant_b")
    assert len(terms_a) == 1
    assert len(terms_b) == 1
    assert terms_a[0].tenant_id == "tenant_a"
    assert terms_b[0].tenant_id == "tenant_b"


async def test_update_term_rename_keeps_node_key_stable():
    """改名（standard_name 变化）不应该改变 node_key——这是 ADR-0003
    的核心断言：node_key 创建后永不变，即使术语被改名。"""
    conn = await aiosqlite.connect(":memory:")
    await ensure_terms_schema(conn)
    await create_term(
        conn, tenant_id="t1", standard_name="错误码E502", aliases=[],
        term_type="t", product_line="p",
    )
    original = await get_term(conn, tenant_id="t1", standard_name="错误码E502")

    await update_term(
        conn, tenant_id="t1", standard_name="错误码E502",
        new_standard_name="错误码E502v2", aliases=[], term_type="t", product_line="p",
    )

    renamed = await get_term(conn, tenant_id="t1", standard_name="错误码E502v2")
    assert renamed.node_key == original.node_key
    assert renamed.standard_name == "错误码E502v2"


async def test_check_name_conflict_does_not_cross_tenant_boundary():
    """租户 A 已经占用的 standard_name/alias，租户 B 应该可以自由使用——
    冲突检测必须按租户隔离，不能全局扫描。"""
    conn = await aiosqlite.connect(":memory:")
    await ensure_terms_schema(conn)
    await create_term(
        conn, tenant_id="tenant_a", standard_name="登录模块", aliases=["认证模块"],
        term_type="t", product_line="p",
    )

    # 不应该抛 TermNameConflictError
    await create_term(
        conn, tenant_id="tenant_b", standard_name="登录模块", aliases=["认证模块"],
        term_type="t", product_line="p",
    )


async def test_delete_term_scoped_to_tenant():
    conn = await aiosqlite.connect(":memory:")
    await ensure_terms_schema(conn)
    await create_term(
        conn, tenant_id="t1", standard_name="待删除", aliases=[], term_type="t", product_line="p",
    )

    await delete_term(conn, "t1", "待删除")

    with pytest.raises(TermNotFoundError):
        await get_term(conn, tenant_id="t1", standard_name="待删除")
```

- [ ] **Step 7: 运行测试确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/graphrag/test_terms_store.py -v`
Expected: 全部新测试 FAIL——`list_terms`/`get_term`/`create_term`/`update_term`/`delete_term` 都还不接受 `tenant_id`

- [ ] **Step 8: 实现迁移函数与全部 CRUD**

`app/graphrag/terms_store.py` 完整替换：

```python
_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS terms (
    tenant_id         TEXT NOT NULL,
    node_key          TEXT NOT NULL,
    standard_name     TEXT NOT NULL,
    aliases           TEXT NOT NULL,
    term_type         TEXT NOT NULL,
    product_line      TEXT NOT NULL,
    extra_properties  TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (tenant_id, node_key)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_terms_tenant_standard_name
    ON terms(tenant_id, standard_name);
"""


async def _migrate_terms_table_to_tenant_scoped_if_needed(
    conn: aiosqlite.Connection,
) -> None:
    """把 2026-08-15 之前的 terms 表（standard_name 主键，无 tenant_id/
    node_key）原地迁移成按租户隔离的新结构。只在表已存在且还是老结构时
    执行，幂等——已经是新结构（有 tenant_id 列）直接跳过。存量数据统一
    归到 tenant_id='default'，node_key 回填成当时的 standard_name 值
    （Global Constraints 的 node_key 生成规则）。SQLite 不支持 ALTER
    TABLE 改主键，只能建新表 + 搬数据 + 删旧表 + 改名。
    """
    cursor = await conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='terms'"
    )
    if await cursor.fetchone() is None:
        return
    cursor = await conn.execute("PRAGMA table_info(terms)")
    existing_columns = {row[1] for row in await cursor.fetchall()}
    if "tenant_id" in existing_columns:
        return
    await conn.executescript(
        """
        CREATE TABLE terms_new (
            tenant_id         TEXT NOT NULL,
            node_key          TEXT NOT NULL,
            standard_name     TEXT NOT NULL,
            aliases           TEXT NOT NULL,
            term_type         TEXT NOT NULL,
            product_line      TEXT NOT NULL,
            extra_properties  TEXT NOT NULL DEFAULT '{}',
            PRIMARY KEY (tenant_id, node_key)
        );
        """
    )
    await conn.execute(
        "INSERT INTO terms_new "
        "(tenant_id, node_key, standard_name, aliases, term_type, product_line, extra_properties) "
        "SELECT 'default', standard_name, standard_name, aliases, term_type, product_line, "
        "extra_properties FROM terms"
    )
    await conn.executescript(
        "DROP TABLE terms; ALTER TABLE terms_new RENAME TO terms; "
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_terms_tenant_standard_name "
        "ON terms(tenant_id, standard_name);"
    )
    await conn.commit()


async def ensure_terms_schema(
    conn: aiosqlite.Connection, *, seed_yaml_path: Path | None = None
) -> None:
    """幂等建表/迁移。

    seed_yaml_path 只在传入且指向一个存在的文件、同时这张表是刚刚第一次
    被创建（不是已经存在）时才生效：从这个 YAML 文件里一次性导入内容，
    此后这份 YAML 不再被任何代码路径读取。导入的每条术语 tenant_id 固定
    是 "default"（见 ontology.py::load_terminology 的说明）。

    向后兼容桥接：分类枚举表为空、但 terms 表已经有历史数据（老版本
    上线时term_type/product_line 还是自由文本，没有枚举表），自动把
    历史数据里出现过的去重值导入枚举表——_bridge_seed_categories_from_
    existing_terms 本任务不改动（它查询/写入的 ontology_term_types 表
    要到下一个任务才会按租户隔离，本任务改完之后它依然按老的全局形态
    工作，行为与本任务改造前完全一致）。
    """
    await ensure_categories_schema(conn)
    cursor = await conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='terms'"
    )
    table_already_existed = await cursor.fetchone() is not None
    if table_already_existed:
        await add_column_if_missing(
            conn, table="terms", column="extra_properties", ddl="TEXT NOT NULL DEFAULT '{}'"
        )
        await _migrate_terms_table_to_tenant_scoped_if_needed(conn)
    await conn.executescript(_SCHEMA_SQL)
    await conn.commit()
    if not table_already_existed and seed_yaml_path is not None and seed_yaml_path.exists():
        try:
            for term in load_terminology(seed_yaml_path):
                await conn.execute(
                    "INSERT OR IGNORE INTO terms "
                    "(tenant_id, node_key, standard_name, aliases, term_type, product_line, "
                    "extra_properties) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        term.tenant_id,
                        term.node_key,
                        term.standard_name,
                        json.dumps(term.aliases, ensure_ascii=False),
                        term.term_type,
                        term.product_line,
                        json.dumps(term.extra_properties, ensure_ascii=False),
                    ),
                )
            await conn.commit()
            cursor = await conn.execute("SELECT COUNT(*) FROM terms")
            row = await cursor.fetchone()
            logger.info("术语表首次建表：从 %s 导入了 %d 条术语", seed_yaml_path, row[0])
        except Exception:
            logger.warning(
                "术语表首次建表，种子文件 %s 解析/导入失败，术语表保持为空",
                seed_yaml_path, exc_info=True,
            )
    elif not table_already_existed:
        logger.warning(
            "术语表首次建表，但未找到种子文件%s——术语表当前为空，"
            "需要通过管理后台手动添加术语，否则知识图谱抽取的术语归一化"
            "将始终落到人工审核队列",
            f"（{seed_yaml_path}）" if seed_yaml_path is not None else "",
        )
    await _bridge_seed_categories_from_existing_terms(conn)


async def _bridge_seed_categories_from_existing_terms(conn: aiosqlite.Connection) -> None:
    """本任务不改动这个函数的行为——它操作的 ontology_term_types 表要到
    下一个任务才会按租户隔离，此时依然是老的全局形态，函数保持原样
    不受影响（terms 表新增的 tenant_id/node_key 列不影响这里用到的
    SELECT DISTINCT term_type/product_line 查询）。下一个任务会在
    ontology_term_types 迁移完成后回来给这个函数补上 tenant_id 参数，
    见该任务的收尾步骤。
    """
    known_types = await list_term_types(conn)
    known_lines = await list_product_lines(conn)
    if known_types or known_lines:
        return
    cursor = await conn.execute("SELECT DISTINCT term_type FROM terms")
    distinct_types = [row[0] for row in await cursor.fetchall()]
    cursor = await conn.execute("SELECT DISTINCT product_line FROM terms")
    distinct_lines = [row[0] for row in await cursor.fetchall()]
    if not distinct_types and not distinct_lines:
        return
    for value in distinct_types:
        await conn.execute(
            "INSERT OR IGNORE INTO ontology_term_types (value, extra_fields) VALUES (?, '[]')",
            (value,),
        )
    for value in distinct_lines:
        await conn.execute(
            "INSERT OR IGNORE INTO ontology_product_lines (value) VALUES (?)", (value,)
        )
    await conn.commit()


def _row_to_term(row: aiosqlite.Row) -> Term:
    return Term(
        tenant_id=row["tenant_id"],
        node_key=row["node_key"],
        standard_name=row["standard_name"],
        aliases=json.loads(row["aliases"]),
        term_type=row["term_type"],
        product_line=row["product_line"],
        extra_properties=json.loads(row["extra_properties"]),
    )


async def list_terms(conn: aiosqlite.Connection, tenant_id: str) -> list[Term]:
    conn.row_factory = aiosqlite.Row
    cursor = await conn.execute(
        "SELECT tenant_id, node_key, standard_name, aliases, term_type, product_line, "
        "extra_properties FROM terms WHERE tenant_id = ? ORDER BY standard_name",
        (tenant_id,),
    )
    rows = await cursor.fetchall()
    return [_row_to_term(row) for row in rows]


async def get_term(conn: aiosqlite.Connection, tenant_id: str, standard_name: str) -> Term:
    conn.row_factory = aiosqlite.Row
    cursor = await conn.execute(
        "SELECT tenant_id, node_key, standard_name, aliases, term_type, product_line, "
        "extra_properties FROM terms WHERE tenant_id = ? AND standard_name = ?",
        (tenant_id, standard_name),
    )
    row = await cursor.fetchone()
    if row is None:
        raise TermNotFoundError(f"术语不存在: {standard_name}")
    return _row_to_term(row)


async def _check_name_conflict(
    conn: aiosqlite.Connection,
    *,
    tenant_id: str,
    standard_name: str,
    aliases: list[str],
    exclude_standard_name: str | None = None,
) -> None:
    """检查 standard_name 和 aliases 有没有跟同一租户下别的术语（编辑时
    排除自己）的 standard_name/alias 重叠。按租户扫描，不同租户之间允许
    使用相同的名字/别名——见 Global Constraints"node_key/standard_name
    只需租户内唯一"。
    """
    tenant_terms = await list_terms(conn, tenant_id)
    candidate_names = {standard_name, *aliases}
    for term in tenant_terms:
        if term.standard_name == exclude_standard_name:
            continue
        existing_names = {term.standard_name, *term.aliases}
        overlap = candidate_names & existing_names
        if overlap:
            conflicting = next(iter(overlap))
            raise TermNameConflictError(
                f"{conflicting!r} 已经是术语 {term.standard_name!r} 的别名/标准名，不能重复使用"
            )


async def _validate_categories(
    conn: aiosqlite.Connection,
    *,
    tenant_id: str,
    term_type: str,
    product_line: str,
    extra_properties: dict[str, str],
    existing_extra_property_keys: frozenset[str] = frozenset(),
) -> None:
    """product_line 校验保持全局（不受本次改造影响）。term_type 校验
    本任务完成后暂时仍是全局查询（list_term_types(conn) 不带 tenant_id）
    ——ontology_categories.py 要到下一个任务才会把这张表按租户隔离，
    这里先接受 tenant_id 参数保持函数签名的前瞻一致性，但暂不使用它
    过滤，下一个任务会回来把这一行改成 list_term_types(conn, tenant_id)，
    完成收口（Global Constraints 的"任务间的临时状态"）。
    """
    types = await list_term_types(conn)
    types_by_value = {t.value: t for t in types}
    if term_type not in types_by_value:
        raise UnknownCategoryError(f"未知分类: {term_type!r}")
    if product_line not in await list_product_lines(conn):
        raise UnknownCategoryError(f"未知产品线: {product_line!r}")
    declared_fields = set(types_by_value[term_type].extra_fields)
    unknown = set(extra_properties) - declared_fields - existing_extra_property_keys
    if unknown:
        raise UnknownCategoryError(
            f"分类 {term_type!r} 没有声明这些属性字段: {sorted(unknown)}"
        )


async def create_term(
    conn: aiosqlite.Connection,
    *,
    tenant_id: str,
    standard_name: str,
    aliases: list[str],
    term_type: str,
    product_line: str,
    extra_properties: dict[str, str] | None = None,
) -> None:
    """node_key 创建时直接取 standard_name 的值（Global Constraints 的
    node_key 生成规则：extraction 模式下没有外部稳定码来源）。"""
    extra_properties = extra_properties or {}
    await _validate_categories(
        conn, tenant_id=tenant_id, term_type=term_type, product_line=product_line,
        extra_properties=extra_properties,
    )
    await _check_name_conflict(conn, tenant_id=tenant_id, standard_name=standard_name, aliases=aliases)
    try:
        await conn.execute(
            "INSERT INTO terms (tenant_id, node_key, standard_name, aliases, term_type, "
            "product_line, extra_properties) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                tenant_id,
                standard_name,
                standard_name,
                json.dumps(aliases, ensure_ascii=False),
                term_type,
                product_line,
                json.dumps(extra_properties, ensure_ascii=False),
            ),
        )
    except aiosqlite.IntegrityError:
        raise TermNameConflictError(f"{standard_name!r} 已经是已有术语的标准名，不能重复创建")
    await conn.commit()


async def update_term(
    conn: aiosqlite.Connection,
    *,
    tenant_id: str,
    standard_name: str,
    new_standard_name: str,
    aliases: list[str],
    term_type: str,
    product_line: str,
    extra_properties: dict[str, str] | None = None,
) -> None:
    """standard_name 是当前（改名前）的名字，用来定位这条记录；
    new_standard_name 是提交的新名字，允许和 standard_name 相同（即不改名）。
    node_key 不受影响，UPDATE 语句不写这一列——ADR-0003 的核心断言：
    身份键创建后永不改变，即使术语被改名。
    """
    extra_properties = extra_properties or {}
    existing_term = await get_term(conn, tenant_id, standard_name)
    await _validate_categories(
        conn, tenant_id=tenant_id, term_type=term_type, product_line=product_line,
        extra_properties=extra_properties,
        existing_extra_property_keys=frozenset(existing_term.extra_properties),
    )
    await _check_name_conflict(
        conn, tenant_id=tenant_id, standard_name=new_standard_name, aliases=aliases,
        exclude_standard_name=standard_name,
    )
    try:
        await conn.execute(
            "UPDATE terms SET standard_name=?, aliases=?, term_type=?, product_line=?, "
            "extra_properties=? WHERE tenant_id=? AND node_key=?",
            (
                new_standard_name,
                json.dumps(aliases, ensure_ascii=False),
                term_type,
                product_line,
                json.dumps(extra_properties, ensure_ascii=False),
                tenant_id,
                existing_term.node_key,
            ),
        )
    except aiosqlite.IntegrityError:
        raise TermNameConflictError(f"{new_standard_name!r} 已经是已有术语的标准名，不能重复使用")
    await conn.commit()


async def delete_term(conn: aiosqlite.Connection, tenant_id: str, standard_name: str) -> None:
    await get_term(conn, tenant_id, standard_name)
    await conn.execute(
        "DELETE FROM terms WHERE tenant_id=? AND standard_name=?", (tenant_id, standard_name)
    )
    await conn.commit()
```

- [ ] **Step 9: 运行测试确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/graphrag/test_terms_store.py -v`
Expected: 全部 PASS

- [ ] **Step 10: 提交**

```bash
git add app/graphrag/ontology.py app/graphrag/terms_store.py tests/graphrag/test_terms_store.py
git commit -m "feat(graphrag): scope terms table and all CRUD to tenant_id, split node_key from standard_name"
```

---

### Task 2: ontology_term_types 按租户隔离 + node_key_template 字段 + 收尾闭环

**Files:**
- Modify: `app/graphrag/ontology_categories.py`
- Modify: `app/graphrag/terms_store.py`（只改两处：`_validate_categories` 里的一行调用、`_bridge_seed_categories_from_existing_terms`）
- Test: `tests/graphrag/test_ontology_categories.py`
- Test: `tests/graphrag/test_terms_store.py`

**Interfaces:**
- Consumes：Task 1 的 `create_term`/`get_term`（测试用）
- Produces：
  - `TermTypeCategory(value: str, extra_fields: list[str], node_key_template: str)`
  - `async def list_term_types(conn, tenant_id: str) -> list[TermTypeCategory]`
  - `async def create_term_type(conn, tenant_id: str, *, value: str, extra_fields: list[str] | None = None, node_key_template: str = "") -> None`
  - `async def update_term_type(conn, tenant_id: str, *, value: str, new_value: str, extra_fields: list[str], node_key_template: str) -> None`
  - `async def delete_term_type(conn, tenant_id: str, value: str) -> None`
  - `list_product_lines`/`create_product_line`/`update_product_line`/`delete_product_line` **签名不变**（保持全局）

- [ ] **Step 1: 写失败的测试**

在 `tests/graphrag/test_ontology_categories.py` 新增（`_conn()` 辅助函数已经调用 `ensure_ontology_schema`）：

```python
async def test_ensure_categories_schema_migrates_legacy_term_types_table():
    """模拟 2026-08-15 之前的 ontology_term_types 表（value 主键，没有
    tenant_id/node_key_template），验证迁移把存量数据归到 tenant_id='default'。"""
    conn = await aiosqlite.connect(":memory:")
    await conn.executescript(
        "CREATE TABLE ontology_term_types (value TEXT PRIMARY KEY, "
        "extra_fields TEXT NOT NULL DEFAULT '[]');"
    )
    await conn.execute(
        "INSERT INTO ontology_term_types (value, extra_fields) VALUES ('error_code', '[]')"
    )
    await conn.commit()

    await ensure_categories_schema(conn)

    types = await list_term_types(conn, tenant_id="default")
    assert len(types) == 1
    assert types[0].value == "error_code"
    assert types[0].node_key_template == ""


async def test_create_and_list_term_types_isolated_per_tenant():
    conn = await _conn()
    await create_term_type(conn, tenant_id="tenant_a", value="错误码", extra_fields=["严重等级"])
    await create_term_type(conn, tenant_id="tenant_b", value="VariantValue", extra_fields=[])

    types_a = await list_term_types(conn, tenant_id="tenant_a")
    types_b = await list_term_types(conn, tenant_id="tenant_b")
    assert [t.value for t in types_a] == ["错误码"]
    assert [t.value for t in types_b] == ["VariantValue"]


async def test_create_term_type_with_node_key_template():
    conn = await _conn()
    await create_term_type(
        conn, tenant_id="muji", value="VariantValue", extra_fields=["numeric_value"],
        node_key_template="Variant:{dim_code}:{value_code}",
    )

    types = await list_term_types(conn, tenant_id="muji")
    assert types[0].node_key_template == "Variant:{dim_code}:{value_code}"


async def test_update_term_type_rename_cascades_within_same_tenant_only():
    """改名级联到 terms/term_type_relation_allowlist，必须只影响同一
    租户的行——term_type 按租户隔离后，不该波及其它租户。"""
    conn = await _conn()
    await create_term_type(conn, tenant_id="tenant_a", value="客房")
    await create_term_type(conn, tenant_id="tenant_a", value="酒店")
    await create_term_type(conn, tenant_id="tenant_b", value="客房")
    await create_term_type(conn, tenant_id="tenant_b", value="酒店")
    from app.graphrag.terms_store import create_term, ensure_terms_schema, get_term
    await ensure_terms_schema(conn)
    await create_term(
        conn, tenant_id="tenant_a", standard_name="A栋客房", aliases=[],
        term_type="客房", product_line="示例产品线",
    )
    await create_term(
        conn, tenant_id="tenant_b", standard_name="B栋客房", aliases=[],
        term_type="客房", product_line="示例产品线",
    )

    await update_term_type(
        conn, tenant_id="tenant_a", value="客房", new_value="客房间",
        extra_fields=[], node_key_template="",
    )

    term_a = await get_term(conn, tenant_id="tenant_a", standard_name="A栋客房")
    term_b = await get_term(conn, tenant_id="tenant_b", standard_name="B栋客房")
    assert term_a.term_type == "客房间"
    assert term_b.term_type == "客房"  # tenant_b 不受 tenant_a 改名影响


async def test_delete_term_type_in_use_by_constraint_returns_error():
    conn = await _conn()
    await create_term_type(conn, tenant_id="t1", value="客房")
    await create_term_type(conn, tenant_id="t1", value="酒店")
    from app.graphrag.ontology_relations import seed_default_relation_types
    from app.graphrag.ontology_constraints import add_allowed_combination
    await seed_default_relation_types(conn, "t1")
    await add_allowed_combination(
        conn, "t1", subject_term_type="客房", relation_type="PART_OF", object_term_type="酒店",
    )

    with pytest.raises(CategoryInUseError):
        await delete_term_type(conn, tenant_id="t1", value="客房")
```

在 `tests/graphrag/test_terms_store.py` 新增（本任务的闭环收尾验证）：

```python
async def test_validate_categories_rejects_term_type_from_another_tenant():
    """term_type 校验闭环之后必须按租户过滤——tenant_a 注册的分类，
    tenant_b 提交同名 term_type 应该被拒绝（对 tenant_b 而言这是未知分类）。"""
    conn = await aiosqlite.connect(":memory:")
    await ensure_terms_schema(conn)
    from app.graphrag.ontology_categories import create_term_type, create_product_line
    await create_term_type(conn, tenant_id="tenant_a", value="错误码")
    await create_product_line(conn, value="示例产品线")

    with pytest.raises(UnknownCategoryError):
        await create_term(
            conn, tenant_id="tenant_b", standard_name="X", aliases=[],
            term_type="错误码", product_line="示例产品线",
        )
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/graphrag/test_ontology_categories.py tests/graphrag/test_terms_store.py -v -k "term_types or node_key_template or rename_cascades or in_use_by_constraint or rejects_term_type_from_another_tenant"`
Expected: 全部 FAIL

- [ ] **Step 3: 改造 ontology_categories.py**

```python
_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS ontology_term_types (
    tenant_id         TEXT NOT NULL,
    value             TEXT NOT NULL,
    extra_fields      TEXT NOT NULL DEFAULT '[]',
    node_key_template TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (tenant_id, value)
);
CREATE TABLE IF NOT EXISTS ontology_product_lines (
    value TEXT PRIMARY KEY
);
"""


@dataclass(frozen=True)
class TermTypeCategory:
    value: str
    extra_fields: list[str]
    node_key_template: str


async def _migrate_term_types_table_if_needed(conn: aiosqlite.Connection) -> None:
    """把 2026-08-15 之前的 ontology_term_types 表（value 主键，无
    tenant_id/node_key_template）原地迁移成按租户隔离的新结构，存量数据
    统一归到 tenant_id='default'，node_key_template 留空。幂等，逻辑与
    terms_store.py::_migrate_terms_table_to_tenant_scoped_if_needed 同构。
    """
    cursor = await conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='ontology_term_types'"
    )
    if await cursor.fetchone() is None:
        return
    cursor = await conn.execute("PRAGMA table_info(ontology_term_types)")
    existing_columns = {row[1] for row in await cursor.fetchall()}
    if "tenant_id" in existing_columns:
        return
    await conn.executescript(
        """
        CREATE TABLE ontology_term_types_new (
            tenant_id         TEXT NOT NULL,
            value             TEXT NOT NULL,
            extra_fields      TEXT NOT NULL DEFAULT '[]',
            node_key_template TEXT NOT NULL DEFAULT '',
            PRIMARY KEY (tenant_id, value)
        );
        """
    )
    await conn.execute(
        "INSERT INTO ontology_term_types_new (tenant_id, value, extra_fields, node_key_template) "
        "SELECT 'default', value, extra_fields, '' FROM ontology_term_types"
    )
    await conn.executescript(
        "DROP TABLE ontology_term_types; "
        "ALTER TABLE ontology_term_types_new RENAME TO ontology_term_types;"
    )
    await conn.commit()


async def ensure_categories_schema(conn: aiosqlite.Connection) -> None:
    await _migrate_term_types_table_if_needed(conn)
    await conn.executescript(_SCHEMA_SQL)
    await conn.commit()


def _row_to_term_type(row: aiosqlite.Row) -> TermTypeCategory:
    return TermTypeCategory(
        value=row["value"],
        extra_fields=json.loads(row["extra_fields"]),
        node_key_template=row["node_key_template"],
    )


async def list_term_types(conn: aiosqlite.Connection, tenant_id: str) -> list[TermTypeCategory]:
    conn.row_factory = aiosqlite.Row
    cursor = await conn.execute(
        "SELECT value, extra_fields, node_key_template FROM ontology_term_types "
        "WHERE tenant_id = ? ORDER BY value",
        (tenant_id,),
    )
    rows = await cursor.fetchall()
    return [_row_to_term_type(row) for row in rows]


async def list_product_lines(conn: aiosqlite.Connection) -> list[str]:
    cursor = await conn.execute("SELECT value FROM ontology_product_lines ORDER BY value")
    rows = await cursor.fetchall()
    return [row[0] for row in rows]


async def create_term_type(
    conn: aiosqlite.Connection,
    tenant_id: str,
    *,
    value: str,
    extra_fields: list[str] | None = None,
    node_key_template: str = "",
) -> None:
    try:
        await conn.execute(
            "INSERT INTO ontology_term_types (tenant_id, value, extra_fields, node_key_template) "
            "VALUES (?, ?, ?, ?)",
            (tenant_id, value, json.dumps(extra_fields or [], ensure_ascii=False), node_key_template),
        )
    except aiosqlite.IntegrityError:
        raise CategoryNameConflictError(f"{value!r} 已经是已有分类，不能重复创建")
    await conn.commit()


async def create_product_line(conn: aiosqlite.Connection, *, value: str) -> None:
    try:
        await conn.execute(
            "INSERT INTO ontology_product_lines (value) VALUES (?)", (value,)
        )
    except aiosqlite.IntegrityError:
        raise CategoryNameConflictError(f"{value!r} 已经是已有产品线，不能重复创建")
    await conn.commit()


async def update_term_type(
    conn: aiosqlite.Connection,
    tenant_id: str,
    *,
    value: str,
    new_value: str,
    extra_fields: list[str],
    node_key_template: str,
) -> None:
    """value 是当前名字，new_value 是提交的新名字，允许相同（即不改名）。
    改名时级联更新该租户下 terms 表和 term_type_relation_allowlist 表里
    所有引用旧名字的行，范围收窄到同一租户——term_type 按租户隔离后，
    跨租户级联会误伤其它租户的同名分类。
    """
    cursor = await conn.execute(
        "SELECT 1 FROM ontology_term_types WHERE tenant_id = ? AND value = ?", (tenant_id, value)
    )
    if await cursor.fetchone() is None:
        raise CategoryNotFoundError(f"分类不存在: {value}")
    try:
        await conn.execute(
            "UPDATE ontology_term_types SET value = ?, extra_fields = ?, node_key_template = ? "
            "WHERE tenant_id = ? AND value = ?",
            (new_value, json.dumps(extra_fields, ensure_ascii=False), node_key_template, tenant_id, value),
        )
    except aiosqlite.IntegrityError:
        raise CategoryNameConflictError(f"{new_value!r} 已经是已有分类，不能重复使用")
    if new_value != value:
        await conn.execute(
            "UPDATE terms SET term_type = ? WHERE tenant_id = ? AND term_type = ?",
            (new_value, tenant_id, value),
        )
        await conn.execute(
            "UPDATE OR IGNORE term_type_relation_allowlist SET subject_term_type = ? "
            "WHERE tenant_id = ? AND subject_term_type = ?",
            (new_value, tenant_id, value),
        )
        await conn.execute(
            "UPDATE OR IGNORE term_type_relation_allowlist SET object_term_type = ? "
            "WHERE tenant_id = ? AND object_term_type = ?",
            (new_value, tenant_id, value),
        )
    await conn.commit()


async def update_product_line(
    conn: aiosqlite.Connection, *, value: str, new_value: str
) -> None:
    cursor = await conn.execute(
        "SELECT 1 FROM ontology_product_lines WHERE value = ?", (value,)
    )
    if await cursor.fetchone() is None:
        raise CategoryNotFoundError(f"产品线不存在: {value}")
    try:
        await conn.execute(
            "UPDATE ontology_product_lines SET value = ? WHERE value = ?", (new_value, value)
        )
    except aiosqlite.IntegrityError:
        raise CategoryNameConflictError(f"{new_value!r} 已经是已有产品线，不能重复使用")
    if new_value != value:
        await conn.execute(
            "UPDATE terms SET product_line = ? WHERE product_line = ?", (new_value, value)
        )
    await conn.commit()


async def delete_term_type(conn: aiosqlite.Connection, tenant_id: str, value: str) -> None:
    """删除保护同样收窄到同一租户范围——见 update_term_type 的说明。"""
    cursor = await conn.execute(
        "SELECT COUNT(*) FROM terms WHERE tenant_id = ? AND term_type = ?", (tenant_id, value)
    )
    terms_count = (await cursor.fetchone())[0]
    cursor = await conn.execute(
        "SELECT COUNT(*) FROM term_type_relation_allowlist "
        "WHERE tenant_id = ? AND (subject_term_type = ? OR object_term_type = ?)",
        (tenant_id, value, value),
    )
    allowlist_count = (await cursor.fetchone())[0]
    if terms_count > 0 or allowlist_count > 0:
        raise CategoryInUseError(
            f"分类 {value!r} 仍被 {terms_count} 条术语、{allowlist_count} 条关系约束引用，无法删除"
        )
    await conn.execute(
        "DELETE FROM ontology_term_types WHERE tenant_id = ? AND value = ?", (tenant_id, value)
    )
    await conn.commit()


async def delete_product_line(conn: aiosqlite.Connection, value: str) -> None:
    cursor = await conn.execute(
        "SELECT COUNT(*) FROM terms WHERE product_line = ?", (value,)
    )
    row = await cursor.fetchone()
    if row[0] > 0:
        raise CategoryInUseError(f"产品线 {value!r} 仍被 {row[0]} 条术语引用，无法删除")
    await conn.execute("DELETE FROM ontology_product_lines WHERE value = ?", (value,))
    await conn.commit()
```

- [ ] **Step 4: 收尾闭环——修正 terms_store.py 里对 list_term_types 的调用**

`app/graphrag/terms_store.py`，两处改动：

1. `_validate_categories` 里：

```python
    types = await list_term_types(conn)
```

改成：

```python
    types = await list_term_types(conn, tenant_id)
```

（去掉 Task 1 里那段"暂不生效"的说明注释，因为现在已经生效了。）

2. `_bridge_seed_categories_from_existing_terms` 整个函数替换成：

```python
async def _bridge_seed_categories_from_existing_terms(
    conn: aiosqlite.Connection, *, tenant_id: str
) -> None:
    known_types = await list_term_types(conn, tenant_id)
    known_lines = await list_product_lines(conn)
    if known_types or known_lines:
        return
    cursor = await conn.execute(
        "SELECT DISTINCT term_type FROM terms WHERE tenant_id = ?", (tenant_id,)
    )
    distinct_types = [row[0] for row in await cursor.fetchall()]
    cursor = await conn.execute(
        "SELECT DISTINCT product_line FROM terms WHERE tenant_id = ?", (tenant_id,)
    )
    distinct_lines = [row[0] for row in await cursor.fetchall()]
    if not distinct_types and not distinct_lines:
        return
    for value in distinct_types:
        await conn.execute(
            "INSERT OR IGNORE INTO ontology_term_types "
            "(tenant_id, value, extra_fields, node_key_template) VALUES (?, ?, '[]', '')",
            (tenant_id, value),
        )
    for value in distinct_lines:
        await conn.execute(
            "INSERT OR IGNORE INTO ontology_product_lines (value) VALUES (?)", (value,)
        )
    await conn.commit()
```

并把 `ensure_terms_schema` 末尾的调用点从：

```python
    await _bridge_seed_categories_from_existing_terms(conn)
```

改成：

```python
    await _bridge_seed_categories_from_existing_terms(conn, tenant_id="default")
```

- [ ] **Step 5: 运行测试确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/graphrag/test_ontology_categories.py tests/graphrag/test_terms_store.py -v`
Expected: 全部 PASS

- [ ] **Step 6: 提交**

```bash
git add app/graphrag/ontology_categories.py app/graphrag/terms_store.py \
  tests/graphrag/test_ontology_categories.py tests/graphrag/test_terms_store.py
git commit -m "feat(graphrag): scope ontology_term_types to tenant, add node_key_template, close validation loop"
```

---

### Task 3: Neo4j :Term 节点按租户隔离 + node_key + 索引

**Files:**
- Modify: `app/graphrag/neo4j_client.py`
- Modify: `app/graphrag/term_guard.py`（`query_subgraph` 调用参数）
- Modify: `app/agent/tools.py`（`query_subgraph` 调用参数）
- Test: `tests/graphrag/test_neo4j_client.py`
- Test: `tests/graphrag/test_term_guard.py`
- Test: `tests/agent/test_tools.py`

**Interfaces:**
- Consumes：Task 1 的 `Term`（含 `tenant_id`/`node_key`）
- Produces：
  - `Neo4jGraphClient.sync_term(term: Term) -> None`（签名不变，内部改用 `term.tenant_id`/`term.node_key`）
  - `Neo4jGraphClient.query_subgraph(node_key: str, *, tenant_id: str) -> list[dict]`（第一个参数语义从"标准名"改成"稳定身份键"，调用方需传 `term.node_key` 而不是 `term.standard_name`）
  - `Neo4jGraphClient.rename_term_node(*, tenant_id: str, node_key: str, new_standard_name: str) -> None`
  - `Neo4jGraphClient.delete_term_node(*, tenant_id: str, node_key: str) -> None`
  - `Neo4jGraphClient.count_relation_edges_for_term(*, tenant_id: str, node_key: str) -> int`
  - `Neo4jGraphClient.ensure_tenant_scoped_schema() -> None`（新增：建索引 + 回填存量节点的 tenant_id/node_key）

- [ ] **Step 1: 写失败的测试**

在 `tests/graphrag/test_neo4j_client.py` 新增（复用文件已有的 `FakeSession`/`FakeDriver`）：

```python
async def test_sync_term_merges_by_tenant_and_node_key():
    session = FakeSession(rows=[])
    client = Neo4jGraphClient(driver=FakeDriver(session))
    term = Term(
        tenant_id="t1", node_key="k1", standard_name="错误码E502",
        aliases=["网关超时"], term_type="error_code", product_line="核心平台",
    )

    await client.sync_term(term)

    assert session.last_parameters["tenant_id"] == "t1"
    assert session.last_parameters["node_key"] == "k1"
    assert session.last_parameters["standard_name"] == "错误码E502"
    assert "MERGE (t:Term {tenant_id: $tenant_id, node_key: $node_key})" in session.last_query
    assert "SET t.standard_name = $standard_name" in session.last_query


async def test_query_subgraph_matches_by_tenant_and_node_key():
    session = FakeSession(rows=[])
    client = Neo4jGraphClient(driver=FakeDriver(session))

    await client.query_subgraph("k1", tenant_id="t1")

    assert "MATCH (t:Term {tenant_id: $tenant_id, node_key: $node_key})" in session.last_query
    assert session.last_parameters["node_key"] == "k1"
    assert session.last_parameters["tenant_id"] == "t1"


async def test_merge_relation_scopes_node_merge_by_tenant():
    """merge_relation 的两端节点 MERGE 现在也要带 tenant_id——不这样做的话
    两个租户各自抽取出同名术语时会共用同一个 Neo4j 节点，是本次改造要
    解决的核心问题（docs/EXECUTION_PLAN.md 第9节列为"尚未做的"欠账）。"""
    session = FakeSession(rows=[])
    client = Neo4jGraphClient(driver=FakeDriver(session))

    await client.merge_relation(
        subject_standard_name="错误码E502", object_standard_name="登录模块",
        relation_type="RELATED_TO", source="a.md", tenant_id="t1",
        provenance="auto_merged", recorded_at=_NOW,
    )

    assert "MERGE (a:Term {tenant_id: $tenant_id, node_key: $subject_name})" in session.last_query
    assert "MERGE (b:Term {tenant_id: $tenant_id, node_key: $object_name})" in session.last_query


async def test_rename_term_node_updates_standard_name_not_node_key():
    session = FakeSession(rows=[])
    client = Neo4jGraphClient(driver=FakeDriver(session))

    await client.rename_term_node(
        tenant_id="t1", node_key="k1", new_standard_name="错误码E502v2"
    )

    assert session.last_parameters == {
        "tenant_id": "t1", "node_key": "k1", "new_standard_name": "错误码E502v2",
    }
    assert "MATCH (t:Term {tenant_id: $tenant_id, node_key: $node_key})" in session.last_query
    assert "SET t.standard_name = $new_standard_name" in session.last_query


async def test_delete_term_node_scopes_by_tenant():
    session = FakeSession(rows=[])
    client = Neo4jGraphClient(driver=FakeDriver(session))

    await client.delete_term_node(tenant_id="t1", node_key="k1")

    assert session.last_parameters == {"tenant_id": "t1", "node_key": "k1"}
    assert "MATCH (t:Term {tenant_id: $tenant_id, node_key: $node_key})" in session.last_query


async def test_count_relation_edges_for_term_scopes_by_tenant():
    session = FakeSession(rows=[{"edge_count": 2}])
    client = Neo4jGraphClient(driver=FakeDriver(session))

    count = await client.count_relation_edges_for_term(tenant_id="t1", node_key="k1")

    assert count == 2
    assert session.last_parameters == {"tenant_id": "t1", "node_key": "k1"}


async def test_ensure_tenant_scoped_schema_creates_indexes_and_backfills_legacy_nodes():
    session = FakeSession(rows=[])
    client = Neo4jGraphClient(driver=FakeDriver(session))

    await client.ensure_tenant_scoped_schema()

    queries = [call[0] for call in session.calls]
    assert any("CREATE INDEX IF NOT EXISTS" in q and "tenant_id" in q and "node_key" in q for q in queries)
    assert any("CREATE INDEX IF NOT EXISTS" in q and "term_type" in q or "t.type" in q for q in queries)
    assert any(
        "WHERE t.tenant_id IS NULL" in q and "SET t.tenant_id = 'default'" in q and "t.node_key = t.standard_name" in q
        for q in queries
    )
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/graphrag/test_neo4j_client.py -v -k "tenant_and_node_key or scopes_node_merge or updates_standard_name or scopes_by_tenant or ensure_tenant_scoped_schema"`
Expected: 全部 FAIL

- [ ] **Step 3: 改造 neo4j_client.py**

```python
_SUBGRAPH_QUERY = """
MATCH (t:Term {tenant_id: $tenant_id, node_key: $node_key})-[r]-(related:Term)
WHERE r.tenant_id = $tenant_id
RETURN related.standard_name AS related_name, type(r) AS relation_type, 1 AS hops

UNION

MATCH (t:Term {tenant_id: $tenant_id, node_key: $node_key})-[r:REQUIRES|PRECEDES|PART_OF*2..2]-(related:Term)
WHERE ALL(rel IN r WHERE rel.tenant_id = $tenant_id) AND related <> t
RETURN related.standard_name AS related_name,
       [rel IN r | type(rel)][-1] AS relation_type,
       2 AS hops
"""

_SYNC_TERM_QUERY = """
MERGE (t:Term {tenant_id: $tenant_id, node_key: $node_key})
SET t.standard_name = $standard_name, t.type = $type, t.product_line = $product_line
SET t += $extra_properties
WITH t
UNWIND $aliases AS alias_name
MERGE (a:Term {alias_name: alias_name})
MERGE (a)-[:ALIAS_OF]->(t)
"""

_COUNT_TERM_RELATION_EDGES_QUERY = """
MATCH (t:Term {tenant_id: $tenant_id, node_key: $node_key})-[r]-()
WHERE type(r) <> 'ALIAS_OF'
RETURN count(r) AS edge_count
"""

_RENAME_TERM_NODE_QUERY = """
MATCH (t:Term {tenant_id: $tenant_id, node_key: $node_key})
SET t.standard_name = $new_standard_name
"""
# node_key 不参与这条语句——ADR-0003 的核心断言：改名只更新展示属性
# standard_name，身份键 node_key 创建后永不改变。

_DELETE_TERM_NODE_QUERY = """
MATCH (t:Term {tenant_id: $tenant_id, node_key: $node_key})
OPTIONAL MATCH (a:Term)-[:ALIAS_OF]->(t)
DETACH DELETE t, a
"""

_ENSURE_INDEXES_QUERIES = [
    "CREATE INDEX IF NOT EXISTS term_tenant_node_key_idx FOR (t:Term) ON (t.tenant_id, t.node_key)",
    "CREATE INDEX IF NOT EXISTS term_tenant_term_type_idx FOR (t:Term) ON (t.tenant_id, t.type)",
]
# 所有节点共享同一个 :Term 标签（"多类型实体"是靠 term_type 取值模拟的，
# 不是原生多标签设计，见 docs/superpowers/specs/2026-08-15-etl-driven-
# schema-construction-design.md §3.4），按 tenant_id/node_key/term_type
# 过滤没有标签可用，量级大的租户（如 MUJI 的 SKU 18万+ 行）没有索引会
# 退化成全表扫描。

_BACKFILL_LEGACY_TERM_NODES_QUERY = """
MATCH (t:Term)
WHERE t.tenant_id IS NULL
SET t.tenant_id = 'default', t.node_key = t.standard_name
"""
# 一次性回填 2026-08-15 之前写入的、没有 tenant_id/node_key 属性的存量
# :Term 节点——WHERE t.tenant_id IS NULL 保证幂等，重复调用只会处理还没
# 打过标记的节点。别名节点（alias_name 属性）不参与这次回填：sync_term
# 的别名节点从来不设置 tenant_id/node_key/standard_name，这次改造不改变
# 别名节点的结构。
```

`Neo4jGraphClient` 类方法：

```python
    async def query_subgraph(
        self, node_key: str, *, tenant_id: str
    ) -> list[dict[str, Any]]:
        async with self._driver.session() as session:
            result = await session.run(
                _SUBGRAPH_QUERY,
                {"node_key": node_key, "tenant_id": tenant_id},
            )
            return await result.data()

    async def merge_relation(
        self,
        *,
        subject_standard_name: str,
        object_standard_name: str,
        relation_type: str,
        source: str,
        tenant_id: str,
        provenance: str,
        recorded_at: datetime,
    ) -> None:
        """幂等写入一条术语间关系（MERGE，不存在则创建，存在则不重复）。

        两端节点的 MERGE 匹配条件现在带 tenant_id——:Term 节点本次改造
        前不分租户、可能被多个租户共用，这是 docs/EXECUTION_PLAN.md 第9节
        列为"尚未做的"多租户隔离项之一，本次一并补齐：不这样做的话两个
        租户各自抽取出同一对术语间的关系时，会共用同一对 Neo4j 节点，
        产生跨租户数据污染。

        subject_standard_name/object_standard_name 在 extraction 数据
        接入模式下本身就是 node_key 的值（见 Global Constraints 的
        node_key 生成规则），因此这里直接把它们当 node_key 用，不改变
        这个函数对外的参数名/调用方传参方式——app/graphrag/
        normalization.py 和 review_queue.py 的现有调用点不用改。
        """
        if relation_type not in _ALLOWED_RELATION_TYPES:
            raise ValueError(
                f"不允许的关系类型: {relation_type!r}，"
                f"仅支持: {sorted(_ALLOWED_RELATION_TYPES)}"
            )
        query = (
            "MERGE (a:Term {tenant_id: $tenant_id, node_key: $subject_name}) "
            "MERGE (b:Term {tenant_id: $tenant_id, node_key: $object_name}) "
            f"MERGE (a)-[r:{relation_type} {{tenant_id: $tenant_id}}]->(b) "
            "SET r.source = $source, r.provenance = $provenance, "
            "r.recorded_at = $recorded_at"
        )
        async with self._driver.session() as session:
            await session.run(
                query,
                {
                    "subject_name": subject_standard_name,
                    "object_name": object_standard_name,
                    "source": source,
                    "tenant_id": tenant_id,
                    "provenance": provenance,
                    "recorded_at": recorded_at.strftime("%Y-%m-%d %H:%M:%S"),
                },
            )

    async def delete_relations_by_source(self, source: str, *, tenant_id: str) -> None:
        async with self._driver.session() as session:
            await session.run(
                _DELETE_RELATIONS_BY_SOURCE_QUERY,
                {"source": source, "tenant_id": tenant_id},
            )

    async def sync_term(self, term: Term) -> None:
        async with self._driver.session() as session:
            await session.run(
                _SYNC_TERM_QUERY,
                {
                    "tenant_id": term.tenant_id,
                    "node_key": term.node_key,
                    "standard_name": term.standard_name,
                    "type": term.term_type,
                    "product_line": term.product_line,
                    "aliases": list(term.aliases),
                    "extra_properties": term.extra_properties,
                },
            )

    async def sync_terms(self, terms: list[Term]) -> None:
        for term in terms:
            await self.sync_term(term)

    async def count_relation_edges_for_term(self, *, tenant_id: str, node_key: str) -> int:
        async with self._driver.session() as session:
            result = await session.run(
                _COUNT_TERM_RELATION_EDGES_QUERY,
                {"tenant_id": tenant_id, "node_key": node_key},
            )
            rows = await result.data()
            return rows[0]["edge_count"] if rows else 0

    async def rename_term_node(
        self, *, tenant_id: str, node_key: str, new_standard_name: str
    ) -> None:
        async with self._driver.session() as session:
            await session.run(
                _RENAME_TERM_NODE_QUERY,
                {"tenant_id": tenant_id, "node_key": node_key, "new_standard_name": new_standard_name},
            )

    async def delete_term_node(self, *, tenant_id: str, node_key: str) -> None:
        async with self._driver.session() as session:
            await session.run(
                _DELETE_TERM_NODE_QUERY, {"tenant_id": tenant_id, "node_key": node_key}
            )

    async def ensure_tenant_scoped_schema(self) -> None:
        """建按租户/节点键、按租户/分类的属性索引，并把存量（本次改造前
        写入、没有 tenant_id/node_key 属性的）:Term 节点回填成
        tenant_id='default'——与 SQLite 侧 terms 表的迁移是同一次改造的
        两半，缺一半就会出现"SQLite 里租户隔离了，Neo4j 里还是老样子"
        的不一致状态。幂等，可在每次进程启动时调用。
        """
        async with self._driver.session() as session:
            for query in _ENSURE_INDEXES_QUERIES:
                await session.run(query)
            await session.run(_BACKFILL_LEGACY_TERM_NODES_QUERY)
```

`migrate_relation_type_edges` 方法（改造前已有，不涉及 `:Term` 节点匹配，只涉及关系边）**不改动**，原样保留在文件里。

- [ ] **Step 4: 改 term_guard.py 和 agent/tools.py 的调用方**

`app/graphrag/term_guard.py` 第 71-75 行，原本：

```python
    async def _query_one(term: Term) -> list[dict[str, Any]]:
        async with query_semaphore:
            return await graph_client.query_subgraph(
                term.standard_name, tenant_id=tenant_id
            )
```

改成：

```python
    async def _query_one(term: Term) -> list[dict[str, Any]]:
        async with query_semaphore:
            return await graph_client.query_subgraph(
                term.node_key, tenant_id=tenant_id
            )
```

`app/agent/tools.py` 第 120-124 行，原本：

```python
    standard_name = resolve_to_standard_name(entity_name, terms)
    if standard_name is None:
        return GraphQueryToolResult(resolved=False, standard_name=None, subgraph=[])

    subgraph = await graph_client.query_subgraph(standard_name, tenant_id=tenant_id)
```

改成：

```python
    standard_name = resolve_to_standard_name(entity_name, terms)
    if standard_name is None:
        return GraphQueryToolResult(resolved=False, standard_name=None, subgraph=[])

    # resolve_to_standard_name 返回的是展示名，查图谱要用稳定身份键——
    # 改名后 standard_name 会变但 node_key 不变（ADR-0003），从已加载的
    # terms 列表里按 standard_name 反查对应的 node_key，不改
    # resolve_to_standard_name 本身（它是抽取管道 normalization.py 也在
    # 用的共享函数，签名不在本计划改动范围内）。
    node_key = next(t.node_key for t in terms if t.standard_name == standard_name)
    subgraph = await graph_client.query_subgraph(node_key, tenant_id=tenant_id)
```

- [ ] **Step 5: 更新受影响的既有测试**

`tests/graphrag/test_neo4j_client.py` 里改造前的测试（`test_query_subgraph_returns_related_terms`、`test_sync_term_writes_standard_node_properties_and_alias_edges`、`test_sync_term_with_no_aliases_sends_empty_alias_list`、`test_sync_term_writes_extra_properties`、`test_sync_terms_syncs_every_term_in_the_list`、`test_rename_term_node_sends_expected_query_and_parameters`、`test_delete_term_node_sends_detach_delete_query`、`test_count_relation_edges_for_term_returns_edge_count` 等）需要同步更新：`Term(...)` 构造调用补上 `tenant_id`/`node_key` 两个必填字段；断言里凡是引用 `standard_name` 作为查询参数键的地方，改成同时校验 `node_key`/`tenant_id`；`rename_term_node`/`delete_term_node`/`count_relation_edges_for_term` 的调用方式从位置参数改成 Step 3 定义的新关键字参数形状。逐条检查文件内每个用到这些 API 的既有测试，按 Step 1 新增测试建立的目标形状对齐，这里不逐条重复列出（都是同一种机械改法：加字段/改参数名）。

`tests/graphrag/test_term_guard.py`、`tests/agent/test_tools.py`（如果这两个文件存在且里面构造了 `Term(...)`）同样按此方式补齐 `tenant_id`/`node_key` 字段。

- [ ] **Step 6: 运行测试确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/graphrag/test_neo4j_client.py tests/graphrag/test_term_guard.py tests/agent/test_tools.py -v`
Expected: 全部 PASS

- [ ] **Step 7: 提交**

```bash
git add app/graphrag/neo4j_client.py app/graphrag/term_guard.py app/agent/tools.py \
  tests/graphrag/test_neo4j_client.py tests/graphrag/test_term_guard.py tests/agent/test_tools.py
git commit -m "feat(graphrag): scope Term neo4j nodes to tenant_id, split node_key from standard_name"
```

---

### Task 4: admin_terms_routes.py + admin_ontology_routes.py 路由层改造

**Files:**
- Modify: `app/api/admin_terms_routes.py`
- Modify: `app/api/admin_ontology_routes.py`（term-types 路由段）
- Test: `tests/api/test_admin_terms_routes.py`
- Test: `tests/api/test_admin_ontology_routes.py`

**Interfaces:**
- Consumes：Task 1 的 `create_term`/`update_term`/`delete_term`/`get_term`/`list_terms`（均需 `tenant_id`）、Task 2 的 `create_term_type`/`update_term_type`/`delete_term_type`/`list_term_types`（均需 `tenant_id`）、Task 3 的 `sync_term`/`rename_term_node`/`delete_term_node`/`count_relation_edges_for_term`
- Produces：路由路径从 `/api/admin/terms` 改成 `/api/admin/{tenant_id}/terms`，与 `admin_ontology_routes.py` 里 relation-types/constraints 已经采用的 `/{tenant_id}/...` 风格对齐；`/api/admin/ontology/term-types` 改成 `/api/admin/ontology/{tenant_id}/term-types`

- [ ] **Step 1: 写失败的测试**

在 `tests/api/test_admin_terms_routes.py` 新增（复用文件已有的 `_settings`/`_authed_headers`/`SpyGraphClient`/`terms_conn` fixture）：

```python
def test_create_term_is_scoped_to_tenant_in_url(terms_conn):
    session_store = AdminSessionStore()
    graph_client = SpyGraphClient()
    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    app.dependency_overrides[deps.get_admin_session_store] = lambda: session_store
    app.dependency_overrides[deps.get_review_conn] = lambda: terms_conn
    app.dependency_overrides[deps.get_graph_client] = lambda: graph_client
    try:
        client = TestClient(app)
        response = client.post(
            "/api/admin/tenant_a/terms",
            json={"standard_name": "新术语", "aliases": [], "term_type": "t", "product_line": "p"},
            headers=_authed_headers(session_store),
        )
        assert response.status_code == 200
        assert response.json()["standard_name"] == "新术语"

        list_resp = client.get("/api/admin/tenant_a/terms", headers=_authed_headers(session_store))
        assert len(list_resp.json()["terms"]) == 1

        other_tenant_resp = client.get("/api/admin/tenant_b/terms", headers=_authed_headers(session_store))
        assert other_tenant_resp.json()["terms"] == []
    finally:
        app.dependency_overrides.clear()
```

（该文件里其它既有测试的 URL/预置数据也需要从"全局无租户"改成带租户段的形状，见 Step 4。）

在 `tests/api/test_admin_ontology_routes.py` 新增：

```python
def test_term_type_routes_are_scoped_to_tenant_in_url(client):
    resp = client.post(
        "/api/admin/ontology/tenant_a/term-types",
        json={"value": "错误码", "extra_fields": [], "node_key_template": ""},
        headers={"Authorization": "Bearer x"},
    )
    assert resp.status_code == 200

    resp = client.get(
        "/api/admin/ontology/tenant_b/term-types", headers={"Authorization": "Bearer x"}
    )
    assert resp.json() == {"term_types": []}
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/api/test_admin_terms_routes.py::test_create_term_is_scoped_to_tenant_in_url tests/api/test_admin_ontology_routes.py::test_term_type_routes_are_scoped_to_tenant_in_url -v`
Expected: FAIL（404，路由还不存在这个路径形状）

- [ ] **Step 3: 改造 admin_terms_routes.py**

```python
router = APIRouter(prefix="/api/admin/{tenant_id}/terms", dependencies=[Depends(deps.require_admin_session)])


def _to_response(term: Term) -> TermResponse:
    return TermResponse(
        standard_name=term.standard_name,
        aliases=term.aliases,
        term_type=term.term_type,
        product_line=term.product_line,
        extra_properties=term.extra_properties,
    )


@router.get("", response_model=TermListResponse)
async def list_all_terms(
    tenant_id: str,
    review_conn: aiosqlite.Connection = Depends(deps.get_review_conn),
) -> TermListResponse:
    terms = await list_terms(review_conn, tenant_id)
    return TermListResponse(terms=[_to_response(term) for term in terms])


@router.post("", response_model=TermResponse)
async def create_new_term(
    tenant_id: str,
    payload: TermWriteRequest,
    review_conn: aiosqlite.Connection = Depends(deps.get_review_conn),
    graph_client: Neo4jGraphClient = Depends(deps.get_graph_client),
) -> TermResponse:
    try:
        await create_term(
            review_conn,
            tenant_id=tenant_id,
            standard_name=payload.standard_name,
            aliases=payload.aliases,
            term_type=payload.term_type,
            product_line=payload.product_line,
            extra_properties=payload.extra_properties,
        )
    except TermNameConflictError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except UnknownCategoryError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    term = Term(
        tenant_id=tenant_id,
        node_key=payload.standard_name,
        standard_name=payload.standard_name,
        aliases=payload.aliases,
        term_type=payload.term_type,
        product_line=payload.product_line,
        extra_properties=payload.extra_properties,
    )
    try:
        await graph_client.sync_term(term)
    except Exception:
        logger.exception(
            "术语 %r（租户 %r）已写入 SQLite 但同步进图谱失败——两侧数据已不一致，需要人工核对",
            term.standard_name, tenant_id,
        )
        raise
    return _to_response(term)


@router.put("/{standard_name}", response_model=TermResponse)
async def update_existing_term(
    tenant_id: str,
    standard_name: str,
    payload: TermWriteRequest,
    review_conn: aiosqlite.Connection = Depends(deps.get_review_conn),
    graph_client: Neo4jGraphClient = Depends(deps.get_graph_client),
) -> TermResponse:
    try:
        existing_before_update = await get_term(review_conn, tenant_id, standard_name)
        await update_term(
            review_conn,
            tenant_id=tenant_id,
            standard_name=standard_name,
            new_standard_name=payload.standard_name,
            aliases=payload.aliases,
            term_type=payload.term_type,
            product_line=payload.product_line,
            extra_properties=payload.extra_properties,
        )
    except TermNotFoundError:
        raise HTTPException(status_code=404, detail="术语不存在")
    except TermNameConflictError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except UnknownCategoryError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    node_key = existing_before_update.node_key
    if payload.standard_name != standard_name:
        try:
            await graph_client.rename_term_node(
                tenant_id=tenant_id, node_key=node_key, new_standard_name=payload.standard_name
            )
        except Exception:
            logger.exception(
                "术语 %r 重命名为 %r（租户 %r）已写入 SQLite 但图谱改名失败——两侧数据已不一致，需要人工核对",
                standard_name, payload.standard_name, tenant_id,
            )
            raise
    term = Term(
        tenant_id=tenant_id,
        node_key=node_key,
        standard_name=payload.standard_name,
        aliases=payload.aliases,
        term_type=payload.term_type,
        product_line=payload.product_line,
        extra_properties=payload.extra_properties,
    )
    try:
        await graph_client.sync_term(term)
    except Exception:
        logger.exception(
            "术语 %r（租户 %r）已写入 SQLite 但同步进图谱失败——两侧数据已不一致，需要人工核对",
            term.standard_name, tenant_id,
        )
        raise
    return _to_response(term)


@router.delete("/{standard_name}")
async def delete_existing_term(
    tenant_id: str,
    standard_name: str,
    review_conn: aiosqlite.Connection = Depends(deps.get_review_conn),
    graph_client: Neo4jGraphClient = Depends(deps.get_graph_client),
) -> dict[str, bool]:
    try:
        term = await get_term(review_conn, tenant_id, standard_name)
    except TermNotFoundError:
        raise HTTPException(status_code=404, detail="术语不存在")
    edge_count = await graph_client.count_relation_edges_for_term(
        tenant_id=tenant_id, node_key=term.node_key
    )
    if edge_count > 0:
        raise HTTPException(status_code=409, detail="该术语已在图谱中使用，无法删除")
    await delete_term(review_conn, tenant_id, standard_name)
    try:
        await graph_client.delete_term_node(tenant_id=tenant_id, node_key=term.node_key)
    except Exception:
        logger.exception(
            "术语 %r（租户 %r）已从 SQLite 删除，但图谱节点删除失败——SQLite 记录已不存在，"
            "图谱节点仍然存在且对管理后台不可见，需要人工核对",
            standard_name, tenant_id,
        )
        raise
    return {"deleted": True}
```

（`TermResponse`/`TermListResponse`/`TermWriteRequest` 三个 Pydantic 模型和它们的 `field_validator` **不改动**，原样保留。）

- [ ] **Step 4: 批量更新既有测试的 URL 和预置数据**

`tests/api/test_admin_terms_routes.py` 里所有 `client.post("/api/admin/terms", ...)`/`client.put("/api/admin/terms/...", ...)`/`client.delete("/api/admin/terms/...", ...)` 调用，统一改成 `/api/admin/{某个测试用租户 ID，如 "t1"}/terms`；对应地，测试里通过 `terms_conn` fixture 预先插入的数据（用 `asyncio.run(create_term(terms_conn, ...))`）也要补上匹配的 `tenant_id="t1"` 关键字参数，保证请求路径里的租户和数据预置时的租户一致。这是纯粹的机械改动（改字符串常量），不在此逐条列出每一处 diff。

- [ ] **Step 5: 改造 admin_ontology_routes.py 的 term-types 路由段**

```python
@router.get("/{tenant_id}/term-types")
async def list_term_type_categories(
    tenant_id: str,
    review_conn: aiosqlite.Connection = Depends(deps.get_review_conn),
) -> dict:
    result = await list_term_types(review_conn, tenant_id)
    return {
        "term_types": [
            {"value": t.value, "extra_fields": t.extra_fields, "node_key_template": t.node_key_template}
            for t in result
        ]
    }


@router.post("/{tenant_id}/term-types")
async def create_term_type_category(
    tenant_id: str,
    payload: TermTypeWriteRequest,
    review_conn: aiosqlite.Connection = Depends(deps.get_review_conn),
) -> dict:
    try:
        await create_term_type(
            review_conn, tenant_id, value=payload.value, extra_fields=payload.extra_fields,
            node_key_template=payload.node_key_template,
        )
    except CategoryNameConflictError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return payload.model_dump()


@router.put("/{tenant_id}/term-types/{value}")
async def update_term_type_category(
    tenant_id: str,
    value: str,
    payload: TermTypeWriteRequest,
    review_conn: aiosqlite.Connection = Depends(deps.get_review_conn),
) -> dict:
    try:
        await update_term_type(
            review_conn, tenant_id, value=value, new_value=payload.value,
            extra_fields=payload.extra_fields, node_key_template=payload.node_key_template,
        )
    except CategoryNotFoundError:
        raise HTTPException(status_code=404, detail="分类不存在")
    except CategoryNameConflictError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return payload.model_dump()


@router.delete("/{tenant_id}/term-types/{value}")
async def delete_term_type_category(
    tenant_id: str, value: str, review_conn: aiosqlite.Connection = Depends(deps.get_review_conn)
) -> dict:
    try:
        await delete_term_type(review_conn, tenant_id, value)
    except CategoryInUseError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return {"deleted": True}
```

`TermTypeWriteRequest` 模型加一个字段（product-lines 相关的路由/模型**不改动**，保持全局路径 `/product-lines`）：

```python
class TermTypeWriteRequest(BaseModel):
    value: str
    extra_fields: list[str] = []
    node_key_template: str = ""
```

- [ ] **Step 6: 批量更新 test_admin_ontology_routes.py 里 term-types 相关测试的 URL**

把该文件里所有 `/api/admin/ontology/term-types...` 的调用改成 `/api/admin/ontology/{某个测试用租户 ID}/term-types...`（`/product-lines`/`/{tenant_id}/relation-types`/`/{tenant_id}/constraints` 等其它路由不受影响，不用改）。

- [ ] **Step 7: 运行测试确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/api/test_admin_terms_routes.py tests/api/test_admin_ontology_routes.py -v`
Expected: 全部 PASS

- [ ] **Step 8: 提交**

```bash
git add app/api/admin_terms_routes.py app/api/admin_ontology_routes.py \
  tests/api/test_admin_terms_routes.py tests/api/test_admin_ontology_routes.py
git commit -m "feat(api): scope term/term-type admin routes under /{tenant_id}/"
```

---

### Task 5: ingestion_mode 租户配置 + checkout_draft 分支

**Files:**
- Create: `app/graphrag/tenant_ingestion_config.py`
- Modify: `app/graphrag/ontology_lifecycle.py`（`ensure_ontology_schema`、`checkout_draft`）
- Test: `tests/graphrag/test_tenant_ingestion_config.py`
- Test: `tests/graphrag/test_ontology_lifecycle.py`

**Interfaces:**
- Produces：
  - `async def ensure_ingestion_config_schema(conn) -> None`
  - `async def get_ingestion_mode(conn, tenant_id: str) -> str`（返回 `"extraction"` 或 `"etl"`，未显式设置过的租户默认 `"extraction"`）
  - `async def set_ingestion_mode(conn, tenant_id: str, mode: str) -> None`（`mode` 不是 `"extraction"`/`"etl"` 之一时抛 `InvalidIngestionModeError`）
- Consumes（由 `ontology_lifecycle.py::checkout_draft` 调用）

- [ ] **Step 1: 写失败的测试**

`tests/graphrag/test_tenant_ingestion_config.py`：

```python
from __future__ import annotations

import aiosqlite
import pytest

from app.graphrag.tenant_ingestion_config import (
    InvalidIngestionModeError,
    ensure_ingestion_config_schema,
    get_ingestion_mode,
    set_ingestion_mode,
)

pytestmark = pytest.mark.anyio


async def _conn() -> aiosqlite.Connection:
    conn = await aiosqlite.connect(":memory:")
    await ensure_ingestion_config_schema(conn)
    return conn


async def test_get_ingestion_mode_defaults_to_extraction():
    conn = await _conn()
    assert await get_ingestion_mode(conn, "unseen_tenant") == "extraction"


async def test_set_and_get_ingestion_mode():
    conn = await _conn()
    await set_ingestion_mode(conn, "muji", "etl")
    assert await get_ingestion_mode(conn, "muji") == "etl"
    # 未设置过的其它租户不受影响，仍是默认值
    assert await get_ingestion_mode(conn, "hotel_tenant") == "extraction"


async def test_set_ingestion_mode_rejects_invalid_value():
    conn = await _conn()
    with pytest.raises(InvalidIngestionModeError):
        await set_ingestion_mode(conn, "muji", "not_a_real_mode")


async def test_set_ingestion_mode_is_idempotent_overwrite():
    conn = await _conn()
    await set_ingestion_mode(conn, "muji", "etl")
    await set_ingestion_mode(conn, "muji", "extraction")
    assert await get_ingestion_mode(conn, "muji") == "extraction"
```

在 `tests/graphrag/test_ontology_lifecycle.py` 新增：

```python
async def test_checkout_draft_skips_default_relation_types_for_etl_tenants():
    conn = await _conn()  # 该文件已有的辅助函数，建整套本体 schema
    from app.graphrag.tenant_ingestion_config import set_ingestion_mode
    await set_ingestion_mode(conn, "muji", "etl")

    await checkout_draft(conn, "muji")

    from app.graphrag.ontology_relations import list_relation_types
    relation_types = await list_relation_types(conn, "muji", status="draft")
    assert relation_types == []


async def test_checkout_draft_still_seeds_defaults_for_extraction_tenants():
    conn = await _conn()

    await checkout_draft(conn, "hotel_tenant")

    from app.graphrag.ontology_relations import list_relation_types
    relation_types = await list_relation_types(conn, "hotel_tenant", status="draft")
    assert len(relation_types) == 10
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/graphrag/test_tenant_ingestion_config.py -v`
Expected: FAIL——`app.graphrag.tenant_ingestion_config` 模块不存在

- [ ] **Step 3: 实现 tenant_ingestion_config.py**

```python
from __future__ import annotations

import aiosqlite

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS tenant_ingestion_config (
    tenant_id      TEXT PRIMARY KEY,
    ingestion_mode TEXT NOT NULL DEFAULT 'extraction'
);
"""

_VALID_MODES = frozenset({"extraction", "etl"})


class InvalidIngestionModeError(Exception):
    """ingestion_mode 只能是 'extraction' 或 'etl' 之一。"""


async def ensure_ingestion_config_schema(conn: aiosqlite.Connection) -> None:
    await conn.executescript(_SCHEMA_SQL)
    await conn.commit()


async def get_ingestion_mode(conn: aiosqlite.Connection, tenant_id: str) -> str:
    """未显式设置过的租户默认 'extraction'——这是现状（本次改造前唯一
    存在的数据接入方式），保证已有租户不需要任何额外配置就维持原行为。
    """
    cursor = await conn.execute(
        "SELECT ingestion_mode FROM tenant_ingestion_config WHERE tenant_id = ?", (tenant_id,)
    )
    row = await cursor.fetchone()
    return row[0] if row is not None else "extraction"


async def set_ingestion_mode(conn: aiosqlite.Connection, tenant_id: str, mode: str) -> None:
    if mode not in _VALID_MODES:
        raise InvalidIngestionModeError(
            f"不支持的接入模式: {mode!r}，仅支持: {sorted(_VALID_MODES)}"
        )
    await conn.execute(
        "INSERT INTO tenant_ingestion_config (tenant_id, ingestion_mode) VALUES (?, ?) "
        "ON CONFLICT(tenant_id) DO UPDATE SET ingestion_mode = excluded.ingestion_mode",
        (tenant_id, mode),
    )
    await conn.commit()
```

- [ ] **Step 4: 运行测试确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/graphrag/test_tenant_ingestion_config.py -v`
Expected: 全部 PASS

- [ ] **Step 5: 提交**

```bash
git add app/graphrag/tenant_ingestion_config.py tests/graphrag/test_tenant_ingestion_config.py
git commit -m "feat(graphrag): add tenant-level ingestion_mode config (extraction vs etl)"
```

- [ ] **Step 6: 改造 ontology_lifecycle.py**

`app/graphrag/ontology_lifecycle.py` 顶部 import 和 `ensure_ontology_schema`：

```python
from app.graphrag.ontology_categories import ensure_categories_schema
from app.graphrag.ontology_constraints import ensure_constraints_schema
from app.graphrag.ontology_relations import ensure_relations_schema, seed_default_relation_types
from app.graphrag.tenant_ingestion_config import ensure_ingestion_config_schema, get_ingestion_mode

_TABLES_WITH_TENANT_LIFECYCLE = ("tenant_relation_types", "term_type_relation_allowlist")


async def ensure_ontology_schema(conn: aiosqlite.Connection) -> None:
    """统一入口：分类（按租户）+ 关系类型/约束（按租户）+ 接入模式配置
    四张表一起建。ensure_ingestion_config_schema 放进来，保证 checkout_draft
    需要读 ingestion_mode 时这张表一定已经存在，不需要调用方自己记得
    额外建表。
    """
    await ensure_categories_schema(conn)
    await ensure_relations_schema(conn)
    await ensure_constraints_schema(conn)
    await ensure_ingestion_config_schema(conn)
```

`checkout_draft`：

```python
async def checkout_draft(conn: aiosqlite.Connection, tenant_id: str) -> None:
    """检出一份可编辑的草稿：如果该租户已经有草稿，什么都不做（幂等，不覆盖正在
    编辑的内容）；如果没有草稿但有已确认版本，从已确认版本复制一份新草稿；如果
    两者都没有（全新租户），关系类型草稿的默认值取决于该租户的接入模式
    （tenant_ingestion_config.ingestion_mode）——extraction 模式播种 10 种
    通用默认关系（面向 LLM 抽取场景设计，见 ontology_relations.py），etl
    模式不播种，从空白草稿开始（这些默认关系对结构化 ETL 租户没有意义，
    见 docs/superpowers/specs/2026-08-15-etl-driven-schema-construction-
    design.md §2）。约束表草稿两种模式都留空（没有分类数据支撑，写不出
    有意义的默认组合）。
    """
    if not await _has_any_row(conn, "tenant_relation_types", tenant_id, "draft"):
        if await _has_any_row(conn, "tenant_relation_types", tenant_id, "confirmed"):
            await conn.execute(
                "INSERT INTO tenant_relation_types "
                "(tenant_id, relation_type, example_phrase, description, allow_chain_query, "
                "source, status) "
                "SELECT tenant_id, relation_type, example_phrase, description, "
                "allow_chain_query, source, 'draft' FROM tenant_relation_types "
                "WHERE tenant_id = ? AND status = 'confirmed'",
                (tenant_id,),
            )
        elif await get_ingestion_mode(conn, tenant_id) == "extraction":
            await seed_default_relation_types(conn, tenant_id)
    if not await _has_any_row(conn, "term_type_relation_allowlist", tenant_id, "draft"):
        if await _has_any_row(conn, "term_type_relation_allowlist", tenant_id, "confirmed"):
            await conn.execute(
                "INSERT INTO term_type_relation_allowlist "
                "(tenant_id, subject_term_type, relation_type, object_term_type, status) "
                "SELECT tenant_id, subject_term_type, relation_type, object_term_type, 'draft' "
                "FROM term_type_relation_allowlist WHERE tenant_id = ? AND status = 'confirmed'",
                (tenant_id,),
            )
    await conn.commit()
```

（`confirm_ontology`/`is_ontology_confirmed`/`_has_any_row` **不改动**，原样保留在文件里。）

- [ ] **Step 7: 运行测试确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/graphrag/test_ontology_lifecycle.py tests/graphrag/test_tenant_ingestion_config.py -v`
Expected: 全部 PASS

- [ ] **Step 8: 提交**

```bash
git add app/graphrag/ontology_lifecycle.py tests/graphrag/test_ontology_lifecycle.py
git commit -m "feat(graphrag): branch checkout_draft default seeding on tenant ingestion_mode"
```

---

### Task 6: deps.py / review_factory.py / ingestion/main.py 接线收尾 + 端到端回归

**Files:**
- Modify: `app/api/deps.py`（`get_graph_client`、`get_terms`）
- Modify: `app/ingestion/main.py`
- Test: `tests/api/test_deps.py`
- Test: `tests/graphrag/test_review_factory.py`

**Interfaces:**
- Consumes：Task 3 的 `Neo4jGraphClient.ensure_tenant_scoped_schema()`；Task 1 的 `list_terms(conn, tenant_id)`
- Produces：`get_graph_client()` 返回的单例已经跑过 `ensure_tenant_scoped_schema()`；`get_terms()` 按 `tenant_id` 过滤

- [ ] **Step 1: 写失败的测试**

在 `tests/api/test_deps.py` 新增（参照该文件已有的 `test_get_review_conn_creates_ontology_tables` 写法，通过真实依赖链而非 `dependency_overrides`；先阅读该文件确认真实使用的 settings 构造辅助函数名字，替换下面示例里的 `_settings()` 占位调用为该文件实际使用的那个）：

```python
async def test_get_graph_client_calls_ensure_tenant_scoped_schema(monkeypatch):
    """走真实的 get_graph_client 依赖链（不用 dependency_overrides 绕过），
    验证单例第一次构建时会调用 ensure_tenant_scoped_schema——这是 Task 3
    新建索引/回填存量节点唯一会被执行到的地方，漏接线的话索引永远不会
    被创建，全表扫描问题在真实环境里一直存在。
    """
    import app.api.deps as deps_module

    monkeypatch.setattr(deps_module, "_graph_client_cache", None)
    calls: list[str] = []

    class _FakeGraphClient:
        async def ensure_tenant_scoped_schema(self) -> None:
            calls.append("ensure_tenant_scoped_schema")

    monkeypatch.setattr(
        deps_module, "build_graph_client_from_settings", lambda settings: _FakeGraphClient()
    )

    await deps_module.get_graph_client(settings=_settings())

    assert calls == ["ensure_tenant_scoped_schema"]
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/api/test_deps.py::test_get_graph_client_calls_ensure_tenant_scoped_schema -v`
Expected: FAIL

- [ ] **Step 3: 改造 deps.py**

```python
async def get_graph_client(
    settings: Settings = Depends(get_settings),
) -> Neo4jGraphClient:
    """进程内单例，避免每次请求都新建一个 Neo4j 驱动连接池。"""
    global _graph_client_cache
    if _graph_client_cache is None:
        async with _graph_client_lock:
            if _graph_client_cache is None:
                client = build_graph_client_from_settings(settings)
                await client.ensure_tenant_scoped_schema()
                _graph_client_cache = client
    return _graph_client_cache


async def get_terms(
    review_conn: aiosqlite.Connection = Depends(get_review_conn),
    tenant_id: str = Depends(resolve_tenant_id),
) -> list[Term]:
    """每次请求都查 terms 表，不再进程级缓存（原因见函数改造前的说明，
    未变）。按 tenant_id 过滤——resolve_tenant_id 是本文件已有的依赖，
    从请求链路解析当前租户。"""
    return await list_terms(review_conn, tenant_id)
```

- [ ] **Step 4: 确认 review_factory.py 无需改动**

检查 `app/graphrag/review_factory.py::build_review_conn_from_settings` 里对 `ensure_terms_schema`/`ensure_ontology_schema` 的调用——这两个函数的**外部签名本任务未改动**（`ensure_terms_schema(conn, *, seed_yaml_path=None)`、`ensure_ontology_schema(conn)`），只是内部行为变了（会做 Task 1/2/5 的迁移和建表），因此 `review_factory.py` 现有的调用代码不需要改动。本步骤只需运行 `tests/graphrag/test_review_factory.py` 里既有的 `test_build_review_conn_from_settings_creates_ontology_tables`，确认仍然通过（证明 Task 1/2/5 的表迁移逻辑挂在这条路径上依然生效）。

- [ ] **Step 5: 改造 app/ingestion/main.py**

第 75 行：

```python
        resolved_graph_terms = graph_terms or await list_terms(resolved_graph_review_conn)
```

改成：

```python
        resolved_graph_terms = graph_terms or await list_terms(
            resolved_graph_review_conn, tenant_id
        )
```

（`main()` 函数签名本身已经有 `tenant_id: str` 参数，这里只是把它传给 `list_terms`。）

- [ ] **Step 6: 运行测试确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/api/test_deps.py tests/graphrag/test_review_factory.py -v`
Expected: 全部 PASS

- [ ] **Step 7: 全量回归测试**

Run: `.venv/Scripts/python.exe -u -m pytest -q`
Expected: 除了预先已知的、与本计划无关的 `tests/providers/test_voice_factory.py::test_returns_none_when_tts_not_configured` 之外，全部 PASS。

- [ ] **Step 8: 提交**

```bash
git add app/api/deps.py app/ingestion/main.py tests/api/test_deps.py
git commit -m "feat(api): wire ensure_tenant_scoped_schema into graph client singleton, scope get_terms to tenant"
```

---

## Self-Review（写计划人自查，非 subagent 执行）

**Spec 覆盖检查**（对照 `docs/superpowers/specs/2026-08-15-etl-driven-schema-construction-design.md`）：
- §2 `ingestion_mode` + checkout_draft 分支 → Task 5 ✅
- §3 node_key/迁移/索引 → Task 1（SQLite）、Task 3（Neo4j 索引+回填）✅
- §4 term_type 按租户隔离 → Task 2 ✅
- §5.3 `node_key_template` → Task 2 ✅
- §6 `extra_fields` 类型化、§7 `schema_etl.py`、§8 结构化过滤查询工具 → **不在本计划范围内**，留给后续"计划 2""计划 3"。
- 本计划新增的 Term/terms 表按租户隔离（源于评估 ADR-0001 时发现的扩大范围，对应 `docs/EXECUTION_PLAN.md` 第9节"尚未做的"欠账）→ Task 1/3/4/6 ✅

**任务间依赖检查（本轮自查新增，修正了初稿的循环依赖问题）**：初稿曾把 `terms_store.py` 的写入函数（Task 2）和 `ontology_categories.py` 的租户化（Task 3）分成两个独立任务，但 `_validate_categories` 需要调用改造后的 `list_term_types(conn, tenant_id)`，而 `ontology_categories.py` 的级联删除又需要 `terms` 表已经完成迁移——两者互相依赖，会导致 Task 2 自己的测试在 Task 3 完成前就报 `TypeError`。修正为：Task 1 合并了 terms_store.py 的全部读写函数（`_validate_categories` 暂时保留全局校验，有文档说明的临时状态），Task 2 完成 `ontology_categories.py` 改造后，用一个收尾步骤（Task 2 Step 4）回来把 `_validate_categories`/`_bridge_seed_categories_from_existing_terms` 的调用收紧成按租户——每个任务现在都能独立跑通自己的测试。

**占位符扫描**：全文所有代码块均为可直接运行的完整实现/测试，无 TBD/TODO/"add appropriate handling" 类占位表述。Task 6 Step 1 的 `_settings()` 是唯一一处要求实现者先查阅目标测试文件里实际的 settings 构造辅助函数名字再替换的地方，已在该步骤正文里显式标注，不是遗漏的占位符。

**类型一致性检查**：`Term` 在 Task 1 定义 `tenant_id`/`node_key`/`standard_name` 三个字符串字段，Task 2-6 所有函数签名和 Cypher 参数名统一使用这三个名字，未出现漂移。`TermTypeCategory.node_key_template` 在 Task 2 定义后，贯穿 Task 2/4 的 CRUD 函数、路由、Pydantic 模型全部一致使用这个字段名。

## 后续计划

- **计划 2**：`extra_fields` 类型化（架构文档 §6）+ `schema_etl.py` ETL 写入引擎与稳定码注册机制（§7）。
- **计划 3**：结构化过滤查询工具（§8）。

两份计划的具体任务待本计划落地、根据实际执行中的发现再细化编写（参照本计划开头 Scope Check 阶段与用户确认的拆分方式）。
