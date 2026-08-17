from __future__ import annotations

import asyncio
import logging
from functools import lru_cache
from pathlib import Path

from fastapi import Depends, Header, HTTPException

from app.api.admin_session import AdminSessionStore
from app.config.settings import Settings
from app.ingestion.ingestion_queue import ensure_ingestion_queue_schema
from app.ingestion.ocr_factory import build_ocr_from_settings
from app.ingestion.ocr_parser import OcrFunction
from app.ingestion.table_extraction import TableExtractionFunction
from app.ingestion.table_extraction_factory import build_table_extractor_from_settings
from app.ingestion.tracking import ensure_tracking_schema
from app.graphrag.ontology_categories import TermTypeCategory, list_term_types
from app.graphrag.ontology_lifecycle import ensure_ontology_schema
from app.graphrag.ontology_relations import list_relation_types
from app.graphrag.review_queue import ensure_review_schema
from app.graphrag.terms_store import ensure_terms_schema, list_terms
from app.graphrag.etl_runs_store import ensure_etl_runs_schema
from app.providers.embedding import EmbeddingRegistry
from app.providers.factory import (
    DEFAULT_EMBEDDING_PROVIDER_NAME,
    DEFAULT_LLM_PROVIDER_NAME,
    build_embedding_registry_from_settings,
    build_llm_registry_from_settings,
)
from app.providers.registry import ProviderRegistry
from app.providers.rerank import RerankProvider
from app.providers.rerank_factory import build_rerank_provider_from_settings
from app.retrieval.bm25 import BM25Index, build_bm25_index_from_store
from app.retrieval.factory import build_vector_store_from_settings
from app.retrieval.vector_store import VectorStore
from app.graphrag.factory import build_graph_client_from_settings
from app.graphrag.neo4j_client import Neo4jGraphClient
from app.graphrag.ontology import Term
from app.memory.factory import build_memory_conn_from_settings
from app.providers.asr import ASRProvider
from app.providers.tts import TTSProvider
from app.providers.voice_factory import (
    build_asr_provider_from_settings,
    build_tts_provider_from_settings,
)

import aiosqlite

__all__ = [
    "DEFAULT_EMBEDDING_PROVIDER_NAME",
    "DEFAULT_LLM_PROVIDER_NAME",
    "get_admin_session_store",
    "get_asr_provider",
    "get_bm25_index",
    "get_confirmed_relation_types",
    "get_embedding_registry",
    "get_gateway_tenant_id",
    "get_graph_client",
    "get_ingestion_conn",
    "get_llm_registry",
    "get_memory_conn",
    "get_ocr_function",
    "get_rerank_provider",
    "get_review_conn",
    "get_settings",
    "get_table_extractor",
    "get_term_type_schema",
    "get_terms",
    "get_tts_provider",
    "get_upload_dir",
    "get_vector_store",
    "parse_banned_terms",
    "require_admin_session",
    "resolve_tenant_id",
]

_bm25_index_cache: BM25Index | None = None
_bm25_index_lock = asyncio.Lock()
_graph_client_cache: Neo4jGraphClient | None = None
_graph_client_lock = asyncio.Lock()
_memory_conn_cache: aiosqlite.Connection | None = None
_memory_conn_lock = asyncio.Lock()

logger = logging.getLogger(__name__)


@lru_cache
def get_settings() -> Settings:
    return Settings()


async def get_gateway_tenant_id(
    x_tenant_id: str | None = Header(default=None),
    x_gateway_secret: str | None = Header(default=None),
    settings: Settings = Depends(get_settings),
) -> str | None:
    """校验请求是否真的经过了网关，而不是被绕过网关直接访问。

    settings.gateway_shared_secret 未配置时（本地开发默认）直接放行返回
    None，交给调用方走 resolve_tenant_id() 的请求体/query 兜底路径；一旦
    配置了密钥，缺失或错误的 X-Gateway-Secret 直接 401 拒绝，绝不允许
    静默降级到不受保护的旧路径——否则攻击者只要不带这个头就能绕过校验，
    密钥形同虚设。
    """
    if not settings.gateway_shared_secret:
        return None
    if x_gateway_secret != settings.gateway_shared_secret:
        raise HTTPException(status_code=401, detail="缺少有效的网关凭证")
    if not x_tenant_id:
        raise HTTPException(status_code=401, detail="网关未声明租户身份")
    return x_tenant_id


