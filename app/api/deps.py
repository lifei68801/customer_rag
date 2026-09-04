from __future__ import annotations

import asyncio
import logging
from functools import lru_cache
from pathlib import Path

from fastapi import Depends, Header, HTTPException, Request

from app.agent.tool_registry import ToolRegistry, discover_tools
from app.api.admin_session import AdminSession, AdminSessionStore
from app.api.session_cookie import (
    CSRF_COOKIE_NAME,
    CSRF_HEADER_NAME,
    SESSION_COOKIE_NAME,
)
from app.auth.admin_users_store import get_admin_user
from app.auth.login_throttle import LoginThrottle
from app.config.settings import Settings
from app.ingestion.ingestion_queue import ensure_ingestion_queue_schema
from app.ingestion.ocr_factory import build_ocr_from_settings
from app.ingestion.ocr_parser import OcrFunction
from app.ingestion.table_extraction import TableExtractionFunction
from app.ingestion.table_extraction_factory import build_table_extractor_from_settings
from app.ingestion.tracking import ensure_tracking_schema
from app.graphrag.ontology_store import open_ontology_store_conn
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
from app.graphrag.neptune_client import NeptuneGraphClient
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
    "get_embedding_registry",
    "get_gateway_tenant_id",
    "get_graph_client",
    "get_ingestion_conn",
    "get_llm_registry",
    "get_login_throttle",
    "get_memory_conn",
    "get_ocr_function",
    "get_rerank_provider",
    "get_review_conn",
    "get_settings",
    "get_table_extractor",
    "get_tool_registry",
    "get_tts_provider",
    "get_upload_dir",
    "get_vector_store",
    "parse_banned_terms",
    "AdminSession",
    "require_admin_role",
    "require_admin_session",
    "require_chat_session",
    "require_csrf",
    "require_tenant_access",
    "resolve_tenant_id",
]

