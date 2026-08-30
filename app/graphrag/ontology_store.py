"""本体库（ontology store）的连接入口——见 CONTEXT.md 的同名词条。

这个 module 只做一件事：打开本体库并建齐它的全部 schema。刻意留在外面的
两件事各有理由：

- **进程内单例**：缓不缓存是调用方的策略。app/api/deps.py 用双检锁把它包成
  请求级依赖（与 get_memory_conn/get_ingestion_conn 保持同一个 pattern），
  CLI 则每次开新连接、自己负责关闭——这两种生命周期正好相反，焊进同一个
  interface 会让"要不要关"变成调用方必须知道的隐含知识。
- **跨库的租户注册表回填**（tenants_store.ensure_tenants_schema）：它要同时
  读本体库和 ingestion 库两个 SQLite 文件才能发现历史 tenant_id，是一次性
  迁移而不是"取连接"的属性。放进来会把第二个数据库拖进这个 module 的
  interface，而绝大多数调用方根本不关心 ingestion 库。它现在由
  app/main.py 的 lifespan 在启动时跑一次；本 module 只用 create_tenants_table
  保证表存在，不做回填。

为什么要有这个 module：在它之前，"打开本体库并建齐表"有两份独立实现——
deps.py::get_review_conn 建 7 类 schema，review_factory 只建 3 类。两份清单
手工维护，已经分叉，而且没有任何测试覆盖生产的建表路径（走 API 的测试一律
用 dependency_overrides 替换掉真实实现），所以分叉只能在生产以
"no such table" 的形式暴露。app/graphrag/duplicate_detection_worker.py 需要
duplicate_review_queue 和 tenants 两张表，工厂两张都不建，它因此只能反向
import app.api.deps——这是全项目唯一一条 graphrag → api 的依赖边。
"""
from __future__ import annotations

from pathlib import Path

import aiosqlite

from app.config.settings import Settings
from app.graphrag.duplicate_review_queue import ensure_duplicate_review_schema
from app.graphrag.etl_runs_store import ensure_etl_runs_schema
from app.graphrag.ontology_lifecycle import ensure_ontology_schema
from app.graphrag.review_queue import ensure_review_schema
from app.graphrag.tenants_store import create_tenants_table
from app.graphrag.terms_store import ensure_terms_schema


async def open_ontology_store_conn(settings: Settings) -> aiosqlite.Connection:
    """打开本体库连接，返回时它的全部 schema 都已建好。

    调用方拥有这个连接的生命周期，用完负责关闭。全部建表步骤都是
    CREATE TABLE IF NOT EXISTS 语义，重复调用幂等。

    建表过程中任何一步失败都会先关掉这个连接再把异常抛出去——半建好的
    连接不能交出去，否则调用方拿到的是一个"看起来能用、碰到某张表就报
    no such table"的连接。
    """
    db_path = Path(settings.graph_review_db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = await aiosqlite.connect(str(db_path))
    try:
        await ensure_review_schema(conn)
        await ensure_duplicate_review_schema(conn)
        await ensure_terms_schema(conn, seed_yaml_path=Path(settings.terminology_path))
        # 一个入口建齐分类/关系类型/约束/接入模式/草稿检出状态五张表。
        await ensure_ontology_schema(conn)
        await ensure_etl_runs_schema(conn)
        # 只建表，不回填——回填要跨 ingestion 库，见模块 docstring。
        await create_tenants_table(conn)
    except Exception:
        await conn.close()
        raise
    return conn
