from __future__ import annotations

import json
import re
from dataclasses import dataclass

import aiosqlite

from app.db_migrations import add_column_if_missing
from app.graphrag.ontology_change_log import (
    ACTION_CREATE,
    ACTION_DELETE,
    ACTION_UPDATE,
    KIND_TERM_TYPE,
    commit_with_change_log,
    ensure_change_log_schema,
)

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS ontology_term_types (
    tenant_id                 TEXT NOT NULL,
    value                     TEXT NOT NULL,
    extra_fields              TEXT NOT NULL DEFAULT '[]',
    node_key_template         TEXT NOT NULL DEFAULT '',
    standard_name_value_type  TEXT NOT NULL DEFAULT 'string',
    status                    TEXT NOT NULL,
    PRIMARY KEY (tenant_id, value, status)
);
"""

_VALID_EXTRA_FIELD_VALUE_TYPES = frozenset({"string", "number", "integer", "number[]", "date"})
# date 有意**不**进下面这张表：standard_name 是实体的名字，一个日期不该当
# 实体的名字。两张白名单从此不对称，这是有意的，不是漏了。
_VALID_STANDARD_NAME_VALUE_TYPES = frozenset({"string", "number", "integer"})

_EXTRA_FIELD_NAME_PATTERN = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]{0,63}\Z")


class CategoryNotFoundError(Exception):
    """指定的分类枚举值不存在。"""


class CategoryInUseError(Exception):
    """删除的分类枚举值仍被合并视图里看得见的术语（或当前草稿的关系约束）引用，
    必须阻止。

    拦截的理由**不是**外键：terms 表的 DDL 里一条 FOREIGN KEY 都没有
    （见 terms_store.py::_SCHEMA_SQL，term_type 只是 TEXT NOT NULL），
    SQLite 默认 PRAGMA foreign_keys=OFF，就算声明了也不强制。删掉一个仍被
    引用的分类，坏掉的是语义而不是行结构——已有术语行照样读得出来，只是
    它的 term_type 指向一个不存在的分类，于是：

    - structured_filter_query 按 term_type 过滤/跳转时直接报错「term_type
      不在已确认 schema 里」（见 structured_filter_query.py::
      _resolve_field_value_type）；
    - extra_fields 的字段类型声明挂在分类上，分类没了，实体
      extra_properties 里的数据还在，但没人知道那些字段该是什么类型；
    - 实体列表的分组摘要按 term_type 分组（admin_terms_routes.py::
      get_terms_summary），会出现一个不在分类列表里的孤儿组。

    这三件事够麻烦，所以拦截保留；但别把它当成结构性约束——它是一道
    应用层守卫，不是数据库替我们把的关。

    除了人话消息，还带一份结构化的"挡路的是谁"：terms_count/allowlist_count
    是总数，blocking_term_node_keys 是前几条挡路术语的 node_key（消息里点名
    的那几条，供调用方生成"去实体列表里筛出它们"的链接）。只报数字的提示
    用户看得见却纠正不了——服务端查引用计数时那几条就在手里。"""

    def __init__(
        self,
        message: str,
        *,
        term_type: str = "",
        terms_count: int = 0,
        allowlist_count: int = 0,
        blocking_term_node_keys: list[str] | None = None,
    ) -> None:
        super().__init__(message)
        self.term_type = term_type
        self.terms_count = terms_count
        self.allowlist_count = allowlist_count
        self.blocking_term_node_keys = blocking_term_node_keys or []


class CategoryNameConflictError(Exception):
    """提交的分类值已存在。"""


class InvalidExtraFieldTypeError(Exception):
    """extra_fields 里某个字段声明的 value_type 不是 "string"/"number"/"integer"/
    "number[]"/"date" 之一——在声明时（create_term_type/update_term_type）就拒绝，
    不推迟到某条术语真正提交这个字段的值时才发现（见 Global Constraints）。"""


@dataclass(frozen=True)
class ExtraFieldSpec:
    """属性字段的声明。

    name 是**内部名**，必须是 ASCII 标识符：它会被拼进 Cypher 文本、成为
    Neo4j 索引的属性名和结构化查询接受的字段名（见
    structured_filter_query.py::_resolve_field_value_type）。label 是
    **显示名**，只给人看，可以是中文，没有格式限制。

    两者分开而不是把 name 放开成中文，是因为这两个名字服务于不同的读者：
    内部名要出现在 Cypher、索引和报错信息里，显示名要出现在界面和问答里。
    客户（MUJI 商品知识中台）的字段设计表本来就是「字段中文名」和「字段ID」
    两列并存（中文名「当前售价」对应 ID md_sku_price），这个拆分是这类项目
    实际的工作方式，不是我们为了绕开格式校验发明的抽象。

    label 允许为空：不做存量数据迁移，2026-09-07 之前声明的字段读出来
    label 就是 ""，此时显示名回退到内部名（见 display_name）。
    """

    name: str
    value_type: str
    label: str = ""

    @property
    def display_name(self) -> str:
        """给人看的名字：有显示名就用显示名，没有就回退到内部名。"""
        return self.label or self.name


@dataclass(frozen=True)
class TermTypeCategory:
    value: str
    extra_fields: list[ExtraFieldSpec]
    standard_name_value_type: str = "string"


def _validate_extra_field_specs(extra_fields: list[ExtraFieldSpec]) -> None:
    for spec in extra_fields:
        if not _EXTRA_FIELD_NAME_PATTERN.match(spec.name):
            raise InvalidExtraFieldTypeError(
                f"字段名 {spec.name!r} 不合法，必须满足 ^[a-zA-Z_][a-zA-Z0-9_]{{0,63}}$"
                f"（后续要作为 Neo4j 索引属性名/结构化查询字段名使用，不能含空格或特殊字符）"
            )
        if spec.value_type not in _VALID_EXTRA_FIELD_VALUE_TYPES:
            raise InvalidExtraFieldTypeError(
                f"字段 {spec.name!r} 声明的类型 {spec.value_type!r} 不合法，"
                f"仅支持: {sorted(_VALID_EXTRA_FIELD_VALUE_TYPES)}"
            )


def _validate_standard_name_value_type(value_type: str) -> None:
    if value_type not in _VALID_STANDARD_NAME_VALUE_TYPES:
        raise InvalidExtraFieldTypeError(
            f"term type 自身取值类型 {value_type!r} 不合法，"
            f"仅支持: {sorted(_VALID_STANDARD_NAME_VALUE_TYPES)}"
        )


def _extra_fields_to_dicts(extra_fields: list[ExtraFieldSpec]) -> list[dict]:
    return [
        {"name": f.name, "value_type": f.value_type, "label": f.label} for f in extra_fields
    ]


def _extra_fields_to_json(extra_fields: list[ExtraFieldSpec]) -> str:
    return json.dumps(_extra_fields_to_dicts(extra_fields), ensure_ascii=False)


def _extra_fields_from_json(raw: str) -> list[ExtraFieldSpec]:
    # label 用 get 兜底而不是 []：存量行的 JSON 里没有这个键，这里不做
    # 数据迁移，缺失就是空串，显示时由 ExtraFieldSpec.display_name 回退到
    # 内部名。
    return [
        ExtraFieldSpec(
            name=item["name"], value_type=item["value_type"], label=item.get("label", "")
        )
        for item in json.loads(raw)
    ]


async def _migrate_term_types_table_if_needed(conn: aiosqlite.Connection) -> None:
    """把 2026-08-15 之前的 ontology_term_types 表（value 主键，无 tenant_id）
    原地迁移成按租户隔离的新结构，存量数据统一归到 tenant_id='default'。
    幂等，逻辑与 terms_store.py::_migrate_terms_table_to_tenant_scoped_if_needed
    同构。

    表里仍保留 node_key_template 这一列（NOT NULL DEFAULT ''），但从
    2026-08-18 起应用层代码不再读写它——这个字段最初设计是给 ETL 场景声明
    node_key 拼接模板用的，但实际的 ETL 写入引擎（app/graphrag/schema_etl.py）
    走的是每个租户 ETL 配置里独立声明的 node_key_parts，从不读取这一列，
    它从未真正被消费过。保留列本身只是为了不做一次没有实际收益的
    ALTER TABLE ... DROP COLUMN 迁移，新行都会落一个从未被读取的空字符串。
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
        "INSERT INTO ontology_term_types_new (tenant_id, value, extra_fields) "
        "SELECT 'default', value, extra_fields FROM ontology_term_types"
    )
    await conn.executescript(
        "DROP TABLE ontology_term_types; "
        "ALTER TABLE ontology_term_types_new RENAME TO ontology_term_types;"
    )
    await conn.commit()