_bm25_index_cache: BM25Index | None = None
_bm25_index_lock = asyncio.Lock()
_graph_client_cache: Neo4jGraphClient | NeptuneGraphClient | None = None
_graph_client_lock = asyncio.Lock()
_memory_conn_cache: aiosqlite.Connection | None = None
_memory_conn_lock = asyncio.Lock()
_tool_registry_cache: ToolRegistry | None = None
_tool_registry_lock = asyncio.Lock()

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

    settings.gateway.shared_secret 未配置时（本地开发默认）直接放行返回
    None，交给调用方走 resolve_tenant_id() 的请求体/query 兜底路径；一旦
    配置了密钥，缺失或错误的 X-Gateway-Secret 直接 401 拒绝，绝不允许
    静默降级到不受保护的旧路径——否则攻击者只要不带这个头就能绕过校验，
    密钥形同虚设。
    """
    if not settings.gateway.shared_secret:
        return None
    if x_gateway_secret != settings.gateway.shared_secret:
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
) -> Neo4jGraphClient | NeptuneGraphClient:
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


async def get_tool_registry() -> ToolRegistry:
    """进程内单例：启动时扫描 app/agent/tools/*/manifest.yaml 构建一次，
    此后复用——跟 get_bm25_index/get_graph_client 同一个双重检查锁定
    单例模式。这意味着新增/修改一个工具目录后，运行中的 API 进程不会
    实时感知到，需要重启服务才能生效，这跟 get_bm25_index 已经接受的
    折衷一致。"""
    global _tool_registry_cache
    if _tool_registry_cache is None:
        async with _tool_registry_lock:
            if _tool_registry_cache is None:
                tools_dir = Path(__file__).resolve().parent.parent / "agent" / "tools"
                _tool_registry_cache = discover_tools(tools_dir)
    return _tool_registry_cache


def get_asr_provider(
    settings: Settings = Depends(get_settings),
) -> ASRProvider | None:
    return build_asr_provider_from_settings(settings)


def get_tts_provider(
    settings: Settings = Depends(get_settings),
) -> TTSProvider | None:
    return build_tts_provider_from_settings(settings)


_admin_session_store_cache: AdminSessionStore | None = None


_login_throttle_cache: LoginThrottle | None = None


def get_login_throttle() -> LoginThrottle:
    """进程内单例：失败计数必须跨请求累积，每次新建等于没有限流。"""
    global _login_throttle_cache
    if _login_throttle_cache is None:
        _login_throttle_cache = LoginThrottle()
    return _login_throttle_cache


def get_admin_session_store() -> AdminSessionStore:
    """进程内单例：所有管理员 session 共用同一份内存存储。"""
    global _admin_session_store_cache
    if _admin_session_store_cache is None:
        _admin_session_store_cache = AdminSessionStore()
    return _admin_session_store_cache


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
                db_path = Path(settings.ingestion.db_path)
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
    """本体库的进程内单例连接，缓存模式同 get_memory_conn。

    这里只负责缓存。开库和建表由 app/graphrag/ontology_store.py 独家持有——
    在它之前这份建表清单在这里和 review_factory 各有一份手工维护的副本，
    两份已经分叉（这里 7 类、那边 3 类），而且生产路径没有任何测试覆盖
    （走 API 的测试一律用 dependency_overrides 把这个函数整个替换掉），
    分叉只能在生产以 "no such table" 的形式暴露。

    租户注册表的跨库回填不在这里做：它要同时读本体库和 ingestion 库才能
    发现历史 tenant_id，是一次性迁移而不是"取连接"的属性，现在由
    app/main.py 的 lifespan 在启动时跑一次。
    """
    global _review_conn_cache
    if _review_conn_cache is None:
        async with _review_conn_lock:
            if _review_conn_cache is None:
                _review_conn_cache = await open_ontology_store_conn(settings)
    return _review_conn_cache


async def require_admin_session(
    request: Request,
    authorization: str | None = Header(default=None),
    session_store: AdminSessionStore = Depends(get_admin_session_store),
    review_conn: aiosqlite.Connection = Depends(get_review_conn),
) -> AdminSession:
    """校验会话凭证（Cookie 优先，Bearer 兜底），返回这个 session 的身份。

    所有 /api/admin/* 路由（登录接口本身除外）都应该依赖这个函数。

    除了校验 session 本身，还要确认这个账号当前仍是 active——「禁用账号」
    必须立即生效，而不是等 session 自然过期（默认 8 小时，被禁的人在那期间
    还能继续动数据，而禁用的场景通常正是"这个人现在就不该再动了"）。

    代价是每个请求多一次 SQLite 查询。替代方案"禁用时主动撤销该用户的所有
    session"只对本进程内已知的 session 有效，多进程部署时另一个进程里的
    session 撤销不掉，会退化成静默失效。查库是唯一在各种部署形态下都成立
    的做法。
    """
    # 先 Cookie 后 Bearer：浏览器走 Cookie（前台与后台同源共享，这正是
    # 不用二次登录的机制）；Bearer 留给脚本与既有测试，两者并存。
    token = request.cookies.get(SESSION_COOKIE_NAME)
    if not token:
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(status_code=401, detail="缺少管理员登录凭证")
        token = authorization.removeprefix("Bearer ")
    session = session_store.get_session(token)
    if session is None:
        raise HTTPException(status_code=401, detail="登录已过期，请重新登录")
    user = await get_admin_user(review_conn, session.username)
    if user is None or user["status"] != "active":
        session_store.revoke_session(token)
        raise HTTPException(status_code=401, detail="账号已停用")
    return session


#: 登录接口豁免 CSRF 校验。
#:
#: 会话是进程内的，后端一重启浏览器里的会话 Cookie 就成了废值，而它是
#: HttpOnly 的、前端删不掉；登录请求本身也不带 X-CSRF-Token。所以把校验
#: 无差别压到登录接口上时，这个人会一直 403、直到 Cookie 自己过期。实测
#: 确认过（tests/api/test_admin_auth_routes.py::
#: test_login_is_not_blocked_by_a_stale_session_cookie_without_csrf_header
#: 在没有这份豁免时返回 403）。
#:
#: 豁免它也不放松什么：登录不使用请求里已有的那个会话，跨站伪造一次登录
#: 换不到攻击者想要的任何东西。
_CSRF_EXEMPT_PATHS = frozenset({"/api/admin/auth/login"})


async def require_csrf(request: Request) -> None:
    """双提交令牌校验，只作用于写方法。

    SameSite=Lax 已经挡掉绝大部分跨站写请求，这是第二道：Lax 对老浏览器
    不完全可靠。成本只有前端一个请求头加这里一次比对。

    只在有会话 Cookie 时校验——纯 Bearer 调用方（脚本、既有测试）不经过
    浏览器，不存在 CSRF 场景，要求它们带这个头只会平白打断。
    """
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return
    if request.url.path in _CSRF_EXEMPT_PATHS:
        return
    if SESSION_COOKIE_NAME not in request.cookies:
        return
    header_value = request.headers.get(CSRF_HEADER_NAME)
    cookie_value = request.cookies.get(CSRF_COOKIE_NAME)
    if not header_value or not cookie_value or header_value != cookie_value:
        raise HTTPException(status_code=403, detail="CSRF 校验失败")


async def require_chat_session(
    session: AdminSession = Depends(require_admin_session),
) -> tuple[str, str]:
    """前台问答的身份：返回 (tenant_id, user_id)。

    租户取 current_tenant_id 而不是 tenant_id——admin 的 tenant_id 恒为
    None，用它的话 admin 在前台根本问不了任何问题。member 两者恒等。

    user_id 取 username：会话历史此后按账号归属，不再是客户端自报的
    随机 UUID。既有的匿名会话因此变成孤儿，这是设计里明确接受的代价
    （见 spec 决定 2）。
    """
    if session.current_tenant_id is None:
        raise HTTPException(status_code=400, detail="请先选择一个租户")
    return session.current_tenant_id, session.username


async def require_tenant_access(
    tenant_id: str,
    session: AdminSession = Depends(require_admin_session),
) -> str:
    """校验登录者有权操作 URL 里的这个租户。

    admin（tenant_id 为 None）放行任意租户——它得能进入自己新建的租户，
    否则建完就管不了。member 只能操作自己那一个。

    这是整个账号体系唯一真正的安全边界。改造之前，任何登录者把请求里的
    tenant_id 换成别的值就能读写另一个租户，返回 200，没有日志也没有报错。

    注意它和 tenant_guard.require_active_tenant_or_404 是正交的两件事：
    那个管的是"这个租户还启用着吗"，这个管的是"你有没有资格碰它"。
    """
    if session.role == "admin":
        return tenant_id
    if session.tenant_id != tenant_id:
        logger.warning(
            "越权访问被拒：username=%s 属于 %s，试图访问 %s",
            session.username,
            session.tenant_id,
            tenant_id,
        )
        raise HTTPException(status_code=403, detail="无权访问该租户")
    return tenant_id


async def require_admin_role(
    session: AdminSession = Depends(require_admin_session),
) -> AdminSession:
    """只有 admin 能过。用在账号管理与租户管理上。

    租户管理（新建/停用租户）也归这里，不归 require_tenant_access：
    /api/admin/tenants/{tenant_id}/disable 路径里那个 tenant_id 是**被操作
    的对象**，不是**操作发生的作用域**。按租户作用域校验的话，member 对
    自己所属的租户会顺利通过，于是就能把自己所在的租户停掉。
    """
    if session.role != "admin":
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return session


# get_terms/get_confirmed_relation_types/get_term_type_schema（曾经横跨
# admin_document_routes.py/admin_graph_review_routes.py/agent_routes.py/
# qa_routes.py/voice_routes.py 的共享依赖）已在 2026-08-23 删除：它们各自
# 独立解析 tenant_id（网关值优先，网关未启用时硬编码回退到 "default"，
# 完全不看请求体/query 里客户端自报的 tenant_id），跟同一请求里
# resolve_tenant_id()（网关优先、网关未启用时退回客户端自报值、两者都
# 没有才报错）是两套不同的策略——网关未配置、请求体传的又不是 "default"
# 租户时，会悄悄用错误的（几乎为空的）"default" 租户术语表做实体消歧，
# 而实际的向量/图谱查询早已正确地用了请求体传入的租户。5 个调用方已
# 全部改为在各自路由体内、拿到 resolve_tenant_id() 算出的权威 tenant_id
# 之后，直接调用 list_terms()/list_relation_types()/list_term_types()——
# 这个模式此前已经在 admin_graph_review_routes.py/admin_document_routes.py/
# app/eval/runner.py 里验证过，见这三处调用点。
