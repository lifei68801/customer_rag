from __future__ import annotations

import asyncio
import logging
from functools import lru_cache

from fastapi import Depends, Header, HTTPException

from app.config.settings import Settings
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
from app.graphrag.factory import build_graph_client_from_settings, load_terms_from_settings
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
    "get_asr_provider",
    "get_bm25_index",
    "get_embedding_registry",
    "get_gateway_tenant_id",
    "get_graph_client",
    "get_llm_registry",
    "get_memory_conn",
    "get_rerank_provider",
    "get_settings",
    "get_terms",
    "get_tts_provider",
    "get_vector_store",
    "resolve_tenant_id",
]

_bm25_index_cache: BM25Index | None = None
_bm25_index_lock = asyncio.Lock()
_graph_client_cache: Neo4jGraphClient | None = None
_graph_client_lock = asyncio.Lock()
_terms_cache: list[Term] | None = None
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


async def get_graph_client(
    settings: Settings = Depends(get_settings),
) -> Neo4jGraphClient:
    """进程内单例，避免每次请求都新建一个 Neo4j 驱动连接池。"""
    global _graph_client_cache
    if _graph_client_cache is None:
        async with _graph_client_lock:
            if _graph_client_cache is None:
                _graph_client_cache = build_graph_client_from_settings(settings)
    return _graph_client_cache


def get_terms(settings: Settings = Depends(get_settings)) -> list[Term]:
    """进程内单例：术语表文件在服务启动期间视为不变，避免逐请求重新解析。"""
    global _terms_cache
    if _terms_cache is None:
        _terms_cache = load_terms_from_settings(settings)
    return _terms_cache


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