async def _migrate_term_types_add_status_if_needed(conn: aiosqlite.Connection) -> None:
    """把 2026-08-19 之前没有 status 列的 ontology_term_types 表（PK 是
    (tenant_id, value)，所有行隐含"直接生效"）迁移成带草稿/已确认两态的
    新结构（PK 变成 (tenant_id, value, status)）。存量行全部落
    status='confirmed'——迁移前它们本来就是"当前生效"的状态，语义上对应
    新模型里的已确认，不是草稿。跟 _migrate_term_types_table_if_needed
    同构，必须排在它之后调用（那个函数先保证 tenant_id 列存在，这个函数
    的 SELECT 依赖 tenant_id 列已经在）。
    """
    cursor = await conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='ontology_term_types'"
    )
    if await cursor.fetchone() is None:
        return
    cursor = await conn.execute("PRAGMA table_info(ontology_term_types)")
    existing_columns = {row[1] for row in await cursor.fetchall()}
    if "status" in existing_columns:
        return
    await conn.executescript(
        """
        CREATE TABLE ontology_term_types_new (
            tenant_id         TEXT NOT NULL,
            value             TEXT NOT NULL,
            extra_fields      TEXT NOT NULL DEFAULT '[]',
            node_key_template TEXT NOT NULL DEFAULT '',
            status            TEXT NOT NULL,
            PRIMARY KEY (tenant_id, value, status)
        );
        """
    )
    await conn.execute(
        "INSERT INTO ontology_term_types_new "
        "(tenant_id, value, extra_fields, node_key_template, status) "
        "SELECT tenant_id, value, extra_fields, node_key_template, 'confirmed' "
        "FROM ontology_term_types"
    )
    await conn.executescript(
        "DROP TABLE ontology_term_types; "
        "ALTER TABLE ontology_term_types_new RENAME TO ontology_term_types;"
    )
    await conn.commit()