def resolve_tenant_id(
    gateway_tenant_id: str | None,
    fallback_tenant_id: str | None,
    *,
    source: str,
) -> str:
    """合并网关声明的租户身份与请求体/query 里的兜底值。

    网关值优先且视为可信；网关未启用鉴权（get_gateway_tenant_id 返回
    None）时才会用到 fallback_tenant_id，此时打印警告日志，提醒这是本地
    开发的降级路径，生产环境不应该出现。两者都缺失时视为客户端请求缺少
    必要参数，返回 422。
    """
    if gateway_tenant_id is not None:
        return gateway_tenant_id
    if fallback_tenant_id:
        logger.warning(
            "%s: 网关鉴权未启用（gateway_shared_secret 未配置），降级信任"
            "客户端自报的 tenant_id=%s，生产环境不应出现此日志",
            source,
            fallback_tenant_id,
        )
        return fallback_tenant_id
    raise HTTPException(status_code=422, detail="缺少 tenant_id")


def parse_banned_terms(raw: str | None) -> list[str] | None:
    """把 Settings.banned_terms 的逗号分隔字符串解析成列表。

    留空返回 None（check_text() 的 banned_terms=None 等价于不启用自定义
    敏感词检测，只有内置正则生效）；每个词两端的空白会被去掉，方便配置
    时随意加空格。
    """
    if not raw:
        return None
    return [term.strip() for term in raw.split(",") if term.strip()]


def get_embedding_registry(
    settings: Settings = Depends(get_settings),
) -> EmbeddingRegistry:
    return build_embedding_registry_from_settings(settings)


def get_llm_registry(
    settings: Settings = Depends(get_settings),
) -> ProviderRegistry:
    return build_llm_registry_from_settings(settings)


def get_vector_store(
    settings: Settings = Depends(get_settings),
) -> VectorStore:
    return build_vector_store_from_settings(settings)


async def get_bm25_index(
    vector_store: VectorStore = Depends(get_vector_store),
) -> BM25Index:
    """进程内单例：首次请求时从向量库全量重建，此后复用，避免逐请求全量扫描。

    这意味着摄取新文档后，运行中的 API 进程不会实时感知到——需要重启服务
    才能让新文档进入 BM25 检索范围，这是当前 MVP 阶段接受的已知折衷。
    """
    global _bm25_index_cache
    if _bm25_index_cache is None:
        async with _bm25_index_lock:
            if _bm25_index_cache is None:
                _bm25_index_cache = await build_bm25_index_from_store(
                    vector_store
                )
    return _bm25_index_cache


def get_rerank_provider(
    settings: Settings = Depends(get_settings),
) -> RerankProvider | None:
    return build_rerank_provider_from_settings(settings)


def get_ocr_function(
    settings: Settings = Depends(get_settings),
) -> OcrFunction | None:
    return build_ocr_from_settings(settings)


def get_table_extractor(
    settings: Settings = Depends(get_settings),
) -> TableExtractionFunction | None:
    return build_table_extractor_from_settings(settings)


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


async def get_memory_conn(
    settings: Settings = Depends(get_settings),
) -> aiosqlite.Connection:
    """进程内单例 SQLite 连接，避免每次请求都新建连接。"""
    global _memory_conn_cache
    if _memory_conn_cache is None:
        async with _memory_conn_lock:
            if _memory_conn_cache is None:
                _memory_conn_cache = await build_memory_conn_from_settings(settings)
    return _memory_conn_cache


def get_asr_provider(
    settings: Settings = Depends(get_settings),
) -> ASRProvider | None:
    return build_asr_provider_from_settings(settings)


def get_tts_provider(
    settings: Settings = Depends(get_settings),
) -> TTSProvider | None:
    return build_tts_provider_from_settings(settings)


_admin_session_store_cache: AdminSessionStore | None = None


def get_admin_session_store() -> AdminSessionStore:
    """进程内单例：所有管理员 session 共用同一份内存存储。"""
    global _admin_session_store_cache
    if _admin_session_store_cache is None:
        _admin_session_store_cache = AdminSessionStore()
    return _admin_session_store_cache


async def require_admin_session(
    authorization: str | None = Header(default=None),
    session_store: AdminSessionStore = Depends(get_admin_session_store),
) -> None:
    """校验 Authorization: Bearer <token> 是否是有效的管理员 session。

    所有 /api/admin/* 路由（登录接口本身除外）都应该依赖这个函数。
    """
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="缺少管理员登录凭证")
    token = authorization.removeprefix("Bearer ")
    if not session_store.verify_session(token):
        raise HTTPException(status_code=401, detail="登录已过期，请重新登录")


_ingestion_conn_cache: aiosqlite.Connection | None = None
_ingestion_conn_lock = asyncio.Lock()


