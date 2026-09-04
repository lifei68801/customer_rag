from __future__ import annotations

import json
import re
from datetime import datetime

import aiosqlite

from app.graphrag.ontology_categories import InvalidExtraFieldTypeError, ensure_categories_schema
from app.graphrag.ontology_constraints import UnknownCategoryError, ensure_constraints_schema
from app.graphrag.ontology_etl_mapping import ensure_etl_mapping_schema, set_draft_etl_mapping
from app.graphrag.ontology_relations import (
    InvalidRelationTypeNameError,
    ensure_relations_schema,
    seed_default_relation_types,
)
from app.graphrag.tenant_ingestion_config import ensure_ingestion_config_schema, get_ingestion_mode

# 下面这几条正则/常量、_validate_draft_* 三个函数，是 ontology_categories.py 的
# _validate_extra_field_specs/_validate_standard_name_value_type 和
# ontology_relations.py 的 _validate_relation_type 的复制品，不是导入。
#
# 为什么不直接跨模块导入：那三个函数名带下划线前缀，是各自模块明确标记的"模块
# 私有实现细节"，不是对外契约——它们的签名（比如接收 list[ExtraFieldSpec] 还是
# list[dict]）随时可能因为各自模块内部重构而改变，不为外部调用方的稳定性负责。
# 这里的输入形状也确实不同：单条创建接口按参数逐个传（value / extra_fields /
# standard_name_value_type），而这里收到的是整份草案里的原始 dict 列表，直接传
# 给对方的私有函数类型对不上。
#
# 代价是两处规则要保持同步：如果以后 ontology_categories.py 或
# ontology_relations.py 改了合法性规则（比如放宽字段名格式），这里也要跟着改，
# 否则会出现"单条创建时报错，整份替换草稿时放行"的不一致体验。
_EXTRA_FIELD_NAME_PATTERN = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]{0,63}\Z")
_VALID_EXTRA_FIELD_VALUE_TYPES = frozenset({"string", "number", "integer", "number[]"})
_VALID_STANDARD_NAME_VALUE_TYPES = frozenset({"string", "number", "integer"})
_RELATION_TYPE_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]{0,63}\Z")


def _validate_draft_extra_fields(extra_fields: list[dict]) -> list[dict]:
    """校验并规整一个 term_type 的 extra_fields 声明，返回只含 name/value_type
    两个键的规整列表（原始 dict 里混入的多余键不落库）。"""
    normalized: list[dict] = []
    for field in extra_fields:
        name = field["name"]
        value_type = field["value_type"]
        if not _EXTRA_FIELD_NAME_PATTERN.match(name):
            raise InvalidExtraFieldTypeError(
                f"字段名 {name!r} 不合法，必须满足 ^[a-zA-Z_][a-zA-Z0-9_]{{0,63}}$"
            )
        if value_type not in _VALID_EXTRA_FIELD_VALUE_TYPES:
            raise InvalidExtraFieldTypeError(
                f"字段 {name!r} 声明的类型 {value_type!r} 不合法，"
                f"仅支持: {sorted(_VALID_EXTRA_FIELD_VALUE_TYPES)}"
            )
        normalized.append({"name": name, "value_type": value_type})
    return normalized


def _validate_draft_standard_name_value_type(value_type: str) -> None:
    if value_type not in _VALID_STANDARD_NAME_VALUE_TYPES:
        raise InvalidExtraFieldTypeError(
            f"term type 自身取值类型 {value_type!r} 不合法，"
            f"仅支持: {sorted(_VALID_STANDARD_NAME_VALUE_TYPES)}"
        )


def _validate_draft_relation_type(relation_type: str, example_phrase: str) -> None:
    if not _RELATION_TYPE_PATTERN.match(relation_type):
        raise InvalidRelationTypeNameError(
            f"关系类型名字不合法: {relation_type!r}，必须满足 ^[A-Z][A-Z0-9_]{{0,63}}$"
        )
    if not example_phrase.strip():
        raise InvalidRelationTypeNameError("example_phrase 不能为空")


_TABLES_WITH_TENANT_LIFECYCLE = (
    "tenant_relation_types", "term_type_relation_allowlist", "ontology_term_types",
    # 引导产出的 ETL 映射与本体同生命周期，见 ontology_etl_mapping.py。
    # 注意它**不**参与 confirm_ontology 的 has_draft_in_any_table 早退判据。
    "ontology_etl_mapping",
)