async def _migrate_extra_fields_value_shape_if_needed(conn: aiosqlite.Connection) -> None:
    """把 2026-08-16 之前的 extra_fields 数据（纯字符串列表，如
    '["严重等级", "影响范围"]'）原地升级成带类型声明的形态（如
    '[{"name": "严重等级", "value_type": "string"}, ...]'）。旧字段统一按
    "string" 类型对待（Global Constraints 的迁移规则——旧数据从没有类型
    信息，"string" 是唯一能兼容旧数据里任意已写文本值的选择）。逐行检测：
    JSON 解出来的列表如果第一个元素是 str（而不是 dict），判定为旧形态，
    转换后 UPDATE 回去；空列表或已经是新形态（元素是 dict）的行原样跳过，
    保证幂等。
    """
    cursor = await conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='ontology_term_types'"
    )
    if await cursor.fetchone() is None:
        return
    cursor = await conn.execute("SELECT tenant_id, value, extra_fields FROM ontology_term_types")
    rows = await cursor.fetchall()
    for tenant_id, value, extra_fields_raw in rows:
        parsed = json.loads(extra_fields_raw)
        if not parsed or isinstance(parsed[0], dict):
            continue
        migrated = json.dumps(
            [{"name": name, "value_type": "string"} for name in parsed], ensure_ascii=False
        )
        await conn.execute(
            "UPDATE ontology_term_types SET extra_fields = ? WHERE tenant_id = ? AND value = ?",
            (migrated, tenant_id, value),
        )
    await conn.commit()


async def _migrate_term_types_add_standard_name_value_type_if_needed(
    conn: aiosqlite.Connection,
) -> None:
    cursor = await conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='ontology_term_types'"
    )
    if await cursor.fetchone() is None:
        return
    await add_column_if_missing(
        conn, table="ontology_term_types", column="standard_name_value_type",
        ddl="TEXT NOT NULL DEFAULT 'string'",
    )


async def ensure_categories_schema(conn: aiosqlite.Connection) -> None:
    # 变更日志表跟着建：本模块每个写入函数都往它写一行，而单独调用
    # ensure_categories_schema 的连接（测试里就有）不会经过
    # ensure_ontology_schema，缺表会在第一次写入时撞 no such table。
    await ensure_change_log_schema(conn)
    await conn.execute("DROP TABLE IF EXISTS ontology_product_lines")
    await _migrate_term_types_table_if_needed(conn)
    await _migrate_term_types_add_status_if_needed(conn)
    await conn.executescript(_SCHEMA_SQL)
    await conn.commit()
    await _migrate_extra_fields_value_shape_if_needed(conn)
    await _migrate_term_types_add_standard_name_value_type_if_needed(conn)


def _row_to_term_type(row: aiosqlite.Row) -> TermTypeCategory:
    return TermTypeCategory(
        value=row["value"],
        extra_fields=_extra_fields_from_json(row["extra_fields"]),
        standard_name_value_type=row["standard_name_value_type"],
    )