async def get_ingestion_conn(
    settings: Settings = Depends(get_settings),
) -> aiosqlite.Connection:
    """进程内单例 SQLite 连接，模式同 get_memory_conn。"""
    global _ingestion_conn_cache
    if _ingestion_conn_cache is None:
        async with _ingestion_conn_lock:
            if _ingestion_conn_cache is None:
                db_path = Path(settings.ingestion_db_path)
                db_path.parent.mkdir(parents=True, exist_ok=True)
                conn = await aiosqlite.connect(str(db_path))
                await ensure_tracking_schema(conn)
                await ensure_ingestion_queue_schema(conn)
                _ingestion_conn_cache = conn
    return _ingestion_conn_cache


def get_upload_dir(settings: Settings = Depends(get_settings)) -> Path:
    upload_dir = Path(settings.upload_dir)
    upload_dir.mkdir(parents=True, exist_ok=True)
    return upload_dir


_review_conn_cache: aiosqlite.Connection | None = None
_review_conn_lock = asyncio.Lock()


async def get_review_conn(
    settings: Settings = Depends(get_settings),
) -> aiosqlite.Connection:
    """进程内单例 SQLite 连接，模式同 get_memory_conn。"""
    global _review_conn_cache
    if _review_conn_cache is None:
        async with _review_conn_lock:
            if _review_conn_cache is None:
                db_path = Path(settings.graph_review_db_path)
                db_path.parent.mkdir(parents=True, exist_ok=True)
                conn = await aiosqlite.connect(str(db_path))
                try:
                    await ensure_review_schema(conn)
                    await ensure_terms_schema(
                        conn, seed_yaml_path=Path(settings.terminology_path)
                    )
                    # Task 4 的统一本体建表入口——建 tenant_relation_types/
                    # term_type_relation_allowlist 两张表，Task 7 新增的关系
                    # 类型/约束/生命周期路由都直接查询这两张表。漏掉这一步的话
                    # 这些路由在真实环境下第一次被访问就会因为 "no such table"
                    # 报 500——ensure_categories_schema 部分和 ensure_terms_schema
                    # 里已经建过的分类表重复，但都是幂等的 CREATE TABLE IF NOT
                    # EXISTS，不会冲突。
                    await ensure_ontology_schema(conn)
                    await ensure_etl_runs_schema(conn)
                except Exception:
                    await conn.close()
                    raise
                _review_conn_cache = conn
    return _review_conn_cache


async def get_terms(
    review_conn: aiosqlite.Connection = Depends(get_review_conn),
    gateway_tenant_id: str | None = Depends(get_gateway_tenant_id),
) -> list[Term]:
    """每次请求都查 terms 表，不再进程级缓存（原因见函数改造前的说明，
    未变）。tenant_id 优先取网关鉴权声明的租户身份（生产环境应始终配置
    gateway_shared_secret，见 get_gateway_tenant_id）；网关鉴权未启用
    （本地开发降级路径）时回退到 "default" 租户——与本计划"存量/未配置
    数据统一归属 tenant_id='default'"的约定一致。get_terms 是横跨 6 个
    结构不同路由（admin_document_routes.py/admin_graph_review_routes.py/
    agent_routes.py/qa_routes.py/voice_routes.py）的共享依赖，各路由自己
    解析 tenant_id 的方式互不相同（Form 字段/请求体字段/query 兜底/完全
    不解析），没有统一的"当前路由级 fallback"可读，因此不复用
    resolve_tenant_id() 的双源合并逻辑，只走网关这一个可信来源 + 固定
    默认值，不引入 422。
    """
    tenant_id = gateway_tenant_id or "default"
    return await list_terms(review_conn, tenant_id)


async def get_confirmed_relation_types(
    review_conn: aiosqlite.Connection = Depends(get_review_conn),
    gateway_tenant_id: str | None = Depends(get_gateway_tenant_id),
) -> set[str]:
    """结构化过滤查询工具校验 relation_type 用——跟 get_terms 一样，每次请求查一次，
    不做进程级缓存（租户在管理后台改关系类型是随时可能发生的事，缓存会导致查询
    工具用旧 schema 拒绝新确认的关系类型）。tenant_id 解析方式与 get_terms 保持
    完全一致，见该函数的说明。"""
    tenant_id = gateway_tenant_id or "default"
    defs = await list_relation_types(review_conn, tenant_id, status="confirmed")
    return {d.relation_type for d in defs}


async def get_term_type_schema(
    review_conn: aiosqlite.Connection = Depends(get_review_conn),
    gateway_tenant_id: str | None = Depends(get_gateway_tenant_id),
) -> dict[str, TermTypeCategory]:
    """结构化过滤查询工具校验 anchor_term_type/target_term_type/field 用。"""
    tenant_id = gateway_tenant_id or "default"
    categories = await list_term_types(review_conn, tenant_id)
    return {c.value: c for c in categories}
