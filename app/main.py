import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI

from app.api.admin_auth_routes import router as admin_auth_router
from app.api.agent_routes import router as agent_router
from app.api.qa_routes import router as qa_router
from app.api.voice_routes import router as voice_router
from app.config.settings import Settings

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
    if not settings.gateway_shared_secret:
        logger.warning(
            "gateway_shared_secret 未配置：当前应用信任客户端自报的 "
            "tenant_id，任何调用方都可以伪造租户身份绕过多租户隔离。"
            "生产环境多租户部署必须配置 CUSTOMER_RAG_GATEWAY_SHARED_SECRET，"
            "否则这条安全修复不会实际生效。"
        )
    yield


app = FastAPI(lifespan=lifespan)
app.include_router(qa_router)
app.include_router(agent_router)
app.include_router(voice_router)
app.include_router(admin_auth_router)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