async def list_term_types(
    conn: aiosqlite.Connection, tenant_id: str, *, status: str
) -> list[TermTypeCategory]:
    conn.row_factory = aiosqlite.Row
    cursor = await conn.execute(
        "SELECT value, extra_fields, standard_name_value_type FROM ontology_term_types "
        "WHERE tenant_id = ? AND status = ? ORDER BY value",
        (tenant_id, status),
    )
    rows = await cursor.fetchall()
    return [_row_to_term_type(row) for row in rows]


async def create_term_type(
    conn: aiosqlite.Connection,
    tenant_id: str,
    *,
    value: str,
    actor: str,
    extra_fields: list[ExtraFieldSpec] | None = None,
    standard_name_value_type: str = "string",
) -> None:
    """actor 是这次变更的操作者，必填、无默认值——见
    ontology_change_log 模块 docstring 里为什么不给默认值。"""
    extra_fields = extra_fields or []
    _validate_extra_field_specs(extra_fields)
    _validate_standard_name_value_type(standard_name_value_type)
    try:
        await conn.execute(
            "INSERT INTO ontology_term_types "
            "(tenant_id, value, extra_fields, standard_name_value_type, status) "
            "VALUES (?, ?, ?, ?, 'draft')",
            (tenant_id, value, _extra_fields_to_json(extra_fields), standard_name_value_type),
        )
    except aiosqlite.IntegrityError:
        raise CategoryNameConflictError(f"{value!r} 已经是该租户草稿里的分类，不能重复创建")
    await commit_with_change_log(
        conn, tenant_id, actor=actor, action=ACTION_CREATE, object_kind=KIND_TERM_TYPE,
        object_id=value,
        details={
            "extra_fields": _extra_fields_to_dicts(extra_fields),
            "standard_name_value_type": standard_name_value_type,
            "status": "draft",
        },
    )


async def update_term_type(
    conn: aiosqlite.Connection,
    tenant_id: str,
    *,
    value: str,
    new_value: str,
    extra_fields: list[ExtraFieldSpec],
    actor: str,
    standard_name_value_type: str = "string",
) -> None:
    """value 是草稿里的当前名字，new_value 是提交的新名字，允许相同（即不
    改名）。改名只级联更新该租户草稿约束表（term_type_relation_allowlist）
    里引用旧名字的 draft 行——不再级联更新 terms 表（真实术语只引用已确认
    类型，改草稿定义不影响它们；已确认类型改名后要同步真实术语，用新的
    "迁移实体类型"工具手动触发，见 terms_store.py::migrate_term_type）。
    """
    _validate_extra_field_specs(extra_fields)
    _validate_standard_name_value_type(standard_name_value_type)
    cursor = await conn.execute(
        "SELECT 1 FROM ontology_term_types WHERE tenant_id = ? AND value = ? AND status = 'draft'",
        (tenant_id, value),
    )
    if await cursor.fetchone() is None:
        raise CategoryNotFoundError(f"草稿里不存在分类: {value}")
    try:
        await conn.execute(
            "UPDATE ontology_term_types SET value = ?, extra_fields = ?, standard_name_value_type = ? "
            "WHERE tenant_id = ? AND value = ? AND status = 'draft'",
            (new_value, _extra_fields_to_json(extra_fields), standard_name_value_type, tenant_id, value),
        )
    except aiosqlite.IntegrityError:
        raise CategoryNameConflictError(f"{new_value!r} 已经是该租户草稿里的分类，不能重复使用")
    if new_value != value:
        await conn.execute(
            "UPDATE OR IGNORE term_type_relation_allowlist SET subject_term_type = ? "
            "WHERE tenant_id = ? AND subject_term_type = ? AND status = 'draft'",
            (new_value, tenant_id, value),
        )
        await conn.execute(
            "UPDATE OR IGNORE term_type_relation_allowlist SET object_term_type = ? "
            "WHERE tenant_id = ? AND object_term_type = ? AND status = 'draft'",
            (new_value, tenant_id, value),
        )
    await commit_with_change_log(
        conn, tenant_id, actor=actor, action=ACTION_UPDATE, object_kind=KIND_TERM_TYPE,
        # object_id 记的是改名前的名字（这次变更操作的是哪一行），改成什么
        # 在 details.new_value 里——两个都要有，否则日志上一条改名前后接不
        # 上另一条。
        object_id=value,
        details={
            "new_value": new_value,
            "extra_fields": _extra_fields_to_dicts(extra_fields),
            "standard_name_value_type": standard_name_value_type,
        },
    )


#: CategoryInUseError 消息里每类引用最多点名几条。3 条够用户认出"哦是那批
#: 测试数据"，再多消息就长得没人读了；剩下的用总数兜底。
_IN_USE_SAMPLE_SIZE = 3