_CHECKOUT_STATE_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS ontology_draft_checkout_state (
    tenant_id TEXT PRIMARY KEY
);
"""


async def _ensure_checkout_state_schema(conn: aiosqlite.Connection) -> None:
    await conn.executescript(_CHECKOUT_STATE_SCHEMA_SQL)
    await conn.commit()


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
    await ensure_etl_mapping_schema(conn)
    await _ensure_checkout_state_schema(conn)


async def _has_any_row(
    conn: aiosqlite.Connection, table: str, tenant_id: str, status: str
) -> bool:
    cursor = await conn.execute(
        f"SELECT 1 FROM {table} WHERE tenant_id = ? AND status = ? LIMIT 1",
        (tenant_id, status),
    )
    return await cursor.fetchone() is not None


async def _has_checked_out_since_last_confirm(conn: aiosqlite.Connection, tenant_id: str) -> bool:
    cursor = await conn.execute(
        "SELECT 1 FROM ontology_draft_checkout_state WHERE tenant_id = ?", (tenant_id,)
    )
    return await cursor.fetchone() is not None


async def checkout_draft(conn: aiosqlite.Connection, tenant_id: str) -> None:
    """检出一份可编辑的草稿：如果该租户自上次 confirm 以来已经检出过草稿，什么都
    不做（哪怕当前三张草稿表都是空的——那是用户主动删完了草稿内容的合法终态，
    不是"还没检出过"，不能拿"草稿是否为空"当检出信号，见下面的踩坑记录）；如果
    还没检出过但有已确认版本，从已确认版本复制一份新草稿；如果两者都没有（全新
    租户），关系类型草稿的默认值取决于该租户的接入模式
    （tenant_ingestion_config.ingestion_mode）——extraction 模式播种 10 种
    通用默认关系（面向 LLM 抽取场景设计，见 ontology_relations.py），etl
    模式不播种，从空白草稿开始（这些默认关系对结构化 ETL 租户没有意义，
    见 docs/superpowers/specs/2026-08-15-etl-driven-schema-construction-
    design.md §2）。约束表草稿两种模式都留空（没有分类数据支撑，写不出
    有意义的默认组合）。

    踩坑记录（这个函数曾经的实现方式，以及为什么改掉了）：早期版本没有
    ontology_draft_checkout_state 这张表，直接用"这张草稿表当前有没有行"
    当"是否需要从已确认版本重新播种"的信号。这个信号在草稿从有到无是"用户主动
    删除干净"的场景下会撒谎——管理后台里删除本体管理页面（实体类型/关系类型/
    约束三个 tab）任何一条草稿记录后，前端都会紧接着调用一次本函数刷新界面，
    如果删除的正好是该租户当时唯一一条草稿记录、且该租户历史上确认过 schema
    （已确认版本非空），旧实现会把这条刚删除的记录从已确认版本原样复制回来，
    用户在界面上完全看不出删除生效过——点删除、页面刷新、数据"原地不动"。现在
    用一张独立的状态表记录"自上次 confirm 以来是否已经检出过"，检出信号不再
    跟"当前行数是否为零"绑在一起，删空草稿就是删空草稿，不会被这个函数悄悄
    撤销。

    并发安全：三条"从已确认版本复制成草稿"的插入都用 INSERT OR IGNORE。
    这个函数是典型的"先查后写、中间无锁"——检查草稿是否为空、检查已确认
    版本是否存在、再插入，三步之间有 await 让出点，而 deps.get_review_conn
    是进程内单例连接，多个请求的协程共用它、可以在这些让出点互相穿插。
    管理后台的本体页面三个 tab（实体类型/关系类型/约束）各自发一次
    checkout，实测会几乎同时到达：两个请求都看到"草稿为空"、都执行复制，
    第二个撞主键 UNIQUE (tenant_id, relation_type, status)，返回 500，
    界面显示"schema 草稿初始化失败"。

    复制这件事天生幂等——行已经在了就该跳过——所以 OR IGNORE 是语义上
    正确的写法，而不是掩盖冲突。用加锁或包事务也能解决，但那会把单例
    连接上的并发请求串行化，代价更大，而且没必要：本来就该幂等的操作
    不需要互斥。
    """
    await _ensure_checkout_state_schema(conn)
    # 防御性建表，不能假设调用方一定跑过 ensure_ontology_schema——下面会对
    # ontology_etl_mapping 表做 INSERT OR IGNORE，缺表会炸
    # sqlite3.OperationalError: no such table（confirm_ontology 那边的同款
    # 调用能踩到这一坑，见那里的注释；这里做同样的防御是同一个理由）。
    await ensure_etl_mapping_schema(conn)
    if await _has_checked_out_since_last_confirm(conn, tenant_id):
        return
    if not await _has_any_row(conn, "tenant_relation_types", tenant_id, "draft"):
        if await _has_any_row(conn, "tenant_relation_types", tenant_id, "confirmed"):
            await conn.execute(
                "INSERT OR IGNORE INTO tenant_relation_types "
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
                "INSERT OR IGNORE INTO term_type_relation_allowlist "
                "(tenant_id, subject_term_type, relation_type, object_term_type, status) "
                "SELECT tenant_id, subject_term_type, relation_type, object_term_type, 'draft' "
                "FROM term_type_relation_allowlist WHERE tenant_id = ? AND status = 'confirmed'",
                (tenant_id,),
            )
    if not await _has_any_row(conn, "ontology_term_types", tenant_id, "draft"):
        if await _has_any_row(conn, "ontology_term_types", tenant_id, "confirmed"):
            await conn.execute(
                "INSERT OR IGNORE INTO ontology_term_types "
                "(tenant_id, value, extra_fields, node_key_template, status) "
                "SELECT tenant_id, value, extra_fields, node_key_template, 'draft' "
                "FROM ontology_term_types WHERE tenant_id = ? AND status = 'confirmed'",
                (tenant_id,),
            )
        # 新租户没有默认实体类型可播种——不同于关系类型有 10 种通用拓扑
        # 关系兜底，实体类型完全依赖业务定义，没有"合理默认值"这回事，
        # 两种接入模式（extraction/etl）在这一点上没有区别。
    await conn.execute(
        "INSERT OR IGNORE INTO ontology_etl_mapping "
        "(tenant_id, status, config_yaml, source_file_name, created_at) "
        "SELECT tenant_id, 'draft', config_yaml, source_file_name, created_at "
        "FROM ontology_etl_mapping WHERE tenant_id = ? AND status = 'confirmed'",
        (tenant_id,),
    )
    await conn.execute(
        "INSERT OR IGNORE INTO ontology_draft_checkout_state (tenant_id) VALUES (?)", (tenant_id,)
    )
    await conn.commit()


async def confirm_ontology(conn: aiosqlite.Connection, tenant_id: str) -> None:
    """把草稿原子性地提升为已确认版本：先删旧的已确认行，再把草稿行的 status 原地
    改成 confirmed——两张表（关系类型、约束）在同一次 commit 里一起提交，不会出现
    "关系类型确认了但约束表没确认"这种半提交状态。确认之后草稿即被清空（status
    改写成 confirmed，不再是 draft），下一次编辑需要重新调用 checkout_draft。

    防护：如果没有任何草稿行，直接返回（不操作已确认的数据），防止重复确认导致
    数据丢失。这使得函数对重复/重试请求自然幂等。

    确认成功后同时清掉 ontology_draft_checkout_state 里这个租户的检出标记——
    草稿这一轮编辑会话已经结束（内容原地提升成了 confirmed），下一次
    checkout_draft 应该被当成"还没检出过"，重新从刚确认的版本复制一份新草稿，
    而不是延续本轮已经过期的检出状态（那样会导致新草稿从一开始就是空的，
    checkout_draft 却因为标记还在而不去重新播种）。
    """
    await _ensure_checkout_state_schema(conn)
    # 防御性建表，不能假设调用方一定跑过 ensure_ontology_schema：
    # tests/graphrag/test_ontology_constraints.py 就只建了分类/关系/约束
    # 三张表的连接，直接调本函数，没有 ontology_etl_mapping 表会在下面的
    # DELETE/INSERT 上炸 sqlite3.OperationalError: no such table。
    await ensure_etl_mapping_schema(conn)
    # 如果两个表都没有草稿，说明没有新的内容要提升，直接返回以避免删除已确认数据
    has_draft_in_any_table = (
        await _has_any_row(conn, "tenant_relation_types", tenant_id, "draft")
        or await _has_any_row(conn, "term_type_relation_allowlist", tenant_id, "draft")
        or await _has_any_row(conn, "ontology_term_types", tenant_id, "draft")
    )
    if not has_draft_in_any_table:
        return

    for table in _TABLES_WITH_TENANT_LIFECYCLE:
        await conn.execute(
            f"DELETE FROM {table} WHERE tenant_id = ? AND status = 'confirmed'", (tenant_id,)
        )
        await conn.execute(
            f"UPDATE {table} SET status = 'confirmed' WHERE tenant_id = ? AND status = 'draft'",
            (tenant_id,),
        )
    await conn.execute(
        "DELETE FROM ontology_draft_checkout_state WHERE tenant_id = ?", (tenant_id,)
    )
    await conn.commit()


async def is_ontology_confirmed(conn: aiosqlite.Connection, tenant_id: str) -> bool:
    return await _has_any_row(conn, "tenant_relation_types", tenant_id, "confirmed")


async def replace_draft(
    conn: aiosqlite.Connection,
    tenant_id: str,
    *,
    term_types: list[dict],
    relation_types: list[dict],
    constraints: list[dict],
    etl_mapping: dict | None = None,
) -> None:
    """把该租户的三张草稿表整份替换成提交的内容。先把所有会失败的校验做完，
    再动手写；写入阶段不会再失败，因此不需要（也不应该）用显式事务包裹。

    为什么需要这个函数，而不是让调用方逐个调 create_term_type /
    create_relation_type / add_allowed_combination：

    引导一次要写入十几个对象，逐个调的话中途失败会留下半份草稿。而
    checkout_draft **不会**清空草稿（它只在"还没检出过"时才从已确认版
    复制），所以用户没有干净的重来方式，只能去三个 tab 逐个删。

    整份替换而不是增量合并：引导每次提交的都是一份完整草案（用户改一条边
    就重新提交整份）。增量合并的话，用户删掉的那个实体类型会留在草稿里
    ——界面上没有了，库里还在，确认时又冒出来。

    为什么不用 BEGIN/COMMIT/ROLLBACK 包住整个函数（这里曾经这样写过）：
    deps.get_review_conn 是进程内单例连接，所有并发请求的协程共用它。
    checkout_draft 的 docstring 记录过实测的并发穿插——管理后台本体页面
    三个 tab 各自发一次 checkout，实测会几乎同时到达同一个连接，在 await
    让出点之间互相插入执行——并且明确否决过"用事务/加锁解决"这条路：
    「用加锁或包事务也能解决，但那会把单例连接上的并发请求串行化，代价更大」。
    显式 BEGIN 会撞上更具体的问题：BEGIN 和 COMMIT 之间，同一连接上另一个
    协程的 execute 可能插进来，撞见 sqlite3 报的
    "cannot start a transaction within a transaction"，或者它的 commit()
    把这边尚未校验完的写入提前提交掉——不可靠的事务比没有事务更糟，因为它
    给人一种"出错会自动回滚"的保证错觉，而这个保证在这个连接模型下根本不
    成立。

    所以改成：原子性靠"先校验、后写入"来保证，而不是靠数据库事务。校验阶段
    （见下面 term_types/relation_types/constraints 三段）任何一项失败就直接
    抛异常，此时一行都还没写，草稿保持原样，效果跟"回滚"一样，但不需要真的
    有事务。校验全部通过后才做 DELETE + INSERT，这个阶段的每条语句本身都
    不会因为业务规则失败（该失败的都在校验阶段失败过了）。

    诚实的代价：写入阶段仍可能撞上 SQLite 层面的硬错误（磁盘满、数据库文件
    损坏等），届时可能留下一份不完整的草稿。但 replace_draft 本身是幂等的
    整份替换——用户在引导页重新提交一次就能覆盖回一致状态，不需要人工清理，
    这也是为什么 Global Constraints/brief 反复强调"整份替换而不是增量合并"
    ：增量合并没有这个自愈性质，整份替换有。

    校验顺序是先类型后约束：约束引用实体类型和关系类型，反过来先校验约束会
    撞上"引用的类型不存在"，而那时还不知道类型列表到底是什么。

    校验阶段同时检查提交内部的重复项（同一份提交里两个同名 term_type / 两个
    同名 relation_type / 两个相同的约束三元组）——这三张草稿表的主键都包含
    这些字段，重复项如果留到写入阶段才被发现，会以 aiosqlite.IntegrityError
    的形式出现，调用方（API 层）没法区分这跟其它数据库错误，只能报未分类的
    500；在校验阶段查出来则可以报一条可读的 400。
    """
    await _ensure_checkout_state_schema(conn)

    # ---- 校验阶段：只读、只算，不写库。任何一项失败此时都还没写过一行。----
    normalized_term_types: list[tuple[str, list[dict], str]] = []
    declared_types: set[str] = set()
    for term_type in term_types:
        value = term_type["value"]
        if value in declared_types:
            raise ValueError(f"提交里有重复的实体类型: {value!r}")
        declared_types.add(value)
        # term_type 的 value（分类名字）本身没有格式校验——真实的
        # create_term_type 也不校验它，是自由文本（跟关系类型名/字段名不同，
        # 后两者要落进 Cypher 拼接或结构化查询字段名，前者只是显示用的分类
        # 标签），这里如实保持一致，不额外发明一条真实实现里不存在的规则。
        extra_fields = _validate_draft_extra_fields(term_type.get("extra_fields", []))
        standard_name_value_type = term_type.get("standard_name_value_type", "string")
        _validate_draft_standard_name_value_type(standard_name_value_type)
        normalized_term_types.append((value, extra_fields, standard_name_value_type))

    normalized_relation_types: list[tuple[str, str, str, bool]] = []
    declared_relations: set[str] = set()
    for relation_type in relation_types:
        relation_type_name = relation_type["relation_type"]
        if relation_type_name in declared_relations:
            raise ValueError(f"提交里有重复的关系类型: {relation_type_name!r}")
        declared_relations.add(relation_type_name)
        example_phrase = relation_type.get("example_phrase", "")
        _validate_draft_relation_type(relation_type_name, example_phrase)
        normalized_relation_types.append(
            (
                relation_type_name,
                example_phrase,
                relation_type.get("description", ""),
                relation_type.get("allow_chain_query", True),
            )
        )

    normalized_constraints: list[tuple[str, str, str]] = []
    declared_constraint_triples: set[tuple[str, str, str]] = set()
    for constraint in constraints:
        # 引用检查放在这里而不是靠外键：SQLite 默认不强制外键，靠它等于
        # 没检查。引用不存在的类型会让 ETL 在跑批时才炸，那时已经晚了。
        for key, pool, label in (
            ("subject_term_type", declared_types, "实体类型"),
            ("object_term_type", declared_types, "实体类型"),
            ("relation_type", declared_relations, "关系类型"),
        ):
            if constraint[key] not in pool:
                raise UnknownCategoryError(
                    f"约束引用了未声明的{label}：{constraint[key]}"
                )
        triple = (
            constraint["subject_term_type"],
            constraint["relation_type"],
            constraint["object_term_type"],
        )
        if triple in declared_constraint_triples:
            raise ValueError(f"提交里有重复的约束: {triple}")
        declared_constraint_triples.add(triple)
        normalized_constraints.append(triple)

    # ---- 写入阶段：校验已经全部通过，这里的每条语句都不会再因为业务规则失败。----
    for table in (
        "ontology_term_types",
        "tenant_relation_types",
        "term_type_relation_allowlist",
    ):
        await conn.execute(
            f"DELETE FROM {table} WHERE tenant_id = ? AND status = 'draft'", (tenant_id,)
        )

    for value, extra_fields, standard_name_value_type in normalized_term_types:
        await conn.execute(
            "INSERT INTO ontology_term_types"
            " (tenant_id, value, extra_fields, standard_name_value_type, status)"
            " VALUES (?, ?, ?, ?, 'draft')",
            (
                tenant_id,
                value,
                json.dumps(extra_fields, ensure_ascii=False),
                standard_name_value_type,
            ),
        )

    for relation_type_name, example_phrase, description, allow_chain_query in normalized_relation_types:
        await conn.execute(
            "INSERT INTO tenant_relation_types"
            " (tenant_id, relation_type, example_phrase, description, allow_chain_query,"
            "  source, status) VALUES (?, ?, ?, ?, ?, 'custom', 'draft')",
            (
                tenant_id,
                relation_type_name,
                example_phrase,
                description,
                1 if allow_chain_query else 0,
            ),
        )

    for subject_term_type, relation_type_name, object_term_type in normalized_constraints:
        await conn.execute(
            "INSERT INTO term_type_relation_allowlist"
            " (tenant_id, subject_term_type, relation_type, object_term_type, status)"
            " VALUES (?, ?, ?, ?, 'draft')",
            (tenant_id, subject_term_type, relation_type_name, object_term_type),
        )

    # 写过草稿就意味着已检出。不标记的话，下一次 checkout_draft 会以为
    # "还没检出过"，把已确认版本复制回来盖在引导刚写的草稿上。
    # 映射跟本体草稿同一次提交落库。为 None 时**不动**已有映射——本体结构页
    # 那三个 tab 改草稿时不带映射，不能因此把引导写的映射抹掉。
    if etl_mapping is not None:
        await set_draft_etl_mapping(
            conn,
            tenant_id,
            config_yaml=etl_mapping["config_yaml"],
            source_file_name=etl_mapping["source_file_name"],
            created_at=datetime.now().isoformat(),
            # 不在这里提交：本函数只在末尾提交一次（见上面 docstring 里
            # 关于单例连接的那段论证）。
            commit=False,
        )

    await conn.execute(
        "INSERT OR IGNORE INTO ontology_draft_checkout_state (tenant_id) VALUES (?)",
        (tenant_id,),
    )
    await conn.commit()
