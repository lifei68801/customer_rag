import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import APIRouter, Depends, FastAPI

from app.api import deps
from app.auth.admin_users_store import ensure_admin_users_schema
from app.auth.bootstrap import disable_stale_test_tenants, seed_admin_user
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

    这条警告的适用范围在“前台与管理后台共用一套会话”那次改造之后缩小了，
    警告文案也随之改窄，这里记下现在的实际情况（已逐行核实）：

    - `/qa`、`/agent/chat` 与三个 `/agent/sessions` 的租户**一律取自会话**
      （`deps.require_chat_session`），请求体和 `X-Tenant-Id` 里的租户都被
      忽略。这三个 router 上仍挂着 `get_gateway_tenant_id`，但它的返回值不
      再有人接，`resolve_tenant_id` 在这条路径上已无调用方——网关头只携带
      租户、不携带用户身份，而这五个接口都需要 user_id，回落时无处可取。
      失效方向是 fail-closed：没有会话就是 401，不存在“只带 X-Tenant-Id
      就进得去”这种绕过。
    - `/api/admin/*` 从来就按会话取租户。
    - 真正还在信任客户端自报 tenant_id 的只剩 `/voice/*`（见
      app/api/voice_routes.py，它今天仍是匿名入口）。

    **配置了这个密钥之后有一个很难排查的故障形态**：`get_gateway_tenant_id`
    会要求上面那三个前台 router 的**每一个请求**都带对 `X-Gateway-Secret`，
    而浏览器自己发不出这个头；`/api/admin/*` 没有这个依赖。于是流量若没有
    真的经过那台会注入该头的网关，表现就是“后台一切正常、前台五个接口全线
    401”——这个形态几乎不会让人想到网关密钥配置。

    启动时只打印一次醒目的警告、不阻断启动：阻断会破坏“未配置=本地开发
    兜底”这条路径，不能因为加了这个检查就让不配网关的本地开发环境起不来。
    """
    settings = Settings()
    if not settings.gateway.shared_secret:
        logger.warning(
            "gateway_shared_secret 未配置：/voice/* 仍然信任客户端自报的 "
            "tenant_id，任何调用方都可以伪造租户身份读取该租户的数据。"
            "（问答与管理后台的租户已改为一律取自登录会话，不受此项影响。）"
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

    # 账号体系的引导。与 BM25 预热/租户回填不同，这里**不吞异常**——没有
    # 管理员账号意味着后台完全不可用，是需要立刻发现并修复的部署错误，不是
    # "暂时不可用、稍后自动恢复"的瞬时故障。处理方式同下面的工具注册表。
    admin_conn = await deps.get_review_conn(settings)
    await ensure_admin_users_schema(admin_conn)
    await seed_admin_user(admin_conn, settings.admin_token)
    await disable_stale_test_tenants(admin_conn)

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
#
# 这三个连同下面的 tenant_scoped 一起收在 admin_scoped 下，为的是让 CSRF
# 校验有且只有一个挂载点：会话改用 Cookie 之后，浏览器会给每个同源请求
# 自动附带凭证，于是每一个 /api/admin/* 的写接口都进入了 CSRF 的射程。
# 各挂各的一定会漏，而漏掉的那条不会有任何报错——它只是一条活着的 CSRF
# 通道。tests/api/test_admin_route_shapes.py::
# test_every_admin_write_route_checks_csrf 从路由表这一侧兜住新增 router
# 忘记挂进来的情况。
admin_scoped = APIRouter(dependencies=[Depends(deps.require_csrf)])
admin_scoped.include_router(admin_auth_router)
admin_scoped.include_router(admin_account_router)
admin_scoped.include_router(admin_tenant_router)

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
admin_scoped.include_router(tenant_scoped)

app.include_router(admin_scoped)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