def _format_samples(samples: list[str], total: int) -> str:
    """把样本拼成"a、b、c 等共 12 条"。样本已经是全部时不加尾巴——
    "1 条：示例登录模块 等共 1 条"读起来像还有别的没列出来。"""
    listed = "、".join(samples)
    if total > len(samples):
        return f"{listed} 等共 {total} 条"
    return listed


async def delete_term_type(
    conn: aiosqlite.Connection, tenant_id: str, value: str, *, actor: str
) -> None:
    """terms 表引用检查走**合并视图**（terms 叠加 term_edits），不是裸表：
    这道守卫回答的是"用户还看得见这个类型下的实体吗"，跟实体列表页同一个
    口径——被人工删除（__deleted__）的实体在列表里已经不存在，不该在这里
    挡住删除，否则用户面对的是一堵他在界面上找不到砖头的墙。裸表那个数字
    （count_terms_by_term_type）回答的是另一个问题——"管道往这个类型里写了
    多少"，本体图的节点数量叠加用它，两者不该混用。检查范围仍不区分
    ontology_term_types 自己的 status（真实术语只引用已确认类型，这个检查
    天然对应"已确认版本是否在用"）。

    term_type_relation_allowlist 引用检查加 status='draft'——只拦"删除会
    破坏当前草稿自洽性"的情况，跟这次删除无关的已确认约束不受影响。这条
    检查跟编辑层无关（它查的是约束表，不是 terms），维持裸查。
    """
    # 函数内导入：terms_store 在模块顶层导入了本模块（list_term_types /
    # ensure_categories_schema），顶层反向导入会形成循环。
    from app.graphrag.terms_store import count_and_sample_terms_merged_by_term_type

    # 计数和"挡路的是谁"一次拿全：只报"1 条术语"用户得自己去实体列表里翻找
    # 挡路的是哪条。样本和计数出自同一次合并，点名的必然是用户在列表里真找
    # 得到的那几条——按裸表点名会报出已被人工删除、界面上根本不存在的名字。
    # 样本按 standard_name 排序：同一份数据每次点名同样那几条，顺序随机时
    # 用户处理掉一条再删会换一批名字，看起来像"越删越多"。
    terms_count, blocking_terms = await count_and_sample_terms_merged_by_term_type(
        conn, tenant_id, value, sample_limit=_IN_USE_SAMPLE_SIZE
    )
    cursor = await conn.execute(
        "SELECT COUNT(*) FROM term_type_relation_allowlist "
        "WHERE tenant_id = ? AND status = 'draft' AND (subject_term_type = ? OR object_term_type = ?)",
        (tenant_id, value, value),
    )
    allowlist_count = (await cursor.fetchone())[0]
    if terms_count > 0 or allowlist_count > 0:
        # 约束这一类同样要点名到具体三元组，理由同上；固定的 ORDER BY
        # 也是同一个理由。
        cursor = await conn.execute(
            "SELECT subject_term_type, relation_type, object_term_type "
            "FROM term_type_relation_allowlist "
            "WHERE tenant_id = ? AND status = 'draft' AND (subject_term_type = ? OR object_term_type = ?) "
            "ORDER BY subject_term_type, relation_type, object_term_type LIMIT ?",
            (tenant_id, value, value, _IN_USE_SAMPLE_SIZE),
        )
        allowlist_rows = await cursor.fetchall()
        parts = []
        if terms_count > 0:
            parts.append(
                f"{terms_count} 条术语（"
                f"{_format_samples([t.standard_name for t in blocking_terms], terms_count)}）"
            )
        if allowlist_count > 0:
            parts.append(
                f"{allowlist_count} 条关系约束（"
                f"{_format_samples([f'{r[0]} -{r[1]}-> {r[2]}' for r in allowlist_rows], allowlist_count)}）"
            )
        raise CategoryInUseError(
            f"分类 {value!r} 仍被 {'、'.join(parts)}引用，无法删除",
            term_type=value,
            terms_count=terms_count,
            allowlist_count=allowlist_count,
            blocking_term_node_keys=[t.node_key for t in blocking_terms],
        )
    await conn.execute(
        "DELETE FROM ontology_term_types WHERE tenant_id = ? AND value = ? AND status = 'draft'",
        (tenant_id, value),
    )
    # 行已经没了，"谁删的"从此只能问这张日志表——这是它存在的全部理由。
    await commit_with_change_log(
        conn, tenant_id, actor=actor, action=ACTION_DELETE, object_kind=KIND_TERM_TYPE,
        object_id=value, details={"status": "draft"},
    )
