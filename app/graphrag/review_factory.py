from __future__ import annotations

from pathlib import Path

import aiosqlite

from app.config.settings import Settings
from app.graphrag.ontology_lifecycle import ensure_ontology_schema
from app.graphrag.review_queue import ensure_review_schema
from app.graphrag.terms_store import ensure_terms_schema


async def build_review_conn_from_settings(settings: Settings) -> aiosqlite.Connection:
    db_path = Path(settings.graph_review_db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = await aiosqlite.connect(str(db_path))
    try:
        await ensure_review_schema(conn)
        await ensure_terms_schema(conn, seed_yaml_path=Path(settings.terminology_path))
        # 同一个连接生命周期同一份缺口修复，见 app/api/deps.py::get_review_conn
        # 里的同款说明——ingestion/CLI 工具走的是这个工厂函数，不是 deps.py，
        # 两条路径都要建齐 tenant_relation_types/term_type_relation_allowlist，
        # 否则任何走这条路径拿到连接的调用方在触达 Task 7 新增的路由/store 函数
        # 时会遇到同样的 "no such table"。
        await ensure_ontology_schema(conn)
    except Exception:
        await conn.close()
        raise
    return conn
