"""启动时的一次性引导：播种 admin、清理测试残留租户。

两件事都幂等，每次启动都会跑。
"""
from __future__ import annotations

import logging

import aiosqlite

from app.auth.admin_users_store import count_active_admins, create_admin_user, get_admin_user
from app.graphrag.tenants_store import list_tenants, set_tenant_status

logger = logging.getLogger(__name__)

__all__ = [
    "STALE_TEST_TENANTS",
    "AdminSeedError",
    "seed_admin_user",
    "disable_stale_test_tenants",
]

#: 2026-08-18 的租户注册表回填从历史表里"发现"的测试残留。这些租户在
#: terms / ingested_documents / graph_review_queue / etl_runs 里均无任何
#: 记录，只在 tenant_relation_types 留有痕迹。挂在租户下拉框里会让第一次
#: 用的人以为它们是真的，把数据建到 t_verify 里去。
#:
#: 禁用可逆（管理后台有 enable 接口），不删除任何数据。
STALE_TEST_TENANTS = (
    "t_verify",
    "t_verify2",
    "review-test",
    "review-ontology-test",
    "e2e_concurrency_test",
    "table_extract_test",
)

_ADMIN_USERNAME = "admin"


class AdminSeedError(Exception):
    """没有 admin 账号，也没有可用于播种的初始密码。"""


async def seed_admin_user(conn: aiosqlite.Connection, admin_token: str | None) -> bool:
    """没有任何 admin 时，用 admin_token 作为初始密码建一个。返回是否播种了。

    已存在时什么都不做——admin_token 是否为空无关紧要，它已经完成使命。
    这保证了改密后重启不会被环境变量覆盖回去；否则"改密码"这个功能是假的。

    没有 admin 又没有 token 时抛异常，让进程起不来。启动成功但无人能登录
    是最坏的形态——运维会以为是自己记错了密码，而不是去看配置。处理方式
    与工具注册表一致（见 app/main.py 的 lifespan）。
    """
    if await count_active_admins(conn) > 0 or await get_admin_user(conn, _ADMIN_USERNAME):
        return False
    if not admin_token:
        raise AdminSeedError(
            "没有管理员账号，且 CUSTOMER_RAG_ADMIN_TOKEN 未配置——"
            "无法播种初始管理员，管理后台将无人能登录。"
        )
    try:
        await create_admin_user(
            conn,
            username=_ADMIN_USERNAME,
            password=admin_token,
            role="admin",
            tenant_id=None,
        )
    except ValueError as exc:
        # 密码长度不合规。原始异常只说"密码至少 8 个字符"，而这里根本没有
        # "用户输入的密码"这回事——值来自环境变量。不翻译的话，运维看到
        # 进程起不来、日志说密码太短，会满世界找哪个表单提交了短密码。
        raise AdminSeedError(
            f"CUSTOMER_RAG_ADMIN_TOKEN 不能用作初始管理员密码：{exc}。"
            "请把它改成一个足够长的随机串后重启。"
        ) from exc
    logger.warning(
        "已用 CUSTOMER_RAG_ADMIN_TOKEN 播种初始管理员 admin。"
        "请尽快在后台修改密码——改密后这个环境变量不再生效。"
    )
    return True


async def disable_stale_test_tenants(conn: aiosqlite.Connection) -> list[str]:
    """把测试残留租户置为 disabled。返回这次真正改动了的租户 id。

    只处理**当前是 active** 的那些，所以第二次启动不会重复上报——那会让
    日志每次启动都刷一遍已经完成的事。

    已知行为：用户手动重新启用某个残留租户后，下次启动会再次禁用它。要
    长期保留其中某个，请改 STALE_TEST_TENANTS 常量。

    不存在的租户直接跳过（全新部署里它们根本没有），更不会把它们建出来。
    """
    # 显式 include_disabled=True 再自己判 status，不依赖 list_tenants 的默认
    # 过滤：靠默认值的话，那个默认哪天改成"全部返回"，这里就会每次启动都
    # 把已经禁用的又禁一遍，日志天天刷同一件事。
    existing = {
        t["tenant_id"]: t["status"] for t in await list_tenants(conn, include_disabled=True)
    }
    disabled: list[str] = []
    for tenant_id in STALE_TEST_TENANTS:
        if existing.get(tenant_id) != "active":
            continue
        await set_tenant_status(conn, tenant_id, "disabled")
        disabled.append(tenant_id)
    if disabled:
        logger.info("已停用测试残留租户：%s", "、".join(disabled))
    return disabled
