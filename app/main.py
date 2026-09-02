import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import APIRouter, Depends, FastAPI

from app.api import deps
from app.api.admin_account_routes import router as admin_account_router
from app.api.admin_auth_routes import router as admin_auth_router
from app.api.admin_document_routes import router as admin_document_router
from app.api.admin_duplicate_review_routes import router as admin_duplicate_review_router
from app.api.admin_graph_review_routes import router as admin_graph_review_router
from app.api.admin_diagnostics_routes import router as admin_diagnostics_router
from app.api.admin_nav_badges_routes import router as admin_nav_badges_router
from app.api.admin_ontology_routes import router as admin_ontology_router
from app.api.admin_schema_etl_routes import router as admin_schema_etl_router
from app.api.admin_tenant_routes import router as admin_tenant_router
from app.api.admin_terms_routes import router as admin_terms_router
from app.api.agent_routes import router as agent_router
from app.api.qa_routes import router as qa_router
from app.api.session_routes import router as session_router
from app.api.voice_routes import router as voice_router
from app.config.settings import Settings
from app.graphrag.tenants_store import ensure_tenants_schema

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """启动时检查 gateway_shared_secret 是否配置，未配置只告警不阻止启动。

    未配置时 get_gateway_tenant_id 会静默放行、resolve_tenant_id 降级信任
    客户端自报的 tenant_id（这是本计划刻意设计的本地开发兜底路径，见
    docs/superpowers/specs/2026-08-06-gateway-tenant-auth-design.md）。问题
    在于这个降级路径本身几乎没有存在感：唯一的信号是请求级别的
    logger.warning，很容易淹没在日常日志噪音里，运营者上线网关时如果忘了
    在应用侧同步配置这个密钥，多租户隔离会在没有任何明显报错的情况下形同
    虚设。这里在启动时只打印一次醒目的警告，不阻断启动——阻断会破坏本计划
    自己设计的“未配置=本地开发兜底”这条路径，不能因为加了这个检查就让不
    配网关的本地开发环境无法启动。
    """
    settings = Settings()
    if not settings.gateway.shared_secret:
        logger.warning(
            "gateway_shared_secret 未配置：当前应用信任客户端自报的 "
            "tenant_id，任何调用方都可以伪造租户身份绕过多租户隔离。"
            "生产环境多租户部署必须配置 CUSTOMER_RAG_GATEWAY_SHARED_SECRET，"
            "否则这条安全修复不会实际生效。"
        )

    # 预热向量库连接 + BM25 索引：get_bm25_index（app/api/deps.py）是进程内
    # 单例，首次调用时才会同步全量扫描向量库重建索引——不预热的话这笔耗时
    # （叠加 Milvus 刚启动/集合刚恢复时的额外延迟）会摊在启动后第一个真实
    # 用户请求上，表现为"偶尔某一次问答格外慢"。这里在启动阶段提前把它
    # 跑一遍，后续请求的同一个 Depends(get_bm25_index) 会直接命中缓存。
    # 失败（比如 Milvus 还没就绪）只告警不阻断启动——不能让向量库暂时不可用
    # 拖垮整个应用的启动，后续请求仍会按原有的"首次访问时重建"逻辑兜底。
    try:
        vector_store = deps.get_vector_store(settings)
        await deps.get_bm25_index(vector_store)
        logger.info("BM25 索引预热完成")
    except Exception:
        logger.warning("启动预热 BM25 索引失败，将在首个请求时重试", exc_info=True)

    # 租户注册表的存量回填：要同时读本体库和 ingestion 库两个 SQLite 文件才能
    # 发现历史 tenant_id（见 tenants_store.py::_discover_historical_tenant_ids），
    # 所以它不属于"取一个本体库连接"这件事，不能焊进 open_ontology_store_conn
    # ——那会把第二个数据库拖进那个 module 的 interface，而它的绝大多数调用方
    # （CLI、ingestion、eval）根本不碰 ingestion 库。它本质是一次性迁移，放在
    # 启动阶段跑一次是它真正的位置。全程幂等，重复启动不会覆盖已有注册记录。
    #
    # 失败只告警不阻断，跟上面的 BM25 预热同一处理方式：注册表回填是存量数据
    # 的补齐，不是请求路径的前提，不该让它把整个应用的启动拖垮。
    try:
        review_conn = await deps.get_review_conn(settings)
        ingestion_conn = await deps.get_ingestion_conn(settings)
        await ensure_tenants_schema(review_conn, ingestion_conn)
        logger.info("租户注册表回填完成")
    except Exception:
        logger.warning("启动回填租户注册表失败，租户列表可能不完整", exc_info=True)

    # 工具注册表必须在启动阶段构建成功——manifest 格式错误/tool.py 缺 TOOL
    # 导出/工具名重复这三类问题，按插件化计划的 Global Constraints 要求
    # 必须让进程直接启动失败，不允许拖到第一个请求才暴露（get_tool_registry
    # 是懒加载单例，不在这里主动预热的话，一个损坏的工具目录会让进程正常
    # 启动、然后在每一次 /agent/chat 请求上都重新扫描、重新失败）。跟上面
    # BM25 预热不同，这里不吞异常——manifest/工具目录损坏是需要立刻发现
    # 并修复的部署错误，不是"暂时不可用、稍后自动恢复"的瞬时故障。
    await deps.get_tool_registry()

    yield


app = FastAPI(lifespan=lifespan)
app.include_router(qa_router)
app.include_router(agent_router)
app.include_router(session_router)
app.include_router(voice_router)

# 不属于任何租户：登录、租户管理。给它们挂租户校验会让 FastAPI 把
# tenant_id 当成必填查询参数，登录接口直接 422——那时谁也进不来。
#
# 租户管理也在这里，尽管 /api/admin/tenants/{tenant_id}/disable 路径里有
# tenant_id：那是**被操作的对象**（停用哪个租户），不是**操作发生的作用
# 域**。按租户作用域校验的话，member 对自己所属的租户会顺利通过，于是就
# 能把自己所在的租户停掉。它靠 require_admin_role 保护。
app.include_router(admin_auth_router)
app.include_router(admin_account_router)
app.include_router(admin_tenant_router)

# 租户作用域的路由统一收在这个父 router 下，而不是各挂各的依赖。各挂各的
# 一定会漏，而漏掉的那条是越权读写，且不会有任何报错——请求照常 200，只是
# 返回的是别人租户的数据。tests/api/test_admin_route_shapes.py 里的结构测试
# 兜住新增路由忘记归类的情况。
tenant_scoped = APIRouter(dependencies=[Depends(deps.require_tenant_access)])
tenant_scoped.include_router(admin_document_router)
tenant_scoped.include_router(admin_graph_review_router)
tenant_scoped.include_router(admin_duplicate_review_router)
tenant_scoped.include_router(admin_nav_badges_router)
tenant_scoped.include_router(admin_diagnostics_router)
tenant_scoped.include_router(admin_ontology_router)
tenant_scoped.include_router(admin_terms_router)
tenant_scoped.include_router(admin_schema_etl_router)
app.include_router(tenant_scoped)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
